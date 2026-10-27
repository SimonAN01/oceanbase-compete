# OceanBase Compete

面向 OceanBase 2026 数据库大赛的非官方 Python CLI 和可安装 Skill：提交固定版本、按任务回执查分、抓取全部可访问榜单、保存变化、比较实验，并执行有限、可恢复的提测队列。

**Python 3.10+，运行时无第三方 Python 依赖。** 代码提交的远端版本检查另需 Git。认证由使用者提供，参赛仓库保持原有访问权限。

```text
固定代码版本 → 检查报名仓库与提交要求 → 保存回执 → 提测一次
                                                   ↓
实验对比 ← 榜单快照及变化 ← 按同一任务 ID 等待并查分
```

## 功能

| 功能 | 命令 |
|---|---|
| 登录检查与环境诊断 | `auth check`、`doctor` |
| 比赛、赛题和实时提交要求 | `camps`、`questions`、`options` |
| 账号与报名仓库绑定的赛题配置 | `profile create/list/show` |
| 完整版本、远端可达性、提交窗口和材料检查 | `preflight`、`submit --dry-run` |
| 单次提交、重复保护、写入前回执 | `submit` |
| 旧任务导入、精确查分、有限等待 | `adopt`、`score --wait` |
| 超时和不确定结果的核实 | `reconcile` |
| 榜单全量分页、JSON/CSV 导出、自动快照 | `leaderboard` |
| 排名/列表位置、分数和队伍变化 | `diff` |
| 历史实验、最好成功结果、固定版本对比 | `experiments`、`compare` |
| 当前进程内监测榜单或任务，只输出变化 | `watch` |
| 次数上限、失败停止、中断恢复的批量队列 | `queue run/status` |
| seekdb 基础实践材料及本队验证行 | `submit --extra`、`practice verification` |

内置 `powercontext`、`seekdb-code`、`seekdb-practice`、`seekdb-performance` 四个目标。配置和能力会在运行时重新向平台核验，详见 [协议与适配范围](skills/oceanbase-compete/references/protocol.md)。

## 安装

作为普通 CLI：

```bash
git clone https://github.com/SimonAN01/oceanbase-compete.git
cd oceanbase-compete
python -m pip install .
obcomp --help
```

作为自带代码的 Skill，不需要安装 Python 包：

```bash
python install.py
# 后续更新
python install.py --update
```

默认安装到 `$CODEX_HOME/skills/oceanbase-compete`；没有设置时使用 `~/.codex/skills/oceanbase-compete`。`--skills-dir <目录>` 可改变位置。安装器先在临时目录验证入口再替换目标；不会打包仓库之外的 Cookie 或本地运行状态。

在源码目录也可直接运行：

```bash
python skills/oceanbase-compete/scripts/obcomp.py --help
```

## 提供登录状态

登录 OceanBase 后，将对应 API 请求中的完整 Cookie 头保存为本地文件。支持原始 Cookie 头、Netscape jar 和浏览器导出的 Cookie JSON 数组。文件只在本地读取，不复制到配置或回执。

```bash
obcomp auth check --cookie-file /path/to/local-cookie.txt
```

也可给当前进程/终端设置 `OBCOMP_COOKIE_FILE`，或用 `--cookie-stdin` 从标准输入读取。`OBCOMP_COOKIE` 适合进程内安全注入。不要把真实 Cookie 作为命令参数值、写进源码、Git、示例或公开日志。工具不自动登录、不查找浏览器凭证、不自动刷新会话。过期后由使用者重新提供。

后续示例假设当前进程已通过 `OBCOMP_COOKIE_FILE` 指定本地文件；也可在每条命令中追加 `--cookie-file`。公开榜单本身不依赖个人身份，读取已有本地 profile 抓榜可不提供 Cookie。

## 配置目标与提交固定版本

```bash
obcomp camps
obcomp questions --camp-id 21
obcomp profile create powercontext-2026 --track powercontext
obcomp profile create seekdb-2026 --track seekdb-code
obcomp profile list
```

profile 绑定当前账号的指纹、camp、question 和平台登记的团队仓库。可用 `--repository` 核对预期仓库；不一致会停止。不会把一个题目的仓库静默替换成另一个题目的仓库。profile 已存在时不会覆盖，可另建配置。

代码题必须指定完整 40 位 commit SHA 和分支。下例中的 SHA 是虚构占位值，运行前替换成自己的已推送版本：

