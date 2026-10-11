# 🎁 Community TG Monitor (多社区抽奖与热帖监控 Bot)

> 默认采用轻量 RSS 客户端的多社区（NodeSeek + 烧饼论坛 sb.sb）抽奖/福利/特价新帖监控与 Telegram 双向交互机器人；烧饼签到可选浏览器运行时。

---

## ✨ 核心特性

- 🌐 **双社区官方源支持**：
  - **NodeSeek** (`https://rss.nodeseek.com/`)：全站实时流监控。
  - **烧饼论坛** (`https://sb.sb/rss.xml`)：RSS 用于新帖观察和自定义关注词；自动抽奖/红包检测只采用论坛官方标记。
  - 首页每 30 秒快速发现，`/lottery/` 与 `/redpacket/` 每 5 分钟补漏；标记事件与 RSS 分开去重，避免“RSS 先出现、后加标记”导致漏报。
  - 0 风控、免登录、免 Cookie、无封号风险，毫秒级轻量 XML 流式解析。
- 🧠 **NodeSeek 双阶段 AI 语义判定**：
  - 不设关键词或板块前置门槛，每篇新帖先抽取活动状态、免费权益、受众和参与机制，再由确定性规则裁决；
  - 边界案例自动二次复审，API 失败保留队列并指数退避重试，不会误记为已处理；
  - 通过 Telegram `/ai` 配置 OpenAI-compatible API 地址、API Key、主模型、复审模型和阈值。
- 📊 **每日算法健康度与成功率日报**：
  - 自动统计每日扫描新帖数、规则命中数、Telegram 送达结果、噪音拦截数与 RSS 轮询成功率；
  - 每日北京时间 **22:00** 准时向 Telegram 推送运行日报，随时发送 `/report` 查阅实时看板。
- 🍪 **烧饼论坛自动签到与全量通知（可选）**：
  - 支持配置 `SBSB_COOKIE` 自动开启每日自动签到（北京时间每天 08:05 自动执行并回传连续天数、烧饼资产与等级）；
  - 使用当前官方 `/checkin/` 表单，提交后核验“今日已签到”才记录成功；统计缺失显示“暂未获取”。遇到人机验证时每天提醒一次，当天停止重复提交、继续查询；在浏览器完成签到后可自动确认。调度每 5 分钟检查，08:05 起执行。
  - 可选浏览器路径使用网站原本的 Cap 组件和提交事件，失败后不补发 HTTP 请求。只查询已签到时明确标为“查询确认”，不归因为本程序提交。
  - 支持在 Telegram 发送 `/signin` 随时一键签到与查分；
  - 实时轮询互动通知（`/u/{uid}/?tab=notifications`）与私信箱（`/messages/`），有人回复主题、点赞支持、@提及或发私信时秒级推送。
- 📱 **Telegram 原生交互式管理**：
  - 自动注册 Telegram 官方快捷指令菜单（输入 `/` 即可一键点选）；
  - 支持 **`/sources` 网站独立开关**，可一键暂停/开启特定网站推送；
  - 支持 **Inline Keyboard 按钮一键点选删除**，无需手动打字；
  - 支持 **对话引导式添加**，点击 `/add` 直接打字发送即可入库。
- 🐳 **多架构 Docker 镜像支持**：默认轻量镜像发布至 GitHub Container Registry（`linux/amd64` 与 `linux/arm64`）；浏览器候选另用 Playwright 镜像，体积与启动时内存占用更高。

---

## 📸 Telegram 交互指令

系统已精简收拢为 **4 大核心指令**，状态卡片内置 Inline Keyboard 快捷操作：

| 指令 | 说明 | 交互形式与特性 |
| :--- | :--- | :--- |
| `/status` | 📊 全局监控守护状态面板与控制台 | 查看运行时间、各源状态、AI 与 Cookie 状态，内置 `[ ⏸️ 暂停/恢复 ]` 与 `[ 📈 查看今日日报 ]` 按钮 |
| `/keywords` | 🎯 专属关注词雷达管理 | 带有 `[ ➕ 添加 ]` 与 `[ 🗑️ 点选删除 ]` 交互卡片。默认未配置不启用；一旦添加即享**最高优先级直通推送** |
| `/sources` | 📡 社区网站独立推送开关 | Inline Keyboard 独立点选切换 NodeSeek 或 烧饼论坛推送 |
| `/signin` | 🍪 烧饼论坛一键签到与查分 | 手动执行签到，返回连续天数、可用烧饼资产、成长值与官方账本签到时间 |

> 💡 辅助指令兼容：发送 `/report` 可直接查看算法过滤与成功率日报，发送 `/pause` / `/resume` 可快速启停推送，发送 `/test` 可查看格式演示卡片。

---

## 🛠️ 环境变量与凭证获取指南

在部署前，你需要准备以下变量并填入 `.env` 文件中：

