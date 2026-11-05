"""GitHub issue #2–#7 的回归测试。

这些测试保留真实的 Client._post()，只替换最底层的 HTTP opener，
这样锁、会话保存、重试和 JSON 解析都按真实路径执行（#2 正是因为替换了整个 _post 才没被发现）。
"""
import contextlib
import email.message
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("kd", ROOT / "kd.py")
kd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kd)


class Resp:
    def __init__(self, body):
        self.body = body.encode("utf-8") if isinstance(body, str) else body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def read(self):
        return self.body


def http_error(code, retry_after=None, body=b""):
    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return kd.urllib.error.HTTPError("https://example.invalid/", code, "err", headers, io.BytesIO(body))


SESSION_LOST = json.dumps([[{"Result": {"ResponseStatus": {
    "IsSuccess": False, "MsgCode": 1, "Errors": [{"Message": "会话信息已丢失，请重新登录"}]}}}]])
LOGIN_OK = json.dumps({"LoginResultType": 1, "Context": {"UserId": 1}})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sleeps = []
        for p in (patch.object(kd.time, "sleep", self.sleeps.append), patch.object(kd, "QUIET", True),
                  patch.object(kd, "CONFIG_DIR", self.tmp.name),
                  patch.object(kd, "CACHE_DIR", os.path.join(self.tmp.name, "cache"))):
            p.start()
            self.addCleanup(p.stop)

    def client(self, responses=(), **cfg):
        base = dict(url="https://example.invalid/k3cloud/", _profile="t", acct_id="a",
                    username="u", password="p", lcid=2052, _rate=1e9, _timeout=5)
        base.update(cfg)
        c = kd.Client(base)
        self.requests = []
        queue = list(responses)

        def fake_open(req, timeout=None):
            self.requests.append(kd.api_name(req.full_url.rsplit("/", 1)[-1]))
            item = queue.pop(0)
            if isinstance(item, BaseException):
                raise item
            return Resp(item)
        c.opener.open = fake_open
        return c


class Issue2DeadlockTests(Base):
    def test_relogin_after_lost_session_does_not_deadlock(self):
        c = self.client([SESSION_LOST, LOGIN_OK, json.dumps([["m"]])])
        result = {}
        t = threading.Thread(target=lambda: result.setdefault("rows", c.call("ExecuteBillQuery", {"data": {}})),
                             daemon=True)
        t.start()
        t.join(timeout=3)
        self.assertFalse(t.is_alive(), "重登时线程卡住了（死锁）")
        self.assertEqual(result["rows"], [["m"]])
        self.assertEqual(self.requests, ["ExecuteBillQuery", "ValidateUser", "ExecuteBillQuery"])


class Issue3PaginationTests(Base):
    def paged_server(self, total, page_cap):
        """模拟服务器：每页最多给 page_cap 行（可能少于请求的 Limit）。"""
        data = [[i] for i in range(total)]
        starts = []

        def call(method, params, retry=True, _relogged=False):
            d = params["data"]
            starts.append(d["StartRow"])
            return data[d["StartRow"]:d["StartRow"] + min(d["Limit"], page_cap)]
        return call, starts

    def test_short_pages_do_not_end_pagination(self):
        c = self.client()
        c.call, starts = self.paged_server(total=12, page_cap=2)
        rows = c.query("X", ["FID"], limit=5, all_pages=True)
        self.assertEqual(rows, [[i] for i in range(12)])
        self.assertEqual(starts, [0, 2, 4, 6, 8, 10, 12])

    def test_iter_query_also_advances_by_returned_rows(self):
        c = self.client()
        c.call, _ = self.paged_server(total=7, page_cap=3)
        self.assertEqual(sum(len(p) for p in c.iter_query("X", ["FID"], limit=5)), 7)

    def test_max_pages_caps_requests(self):
        c = self.client()
        c.call, starts = self.paged_server(total=100, page_cap=10)
        rows = c.query("X", ["FID"], limit=10, all_pages=True, max_pages=3)
        self.assertEqual(len(rows), 30)
        self.assertEqual(len(starts), 3)

    def test_single_page_without_all(self):
        c = self.client()
        c.call, starts = self.paged_server(total=100, page_cap=10)
        self.assertEqual(len(c.query("X", ["FID"], limit=10)), 10)
        self.assertEqual(starts, [0])

    def test_malformed_rows_are_rejected(self):
        c = self.client()
        c.call = lambda m, p, retry=True, _relogged=False: [[1, 2]]
        with self.assertRaises(kd.KDError):
            c.query("X", ["FID"])

    def test_stable_order_uses_primary_keys_of_entities_in_use(self):
        meta = {"PkFieldName": "FID", "Entrys": [
            {"Key": "FBillHead", "EntityType": "HeadEntity", "Fields": [{"Key": "FBillNo"}]},
            {"Key": "FEntry", "EntityType": "EntryEntity", "EntryPkFieldName": "FEntryID",
             "Fields": [{"Key": "FQty"}]},
            {"Key": "FOther", "EntityType": "EntryEntity", "EntryPkFieldName": "FEntryID",
             "Fields": [{"Key": "FNote"}]}]}
        c = SimpleNamespace(metadata=lambda form_id: meta)
        self.assertEqual(kd.stable_order(c, "X", ["FBillNo"]), "FID ASC")
        self.assertEqual(kd.stable_order(c, "X", ["FBillNo", "FMaterial.FNumber", "FQty"]),
                         "FID ASC,FEntry_FEntryID ASC")