```bash
obcomp preflight --profile powercontext-2026 \
  --branch feature/example --commit aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa

obcomp submit --profile powercontext-2026 \
  --branch feature/example --commit aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
  --dry-run

obcomp submit --profile powercontext-2026 \
  --branch feature/example --commit aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
  --wait --snapshot --interval 15 --timeout 600
```

也可在创建 profile 时保存明确的 `--branch` 和 `--commit`，以后直接使用 `submit --profile ... --wait --snapshot`。本地未推送修改不会被自动提交。Git 通过现有的凭证管理器访问私有仓库，OceanBase Cookie 不会发送到 GitHub。

默认要求 commit 等于远端指定分支的 tip。若提交该分支历史中的旧 commit，另加 `--repo-path /path/to/checkout`；本地 origin 必须匹配报名仓库，并能证明 commit 是远端 tip 的祖先。远端验证失败会在发出提测请求前停止。

同一题目、仓库、分支、版本和材料重复执行时默认复用回执或已有任务。确实需要重新评测相同版本时显式指定 `--resubmit`。`--snapshot` 必须搭配 `--wait`，仅在该任务终止后捕获当时的榜单，不代表这次失败的提交产生了新分数。

榜单抓取遇到变动或请求错误时，组合命令仍输出已完成任务的结果和回执，并将 `leaderboard.complete` 标为 `false`。可单独重跑只读的 `leaderboard` 命令；不需要重新提交。任务失败时退出码为 2，任务成功但榜单抓取失败时为 1。

## 回执、查分与恢复

```bash
obcomp score --receipt /path/to/receipt.json --wait
# 等待超时后使用同一回执恢复；终止时抓取并关联榜单，不重新提测
obcomp score --receipt /path/to/receipt.json --wait --snapshot
obcomp reconcile --receipt /path/to/receipt.json
obcomp reconcile --receipt /path/to/receipt.json --task-id 123456

# 导入平台已有任务，无需重新提测
obcomp adopt --profile powercontext-2026 --task-id 123456
```

回执记录固定 payload、账号指纹、提交前任务集合、服务端任务 ID、代码核验结果和状态。提交请求发出前即原子保存。写入超时、模糊业务结果、5xx、异常重定向或非 JSON 响应只保留不确定回执，**不重试提测**。此时先核对记录；没有唯一关联就停止。显式指定任务 ID 后仍校验题目、仓库和版本。

任务状态为 `QUEUED`、`RUNNING`、`SUCCEEDED`、`FAILED`、`CANCELLED`。只有成功状态返回数值成绩；`0.000` 是有效成功成绩。失败时输出平台错误消息或原始失败编号。查不到错误解释时不编造原因，也不使用历史最好成绩代替本次成绩。

任务优先读取 `strMachineScore`，榜单优先读取 `webTotalScore`，同时保留来源与小数精度。原始内部数值不会自行换算。不同分数来源不直接求差或选最好结果。

## 榜单、快照与版本对比

```bash
obcomp leaderboard --profile powercontext-2026
obcomp leaderboard --profile seekdb-2026 --format csv --output leaderboard.csv
obcomp leaderboard --profile seekdb-2026 --format json --output leaderboard.json
obcomp diff --before /path/to/old-snapshot.json --after /path/to/new-snapshot.json

obcomp experiments --profile powercontext-2026 --refresh
obcomp experiments --profile powercontext-2026 --format csv --output experiments.csv
obcomp compare --before /path/to/old-receipt.json --after /path/to/new-receipt.json
```

每次成功抓榜都会保存快照，记录抓取起止时间、题目范围、页数、完整性和原始顺序。当前接口的 total/pageNum 不可靠，工具按 pageNo 翻页，读到不足一页或空页后检查首末页未变化；重复 ID、变化、业务错误或页数上限会报错，不输出“完整”快照。`--page-size`、`--max-pages` 可设请求边界。

complete 的含义是读取了该赛题端点开放的数据；`atomic=false` 明确表示抓取不是数据库原子快照。接口未给官方名次时 `rank=null`，只给列表位置 `position`，不假定并列规则。CSV 会转义可能被电子表格当作公式的队伍名称，JSON 保留原文本。

本地实验记录只代表已保存/刷新的任务结果，`scores_are_cached=true` 明确标记这一点。`--direction max|min` 在创建 profile 时决定如何选择同来源的最好成功分数。

