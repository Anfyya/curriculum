#!/usr/bin/env python3
"""
sync_curriculum_ics.py  v3.0

自动同步江西工程学院教务系统课程表，生成 .ics 日历文件。
无需 accessToken，通过 Playwright 浏览器自动完成 SSO + aTrust 认证。

环境变量:
  SSO_USERNAME             - SSO 用户名（学号）    [必须]
  SSO_PASSWORD             - SSO 密码              [必须]
  CURRICULUM_OUTPUT        - 输出文件路径           [默认 curriculum.ics]
  CURRICULUM_CALENDAR_NAME - 日历名称              [默认 课程表]
"""
import base64
import hashlib
import http.cookiejar
import json
import os
import random
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone, date
from typing import Dict, List, Tuple, Iterable

TZ_UTC = timezone.utc
TZ_CST = timezone(timedelta(hours=8))

_PORTAL_ORIGIN = "https://0xr.jxec.edu.cn:10443"
_SSO_ORIGIN = "https://sso.jxec.edu.cn:10445"
_JW_ORIGIN = "https://jiaowu.jxec.edu.cn:19995"
_JW_BACKEND = "https://jiaowu.jxec.edu.cn:19090"
_CAS_SERVICE = f"{_JW_BACKEND}/api/cas/login?pattern=teacher-login"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

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

PERIOD_RANGE_OVERRIDES: Dict[Tuple[int, int], Tuple[str, str]] = {
    (1, 2): ("08:30", "09:55"),
    (3, 4): ("10:15", "11:40"),
    (5, 6): ("14:00", "15:24"),
    (7, 8): ("15:45", "17:10"),
    (1, 4): ("08:30", "11:40"),
    (5, 8): ("14:00", "17:10"),
}

# dayOfWeek: 2=Mon ... 7=Sat, 1=Sun -> Python weekday offset (Mon=0)
DAY_OFFSET_MAP = {1: 6, 2: 0, 3: 1, 4: 2, 5: 3, 6: 4, 7: 5}


# ---------------------------------------------------------------------------
#  RSA 加密（联奕 CAS 专用分块 RSA）
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
    text = text.replace("\u00d7", "*").replace("x", "*").replace("X", "*").replace("\u00f7", "/")
    try:
        return str(int(eval(text, {"__builtins__": {}})))
    except Exception:
        return text


def _build_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


# ---------------------------------------------------------------------------
#  工具函数
# ---------------------------------------------------------------------------

def parse_periods(v) -> List[int]:
    if v is None:
        return []
    return sorted(set(int(x) for x in re.findall(r"\d+", str(v))))


def parse_weeks_expr(expr: str, max_week: int) -> List[int]:
    if not expr:
        return []
    out: set = set()
    for seg in re.split(r"[;\uff1b]+", expr.strip()):
        seg = seg.strip()
        if not seg:
            continue
        odd_only = "\u5355" in seg
        even_only = "\u53cc" in seg
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
            if 1 <= w <= max_week:
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
    s = e = arr[0]
    for p in arr[1:]:
        if p == e + 1:
            e = p
        else:
            merged.append((s, e))
            s = e = p
    merged.append((s, e))
    return merged


def period_range_time(start_p: int, end_p: int,
                      period_map: Dict[int, Tuple[str, str]]) -> Tuple[str, str]:
    override = PERIOD_RANGE_OVERRIDES.get((start_p, end_p))
    if override:
        return override

    def _fallback(p: int) -> Tuple[str, str]:
        s = 8 * 60 + (p - 1) * 55
        return (f"{s // 60:02d}:{s % 60:02d}",
                f"{(s + 45) // 60:02d}:{(s + 45) % 60:02d}")

    st = period_map.get(start_p, _fallback(start_p))[0]
    et = period_map.get(end_p, _fallback(end_p))[1]
    return st, et


def normalize_teacher(name: str) -> str:
    if not name:
        return ""
    parts = [p.strip() for p in re.split(r"[,\uff0c\u3001/]+", name) if p.strip()]
    dedup: List[str] = []
    seen: set = set()
    for p in parts:
        if p not in seen:
            seen.add(p)
            dedup.append(p)
    return "\u3001".join(dedup)


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
#  Phase 1: 门户认证 + aTrust 隧道 (urllib)
# ---------------------------------------------------------------------------

