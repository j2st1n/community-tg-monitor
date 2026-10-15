# TG 内容与签到反馈修复验收

本次实现用户确认的通知预览及签到补充反馈。预算、参与筛查、随机等待、既有账本与去重格式保持；人工明细入口只读。通知接口成功不等于TG收件端已读。

## 原审查问题对应实现

| 编号 | 修复 | 验证位置 |
| --- | --- | --- |
| 1 | 动态字段按数据转义，源站实体解码；发送和编辑使用受限、平衡的HTML | `test_telegram_ui.FormattingTest`、`PauseAndPrivateTest` |
| 2 | 从积分页当前等级节点提取等级，成长值独立；排除下级和等级阶梯 | `test_signin_state.SigninEvidenceTest` |
| 3 | 全局暂停包含活动、私信、互动和自动参与，继续去重，恢复不补发 | `test_telegram_ui.PauseAndPrivateTest`、既有参与控制测试 |
| 4 | Key保存与原消息删除分别核验；删除明确返回True才称已删除 | `InputAndButtonsTest`、`FormattingTest` |
| 5 | 关注词/人工列表分页、短ID按钮、全文入口；超长消息按UTF-16保守边界平衡分段 | `FormattingTest`、`InputAndButtonsTest` |
| 6 | 分开呈现推送、参与、AI配置启用和官方签到；Cookie不等于完成 | `StatisticsAndDetailTest`、`DurableSigninTest` |
| 7 | 发现活动与通知发送独立计数 | `StatisticsAndDetailTest` |
| 8 | 发送统计明确公开活动卡片范围，零记录不显示100% | `StatisticsAndDetailTest` |
| 9 | 当天截至当前时刻；跨日旧计数不冒充今日；历史待处理单独查看 | `StatisticsAndDetailTest`、`ParticipationControlsTest` |
| 10 | 摘要下沉明细，保留程序受理/账号查询归因，预留不写成实扣；明细不重试 | `StatisticsAndDetailTest`、既有账本与参与防重测试 |
| 11 | AI、日报、参与和待处理均有面板入口，帮助列出扩展指令 | `StatisticsAndDetailTest` |
| 12 | 校验失败保留输入，支持取消，旧按钮给反馈；开关按钮设置目标状态，重复点击不反转 | `InputAndButtonsTest` |
| 13 | API、Key、模型、复审、阈值和显式环境启用开关统一锁定 | `InputAndButtonsTest` |
| 14 | 关注词新增/重复/内置过滤分别反馈，无新增不报成功 | `InputAndButtonsTest` |
| 15 | 高频面板与保存回执精简，模型内部状态翻译，默认不显示私有端点或Key片段 | `InputAndButtonsTest`、`StatisticsAndDetailTest` |
| 16 | 私信称新对话，不显示无意义编号；帮助说明不保证已有对话新消息提醒 | `PauseAndPrivateTest`、`StatisticsAndDetailTest` |

## 签到补充验收

- 签到周期按UTC日期，即北京时间08:00开始；凌晨查询上一轮不会完成下一轮08:05任务。旧版只有日期的记录先GET核验升级。
- 分开处理已提交且官方确认、查询确认已签、查询确认未签、提交结果未确认、查询不可用和停止自动提交；不由组件存在或通用异常推断验证码拒绝。
- 同周期已确认成功不因后续查询异常而撤销；可选资产失败不撤销签到。缺失字段不补0，先前字段明确标记此前核验；下一周期不复用旧资产。
- 允许自动流程前落盘提交保护，手动与调度串行。异常中断后的未确认保护只允许查询恢复；确认成功才清除。
- 签到确认先持久化，通知接口结果另记。通知未确认不会触发再次签到；旧通知没有接口证据则标记未知，不冒充发送成功。
- 签到时间只取同一账单行的每日签到记录。导航、侧栏、页脚的同名链接不得复用其他积分记录时间；同周期时间冲突时不猜选一个。
- 实际文件与包入口均经过合成浏览器/调度测试；没有通过真实额外POST、验证码或TG测试来证明修复。

对应 `tests/test_signin.py`、`test_signin_state.py`、`test_browser_signin.py`、`test_script_entrypoint.py`。全量命令为 `python3 -m unittest discover -s tests`。GitHub镜像发布前以Python3.12、断网容器执行同一套测试。

## 生产升级边界

将完整app目录部署到现有挂载，保留环境、设置、签到、去重和参与账本；仅重启原容器，不以拉取轻量镜像替换已启用的桌面浏览器镜像。备份源码供回滚，回滚不恢复旧业务账本。上线验收分别检查源码摘要、容器运行、状态迁移与只读官方查询；次日自然调度仍由既有监控复核，不能把静态测试当作未来业务成功。
