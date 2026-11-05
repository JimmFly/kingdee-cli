#!/usr/bin/env python3
"""kd - 金蝶云星空 WebAPI 只读命令行工具（仅依赖 Python 3 标准库）

只会调用读取类接口（白名单见 READ_METHODS），不会修改任何数据。
配置优先级：环境变量 > ~/.config/kdcli/config.json 中的 profile
"""
__version__ = "0.2.0"

import argparse
import csv
from decimal import Decimal
import getpass
import hashlib
import http.cookiejar
import json
import os
import re
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

CONFIG_DIR = os.path.expanduser(os.environ.get("KD_CONFIG_DIR", "~/.config/kdcli"))
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
CACHE_DIR = os.path.join(CONFIG_DIR, "cache")

SVC = "Kingdee.BOS.WebApi.ServicesStub"
AUTH_APPSECRET = f"{SVC}.AuthService.LoginByAppSecret.common.kdsvc"
AUTH_PASSWORD = f"{SVC}.AuthService.ValidateUser.common.kdsvc"
DATACENTERS = "Kingdee.BOS.ServiceFacade.ServicesStub.Account.AccountService.GetDataCenterList.common.kdsvc"

# 只读白名单：除登录外，只允许调用这些方法
READ_METHODS = {
    "View", "ExecuteBillQuery", "BillQuery", "QueryBusinessInfo",
    "QueryGroupInfo", "GetSysReportData", "AttachmentDownLoad",
}

MODEL_TYPES = {  # BOS_ObjectType.FModelTypeId
    "100": "单据", "400": "基础资料", "900": "报表", "500": "动态表单",
    "600": "套打模板", "700": "转换规则", "1400": "移动", "220": "参数",
}
TYPE_ALIASES = {"bill": "100", "base": "400", "report": "900"}
GUID_RE = re.compile(r"^[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}$", re.I)

CONFIG_KEYS = {
    "url": ("KD_URL", "服务地址，如 https://xxx.ik3cloud.com/k3cloud/"),
    "acct_id": ("KD_ACCT_ID", "数据中心 ID（可用 `kd dbs` 查询）"),
    "username": ("KD_USERNAME", "用户名"),
    "app_id": ("KD_APP_ID", "第三方应用 AppID（可选）"),
    "app_secret": ("KD_APP_SECRET", "第三方应用 AppSecret（可选）"),
    "password": ("KD_PASSWORD", "密码（不用 AppID 时填写）"),
    "lcid": ("KD_LCID", "语言，2052=简体中文"),
}


class KDError(Exception):
    """code: config | auth | not_found | ambiguous | server | rate_limit | network | readonly"""

    EXIT = {"internal": 1, "config": 3, "auth": 3, "not_found": 4, "ambiguous": 4, "server": 4,
            "readonly": 6, "rate_limit": 5, "network": 5}

    def __init__(self, message, code=None, data=None):
        super().__init__(message)
        self.code = code or self._guess(message)
        self.data = data

    @staticmethod
    def _guess(m):
        if "限流" in m:
            return "rate_limit"
        if "无法连接" in m or m.startswith("HTTP "):
            return "network"
        if "登录失败" in m or "缺少配置项" in m or "未配置" in m or "请配置" in m:
            return "config" if "配置" in m and "登录" not in m else "auth"
        if "不是只读接口" in m:
            return "readonly"
        return "server"


QUIET = False


def api_name(path):
    """'Kingdee.BOS...DynamicFormService.View.common.kdsvc' -> 'View'"""
    parts = path.split(".")
    return parts[-3] if len(parts) >= 3 else path


def to_json(obj, indent=None):
    """和 json.dumps(obj, ensure_ascii=False, indent=indent) 输出一致，
    但 Decimal 直接输出为原样的 JSON 数字（不经过 float，位数不丢）。

    逐层自己拼接容器，标量交给 json.dumps；不使用占位符和全局替换，
    所以任何字符串内容都会原样保留。"""
    def enc(o, level):
        if isinstance(o, Decimal):
            if not o.is_finite():
                raise TypeError(f"无法序列化非有限数 {o}")
            return str(o)
        if isinstance(o, dict):
            return wrap("{", "}", [f"{json.dumps(k if isinstance(k, str) else json.dumps(k), ensure_ascii=False)}: "
                                   f"{enc(v, level + 1)}" for k, v in o.items()], level)
        if isinstance(o, (list, tuple)):
            return wrap("[", "]", [enc(v, level + 1) for v in o], level)
        return json.dumps(o, ensure_ascii=False)

    def wrap(open_, close, items, level):
        if not items:
            return open_ + close
        if indent is None:
            return open_ + ", ".join(items) + close
        pad, end = " " * (indent * (level + 1)), " " * (indent * level)
        return open_ + "\n" + ",\n".join(pad + i for i in items) + "\n" + end + close

    return enc(obj, 0)


def log(msg):
    if not QUIET:
        print(msg, file=sys.stderr, flush=True)


# ================================================================ 配置

def load_config_file():
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {"default": "default", "profiles": {}}
    except (OSError, ValueError) as e:
        raise KDError(f"配置文件无法读取（{CONFIG_FILE}）：{e}", code="config")
    if not isinstance(data, dict) or not isinstance(data.get("profiles", {}), dict):
        raise KDError(f"配置文件格式不正确（{CONFIG_FILE}）：应为 {{\"profiles\": {{...}}}}", code="config")
    return data


def _positive_number(name, value, cast=float):
    try:
        v = cast(value)
    except (TypeError, ValueError):
        v = None
    if v is None or v <= 0:
        raise KDError(f"{name} 必须是正数，当前是 {value!r}", code="config")
    return v


def save_config_file(data):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.chmod(CONFIG_FILE, 0o600)


def resolve_config(profile_name):
    data = load_config_file()
    name = profile_name or os.environ.get("KD_PROFILE") or data.get("default", "default")
    cfg = dict(data.get("profiles", {}).get(name, {}))
    for key, (env, _) in CONFIG_KEYS.items():
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    cfg.setdefault("lcid", 2052)
    cfg["lcid"] = int(_positive_number("lcid（KD_LCID）", cfg["lcid"], int))
    cfg["_profile"] = name
    cfg["_rate"] = _positive_number("KD_RATE", os.environ.get("KD_RATE", 450))
    cfg["_timeout"] = _positive_number("KD_TIMEOUT", os.environ.get("KD_TIMEOUT", 180))
    if not cfg.get("url"):
        raise KDError(f"未配置服务地址。请先运行 `kd config init`（profile: {name}）", code="config")
    if not cfg["url"].endswith("/"):
        cfg["url"] += "/"
    return cfg


def identity_namespace(cfg):
    """会话和缓存的命名空间：profile + 地址/账套/用户的摘要。
    同一个 profile 换了账套或用户，就是另一个命名空间，不会串用会话和元数据（#4）。
    文件名里只出现摘要，不暴露地址、账套和用户名。"""
    ident = "\n".join([(cfg.get("url") or "").strip().lower().rstrip("/"),
                       str(cfg.get("acct_id") or ""), str(cfg.get("username") or "")])
    digest = hashlib.sha256(ident.encode("utf-8")).hexdigest()[:12]
    profile = re.sub(r"[^\w.-]", "_", cfg.get("_profile") or "default")
    return f"{profile}-{digest}"


def _private_file(path):
    """会话文件就是登录凭据：保证只有自己能读写，也修正旧文件的宽权限。"""
    try:
        if os.path.exists(path) and (os.stat(path).st_mode & 0o077):
            os.chmod(path, 0o600)
    except OSError:
        pass


# ================================================================ 客户端

RETRYABLE_HTTP = {429, 502, 503, 504}
MAX_ATTEMPTS = 3


