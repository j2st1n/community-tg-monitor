# 🎁 Community TG Monitor (多社区抽奖与热帖监控 Bot)

> 纯轻量、零外部依赖、官方原生 RSS 驱动的多社区（NodeSeek + 烧饼论坛 sb.sb）抽奖/福利/特价新帖实时监控、烧饼论坛自动签到与 Telegram 双向交互推送机器人。

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
  - 支持在 Telegram 发送 `/signin` 随时一键签到与查分；
  - 实时轮询互动通知（`/u/{uid}/?tab=notifications`）与私信箱（`/messages/`），有人回复主题、点赞支持、@提及或发私信时秒级推送。
- 📱 **Telegram 原生交互式管理**：
  - 自动注册 Telegram 官方快捷指令菜单（输入 `/` 即可一键点选）；
  - 支持 **`/sources` 网站独立开关**，可一键暂停/开启特定网站推送；
  - 支持 **Inline Keyboard 按钮一键点选删除**，无需手动打字；
  - 支持 **对话引导式添加**，点击 `/add` 直接打字发送即可入库。
- 🐳 **多架构 Docker 镜像支持**：自动构建发布至 GitHub Container Registry（`linux/amd64` 与 `linux/arm64`），体积仅 30MB，常驻内存仅约 15~20MB。

---

## 📸 Telegram 交互指令

| 指令 | 说明 | 交互形式 |
| :--- | :--- | :--- |
| `/status` | 📊 查看运行状态、启用的社区源、运行时间与去重扫描统计 | 文本卡片 |
| `/report` | 📈 查看今日算法过滤日报、扫描总数与成功率看板 | 完整数据日报 |
| `/sources` | 📡 各网站推送独立开关控制台（Inline Keyboard 点选切换） | 交互卡片 |
| `/ai` | 🧠 配置并测试 NodeSeek AI API、密钥、模型与判定阈值 | 交互卡片 |
| `/signin` | 🍪 烧饼论坛一键签到并返回连续天数、可用烧饼与等级 | 完整签到卡片 |
| `/keywords` | 🎯 查看并管理自定义专属关注词库 | 带有 `[ ➕ 添加 ]` 与 `[ 🗑️ 按钮删除 ]` 的交互卡片 |
| `/blocks` | 🚫 查看当前生效的屏蔽词列表 | 带有 `[ ➕ 添加 ]` 与 `[ 🗑️ 按钮解除 ]` 的交互卡片 |
| `/pause` | ⏸️ 全局暂停推送通知（后台继续记录去重索引，免打扰） | 文本确认 |
| `/resume` | ▶️ 全局恢复推送通知 | 文本确认 |
| `/test` | 🧪 手动向自己触发双社区格式与签到演示卡片 | 完整卡片 |
| `/help` | 📖 显示使用说明书 | 菜单列表 |

---

## 🛠️ 环境变量与凭证获取指南

在部署前，你需要准备以下变量并填入 `.env` 文件中：

| 环境变量 | 是否必填 | 默认值 | 说明与示例 |
| :--- | :---: | :---: | :--- |
| `TG_BOT_TOKEN` | **必填** | 无 | 你的 Telegram Bot API Token，用于发送消息和接收指令交互 |
| `TG_CHAT_ID` | **必填** | 无 | 你的 Telegram 个人纯数字 UID（仅允许管理员本人操作并接收私信推送） |
| `SBSB_COOKIE` | *可选* | 空 | 烧饼论坛登录 Cookie（填入后自动激活每日 08:05 签到、查分与私信/回帖通知） |
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

数据持久化保存在挂载的 `./data` 目录下：
* `settings.json`：存储你动态配置的关键词、屏蔽词列表、网站开关和推送状态；
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
