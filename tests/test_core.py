"""核心逻辑的离线测试：不连接金蝶服务器，网络请求由 FakeClient 按脚本返回。"""
import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("kd", Path(__file__).resolve().parents[1] / "kd.py")
kd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kd)


def ok(rows):
    return rows


def fail(message, msg_code=11, as_list=False):
    """金蝶的失败响应。ExecuteBillQuery 出错时会包在 [[...]] 里。"""
    body = {"Result": {"ResponseStatus": {"IsSuccess": False, "MsgCode": msg_code,
                                           "Errors": [{"FieldName": None, "Message": message}]}}}
    return [[body]] if as_list else body


class FakeClient(kd.Client):
    """按顺序返回预设响应；记录每次请求的接口名和参数。"""

    def __init__(self, responses, tmp):
        cfg = dict(url="https://example.invalid/k3cloud/", _profile="test",
                   acct_id="a", username="u", password="p", lcid=2052)
        with patch.object(kd, "CONFIG_DIR", tmp):
            super().__init__(cfg)
        self.min_interval = 0
        self.responses = list(responses)
        self.calls = []

    def _post(self, path, body, retries=2):
        self.calls.append((kd.api_name(path), body))
        if not self.responses:
            raise AssertionError(f"没有预设响应：{kd.api_name(path)}")
        return self.responses.pop(0)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for p in (patch.object(kd.time, "sleep", lambda s: None), patch.object(kd, "QUIET", True)):
            p.start()
            self.addCleanup(p.stop)

    def client(self, *responses):
        return FakeClient(responses, self.tmp.name)


class ReadOnlyTests(Base):
    def test_write_methods_are_refused_without_any_request(self):
        c = self.client()
        for method in ("Save", "BatchSave", "Submit", "Audit", "UnAudit", "Delete",
                       "Push", "Draft", "ExcuteOperation", "Allocate", "WorkflowAudit"):
            with self.subTest(method=method), self.assertRaises(kd.KDError) as cm:
                c.call(method, {})
            self.assertEqual(cm.exception.code, "readonly")
        self.assertEqual(c.calls, [])

    def test_whitelist_contains_only_read_methods(self):
        self.assertEqual(kd.READ_METHODS, {
            "View", "ExecuteBillQuery", "BillQuery", "QueryBusinessInfo",
            "QueryGroupInfo", "GetSysReportData", "AttachmentDownLoad"})


class CallRetryTests(Base):
    def test_rate_limit_is_retried(self):
        throttled = fail("系统对WebAPI接口调用进行了限流……请在3秒后重试。", msg_code=14)
        c = self.client(throttled, throttled, ok([["m"]]))
        self.assertEqual(c.call("ExecuteBillQuery", {"data": {}}), [["m"]])
        self.assertEqual(len(c.calls), 3)

    def test_persistent_rate_limit_gives_up(self):
        throttled = fail("限流，请在1秒后重试。", msg_code=14)
        c = self.client(*[throttled] * 20)
        with self.assertRaises(kd.KDError):
            c.call("View", {})

    def test_lost_session_logs_in_again_once(self):
        lost = fail("会话信息已丢失，请重新登录", msg_code=1, as_list=True)
        login = {"LoginResultType": 1, "Context": {"UserId": 7}}
        c = self.client(lost, login, ok([["m"]]))
        self.assertEqual(c.call("ExecuteBillQuery", {"data": {}}), [["m"]])
        self.assertEqual([name for name, _ in c.calls], ["ExecuteBillQuery", "ValidateUser", "ExecuteBillQuery"])
        self.assertEqual(c.user_id, 7)

    def test_login_failure_is_reported(self):
        c = self.client({"LoginResultType": 0, "Message": "用户名或密码错误"})
        with self.assertRaises(kd.KDError) as cm:
            c.login()
        self.assertEqual(cm.exception.code, "auth")


class ResponseParsingTests(unittest.TestCase):
    def test_find_status_in_all_response_shapes(self):
        self.assertFalse(kd.find_status(fail("x"))["IsSuccess"])
        self.assertFalse(kd.find_status(fail("x", as_list=True))["IsSuccess"])
        self.assertIsNone(kd.find_status([["a", 1]]))

    def test_check_raises_with_server_message(self):
        with self.assertRaises(kd.KDError) as cm:
            kd.check(fail("元数据中标识为FXX的字段不存在", as_list=True))
        self.assertIn("FXX", str(cm.exception))
        self.assertEqual(cm.exception.code, "server")

    def test_api_name(self):
        self.assertEqual(kd.api_name(f"{kd.SVC}.DynamicFormService.View.common.kdsvc"), "View")


