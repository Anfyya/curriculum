#!/usr/bin/env python3
import base64
import hashlib
import html
import http.cookiejar
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
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
    opener: Optional[urllib.request.OpenerDirector] = None


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


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except Exception:
        return default


def _is_retryable_error(err: Exception) -> bool:
    if isinstance(err, (TimeoutError, socket.timeout)):
        return True
    if isinstance(err, urllib.error.URLError):
        reason = getattr(err, "reason", None)
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return True
        msg = str(reason or err).lower()
        retry_tokens = (
            "timed out",
            "temporarily unavailable",
            "connection reset",
            "connection aborted",
            "network is unreachable",
            "name or service not known",
            "connection refused",
        )
        return any(t in msg for t in retry_tokens)
    return False


def _open_with_retry(
    req: urllib.request.Request,
    *,
    opener: Optional[urllib.request.OpenerDirector] = None,
    context: Optional[ssl.SSLContext] = None,
    timeout: int = 30,
    retries: int = 3,
    label: str = "request",
):
    retries = max(1, retries)
    last_err: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            if opener is not None:
                return opener.open(req, timeout=timeout)
            if context is not None:
                return urllib.request.urlopen(req, timeout=timeout, context=context)
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            if attempt < retries and e.code in (408, 429, 500, 502, 503, 504):
                wait_s = min(1.0 + attempt * 0.8, 4.0)
                print(f"  {label} HTTP {e.code}，重试 {attempt}/{retries} ...")
                time.sleep(wait_s)
                continue
            raise
        except Exception as e:
            last_err = e
            if attempt >= retries or not _is_retryable_error(e):
                raise
            wait_s = min(1.0 + attempt * 0.8, 4.0)
            print(f"  {label} 网络异常，重试 {attempt}/{retries}: {e}")
            time.sleep(wait_s)
    if last_err is not None:
        raise last_err
    raise RuntimeError(f"{label} 请求失败")


def _domain_matches(host: str, cookie_domain: str) -> bool:
    if not host or not cookie_domain:
        return False
    h = host.lower().strip(".")
    d = cookie_domain.lower().strip(".")
    return h == d or h.endswith("." + d)


def _cookies_for_host(cj: http.cookiejar.CookieJar, host: str) -> str:
    parts: List[str] = []
    seen = set()
    for c in cj:
        if c.name in seen:
            continue
        if _domain_matches(host, c.domain or ""):
            seen.add(c.name)
            parts.append(f"{c.name}={c.value}")
    return "; ".join(parts)


def _get_service_ticket(username: str, password: str, max_retries: int = 5) -> str:
    try:
        import ddddocr  # type: ignore
    except ImportError:
        raise RuntimeError("自动登录需要 ddddocr")

    ocr = ddddocr.DdddOcr(show_ad=False)
    ssl_ctx = _build_ssl_context()
    cas_name = "/lyuapServer"
    encrypted_pwd = _rsa_encrypt_block(password)
    sso_timeout = _env_int("CURRICULUM_SSO_TIMEOUT", 20)
    sso_net_retries = _env_int("CURRICULUM_SSO_NET_RETRIES", 2)

    for attempt in range(1, max_retries + 1):
        kaptcha_url = f"{_SSO_ORIGIN}{cas_name}/kaptcha?uid=&sf_request_type=ajax"
        req = urllib.request.Request(kaptcha_url)
        with _open_with_retry(
            req,
            context=ssl_ctx,
            timeout=sso_timeout,
            retries=sso_net_retries,
            label="kaptcha",
        ) as resp:
            kdata = json.loads(resp.read().decode("utf-8-sig"))

        uid = kdata["uid"]
        img_b64 = kdata["content"].split(",", 1)[1]
        img_bytes = base64.b64decode(img_b64)

        raw_text = ocr.classification(img_bytes)
        code = _solve_captcha_expr(raw_text)
        print(f"  auto_login 尝试 {attempt}/{max_retries}: OCR={raw_text!r} -> code={code}")

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
        req = urllib.request.Request(
            login_url,
            data=form,
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            },
        )
        with _open_with_retry(
            req,
            context=ssl_ctx,
            timeout=sso_timeout,
            retries=sso_net_retries,
            label="sso_login",
        ) as resp:
            login_body = resp.read().decode("utf-8-sig")
        login_resp = json.loads(login_body)

        ticket = login_resp.get("ticket") or login_resp.get("data", {})
        if isinstance(ticket, dict):
            err_code = ticket.get("code", "")
            if err_code == "CODEFALSE":
                print("  验证码错误，重试...")
                continue
            if err_code == "BINDPHONE":
                raise RuntimeError("SSO 要求绑定手机，无法自动登录")
            if err_code:
                raise RuntimeError(f"SSO 登录返回错误码: {err_code}")
            ticket = ticket.get("ticket") or ""

        ticket = str(ticket)
        if ticket.startswith("ST-"):
            return ticket
        raise RuntimeError(f"SSO 登录返回未知格式: {login_body[:300]}")

    raise RuntimeError(f"验证码识别连续 {max_retries} 次失败")