def _sso_login(opener, ocr, encrypted_pwd: str, username: str,
               service: str, max_retries: int = 8) -> str:
    for attempt in range(1, max_retries + 1):
        req = urllib.request.Request(
            f"{_SSO_ORIGIN}/lyuapServer/kaptcha?uid=&sf_request_type=ajax",
            headers={"User-Agent": _UA})
        with opener.open(req, timeout=30) as resp:
            kdata = json.loads(resp.read().decode("utf-8-sig"))

        uid = kdata["uid"]
        img_b64 = kdata["content"].split(",", 1)[1]
        raw_text = ocr.classification(base64.b64decode(img_b64))
        code = _solve_captcha_expr(raw_text)
        print(f"  SSO 尝试 {attempt}/{max_retries}: OCR={raw_text!r} -> {code}")

        form = urllib.parse.urlencode({
            "username": username, "password": encrypted_pwd,
            "service": service, "loginType": "",
            "id": uid, "code": code, "otpcode": "",
        }).encode()
        req = urllib.request.Request(
            f"{_SSO_ORIGIN}/lyuapServer/v1/tickets?sf_request_type=ajax",
            data=form, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "User-Agent": _UA})
        with opener.open(req, timeout=30) as resp:
            lr = json.loads(resp.read().decode("utf-8-sig"))

        ticket = lr.get("ticket") or lr.get("data", {})
        if isinstance(ticket, dict):
            if ticket.get("code") == "CODEFALSE":
                continue
            ticket = ticket.get("ticket", "")
        if ticket and str(ticket).startswith("ST-"):
            return str(ticket)

    raise RuntimeError(f"SSO 登录失败：验证码识别连续 {max_retries} 次未通过")