class ErrorCodeTests(unittest.TestCase):
    def test_codes_and_exit_status(self):
        cases = {"服务器限流": ("rate_limit", 5), "无法连接服务器（View）：x": ("network", 5),
                 "未配置服务地址": ("config", 3), "登录失败：x": ("auth", 3),
                 "Save 不是只读接口，已拒绝": ("readonly", 6), "字段不存在": ("server", 4)}
        for msg, (code, status) in cases.items():
            with self.subTest(msg=msg):
                e = kd.KDError(msg)
                self.assertEqual(e.code, code)
                self.assertEqual(kd.KDError.EXIT[e.code], status)


META = {
    "PkFieldName": "FID",
    "Entrys": [
        {"Key": "FBillHead", "EntityType": "HeadEntity",
         "Fields": [{"Key": "FBillNo", "Name": [{"Key": 2052, "Value": "单据编号"}]},
                    {"Key": "FCustId", "LookUpObjectFormId": "BD_Customer"},
                    {"Key": "FCreatorId", "LookUpObjectFormId": "SEC_User"}]},
        {"Key": "FFinance", "EntityType": "SubHeadEntity", "Fields": [{"Key": "FAmount"}]},
        {"Key": "FEntry", "EntityType": "EntryEntity", "EntryPkFieldName": "FEntryID",
         "Fields": [{"Key": "FQty"}]},
        {"Key": "FPlan", "EntityType": "SubEntryEntity", "ParentKey": "FEntry",
         "EntryPkFieldName": "FDetailID", "Fields": [{"Key": "FPlanQty"}]},
        {"Key": "FEmpty", "EntityType": "EntryEntity", "Fields": []},
    ],
}


class MetadataTests(unittest.TestCase):
    def test_entity_groups(self):
        groups = {name: (pks, [k for k, _, _ in fields]) for name, pks, fields in kd.entity_groups(META)}
        self.assertEqual(groups["head"], (["FID"], ["FBillNo", "FCustId", "FCreatorId", "FAmount"]))
        self.assertEqual(groups["FEntry"], (["FID", "FEntry_FEntryID"], ["FQty"]))
        self.assertEqual(groups["FPlan"], (["FID", "FEntry_FEntryID", "FPlan_FDetailID"], ["FPlanQty"]))
        self.assertNotIn("FEmpty", groups)

    def test_head_is_kept_when_only_primary_key(self):
        meta = {"PkFieldName": "FUserId", "Entrys": [{"Key": "FBillHead", "EntityType": "HeadEntity", "Fields": []}]}
        self.assertEqual(kd.entity_groups(meta), [("head", ["FUserId"], [])])

    def test_lookup_key(self):
        self.assertEqual(kd.lookup_key("FBillNo", None), "FBillNo")
        self.assertEqual(kd.lookup_key("FCustId", "BD_Customer"), "FCustId.FNumber")
        self.assertEqual(kd.lookup_key("FCreatorId", "SEC_User"), "FCreatorId.FName")


class RobustFieldsTests(Base):
    """robust_fields 用试查 1 行的方式找出服务器不支持的字段。"""

    def fake_server(self, bad, renamed=()):
        """bad: 不存在的字段；renamed: 只支持基础写法的引用字段（X.FNumber 不行，X 行）。
        报错时不点名字段（像 SQL 列名错误那样），逼 robust_fields 走二分。"""
        def post(path, body, retries=2):
            keys = body["data"]["FieldKeys"].split(",")
            for k in keys:
                if k in bad or k in renamed:
                    return fail("Invalid column name 'XYZ'.", as_list=True)
            return [[1] * len(keys)]
        return post

    def test_named_field_is_dropped(self):
        c = self.client(fail("元数据中标识为FBad的字段不存在", as_list=True), ok([[1, 2]]))
        keys, dropped = kd.robust_fields(c, "X", ["FA", "FBad", "FB"])
        self.assertEqual((keys, dropped), (["FA", "FB"], ["FBad"]))

    def test_lookup_falls_back_to_base_field(self):
        c = self.client(fail("元数据中标识为FCustId.FNumber的字段不存在", as_list=True), ok([[1, 2]]))
        keys, dropped = kd.robust_fields(c, "X", ["FA", "FCustId.FNumber"])
        self.assertEqual((keys, dropped), (["FA", "FCustId"], []))

    def test_unnamed_bad_field_is_found_by_bisection(self):
        c = self.client()
        fields = [f"F{i}" for i in range(20)]
        with patch.object(c, "_post", self.fake_server(bad={"F13"})):
            keys, dropped = kd.robust_fields(c, "X", fields)
        self.assertEqual(dropped, ["F13"])
        self.assertEqual(keys, [f for f in fields if f != "F13"])

    def test_missing_table_is_not_a_field_problem(self):
        c = self.client(fail("Invalid object name 't_Missing'.", as_list=True))
        with self.assertRaises(kd.KDError):
            kd.robust_fields(c, "X", ["FA", "FB"])


