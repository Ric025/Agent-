# Windows 本机办公 Agent 技术架构

工程评审稿 · 2026-11-19

**框架决策：采用 Python LangGraph（StateGraph）作为唯一的 Agent 编排框架。** Windows 每位用户有一个本机 Python 协调进程；C#/.NET 负责桌面界面与原生工具执行。首版由供应商 SDK 或 HTTP 适配器调用模型，省去 LangChain 的高层 `create_agent` 循环。LangGraph 可以独立编排自定义节点，LangChain 的模型或检索组件以后按需引入。这是分层的 Python + .NET 架构，任务控制流始终归一张 LangGraph 主图。[LangGraph 官方定位](https://docs.langchain.com/oss/python/langgraph/overview) · [LangChain 的 create_agent](https://docs.langchain.com/oss/python/langchain/overview)

**每台员工 Windows 电脑运行一个按用户隔离的 Agent Runtime，可以接 OpenAI、Anthropic 等外部模型 API，也可以调用两台 128 GB GX10 上的内网模型。** 本机持有聊天、资料、任务检查点、工具权限与回执。工作区策略决定本轮哪些文字、截图可以发给哪个端点。外部 API 是正式推理选项；受限工作区只准用内网模型。掉线后员工用 `/resume` 找回任务，输入“继续”后由本机核实现场、接着执行。