class Issue4IsolationTests(Base):
    def test_sessions_and_caches_are_isolated_per_identity(self):
        a = self.client(acct_id="A1", username="alice")
        b = self.client(acct_id="A2", username="alice")
        c = self.client(acct_id="A1", username="bob")
        d = self.client(acct_id="A1", username="alice")
        self.assertEqual(len({a.cookie_path, b.cookie_path, c.cookie_path}), 3)
        self.assertEqual(len({a.cache_dir, b.cache_dir, c.cache_dir}), 3)
        self.assertEqual((a.cookie_path, a.cache_dir), (d.cookie_path, d.cache_dir))

    def test_namespace_does_not_expose_identity(self):
        c = self.client(url="https://secret-host.example.invalid/k3cloud/", acct_id="999888", username="alice")
        for value in ("secret-host", "999888", "alice"):
            self.assertNotIn(value, c.cookie_path)
            self.assertNotIn(value, c.cache_dir)

    @unittest.skipIf(os.name == "nt", "POSIX 权限")
    def test_session_file_is_private(self):
        c = self.client([json.dumps([["m"]])])
        c.call("View", {})
        self.assertEqual(stat.S_IMODE(os.stat(c.cookie_path).st_mode), 0o600)

    @unittest.skipIf(os.name == "nt", "POSIX 权限")
    def test_existing_loose_session_file_is_tightened(self):
        path = self.client().cookie_path
        Path(path).write_text("#LWP-Cookies-2.0\n")
        os.chmod(path, 0o644)
        self.client()
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)


class Issue5RetryTests(Base):
    def test_429_is_retried_honouring_retry_after(self):
        c = self.client([http_error(429, retry_after=7), json.dumps([["m"]])])
        self.assertEqual(c.call("View", {}), [["m"]])
        self.assertEqual(len(self.requests), 2)
        self.assertIn(7, self.sleeps)

    def test_retryable_5xx_gives_up_after_three_attempts(self):
        c = self.client([http_error(503)] * 3)
        with self.assertRaises(kd.KDError) as cm:
            c.call("View", {})
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(cm.exception.code, "network")
        self.assertEqual([s for s in self.sleeps if s >= 1], [1, 2])  # 指数退避

    def test_client_errors_are_not_retried(self):
        c = self.client([http_error(400)])
        with self.assertRaises(kd.KDError) as cm:
            c.call("View", {})
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(cm.exception.code, "server")

    def test_login_is_attempted_once(self):
        c = self.client([kd.urllib.error.URLError("down")] * 3)
        with self.assertRaises(kd.KDError):
            c.login()
        self.assertEqual(len(self.requests), 1)

    def test_direct_call_is_attempted_once(self):
        c = self.client([http_error(503)] * 3)
        with self.assertRaises(kd.KDError):
            c.call("View", {}, retry=False)
        self.assertEqual(len(self.requests), 1)

    def test_403_without_api_root_hints_at_k3cloud_path(self):
        c = self.client([http_error(403)], url="https://example.invalid/")
        with self.assertRaises(kd.KDError) as cm:
            c.call("View", {})
        self.assertIn("/k3cloud/", str(cm.exception))
        self.assertEqual(cm.exception.code, "auth")


def run_cli(*args, env=None, config=None):
    """以子进程运行 kd，返回 (退出码, stdout, stderr)。"""
    with tempfile.TemporaryDirectory() as d:
        if config is not None:
            Path(d, "config.json").write_text(config, encoding="utf-8")
        full_env = {k: v for k, v in os.environ.items() if not k.startswith("KD_")}
        full_env.update({"KD_CONFIG_DIR": d, "PYTHONIOENCODING": "utf-8"}, **(env or {}))
        p = subprocess.run([sys.executable, str(ROOT / "kd.py"), *args], capture_output=True,
                           text=True, encoding="utf-8", env=full_env, timeout=60)
        return p.returncode, p.stdout, p.stderr


