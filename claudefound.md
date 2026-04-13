# Claude 调查发现汇总

## 一、项目概述

`sync_curriculum_ics.py` 自动从江西工程学院课程系统拉取课程表数据，生成 `.ics` 日历文件，通过 GitHub Actions 定期同步并推送到仓库，供 webcal 订阅使用。

---

## 二、已解决的问题

### 1. Sangfor aTrust 零信任网关会话激活

**问题**: CAS SSO 登录成功后，门户会话处于 `secondary_auth` 状态，无法访问后端课程系统 API。

**根因**: 缺少从 `secondary_auth` 到 `online` 的会话激活流程。这是 Sangfor aTrust 零信任网关的安全机制，需要在浏览器端完成以下四步 API 调用：

1. `GET /passport/v1/public/authConfig?mod=1` — 获取 CSRF token
2. `POST /controller/v1/public/reportEnv` — 上报浏览器设备环境
3. `GET /passport/v1/auth/authCheck` — 获取 `sidTicket`
4. `POST /passport/v1/public/sessionIdExchange` — 用 sidTicket 交换，激活会话为 online 状态

**关键发现**: `x-sdp-traceid` 请求头**绝对不能**在 `secondary_auth` 状态下发送，否则网关直接返回 401。这是最难排查的坑。

**修复**: 在 `auto_login()` 函数中，CAS 回调之后、访问课程入口 URL 之前，增加了上述四步会话激活代码（约 80 行）。

### 2. accessToken 前缀问题

**问题**: CI 上 API 调用返回 403 "token解析错误"。

**根因**: 用户将 GitHub Secret `CURRICULUM_ACCESS_TOKEN` 的值设为 `accessToken=f146971...`（带了参数名前缀），而代码直接使用这个值作为 token。

**修复**: 在 `build_config()` 中增加前缀剥离：
```python
if access_token.startswith("accessToken="):
    access_token = access_token[len("accessToken="):]
```

### 3. 新文件无法 commit

**问题**: 首次生成 `curriculum.ics` 时，GitHub Actions 的 commit 步骤不会提交新文件。

**根因**: 原工作流先执行 `git diff --quiet`，但 `git diff` 只检测已跟踪文件的变化，新增的未跟踪文件被静默忽略。

**修复**: 改为先 `git add curriculum.ics`，再用 `git diff --cached --quiet` 检查：
```yaml
git add curriculum.ics
if git diff --cached --quiet -- curriculum.ics; then
  echo "curriculum.ics 无变化"
  exit 0
fi
```

### 4. SSL 证书验证

**问题**: GitHub Actions 环境中，urllib 请求课程系统时 SSL 验证失败。

**修复**: 在 `request_json()` 中使用 `_build_ssl_context()` 创建自定义 SSL 上下文（禁用严格验证）。

### 5. Python 3.9 类型语法兼容

**问题**: Ubuntu 上 Python 3.9 不支持 `dict | None` 等 PEP 604 联合类型语法。

**修复**: 改用函数参数默认值代替类型注解，如 `def _portal_api(method, path, params=None, body=None, csrf=""):`。

---


## 四、关键技术细节

### 三个域名角色

| 域名 | 用途 |
|------|------|
| `sso.jxec.edu.cn:10445` | 联奕 CAS SSO 统一认证 |
| `0xr.jxec.edu.cn:10443` | Sangfor aTrust 零信任门户 |
| `zichan.jxec.edu.cn:14340` | 课程系统后端 API |

### 登录流程

```
SSO CAS 登录 (验证码OCR + RSA加密)
    → CAS ticket
    → 门户 CAS 回调 (获取 secondary_auth 状态 cookies)
    → 门户会话激活 4 步 API (secondary_auth → online)
    → 访问课程入口 URL (aTrust 重定向获取 sdp_app_session)
    → 课程系统 API 调用 (带 accessToken + Cookie)
```

### DAY_OFFSET_MAP 含义

系统 `dayOfWeek` 值与实际星期的映射：
- `2` → 星期一 (offset=0)
- `3` → 星期二 (offset=1)
- `4` → 星期三 (offset=2)
- `5` → 星期四 (offset=3)
- `6` → 星期五 (offset=4)
- `7` → 星期六 (offset=5)
- `1` → 星期日 (offset=6)

### 逐周拉取策略

代码当前采用**逐周拉取**策略（`sync_curriculum_ics.py:704`），每次 POST 请求只传一个周号 `weeks: [week_code]`，避免一次请求整学期导致后端把不同周次课程错误合并。每条返回数据打上 `_week_code` 标记，后续优先使用这个单周号而非接口返回的聚合周次字符串。

---

## 五、待清理的临时文件

以下测试脚本位于 `C:/Users/yjlnq/`，调试完成后可删除：

- `test_session_activate.py` / `test_session_v2.py` / `test_session_v3.py` — 门户会话激活测试
- `test_minimal.py` / `test_headers.py` — 排查 401 原因的最小化测试
- `test_auto_login.py` / `test_api_call.py` / `test_post_api.py` — API 调用测试
- `test_schedule_data.py` / `test_schedule_data2.py` — 课程数据拉取测试
- `fetch_sfsdp.py` — 获取门户前端 JS (shortcut_api.js) 的脚本
- `shortcut_api.js` — 门户前端 JS 源码（约 100KB）
