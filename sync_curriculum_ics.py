#!/usr/bin/env python3
import hashlib
import html
import json
import os
import re
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


def env(name: str, default: Optional[str] = None, required: bool = False) -> str:
    val = os.getenv(name, default)
    if required and (val is None or str(val).strip() == ""):
        raise RuntimeError(f"缺少环境变量: {name}")
    return "" if val is None else str(val)


def parse_entry(entry_url: str) -> Dict[str, str]:
    # 兼容从网页复制出来的 URL（可能包含 &amp;）
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

    # 兼容网关中转链接：...shortcut.html?appUrl=<encode(url)>
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

    # 最后兜底：从整串文本里做正则提取
    if not result["accessToken"]:
        m = re.search(r"(?:^|[?&])accessToken=([^&\\s]+)", normalized)
        if m:
            result["accessToken"] = urllib.parse.unquote(m.group(1))
    if not result["id"]:
        m = re.search(r"(?:^|[?&])id=([^&\\s]+)", normalized)
        if m:
            result["id"] = urllib.parse.unquote(m.group(1))
    if not result["userType"]:
        m = re.search(r"(?:^|[?&])userType=([^&\\s]+)", normalized)
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
    st = period_map.get(start_period, fallback_period_time(start_period))[0]
    et = period_map.get(end_period, fallback_period_time(end_period))[1]
    return st, et


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
    # 优先环境变量，其次仓库内 period_times.json，最后默认值
    inline = os.getenv("CURRICULUM_PERIOD_TIMES_JSON", "").strip()
    if inline:
        obj = json.loads(inline.lstrip("\ufeff"))
        return {int(k): (str(v[0]), str(v[1])) for k, v in obj.items()}

    path = os.getenv("CURRICULUM_PERIOD_TIMES_FILE", "period_times.json")
    if os.path.exists(path):
        # 兼容带 UTF-8 BOM 的 JSON 文件
        with open(path, "r", encoding="utf-8-sig") as f:
            obj = json.load(f)
        return {int(k): (str(v[0]), str(v[1])) for k, v in obj.items()}

    return dict(DEFAULT_PERIOD_TIMES)


def build_config() -> Config:
    entry_url = env("CURRICULUM_ENTRY_URL", required=True)
    parsed = parse_entry(entry_url)

    access_token = env("CURRICULUM_ACCESS_TOKEN", parsed.get("accessToken") or "", required=False).strip()
    if not access_token:
        raise RuntimeError("缺少 accessToken：请设置 CURRICULUM_ACCESS_TOKEN，或在 CURRICULUM_ENTRY_URL 中带上 accessToken 参数")

    user_id = env("CURRICULUM_USER_ID", parsed.get("id") or "", required=False).strip()
    if not user_id:
        raise RuntimeError("缺少 userId：请设置 CURRICULUM_USER_ID，或在 CURRICULUM_ENTRY_URL 中带上 id 参数")

    user_type = env("CURRICULUM_USER_TYPE", parsed.get("userType") or "0", required=False).strip() or "0"

    cookie = env("CURRICULUM_COOKIE", required=True)
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
    with urllib.request.urlopen(req, timeout=30) as resp:
        # 兼容接口返回体带 UTF-8 BOM
        raw = resp.read().decode("utf-8-sig", "replace").lstrip("\ufeff")
        ct = (resp.headers.get("Content-Type") or "").lower()

    if "json" not in ct and not raw.strip().startswith("{"):
        raise ApiError(f"接口返回非 JSON，可能登录态失效: {path}")

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

    payload = {
        "academicYearSemester": semester,
        "userId": config.user_id,
        "userType": config.user_type,
        "weeks": week_codes,
    }
    schedule = request_json(origin, headers, "POST", "/api/arrange/mobile/courseSchedule/courseSchedule", payload).get("data", {})

    # 取今天日期作为当前周锚点（比系统时区更稳）
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

    rows = [r for r in schedule.get("course", []) if str(r.get("courseName") or "").strip()]

    grouped: Dict[Tuple[str, str, str, int, str, str], set] = {}
    for r in rows:
        offset = day_offset(r)
        if offset is None:
            continue

        course = str(r.get("courseName") or "").strip()
        teacher = normalize_teacher(str(r.get("teacherName") or "").strip())
        classroom = re.sub(r"\s+", " ", str(r.get("classroomName") or "").strip())
        class_name = str(r.get("teachingClassName") or r.get("className") or "").strip()
        weeks_expr = str(r.get("weeks") or "").strip()

        periods = parse_periods(r.get("time"))
        if not periods:
            periods = parse_periods(r.get("sectionName"))
        if not periods:
            continue

        key = (course, teacher, classroom, offset, weeks_expr, class_name)
        grouped.setdefault(key, set()).update(periods)

    events = []
    for (course, teacher, classroom, offset, weeks_expr, class_name), period_set in grouped.items():
        week_nums = parse_weeks_expr(weeks_expr, max_week)
        if not week_nums:
            week_nums = [current_week]

        period_ranges = merge_periods(period_set)
        for wn in week_nums:
            day_date = current_week_monday + timedelta(days=offset + (wn - current_week) * 7)
            for start_p, end_p in period_ranges:
                st_s, et_s = period_range_time(start_p, end_p, config.period_times)
                dt_start = parse_hhmm_to_dt(day_date, st_s)
                dt_end = parse_hhmm_to_dt(day_date, et_s)
                if dt_end <= dt_start:
                    dt_end += timedelta(days=1)

                summary = course
                location = classroom or "待定教室"
                desc = "\n".join([
                    f"教师: {teacher or '待定'}",
                    f"班级: {class_name or '待定'}",
                    f"周次规则: {weeks_expr or str(wn)}",
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
    try:
        cfg = build_config()
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
