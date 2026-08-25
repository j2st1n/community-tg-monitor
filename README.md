# 🎁 Multi-Community TG Monitor (NodeSeek & 烧饼论坛 抽奖与热帖监控 Bot)

> 纯轻量、零外部依赖、官方原生 RSS 驱动的多社区（NodeSeek + 烧饼论坛 sb.sb）抽奖/福利/特价新帖实时监控与 Telegram 双向交互推送机器人。

---

## ✨ 核心特性

- 🌐 **双社区官方源支持**：
  - **NodeSeek** (`https://rss.nodeseek.com/`)：全站实时流监控。
  - **烧饼论坛** (`https://sb.sb/rss.xml`)：原生支持全站流与抽奖专区（`/lottery/`）。
  - 0 风控、免登录、免 Cookie、无封号风险，毫秒级轻量 XML 流式解析。
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
| `/keywords` | 🎯 查看当前生效词库（分类胶囊展示） | 带有 `[ ➕ 添加 ]` 与 `[ 🗑️ 按钮删除 ]` 的交互卡片 |
| `/blocks` | 🚫 查看当前生效的屏蔽词列表 | 带有 `[ ➕ 添加 ]` 与 `[ 🗑️ 按钮解除 ]` 的交互卡片 |
| `/pause` | ⏸️ 暂停推送通知（后台继续记录去重索引，免打扰） | 文本确认 |
| `/resume` | ▶️ 恢复推送通知 | 文本确认 |
| `/test` | 🧪 手动向自己触发双社区格式演示卡片 | 完整卡片 |
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

# 可选：指定启用的监控源（默认 nodeseek,sbsb 全部启用）
# MONITOR_SOURCES=nodeseek,sbsb
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

### 方式二：单行 Docker Run 极速运行

```bash
docker run -d \
  --name nodeseek-monitor \
  --restart unless-stopped \
  -e TG_BOT_TOKEN="你的_BOT_TOKEN" \
  -e TG_CHAT_ID="你的_CHAT_ID" \
  -v $(pwd)/data:/app/data \
  ghcr.io/j2st1n/nodeseek-tg-monitor:latest
```

---

### 方式三：本地免 Docker 原生运行

```bash
git clone https://github.com/j2st1n/nodeseek-tg-monitor.git
cd nodeseek-tg-monitor

export TG_BOT_TOKEN="你的_BOT_TOKEN"
export TG_CHAT_ID="你的_CHAT_ID"
export DATA_DIR="./data"

python3 app/main.py
```

---

## ⚙️ 持久化与数据说明

数据持久化保存在挂载的 `./data` 目录下：
* `settings.json`：存储你动态配置的关键词、屏蔽词列表和推送开关（通过 Telegram 修改后自动保存）；
* `seen_ids.json`：存储已扫描的帖子 ID 历史，带各社区前缀，避免重启后重复推送。

---

## 📄 开源协议

本项目采用 [MIT License](LICENSE) 许可协议。