class Issue6ContractTests(Base):
    def test_invalid_rate_is_a_config_error(self):
        for rate in ("0", "-1", "abc"):
            with self.subTest(rate=rate):
                code, out, err = run_cli("-q", "query", "BD_UNIT", "-f", "FNumber",
                                         env={"KD_URL": "https://example.invalid/k3cloud/", "KD_RATE": rate})
                self.assertEqual(code, 3, err)
                self.assertEqual(json.loads(out)["error"]["code"], "config")
                self.assertNotIn("Traceback", err)

    def test_invalid_lcid_and_corrupt_config_are_config_errors(self):
        code, out, _ = run_cli("-q", "login", env={"KD_URL": "https://example.invalid/k3cloud/", "KD_LCID": "zh"})
        self.assertEqual((code, json.loads(out)["error"]["code"]), (3, "config"))
        code, out, _ = run_cli("-q", "login", config="{not json")
        self.assertEqual((code, json.loads(out)["error"]["code"]), (3, "config"))

    def test_write_method_is_refused_before_reading_config(self):
        code, out, _ = run_cli("-q", "call", "Save", "BD_MATERIAL", "-d", "{}", env={"KD_RATE": "0"})
        self.assertEqual((code, json.loads(out)["error"]["code"]), (6, "readonly"))

    def run_main(self, argv, client):
        out = io.StringIO()
        with patch.object(kd, "resolve_config", return_value=client.cfg), \
             patch.object(kd, "Client", return_value=client), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            try:
                kd.main(argv)
                status = 0
            except SystemExit as e:
                status = e.code or 0
        return status, out.getvalue()

    def test_call_business_failure_exits_4(self):
        failure = json.dumps({"Result": {"ResponseStatus": {"IsSuccess": False, "MsgCode": 11,
                                                            "Errors": [{"Message": "单据不存在"}]}}})
        status, out = self.run_main(["-q", "call", "View", "BD_MATERIAL", "-d", "{}"], self.client([failure]))
        self.assertEqual(status, 4)
        self.assertEqual(json.loads(out)["error"]["code"], "server")

    def test_login_outputs_minimal_json(self):
        login = json.dumps({"LoginResultType": 1, "Context": {"UserName": "alice", "DataCenterName": "SecretDC"}})
        status, out = self.run_main(["-q", "login"], self.client([login]))
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(out), {"authenticated": True, "profile": "t"})
        self.assertNotIn("SecretDC", out)

    def test_unexpected_exception_is_reported_as_json(self):
        c = self.client()
        with patch.object(kd, "cmd_dbs", side_effect=RuntimeError("boom")):
            status, out = self.run_main(["-q", "dbs"], c)
        self.assertEqual(status, 1)
        self.assertEqual(json.loads(out)["error"]["code"], "internal")


class Issue7PrecisionTests(Base):
    def test_decimals_survive_query_output_unchanged(self):
        c = self.client(['[["SO-1", 1234567890.123456789, 2.50]]'])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            kd.out(c.query("X", ["FBillNo", "FAmount", "FQty"]), "json", ["FBillNo", "FAmount", "FQty"])
        text = out.getvalue()
        self.assertIn("1234567890.123456789", text)
        self.assertIn("2.50", text)
        self.assertEqual(json.loads(text, parse_float=Decimal)[0]["FAmount"], Decimal("1234567890.123456789"))

    def test_strings_that_look_like_internal_markers_are_untouched(self):
        # PR #8 review：旧实现用占位符加全局替换，这类普通字符串会被误改成数字或产生非法 JSON
        for text in ("⁣kd-decimal:123", "⁣kd-decimal:hello", '"quoted"', "kd-decimal:1"):
            with self.subTest(text=text):
                obj = {"FName": text, "FAmount": Decimal("1.10")}
                parsed = json.loads(kd.to_json(obj), parse_float=Decimal)
                self.assertEqual(parsed, obj)

    def test_matches_json_dumps_for_plain_data(self):
        samples = [{}, [], {"a": [1, 2, {"b": None, "c": True}], "d": 'x"y\n', "e": []},
                   [[1, "二"], [2.5, {}]], {1: "int key", "k": {"n": [{}]}}, "s", 3, None]
        for obj in samples:
            for indent in (None, 1, 2):
                with self.subTest(obj=obj, indent=indent):
                    self.assertEqual(kd.to_json(obj, indent), json.dumps(obj, ensure_ascii=False, indent=indent))

    def test_decimals_in_csv(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            kd.out([[Decimal("0.10")]], "csv", ["FAmount"])
        self.assertEqual(out.getvalue().split()[-1], "0.10")


if __name__ == "__main__":
    unittest.main()
