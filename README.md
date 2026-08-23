# 🎁 NodeSeek TG Monitor (NodeSeek 抽奖与热帖监控 Bot)

> 纯轻量、零外部依赖、官方 RSS 驱动的 NodeSeek 抽奖/福利/特价新帖实时监控与 Telegram 双向交互推送机器人。

---

## ✨ 核心特性

- 🎯 **官方数据源保障**：通过官方 `https://rss.nodeseek.com/` 实时抓取，0 风控、免登录、免 Cookie、无封号风险。
- 📱 **Telegram 原生交互式管理**：
  - 自动注册 Telegram 官方快捷指令菜单（输入 `/` 即可一键点选）；
  - 支持 **Inline Keyboard 按钮一键点选删除**，无需手动打字；
  - 支持 **对话引导式添加**，点击 `/add` 直接打字发送即可入库。
- 🏷️ **优雅分类胶囊流**：关键词分类聚合展示（🎲 抽奖活动 / 🧧 福利赠送 / 🏷️ 自定义关注词），告别传统死板的纵向列表。
- 🔗 **清晰直达链接**：推送卡片直接展示完整明文 HTTP/HTTPS 链接，点击直达论坛回帖参与。
- 🚫 **智能降噪过滤**：内置负向屏蔽机制，自动过滤买卖求购帖（如 `【慢收】`、`求购`）。
- 🐳 **Docker 极简开箱即用**：基于 `python:3.12-alpine` 构建，镜像体积小于 40MB，内存占用仅 20MB。

---

## 📸 Telegram 交互指令

| 指令 | 说明 | 交互形式 |
| :--- | :--- | :--- |
| `/status` | 📊 查看运行状态、运行时间与去重扫描统计 | 文本卡片 |
| `/keywords` | 🎯 查看当前生效词库（分类胶囊展示） | 带有 `[ ➕ 添加 ]` 与 `[ 🗑️ 按钮删除 ]` 的交互卡片 |
| `/blocks` | 🚫 查看当前生效的屏蔽词列表 | 带有 `[ ➕ 添加 ]` 与 `[ 🗑️ 按钮解除 ]` 的交互卡片 |
| `/pause` | ⏸️ 暂停推送通知（后台继续记录去重索引，免打扰） | 文本确认 |
| `/resume` | ▶️ 恢复推送通知 | 文本确认 |
| `/test` | 🧪 手动向自己触发一条格式演示卡片 | 完整卡片 |
| `/help` | 📖 显示使用说明书 | 菜单列表 |

---

## 🚀 快速开始 (Docker Compose)

### 1. 克隆仓库与配置
```bash
git clone https://github.com/j2st1n/nodeseek-tg-monitor.git
cd nodeseek-tg-monitor

# 复制环境变量模板
cp .env.example .env
```

### 2. 配置 Telegram 凭据
编辑 `.env` 文件：
```ini
TG_BOT_TOKEN=你的_TELEGRAM_BOT_TOKEN
TG_CHAT_ID=你的_TELEGRAM_CHAT_ID
```
> 💡 **获取方式**：
> * `TG_BOT_TOKEN`：在 Telegram 联系 [@BotFather](https://t.me/BotFather) 发送 `/newbot` 获取。
> * `TG_CHAT_ID`：在 Telegram 联系 [@userinfobot](https://t.me/userinfobot) 获取你的数字 ID。

### 3. 一键启动
```bash
docker compose up -d --build
```

### 4. 查看运行日志
```bash
docker compose logs -f
```

---

## 📦 本地免 Docker 运行

如果你不想使用 Docker，也可以直接在宿主机运行：

```bash
# 设置环境变量并后台启动
export TG_BOT_TOKEN="你的_BOT_TOKEN"
export TG_CHAT_ID="你的_CHAT_ID"
export DATA_DIR="./data"

python3 app/main.py
```

---

## ⚙️ 持久化与数据说明

数据持久化保存在挂载的 `./data` 目录下：
* `settings.json`：存储你动态配置的关键词、屏蔽词列表和推送开关（通过 Telegram 修改后自动保存）；
* `seen_ids.json`：存储已扫描的帖子 ID 历史，避免重启后重复推送。

---

## 📄 开源协议

本项目采用 [MIT License](LICENSE) 许可协议。
