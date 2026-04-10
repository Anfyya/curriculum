# 课程表自动同步（GitHub Actions）

## 1. 作用

- 每 30 分钟自动抓取课程系统接口。
- 生成并更新仓库根目录 `curriculum.ics`。
- 苹果日历订阅这个文件后，会自动刷新课程变动。

## 2. 你需要配置的 GitHub Secrets

进入仓库 `Settings -> Secrets and variables -> Actions`，新增：

- `CURRICULUM_ENTRY_URL`：你课程页面完整链接（含 `id`、`accessToken` 等参数）
- `CURRICULUM_COOKIE`：浏览器请求课表接口时的 `Cookie` 请求头值
- 可选：`CURRICULUM_ACCESS_TOKEN`（不填则从 `CURRICULUM_ENTRY_URL` 解析）
- 可选：`CURRICULUM_USER_ID`（不填则从 `CURRICULUM_ENTRY_URL` 解析）
- 可选：`CURRICULUM_USER_TYPE`（不填则从 `CURRICULUM_ENTRY_URL` 解析，默认 `0`）

## 3. 如何拿到 `CURRICULUM_COOKIE`

1. 打开课程表页面并保持已登录。
2. 按 `F12` 打开开发者工具，切到 `Network`。
3. 刷新页面，点开请求：
   - `/api/arrange/mobile/courseSchedule/courseSchedule`
4. 在 `Request Headers` 里复制 `Cookie` 的完整值。
5. 粘贴到 GitHub Secret `CURRICULUM_COOKIE`。

## 4. 苹果日历订阅链接

- HTTPS：
  - `https://raw.githubusercontent.com/Anfyya/curriculum/main/curriculum.ics`
- 或 webcal：
  - `webcal://raw.githubusercontent.com/Anfyya/curriculum/main/curriculum.ics`

## 5. 注意事项

- `CURRICULUM_COOKIE` 可能会过期，过期后工作流会失败，需要重新复制一次。
- 如果课程节次时间与学校实际不同，修改 `period_times.json` 即可。
- 工作流文件：`.github/workflows/sync-curriculum.yml`
- 主脚本：`sync_curriculum_ics.py`