FORMS = [
    {"id": "SAL_SaleOrder", "name": "销售订单", "type": "100", "ext": False},
    {"id": "V_SALORDER", "name": "销售订单", "type": "400", "ext": False},
    {"id": "BD_Customer", "name": "客户", "type": "400", "ext": False},
    {"id": "CRE_Customer", "name": "客户", "type": "400", "ext": False},
    {"id": "GL_Balance", "name": "科目余额表", "type": "100", "ext": False},
    {"id": "GL_RPT_AccountBalance", "name": "科目余额表", "type": "900", "ext": False},
    {"id": "BD_MATERIAL", "name": "物料", "type": "400", "ext": False},
    {"id": "0a1b2c3d-0000-0000-0000-000000000000", "name": "物料扩展", "type": "400", "ext": True},
]
SCAN = {"BD_Customer": {"行数": 5}, "CRE_Customer": {"行数": 3}}


class ResolveFormTests(unittest.TestCase):
    def setUp(self):
        for name, value in (("load_forms", FORMS), ("scan_status", SCAN)):
            p = patch.object(kd, name, lambda client, refresh=False, v=value: v)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(kd, "QUIET", True)
        p.start()
        self.addCleanup(p.stop)

    def test_form_id_passes_through_case_insensitively(self):
        self.assertEqual(kd.resolve_form(None, "SAL_SaleOrder"), "SAL_SaleOrder")
        self.assertEqual(kd.resolve_form(None, "bd_material"), "BD_MATERIAL")

    def test_same_name_prefers_bill(self):
        self.assertEqual(kd.resolve_form(None, "销售订单"), "SAL_SaleOrder")

    def test_report_command_prefers_report(self):
        self.assertEqual(kd.resolve_form(None, "科目余额表", prefer="900"), "GL_RPT_AccountBalance")
        self.assertEqual(kd.resolve_form(None, "科目余额表"), "GL_Balance")

    def test_unique_fuzzy_match_ignores_extension_forms(self):
        self.assertEqual(kd.resolve_form(None, "物"), "BD_MATERIAL")

    def test_ambiguous_name_lists_candidates_by_row_count(self):
        with self.assertRaises(kd.KDError) as cm:
            kd.resolve_form(None, "客户")
        self.assertEqual(cm.exception.code, "ambiguous")
        self.assertEqual([c["FormId"] for c in cm.exception.data["candidates"]], ["BD_Customer", "CRE_Customer"])

    def test_unknown_name_is_tried_as_is(self):
        self.assertEqual(kd.resolve_form(None, "NOT_IN_CATALOG"), "NOT_IN_CATALOG")


class MainOutputTests(Base):
    """agent 模式（stdout 不是终端）：结果和错误都是 JSON，退出码区分错误类别。"""

    def run_main(self, argv, client):
        out = io.StringIO()
        with patch.object(kd, "resolve_config", return_value=client.cfg), \
             patch.object(kd, "Client", return_value=client), \
             patch.object(kd, "load_forms", lambda c, refresh=False: FORMS), \
             patch.object(kd, "scan_status", lambda c: SCAN), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            try:
                kd.main(argv)
                status = 0
            except SystemExit as e:
                status = e.code or 0
        return status, json.loads(out.getvalue())

    def test_query_outputs_json_records(self):
        c = self.client(ok([["SO-1", "2025-01-01T00:00:00"]]))
        status, data = self.run_main(["-q", "query", "销售订单", "-f", "FBillNo,FDate"], c)
        self.assertEqual(status, 0)
        self.assertEqual(data, [{"FBillNo": "SO-1", "FDate": "2025-01-01T00:00:00"}])

    def test_ambiguous_name_error(self):
        status, data = self.run_main(["-q", "query", "客户", "-f", "FNumber"], self.client())
        self.assertEqual(status, 4)
        self.assertEqual(data["error"]["code"], "ambiguous")
        self.assertEqual(len(data["error"]["candidates"]), 2)

    def test_readonly_error(self):
        status, data = self.run_main(["-q", "call", "Save", "BD_MATERIAL", "-d", "{}"], self.client())
        self.assertEqual(status, 6)
        self.assertEqual(data["error"]["code"], "readonly")

    def test_server_error_message_is_passed_through(self):
        c = self.client(fail("元数据中标识为FNope的字段不存在", as_list=True))
        status, data = self.run_main(["-q", "query", "BD_MATERIAL", "-f", "FNope"], c)
        self.assertEqual(status, 4)
        self.assertIn("FNope", data["error"]["message"])