## 自动监测

```bash
obcomp watch --profile powercontext-2026 --interval 60 --timeout 1800 --max-cycles 30
obcomp watch --receipt /path/to/receipt.json --interval 15 --timeout 600
```

在当前进程中运行，到时间、次数上限或任务完成/失败后结束。开始时输出初始状态；以后只有状态/榜单变化才输出进度，不因抓取时间更新而重复通知。进度写标准错误，最终结果写标准输出。中断后快照和回执保留。本命令不创建系统后台服务或托管定时任务，也不向外部聊天、邮箱发送消息。

## 批量队列

创建计划文件；下面全是虚构版本，使用前替换：

```json
{
  "profile": "powercontext-2026",
  "jobs": [
    {"branch": "feature/example", "commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
    {"branch": "feature/another", "commit": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}
  ]
}
```

```bash
obcomp queue run plan.json --max-submissions 2 --interval 60 --timeout 600
obcomp queue status /path/to/queue-checkpoint.json
# 中断后执行完全相同的计划会继续已有检查点
obcomp queue run plan.json --max-submissions 2 --interval 60 --timeout 600
```

先检查整个计划的版本格式、材料字段和重复项。逐项提测和等待，默认失败即停；`--continue-on-failure` 可继续后续明确的任务，最终仍以失败退出码报告失败项。未知提交结果始终停止，不受此选项影响。次数上限包含不确定写入尝试，复用已有任务不算新写入；断在请求已受理但尚未写回队列结果时，也通过提交回执恢复。再次运行同一计划可提高明确的次数上限。

平台未返回的每日限额或间隔不会猜测；preflight 展示实际返回值，平台限制仍由服务端执行。队列的次数上限和间隔是额外的本地约束，不会绕过平台限流。

## seekdb 基础实践

```bash
obcomp profile create seekdb-practice-2026 --track seekdb-practice
obcomp practice verification --profile seekdb-practice-2026
obcomp submit --profile seekdb-practice-2026 \
  --extra deploymentUuid=aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee --wait
```

也可按本次材料提供 `--extra forkUrl=https://github.com/example/seekdb` 或 `--extra prUrl=https://github.com/oceanbase/seekdb/pull/123`，或者 `--extra-file materials.json`。实践题不发送 branch/commit。工具校验格式，平台核验材料真实性与归属；不会自动 Fork、提交官方 PR、修改 README 或公开报名仓库。

## 本地状态与退出码

默认状态目录为 Windows `%LOCALAPPDATA%/oceanbase-compete`，其他系统 `~/.local/state/oceanbase-compete`，可用 `OBCOMP_STATE_DIR` 或 `--state-dir` 修改。配置、回执、快照和检查点均保存在这里。状态文件原子替换，POSIX 新建文件为 0600；提交使用进程锁。输入 Cookie 不会复制进这些文件。代码仓库只包含实现、说明及虚构测试数据。

| 退出码 | 含义 |
|---:|---|
| 0 | 命令完成、提交确认或复用已有提交 |
| 1 | 参数、认证、网络或协议错误 |
| 2 | 评测失败/取消，或队列包含失败项 |
| 3 | 评分仍在等待、等待超时或队列达到次数上限 |
| 4 | 提交结果或任务关联尚未确认 |
| 130 | 用户中断，回执和检查点保留 |

HTTP 查询仅有限重试；遇 429/临时错误退避。提交写入不重试。凭证只发送到工具允许的 OceanBase HTTPS API 主机，拒绝重定向转发。`--http-timeout` 和 `--retries` 可调读取边界。

## 测试与验证范围

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
python skills/oceanbase-compete/scripts/obcomp.py --version
```

测试使用虚构数据和模拟 HTTP/服务，覆盖身份识别、请求载荷、失败和未知状态、分页、成绩关联、重复保护、超时核实、队列中断恢复、导出、安装及凭证脱敏。GitHub Actions 在 Linux、macOS、Windows 和多个 Python 版本运行同一套检查。

实时验证与模拟测试的区别见 [验证记录](docs/VALIDATION.md)。前端协议变化后需要重新核对，不能仅以测试通过推断任意赛题兼容。

## 许可证与参考

[MIT](LICENSE)。独立实现，参考 [SimonAN01/aicomp-results](https://github.com/SimonAN01/aicomp-results) 的 Skill、CLI 与回执设计。
