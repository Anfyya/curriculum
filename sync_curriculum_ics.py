#!/usr/bin/env python3
import base64
import hashlib
import html
import http.cookiejar
import json
import os
import re
import ssl
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, date
from typing import Dict, List, Tuple, Optional, Iterable

TZ_UTC = timezone.utc
TZ_CST = timezone(timedelta(hours=8))

DEFAULT_PERIOD_TIMES = {
    1: ("08:00", "08:45"),
    2: ("08:55", "09:40"),
    3: ("10:00", "10:45"),
    4: ("10:55", "11:40"),
    5: ("14:00", "14:45"),
    6: ("14:55", "15:40"),
    7: ("16:00", "16:45"),
    8: ("16:55", "17:40"),
    9: ("19:00", "19:45"),
    10: ("19:55", "20:40"),
    11: ("20:50", "21:35"),
    12: ("21:45", "22:30"),
    13: ("22:40", "23:25"),
    14: ("23:35", "00:20"),
}

# 指定节次区间的时间覆盖（优先于 period_times.json）
PERIOD_RANGE_OVERRIDES: Dict[Tuple[int, int], Tuple[str, str]] = {
    (1, 2): ("08:30", "09:55"),
    (3, 4): ("10:15", "11:40"),
    (5, 6): ("14:00", "15:24"),
    (7, 8): ("15:45", "17:10"),
    (1, 4): ("08:30", "11:40"),
    (5, 8): ("14:00", "17:10"),
}

# 系统 dayOfWeek 映射：2=星期一 ... 7=星期六, 1=星期日
DAY_OFFSET_MAP = {1: 6, 2: 0, 3: 1, 4: 2, 5: 3, 6: 4, 7: 5}
DAY_NAME_OFFSET_MAP = {
    "星期一": 0,
    "星期二": 1,
    "星期三": 2,
    "星期四": 3,
    "星期五": 4,
    "星期六": 5,
    "星期日": 6,
    "星期天": 6,
    "周一": 0,
    "周二": 1,
    "周三": 2,
    "周四": 3,
    "周五": 4,
    "周六": 5,
    "周日": 6,
}


@dataclass
class Config:
    entry_url: str
    cookie: str
    access_token: str
    user_id: str
    user_type: str
    output_path: str
    calendar_name: str
    period_times: Dict[int, Tuple[str, str]]


class ApiError(RuntimeError):
    pass


def normalize_cookie(raw_cookie: str) -> str:
    s = (raw_cookie or "").lstrip("\ufeff").strip()
    if not s:
        return ""
    if s.lower().startswith("cookie:"):
        s = s.split(":", 1)[1].strip()
    s = s.replace("\r", ";").replace("\n", ";")
    parts = []
    for p in s.split(";"):
        p = p.strip()
        if not p or "=" not in p:
            continue
        k, v = p.split("=", 1)
        k = k.strip()
        v = v.strip()
        if not k:
            continue
        parts.append(f"{k}={v}")
    return "; ".join(parts)


def env(name: str, default: Optional[str] = None, required: bool = False) -> str:
    val = os.getenv(name, default)
    if required and (val is None or str(val).strip() == ""):
        raise RuntimeError(f"缺少环境变量: {name}")
    return "" if val is None else str(val)


def normalize_entry_url(entry_url: str) -> str:
    normalized = html.unescape((entry_url or "").strip()).replace("&amp;", "&")
    if not normalized:
        return ""
    split = urllib.parse.urlsplit(normalized)
    path = urllib.parse.quote(split.path, safe="/%:@!$&'()*+,;=-._~")
    query = urllib.parse.quote(split.query, safe="=&%:@!$'()*+,;/?-._~")
    fragment = urllib.parse.quote(split.fragment, safe="=&%:@!$'()*+,;/?-._~")
    return urllib.parse.urlunsplit((split.scheme, split.netloc, path, query, fragment))