class ReportTests(Base):
    def test_auto_columns_drop_missing_ones(self):
        c = self.client(fail("标识为FGone的字段不存在", msg_code=9),
                        {"Result": {"IsSuccess": True, "RowCount": 1, "Rows": [["1002", "银行存款"]]}})
        args = SimpleNamespace(form_id="R", schemes=False, fields=None, data=None, scheme="S1",
                               start=0, limit=10, format="json")
        out = io.StringIO()
        with patch.object(kd, "report_columns", return_value=["FID", "FGone", "FName"]), \
             patch.object(kd, "report_schemes", return_value=[]), \
             patch.object(kd, "CACHE_DIR", self.tmp.name), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            kd.cmd_report(args, c)
        self.assertEqual(json.loads(out.getvalue()), [{"FID": "1002", "FName": "银行存款"}])
        self.assertEqual(c.calls[-1][1]["data"]["FieldKeys"], "FID,FName")


class DownloadTests(Base):
    def test_chunks_are_joined(self):
        import base64
        part = lambda b, last, nxt: {"Result": {"ResponseStatus": {"IsSuccess": True}, "FilePart":
                                                base64.b64encode(b).decode(), "IsLast": last, "StartIndex": nxt}}
        c = self.client(part(b"hello ", False, 6), part(b"world", True, 11))
        target = Path(self.tmp.name) / "f.txt"
        with contextlib.redirect_stderr(io.StringIO()):
            kd.cmd_download(SimpleNamespace(file_id="0123456789abcdef0123456789abcdef", output=str(target)), c)
        self.assertEqual(target.read_bytes(), b"hello world")
        self.assertEqual([b["data"]["StartIndex"] for _, b in c.calls], [0, 6])

    def test_server_refusal_is_reported(self):
        refused = {"Result": {"ResponseStatus": {"IsSuccess": False, "MsgCode": 0, "Errors": []},
                              "FilePart": None, "Message": "文件分块下载发生错误，错误提示信息：附件禁止下载。\r\n   在 ..."}}
        c = self.client(refused)
        with self.assertRaises(kd.KDError) as cm:
            kd.cmd_download(SimpleNamespace(file_id="0123456789abcdef0123456789abcdef",
                                            output=str(Path(self.tmp.name) / "x")), c)
        self.assertIn("附件禁止下载", str(cm.exception))
        self.assertNotIn("在 ...", str(cm.exception))


class NetworkPrivacyTests(Base):
    def test_network_error_does_not_include_service_url(self):
        cfg = dict(url="https://secret-tenant.example.invalid/k3cloud/", _profile="t",
                   acct_id="a", username="u", password="p", lcid=2052)
        with patch.object(kd, "CONFIG_DIR", self.tmp.name):
            c = kd.Client(cfg)
        c.min_interval = 0
        with patch.object(c.opener, "open", side_effect=kd.urllib.error.URLError("boom")):
            with self.assertRaises(kd.KDError) as cm:
                c.call("View", {})
        self.assertEqual(cm.exception.code, "network")
        self.assertNotIn("secret-tenant", str(cm.exception))
        self.assertIn("View", str(cm.exception))


class EncodingTests(unittest.TestCase):
    def test_piped_output_uses_utf8_even_if_locale_is_not(self):
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="cp1252")  # 模拟英文 Windows 的管道
        with patch.object(kd.sys, "stdout", stream), patch.object(kd.sys, "stderr", stream):
            kd._utf8_streams()
            print("销售订单", file=kd.sys.stdout)
            stream.flush()
        self.assertEqual(raw.getvalue().decode("utf-8").strip(), "销售订单")


if __name__ == "__main__":
    unittest.main()