class Client:
    def __init__(self, cfg, verbose=False):
        self.cfg = cfg
        self.verbose = verbose
        self.user_id = None
        # 两把锁各管一件事，持有其中一把时不会再去申请另一把，避免死锁（#2）：
        #   login_lock 只用来让会话失效后的重登串行进行；
        #   jar_lock 只保护会话文件的读写，持有期间不做任何网络请求。
        self.login_lock = threading.Lock()
        self.jar_lock = threading.Lock()
        self.rate_lock = threading.Lock()
        self.next_slot = 0.0
        # 服务器限制每分钟 500 次 WebAPI 调用；客户端默认按每分钟 450 次节流
        self.min_interval = 60.0 / cfg.get("_rate", 450)
        self.timeout = cfg.get("_timeout", 180)
        ns = identity_namespace(cfg)
        self.cache_dir = os.path.join(CACHE_DIR, ns)
        os.makedirs(CONFIG_DIR, exist_ok=True)
        self.cookie_path = os.path.join(CONFIG_DIR, f"session-{ns}.txt")
        self.jar = http.cookiejar.LWPCookieJar(self.cookie_path)
        if os.path.exists(self.cookie_path):
            _private_file(self.cookie_path)
            try:
                self.jar.load(ignore_discard=True, ignore_expires=True)
            except Exception:
                pass
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def _throttle(self):
        with self.rate_lock:
            now = time.monotonic()
            wait = self.next_slot - now
            self.next_slot = max(now, self.next_slot) + self.min_interval
        if wait > 0:
            time.sleep(wait)

    def _save_session(self):
        """原子写入会话文件，权限 0600。"""
        with self.jar_lock:
            tmp = f"{self.cookie_path}.{os.getpid()}.{threading.get_ident()}.tmp"
            try:
                self.jar.save(tmp, ignore_discard=True, ignore_expires=True)
                os.chmod(tmp, 0o600)
                os.replace(tmp, self.cookie_path)
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)

    def redact(self, text):
        """把服务地址（完整 URL、主机名）替换成 <server>。
        TLS 证书错误、HTTP 错误正文等都可能带出租户主机名。"""
        text = str(text)
        url = self.cfg.get("url") or ""
        host = urllib.parse.urlsplit(url).hostname or ""
        for secret in sorted({url.rstrip("/"), host}, key=len, reverse=True):
            if secret:
                text = re.sub(re.escape(secret), "<server>", text, flags=re.I)
        return text

    @staticmethod
    def _backoff(attempt, retry_after=None):
        """第 n 次失败后等待的秒数：优先用服务器给的 Retry-After（最多 60 秒），否则 1、2、4…（最多 8 秒）。"""
        if retry_after and str(retry_after).strip().isdigit():
            return min(int(retry_after), 60)
        return min(2 ** (attempt - 1), 8)

    def _http_error(self, path, e):
        detail = e.read().decode("utf-8", "replace")[:500] if e.fp else ""
        hint = ""
        if e.code in (401, 403, 404) and "/k3cloud" not in self.cfg["url"].lower():
            hint = "（服务地址一般要包含部署根路径，常见为 https://<主机>/k3cloud/）"
        code = {429: "rate_limit", 401: "auth", 403: "auth", 404: "config"}.get(
            e.code, "network" if e.code >= 500 else "server")
        return KDError(self.redact(f"HTTP {e.code}{hint}: {detail}"), code=code)

    def _post(self, path, body, attempts=MAX_ATTEMPTS):
        """发请求。attempts > 1 时，网络错误和 HTTP 429/502/503/504 会按退避重试（#5）。"""
        url = self.cfg["url"] + path
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=payload, method="POST",
                                     headers={"Content-Type": "application/json"})
        for attempt in range(1, attempts + 1):
            self._throttle()
            if self.verbose:
                log(f">>> POST {path} ({len(payload)} bytes; body omitted)")
            try:
                with self.opener.open(req, timeout=self.timeout) as resp:
                    text = resp.read().decode("utf-8-sig")
                break
            except urllib.error.HTTPError as e:
                if e.code in RETRYABLE_HTTP and attempt < attempts:
                    wait = self._backoff(attempt, e.headers.get("Retry-After") if e.headers else None)
                    log(f"  HTTP {e.code}，{wait} 秒后重试（{attempt}/{attempts - 1}）…")
                    time.sleep(wait)
                    continue
                raise self._http_error(path, e)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt < attempts:
                    wait = self._backoff(attempt)
                    log(f"  网络错误，{wait} 秒后重试（{attempt}/{attempts - 1}）…")
                    time.sleep(wait)
                    continue
                # 不在错误信息里带出服务地址：agent 模式下错误会写进 stdout，可能被记录到日志
                raise KDError(self.redact(f"无法连接服务器（{api_name(path)}）：{getattr(e, 'reason', e)}"),
                              code="network")
        if self.verbose:
            log(f"<<< response ({len(text)} characters; body omitted)")
        self._save_session()
        try:
            # 小数按 Decimal 解析，金额、数量不经过二进制浮点，位数原样保留（#7）
            return json.loads(text, parse_float=Decimal)
        except json.JSONDecodeError:
            raise KDError(self.redact(f"服务器返回的不是 JSON: {text[:300]}"))

    def login(self):
        c = self.cfg
        for k in ("acct_id", "username"):
            if not c.get(k):
                raise KDError(f"缺少配置项 {k}", code="config")
        # 登录只请求一次：认证失败重试没有意义，还可能触发账号锁定（#5）
        if c.get("app_id") and c.get("app_secret"):
            res = self._post(AUTH_APPSECRET, {
                "acctID": c["acct_id"], "userName": c["username"],
                "appId": c["app_id"], "appSecret": c["app_secret"], "lcid": int(c["lcid"]),
            }, attempts=1)
        elif c.get("password"):
            res = self._post(AUTH_PASSWORD, {
                "acctID": c["acct_id"], "username": c["username"],
                "password": c["password"], "lcid": int(c["lcid"]),
            }, attempts=1)
        else:
            raise KDError("请配置 app_id + app_secret，或 password", code="config")
        if not isinstance(res, dict):
            raise KDError("登录接口返回的格式不符合预期", code="auth")
        self.user_id = (res.get("Context") or {}).get("UserId")
        code = res.get("LoginResultType")
        if code not in (1, -5):
            msg = res.get("Message") or res.get("MessageCode") or json.dumps(res, ensure_ascii=False, default=str)
            raise KDError(f"登录失败（LoginResultType={code}）：{msg}", code="auth")
        return res

    def call(self, method, params, retry=True, _relogged=False):
        """调用只读接口。retry=False 时网络和 HTTP 层只请求一次（kd call 使用）；
        金蝶业务层的「限流」提示无论如何都会按提示的秒数等待后重试。"""
        if method not in READ_METHODS:
            raise KDError(f"{method} 不是只读接口，已拒绝。允许：{', '.join(sorted(READ_METHODS))}",
                          code="readonly")
        attempts = MAX_ATTEMPTS if retry else 1
        for _ in range(20):
            res = self._post(f"{SVC}.DynamicFormService.{method}.common.kdsvc", params, attempts=attempts)
            status = find_status(res)
            msg = error_text(res) if status and not status.get("IsSuccess", True) else ""
            if "限流" not in msg:
                break
            m = re.search(r"(\d+)\s*秒后重试", msg)
            wait = int(m.group(1)) + 1 if m else 15
            log(f"  服务器限流（每分钟 500 次），{wait} 秒后重试…")
            with self.rate_lock:  # 让所有线程一起等
                self.next_slot = max(self.next_slot, time.monotonic() + wait)
            self._throttle()
        else:
            raise KDError("服务器持续限流，请稍后再试", code="rate_limit")
        if status and not status.get("IsSuccess", True) and status.get("MsgCode") == 1 and not _relogged:
            with self.login_lock:  # 只串行化重登；login() 内部只会用到 jar_lock
                self.login()
            return self.call(method, params, retry=retry, _relogged=True)
        return res

    # ---- 便捷方法
    def query(self, form_id, fields, filter_="", order="", start=0, limit=2000, all_pages=False,
              max_pages=None):
        rows = []
        for page in self.iter_query(form_id, fields, filter_, order, start, limit,
                                    max_pages=max_pages if all_pages else 1):
            rows.extend(page)
        return rows

    def iter_query(self, form_id, fields, filter_="", order="", start=0, limit=2000, max_pages=None):
        """逐页产出 ExecuteBillQuery 的结果。

        按实际返回的行数推进偏移，直到遇到空页才停：服务器有时一页给的比 Limit 少，
        但后面还有数据，用「本页不满」判断结束会漏行（#3）。
        max_pages 限制请求页数，到达上限时提示结果可能不完整。"""
        para = {"FormId": form_id, "FieldKeys": ",".join(fields), "FilterString": filter_ or "",
                "OrderString": order or "", "TopRowCount": 0, "StartRow": start, "Limit": limit}
        pages = 0
        while True:
            page = check(self.call("ExecuteBillQuery", {"data": para}))
            if not isinstance(page, list) or any(not isinstance(r, list) or len(r) != len(fields) for r in page):
                raise KDError(f"{form_id} 返回的数据格式不符合预期（应为每行 {len(fields)} 列的列表）")
            pages += 1
            if page:
                yield page
            if not page or limit == 0:  # Limit=0 表示服务器一次返回全部
                return
            if max_pages is not None and pages >= max_pages:
                if max_pages > 1:
                    log(f"  已达到页数上限 {max_pages}，结果可能不完整")
                return
            para["StartRow"] += len(page)

    def metadata(self, form_id, refresh=False):
        path = os.path.join(self.cache_dir, "meta", f"{form_id}.json")
        if not refresh and os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        res = check(self.call("QueryBusinessInfo", {"data": {"FormId": form_id}}))
        meta = res.get("Result", {}).get("NeedReturnData") or {}
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(to_json(meta))
        return meta