Claude Desktop / Cowork 提供了三个值得复现的行为：给出一个目标后连续工作、工作中允许人纠正、最后交出可打开和修改的成品。Anthropic 也区分连接器、浏览器和原生桌面操作。这些行为决定下文的执行循环、状态模型和工具边界。对接公司 GX10 采用自有客户端及推理协议；Anthropic 的第三方网关文档面向 Claude 模型及 Messages API。[Cowork 工作流](https://support.claude.com/en/articles/13345190-get-started-with-claude-cowork) · [电脑操作机制](https://support.claude.com/en/articles/14128542-let-claude-use-your-computer-in-cowork) · [Claude 官方网关协议](https://claude.com/docs/third-party/claude-desktop/gateway)

## 1. 部署边界与控制权

```text
每台员工 Windows 电脑 · 每个登录用户一套
  桌面客户端 ──提交目标、改指令、批准、暂停──► Agent Runtime
           ◄──任务事件、待确认动作、成品版本──┤
                                             ├── SQLite：任务、事件、聊天、批准、记忆
                                             ├── 本机工作区：原件引用、索引、暂存和成品
                                             ├── 出站策略 + 模型适配器
                                             │    ├─ 许可的外部 API：OpenAI Responses / Anthropic Messages
                                             │    └─ 内网 API：GX10 A 或 B
                                             └── 授权及工具执行器
                                                   ├─ 文档与业务 API 工具
                                                   ├─ 独立的受限计算进程
                                                   └─ 浏览器 / Windows 交互会话
```

| 部件 | 职责 | 谁拥有最终决定 |
|---|---|---|
| 桌面客户端 | 一段对话可以发起任务；展示当前步骤、依据、成品和变更；接收中途纠正 | 员工决定目标、授权范围与正式提交 |
| 本机 Runtime | 每个任务一个可恢复的执行循环；保存事实、选择上下文、校验候选动作、调度及验收 | Runtime 决定任务状态，执行器在实际动作前再次检查权限 |
| 模型适配器 | 先按工作区、内容类型和能力选端点，再发送有限上下文，接收文本及候选动作 | 模型端不能调用本机驱动；Windows 凭据和提交权由本机持有 |
| 本机工具执行器 | 分配文件或窗口访问权，执行工具、产生回执并提供独立回读能力 | 文件系统和业务系统是操作结果的权威来源 |

首版随桌面安装包分发 **Python LangGraph 协调进程、C#/.NET 桌面端与工具进程、本机 SQLite**。UI 发送任务命令并订阅事件；Python 协调器通过本机 IPC 或 MCP 调用受管工具，C# 执行器负责在真正提交前复核授权。Python 依赖与 .NET 组件随安装包锁定版本，员工无需配置开发环境。桌面交互在当前用户会话运行。界面与协调器用限制到当前用户的本机 IPC 连接，子工具优先走继承的标准输入输出；若采用命名管道，配置用户 ACL、校验调用方并给本次会话发临时凭据。每个用户只允许一个活动协调器写任务账本。首期不引入中央编排服务、分布式消息队列或专职多 Agent 集群。

## 2. 核心是一个可恢复的执行循环

主图节点为 **组装上下文 → 请求模型 → 校验候选动作 → 按需等待批准 → 执行工具 → 回读验证**。验证节点按结果分支到下一轮、完成或待核实。图固定的是执行规则，模型每轮选择的业务工具和步骤可变。批准和实际写入使用不同节点。

例如员工说“核对这个目录里的交付资料，做一份差异表，把异常整理成工单草稿”。这项任务不需要预先画出每个 Excel 操作。Runtime 保存目标、选定目录、预期输出与结束条件。模型根据已知证据给出短计划，每轮只提交当前可执行的动作。

1. **读取任务快照。** 获取目标、当前修订号、已完成动作、已有文件、待处理问题和剩余预算。
2. **构造本轮上下文。** 加入许可范围内的必要片段、工具定义及最近回执；大文件留在本机，用带来源的引用代替全文。
3. **请求模型。** 模型可建议调用工具、向人提问、修改后续计划或提交候选成品。任务计划用于向人解释和分解工作，本身不赋予权限。
4. **决定与执行。** Runtime 校验工具名、完整参数、当前修订、权限、前置条件及预算。可并行做独立的只读工作；对同一成品的写入与前台桌面操作分别串行。需批准的动作在本机暂停。
5. **记录观察，决定下一步。** 工具返回结构化事实，如“工作表有 218 行，3 个订单号重复”，或“工单已保存，编号为……”。结果写入账本，下一轮根据这些事实继续。
6. **验证或交接。** 差异表须能打开，行数和金额须通过程序校验；工单须取得可读回的业务编号。达到用户要求后才显示完成。缺少证据、连续失败或预算耗尽时，保存现场，向员工提出具体问题。

**一个协调器可以完成多步工作。** 第一版用一个模型角色负责计划和下一步选择，程序负责授权、执行及可确定的验证。确有需要时，可以并行派发“只读分析甲表”和“只读分析乙表”这样的子任务；子任务返回证据，所有写入仍经过同一提交路径。后台显示的是工具事件与检查结果。

下面是一段可直接做成自动化回放的例子。员工的纠正发生在模型已经提出提交、工具尚未开始时。

```text
修订 1  员工    “核对交付表，有异常就建工单。”
        Runtime 读取本机列结构和业务键，附上工具清单，调用 GX10。
        模型    提议比对两份工作簿。执行器返回 3 条差异及行号。
        模型    提议生成差异表并建工单。执行器发布 XLSX v1，建单动作等待批准。
修订 2  员工    “先别建单，把这三条按客户分组。”
        Runtime 作废修订 1 的待批建单动作，保留差异记录和 XLSX v1。
        工具    生成 XLSX v2。验证器重新比较三条记录、金额及分组汇总。
        完成    返回可打开的 v2、引用行号和“未建工单”的已核实状态。
```

## 3. 状态机与数据契约

| 状态 | 进入原因 | 后续处理 |
|---|---|---|
| 待执行／运行中 | 用户下达目标或恢复任务 | 分配推理请求或调用已授权工具 |
| 等待输入／等待批准 | 缺少业务字段、出现多个可能对象或即将产生正式变更 | 保存检查点，展示具体问题或字段差异，释放模型推理名额 |
| 已暂停 | 用户暂停、PC 睡眠、桌面锁定或任务允许使用的模型端点均不可用 | 记录原因，故障恢复走 `/resume` 与“继续”，重新核对用户、版本和目标窗口 |
| 待核实 | 目标系统可能已提交，但回执丢失 | 先查业务键或产物指纹；无法确定时交给员工处理 |
| 已完成／已取消／失败 | 验收通过、员工终止或不可恢复错误 | 展示已有产物、已经生效的动作及原因 |

任务、模型推理尝试和工具动作各有独立标识。下面是建议实现的内部消息字段，适用于任意工具协议。

```text
Task          task_id, user_id, workspace_id, goal, revision, status, done_when
ModelAttempt  attempt_id, task_id, revision, provider, model_version, context_manifest, status
ActionIntent  action_id, task_id, revision, tool, sealed_args_ref, args_digest, target_ref, expected_version, business_key, dispatch_state
Approval      action_id, revision, args_digest, target_ref, expected_version, expires_at
ToolResult    action_id, status, business_key, observed_version, receipt_ref, error_code
Artifact      artifact_id, task_id, version, parent_version, local_path, content_hash, source_refs
TaskEvent     task_id, sequence, revision, event_type, redacted_payload, recorded_at
```

本机有两类持久记录：LangGraph 检查点保存图状态、待运行节点及中断；业务账本保存批准、动作意图和已核实回执。协调器只在授权用户的本机工作区内解析任务 ID。首版一项任务对应一个稳定的 `thread_id`，同一任务内的多个模型回合共用它。新动作在登记节点分配稳定业务操作键，等该节点的检查点同步落盘后才进入执行节点。提交器以操作键唯一约束，通过一次账本事务登记或领取动作。框架写检查点与账本写回执可先后完成，重启时按稳定操作键对账，已执行节点即使被重放也沿用实际回执。试点选用本地 SQLite checkpointer。官方将其列为本地开发用后端，批量发布前需在目标 Windows 终端验证并发、锁等待及异常关机恢复。[LangGraph 持久化](https://docs.langchain.com/oss/python/langgraph/persistence)

UI 订阅带顺序号的事件，应用重开后从上次顺序号补读。原始资料、文件版本和大对象独立落盘；文件生成先写暂存区、校验并记录哈希，再发布到目标目录。SQLite 位于本地磁盘，备份使用一致性快照，覆盖运行中的 WAL。工作区删除要连同索引、摘要、暂存及保留期内的副本一起处理。[SQLite WAL 机制](https://www.sqlite.org/wal.html)

派发前写入受当前用户保护的不可变参数或其本地引用、参数摘要、执行阶段和预生成的业务键。重试相同动作复用这组参数和键。只有摘要无法重建真实调用；密钥与口令则由执行器在调用时从 Windows 凭据存储取得。对于不接受业务键的目标系统，在意图中记录可供独立查询的业务字段，并标注本次写入是否能自动核实。

**中途改要求是一次有序的状态变化。** 员工说“只对账，不提交工单”时，协调器递增 `revision`，取消旧模型尝试和未开始的写入，使旧批准失效。提交器在实际触发动作前检查修订号和目标版本。已经越过提交点的操作进入回读核实，界面告诉员工哪些事已发生。旧模型迟到的工具调用不能改变新任务。员工关闭窗口可以只收起界面；退出 Runtime、设备关机或休眠时依靠检查点恢复，定时任务在下次可用会话按规则补跑。

## 4. 工具层和 Windows 的权限边界

| 工具层 | 例子 | 执行与验证 |
|---|---|---|
| 文档／业务接口 | 枚举指定目录、读工作表、生成 DOCX、按业务键查单 | 输入输出结构化，写新文件先用暂存副本。金额、字段和总数通过程序核对 |
| 浏览器／原生控件 | 在许可站点填草稿，或操作指定 Windows 工单窗口 | Playwright 使用独立业务配置，FlaUI 按窗口和控件操作；写前核对客户、账号和记录，写后重新读取 |
| 视觉操作 | 老应用无法识别控件，需要看局部窗口再定位 | 截图限已授权窗口。失焦、遮挡、目标不唯一、UAC、密码或 MFA 界面出现时暂停。单桌面只允许一个前台任务 |
| 通用计算 | 新任务需要临时分析脚本，例如归并此前没见过的表格结构 | 在受限、可丢弃的计算环境运行，提供选定输入的只读副本和独立输出目录，默认封闭网络。工具校验输出后由本机执行器发布 |

MCP 用于工具发现、参数及结果传递。工具注册表另行定义可读写范围、副作用、审批和验证方式。例如模型可提出 `ticket.create_draft(project_ref, fields)`；正式提交只接受本机生成的草稿引用及经 Runtime 核对的一次性批准凭据，模型无权生成该凭据。工作进程不能访问批准接口或发布凭据。Windows 执行器须按实际打开的目标复核权限，防止目录联接、别名、重定向和窗口切换使用户批准的对象发生变化。桌面应用运行在已登录用户权限下，窗口授权并不能从操作系统层面保证该应用只能打开选定目录。正式提交尽量经可核对字段和账号的业务工具完成。只靠坐标、无法回读结果的软件，允许填草稿，由员工完成最后一步。[MCP 工具规范](https://modelcontextprotocol.io/specification/2025-11-25/server/tools) · [Windows 重解析点](https://learn.microsoft.com/en-us/windows/win32/fileio/reparse-points) · [Playwright .NET](https://github.com/microsoft/playwright-dotnet) · [FlaUI](https://github.com/FlaUI/FlaUI)

实现通用计算前先清点员工 Windows 版本与虚拟化支持。可用 Windows Sandbox 的终端用专门配置禁网、禁剪贴板、只映射必要目录；不具备隔离环境的设备，首版只运行受管工具。能力令牌绑定任务、工具、对象、权限和时效。一般文件读写与可运行任意代码的进程须有真实的操作系统权限隔离。系统提示词、工具名称、子进程和普通进程间通信都不能单独提供这种隔离。[Windows Sandbox 配置](https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/windows-sandbox-configure-using-wsb-file) · [AppContainer 隔离](https://learn.microsoft.com/en-us/windows/win32/secauthz/appcontainer-isolation)

## 5. 上下文、记忆与成品

上下文由本机按轮组装，顺序为公司策略、目标及当前修订、工作区显式指令、已核实的任务摘要、本轮相关原文和工具结果。手册、网页、OCR、窗口文字及工具返回均作为带来源的数据。工作区文件不能自行授权工具。缩短长任务上下文时保留员工的最新纠正、待核实写入、工具回执、成品版本与引用，再删减重复原文。模型摘要不能取代金额、单号或权限记录。

**记忆分三个作用域。** 当前任务的运行状态保存在本机 SQLite；项目事实与资料索引归当前工作区；跨项目的写作偏好只保存员工明确认可的条目。每条长期记忆带作用域、来源、确认时间并可删除。需要继承共享盘权限的内容，每次用于推理前重新验证原文件读取权限与版本。临时截图默认只在内存传递，若确有诊断要求才由员工选择留存。

**成品有独立身份和版本。** 模型返回候选内容，文档工具在本机生成实际 XLSX / DOCX / PDF，新版本关联来源、父版本和校验结果。员工在成品旁说“把税额拆成两列”时，只把上一版本和相关证据送入下一轮；新旧文件均能打开、比较和恢复。首期覆盖 Office 文件、表格和文稿的编辑反馈。交互网页产物后续可增加无网络、无宿主权限的预览环境。[Claude Artifacts 体验参照](https://support.claude.com/en/articles/17153992-what-are-artifacts-and-how-do-i-use-them) · [Open XML SDK](https://github.com/dotnet/Open-XML-SDK)

## 6. 外部模型 API 与内网 GX10

**模型接口与电脑驱动分开实现。** 公司采用三个适配器：OpenAI Responses、Anthropic Messages、GX10 上的 vLLM。上层把完整的模型返回转换为候选文本、工具请求或待处理的安全检查；受管 Windows 驱动接受经过本机策略校验的动作。公司业务层复用一套工具与恢复协议。兼容 Chat Completions 的供应商先测试函数调用、图像和流式参数能力，再决定是否启用视觉操作；原生电脑工具按各家的协议实现。

| 路径 | 推理服务返回什么 | Windows 执行器怎样应答 |
|---|---|---|
| OpenAI Responses 的原生电脑工具 | 向 `POST /v1/responses` 声明 `{"type":"computer"}`。响应中的 `computer_call.actions[]` 是有序动作，有时首个请求只有截屏 | 按序校验并执行，需要处理的安全检查先走审批；完成后取新截图，以原 `call_id` 回传 `computer_call_output`。若半批中断，记录实际执行到哪一步，先核实现场。[当前接口](https://developers.openai.com/api/docs/guides/tools-computer-use) · [驱动和逐动作钩子](https://developers.openai.com/api/docs/guides/tools-computer-use-sdk-drivers) |
| Anthropic Messages 的原生电脑工具 | 向 `POST /v1/messages` 声明 `{"type":"computer_toolset_20260801"}`，选用支持该工具集的模型。同轮可返回多条带 `toolset_name: "computer"` 的 `tool_use` | 顺序处理，下一个用户消息里逐项返回匹配 `tool_use_id` 的 `tool_result` 并回显 `toolset_name`；截图及缩放返回图像，其他动作按协议返回文本或错误。[当前接口](https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool) · [官方 SDK 驱动接口](https://platform.claude.com/docs/en/agents-and-tools/tool-use/browser-use-sdk#computer-toolset) |
| GX10 或其他经验证的兼容 API | 通过函数调用请求诸如 `read_window`、`set_field`、`save_draft` 等公司工具 | 复用 Windows 驱动。支持图像且通过定位测试才开放坐标操作；仅支持文本工具调用时，可使用 UI Automation 控件树或浏览器快照。[vLLM 工具调用](https://docs.vllm.ai/en/stable/features/tool_calling/) |

每个动作都绑定本机 `action_id`、任务修订和窗口。批量动作另记供应商调用引用、批号和序号，先前已执行的点击有自己的记录。模型给出的供应商调用 ID 只用于协议匹配。出现焦点变化或批准失效时，停止本批剩余动作；能继续原协议时如实返回结果与中止信息，无法安全衔接时按本机检查点重新观察并开一轮新推理。截图经授权后裁剪、遮住敏感区域并记录窗口及坐标映射；点击前复验窗口、显示器、缩放及截图是否仍有效。

**外部 API 的数据边界。** 员工电脑保存资料、聊天和索引。走外部端点时，实际发送的片段、对话和截图会被供应商处理，并可能按其政策留存。文本外发和窗口截图外发分别授权；遮罩无法可靠去除敏感信息时，转用已许可的结构化工具或内网路径。OpenAI 可按端点配置 `store:false`，其滥用监测另有保留规则；Anthropic 商业 API 有常规保留期限、合同例外及部分模型的特别要求。若要求公司外不得持久保存内容，须按所选模型、端点及公司协议核实资格，否则将工作区绑定 GX10。[OpenAI 数据控制](https://developers.openai.com/api/docs/guides/your-data) · [Anthropic 商业数据留存](https://privacy.claude.com/en/articles/7996866-how-long-do-you-store-my-organization-s-data) · [Anthropic 特定模型规则](https://privacy.claude.com/en/articles/15425996-data-retention-practices-for-covered-models)

接入配置固定 `provider / endpoint / model / capabilities / egress_policy`。生产优先复用公司现有 API 网关保管组织级密钥，终端只拿短期、按员工或设备限定的凭据；小规模直连试点使用单独的可撤销密钥，放在 Windows 凭据存储。原生模型调用、长文本规划和 GUI 截图都在出站前过策略。认证失败待修复，限流按退避重试，超时重试只覆盖允许的推理端点。切换供应商先查数据边界及能力，用本地事实构造新请求；不会把一家的会话 ID、不完整动作、隐藏推理块或未核实结果发给另一家。局域网模型中断时，受限任务留在本机等待。

**内网推理部署。**

两台各放一份相同修订的模型、工具模板和解析器，统一内网 HTTPS 请求格式。初选 Qwen3.8-27B NVFP4 + vLLM，使用 NVIDIA 在 128 GB GB10 上验证的容器及参数建立基线。适配器显式支持流式文本、图片、结构化工具调用、取消和超时；用固定测试消息验证模型、服务端解析器与客户端对同一次调用的名称和参数理解一致。节点只保存权重及无正文指标。操作系统、容器、代理与异常转储的配置须纳入持久数据验收。[NVIDIA 固定配方](https://github.com/NVIDIA/local-ai-inference-recipes/blob/7d1162cfb965847d9412cef9bd5c5d4ade53d957/models/qwen/qwen3.8-27b/dgx-spark/linux/vllm/128gb/nvfp4/recipe.yaml) · [vLLM 工具调用](https://docs.vllm.ai/en/stable/features/tool_calling/)

模型适配器优先复用当前健康端点，避免单次任务在两台间来回切换。每个端点执行用户／设备并发配额和有界排队，员工等批准时释放推理槽位；工作区索引、OCR 和桌面动作留在员工电脑。达到队列上限时显示等待或繁忙，可取消。双机不是一个自动合并的 256 GB 内存池。席位以短问、长文、单图和多轮任务混合并发测得的 P95 首字、排队时间、任务用时及验收成功率决定。

在每台 GX10 的推理服务前配置同机 HTTPS 入口。试点由 IT 向终端下发设备证书，入口校验签发链和设备身份后执行配额；已有内网身份系统时可叠加短期用户令牌，分别控制用户与设备。vLLM 只接受入口转发的请求。两端配额先按总目标拆分；切换到单机时使用实测的降级上限。批准、文件工具和聊天存储不部署到该入口。

## 7. 失败、恢复与重复执行

### 两步续跑协议

`/resume` 与“继续”是本机 Runtime 解析的指令。Claude Code 的同名命令可选择历史会话；本方案另外持久化业务动作检查点，因此员工能接着做同一个任务。本机用 `thread_id` 定位 LangGraph 检查点。输入“继续”后，协调器读取中断原因：业务审批仍待正式批准，已批准时才向对应的 `interrupt()` 传入决定；命中人工中断时使用同一线程的 `Command(resume=决定)`。异常退出且仍有待运行节点时，核对账本后用同一线程的 `graph.invoke(None, config)` 续跑。两者均取最新检查点。[恢复方式对照](https://docs.langchain.com/oss/python/langgraph/checkpointers#choose-how-to-continue-a-thread)中断节点可能从头运行，因此把外部写入置于独立执行节点。[Claude Code 命令](https://code.claude.com/docs/en/commands) · [LangGraph 检查点](https://docs.langchain.com/oss/python/langgraph/checkpointers) · [中断及恢复](https://docs.langchain.com/oss/python/langgraph/interrupts)

| 员工操作 | 本机行为 | 可观察结果 |
|---|---|---|
| 输入 `/resume` 或 `/resume <任务ID>` | 只读检索当前 Windows 用户的任务。有多项则列出项目、停点和时间；选中后装载本地快照及事件。此时不唤醒模型、不采屏、不发业务请求 | 展示目标、已完成的步骤和文件版本、上次中断点、待核实项及预计下一步。即使模型端点离线也能查看 |
| 在选定任务中输入“继续” | 原子认领该任务的执行权，绑定任务修订与新的运行代次；重复输入或两个窗口同时操作只产生一次恢复。校验工作区、账号、数据策略、凭据、文件版本和窗口；需要时先取得新的观察 | 从第一个未完成且可安全执行的步骤继续。已确认完成的步骤沿用回执；过期批准重新申请，待核实动作先查结果。“继续”不代替提交审批 |
| 进入仍在运行或已完成的任务 | 若断的是界面连接而后台仍活着，重新订阅原执行实例；已完成任务展示成品及回执 | 不生成第二个执行循环，已结束任务不因再次输入“继续”重新运行 |

持久化的恢复依据包括 `task_id / user_id / workspace_id / revision / last_event_seq / run_epoch`，以及每个动作的意图、派发状态、回执和成品指纹。执行器拒绝旧运行代次的迟到消息。完全退出后，在同一台电脑、同一用户且本地检查点仍在的前提下可恢复。供应商响应 ID 可用于短时续接；会话过期或更换模型时，用本地证据重新构造请求。中断的流式响应、模型内部推理位置、未保存的应用状态都不作为恢复点。

以工单为例，断线时差异表已经生成，提交动作已派发。员工输入 `/resume`，先看到“差异表已保存，工单提交结果待核实”；输入“继续”后，Runtime 查业务键。查到工单号就补写回执，往下做。确需重发时，必须先验证批准仍覆盖本次操作，并沿用目标系统承认且仍有效的原幂等键。没有幂等键时，要取得前次调用已终止且未提交的可靠证据；一次“暂未查到”不足以重发。无法核实时保持“待人工核实”，避免产生第二张工单。

### 各故障点的处理

| 出错位置 | Runtime 应怎样处理 |
|---|---|
| GX10 A 只返回一半工具参数后失联 | 丢弃整个尝试，向 B 发起新尝试；拒收旧尝试的迟到事件。参数完整并通过校验才可入账 |
| 模型调用已完成，本机动作尚未开始 | 读取已有动作意图，检查当前修订、批准与目标版本。相同动作 ID 只允许一个执行者取得执行权 |
| 本机或目标系统已写入，回执尚未持久化 | 使用文件哈希、业务幂等键或独立查询重建状态；不能查实则转“待核实”。模型故障切换不得触发第二次业务提交 |
| 原表在运行中被同事修改 | 比对来源版本；重新计算差异，展示新预览，旧批准失效 |
| 员工拒绝、取消或锁屏 | 停止派发新动作，标明已生效和仍待核实的动作；解锁后再次核对活动窗口与账号 |
| 工作区许可的模型端点均不可用 | 保留任务，可用 `/resume` 查本机进度、成品和回执；需要规划和生成的步骤等端点恢复后由员工续跑 |

SQLite 事务无法同时覆盖另一套业务系统的“提交”按钮。对于支持幂等键的业务 API，重试沿用原键；对于既无幂等键又不能查询结果的旧 GUI，提交超时后由人核查。界面必须能显示“结果未知”。[SQLite 事务边界](https://www.sqlite.org/atomiccommit.html)

## 8. 哪些复用 GitHub，哪些自己掌握

| 决策 | 取舍 |
|---|---|
| OpenAI 的公开实现 | [openai/openai-cua-sample-app](https://github.com/openai/openai-cua-sample-app/tree/6dd34b40acc1d002696ccd41aaf0a1f2f2be290c) 是 MIT 样例，适合复用模型循环、Playwright/PyAutoGUI 驱动分层和中断处理测试。生产 Windows 执行器另做授权、隔离、动作账本和业务回读。[Python 示例及恢复边界](https://github.com/openai/openai-cua-sample-app/blob/6dd34b40acc1d002696ccd41aaf0a1f2f2be290c/python-app/README.md) |
| Anthropic 的公开实现 | [anthropics/claude-quickstarts 的 computer-toolset](https://github.com/anthropics/claude-quickstarts/tree/4a4224bdf0e9cfb3332167b69c90457403813625/computer-toolset) 提供 Python、TypeScript 的 VNC 驱动与无需模型的动作测试脚本。它演示了部分桌面动作，企业 Windows 控件和业务回读要另接；仓库为 [MIT 许可](https://github.com/anthropics/claude-quickstarts/blob/4a4224bdf0e9cfb3332167b69c90457403813625/LICENSE)。 |
| Windows 与浏览器执行器 | [Windows-MCP](https://github.com/CursorTouch/Windows-MCP/tree/3e24d26aa2d7ee8c07d0aa8b17b9f395e569b828)（MIT）可在真实 Windows 用户会话提供 UIA 快照、点击、输入和截图，适合验证通用动作。生产 C# 路径可直接用 [FlaUI](https://github.com/FlaUI/FlaUI) 定位指定窗口和控件；网页用 [Playwright MCP](https://github.com/microsoft/playwright-mcp/tree/2337aff0213d28db41d3e3daf48cee19b3d0bd82)（Apache-2.0）验证可访问性树，固定业务流程用 Playwright .NET。仅向模型发布本任务允许的工具，通用执行器经公司 Runtime 调用。 |
| 参考成熟桌面实现 | **LobsterAI** 有面向 Windows x64 的 Cowork 会话、工具时间线、Office 预览和电脑操作，仓库说明其 Electron 主进程保存 SQLite 会话，OpenClaw 负责推理循环。可用于本地体验对照和考察现有实现。仓库还包含使用统计、外部分享和联网功能，企业内网要分别核查。[固定版本源码](https://github.com/netease-youdao/LobsterAI/tree/2026.11.16) · [使用统计实现](https://github.com/netease-youdao/LobsterAI/blob/2026.11.16/src/main/libs/usageAnalytics.ts) |
| 验证自定义内网模型 | **OpenWork** 的本机工作区和自定义 OpenAI 兼容端点适合快速测试 GX10 的工具调用。即使原型能完成任务，也要独立验证工具边界、运行时持久化和外部连接。[OpenWork](https://github.com/different-ai/openwork) · [本地模型配置](https://openworklabs.com/guides/ollama) |
| 复用工具和 SDK | C# MCP SDK、Open XML、Playwright 和 FlaUI 负责成熟的互操作部分。已有业务 API 优先复用。公司适配器只暴露带验证方式的动作。[C# MCP SDK](https://github.com/modelcontextprotocol/csharp-sdk) · [Open XML](https://github.com/dotnet/Open-XML-SDK) · [Playwright](https://github.com/microsoft/playwright-dotnet) · [FlaUI](https://github.com/FlaUI/FlaUI) |
| 公司掌握的核心 | 目标／修订语义、状态与恢复、上下文选择、批准与提交边界、业务回执、成品版本、模型路由和可重复的验收集。首版由本机 LangGraph 协调器独占任务调度权，前端通过其接口接入 |

前端或工具组件可从已有项目复用，准入条件是工具调用进入同一授权路径、迟到事件可丢弃、外部连接受工作区策略约束。首版全部正式任务交给本机 LangGraph 主图调度。

## 9. 工程验证顺序

**第一个里程碑是最小执行内核。** 在一台 Windows 和一个 GX10 上打通一句自然语言目标、两个不同工具、一份真实 XLSX、一次修改及重启恢复。先用固定的假模型响应测试状态、批准、断线和重放，再接真实模型测选择正确率。这样可以区分推理错误、调度错误和工具错误。

| 验证 | 方法与通过条件 |
|---|---|
| 能处理新任务 | 保留一组开发时没见过的表格与办公指令，包含改列名、多个工作表与新输出要求；允许 Agent 组合通用工具。按数字、来源和产物可打开性评分 |
| 能接受中途纠正 | 在模型调用中、等待批准、提交刚开始时分别插入“只生成文件”；旧修订在切换后新增的未授权写入为零，已开始的写入有明确回执或待核实状态 |
| 能故障恢复 | 在动作登记前、外部操作后但记账前、记账后分别终止进程；恢复后不得漏报未知状态或重复提交。分别中断推理流前半段和工具返回后的下一轮 |
| 两步续跑 | 断模型网络、仅断 UI、强制退出进程、重启电脑各做一次。发 `/resume` 后模型及业务请求数为零；双窗口同时输入“继续”最多取得一个新执行权；已提交单据总数不增加，原会话 ID 失效仍可基于本机检查点恢复 |
| 供应商适配与外传 | 用相同的脱敏窗口，在 OpenAI、Anthropic 及合适的 GX10 模型上分别回放“读状态 → 输入 → 回读”。校验动作顺序、DPI、坐标、部分批次失败及真实回包。分别测试允许传文字但禁止截图、仅限内网、外部 API 超时；禁传标记对外出现次数为零 |
| 能隔离员工与项目 | 两台员工电脑放同名文件与不同唯一标记，切换客户、撤销共享盘权限、使用目录联接；检查真实推理载荷、工单账号、成品、日志和落盘，禁止标记不得泄露 |
| 能管理真实桌面 | 窗口失焦、账号改变、锁屏、MFA、坐标不唯一时均暂停；独立回读业务值，并验证人工接管后不会收到残留点击 |
| 能服务预期负载 | 两台 GX10 健康与单机维护两种情况下测 1、2、4、8 并发；记录有效答案／有效工具参数首到时间、完整任务耗时、成功率和排队，不以单独的 token/s 决定席位 |

**工程决策的依据是实机回放。** 首先取得一组可脱敏的手册、对账文件和可回读的测试工单，再做上述状态机和故障注入。Claude 的交互流程可以作为对标基线；公司模型在新任务上的推理质量、设备兼容性、用户并发上限和节时幅度均需实测。业务试点与部署清单见[岗位流程与部署方案](Windows本机办公Agent设计方案.md)。