def portal_auth(username: str, password: str, ocr) -> http.cookiejar.CookieJar:
    ssl_ctx = _build_ssl_context()
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cj),
        urllib.request.HTTPSHandler(context=ssl_ctx))
    encrypted_pwd = _rsa_encrypt_block(password)

    # 1. SSO 登录 -> 门户 ticket
    portal_cas = f"{_PORTAL_ORIGIN}/passport/v1/auth/cas?sfDomain=1"
    ticket = _sso_login(opener, ocr, encrypted_pwd, username, portal_cas)
    print("  门户 SSO ticket 获取成功")

    # 2. CAS 回调
    try:
        opener.open(urllib.request.Request(
            f"{portal_cas}&ticket={urllib.parse.quote(ticket, safe='')}",
            headers={"User-Agent": _UA}), timeout=30)
    except Exception:
        pass

    # 3. 门户激活
    host = urllib.parse.urlsplit(_PORTAL_ORIGIN).netloc
    xr = base64.b64encode(host.encode()).decode()
    bp = {"clientType": "SDPBrowserClient", "platform": "Windows", "lang": "zh-CN"}

    def _papi(method, path, params=None, body=None, csrf=""):
        url = f"{_PORTAL_ORIGIN}{path}?" + urllib.parse.urlencode(
            {**bp, **(params or {})})
        hdrs = {"User-Agent": _UA, "Accept": "application/json",
                "Referer": f"{_PORTAL_ORIGIN}/portal/shortcut.html",
                "Origin": _PORTAL_ORIGIN, "x-sdp-rid": xr}
        if csrf:
            hdrs["x-csrf-token"] = csrf
        rb = json.dumps(body).encode() if body else None
        if rb:
            hdrs["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=rb, method=method, headers=hdrs)
        try:
            with opener.open(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read().decode("utf-8", "replace"))
            except Exception:
                return {}

    cfg = _papi("GET", "/passport/v1/public/authConfig",
                {"mod": "1", "sfDomain": "1"})
    csrf = cfg.get("data", {}).get("security", {}).get("csrfToken", "")

    sid_val = next((c.value for c in cj if c.name == "sid"), "")
    dev_id = hashlib.md5(f"{random.random()}{time.time()}".encode()).hexdigest()
    _papi("POST", "/controller/v1/public/reportEnv", csrf=csrf,
          body={"ticket": sid_val, "deviceId": dev_id,
                "env": {"endpoint": {"device_id": dev_id,
                                     "device": {"type": "browser"}}}})

    ac = _papi("GET", "/passport/v1/auth/authCheck", csrf=csrf)
    sid_ticket = ac.get("data", {}).get("sidTicket", "")
    if sid_ticket:
        _papi("POST", "/passport/v1/public/sessionIdExchange", csrf=csrf,
              body={"sidTicket": sid_ticket})

    # 4. 建立教务系统隧道
    opener.open(urllib.request.Request(
        f"{_JW_ORIGIN}/", headers={"User-Agent": _UA}), timeout=30).read()
    print("  门户认证 + 隧道建立完成")
    return cj


# ---------------------------------------------------------------------------
#  Phase 2: Playwright 浏览器 SPA 认证 + 数据捕获
# ---------------------------------------------------------------------------

def fetch_schedule_data(cj: http.cookiejar.CookieJar, username: str,
                        password: str, ocr) -> Tuple[dict, List[int], list]:
    from playwright.sync_api import sync_playwright

    encrypted_pwd = _rsa_encrypt_block(password)

    # 转移 Cookie（排除 LYSESSIONID / user，由浏览器 CAS 获取）
    pw_cookies = []
    for c in cj:
        if c.name in ("LYSESSIONID", "user"):
            continue
        pw_cookies.append({
            "name": c.name, "value": c.value,
            "domain": c.domain, "path": c.path or "/", "secure": True,
        })

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(ignore_https_errors=True, user_agent=_UA)
        for c in pw_cookies:
            try:
                ctx.add_cookies([c])
            except Exception:
                pass
        page = ctx.new_page()

        # 访问教务系统根路径（建立 sdp_app_session）
        page.goto(f"{_JW_ORIGIN}/", timeout=60000,
                  wait_until="domcontentloaded")
        time.sleep(1)

        # 导航到 SSO 登录页
        page.goto(
            f"{_SSO_ORIGIN}/lyuapServer/login"
            f"?service={urllib.parse.quote(_CAS_SERVICE, safe='')}",
            timeout=30000)

        # 在浏览器中完成 SSO 登录
        jw_ticket = None
        for attempt in range(8):
            r = page.evaluate(
                "async()=>{const r=await fetch("
                "'/lyuapServer/kaptcha?uid=&sf_request_type=ajax');"
                "return await r.json()}")
            img_b64 = r.get("content", "").split(",", 1)[-1]
            raw_text = ocr.classification(base64.b64decode(img_b64))
            code = _solve_captcha_expr(raw_text)
            uid = r.get("uid", "")
            print(f"  浏览器 SSO 尝试 {attempt + 1}/8: "
                  f"OCR={raw_text!r} -> {code}")

            lr = page.evaluate(
                f"async()=>{{const f=new URLSearchParams({{"
                f"username:'{username}',"
                f"password:'{encrypted_pwd}',"
                f"service:'{_CAS_SERVICE}',"
                f"loginType:'',id:'{uid}',code:'{code}',otpcode:''}});"
                f"const r=await fetch("
                f"'/lyuapServer/v1/tickets?sf_request_type=ajax',"
                f"{{method:'POST',headers:"
                f"{{'Content-Type':'application/x-www-form-urlencoded'}},"
                f"body:f.toString()}});return await r.json()}}")

            ticket = lr.get("ticket") or lr.get("data", {})
            if isinstance(ticket, dict):
                if ticket.get("code") == "CODEFALSE":
                    continue
                ticket = ticket.get("ticket", "")
            if ticket and str(ticket).startswith("ST-"):
                jw_ticket = str(ticket)
                break

        if not jw_ticket:
            browser.close()
            raise RuntimeError("浏览器 SSO 登录失败")
        print("  浏览器 SSO ticket 获取成功")

        # 路由拦截：将课表请求修改为查询全部周次
        captured: Dict[str, str] = {}

        def handle_route(route):
            req = route.request
            if "studentCourseSchedule" in req.url and req.method == "POST":
                try:
                    body = json.loads(req.post_data) if req.post_data else {}
                    body["weeks"] = list(range(1, 30))
                    route.continue_(post_data=json.dumps(body))
                    return
                except Exception:
                    pass
            route.continue_()

        page.route("**/api/**", handle_route)

        # 捕获 API 响应
        def capture_response(resp):
            url = resp.url
            if "/api/" not in url or "jiaowu" not in url:
                return
            if resp.status != 200:
                return
            try:
                body = resp.text()
            except Exception:
                return
            parsed = urllib.parse.urlsplit(url)
            key = parsed.path
            if parsed.query:
                key += "?" + parsed.query
            captured[key] = body

        page.on("response", capture_response)

        # CAS 回调 -> SPA 加载 -> 自动触发所有 API
        cb_url = (f"{_JW_ORIGIN}/api/api/cas/login?pattern=teacher-login"
                  f"&ticket={urllib.parse.quote(jw_ticket, safe='')}")
        page.goto(cb_url, timeout=60000, wait_until="networkidle")
        time.sleep(5)
        browser.close()

    print(f"  捕获到 {len(captured)} 个 API 响应")

    # --- 解析学期信息 ---
    semester_info = None
    for key, val in captured.items():
        if "selectCurrentXnXq" in key:
            semester_info = json.loads(val).get("data", {})
            break
    if not semester_info:
        raise RuntimeError("未获取到学期信息")

    # --- 解析周次列表（优先取不带 type 参数的完整列表）---
    all_weeks: List[int] = []
    for key, val in captured.items():
        if "queryWeek" in key and "type=" not in key:
            all_weeks = json.loads(val).get("data", [])
            break
    if not all_weeks:
        for key, val in captured.items():
            if "queryWeek" in key:
                all_weeks = json.loads(val).get("data", [])
                break
    if not all_weeks:
        all_weeks = list(range(1, 20))

    # --- 解析课表 ---
    schedule_data = None
    for key, val in captured.items():
        if "studentCourseSchedule" in key:
            schedule_data = json.loads(val).get("data", [])
            break
    if not schedule_data:
        raise RuntimeError("未获取到课表数据")

    return semester_info, all_weeks, schedule_data


# ---------------------------------------------------------------------------
#  Phase 3: 生成 ICS
# ---------------------------------------------------------------------------

def generate_ics(semester_info: dict, all_weeks: List[int],
                 schedule_data: list,
                 period_map: Dict[int, Tuple[str, str]],
                 calendar_name: str) -> Tuple[str, int]:
    semester = semester_info.get("semester", "")
    start_date_str = semester_info.get("ksrq", "")
    if not start_date_str:
        raise RuntimeError("学期开始日期缺失")

    week1_monday = date.fromisoformat(start_date_str)
    week1_monday -= timedelta(days=week1_monday.weekday())
    max_week = max(all_weeks) if all_weeks else 19

    # 从 SPA 响应提取课程行
    rows: List[dict] = []
    for item in schedule_data:
        for course in item.get("courseList", []):
            if not str(course.get("courseName", "")).strip():
                continue
            rows.append(course)

    # 按 (课程, 教师, 教室, dayOfWeek, 周次, 班级) 分组合并节次
    grouped: Dict[Tuple, set] = {}
    for r in rows:
        dow = int(r.get("dayOfWeek", 0))
        offset = DAY_OFFSET_MAP.get(dow)
        if offset is None:
            continue

        course_name = str(r.get("courseName", "")).strip()
        teacher = normalize_teacher(str(r.get("teacherName", "")).strip())
        classroom = re.sub(r"\s+", " ", str(r.get("classroomName", "")).strip())
        class_name = str(r.get("teachingClassName", "")).strip()
        weeks_expr = str(r.get("weeks", "")).strip()

        periods = parse_periods(r.get("time"))
        if not periods:
            periods = parse_periods(r.get("sectionName"))
        if not periods:
            continue

        key = (course_name, teacher, classroom, offset, weeks_expr, class_name)
        grouped.setdefault(key, set()).update(periods)

    events = []
    for (course_name, teacher, classroom, offset, weeks_expr, class_name), \
            period_set in grouped.items():
        week_nums = parse_weeks_expr(weeks_expr, max_week)
        if not week_nums:
            try:
                week_nums = [int(weeks_expr)]
            except ValueError:
                continue

        period_ranges = merge_periods(period_set)
        for wn in week_nums:
            day_date = week1_monday + timedelta(days=offset + (wn - 1) * 7)
            for start_p, end_p in period_ranges:
                st_s, et_s = period_range_time(start_p, end_p, period_map)
                h1, m1 = st_s.split(":")
                h2, m2 = et_s.split(":")
                dt_start = datetime(day_date.year, day_date.month, day_date.day,
                                    int(h1), int(m1), tzinfo=TZ_CST)
                dt_end = datetime(day_date.year, day_date.month, day_date.day,
                                  int(h2), int(m2), tzinfo=TZ_CST)
                if dt_end <= dt_start:
                    dt_end += timedelta(days=1)

                desc = "\n".join([
                    f"教师: {teacher or '待定'}",
                    f"班级: {class_name or '待定'}",
                    f"周次: {weeks_expr}",
                    f"节次: 第{start_p}-{end_p}节",
                    f"学期: {semester}",
                    "来源: 江西工程学院课程系统",
                ])

                uid_seed = (f"{course_name}|{teacher}|{classroom}|"
                            f"{day_date.isoformat()}|{start_p}-{end_p}|"
                            f"{weeks_expr}")
                uid = hashlib.sha1(uid_seed.encode()).hexdigest() \
                    + "@curriculum"

                events.append((dt_start, {
                    "uid": uid,
                    "start": dt_start,
                    "end": dt_end,
                    "summary": course_name,
                    "location": classroom or "待定教室",
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
        f"X-WR-CALNAME:{ics_escape(calendar_name)}",
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
            f"DTSTART;TZID=Asia/Shanghai:"
            f"{ev['start'].strftime('%Y%m%dT%H%M%S')}",
            f"DTEND;TZID=Asia/Shanghai:"
            f"{ev['end'].strftime('%Y%m%dT%H%M%S')}",
            f"SUMMARY:{ics_escape(ev['summary'])}",
            f"LOCATION:{ics_escape(ev['location'])}",
            f"DESCRIPTION:{ics_escape(ev['description'])}",
            "END:VEVENT",
        ])

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n", len(events)


# ---------------------------------------------------------------------------
#  主函数
# ---------------------------------------------------------------------------

def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(line_buffering=True)

    try:
        import ddddocr  # noqa: F811
    except ImportError:
        print("缺少 ddddocr，请先运行: pip install ddddocr", file=sys.stderr)
        return 1

    username = os.getenv("SSO_USERNAME", "").strip()
    password = os.getenv("SSO_PASSWORD", "").strip()
    if not username or not password:
        print("缺少环境变量: SSO_USERNAME 和 SSO_PASSWORD", file=sys.stderr)
        return 1

    output_path = os.getenv("CURRICULUM_OUTPUT", "curriculum.ics")
    calendar_name = os.getenv("CURRICULUM_CALENDAR_NAME", "课程表")
    period_map = load_period_map()

    try:
        ocr = ddddocr.DdddOcr(show_ad=False)

        print("Phase 1: 门户认证...")
        cj = portal_auth(username, password, ocr)

        print("Phase 2: SPA 认证 + 数据抓取...")
        semester_info, all_weeks, schedule_data = \
            fetch_schedule_data(cj, username, password, ocr)
        print(f"  学期: {semester_info.get('semester')}")
        print(f"  周次: {all_weeks}")
        course_count = sum(
            len(item.get("courseList", []))
            for item in schedule_data)
        print(f"  课程条目: {course_count}")

        print("Phase 3: 生成 ICS...")
        ics_text, count = generate_ics(
            semester_info, all_weeks, schedule_data,
            period_map, calendar_name)

        with open(output_path, "w", encoding="utf-8", newline="") as f:
            f.write(ics_text)
        print(f"已生成 {output_path}，事件数: {count}")
        return 0

    except Exception as e:
        print(f"同步失败: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
