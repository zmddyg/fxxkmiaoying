# 用 GitHub Actions 运行课堂签到

这个方案由 GitHub Actions 运行签到脚本，并使用 CloudBase 文档型数据库保存照片准备、提交和回执状态。运行需要有效的秒应会话、由本人课表编译的计划、GitHub 仓库和能够访问该数据库的腾讯云凭证。

项目目前按北京时间 07:50、10:00、13:50、15:45、18:20 五个窗口匹配课表。活动需要与代码中的标题和时段匹配。程序使用仓库中的黑点 JPEG，照片和地点要求需要按照实际活动核对。

## 让 Agent 协助部署

在自己的仓库中打开项目后，可以将下面的任务交给有文件和浏览器操作能力的 Agent：

```text
请阅读 README.md 和 GITHUB_ACTIONS.md，协助我为自己的秒应课堂签到配置 GitHub Actions。

使用我的活动、课表、表单与地点信息准备本地配置。需要登录时启动扫码程序，由我用自己的手机确认。编译计划并核对课程与签到窗口。

在我指定的 CloudBase 环境中准备管理端专用的 miaoying_class_attendance_v1 集合，配置 GitHub Actions Secrets。凭据与计划只用于我指定的仓库和云环境，不输出到聊天、日志或公开文件。

先使用我自己的仿真活动检查数据库访问、活动规则及回执。在我指定的课表时段配置定时，并检查下一次计划运行时间和运行记录。报告定时延迟、失败状态及是否读到实际回执。不要把 Actions 运行成功或接口应答当成已审核通过的签到回执。
```

## 准备计划和数据库

1. 将本项目复制或 fork 到自己有写入权限的 GitHub 仓库。安装 Python 依赖并使用本人账号扫码登录，具体见 [Agent 快速部署教程](AGENT_DEPLOY.md)的准备步骤。
2. 按实际活动填写 `签到配置.json` 和 `课表.json`，运行 `python 编译课表.py`。检查生成的 `function/plan.json` 覆盖的课程日期和时段。活动、课表或表单规则变化后重新编译。
3. 在 CloudBase 文档型数据库创建集合 `miaoying_class_attendance_v1`，设置为管理端读写。用于 Actions 的腾讯云凭证需要能够对该集合读取、插入和按条件更新。按实际腾讯云 CAM 和环境配置授予所需权限，并验证访问。

计划文件包含个人活动、身份和课表信息，不要提交到公开仓库。默认路径已列入 `.gitignore`。

## 配置 GitHub Secrets

在自己的仓库进入 **Settings → Secrets and variables → Actions**，添加下面的仓库 Secrets。

| 名称 | 内容 |
| --- | --- |
| `MIAOYING_PLAN` | 本人 `function/plan.json` 文件的 Base64 编码 |
| `MIAOYING_SESSION_TOKEN` | 本人秒应会话文件中的 `token` |
| `TCB_ENV_ID` | CloudBase 环境 ID |
| `TENCENT_SECRET_ID` | 有权访问目标数据库的腾讯云凭证 SecretId |
| `TENCENT_SECRET_KEY` | 对应的 SecretKey |

Base64 只是编码。计划和令牌应放入 Secrets，不要放进工作流文件或日志。GitHub 的单个 Secret 最大为 48 KB，编码后的计划超出限制时需要另行安排私密存储。 [GitHub Secrets 文档](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets)

已安装并登录 GitHub CLI 时，可在自己仓库的项目目录用下列命令直接从文件设置前两项，命令不会把内容写入仓库：

```sh
python -c "import base64, pathlib, sys; sys.stdout.write(base64.b64encode(pathlib.Path('function/plan.json').read_bytes()).decode())" | gh secret set MIAOYING_PLAN
python -c "import json, pathlib, sys; sys.stdout.write(json.loads(pathlib.Path('运行状态/session.json').read_text(encoding='utf-8'))['token'])" | gh secret set MIAOYING_SESSION_TOKEN
```

其他三个值可以在 GitHub 的 Secrets 页面输入。会话失效时，重新运行 `python 秒应会话工具/miaoying_session.py recover`，再更新 `MIAOYING_SESSION_TOKEN`。项目不保证一次扫码永久有效。

仓库变量 `MIAOYING_ENABLE_SUBMIT` 未设置或值不是 `1` 时，运行只读检查，结果 `probe_ok` 和 `database: reachable` 表示本次活动及数据库访问检查通过。在同一页面的 **Variables** 标签中添加该变量并设为 `1` 后启用提交。启用前请确认活动、课表和本人希望使用的签到方式。

## 启动和查看结果

仓库中的 `.github/workflows/actions-checkin.yml` 默认提供手动触发。配置好 Secrets 后，在 GitHub **Actions** 页面选择对应工作流，使用 **Run workflow**。工作流中提供了注释的定时配置，需要自动运行时按个人需求启用并提交。

GitHub 定时默认使用 UTC，工作流中的时区及运行时间需要与课表核对。最短定时间隔为五分钟，GitHub 可能延迟或丢弃定时任务。脚本会寻找当前开放或未来十分钟内开放的计划时段，也会回查十五分钟内刚结束的时段。延迟过大时仍可能错过签到窗口。公开仓库长期无活动时定时工作流可能被停用。 [GitHub 定时触发文档](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)

每次运行使用 CloudBase 数据库的持久状态，提交结果不确定时由后续运行查询回执。`submission_acknowledged_waiting_for_receipt` 表示接口已应答，`submitted` 表示读取到匹配且审核通过的回执。请在真实或自有仿真活动中检查结果；网络耗时和审核状态会变化，一次成功不保证后续运行都成功。

暂停自动执行可在 GitHub Actions 页面禁用工作流，或将变量 `MIAOYING_ENABLE_SUBMIT` 改为其他值。课表或活动变更后重新编译并更新 `MIAOYING_PLAN`。在 GitHub 的 **Actions** 页面查看运行状态和日志，出现授权失效或活动规则变化时按实际情况更新会话或计划。

已有正式签到服务时，应核对各运行入口使用的数据库和状态，检查回执后再调整安排。定时执行和 CloudBase 数据库访问的完整线上组合需要在目标环境验证。
