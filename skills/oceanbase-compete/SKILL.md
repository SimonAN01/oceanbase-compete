---
name: oceanbase-compete
description: 通过 HTTP 操作 OceanBase 2026 大赛的 PowerContext、seekdb 代码、基础实践和性能优化赛题，执行提测、按回执查分、榜单抓取与比较、有限监测或明确版本的批量队列。需要用户提供登录状态；适配范围和真实验证边界见技能参考。
---

# OceanBase 竞赛提测与榜单

使用 `python <skill>/scripts/obcomp.py`。工具只依赖 Python 3.10+ 标准库；代码版本核验另需 Git，以及使用者自己配置的私有仓库访问权限。

## 登录和目标

用户提供 Cookie 文件时使用 `--cookie-file <绝对路径>`，也可读取 `OBCOMP_COOKIE_FILE`、进程内的 `OBCOMP_COOKIE` 或 `--cookie-stdin`。复用当前会话已提供且仍有效的登录状态。不要搜索浏览器凭证、把 Cookie 放入命令参数值、代码或 Git。登录过期时停止需要登录的操作。

先执行 `auth check`。HTTP 200 和公开比赛列表可访问不能证明登录成功。`profile list` 查看已有目标；没有配置时用 `profile create <名称> --track <赛题>` 建立经过账号和报名仓库验证的配置。内置 track 为 `powercontext`、`seekdb-code`、`seekdb-practice`、`seekdb-performance`，具体说明见 [协议与适配范围](references/protocol.md)。多赛题或多版本不明确时只询问缺少的信息，不猜测目标。

## 提测和精确查分

用户要求提交即授权对指定版本提交，无需再次确认。代码题必须提供已推送的完整 40 位 commit 和分支，或使用配置内明确保存的版本。工具校验远端分支；旧提交需要 `--repo-path` 证明其仍在远端分支历史中。不要把未推送修改当作待测版本，也不要自动推送或公开参赛仓库。

```text
python <skill>/scripts/obcomp.py preflight --profile <名称> --branch <分支> --commit <完整SHA>
python <skill>/scripts/obcomp.py submit --profile <名称> --branch <分支> --commit <完整SHA> --wait --snapshot
python <skill>/scripts/obcomp.py score --receipt <回执绝对路径> --wait
```

默认跳过同版本重复写入；仅当用户明确要求重新评测相同版本时使用 `--resubmit`。提交前回执会落盘。写入超时、无明确业务结果、服务端错误或任务关联不唯一时，保留回执并用 `reconcile --receipt <路径>` 核实，不重新发送提交请求。不确定关联时可由用户指定 `--task-id`，工具仍验证题目、仓库和版本。

用户只要求查询时，使用已有回执；也可 `adopt --profile <名称> --task-id <ID>` 导入当前账号可见的旧任务，无需提交。状态以服务端为准，失败、取消和等待状态不输出旧数值成绩；成功的 `0.000` 是有效成绩。未经验证的错误编号只报告原始编号，不编造解释。

## 榜单、比较和自动化

`leaderboard --profile <名称>` 自动保存完整快照；用 `--format csv --output <路径>` 导出。`diff --before <快照> --after <快照>` 比较同一目标；`compare --before <回执> --after <回执>` 查询并比较版本结果。`experiments --profile <名称> --refresh` 刷新本地实验记录。仅在分数来源一致时比较数值，不手动换算平台内部整数。

`watch --profile <名称> --interval 60 --timeout 1800 --max-cycles 30` 在当前进程中监测榜单；使用 `--receipt <路径>` 则等待单个任务。进度写标准错误，主结果写标准输出；没有变化时保持安静，有限运行后结束，不创建后台定时任务。

队列使用 `queue run <JSON计划> --max-submissions <最大次数>`，计划包含 `profile` 和明确的 `jobs` 数组，代码版本必须固定。默认失败即停；只有用户要求后才使用 `--continue-on-failure`。再次运行同一计划会从检查点继续。遇到未知提交结果先核对回执，禁止循环重新提交。

seekdb 基础实践提交 `--extra deploymentUuid=...`、`--extra forkUrl=...` 或 `--extra prUrl=...`；不发送分支和 commit。需要核验 Fork/PR 材料时可执行 `practice verification --profile <名称>` 获取本队验证行。获取验证行不会自动修改 README、PR 或创建 PR。

## 报告

报告目标、完整版本、任务状态、分数或失败原因、回执和榜单快照路径。区分官方排名 `rank` 和列表位置 `position`。不把自动化测试或跳过重复写入描述成真实提测成功。运行退出码：0 完成；1 输入/登录/协议错误；2 评测失败；3 仍在等待或队列达到次数上限；4 提交结果未确认；130 用户中断。回执、快照和检查点保存在工具的用户状态目录，留在本地。
