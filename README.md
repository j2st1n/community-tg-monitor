# 🎁 Multi-Community TG Monitor (NodeSeek & 烧饼论坛 抽奖与热帖监控 Bot)

> 纯轻量、零外部依赖、官方原生 RSS 驱动的多社区（NodeSeek + 烧饼论坛 sb.sb）抽奖/福利/特价新帖实时监控、烧饼论坛自动签到与 Telegram 双向交互推送机器人。

---

## ✨ 核心特性

- 🌐 **双社区官方源支持**：
  - **NodeSeek** (`https://rss.nodeseek.com/`)：全站实时流监控。
  - **烧饼论坛** (`https://sb.sb/rss.xml`)：原生支持全站流与抽奖专区（`/lottery/`）。
  - 0 风控、免登录、免 Cookie、无封号风险，毫秒级轻量 XML 流式解析。
- 🍪 **烧饼论坛自动签到与全量通知（可选）**：
  - 支持配置 `SBSB_COOKIE` 自动开启每日自动签到（北京时间每天 08:05 自动执行并回传连续天数、烧饼资产与等级）；
  - 支持在 Telegram 发送 `/signin` 随时一键签到与查分；
  - 实时轮询互动通知（`/u/{uid}/?tab=notifications`）与私信箱（`/messages/`），有人回复主题、点赞支持、@提及或发私信时秒级推送。
- 📱 **Telegram 原生交互式管理**：
  - 自动注册 Telegram 官方快捷指令菜单（输入 `/` 即可一键点选）；
  - 支持 **Inline Keyboard 按钮一键点选删除**，无需手动打字；
  - 支持 **对话引导式添加**，点击 `/add` 直接打字发送即可入库。
- 🏷️ **优雅分类胶囊流**：关键词分类聚合展示（🎲 抽奖活动 / 🧧 福利赠送 / 🏷️ 自定义关注词），告别传统死板的纵向列表。
- 🔗 **清晰直达链接**：推送卡片明确区分社区来源（`🌐 [NodeSeek]` / `🍪 [烧饼论坛]`），展示完整明文 HTTP/HTTPS 链接与具体楼层定位，点击直达论坛参与。
- 🚫 **智能降噪过滤**：内置负向屏蔽机制，自动过滤买卖求购帖（如 `【慢收】`、`求购`）。
- 🐳 **多架构 Docker 镜像支持**：自动构建发布至 GitHub Container Registry（`linux/amd64` 与 `linux/arm64`），体积仅 30MB，常驻内存仅约 15~20MB。

---

## 📸 Telegram 交互指令

| 指令 | 说明 | 交互形式 |
| :--- | :--- | :--- |
| `/status` | 📊 查看运行状态、启用的社区源、运行时间与去重扫描统计 | 文本卡片 |
| `/signin` | 🍪 烧饼论坛一键签到并返回连续天数、可用烧饼与等级 | 完整签到卡片 |
| `/keywords` | 🎯 查看当前生效词库（分类胶囊展示） | 带有 `[ ➕ 添加 ]` 与 `[ 🗑️ 按钮删除 ]` 的交互卡片 |
| `/blocks` | 🚫 查看当前生效的屏蔽词列表 | 带有 `[ ➕ 添加 ]` 与 `[ 🗑️ 按钮解除 ]` 的交互卡片 |
| `/pause` | ⏸️ 暂停推送通知（后台继续记录去重索引，免打扰） | 文本确认 |
| `/resume` | ▶️ 恢复推送通知 | 文本确认 |
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
| `MONITOR_SOURCES` | *可选* | `nodeseek,sbsb` | 启用的公开源，可用逗号分隔过滤（支持 `nodeseek`, `sbsb`） |
| `DATA_DIR` | *可选* | `/app/data` | 数据持久化存储路径 |

### 1. 获取 `TG_BOT_TOKEN`
1. 在 Telegram 中搜索官方机器人 [@BotFather](https://t.me/BotFather) 并点击开始；
2. 发送 `/newbot`，按提示先输入你的 Bot 昵称（如 `MyCommunityBot`），再输入用户名（必须以 `bot` 结尾，如 `my_community_notify_bot`）；
3. 创建成功后，BotFather 会返回一串 Token，形如：`YOUR_TELEGRAM_BOT_TOKEN`。

### 2. 获取 `TG_CHAT_ID`
1. 在 Telegram 中搜索机器人 [@userinfobot](https://t.me/userinfobot) 并点击开始；
2. 它会立即回复你的个人信息，复制其中的 `Id` 纯数字（例如 `5020626401`）；
3. ⚠️ **重要**：创建好你自己的 Bot 后，**请先在 Telegram 里搜索并私聊你自己的 Bot，点击一次底部的 `Start`**（如果不先点击 Start，Bot 无法主动向陌生私聊发消息）。

### 3. 获取 `SBSB_COOKIE`（可选，用于自动签到和私信）
1. 电脑浏览器打开并登录 `https://sb.sb/`；
2. 按 `F12` 打开开发者工具 ➡️ 切换到 **应用 (Application)** 标签页（Firefox 为 **存储 (Storage)**）；
3. 展开左侧 **Cookie** ➡️ 点击 `https://sb.sb`；
4. 找到 **`__Host-bbs_session`**，双击复制它的 **值 (Value)**；
5. 拼接格式填入：`SBSB_COOKIE="__Host-bbs_session=你复制的一长串值"`。

---

## 🚀 极速部署

### 方式一：Docker Compose（推荐）

#### 1. 创建目录与配置文件
```bash
mkdir -p nodeseek-monitor && cd nodeseek-monitor

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

### 方式二：单行 Docker Run 极速运行

```bash
docker run -d \
  --name nodeseek-monitor \
  --restart unless-stopped \
  -e TG_BOT_TOKEN="你的_BOT_TOKEN" \
  -e TG_CHAT_ID="你的_CHAT_ID" \
  -e SBSB_COOKIE="__Host-bbs_session=你的值" \
  -v $(pwd)/data:/app/data \
  ghcr.io/j2st1n/nodeseek-tg-monitor:latest
```

---

## ⚙️ 持久化与数据说明

数据持久化保存在挂载的 `./data` 目录下：
* `settings.json`：存储你动态配置的关键词、屏蔽词列表和推送开关（通过 Telegram 修改后自动保存）；
* `seen_ids.json`：存储已扫描的公开帖子 ID 历史，带各社区前缀，避免重启后重复推送；
* `seen_msgs.json`：存储已扫描的烧饼论坛私信与互动通知唯一去重键；
* `checkin_state.json`：记录每日签到执行状态与日期。

---

## 📄 开源协议

本项目采用 [MIT License](LICENSE) 许可协议。