def _find_chrome_bin() -> Optional[str]:
    env_val = (os.getenv("CURRICULUM_CHROME_BIN") or os.getenv("CHROME_BIN") or "").strip()
    if env_val and os.path.exists(env_val):
        return env_val

    local_appdata = os.getenv("LOCALAPPDATA", "").strip()
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.join(local_appdata, "Google", "Chrome", "Application", "chrome.exe") if local_appdata else "",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
    ]
    for p in candidates:
        if p and os.path.exists(p):
            return p

    for cmd in ("google-chrome", "chromium-browser", "chromium", "chrome"):
        p = shutil.which(cmd)
        if p:
            return p
    return None


def _browser_cookie_login(callback_url: str, entry_url: str) -> Tuple[str, str]:
    node_bin = shutil.which("node")
    if not node_bin:
        raise RuntimeError("未找到 node，无法执行浏览器登录桥接")

    chrome_bin = _find_chrome_bin()
    if not chrome_bin:
        raise RuntimeError("未找到 Chrome/Chromium，请设置 CURRICULUM_CHROME_BIN")

    js_code = r"""
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const callbackUrl = process.argv[2];
const entryUrl = process.argv[3];
const outPath = process.argv[4];
const chromePath = process.argv[5];
const debugPort = Number(process.argv[6] || '0');

function sleep(ms){ return new Promise(r=>setTimeout(r, ms)); }
async function waitForJsonVersion(port, getErr, timeoutMs=25000){
  const end = Date.now() + timeoutMs;
  while (Date.now() < end){
    try {
      const r = await fetch(`http://127.0.0.1:${port}/json/version`);
      if (r.ok) return await r.json();
    } catch {}
    await sleep(200);
  }
  const tail = (getErr ? String(getErr() || '') : '').slice(-1500);
  throw new Error('waitForJsonVersion timeout' + (tail ? `; chromeErr=${tail}` : ''));
}

async function main(){
  const profileDir = path.join(os.tmpdir(), 'curriculum_chrome_' + Date.now());
  fs.mkdirSync(profileDir, { recursive: true });

  const cp = spawn(chromePath, [
    `--remote-debugging-port=${debugPort}`,
    '--headless=new',
    '--disable-gpu',
    '--no-sandbox',
    '--disable-setuid-sandbox',
    '--disable-dev-shm-usage',
    '--no-first-run',
    '--no-default-browser-check',
    '--window-size=1366,900',
    `--user-data-dir=${profileDir}`,
    'about:blank'
  ], { stdio: ['ignore', 'pipe', 'pipe'] });

  let chromeErr = '';
  cp.stderr.on('data', (chunk) => {
    chromeErr += String(chunk || '');
    if (chromeErr.length > 12000) chromeErr = chromeErr.slice(-12000);
  });
  cp.on('error', (e) => {
    chromeErr += `\n[spawn_error] ${e && e.message ? e.message : String(e)}`;
  });

  try {
    const info = await waitForJsonVersion(debugPort, () => chromeErr);
    if (typeof WebSocket === 'undefined') {
      throw new Error('global WebSocket is undefined in current Node runtime');
    }
    const ws = new WebSocket(info.webSocketDebuggerUrl);
    const pending = new Map();
    let id = 0;

    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id && pending.has(msg.id)) {
        const p = pending.get(msg.id);
        pending.delete(msg.id);
        if (msg.error) p.reject(new Error(JSON.stringify(msg.error)));
        else p.resolve(msg.result);
      }
    };
    await new Promise((resolve, reject) => {
      ws.onopen = resolve;
      ws.onerror = reject;
    });

    const send = (method, params = {}, sessionId = null) => {
      const mid = ++id;
      const payload = { id: mid, method, params };
      if (sessionId) payload.sessionId = sessionId;
      ws.send(JSON.stringify(payload));
      return new Promise((resolve, reject) => pending.set(mid, { resolve, reject }));
    };

    const { targetId } = await send('Target.createTarget', { url: 'about:blank' });
    const { sessionId } = await send('Target.attachToTarget', { targetId, flatten: true });
    await send('Page.enable', {}, sessionId);
    await send('Runtime.enable', {}, sessionId);

    await send('Page.navigate', { url: callbackUrl }, sessionId);
    let href1 = '';
    for (let i = 0; i < 20; i++) {
      await sleep(1000);
      const r = await send('Runtime.evaluate', { expression: 'location.href', returnByValue: true }, sessionId).catch(() => null);
      href1 = (r && r.result && r.result.value) ? String(r.result.value) : '';
      if (href1.includes('/portal/?redirectid=') || href1.includes('#/app_center')) break;
    }

    await send('Page.navigate', { url: entryUrl }, sessionId);
    let href2 = '';
    for (let i = 0; i < 15; i++) {
      await sleep(1000);
      const r = await send('Runtime.evaluate', { expression: 'location.href', returnByValue: true }, sessionId).catch(() => null);
      href2 = (r && r.result && r.result.value) ? String(r.result.value) : '';
      if (href2.includes('zichan.jxec.edu.cn')) break;
    }

    const c = await send('Storage.getCookies');
    fs.writeFileSync(outPath, JSON.stringify({ href1, href2, cookies: (c && c.cookies) ? c.cookies : [] }), 'utf8');
    ws.close();
  } finally {
    if (cp.exitCode !== null && cp.exitCode !== 0 && chromeErr) {
      console.error('[chrome_stderr]', chromeErr);
    }
    try { cp.kill(); } catch {}
    await sleep(300);
    try { fs.rmSync(profileDir, { recursive: true, force: true }); } catch {}
  }
}

main().catch((e) => {
  console.error(e && e.stack ? e.stack : String(e));
  process.exit(1);
});
"""

    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f_js:
        f_js.write(js_code)
        js_path = f_js.name
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f_out:
        out_path = f_out.name

    debug_port = str(int(os.getenv("CURRICULUM_CHROME_DEBUG_PORT", "9262")))
    browser_timeout = max(60, _env_int("CURRICULUM_BROWSER_TIMEOUT", 120))
    result: dict
    try:
        subprocess.run(
            [node_bin, js_path, callback_url, entry_url, out_path, chrome_bin, debug_port],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=browser_timeout,
        )
    except subprocess.CalledProcessError as e:
        stderr_tail = (e.stderr or "").strip()[-1600:]
        stdout_tail = (e.stdout or "").strip()[-600:]
        details = f"exit_code={e.returncode}"
        if stderr_tail:
            details += f", stderr={stderr_tail}"
        if stdout_tail:
            details += f", stdout={stdout_tail}"
        raise RuntimeError(f"浏览器登录子进程失败: {details}") from e
    except subprocess.TimeoutExpired as e:
        stderr_tail = (e.stderr or "").strip()[-1600:] if isinstance(e.stderr, str) else ""
        raise RuntimeError(f"浏览器登录子进程超时({browser_timeout}s){', stderr=' + stderr_tail if stderr_tail else ''}") from e
    try:
        with open(out_path, "r", encoding="utf-8") as f:
            result = json.load(f)
    except Exception as e:
        raise RuntimeError(f"浏览器登录结果读取失败: {e}") from e
    finally:
        try:
            os.remove(js_path)
        except OSError:
            pass
        try:
            os.remove(out_path)
        except OSError:
            pass

    entry_host = (urllib.parse.urlsplit(entry_url).hostname or "").strip()
    portal_host = (urllib.parse.urlsplit(_PORTAL_ORIGIN).hostname or "").strip()

    cookie_parts: List[str] = []
    seen = set()
    for c in result.get("cookies", []):
        domain = str(c.get("domain") or "").lstrip(".")
        if not (_domain_matches(entry_host, domain) or _domain_matches(portal_host, domain)):
            continue
        name = str(c.get("name") or "").strip()
        value = str(c.get("value") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        cookie_parts.append(f"{name}={value}")

    if not cookie_parts:
        raise RuntimeError("浏览器登录后未提取到可用 Cookie")

    final_href = str(result.get("href2") or entry_url)
    print(
        f"  浏览器登录完成，Cookie {len(cookie_parts)} 项，"
        f"href1={result.get('href1','-')} href2={final_href}"
    )
    return "; ".join(cookie_parts), final_href


def auto_login_cookie_via_browser(
    username: str,
    password: str,
    entry_url: str,
    max_retries: int = 5,
) -> Tuple[str, str]:
    ticket = _get_service_ticket(username, password, max_retries=max_retries)
    callback_url = f"{_CAS_SERVICE}&ticket={urllib.parse.quote(ticket, safe='')}"
    browser_retries = max(1, _env_int("CURRICULUM_BROWSER_RETRIES", 1))
    last_err: Optional[Exception] = None
    for i in range(1, browser_retries + 1):
        try:
            return _browser_cookie_login(callback_url, entry_url)
        except Exception as e:
            last_err = e
            if i >= browser_retries:
                raise
            print(f"  浏览器登录失败，准备重试 {i}/{browser_retries}: {e}")
            time.sleep(min(1.0 + i * 0.8, 4.0))
    if last_err is not None:
        raise last_err
    raise RuntimeError("浏览器登录失败")


def auto_login_with_session(
    username: str,
    password: str,
    entry_url: str,
    max_retries: int = 5,
) -> Tuple[str, urllib.request.OpenerDirector]:
    try:
        import ddddocr  # type: ignore
    except ImportError:
        raise RuntimeError("自动登录需要 ddddocr")

    ocr = ddddocr.DdddOcr(show_ad=False)
    ssl_ctx = _build_ssl_context()
    target_host = (urllib.parse.urlsplit(entry_url).hostname or "").strip()
    sso_timeout = _env_int("CURRICULUM_SSO_TIMEOUT", 20)
    sso_net_retries = _env_int("CURRICULUM_SSO_NET_RETRIES", 2)

    cas_name = "/lyuapServer"
    encrypted_pwd = _rsa_encrypt_block(password)

    for attempt in range(1, max_retries + 1):
        cj = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cj),
            urllib.request.HTTPSHandler(context=ssl_ctx),
        )

        kaptcha_url = f"{_SSO_ORIGIN}{cas_name}/kaptcha?uid=&sf_request_type=ajax"
        req = urllib.request.Request(kaptcha_url)
        with _open_with_retry(
            req,
            opener=opener,
            timeout=sso_timeout,
            retries=sso_net_retries,
            label="kaptcha",
        ) as resp:
            kdata = json.loads(resp.read().decode("utf-8-sig"))

        uid = kdata["uid"]
        img_b64 = kdata["content"].split(",", 1)[1]
        img_bytes = base64.b64decode(img_b64)

        raw_text = ocr.classification(img_bytes)
        code = _solve_captcha_expr(raw_text)
        print(f"  auto_login 尝试 {attempt}/{max_retries}: OCR={raw_text!r} -> code={code}")

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
        req = urllib.request.Request(
            login_url,
            data=form,
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            },
        )
        with _open_with_retry(
            req,
            opener=opener,
            timeout=sso_timeout,
            retries=sso_net_retries,
            label="sso_login",
        ) as resp:
            login_body = resp.read().decode("utf-8-sig")

        login_resp = json.loads(login_body)
        ticket = login_resp.get("ticket") or login_resp.get("data", {})
        if isinstance(ticket, dict):
            err_code = ticket.get("code", "")
            if err_code == "CODEFALSE":
                print("  验证码错误，重试...")
                continue
            if err_code == "BINDPHONE":
                raise RuntimeError("SSO 要求绑定手机，无法自动登录")
            if err_code:
                raise RuntimeError(f"SSO 登录返回错误码: {err_code}")
            ticket = ticket.get("ticket") or ""

        if not ticket or not str(ticket).startswith("ST-"):
            raise RuntimeError(f"SSO 登录返回未知格式: {login_body[:300]}")
        ticket = str(ticket)
        print("  SSO 登录成功，获取到 ticket")

        callback_url = f"{_CAS_SERVICE}&ticket={urllib.parse.quote(ticket, safe='')}"
        req = urllib.request.Request(
            callback_url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        )
        try:
            _open_with_retry(
                req,
                opener=opener,
                timeout=sso_timeout,
                retries=sso_net_retries,
                label="cas_callback",
            )
        except urllib.error.HTTPError:
            pass

        try:
            req = urllib.request.Request(
                entry_url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Referer": _PORTAL_ORIGIN + "/",
                },
            )
            _open_with_retry(
                req,
                opener=opener,
                timeout=sso_timeout,
                retries=sso_net_retries,
                label="entry_page",
            )
        except urllib.error.HTTPError:
            pass

        cookie_parts = [f"{c.name}={c.value}" for c in cj]
        if not cookie_parts:
            raise RuntimeError("登录成功但未获取到 Cookie")

        target_cookie = _cookies_for_host(cj, target_host)
        if target_cookie:
            print(f"  获取到 Cookie ({len(cookie_parts)} 项，包含业务域)")
            return target_cookie, opener

        print(f"  获取到 Cookie ({len(cookie_parts)} 项，业务域缺失，回退全量)")
        return "; ".join(cookie_parts), opener

    raise RuntimeError(f"验证码识别连续 {max_retries} 次失败")