def find_status(res):
    if isinstance(res, dict):
        if "ResponseStatus" in res:
            return res["ResponseStatus"]
        if isinstance(res.get("Result"), dict):
            return find_status(res["Result"])
    if isinstance(res, list) and res and isinstance(res[0], list) and res[0] and isinstance(res[0][0], dict):
        return find_status(res[0][0])
    return None


def error_text(res):
    status = find_status(res) or {}
    errs = status.get("Errors") or []
    return "; ".join(f"{e.get('FieldName') or ''} {e.get('Message')}".strip() for e in errs) \
        or to_json(status)


def check(res):
    status = find_status(res)
    if status and not status.get("IsSuccess", True):
        raise KDError(error_text(res))
    return res


# ================================================================ 元数据工具

def lname(names, lcid=2052):
    if isinstance(names, list):
        for n in names:
            if n.get("Key") == lcid:
                return n.get("Value")
        return names[0].get("Value") if names else ""
    return names or ""


def entity_groups(meta):
    """把表单拆成可分别导出的组：表头(含子表头)、每个分录、每个子分录。

    返回 [(组名, 主键字段列表, 业务字段列表[(key, lookup_form, 名称)])]
    """
    pk = meta.get("PkFieldName") or "FID"
    entrys = meta.get("Entrys") or []
    by_key = {e["Key"]: e for e in entrys}

    def fields_of(e):
        return [(f["Key"], f.get("LookUpObjectFormId"), lname(f.get("Name")))
                for f in e.get("Fields") or [] if f.get("Key")]

    head = []
    groups = []
    for e in entrys:
        et = e.get("EntityType")
        if et in ("HeadEntity", "SubHeadEntity") and not e.get("ParentKey"):
            head.extend(fields_of(e))
    groups.append(("head", [pk], head))
    for e in entrys:
        et, key = e.get("EntityType"), e["Key"]
        if et == "EntryEntity":
            groups.append((key, [pk, f"{key}_{e.get('EntryPkFieldName') or 'FEntryID'}"], fields_of(e)))
        elif et == "SubEntryEntity" and e.get("ParentKey") in by_key:
            parent = by_key[e["ParentKey"]]
            groups.append((key, [pk, f"{parent['Key']}_{parent.get('EntryPkFieldName') or 'FEntryID'}",
                                 f"{key}_{e.get('EntryPkFieldName') or 'FDetailID'}"], fields_of(e)))
    return [g for g in groups if g[2] or g[0] == "head"]


def lookup_key(key, lookup_form):
    """引用字段输出编码；用户类字段输出姓名；普通字段原样。"""
    if not lookup_form:
        return key
    return f"{key}.FName" if lookup_form == "SEC_User" else f"{key}.FNumber"


def _try_query(client, form_id, keys):
    res = client.call("ExecuteBillQuery", {"data": {
        "FormId": form_id, "FieldKeys": ",".join(keys), "FilterString": "",
        "OrderString": "", "TopRowCount": 0, "StartRow": 0, "Limit": 1}})
    status = find_status(res)
    return None if not status or status.get("IsSuccess", True) else error_text(res)