def parse_entry(entry_url: str) -> Dict[str, str]:
    normalized = html.unescape((entry_url or "").strip()).replace("&amp;", "&")
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(normalized).query)

    def first(*keys: str) -> str:
        for key in keys:
            vals = q.get(key)
            if vals and vals[0]:
                return vals[0]
        return ""

    result = {
        "accessToken": first("accessToken", "amp;accessToken"),
        "id": first("id", "amp;id"),
        "userType": first("userType", "amp;userType"),
    }

    app_url = first("appUrl", "amp;appUrl")
    if app_url and (not result["accessToken"] or not result["id"]):
        decoded_app = html.unescape(urllib.parse.unquote(app_url))
        aq = urllib.parse.parse_qs(urllib.parse.urlsplit(decoded_app).query)
        if not result["accessToken"]:
            result["accessToken"] = (aq.get("accessToken") or [""])[0]
        if not result["id"]:
            result["id"] = (aq.get("id") or [""])[0]
        if not result["userType"]:
            result["userType"] = (aq.get("userType") or [""])[0]

    if not result["accessToken"]:
        m = re.search(r"(?:^|[?&])accessToken=([^&\s]+)", normalized)
        if m:
            result["accessToken"] = urllib.parse.unquote(m.group(1))
    if not result["id"]:
        m = re.search(r"(?:^|[?&])id=([^&\s]+)", normalized)
        if m:
            result["id"] = urllib.parse.unquote(m.group(1))
    if not result["userType"]:
        m = re.search(r"(?:^|[?&])userType=([^&\s]+)", normalized)
        if m:
            result["userType"] = urllib.parse.unquote(m.group(1))

    return result


def normalize_teacher(name: str) -> str:
    if not name:
        return ""
    parts = [p.strip() for p in re.split(r"[,，、/]+", name) if p.strip()]
    dedup = []
    seen = set()
    for p in parts:
        if p not in seen:
            seen.add(p)
            dedup.append(p)
    return "、".join(dedup)


def safe_int(v: object) -> Optional[int]:
    if v is None:
        return None
    m = re.search(r"\d+", str(v))
    if not m:
        return None
    return int(m.group(0))


def parse_periods(v: object) -> List[int]:
    if v is None:
        return []
    nums = [int(x) for x in re.findall(r"\d+", str(v))]
    return sorted(set(nums))


def parse_weeks_expr(expr: str, max_week: int) -> List[int]:
    if not expr:
        return []
    expr = expr.strip()
    out = set()
    for seg in re.split(r"[;；]+", expr):
        seg = seg.strip()
        if not seg:
            continue
        odd_only = "单" in seg
        even_only = "双" in seg
        core = re.sub(r"[^0-9\-]", "", seg)
        if not core:
            continue
        if "-" in core:
            a, b = core.split("-", 1)
            if not a or not b:
                continue
            start, end = int(a), int(b)
            if start > end:
                start, end = end, start
        else:
            start = end = int(core)

        for w in range(start, end + 1):
            if w < 1 or w > max_week:
                continue
            if odd_only and w % 2 == 0:
                continue
            if even_only and w % 2 != 0:
                continue
            out.add(w)
    return sorted(out)


def merge_periods(periods: Iterable[int]) -> List[Tuple[int, int]]:
    arr = sorted(set(p for p in periods if p > 0))
    if not arr:
        return []
    merged = []
    s = arr[0]
    e = arr[0]
    for p in arr[1:]:
        if p == e + 1:
            e = p
        else:
            merged.append((s, e))
            s = e = p
    merged.append((s, e))
    return merged


def minutes_to_hhmm(total_minutes: int) -> str:
    total_minutes %= (24 * 60)
    h = total_minutes // 60
    m = total_minutes % 60
    return f"{h:02d}:{m:02d}"


def fallback_period_time(period: int) -> Tuple[str, str]:
    start = 8 * 60 + (period - 1) * 55
    end = start + 45
    return minutes_to_hhmm(start), minutes_to_hhmm(end)


def period_range_time(start_period: int, end_period: int, period_map: Dict[int, Tuple[str, str]]) -> Tuple[str, str]:
    override = PERIOD_RANGE_OVERRIDES.get((start_period, end_period))
    if override:
        return override
    st = period_map.get(start_period, fallback_period_time(start_period))[0]
    et = period_map.get(end_period, fallback_period_time(end_period))[1]
    return st, et


def periods_to_blocks(periods: Iterable[int]) -> List[Tuple[int, int]]:
    # 先识别成对节次，再保留剩余单节
    s = set(p for p in periods if p > 0)
    blocks: List[Tuple[int, int]] = []
    for a, b in ((1, 2), (3, 4), (5, 6), (7, 8)):
        if a in s and b in s:
            blocks.append((a, b))
            s.remove(a)
            s.remove(b)
    for p in sorted(s):
        blocks.append((p, p))
    return blocks