def auto_login(username: str, password: str, max_retries: int = 5) -> str:
    try:
        import ddddocr  # type: ignore
    except ImportError:
        raise RuntimeError("auto_login 需要 ddddocr，请先 pip install ddddocr")

    ocr = ddddocr.DdddOcr(show_ad=False)
    ssl_ctx = _build_ssl_context()
    sso_timeout = _env_int("CURRICULUM_SSO_TIMEOUT", 20)
    sso_net_retries = _env_int("CURRICULUM_SSO_NET_RETRIES", 2)

    cas_name = "/lyuapServer"
    encrypted_pwd = _rsa_encrypt_block(password)

    for attempt in range(1, max_retries + 1):
        # --- 1. 获取验证码 ---
        kaptcha_url = f"{_SSO_ORIGIN}{cas_name}/kaptcha?uid=&sf_request_type=ajax"
        req = urllib.request.Request(kaptcha_url)
        with _open_with_retry(
            req,
            context=ssl_ctx,
            timeout=sso_timeout,
            retries=sso_net_retries,
            label="kaptcha",
        ) as resp:
            kdata = json.loads(resp.read().decode("utf-8-sig"))

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
        with _open_with_retry(
            req,
            context=ssl_ctx,
            timeout=sso_timeout,
            retries=sso_net_retries,
            label="sso_login",
        ) as resp:
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
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cj),
            urllib.request.HTTPSHandler(context=ssl_ctx),
        )

        callback_url = f"{_CAS_SERVICE}&ticket={urllib.parse.quote(ticket, safe='')}"
        req = urllib.request.Request(callback_url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        })
        try:
            _open_with_retry(
                req,
                opener=opener,
                timeout=sso_timeout,
                retries=sso_net_retries,
                label="cas_callback",
            )
        except urllib.error.HTTPError:
            pass

        cookie_parts = []
        for c in cj:
            cookie_parts.append(f"{c.name}={c.value}")

        if not cookie_parts:
            raise RuntimeError("CAS 回调后未获取到任何 Cookie")

        cookie_str = "; ".join(cookie_parts)
        print(f"  获取到 Cookie ({len(cookie_parts)} 项)")
        return cookie_str

    raise RuntimeError(f"验证码识别连续 {max_retries} 次失败，请检查 ddddocr 或手动登录")


