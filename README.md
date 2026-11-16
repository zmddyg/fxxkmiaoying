# fxxkmiaoying

使用腾讯云开发 CloudBase 定时运行的秒应课堂签到工具。项目根据本地课表和活动配置生成签到计划，在签到窗口前准备图片，按计划提交并查询回执。云函数使用数据库记录阶段和提交状态，以便后续触发继续处理。

使用前需要自行准备秒应活动、有效登录会话、课表和表单字段。本仓库中的配置和课表均为虚构示例，实际生成的计划和登录数据保存在本地。当前课表匹配逻辑使用北京时间 07:50、10:00、13:50、15:45、18:20 五个固定签到窗口，活动窗口需与这些时间匹配。代码要求活动标题为“课堂签到”，其他标题需要修改 `秒应现场采集/attendance_schedule.py` 中的 `CLASS_ACTIVITY_TITLE`。

希望交给编程 Agent 协助部署时，可使用 [Agent 快速部署教程](AGENT_DEPLOY.md)。

## 环境

- Python 3 和 Node.js 20 或更高版本，以及 npm。
- 安装 Python 依赖：`python -m pip install -r 秒应现场采集/requirements.txt`。部分 Windows 依赖用于本地采集功能。
- 可运行普通云函数和文档型数据库的 CloudBase 环境。

## 准备数据

1. 将 `签到配置.example.json` 复制为 `签到配置.json`，填入活动 ID 或分享链接、活动表单字段和其他所需设置。示例中的字段、名单序号和地点只用于展示结构，按实际活动填写。
2. 将 `课表.example.json` 复制为 `课表.json`，填入实际课程与带时区的开始、结束时间。课表路径由配置中的 `timetable` 指定。
3. 在 `运行状态/session.json` 放入有效的秒应会话。文件包含 `version`（值为 1）、`account` 和 `token`。也可通过 `MIAOYING_STATE_DIR` 指定会话目录。会话文件包含明文凭据，请妥善保管。
4. 使用仓库的 `function/black.jpg` 作为图片。当前编译器要求文件为 285 字节，活动对照片的具体要求应自行核对。

在项目目录运行：

```sh
python 编译课表.py
python 打包.py
```

编译器会读取当前活动规则，生成 `function/plan.json` 及北京时间和 UTC 两种定时触发器配置。`python 编译课表.py --from YYYY-MM-DD` 可从指定日期开始生成计划。打包脚本安装并捆绑 CloudBase Node SDK，生成 `秒应-CloudBase云函数.zip`。

可运行 `node --test 签到流程.test.js` 检查使用虚构数据的签到流程。

## 部署

1. 在 CloudBase 文档型数据库创建集合 `miaoying_class_attendance_v1`，限制为管理端访问。
2. 将生成的 ZIP 上传为普通云函数，使用 Node.js 20 运行环境，入口为 `index.main`。参考配置为 256 MB 内存和 3 秒超时。
3. 配置环境变量 `MIAOYING_SESSION_TOKEN` 为秒应会话令牌。先设置 `MIAOYING_ENABLE_SUBMIT=0`，在控制台使用计划中已有的日期和时段运行只读检查：

```json
{"mode":"probe","day":"YYYY-MM-DD","slot":1}
```

4. 按云环境实际触发时区导入对应的定时触发器文件，核对测试日志和活动规则。准备启用提交时，将 `MIAOYING_ENABLE_SUBMIT` 设为 `1`。将它设回 `0` 可暂停提交。

定时调用需要云平台的触发器来源。云端日志中的 `submitted` 表示读到了匹配且审核通过的记录。签到接口、网络、活动规则、令牌有效期和云资源状态都可能变化，需要通过实际回执和日志核对运行结果。令牌过期时需重新取得有效会话并更新环境变量。课表或活动规则变化后应重新编译和部署。

## 数据与许可证

`签到配置.json`、课表、会话、生成的计划、触发器文件和部署包可能包含私人信息，已列入 `.gitignore`，请在分享文件前自行核查。提交凭据前先确认目标位置。

本项目自有代码采用 [GNU GPL-3.0-only](LICENSE)。第三方依赖各自适用其许可证。打包脚本将依赖中可识别的许可文件汇总到部署包的 `THIRD_PARTY_LICENSES.txt`。部分依赖未在安装包内提供独立许可文件，分发部署包前需要进一步核对其许可信息。