def merge_blocks_with_rule(blocks: Iterable[Tuple[int, int]]) -> List[Tuple[int, int]]:
    # 仅允许 1-2+3-4 => 1-4，5-6+7-8 => 5-8
    block_set = set(blocks)
    merged: List[Tuple[int, int]] = []
    if (1, 2) in block_set and (3, 4) in block_set:
        merged.append((1, 4))
        block_set.remove((1, 2))
        block_set.remove((3, 4))
    if (5, 6) in block_set and (7, 8) in block_set:
        merged.append((5, 8))
        block_set.remove((5, 6))
        block_set.remove((7, 8))
    merged.extend(sorted(block_set, key=lambda x: (x[0], x[1])))
    merged.sort(key=lambda x: (x[0], x[1]))
    return merged


def split_names(raw: str) -> List[str]:
    if not raw:
        return []
    return [x.strip() for x in re.split(r"[、,，\s]+", raw) if x.strip()]


def parse_hhmm_to_dt(d: date, hhmm: str) -> datetime:
    h, m = hhmm.split(":", 1)
    return datetime(d.year, d.month, d.day, int(h), int(m), 0, tzinfo=TZ_CST)


def ics_escape(s: str) -> str:
    return (
        s.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\n")
        .replace("\n", "\\n")
    )


def load_period_map() -> Dict[int, Tuple[str, str]]:
    inline = os.getenv("CURRICULUM_PERIOD_TIMES_JSON", "").strip()
    if inline:
        obj = json.loads(inline.lstrip("\ufeff"))
        return {int(k): (str(v[0]), str(v[1])) for k, v in obj.items()}

    path = os.getenv("CURRICULUM_PERIOD_TIMES_FILE", "period_times.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8-sig") as f:
            obj = json.load(f)
        return {int(k): (str(v[0]), str(v[1])) for k, v in obj.items()}

    return dict(DEFAULT_PERIOD_TIMES)


# ---------------------------------------------------------------------------
#  SSO 自动登录（联奕 CAS）
# ---------------------------------------------------------------------------