def build_config() -> Config:
    entry_url_raw = env("CURRICULUM_ENTRY_URL", required=True)
    entry_url = normalize_entry_url(entry_url_raw)
    parsed = parse_entry(entry_url_raw)

    cookie_raw = env("CURRICULUM_COOKIE", "").strip()
    sso_user = env("SSO_USERNAME", "").strip()
    sso_pass = env("SSO_PASSWORD", "").strip()
    opener = None
    cookie = ""

    if sso_user and sso_pass:
        print("使用 SSO_USERNAME/SSO_PASSWORD 自动登录...")
        try:
            cookie, browser_entry_url = auto_login_cookie_via_browser(sso_user, sso_pass, entry_url)
            if browser_entry_url:
                entry_url = normalize_entry_url(browser_entry_url)
                browser_parsed = parse_entry(browser_entry_url)
                if browser_parsed.get("accessToken"):
                    parsed["accessToken"] = browser_parsed["accessToken"]
                if browser_parsed.get("id"):
                    parsed["id"] = browser_parsed["id"]
                if browser_parsed.get("userType"):
                    parsed["userType"] = browser_parsed["userType"]
        except Exception as e:
            print(f"  浏览器登录失败，回退 HTTP 登录: {e}")
            try:
                cookie, opener = auto_login_with_session(sso_user, sso_pass, entry_url)
            except Exception:
                if cookie_raw:
                    print("  账号密码登录失败，回退使用 CURRICULUM_COOKIE")
                    cookie = normalize_cookie(cookie_raw)
                else:
                    raise
    elif cookie_raw:
        print("未设置账号密码，使用 CURRICULUM_COOKIE")
        cookie = normalize_cookie(cookie_raw)
    else:
        raise RuntimeError(
            "缺少登录凭据：请设置 SSO_USERNAME/SSO_PASSWORD（推荐），或设置 CURRICULUM_COOKIE（备用）"
        )

    access_token = env("CURRICULUM_ACCESS_TOKEN", parsed.get("accessToken") or "", required=False).strip()
    if not access_token:
        raise RuntimeError("缺少 accessToken：请设置 CURRICULUM_ACCESS_TOKEN，或在 CURRICULUM_ENTRY_URL 中带上 accessToken 参数")

    user_id = env("CURRICULUM_USER_ID", parsed.get("id") or "", required=False).strip()
    if not user_id:
        raise RuntimeError("缺少 userId：请设置 CURRICULUM_USER_ID，或在 CURRICULUM_ENTRY_URL 中带上 id 参数")

    user_type = env("CURRICULUM_USER_TYPE", parsed.get("userType") or "0", required=False).strip() or "0"

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
        opener=opener,
    )