| 环境变量 | 是否必填 | 默认值 | 说明与示例 |
| :--- | :---: | :---: | :--- |
| `TG_BOT_TOKEN` | **必填** | 无 | 你的 Telegram Bot API Token，用于发送消息和接收指令交互 |
| `TG_CHAT_ID` | **必填** | 无 | 你的 Telegram 个人纯数字 UID（仅允许管理员本人操作并接收私信推送） |
| `SBSB_COOKIE` | *可选* | 空 | 烧饼论坛登录 Cookie（填入后自动激活每日 08:05 签到、查分与私信/回帖通知） |
| `SBSB_BROWSER_SIGNIN` | *可选* | `0` | `1` 使用浏览器签到，须以 `Dockerfile.browser` 构建；正常执行站点验证，服务端拒绝后当天只查询 |
| `SBSB_BROWSER_MODE` | *可选* | `headless` | `desktop` 使用临时 Xvfb 中的完整 Chromium 与本地 CDP；browser Compose overlay 默认采用此模式 |
| `AI_API_KEY` | *可选* | 空 | OpenAI-compatible API Key（推荐直接在 .env 注入，免除在 TG 对话框输入密钥） |
| `AI_ENDPOINT` | *可选* | 空 | API 基础地址，如 `https://api.openai.com/v1` 或 `https://api.deepseek.com/v1` |
| `AI_MODEL` | *可选* | 空 | 主抽取与判定模型名称（如 `gpt-4o-mini`, `deepseek-chat`） |
| `AI_JUDGE_MODEL` | *可选* | 空 | 边界案例独立复审模型（留空默认跟随主模型） |
| `AI_ACCEPT_THRESHOLD` | *可选* | `0.90` | 自动通过置信度阈值（范围 0.70～0.99） |
| `AI_ENABLED` | *可选* | `true` | 是否启用 NodeSeek AI 语义判定（配置上述三项后默认激活） |
| `MONITOR_SOURCES` | *可选* | `nodeseek,sbsb` | 启用的公开源，可用逗号分隔过滤（支持 `nodeseek`, `sbsb`） |
| `DATA_DIR` | *可选* | `/app/data` | 数据持久化存储路径 |

---

## 🚀 极速部署

### 方式一：Docker Compose（推荐）

#### 1. 创建目录与配置文件
```bash
mkdir -p community-monitor && cd community-monitor

# 创建配置文件 .env
cat << 'ENV_EOF' > .env
TG_BOT_TOKEN=YOUR_TELEGRAM_BOT_TOKEN
TG_CHAT_ID=YOUR_TELEGRAM_CHAT_ID

# 可选：烧饼论坛 Cookie（配置后自动开启每日 08:05 签到与互动提醒）
SBSB_COOKIE="__Host-bbs_session=你的烧饼论坛Session值"
ENV_EOF

# 安全加固：收敛配置文件权限
chmod 600 .env

# 创建 docker-compose.yml
cat << 'COMPOSE_EOF' > docker-compose.yml
services:
  community-monitor:
    image: ghcr.io/j2st1n/community-tg-monitor:latest
    container_name: community-monitor
    restart: unless-stopped
    env_file:
      - .env
    volumes:
      - ./data:/app/data
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"
COMPOSE_EOF
```

#### 2. 一键启动
```bash
docker compose up -d
```

---

## ⚙️ 持久化与数据说明

浏览器候选使用官方 Playwright Python 1.63.0 镜像及同版本 SDK，支持 ARM64；可在源码目录通过 `docker compose -f docker-compose.yml -f docker-compose.browser.yml up -d --build` 启用。overlay 默认采用 `desktop`：启动临时 Xvfb 和完整 Chromium，经容器内部回环地址上的 CDP 连接，使用原生窗口尺寸和新建私密上下文，不注入浏览器属性覆盖或伪造验证 token。进程环境排除机器人密钥与 Cookie，不发布调试端口；完成或失败后关闭进程并删除临时资料。

浏览器仅在未签到且允许提交时启动；登录过期或站点策略变化仍可能需要处理。只通过组件和合成测试不代表站点接受，必须有真实表单提交和官方已签到确认；表单导航期间只重试已知执行上下文切换的读取，不追加 POST。

若唯一一次业务 POST 已得到允许的成功响应，但浏览器随后读取或交付页面时报错，主程序会再做一次官方 GET。只有该次查询也确认今日已签到才报提交成功；查询失败或仍未签时保持原失败结果，不补发业务或验证请求。明确验证拒绝、业务响应失败或重复提交不会进入补确认路径。

验证接口取自签到表单，允许论坛自身与其官方页面声明的 `https://capjs.net` 服务，仅放行该表单对应的 challenge/redeem 与必要预检。论坛会话仅绑定 `sb.sb`；访问外部验证服务时移除 Cookie 和 Authorization。异常只报告安全阶段与类型，不输出接口响应、会话或验证凭据。

原组件的客户端错误只保留白名单 code；事件 message、token 和未知值不会写入日志。服务端返回 `automated_browser_detected` / `instr_automated_browser` 时明确报告当前运行方式被拒绝，当天停止提交。该浏览器候选无法保证永久通过站方策略，失败时应检查安全阶段日志。

数据持久化保存在挂载的 `./data` 目录下：
* `settings.json`：存储你动态配置的关键词、网站开关和推送状态；
* `seen_ids.json`：存储已扫描的公开帖子 ID 历史，带各社区前缀，避免重启后重复推送；
* `sbsb_events.json`：分别存储烧饼论坛官方抽奖与红包标记事件，首次启用静默建立历史基线；
* `ai_secret.json`：单独保存 Telegram 配置的 AI API Key，程序自动设置为 `600` 权限且不会显示完整值；
* `nodeseek_ai_state.json`：保存待判定、失败重试队列及最近分类审计记录；
* `seen_msgs.json`：存储已扫描的烧饼论坛私信与互动通知唯一去重键；
* `daily_stats.json`：记录每日算法扫描、过滤拦截与命中统计；
* `checkin_state.json`：记录每日签到执行状态与日期。

---

## 📄 开源协议

本项目采用 [MIT License](LICENSE) 许可协议。