def _bisect_bad(client, form_id, keys):
    """报错无法对应到字段（例如 SQL 列名错误）时，二分找出导致失败的那个字段。"""
    while len(keys) > 1:
        half = keys[:len(keys) // 2]
        if _try_query(client, form_id, half):
            keys = half
        else:
            rest = keys[len(keys) // 2:]
            if not _try_query(client, form_id, rest):
                return None  # 字段组合才出错，无法定位
            keys = rest
    return keys[0]


def robust_fields(client, form_id, keys, max_tries=40):
    """不断试查 1 行，把服务器报错的字段降级（X.FNumber -> X）或剔除，返回可用字段列表。"""
    keys = list(dict.fromkeys(keys))
    dropped = []
    for _ in range(max_tries):
        if not keys:
            break
        msg = _try_query(client, form_id, keys)
        if not msg:
            return keys, dropped
        hit = sorted([k for k in keys if k in msg], key=len, reverse=True) or \
            sorted([k for k in keys if k.split(".")[0] in msg], key=len, reverse=True)
        if hit:
            bad = hit[0]
        else:
            if "Invalid object name" in msg or "业务对象不存在" in msg or len(keys) == 1:
                raise KDError(msg)  # 整张表有问题，不是字段问题
            bad = _bisect_bad(client, form_id, keys)
            if not bad:
                raise KDError(msg)
        i = keys.index(bad)
        if "." in bad and bad.split(".")[0] not in keys:
            keys[i] = bad.split(".")[0]
        else:
            keys.pop(i)
            dropped.append(bad)
    raise KDError("字段过多无法自动修复")


# ================================================================ 表单目录

def load_forms(client, refresh=False):
    path = os.path.join(client.cache_dir, "forms.json")
    if not refresh and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    log("正在下载表单目录（约 1 万条，首次需要几十秒）…")
    subs = {r[0]: (r[1], r[2]) for r in client.query(
        "BOS_SubSystem", ["FID", "FName", "FTopClassId.FName"], order="FID ASC", all_pages=True)}
    forms = []
    for fid, name, mtype, sub in client.query(
            "BOS_ObjectType", ["FID", "FName", "FModelTypeId", "FSubSystemId"], order="FID ASC", all_pages=True):
        sname, cloud = subs.get(sub, ("", ""))
        forms.append({"id": fid, "name": name or "", "type": str(mtype), "cloud": cloud or "",
                      "subsystem": sname or "", "ext": bool(GUID_RE.match(fid or ""))})
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(forms, f, ensure_ascii=False)
    return forms


def filter_forms(forms, types=None, keyword=None, cloud=None, include_ext=False):
    want = None
    if types:
        want = {TYPE_ALIASES.get(t, t) for t in types}
    out_ = []
    for f in forms:
        if f["ext"] and not include_ext:
            continue
        if want and f["type"] not in want:
            continue
        if cloud and cloud not in f["cloud"] and cloud not in f["subsystem"]:
            continue
        if keyword and keyword.lower() not in (f["id"] + " " + f["name"]).lower():
            continue
        out_.append(f)
    return sorted(out_, key=lambda f: (f["cloud"], f["subsystem"], f["id"]))


# ================================================================ 输出

def _w(s):
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in str(s))


def out(data, fmt="json", columns=None, file=None):
    file = file or sys.stdout
    if fmt == "json" or columns is None:
        if columns is not None:
            data = [dict(zip(columns, r)) for r in data]
        print(to_json(data, indent=2), file=file)
        return
    rows = [["" if v is None else v for v in r] for r in data]
    if fmt == "csv":
        w = csv.writer(file)
        w.writerow(columns)
        w.writerows(rows)
        return
    widths = [max([_w(c)] + [_w(r[i]) for r in rows if i < len(r)]) for i, c in enumerate(columns)]
    widths = [min(w, 60) for w in widths]

    def cell(v, n):
        s = str(v).replace("\n", " ")
        while _w(s) > n:
            s = s[:-2] + "…"
        return s + " " * (n - _w(s))
    print("  ".join(cell(c, w) for c, w in zip(columns, widths)), file=file)
    print("  ".join("-" * w for w in widths), file=file)
    for r in rows:
        print("  ".join(cell(v, w) for v, w in zip(r, widths)), file=file)
    log(f"\n({len(rows)} 行)")


def split_list(s):
    return [x.strip() for x in s.split(",") if x.strip()] if s else []


def read_json_arg(arg):
    if arg is None:
        return {}
    text = sys.stdin.read() if arg == "-" else open(arg[1:], encoding="utf-8").read() \
        if arg.startswith("@") else arg
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise KDError(f"--data 不是合法 JSON：{e}")


# ================================================================ 命令

def cmd_config_init(args):
    data = load_config_file()
    name = args.profile or data.get("default", "default")
    cur = data.setdefault("profiles", {}).setdefault(name, {})
    print(f"配置 profile「{name}」（回车保留当前值）")
    for key, (_, desc) in CONFIG_KEYS.items():
        old = cur.get(key, 2052 if key == "lcid" else "")
        shown = "******" if old and key in ("app_secret", "password") else old
        prompt = f"{desc} [{shown}]: "
        val = (getpass.getpass(prompt) if key in ("app_secret", "password") else input(prompt)).strip()
        if val:
            cur[key] = int(val) if key == "lcid" else val
    data.setdefault("default", name)
    save_config_file(data)
    print(f"已保存到 {CONFIG_FILE}")


def cmd_config_show(args):
    data = json.loads(json.dumps(load_config_file()))
    for p in data.get("profiles", {}).values():
        for k in ("app_secret", "password"):
            if p.get(k):
                p[k] = "******"
    print(json.dumps(data, ensure_ascii=False, indent=2))


def cmd_config_use(args):
    data = load_config_file()
    if args.name not in data.get("profiles", {}):
        raise KDError(f"没有 profile：{args.name}")
    data["default"] = args.name
    save_config_file(data)
    print(f"默认 profile 已切换为 {args.name}")


def cmd_dbs(args, client):
    res = client._post(DATACENTERS, {})
    rows = [[d.get("Id"), d.get("Number"), d.get("Name")] for d in (res if isinstance(res, list) else [])]
    out(rows, args.format, ["acct_id", "编码", "名称"])


def cmd_login(args, client):
    client.login()
    # 只确认登录结果，不默认输出用户名、数据中心等环境信息（#6）
    if args.json_output:
        print(to_json({"authenticated": True, "profile": client.cfg["_profile"]}))
    else:
        print(f"登录成功（profile: {client.cfg['_profile']}）")


def cmd_forms(args, client):
    forms = filter_forms(load_forms(client, args.refresh), split_list(args.type) or ["bill", "base", "report"],
                         args.keyword, args.cloud, args.ext)
    if args.tree:
        last = (None, None)
        for f in forms:
            if f["cloud"] != last[0]:
                print(f"{f['cloud'] or '(未分类)'}")
            if (f["cloud"], f["subsystem"]) != last:
                print(f"  {f['subsystem'] or '(未分类)'}")
            last = (f["cloud"], f["subsystem"])
            print(f"    {f['id']:<36} {f['name']}  [{MODEL_TYPES.get(f['type'], f['type'])}]")
        log(f"\n({len(forms)} 个表单)")
        return
    rows = [[f["cloud"], f["subsystem"], f["id"], f["name"], MODEL_TYPES.get(f["type"], f["type"])] for f in forms]
    out(rows, args.format, ["云", "子系统", "FormId", "名称", "类型"])


def cmd_fields(args, client):
    meta = client.metadata(args.form_id, refresh=args.refresh)
    if args.raw:
        return out(meta)
    if args.ops:
        rows = [[o.get("OperationNumber"), lname(o.get("OperationName")),
                 o.get("OperationNumber") in READ_METHODS] for o in meta.get("Operations", [])]
        return out(rows, args.format, ["操作", "名称", "只读"])
    rows = []
    for e in meta.get("Entrys", []):
        for f in e.get("Fields") or []:
            rows.append([e["Key"], e.get("EntityType", ""), f.get("Key"), lname(f.get("Name")),
                         f.get("LookUpObjectFormId") or "", bool(f.get("MustInput"))])
    log(f"{args.form_id}  {lname(meta.get('Name'))}  主键：{meta.get('PkFieldName')}  "
        f"（查某分录全部字段：kd query {args.form_id} -f '*实体名'）")
    out(rows, args.format, ["实体", "实体类型", "字段", "名称", "引用表单", "必填"])


def resolve_form(client, spec, prefer=None):
    """表单可以写 FormId，也可以写中文名（精确或唯一的模糊匹配）。"""
    forms = load_forms(client)
    ids = {f["id"] for f in forms}
    if spec in ids:
        return spec
    lower = {f["id"].lower(): f["id"] for f in forms}
    if spec.lower() in lower:
        return lower[spec.lower()]
    readable = scan_status(client)
    cands = [f for f in forms if not f["ext"] and f["type"] in ("100", "400", "900")]
    rank = {"900": 0, "100": 1, "400": 2} if prefer == "900" else {"100": 0, "400": 1, "900": 2}
    if prefer == "900":  # report 命令只在报表里找
        cands = [f for f in cands if f["type"] == "900"] or cands
    exact = [f for f in cands if f["name"] == spec]
    if len(exact) > 1:  # 同名：单据 > 基础资料 > 报表，再优先有数据的
        best = min(rank[f["type"]] for f in exact)
        exact = [f for f in exact if rank[f["type"]] == best]
        if len(exact) > 1 and readable:
            exact = [f for f in exact if readable.get(f["id"], {}).get("行数")] or exact
    fuzzy = [f for f in cands if spec in f["name"]]
    for hit in (exact, fuzzy):
        if len(hit) == 1:
            log(f"（{spec} → {hit[0]['id']}）")
            return hit[0]["id"]
        if hit:
            hit.sort(key=lambda f: (-(readable.get(f["id"], {}).get("行数") or 0), rank[f["type"]]))
            lines = "\n".join(f"  {f['id']:<32} {f['name']}  [{MODEL_TYPES.get(f['type'], f['type'])}]"
                              + (f"  {readable[f['id']]['行数']} 行" if readable.get(f["id"], {}).get("行数") else "")
                              for f in hit[:15])
            raise KDError(f"「{spec}」匹配到 {len(hit)} 个表单，请用 FormId：\n{lines}"
                          + ("\n  …（用 kd forms -k 查看全部）" if len(hit) > 15 else ""),
                          code="ambiguous",
                          data={"candidates": [{"FormId": f["id"], "名称": f["name"],
                                                "类型": MODEL_TYPES.get(f["type"], f["type"]),
                                                "行数": readable.get(f["id"], {}).get("行数")} for f in hit[:30]]})
    return spec  # 不在目录里也照常尝试


def scan_status(client):
    path = os.path.join(client.cache_dir, "scan.json")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fp:
        return {r["FormId"]: r for r in json.load(fp)}


def entity_fields(client, form_id, entity="head", refresh=False):
    """某实体可查询的全部字段（结果缓存，之后查询不再试探）。"""
    path = os.path.join(client.cache_dir, "fields", f"{form_id}__{entity}.json")
    if not refresh and os.path.exists(path):
        with open(path, encoding="utf-8") as fp:
            d = json.load(fp)
        return d["keys"], d["dropped"]
    meta = client.metadata(form_id)
    groups = entity_groups(meta)
    for name, pks, fields in groups:
        if name == entity:
            keys, dropped = robust_fields(client, form_id, pks + [lookup_key(k, lk) for k, lk, _ in fields])
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fp:
                json.dump({"keys": keys, "dropped": dropped}, fp, ensure_ascii=False)
            return keys, dropped
    raise KDError(f"{form_id} 没有实体 {entity}。可选：{', '.join(g[0] for g in groups) or '无'}")


def resolve_field_arg(client, form_id, spec):
    """不填或 '*' = 全部表头字段；'*FEntity' = 某分录全部字段；否则逗号分隔的字段。"""
    spec = spec or "*"
    if not spec.startswith("*"):
        return split_list(spec), []
    return entity_fields(client, form_id, spec[1:] or "head")


def stable_order(client, form_id, fields):
    """分页用的稳定排序：表头主键，加上查询涉及到的分录/子分录内码，保证每行顺序唯一（#3）。"""
    try:
        groups = entity_groups(client.metadata(form_id))
    except KDError:
        return ""
    used = {f.split(".")[0] for f in fields}
    keys = []
    for _, pks, flds in groups:
        if not keys or used & {k for k, _, _ in flds} or used & set(pks):
            for k in pks:
                if k not in keys:
                    keys.append(k)
    return ",".join(f"{k} ASC" for k in keys)


def cmd_query(args, client):
    form_id = args.form_id
    fields, dropped = resolve_field_arg(client, form_id, args.fields)
    if dropped and args.verbose_fields:
        log(f"以下字段服务器不支持查询，已跳过：{', '.join(dropped)}")
    order = args.order or stable_order(client, form_id, fields)
    try:
        rows = client.query(form_id, fields, args.filter, order, args.start, args.limit, args.all, args.max_pages)
    except KDError as e:
        if args.order or not order or e.code != "server":
            raise
        # 个别表单的主键不能用于排序：退回服务器默认顺序
        log(f"  无法按主键排序（{e}），改用服务器默认顺序，翻页结果可能不稳定")
        rows = client.query(form_id, fields, args.filter, "", args.start, args.limit, args.all, args.max_pages)
    out(rows, args.format, fields)


def check_one(client, form_id):
    meta = client.metadata(form_id)
    res = []
    for name, _, _ in entity_groups(meta):
        try:
            keys, dropped = entity_fields(client, form_id, name)
            if not keys:
                raise KDError("无可查询字段")
            rows = client.query(form_id, keys, limit=1)
            res.append((name, "ok", len(keys), len(dropped), len(rows)))
        except KDError as e:
            res.append((name, str(e)[:150], 0, 0, 0))
    return res


def cmd_check(args, client):
    if args.form_ids:
        ids = [resolve_form(client, i) for i in args.form_ids]
    else:
        st = scan_status(client)
        if not st:
            raise KDError("请先运行 `kd scan`")
        ids = [i for i, r in st.items() if r["状态"] == "ok" and (r["行数"] or args.include_empty)]
    log(f"检查 {len(ids)} 个表单的全部查询入口（表头+各分录，并发 {args.jobs}）…")
    client.login()
    rows = []
    with ThreadPoolExecutor(args.jobs) as ex:
        futs = {ex.submit(check_one, client, i): i for i in ids}
        for n, fu in enumerate(as_completed(futs), 1):
            fid = futs[fu]
            try:
                for ent, st_, nk, nd, nr in fu.result():
                    rows.append([fid, ent, st_, nk, nd])
            except KDError as e:
                rows.append([fid, "", str(e)[:150], 0, 0])
            if n % 100 == 0 or n == len(ids):
                log(f"  {n}/{len(ids)}")
    rows.sort(key=lambda r: (r[2] != "ok", r[0], r[1] != "head", r[1]))
    path = os.path.join(client.cache_dir, "check.json")
    merged = rows
    if args.form_ids and os.path.exists(path):  # 部分检查：合并进已有结果
        with open(path, encoding="utf-8") as fp:
            merged = [r for r in json.load(fp) if r[0] not in set(ids)] + rows
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(merged, fp, ensure_ascii=False)
    bad = [r for r in rows if r[2] != "ok"]
    allbad = [r for r in merged if r[2] != "ok"]
    out(bad if not args.show_all else rows, args.format, ["FormId", "实体", "状态", "可查字段数", "跳过字段数"])
    log(f"本次：入口 {len(rows)} 个，可查询 {len(rows) - len(bad)} 个，失败 {len(bad)} 个。")
    log(f"合计：入口 {len(merged)} 个（{len({r[0] for r in merged})} 个表单），"
        f"可查询 {len(merged) - len(allbad)} 个，失败 {len(allbad)} 个。结果：{path}")


def cmd_view(args, client):
    if not (args.number or args.id):
        raise KDError("需要 --number 或 --id")
    para = {"CreateOrgId": 0, "Number": args.number or "", "Id": args.id or ""}
    out(check(client.call("View", {"formid": args.form_id, "data": para})))


def cmd_groups(args, client):
    res = check(client.call("QueryGroupInfo", {"data": {
        "FormId": args.form_id, "GroupFieldKey": args.group_field or "", "GroupPkIds": "", "Ids": ""}}))
    data = res.get("Result", {}).get("NeedReturnData") or []
    rows = [[d.get("FID"), d.get("FNUMBER"), d.get("FNAME"), d.get("FPARENTID")] for d in data]
    out(rows, args.format, ["FID", "编码", "名称", "上级FID"])


def report_schemes(client, form_id):
    rows = client.query("BOS_FilterScheme", ["FSCHEMEID", "FSchemeName", "FUserID", "FIsShare", "FIsDefault", "FScheme"],
                        filter_=f"FFormID='{form_id}'", order="FSCHEMEID ASC", all_pages=True)
    me = getattr(client, "user_id", None)
    # 优先：有内容的方案（系统空白方案没有过滤条件，查出来常是 0 行）> 自己的 > 共享的 > 默认
    rows.sort(key=lambda r: (not r[5], r[2] != me, not r[3], not r[4]))
    return [r[:5] for r in rows]


def report_columns(client, form_id):
    """从该报表所有已保存过滤方案的 ColumnSetting 里收集列名（按显示顺序）。"""
    path = os.path.join(client.cache_dir, "report_cols", f"{form_id}.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fp:
            return json.load(fp)
    order = {}
    for (sch,) in client.query("BOS_FilterScheme", ["FScheme"], f"FFormID='{form_id}'",
                               order="FSCHEMEID ASC", all_pages=True):
        m = re.search(r"<ColumnSetting>(.*?)</ColumnSetting>", sch or "", re.S)
        if not m:
            continue
        try:
            for c in json.loads(m.group(1)):
                if c.get("F"):
                    order.setdefault(c["F"], c.get("I", 999))
        except ValueError:
            pass
    return sorted(order, key=order.get)


def save_report_columns(client, form_id, cols):
    path = os.path.join(client.cache_dir, "report_cols", f"{form_id}.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(cols, fp, ensure_ascii=False)


def cmd_report(args, client):
    if args.schemes:
        rows = report_schemes(client, args.form_id)
        return out([[r[0], r[1], {-1: "系统"}.get(r[2], r[2]), "是" if r[3] else "", "是" if r[4] else ""]
                    for r in rows], args.format, ["SchemeId", "方案名", "创建人ID", "共享", "默认"])
    data = read_json_arg(args.data)
    auto_cols = False
    if args.fields:
        data["FieldKeys"] = args.fields
    if not data.get("FieldKeys"):
        cols = report_columns(client, args.form_id)
        if cols:
            data["FieldKeys"] = ",".join(cols)
            auto_cols = True
    if not data.get("FieldKeys"):
        raise KDError("这个报表还不知道有哪些列，请用 -f 指定列字段。\n"
                      "  获取列名的办法：在金蝶网页打开该报表 → 过滤 → 保存一个过滤方案，"
                      "之后 kd report 会自动从方案里读取列。")
    scheme = args.scheme
    if not scheme and "SchemeId" not in data:
        rows = report_schemes(client, args.form_id)
        if rows:
            scheme = rows[0][0]
            log(f"（使用过滤方案「{rows[0][1]}」{scheme}；--schemes 查看全部）")
    elif scheme:
        for r in report_schemes(client, args.form_id):
            if scheme == r[1]:
                scheme = r[0]
    data.setdefault("SchemeId", scheme or "")
    data.setdefault("StartRow", args.start)
    data.setdefault("Limit", args.limit)
    data.setdefault("IsVerifyBaseDataField", "false")
    data.setdefault("Model", {})
    for _ in range(60):
        res = client.call("GetSysReportData", {"formid": args.form_id, "data": data})
        st = find_status(res)
        msg = error_text(res) if st and not st.get("IsSuccess", True) else ""
        m = re.search(r"标识为(\S+?)的字段不存在", msg)
        if auto_cols and m and m.group(1) in data["FieldKeys"].split(","):
            data["FieldKeys"] = ",".join(k for k in data["FieldKeys"].split(",") if k != m.group(1))
            continue
        break
    check(res)
    if auto_cols:
        save_report_columns(client, args.form_id, split_list(data["FieldKeys"]))
    result = res.get("Result", {})
    rows = result.get("Rows") or []
    cols = split_list(data["FieldKeys"])
    if args.format == "json" and not rows:
        return out(result)
    log(f"（报表共 {result.get('RowCount', len(rows))} 行）")
    out(rows, args.format, cols)


def cmd_attachments(args, client):
    cond = []
    if args.form_id:
        cond.append(f"FBillType='{args.form_id}'")
    if args.number:
        cond.append(f"FBillNo='{args.number}'")
    if args.id:
        cond.append(f"FInterID='{args.id}'")
    fields = ["FID", "FBillType", "FBillNo", "FInterID", "FAttachmentName", "FExtName",
              "FCreateTime", "FCreateMen.FName", "FFileId"]
    rows = client.query("BOS_Attachment", fields, " and ".join(cond + ["FFileId<>' '"]),
                        "FCreateTime DESC,FID DESC", 0, args.limit, args.all)
    out(rows, args.format, ["附件ID", "表单", "单据编号", "单据内码", "文件名", "类型", "上传时间", "上传人", "FileId"])


def cmd_download(args, client):
    import base64
    file_id = args.file_id
    name = args.output
    if not file_id.isalnum() or len(file_id) < 20:  # 传的是附件ID（BOS_Attachment.FID）
        rows = client.query("BOS_Attachment", ["FFileId", "FAttachmentName"], f"FID={int(file_id)}")
        if not rows:
            raise KDError(f"没有附件 {file_id}")
        file_id, fname = rows[0]
        name = name or fname
    if not name:
        rows = client.query("BOS_Attachment", ["FAttachmentName"], f"FFileId='{file_id}'")
        name = rows[0][0] if rows else file_id
    start, chunks = 0, []
    while True:
        res = client.call("AttachmentDownLoad", {"data": {"FileId": file_id, "StartIndex": start}})
        r = res.get("Result") or {}
        part = r.get("FilePart")
        if not part:
            msg = r.get("Message") or error_text(res)
            msg = msg.split("\r\n")[0].split("错误提示信息：")[-1]
            raise KDError(f"服务器拒绝下载：{msg}")
        chunks.append(base64.b64decode(part))
        if r.get("IsLast"):
            break
        start = r.get("StartIndex") or (start + len(chunks[-1]))
    data = b"".join(chunks)
    with open(name, "wb") as fp:
        fp.write(data)
    log(f"已保存 {name}（{len(data)} 字节）")


def cmd_call(args, client):
    data = read_json_arg(args.data)
    params = {"data": data} if args.form_id is None else {"formid": args.form_id, "data": data}
    # 直接调用只请求一次，不做网络层重试；业务失败按统一的错误格式和退出码返回（#5 #6）
    out(check(client.call(args.method, params, retry=False)))


# ---------------------------------------------------------------- scan / export

def scan_one(client, f):
    rec = {"FormId": f["id"], "名称": f["name"], "类型": MODEL_TYPES.get(f["type"], f["type"]),
           "云": f["cloud"], "子系统": f["subsystem"], "行数": "", "状态": ""}
    try:
        meta = client.metadata(f["id"])
        pk = meta.get("PkFieldName")
        if not pk:
            rec["状态"] = "无主键（非数据表单）"
            return rec
        key = pk
        probe = client.call("ExecuteBillQuery", {"data": {"FormId": f["id"], "FieldKeys": pk, "Limit": 1,
                                                           "FilterString": "", "OrderString": "",
                                                           "TopRowCount": 0, "StartRow": 0}})
        if find_status(probe) and not find_status(probe).get("IsSuccess", True):
            # 主键不可查询时，改用表头中第一个可查询的字段计数
            groups = entity_groups(meta)
            head_keys = [k for k, _, _ in groups[0][2]] if groups and groups[0][0] == "head" else []
            try:
                ok_keys, _ = robust_fields(client, f["id"], head_keys[:30])
            except KDError:
                ok_keys = []
            if not ok_keys:
                rec["状态"] = "无可查询字段（模板/非数据表单）"
                return rec
            key = ok_keys[0]
        n = 0
        for page in client.iter_query(f["id"], [key], order=f"{key} ASC", limit=10000):
            n += len(page)
        rec["行数"] = n
        rec["状态"] = "ok"
    except KDError as e:
        rec["状态"] = str(e)[:200]
    return rec


def cmd_scan(args, client):
    forms = select_forms(args, client)
    log(f"扫描 {len(forms)} 个表单（并发 {args.jobs}）…")
    client.login()
    results = []
    with ThreadPoolExecutor(args.jobs) as ex:
        futs = [ex.submit(scan_one, client, f) for f in forms]
        for i, fu in enumerate(as_completed(futs), 1):
            r = fu.result()
            results.append(r)
            if i % 50 == 0 or i == len(forms):
                log(f"  {i}/{len(forms)}")
    path = os.path.join(client.cache_dir, "scan.json")
    if args.form_ids and os.path.exists(path):  # 只扫了部分表单：合并进已有结果
        with open(path, encoding="utf-8") as fp:
            old = {r["FormId"]: r for r in json.load(fp)}
        for r in results:
            prev = old.get(r["FormId"], {})
            r.update({k: prev[k] for k in ("名称", "类型", "云", "子系统") if prev.get(k) and not r[k]})
            old[r["FormId"]] = r
        merged = list(old.values())
    else:
        merged = results
    merged.sort(key=lambda r: (r["状态"] != "ok", -(r["行数"] or 0)))
    results.sort(key=lambda r: (r["状态"] != "ok", -(r["行数"] or 0)))
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(merged, fp, ensure_ascii=False)
    shown = results if args.show_all else [r for r in results if r["状态"] == "ok" and r["行数"]]
    cols = ["FormId", "名称", "类型", "云", "子系统", "行数", "状态"]
    out([[r[c] for c in cols] for r in shown], args.format, cols)
    ok = [r for r in merged if r["状态"] == "ok"]
    log(f"（合计）可读 {len(ok)} 个，其中有数据 {sum(1 for r in ok if r['行数'])} 个，"
        f"共 {sum(r['行数'] or 0 for r in ok)} 行；失败 {len(merged) - len(ok)} 个。结果已存 {path}")


def export_one(client, form_id, outdir, fmt, force):
    fdir = os.path.join(outdir, form_id)
    done_flag = os.path.join(fdir, ".done")
    if os.path.exists(done_flag) and not force:
        return form_id, "已存在，跳过", 0
    os.makedirs(fdir, exist_ok=True)
    meta = client.metadata(form_id)
    total = 0
    notes = []
    with open(os.path.join(fdir, "_fields.json"), "w", encoding="utf-8") as fp:
        fp.write(to_json(meta, indent=1))
    for name, pks, fields in entity_groups(meta):
        keys = pks + [lookup_key(k, lk) for k, lk, _ in fields]
        titles = {k: t for k, _, t in fields}
        try:
            keys, dropped = robust_fields(client, form_id, keys)
        except KDError as e:
            notes.append(f"{name}: {e}")
            continue
        if dropped:
            notes.append(f"{name} 跳过字段 {','.join(dropped)}")
        header = [f"{k}|{titles.get(k.split('.')[0], '')}".rstrip("|") for k in keys]
        path = os.path.join(fdir, f"{name}.{fmt}")
        n = 0
        with open(path, "w", encoding="utf-8-sig" if fmt == "csv" else "utf-8", newline="") as fp:
            w = csv.writer(fp) if fmt == "csv" else None
            if w:
                w.writerow(header)
            for page in client.iter_query(form_id, keys, order=",".join(f"{k} ASC" for k in pks if k in keys)):
                for r in page:
                    if w:
                        w.writerow(["" if v is None else v for v in r])
                    else:
                        fp.write(to_json(dict(zip(keys, r))) + "\n")
                n += len(page)
        if n == 0:
            os.remove(path)
        total += n
    with open(done_flag, "w") as fp:
        fp.write(json.dumps({"rows": total, "notes": notes}, ensure_ascii=False))
    return form_id, "; ".join(notes) or "ok", total


def select_forms(args, client):
    if args.form_ids:
        return [{"id": i, "name": "", "type": "", "cloud": "", "subsystem": ""} for i in args.form_ids]
    if getattr(args, "from_scan", False):
        path = os.path.join(client.cache_dir, "scan.json")
        if not os.path.exists(path):
            raise KDError("还没有扫描结果，请先运行 `kd scan`")
        with open(path, encoding="utf-8") as fp:
            return [{"id": r["FormId"], "name": r["名称"]} for r in json.load(fp)
                    if r["状态"] == "ok" and r["行数"]]
    return filter_forms(load_forms(client), split_list(args.type) or ["bill", "base"],
                        args.keyword, args.cloud, False)


def cmd_export(args, client):
    if not (args.form_ids or args.from_scan or args.all_forms):
        raise KDError("请指定表单 ID，或用 --from-scan（导出 scan 发现有数据的全部表单）/ --all-forms")
    forms = select_forms(args, client)
    os.makedirs(args.out, exist_ok=True)
    log(f"导出 {len(forms)} 个表单到 {args.out}（并发 {args.jobs}）…")
    client.login()
    summary = []
    with ThreadPoolExecutor(args.jobs) as ex:
        futs = {ex.submit(export_one, client, f["id"], args.out, args.format, args.force): f for f in forms}
        for i, fu in enumerate(as_completed(futs), 1):
            f = futs[fu]
            try:
                fid, note, n = fu.result()
            except KDError as e:
                fid, note, n = f["id"], f"失败：{e}"[:300], 0
            summary.append([fid, f.get("name", ""), n, note])
            log(f"  [{i}/{len(forms)}] {fid}: {n} 行 {'' if note == 'ok' else note[:120]}")
    with open(os.path.join(args.out, "_summary.csv"), "w", encoding="utf-8-sig", newline="") as fp:
        w = csv.writer(fp)
        w.writerow(["FormId", "名称", "行数", "备注"])
        w.writerows(sorted(summary))
    log(f"完成，共 {sum(r[2] for r in summary)} 行。汇总：{os.path.join(args.out, '_summary.csv')}")


# ================================================================ main

def build_parser():
    p = argparse.ArgumentParser(prog="kd", description="金蝶云星空 WebAPI 只读命令行工具")
    p.add_argument("--version", action="version", version=f"kd {__version__}")
    p.add_argument("-p", "--profile", help="使用哪个配置 profile")
    p.add_argument("-v", "--verbose", action="store_true", help="打印请求方法和响应大小（不含地址、请求及响应正文）")
    p.add_argument("-q", "--quiet", action="store_true", help="不输出提示信息（只输出结果）")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="命令")

    def fmt(sp, default=None, choices=("table", "json", "csv")):
        sp.add_argument("-o", "--format", choices=choices, default=default,
                        help="输出格式；默认：终端里 table，被程序/管道调用时 json（也可设 KD_FORMAT）")

    c = sub.add_parser("config", help="管理配置").add_subparsers(dest="sub", required=True)
    c.add_parser("init", help="交互式填写配置").set_defaults(func=cmd_config_init, local=True)
    c.add_parser("show", help="查看配置（密钥打码）").set_defaults(func=cmd_config_show, local=True)
    u = c.add_parser("use", help="切换默认 profile")
    u.add_argument("name")
    u.set_defaults(func=cmd_config_use, local=True)

    s = sub.add_parser("dbs", help="列出数据中心（无需登录）")
    fmt(s)
    s.set_defaults(func=cmd_dbs)
    sub.add_parser("login", help="登录并保存会话").set_defaults(func=cmd_login)

    s = sub.add_parser("forms", help="列出系统里所有表单（等同 WebAPI 文档页左侧目录）")
    s.add_argument("-k", "--keyword", help="按 FormId 或名称搜索")
    s.add_argument("-t", "--type", help="类型：bill,base,report 或数字，逗号分隔（默认三者）")
    s.add_argument("-c", "--cloud", help="按云/子系统名过滤，如 供应链、销售管理")
    s.add_argument("--tree", action="store_true", help="按 云 > 子系统 树状显示")
    s.add_argument("--ext", action="store_true", help="包含 GUID 形式的扩展表单")
    s.add_argument("--refresh", action="store_true", help="重新下载目录")
    fmt(s)
    s.set_defaults(func=cmd_forms)

    s = sub.add_parser("fields", help="查看表单字段 / 支持的操作 (QueryBusinessInfo)")
    s.add_argument("form_id")
    s.add_argument("--ops", action="store_true", help="显示操作列表")
    s.add_argument("--raw", action="store_true", help="输出原始元数据 JSON")
    s.add_argument("--refresh", action="store_true")
    fmt(s)
    s.set_defaults(func=cmd_fields)

    s = sub.add_parser("query", help="查询数据 (ExecuteBillQuery)")
    s.add_argument("form_id", help="表单 ID 或中文名，如 SAL_SaleOrder / 销售订单")
    s.add_argument("-f", "--fields",
                   help="字段逗号分隔；不填 = 全部表头字段；'*FSaleOrderEntry' = 某分录全部字段")
    s.add_argument("--show-skipped", dest="verbose_fields", action="store_true", help="提示被跳过的字段")
    s.add_argument("-w", "--filter", help="过滤，如 \"FDocumentStatus='C' and FDate>='2025-01-01'\"")
    s.add_argument("--order", help="排序，如 \"FDate DESC\"")
    s.add_argument("-n", "--limit", type=int, default=100, help="每页行数（默认 100）")
    s.add_argument("--start", type=int, default=0)
    s.add_argument("-a", "--all", action="store_true", help="自动翻页取全部")
    s.add_argument("--max-pages", type=int, help="和 -a 一起用：最多请求多少页（达到上限会提示结果可能不完整）")
    fmt(s)
    s.set_defaults(func=cmd_query)

    s = sub.add_parser("view", help="查看单条完整数据 (View)")
    s.add_argument("form_id")
    s.add_argument("--number", help="编号")
    s.add_argument("--id", help="内码")
    s.set_defaults(func=cmd_view)

    s = sub.add_parser("groups", help="基础资料分组 (QueryGroupInfo)，如物料分组")
    s.add_argument("form_id")
    s.add_argument("--group-field", help="分组字段，默认自动")
    fmt(s)
    s.set_defaults(func=cmd_groups)

    s = sub.add_parser("attachments", help="列出附件（可按表单、单据过滤）")
    s.add_argument("form_id", nargs="?", help="表单 ID 或中文名，如 PUR_PurchaseOrder")
    s.add_argument("--number", help="单据编号")
    s.add_argument("--id", help="单据内码")
    s.add_argument("-n", "--limit", type=int, default=50)
    s.add_argument("-a", "--all", action="store_true")
    fmt(s)
    s.set_defaults(func=cmd_attachments)

    s = sub.add_parser("download", help="下载附件 (AttachmentDownLoad)")
    s.add_argument("file_id", help="FileId，或 attachments 列出的附件ID")
    s.add_argument("-O", "--output", help="保存的文件名（默认原文件名）")
    s.set_defaults(func=cmd_download)

    s = sub.add_parser("report", help="查报表（简单账表，GetSysReportData），自动使用已保存的过滤方案")
    s.add_argument("form_id", help="报表 FormId 或中文名，如 AR_SumReport / 应收款汇总表")
    s.add_argument("-f", "--fields", help="列字段，逗号分隔")
    s.add_argument("--scheme", help="过滤方案 ID 或名称（默认自动选）")
    s.add_argument("--schemes", action="store_true", help="列出该报表的过滤方案")
    s.add_argument("-n", "--limit", type=int, default=2000)
    s.add_argument("--start", type=int, default=0)
    s.add_argument("-d", "--data", help="完整参数 JSON / @文件（可传 Model 过滤条件）")
    fmt(s)
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("scan", help="扫描所有表单，找出能读且有数据的")
    s.add_argument("form_ids", nargs="*", help="只扫描这些表单（默认全部单据+基础资料）")
    s.add_argument("-k", "--keyword")
    s.add_argument("-t", "--type", help="默认 bill,base")
    s.add_argument("-c", "--cloud")
    s.add_argument("-j", "--jobs", type=int, default=4, help="并发数（默认 4）")
    s.add_argument("--show-all", action="store_true", help="也显示无数据/失败的表单")
    fmt(s)
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("check", help="逐个验证所有查询入口（表头+分录）并缓存可用字段")
    s.add_argument("form_ids", nargs="*", help="只检查这些（默认 scan 中有数据的全部表单）")
    s.add_argument("--include-empty", action="store_true", help="也检查可读但没数据的表单")
    s.add_argument("-j", "--jobs", type=int, default=6)
    s.add_argument("--show-all", action="store_true", help="显示全部入口，而不只是失败的")
    fmt(s)
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("export", help="把表单全部数据（表头+分录）导出到目录")
    s.add_argument("form_ids", nargs="*", help="要导出的表单 ID")
    s.add_argument("--from-scan", action="store_true", help="导出 scan 结果中有数据的全部表单")
    s.add_argument("--all-forms", action="store_true", help="导出全部单据+基础资料（很慢）")
    s.add_argument("-k", "--keyword")
    s.add_argument("-t", "--type")
    s.add_argument("-c", "--cloud")
    s.add_argument("-d", "--out", default="kd-export", help="输出目录（默认 ./kd-export）")
    s.add_argument("-o", "--format", choices=["csv", "jsonl"], default="csv")
    s.add_argument("-j", "--jobs", type=int, default=3)
    s.add_argument("--force", action="store_true", help="重新导出已完成的表单")
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("call", help="直接调用只读接口：" + ", ".join(sorted(READ_METHODS)))
    s.add_argument("method")
    s.add_argument("form_id", nargs="?")
    s.add_argument("-d", "--data", help="JSON 字符串、@文件 或 -")
    s.set_defaults(func=cmd_call)
    return p


def _utf8_streams():
    """Windows 等系统的管道默认用本地编码（如 cp1252），输出中文会报错。
    被程序调用时统一用 UTF-8；在终端里则把无法显示的字符替换掉，而不是崩溃。"""
    for stream in (sys.stdout, sys.stderr):
        if not hasattr(stream, "reconfigure") or (stream.encoding or "").lower().replace("-", "") == "utf8":
            continue
        if stream.isatty():
            stream.reconfigure(errors="replace")
        else:
            stream.reconfigure(encoding="utf-8")


def main(argv=None):
    global QUIET
    import signal
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    _utf8_streams()
    args = build_parser().parse_args(argv)
    QUIET = args.quiet
    interactive = sys.stdout.isatty()
    if hasattr(args, "format") and args.format is None:
        args.format = os.environ.get("KD_FORMAT") or ("table" if interactive else "json")
    json_errors = getattr(args, "format", None) == "json" or not interactive
    args.json_output = json_errors
    client = None
    try:
        if args.cmd == "call" and args.method not in READ_METHODS:  # 不读配置就先拒绝写接口
            raise KDError(f"{args.method} 不是只读接口，已拒绝。允许：{', '.join(sorted(READ_METHODS))}",
                          code="readonly")
        if getattr(args, "local", False):
            return args.func(args)
        client = Client(resolve_config(args.profile), verbose=args.verbose)
        if getattr(args, "form_id", None) and args.cmd != "call":
            args.form_id = resolve_form(client, args.form_id, prefer="900" if args.cmd == "report" else None)
        args.func(args, client)
    except KDError as e:
        message = client.redact(e) if client else str(e)  # 最后一道防线：任何错误都不带出服务地址
        if json_errors:
            err = {"code": e.code, "message": message}
            if e.data:
                err.update(e.data)
            print(to_json({"error": err}, indent=2))
        log(f"错误：{message}")
        sys.exit(KDError.EXIT.get(e.code, 1))
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:  # 程序缺陷：也按统一格式输出，不让调用方面对 traceback
        message = client.redact(f"{type(e).__name__}: {e}") if client else f"{type(e).__name__}: {e}"
        if json_errors:
            print(to_json({"error": {"code": "internal", "message": message}}, indent=2))
        log(f"内部错误：{message}（加 -v 查看调用栈）")
        if args.verbose:
            import traceback
            traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