def request_json(
    origin: str,
    headers: Dict[str, str],
    method: str,
    path: str,
    data: Optional[dict] = None,
    opener: Optional[urllib.request.OpenerDirector] = None,
) -> dict:
    body = None
    req_headers = dict(headers)
    api_timeout = _env_int("CURRICULUM_API_TIMEOUT", 30)
    api_retries = _env_int("CURRICULUM_API_RETRIES", 2)
    if data is not None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        req_headers["Content-Type"] = "application/json;charset=UTF-8"

    req = urllib.request.Request(origin + path, data=body, headers=req_headers, method=method)
    try:
        resp_obj = _open_with_retry(
            req,
            opener=opener,
            timeout=api_timeout,
            retries=api_retries,
            label=path,
        )
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", "replace")
        raise ApiError(f"HTTP {e.code} path={path} body={err_body[:200]}") from e
    except Exception as e:
        raise ApiError(f"HTTP request failed path={path} error={e}") from e

    with resp_obj as resp:
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
        "Origin": origin,
        "Referer": config.entry_url,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }
    if config.cookie:
        headers["Cookie"] = config.cookie

    current = request_json(
        origin, headers, "GET", "/api/baseInfo/mobile/common/selectCurrentInfo", opener=config.opener
    ).get("data", {})
    current_week = int(current.get("currentWeek") or 1)
    semester = str(current.get("currentSemester") or "")

    week_data = request_json(
        origin, headers, "GET", "/api/baseInfo/mobile/common/queryCurrentSemesterWeekList", opener=config.opener
    ).get("data", [])
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
    schedule = request_json(
        origin,
        headers,
        "POST",
        "/api/arrange/mobile/courseSchedule/courseSchedule",
        payload,
        opener=config.opener,
    ).get("data", {})

    today_iso = None
    try:
        idx = request_json(
            origin,
            headers,
            "POST",
            "/api/arrange/mobile/courseSchedule/indexCourseSchedule",
            {
                "academicYearSemester": semester,
                "userId": config.user_id,
                "userType": config.user_type,
                "weeks": [current_week],
            },
            opener=config.opener,
        ).get("data", {})
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