_RSA_EXPONENT = 0x010001
_RSA_MODULUS = int(
    "00b5eeb166e069920e80bebd1fea4829d3d1f3216f2aabe79b6c47a3c18dcee5"
    "fd22c2e7ac519cab59198ece036dcf289ea8201e2a0b9ded307f8fb704136eae"
    "b670286f5ad44e691005ba9ea5af04ada5367cd724b5a26fdb5120cc95b64316"
    "04bd219c6b7d83a6f8f24b43918ea988a76f93c333aa5a20991493d4eb1117e7b1",
    16,
)
_RSA_CHUNK_SIZE = 2 * ((_RSA_MODULUS.bit_length() + 15) // 16)

_SSO_ORIGIN = "https://sso.jxec.edu.cn:10445"
_PORTAL_ORIGIN = "https://0xr.jxec.edu.cn:10443"
_CAS_SERVICE = _PORTAL_ORIGIN + "/passport/v1/auth/cas?sfDomain=1"


def _rsa_encrypt_block(plaintext: str) -> str:
    codes = [ord(ch) for ch in plaintext]
    while len(codes) % _RSA_CHUNK_SIZE != 0:
        codes.append(0)

    digits: List[int] = []
    for i in range(0, len(codes), 2):
        digits.append(codes[i] | (codes[i + 1] << 8))

    m = 0
    for i in range(len(digits) - 1, -1, -1):
        m = (m << 16) | digits[i]

    c = pow(m, _RSA_EXPONENT, _RSA_MODULUS)

    hex_len = (_RSA_MODULUS.bit_length() + 3) // 4
    return format(c, f"0{hex_len}x")


def _solve_captcha_expr(text: str) -> str:
    text = text.strip().rstrip("=").strip()
    text = text.replace("×", "*").replace("x", "*").replace("X", "*").replace("÷", "/")
    try:
        result = eval(text, {"__builtins__": {}})
        return str(int(result))
    except Exception:
        return text


def _build_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def auto_login(username: str, password: str, entry_url: str = "", max_retries: int = 5) -> str:
    try:
        import ddddocr  # type: ignore
    except ImportError:
        raise RuntimeError("auto_login 需要 ddddocr，请先 pip install ddddocr")

    ocr = ddddocr.DdddOcr(show_ad=False)
    ssl_ctx = _build_ssl_context()

    cas_name = "/lyuapServer"
    encrypted_pwd = _rsa_encrypt_block(password)

    for attempt in range(1, max_retries + 1):
        # --- 1. 获取验证码 ---
        kaptcha_url = f"{_SSO_ORIGIN}{cas_name}/kaptcha?uid=&sf_request_type=ajax"
        req = urllib.request.Request(kaptcha_url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        })
        try:
            with urllib.request.urlopen(req, timeout=30, context=ssl_ctx) as resp:
                kdata = json.loads(resp.read().decode("utf-8-sig"))
        except Exception as e:
            print(f"  auto_login 尝试 {attempt}/{max_retries}：获取验证码失败: {e}")
            if attempt < max_retries:
                continue
            raise

        uid = kdata["uid"]
        img_b64 = kdata["content"].split(",", 1)[1]
        img_bytes = base64.b64decode(img_b64)

        # --- 2. OCR 识别验证码 ---
        raw_text = ocr.classification(img_bytes)
        code = _solve_captcha_expr(raw_text)
        print(f"  auto_login 尝试 {attempt}/{max_retries}：OCR={raw_text!r} → code={code}")

        # --- 3. POST 登录 ---
        login_url = f"{_SSO_ORIGIN}{cas_name}/v1/tickets?sf_request_type=ajax"
        form = urllib.parse.urlencode({
            "username": username,
            "password": encrypted_pwd,
            "service": _CAS_SERVICE,
            "loginType": "",
            "id": uid,
            "code": code,
            "otpcode": "",
        }).encode("utf-8")
        req = urllib.request.Request(login_url, data=form, method="POST", headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        })
        with urllib.request.urlopen(req, timeout=30, context=ssl_ctx) as resp:
            login_body = resp.read().decode("utf-8-sig")

        login_resp = json.loads(login_body)

        # --- 兼容两种格式：顶层直接有 ticket，或包在 data 里 ---
        ticket = login_resp.get("ticket") or login_resp.get("data", {})
        if isinstance(ticket, dict):
            err_code = ticket.get("code", "")
            if err_code == "CODEFALSE":
                print(f"  验证码错误，重试...")
                continue
            elif err_code == "BINDPHONE":
                raise RuntimeError("SSO 要求绑定手机，无法自动登录")
            elif err_code:
                raise RuntimeError(f"SSO 登录返回错误码: {err_code}")
            ticket = ticket.get("ticket") or ""
        if not ticket or not str(ticket).startswith("ST-"):
            raise RuntimeError(f"SSO 登录返回未知格式: {login_body[:300]}")
        ticket = str(ticket)

        print(f"  SSO 登录成功，获取到 ticket")

        # --- 4. 用 ticket 换取门户 Cookie ---
        cj = http.cookiejar.CookieJar()

        class _DebugRedirectHandler(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                print(f"    [REDIR] {code} {req.full_url[:120]}")
                print(f"         -> {newurl[:150]}")
                for hk, hv in headers.items():
                    hl = hk.lower()
                    if "cookie" in hl or "location" in hl:
                        print(f"         {hk}: {hv[:200]}")
                return super().redirect_request(req, fp, code, msg, headers, newurl)

        opener = urllib.request.build_opener(
            _DebugRedirectHandler,
            urllib.request.HTTPCookieProcessor(cj),
            urllib.request.HTTPSHandler(context=ssl_ctx),
        )
        _UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

        callback_url = f"{_CAS_SERVICE}&ticket={urllib.parse.quote(ticket, safe='')}"
        print(f"  CAS 回调: {callback_url[:120]}")
        req = urllib.request.Request(callback_url, headers=_UA)
        try:
            resp = opener.open(req, timeout=30)
            print(f"  CAS 回调结束: status={resp.status} url={resp.geturl()[:120]}")
        except urllib.error.HTTPError as e:
            print(f"  CAS 回调 HTTPError: {e.code}")

        portal_cookies = {c.name: c for c in cj}
        print(f"  门户 Cookie ({len(portal_cookies)} 项): {list(portal_cookies.keys())}")

        # --- 5. 激活门户会话 (secondary_auth → online) ---
        #  注意: 请求中不能带 x-sdp-traceid 头，否则网关会在 secondary_auth 状态下返回 401
        _portal_host = urllib.parse.urlsplit(_PORTAL_ORIGIN).netloc
        _x_sdp_rid = base64.b64encode(_portal_host.encode()).decode()
        _base_params = {"clientType": "SDPBrowserClient", "platform": "Windows", "lang": "zh-CN"}

        def _portal_api(method, path, params=None, body=None, csrf=""):
            url = f"{_PORTAL_ORIGIN}{path}"
            if params:
                url += "?" + urllib.parse.urlencode(params)
            hdrs = {**_UA, "Accept": "application/json, text/plain, */*",
                    "Referer": f"{_PORTAL_ORIGIN}/portal/shortcut.html",
                    "Origin": _PORTAL_ORIGIN, "x-sdp-rid": _x_sdp_rid}
            if csrf:
                hdrs["x-csrf-token"] = csrf
            raw_body = None
            if body is not None:
                raw_body = json.dumps(body).encode()
                hdrs["Content-Type"] = "application/json"
            r = urllib.request.Request(url, data=raw_body, method=method, headers=hdrs)
            try:
                rsp = opener.open(r, timeout=15)
                return rsp.status, json.loads(rsp.read().decode("utf-8", errors="replace"))
            except urllib.error.HTTPError as e:
                txt = e.read().decode("utf-8", errors="replace") if e.fp else ""
                try:
                    return e.code, json.loads(txt)
                except Exception:
                    return e.code, {"raw": txt[:300]}

        # 5a. 获取 CSRF token
        st, cfg = _portal_api("GET", "/passport/v1/public/authConfig",
                              {"mod": "1", "sfDomain": "1", **_base_params})
        csrf_token = ""
        if st == 200 and isinstance(cfg.get("data"), dict):
            csrf_token = cfg["data"].get("security", {}).get("csrfToken", "")
        print(f"  authConfig: status={st}, CSRF={'OK' if csrf_token else 'MISSING'}")

        # 5b. reportEnv (上报浏览器环境)
        import random as _rnd, time as _tm
        _dev_id = hashlib.md5(f"{_rnd.random()}{_tm.time()}".encode()).hexdigest()
        _portal_ticket = ""
        for _c in cj:
            if _c.name == "sid":
                _portal_ticket = _c.value
                break
        st2, _ = _portal_api("POST", "/controller/v1/public/reportEnv",
                             params=_base_params, csrf=csrf_token,
                             body={"ticket": _portal_ticket, "deviceId": _dev_id,
                                   "env": {"endpoint": {"device_id": _dev_id,
                                                        "device": {"type": "browser"}}}})
        print(f"  reportEnv: status={st2}")

        # 5c. authCheck → 获取 sidTicket
        st3, ac_resp = _portal_api("GET", "/passport/v1/auth/authCheck",
                                   params=_base_params, csrf=csrf_token)
        sid_ticket = ""
        if st3 == 200 and isinstance(ac_resp.get("data"), dict):
            sid_ticket = ac_resp["data"].get("sidTicket", "")
        print(f"  authCheck: status={st3}, sidTicket={'OK' if sid_ticket else 'MISSING'}")

        # 5d. sessionIdExchange → 激活会话
        if sid_ticket:
            st4, ex_resp = _portal_api("POST", "/passport/v1/public/sessionIdExchange",
                                       params=_base_params, csrf=csrf_token,
                                       body={"sidTicket": sid_ticket})
            print(f"  sessionIdExchange: status={st4}")
        else:
            print(f"  跳过 sessionIdExchange (无 sidTicket)")

        # 检查会话状态
        auth_tag = ""
        for _c in cj:
            if _c.name == "sdp_limit_auth_tag":
                auth_tag = _c.value
        print(f"  会话状态: sdp_limit_auth_tag={auth_tag}")

        # --- 6. 访问课表入口 URL，跟着 aTrust 重定向拿到目标域名的 sdp_app_session ---
        if entry_url:
            target_host = urllib.parse.urlsplit(entry_url).hostname  # e.g. zichan.jxec.edu.cn
            print(f"  正在建立 {target_host} 的会话 (跟随 aTrust 重定向)...")
            req = urllib.request.Request(entry_url, headers=_UA)
            try:
                resp = opener.open(req, timeout=30)
                final_url = resp.geturl()
                body_preview = resp.read(2000).decode("utf-8", errors="replace")
                print(f"  重定向结束: status={resp.status} final_url={final_url[:150]}")
            except urllib.error.HTTPError as e:
                print(f"  重定向链 HTTPError: {e.code} {e.url[:150] if e.url else ''}")
            except urllib.error.URLError as e:
                print(f"  重定向链 URLError: {e.reason}")
            except Exception as e:
                print(f"  重定向链异常: {type(e).__name__}: {e}")

            # 只收集目标域名的 cookie
            target_cookies = [c for c in cj if target_host and target_host in c.domain]
            app_session = [c for c in target_cookies if "sdp_app_session" in c.name]
            if app_session:
                print(f"  aTrust 会话建立成功: {[c.name for c in app_session]}")
            else:
                print(f"  警告: 未获取到 sdp_app_session cookie (目标域名 {target_host} 共 {len(target_cookies)} 个 cookie)")
                for c in cj:
                    print(f"    [{c.domain}] {c.name}")

            cookie_parts = [f"{c.name}={c.value}" for c in target_cookies]
        else:
            cookie_parts = [f"{c.name}={c.value}" for c in cj]

        if not cookie_parts:
            raise RuntimeError("未获取到目标域名的 Cookie，aTrust 重定向可能失败")

        cookie_str = "; ".join(cookie_parts)
        print(f"  最终 Cookie ({len(cookie_parts)} 项)")
        return cookie_str

    raise RuntimeError(f"验证码识别连续 {max_retries} 次失败，请检查 ddddocr 或手动登录")


def build_config() -> Config:
    entry_url_raw = env("CURRICULUM_ENTRY_URL", required=True)
    entry_url = normalize_entry_url(entry_url_raw)
    parsed = parse_entry(entry_url_raw)

    access_token = env("CURRICULUM_ACCESS_TOKEN", parsed.get("accessToken") or "", required=False).strip()
    # 容错：如果用户把 "accessToken=xxx" 整个粘贴为值，去掉前缀
    if access_token.startswith("accessToken="):
        access_token = access_token[len("accessToken="):]
    if not access_token:
        raise RuntimeError("缺少 accessToken：请设置 CURRICULUM_ACCESS_TOKEN，或在 CURRICULUM_ENTRY_URL 中带上 accessToken 参数")

    user_id = env("CURRICULUM_USER_ID", parsed.get("id") or "", required=False).strip()
    if not user_id:
        raise RuntimeError("缺少 userId：请设置 CURRICULUM_USER_ID，或在 CURRICULUM_ENTRY_URL 中带上 id 参数")

    user_type = env("CURRICULUM_USER_TYPE", parsed.get("userType") or "0", required=False).strip() or "0"

    cookie_raw = env("CURRICULUM_COOKIE", "").strip()
    sso_user = env("SSO_USERNAME", "").strip()
    sso_pass = env("SSO_PASSWORD", "").strip()

    if cookie_raw:
        cookie = normalize_cookie(cookie_raw)
    elif sso_user and sso_pass:
        print("CURRICULUM_COOKIE 未设置，使用 SSO_USERNAME/SSO_PASSWORD 自动登录...")
        cookie = auto_login(sso_user, sso_pass, entry_url=entry_url)
    else:
        raise RuntimeError(
            "缺少登录凭据：请设置 CURRICULUM_COOKIE，或同时设置 SSO_USERNAME 和 SSO_PASSWORD"
        )

    if not cookie:
        raise RuntimeError("Cookie 为空或格式无效")

    output_path = env("CURRICULUM_OUTPUT", "curriculum.ics")
    calendar_name = env("CURRICULUM_CALENDAR_NAME", "课程表")

    return Config(
        entry_url=entry_url,
        cookie=cookie,
        access_token=access_token,
        user_id=user_id,
        user_type=user_type,
        output_path=output_path,
        calendar_name=calendar_name,
        period_times=load_period_map(),
    )


def request_json(origin: str, headers: Dict[str, str], method: str, path: str, data: Optional[dict] = None) -> dict:
    body = None
    req_headers = dict(headers)
    if data is not None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        req_headers["Content-Type"] = "application/json;charset=UTF-8"

    req = urllib.request.Request(origin + path, data=body, headers=req_headers, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=45, context=_build_ssl_context())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")[:500] if e.fp else ""
        err_url = e.url or (origin + path)
        print(f"  API请求失败: {e.code} {path}")
        print(f"    URL: {err_url[:200]}")
        print(f"    响应头: { {k: v for k, v in (e.headers.items() if e.headers else [])} }")
        print(f"    响应体: {err_body[:300]}")
        raise
    with resp:
        raw = resp.read().decode("utf-8-sig", "replace").lstrip("\ufeff")
        ct = (resp.headers.get("Content-Type") or "").lower()
        final_url = resp.geturl()

    if "json" not in ct and not raw.strip().startswith("{"):
        preview = re.sub(r"\s+", " ", raw[:120]).strip()
        raise ApiError(
            f"接口返回非 JSON，可能登录态失效: {path} final_url={final_url} content_type={ct or '-'} preview={preview}"
        )

    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ApiError(f"接口 JSON 解析失败: {path}, {e}") from e

    if obj.get("code") != 200:
        raise ApiError(f"接口返回异常 code={obj.get('code')} path={path} message={obj.get('message')}")

    return obj


def day_offset(row: dict) -> Optional[int]:
    day_num = safe_int(row.get("dayOfWeek"))
    if day_num in DAY_OFFSET_MAP:
        return DAY_OFFSET_MAP[day_num]
    day_name = str(row.get("dayOfWeekName") or "").strip()
    return DAY_NAME_OFFSET_MAP.get(day_name)


def build_ics(config: Config) -> Tuple[str, int]:
    origin = urllib.parse.urlsplit(config.entry_url).scheme + "://" + urllib.parse.urlsplit(config.entry_url).netloc
    headers = {
        "accessToken": config.access_token,
        "Cookie": config.cookie,
        "Origin": origin,
        "Referer": config.entry_url,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }

    current = request_json(origin, headers, "GET", "/api/baseInfo/mobile/common/selectCurrentInfo").get("data", {})
    current_week = int(current.get("currentWeek") or 1)
    semester = str(current.get("currentSemester") or "")

    week_data = request_json(origin, headers, "GET", "/api/baseInfo/mobile/common/queryCurrentSemesterWeekList").get("data", [])
    week_codes = sorted({int(w.get("code") or w.get("value") or 0) for w in week_data if safe_int(w.get("code") or w.get("value"))})
    if not week_codes:
        week_codes = [current_week]
    max_week = max(week_codes)

    rows: List[dict] = []
    # 避免一次请求整学期导致后端把不同周次课程错误合并，改为逐周拉取后汇总
    for week_code in week_codes:
        payload = {
            "academicYearSemester": semester,
            "userId": config.user_id,
            "userType": config.user_type,
            "weeks": [week_code],
        }
        schedule = request_json(origin, headers, "POST", "/api/arrange/mobile/courseSchedule/courseSchedule", payload).get("data", {})
        for r in schedule.get("course", []):
            if not str(r.get("courseName") or "").strip():
                continue
            rr = dict(r)
            rr["_week_code"] = week_code
            rows.append(rr)

    today_iso = None
    try:
        idx = request_json(origin, headers, "POST", "/api/arrange/mobile/courseSchedule/indexCourseSchedule", {
            "academicYearSemester": semester,
            "userId": config.user_id,
            "userType": config.user_type,
            "weeks": [current_week],
        }).get("data", {})
        today_iso = idx.get("todayTime")
    except Exception:
        today_iso = None

    if today_iso:
        today_date = date.fromisoformat(today_iso)
    else:
        today_date = datetime.now(TZ_CST).date()

    current_week_monday = today_date - timedelta(days=today_date.weekday())

    grouped_blocks: Dict[Tuple[str, str, int, int], set] = {}
    grouped_teachers: Dict[Tuple[str, str, int, int], List[str]] = {}
    grouped_classes: Dict[Tuple[str, str, int, int], List[str]] = {}
    for r in rows:
        offset = day_offset(r)
        if offset is None:
            continue

        course = str(r.get("courseName") or "").strip()
        teacher = normalize_teacher(str(r.get("teacherName") or "").strip())
        classroom = re.sub(r"\s+", " ", str(r.get("classroomName") or "").strip())
        class_name = str(r.get("teachingClassName") or r.get("className") or "").strip()
        week_num = safe_int(r.get("_week_code"))
        if week_num is None:
            parsed = parse_weeks_expr(str(r.get("weeks") or "").strip(), max_week)
            if not parsed:
                continue
            week_num = parsed[0]

        periods = parse_periods(r.get("time"))
        if not periods:
            periods = parse_periods(r.get("sectionName"))
        if not periods:
            continue

        block_ranges = periods_to_blocks(periods)
        if not block_ranges:
            continue

        # 合并键只看课程+教室+星期+周次；不同教室天然拆分
        key = (course, classroom, offset, week_num)
        grouped_blocks.setdefault(key, set()).update(block_ranges)

        teacher_list = grouped_teachers.setdefault(key, [])
        for t in split_names(teacher):
            if t not in teacher_list:
                teacher_list.append(t)

        class_list = grouped_classes.setdefault(key, [])
        if class_name and class_name not in class_list:
            class_list.append(class_name)

    events = []
    for (course, classroom, offset, week_num), block_set in grouped_blocks.items():
        period_ranges = merge_blocks_with_rule(block_set)
        day_date = current_week_monday + timedelta(days=offset + (week_num - current_week) * 7)
        weeks_expr = str(week_num)
        teacher = "、".join(grouped_teachers.get((course, classroom, offset, week_num), []))
        class_name = "、".join(grouped_classes.get((course, classroom, offset, week_num), []))

        for start_p, end_p in period_ranges:
            st_s, et_s = period_range_time(start_p, end_p, config.period_times)
            dt_start = parse_hhmm_to_dt(day_date, st_s)
            dt_end = parse_hhmm_to_dt(day_date, et_s)
            if dt_end <= dt_start:
                dt_end += timedelta(days=1)

            # Apple 日历列表可见位置展示教师名
            summary = course if not teacher else f"{course}（{teacher}）"
            location = classroom or "待定教室"
            desc = "\n".join([
                f"教师: {teacher or '待定'}",
                f"班级: {class_name or '待定'}",
                f"周次规则: {weeks_expr}",
                f"节次: 第{start_p}-{end_p}节",
                f"学期: {semester}",
                "来源: 江西工程学院课程系统",
            ])

            uid_seed = f"{course}|{teacher}|{classroom}|{day_date.isoformat()}|{start_p}-{end_p}|{weeks_expr}"
            uid = hashlib.sha1(uid_seed.encode("utf-8")).hexdigest() + "@curriculum"
            events.append((dt_start, {
                "uid": uid,
                "start": dt_start,
                "end": dt_end,
                "summary": summary,
                "location": location,
                "description": desc,
            }))

    events.sort(key=lambda x: x[0])
    dtstamp = datetime.now(TZ_UTC).strftime("%Y%m%dT%H%M%SZ")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Anfyya//Curriculum Sync//CN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{ics_escape(config.calendar_name)}",
        "X-WR-TIMEZONE:Asia/Shanghai",
        "BEGIN:VTIMEZONE",
        "TZID:Asia/Shanghai",
        "BEGIN:STANDARD",
        "DTSTART:19700101T000000",
        "TZOFFSETFROM:+0800",
        "TZOFFSETTO:+0800",
        "TZNAME:CST",
        "END:STANDARD",
        "END:VTIMEZONE",
    ]

    for _, ev in events:
        lines.extend([
            "BEGIN:VEVENT",
            f"UID:{ev['uid']}",
            f"DTSTAMP:{dtstamp}",
            f"DTSTART;TZID=Asia/Shanghai:{ev['start'].strftime('%Y%m%dT%H%M%S')}",
            f"DTEND;TZID=Asia/Shanghai:{ev['end'].strftime('%Y%m%dT%H%M%S')}",
            f"SUMMARY:{ics_escape(ev['summary'])}",
            f"LOCATION:{ics_escape(ev['location'])}",
            f"DESCRIPTION:{ics_escape(ev['description'])}",
            "END:VEVENT",
        ])

    lines.append("END:VCALENDAR")
    ics_text = "\r\n".join(lines) + "\r\n"
    return ics_text, len(events)


def main() -> int:
    # 确保 print 实时输出（GitHub Actions 默认全缓冲）
    sys.stdout.reconfigure(line_buffering=True) if hasattr(sys.stdout, "reconfigure") else None
    sys.stderr.reconfigure(line_buffering=True) if hasattr(sys.stderr, "reconfigure") else None

    try:
        cfg = build_config()
        print(f"配置加载完成，origin={urllib.parse.urlsplit(cfg.entry_url).netloc}")
        ics_text, count = build_ics(cfg)
        with open(cfg.output_path, "w", encoding="utf-8", newline="") as f:
            f.write(ics_text)
        print(f"已生成 {cfg.output_path}，事件数: {count}")
        return 0
    except Exception as e:
        print(f"同步失败: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
