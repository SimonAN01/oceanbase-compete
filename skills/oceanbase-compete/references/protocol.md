# 协议与适配范围

此工具独立实现，参考 [aicomp-results](https://github.com/SimonAN01/aicomp-results) 的 CLI 与 Skill 组织方式。不是 OceanBase 官方工具。

## 2026-10-27 核验的目标

| track | campId | questionId | 注册仓库槽 | 提交类型 | 可显示最高分配置 |
|---|---:|---:|---|---|---:|
| powercontext | 21 | 600070 | gitUrl | CODE | 240 |
| seekdb-code | 21 | 600069 | gitUrlPlus | CODE | 160 |
| seekdb-practice | 21 | 600071 | gitUrlPlus | ARTIFACT | 30 |
| seekdb-performance | 21 | 600080 | gitUrlPlus | CODE | 50 |

这些值是当日公开/已登录接口的观察结果，运行时始终重新读取 submissionOptions，核对题目、camp、类型、所需字段及 available。未知 verifier 或字段拒绝提交。显式 camp/question 配置仍须被实时接口确认；本版不声称已适配其他赛事或未来决赛。

## 使用的服务

身份：`GET https://webapi.oceanbase.com/api/user`，必须 `success=true` 且存在 uid。UID 只在内存中用于生成账号指纹。

以下接口前缀为 `https://openwebapi.oceanbase.com/api/techcamp/testplatform/`：

| 接口 | 方法 | 用途 |
|---|---|---|
| camp/queryOpenTestPlatformCampList | GET | camp 列表 |
| question/webQueryTestPlatformQuestionList | GET | campId、pageNo、pageSize 查询赛题 |
| taskrecord/submissionOptions | GET | questionId 获取实时提交模式 |
| taskrecord/backFillCommitDetail | POST | questionId 回填报名仓库及版本，前端以此读取注册仓库 |
| taskrecord/commitTestPlatformTaskRecord | POST | 唯一提测写入端点 |
| taskrecord/queryTestPlatformTaskRecordList | GET | questionId、pageNo、pageSize、sortType=1 查询当前账号可见的任务 |
| taskrecord/queryTaskRecordDetail | GET | taskId 获取该任务的测试详情 |
| score/queryTestPlatformScoreList | GET | questionId、pageNo、pageSize 查询赛题榜单 |
| taskrecord/seekdb-practice/verification | POST | 获取本队固定实践验证行 |

CODE 载荷：`questionId`、报名仓库 `gitRepo`、`branch`、完整 `commitId`，可附 `subQuestionName`。ARTIFACT 不发送 branch/commit，材料放 `extraParams`。本版不提供平台未要求的 ZIP 上传。

实践材料包含 deploymentUuid、官方 seekdb PR 的 prUrl、公开 Fork 的 forkUrl。工具校验字段格式；是否确为官方 Fork、材料是否已被别队认领、部署 UUID 是否上报，由平台评测确认。工具不会伪造或代填这些信息。

## 必须保留的协议差异

- 任务状态 `0/1/2/3/4` 分别为等待、运行、成功、失败、取消。只有成功才返回数值成绩。未知状态报协议错误。
- 身份接口与大赛接口使用不同的成功包裹。大赛接口必须检查业务 code；HTTP 200 本身不足。
- 空任务列表实际可能返回 `pageNum=0,pageSize=0,total=0,list=null`，这是可接受的空结果。
- 赛题、任务按 pageNo 分页并检查稳定总数。榜单接口观察到 **pageNo=2 仍报告 pageNum=1，total 仅为该页条数**；榜单继续到不足请求页大小的末页或空页、检查 ID 不重复，再重读首末页检测变化。越过末页可能返回 code=508，不能将任意业务错误当作空结果。达到上限、重复页、错误页均不宣称完整。
- 榜单 complete 表示读取到此端点开放的数据末尾，不保证平台没有额外未公开数据；读取并非数据库原子快照，输出明确 `atomic=false`。
- 榜单优先使用 `webTotalScore`，任务优先使用 `strMachineScore`。原始 totalScore/machineScore 可能使用内部计量单位，不自行除以 1000；返回来源，并禁止不同来源的数值直接比较。
- 榜单未返回官方排名时，`rank=null`，仅给出原始页面顺序的 `position`，不推断并列名次。
- dailyCommitNumber、commitTimeInterval 等 null 值代表未知，不解释为无限。未知计量单位不自行换算；队列使用用户给定的次数上限和间隔，服务端限制为最终依据。

协议来源为 OceanBase 官网前端及当日接口响应：[训练营页面](https://open.oceanbase.com/train)、[当前前端入口](https://render-intl.alipayobjects.com/p/yuyan/180020010001011015/umi.596905d7.js)、[提测表单实现](https://render-intl.alipayobjects.com/p/yuyan/180020010001011015/3940.a3de09c7.async.js)、[任务记录界面](https://render-intl.alipayobjects.com/p/yuyan/180020010001011015/p__Train__TestRecord.1a4cf7bf.async.js)。这些资源会随官网更新，不是稳定 API 承诺。
