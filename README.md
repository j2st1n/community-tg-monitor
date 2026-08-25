# 🎁 Multi-Community TG Monitor (NodeSeek & 烧饼论坛 抽奖与热帖监控 Bot)

> 纯轻量、零外部依赖、官方原生 RSS 驱动的多社区（NodeSeek + 烧饼论坛 sb.sb）抽奖/福利/特价新帖实时监控、烧饼论坛自动签到与 Telegram 双向交互推送机器人。

---

## ✨ 核心特性

- 🌐 **双社区官方源支持**：
  - **NodeSeek** (`https://rss.nodeseek.com/`)：全站实时流监控。
  - **烧饼论坛** (`https://sb.sb/rss.xml`)：原生支持全站流与抽奖专区（`/lottery/`）。
  - 0 风控、免登录、免 Cookie、无封号风险，毫秒级轻量 XML 流式解析。
- 🍪 **烧饼论坛自动签到与私信提醒（可选）**：
  - 支持配置 `SBSB_COOKIE` 自动开启每日自动签到（北京时间每天 08:05 自动执行并回传积分/天数卡片）；
  - 支持在 Telegram 发送 `/signin` 随时一键签到与查分；
  - 实时轮询私信箱（`/messages/`），有新私信或互动秒级推送。
- 📱 **Telegram 原生交互式管理**：
  - 自动注册 Telegram 官方快捷指令菜单（输入 `/` 即可一键点选）；
  - 支持 **Inline Keyboard 按钮一键点选删除**，无需手动打字；
  - 支持 **对话引导式添加**，点击 `/add` 直接打字发送即可入库。
- 🏷️ **优雅分类胶囊流**：关键词分类聚合展示（🎲 抽奖活动 / 🧧 福利赠送 / 🏷️ 自定义关注词），告别传统死板的纵向列表。
- 🔗 **清晰直达链接**：推送卡片明确区分社区来源（`🌐 [NodeSeek]` / `🍪 [烧饼论坛]`），展示完整明文 HTTP/HTTPS 链接，点击直达论坛回帖参与。
- 🚫 **智能降噪过滤**：内置负向屏蔽机制，自动过滤买卖求购帖（如 `【慢收】`、`求购`）。
- 🐳 **多架构 Docker 镜像支持**：自动构建发布至 GitHub Container Registry（`linux/amd64` 与 `linux/arm64`），体积仅 30MB，常驻内存仅约 15~20MB。

---

## 📸 Telegram 交互指令

| 指令 | 说明 | 交互形式 |
| :--- | :--- | :--- |
| `/status` | 📊 查看运行状态、启用的社区源、运行时间与去重扫描统计 | 文本卡片 |
| `/signin` | 🍪 烧饼论坛一键签到并返回连续天数与烧饼资产 | 完整签到卡片 |
| `/keywords` | 🎯 查看当前生效词库（分类胶囊展示） | 带有 `[ ➕ 添加 ]` 与 `[ 🗑️ 按钮删除 ]` 的交互卡片 |
| `/blocks` | 🚫 查看当前生效的屏蔽词列表 | 带有 `[ ➕ 添加 ]` 与 `[ 🗑️ 按钮解除 ]` 的交互卡片 |
| `/pause` | ⏸️ 暂停推送通知（后台继续记录去重索引，免打扰） | 文本确认 |
| `/resume` | ▶️ 恢复推送通知 | 文本确认 |
| `/test` | 🧪 手动向自己触发双社区格式与签到演示卡片 | 完整卡片 |
| `/help` | 📖 显示使用说明书 | 菜单列表 |

---

## 🚀 极速部署

### 方式一：Docker Compose（推荐）

#### 1. 创建目录与配置文件
```bash
mkdir -p nodeseek-monitor && cd nodeseek-monitor

# 创建配置文件 .env
cat << 'ENV_EOF' > .env
TG_BOT_TOKEN=你的_TELEGRAM_BOT_TOKEN
TG_CHAT_ID=你的_TELEGRAM_CHAT_ID

# 可选：烧饼论坛 Cookie（配置后自动开启每日 08:05 签到与私信通知）
# SBSB_COOKIE="__Host-bbs_csrf=...; bbs_session=..."
ENV_EOF

# 创建 docker-compose.yml
cat << 'COMPOSE_EOF' > docker-compose.yml
services:
  nodeseek-monitor:
    image: ghcr.io/j2st1n/nodeseek-tg-monitor:latest
    container_name: nodeseek-monitor
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
* `settings.json`：存储你动态配置的关键词、屏蔽词列表和推送开关（通过 Telegram 修改后自动保存）；
* `seen_ids.json`：存储已扫描的公开帖子 ID 历史，带各社区前缀，避免重启后重复推送；
* `seen_msgs.json`：存储已扫描的烧饼私信 ID；
* `checkin_state.json`：记录每日签到执行状态与日期。

---

## 📄 开源协议

本项目采用 [MIT License](LICENSE) 许可协议。
