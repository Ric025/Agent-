# CompanyAgent：装在自己电脑上的办公 Agent

说一个目标，它自己分步完成：读文件、对账、算数、写 Word 和 PPT、查资料。动手改文件之前会先问你。
对话、资料、任务记录都只存在这台电脑上；模型用公司内网的服务。

设计依据见上一级目录的《Agent架构与选型.md》，接口细节见 `docs/实施说明.md`。

## 启动

```bash
# macOS：在“访达”里双击 启动.command（第一次如果提示“无法验证开发者”，右键 → 打开）
# macOS / Linux 命令行
bash start.sh

# Windows：双击 start.bat，或在命令行里运行
start.bat
```

**第一次启动会自动检查电脑、自动安装依赖**（见下面“安装和环境检查”），装完自己接着启动，大约 3–10 分钟，看网速。以后再启动时只花不到 1 秒确认“装过了、清单没变”，直接启动。

启动后终端会打印一个地址，形如 `http://127.0.0.1:8765/?t=一串字符`，并自动用浏览器打开。
**一定要用这个带 `?t=` 的地址打开**，它里面有本次启动生成的令牌；没有令牌的页面会提示“请用启动时打印的地址打开”。

常用参数：

| 参数 | 作用 |
|---|---|
| `--mock` | 用假模型启动，不连真实模型，用来先看看界面 |
| `--port 8765` | 端口；被占用时自动往后找 |
| `--home 目录` | 数据目录，默认 `~/.companyagent`（Windows 为 `%LOCALAPPDATA%\CompanyAgent`） |
| `--no-open` | 不自动打开浏览器 |
| `--setup` | 先重新检查、重新安装一遍，再启动（装坏了、换了网络以后用） |
| `--no-setup` | 这次跳过自动安装，直接启动 |
| `--check` | 只检查电脑和环境、打印结果，不安装也不启动 |

启动后直接是对话页，打字就能说话，不用先选文件夹。

- **默认文件夹**：没连别的文件夹时，对话都在“默认”空间里，Agent 读写的是默认文件夹。它的位置：`settings.json` 里配了 `default_folder` 就用它；否则用 `~/CompanyAgent`（Windows 为 `C:\Users\你的名字\CompanyAgent`）；用 `--home` 或环境变量 `COMPANYAGENT_HOME` 另指了数据目录时，用 `<数据目录>/files`。启动时终端会打印默认文件夹的位置。默认空间可以改名、改指令，不能删除。
- **把文件交给它**：在输入框里点“+”添加文件，或者直接把文件拖进窗口、粘贴进输入框。文件会存到当前文件夹的 `上传/` 里（重名时自动加 `(2)`、`(3)`，不会覆盖已有文件），单个文件最大 50 MB（设置项 `upload_max_mb`）。
- **连别的文件夹**：点“+”里的“用一个文件夹工作…”，或侧栏“文件夹”里的“添加文件夹…”，会弹出系统的选择文件夹窗口（macOS、Windows；Linux 要装 zenity）。弹不出来时改为手工填路径。连上以后，Agent 只能读写那个文件夹里的东西。

### 安装和环境检查

`start.sh` / `start.bat` 每次启动先调用 `setup/install.sh`（Windows 是 `setup\install.ps1`）。只在**第一次**、**依赖清单改过**、或者**上次没装成**时真正安装；做的事：

1. **看电脑**：系统、CPU、内存、磁盘剩余空间（少于 1.5 GB 不装）。
2. **找 Python 3.11 以上**：按 3.12、3.11、3.13 的顺序找电脑上已有的。没有（比如 Mac 自带的是 3.9）就用 uv 自动下载一个 Python 3.12，**只放在项目的 `.tools/` 里**，不动系统的 Python、不改 PATH、不要管理员权限。
3. **建虚拟环境** `.venv/`，所有包都装在里面。
4. **装依赖**：必需的（`setup/requirements-core.txt`）一个装不上就停下来说原因；可选的（`setup/requirements-extra.txt`：操作网页、向量库）逐个装，装不上的只是那个功能不能用。电脑上没有 Chrome / Edge 时，再下载一个操作网页用的浏览器（约 150 MB）。
5. **检查**：逐项检查环境，连一下 `settings.json` 里配的模型服务，再在这台电脑上把新内核自检跑一遍（`tests/lg_smoke.py`，几十秒）。**自检没通过时，`kernel=auto` 会先用过渡内核**，并在任务里说明原因，不会整个用不了。

结果都写在项目里，出问题时把这几个文件发给 IT：

| 文件 | 内容 |
|---|---|
| `setup/环境检查结果.txt` | 每一项“好 / 注意 / 不行”，不行的写了怎么修；各功能能不能用 |
| `setup/安装记录.txt` | 安装过程的完整输出 |
| `setup/新内核自检.txt` | 新内核自检的输出 |
| `.tools/setup-state.json` | 装好了没有、用的哪个 Python、哪些可选的没装上 |

没装成时：启动脚本说明原因，**一天之内不再自动重试**（免得每次启动都卡几分钟），先用以前装好的环境或电脑上已有的 Python 启动。修好以后运行 `bash start.sh --setup`（Windows：`start.bat --setup`）。

**公司网络要走代理、或者只能用内部软件源**：把 `setup/mirror.env.example` 复制成 `setup/mirror.env`，填上软件源（`PYPI_MIRROR`）、代理（`HTTPS_PROXY`）、Python 下载地址（`PYTHON_MIRROR`）等，再启动。

**完全不能上网的电脑**：在一台同样系统、能上网的电脑上把安装包下载好，放进 `setup/wheels/`，安装时会自动只用这里的包：

```bash
# 例：给 Apple 芯片的 Mac、Python 3.12 准备
python3.12 -m pip download -r requirements.txt -d setup/wheels \
  --only-binary=:all: --platform macosx_11_0_arm64 --python-version 3.12
```

这种电脑上还要先装好 Python 3.11 以上（不能自动下载）。

IT 统一部署时可以用环境变量 `CA_SETUP_PYTHON=某个 Python 3.11+ 的路径` 指定用哪个 Python 建 `.venv`。

依赖清单：

| 包 | 用途 |
|---|---|
| langchain、langchain-core、langgraph、langgraph-checkpoint-sqlite | 执行内核：Agent、批准、压缩、计划、检查点 |
| langchain-openai、httpx | 接 vLLM 等 OpenAI 兼容的模型服务 |
| langchain-mcp-adapters | 连接器（MCP）。没配置连接器时用不到 |
| openpyxl、python-docx、python-pptx、pypdf | 读写 Excel、Word、PPT，读 PDF |
| numpy | 语义检索的向量计算 |
| playwright（可选） | 操作网页 |
| chromadb、langchain-chroma、langchain-classic（可选） | 资料多时用的向量库；没有时用 numpy 照样能检索 |

> 自动安装在 Linux 上用假的离线安装包测过完整流程（`tests/test_setup.py`）；macOS 上真实下载要第一次运行时确认；**Windows 的 `install.ps1` / `start.bat` 还没在真机上跑过**。

### 执行内核

“内核”是中间那层：问模型、等批准、执行工具、断点续跑。界面、工具和接口不受影响。

- **LangChain / LangGraph（正式内核）**：LangChain 的 `create_agent` 加官方中间件，任务状态存在数据目录的 `checkpoints.db`。
- **过渡内核**：之前自研的单循环，只用标准库。新内核在真机上验证通过之前保留。

设置项 `kernel`（在 `settings.json` 里）：`auto`（默认，装了 LangChain / LangGraph 就用它，没装就用过渡内核，并在任务里提示）、`langgraph`（必须用新内核，装不上就报错退出）、`builtin`（一直用过渡内核）。启动时终端会打印当前用的是哪个内核。新旧内核的差异和第一次运行的步骤见 `docs/内核迁移说明.md`。

## 配置模型地址

代码里不带任何模型地址（内置的默认值只是占位）。三种方法任选：

1. **在界面里改**：左栏“设置” → “模型服务”，填名称、地址、模型名，点“保存”，再点“测试连接”。
2. **给整个单位定一个默认值**：把项目目录里的 `site.example.json` 复制成 `site.json`，填上模型地址和模型名。新装的电脑不用再逐台去界面里填。`site.json` 不进 git（`.gitignore` 里排除了），内网地址不会跟着代码传出去；格式和 `settings.json` 一样，只写要改的项。先后：本机 `settings.json` > `site.json` > 程序内置。
3. **改配置文件**：数据目录里的 `settings.json`（第一次在界面保存设置后生成），例如：

```json
{
  "endpoints": [
    {"name": "GX10-A", "base_url": "http://10.0.0.11:8000/v1", "model": "nvidia/Qwen3.8-Flash-Next-FP4-Local", "api_key": "", "internal": true},
    {"name": "GX10-B", "base_url": "http://10.0.0.12:8000/v1", "model": "nvidia/Qwen3.8-Flash-Next-FP4-Local", "api_key": "", "internal": true}
  ],
  "approval_mode": "auto"
}
```

- `base_url` 是 OpenAI 兼容接口的地址，通常以 `/v1` 结尾。可以写多台，一台不通时自动换另一台。
- `internal: true` 表示内网地址，访问时不走系统代理。
- `approval_mode`：`manual` 每步都问；`auto` 读文件和新建文件直接做，覆盖、删除、运行代码、对外发送先问；`full` 除了删除都直接做。删除任何模式下都要问。
- 联网搜索默认关闭，在“设置 → 联网”里打开，需要公司提供兼容 SearXNG 的搜索服务。

改完 `settings.json` 后重启生效；在界面里改的立即生效。

### 接多个不同的模型

`endpoints` 里可以是不同的模型，**列表顺序就是优先顺序**：平时用排在最前面、在线的那个；把又快又省的放前面、大模型放后面，大模型只在需要时才用（例如带图片的消息、内容太长时），换了会在对话里提示一句。每个端点可以多写两项：

```json
{"name": "GX10-B", "base_url": "http://10.0.0.12:8000/v1", "model": "某个能看图的大模型",
 "context_window": 262144, "caps": {"vision": true, "pointing": "norm1000"}}
```

- `context_window`：这个模型的上下文大小，不写就用 `model.context_window`。
- `caps`：手写的能力（`tools` 工具调用、`vision` 看图、`pointing` 能在图上指位置：`pixel` / `norm1000` / `false`）。不写就由程序启动时自动探测，“设置 → 高级 → 模型能力”里能看到结果、也能点“重新检测”。

模型做不到的功能（例如模型不能看图时的“看图片附件”），Agent 会直接告诉你“当前模型不支持……，无法使用……”，并说明可以改用什么办法，不会悄悄失败。

## 操作网页

Agent 可以替你打开网页、读页面、填表、点按钮（例如在 OA 上填报销单）。它用的是电脑上已有的 Chrome（Windows 上优先用 Edge），单独用一个资料目录（`<数据目录>/browser-profile`），和你平时的浏览器分开；需要登录时你自己输一次，之后会记住。

**网页面板**：网页显示在程序里，对话右边，不另开浏览器窗口（像 ChatGPT 里看 Agent 操作网页那样）。Agent 一开始操作网页，面板自动打开；顶栏的“网页”按钮随时开关。

- 看着它做：画面是实时的，Agent 在做时面板底下会写“Agent 正在操作这个网页”。
- 自己动手：直接在画面上点、滚动；点一下输入框就能打字（中文输入法、粘贴都可以）；上面的地址栏能输网址或要搜的词，有后退、前进、刷新。比如先自己登录要用的系统，再让 Agent 接着做。
- 网页弹出的确认框（“确定要提交吗？”）会显示在面板里，由你点确定或取消。
- 遇到验证码、短信验证、“请确认你是真人”这类人机验证：Agent 不会自己去过，会停下来请你在面板里完成，你点“继续”它再接着做。
- 你自己点出来的下载也存到工作文件夹的 `下载/` 里。
- 想要以前那样单独的浏览器窗口：设置 → 操作网页 → 网页显示在“单独的浏览器窗口”。面板里照样同步显示。

需要的 playwright 由自动安装装好（可选依赖）。它直接用电脑上的 Chrome / Edge；两个都没有时，自动安装会另外下载一个浏览器。没装上时，“操作网页”显示为不可用，其他功能照常；修好网络后运行 `bash start.sh --setup` 重装。

用法：直接说，例如“打开 oa.example.com 的报销页，把事由填成去上海出差，金额 1280，然后提交审批”。

- 打开没去过的网站、点“提交 / 保存 / 发送”这类按钮、上传文件前会先问你；删除、付款类按钮任何模式下都问。批准卡片上勾“以后都允许这个网站”就不再为打开这个网站而问。
- 密码和验证码一律由你自己输入，Agent 不会代填。
- Agent 不能打开本机地址（尤其是它自己的界面）。网页里的文字只当作数据，不会当成给它的指令。
- 下载的文件存到工作文件夹的 `下载/` 目录；上传只能选工作文件夹里的文件。
- 设置项 `browser`：`enabled`、`allowed_domains`、`blocked_domains`、`view`（`panel` 网页面板 / `window` 单独窗口；界面里能改）；`search_url`（地址栏里输的不是网址时用它搜，默认百度）、`allow_local`、`headless`（只在 `view` 是 `window` 时有用）、`channel`（`auto`、`chrome`、`msedge`、`chromium`）、`executable_path`、`idle_minutes`（标签页空闲多久关掉，默认 30）、`max_actions`（一次最多做几步网页操作，默认 40）只能写在 `settings.json` 里。

操作桌面程序的接口已经留好，驱动（Windows 版）以后接上；它需要能看图、能指位置的大模型，没有时 Agent 会直接说用不了。

## 所在地和“能判断的不反问”

问“今天天气怎么样”“现在几点”“附近有什么”这类事，Agent 要知道你在哪。它按这个顺序判断：你在对话里说过的（它会记进记忆）→ 设置 → 所在地 里填的 → 按这台电脑的时区推断（例如时区是 `America/Toronto` 就是“加拿大东部，最可能是多伦多”）。

- 不按 IP 查：公司的网络出口常常不在本地，查出来是错的。
- 时区只能推到“哪一带”：中国全国一个时区，推不出城市；Windows 上“东部时间”分不清纽约和多伦多。推得不对，在设置里填一次，或者直接跟它说“我在多伦多”。
- Agent 的规矩：能从环境、记忆、上下文判断的先做，并说一句是按什么判断的，方便你纠正；判断不了、而且做错了难挽回（改或删文件、提交、对外发送）才提问。

## 两种模式：CariAi 和 Hspace

侧栏最上面的开关在两个模式之间切，两边的对话各管各的（像 ChatGPT 桌面应用里的 ChatGPT 和 Codex）。

- **CariAi**：日常助手，动手做事、操作电脑。就是到现在为止的样子。
- **Hspace**：高阶工作空间，以公司资料为底。CariAi 能做的它都能做；涉及公司的制度、流程、数据、过往材料时先查资料库，按资料回答、每个结论标出处 `[1]`，资料里没有就明说；同一份制度有新旧版本时以现行的为准；对比两份表、照着已有的 PPT 做新的也在这里。

**资料库**（"知识库"页）跟对话无关，可以建好几个（"公司 SOP""销售资料""历年汇报"），每个由几个文件夹或文件组成（可以是共享盘）。资料库里的文件对 Agent 是只读的。加了文件夹就在后台收录，之后每 30 分钟同步一次（`kb.sync_minutes`），文件改了、删了自动跟上。支持 txt、md、csv、docx、pdf、xlsx、xlsm、pptx；扫描件 PDF 读不出字，会在文档列表里标出来。表头里有"问题""答案"两列的表格按问答对收录。

检索是关键词加向量的混合检索。**没有向量模型也能用**，只是退回关键词检索（要用资料里出现过的词来问）；接上向量模型后，意思相近的说法也能找到。只沾上"公司""子公司"这种到处都有的词、问题里要紧的词一个都没对上的片段不算找到（覆盖率低于 `kb.min_cover`，默认 0.35；向量相似度低于 `kb.min_sim`，默认 0.3，换了向量模型可能要调），这时如实回答"资料里没有找到"。检索测试页会把这些"差点对上的"单独列出来。

Hspace 另外三样：

- **核对数字**：每条回答说完，程序自己把带出处 `[n]` 的句子里的数字（金额、百分比、天数……）和那段原文对一遍，回答下面那行小字写"有 1 个数字没对上"，对不上的出处标签带个提醒点。不带出处却有数字的句子也会标出来。只是提醒，不改回答。CariAi 里的回答带了出处编号时也核对。
- **深入研究**：输入框上的"深入研究"开关（或 `/深入 问题`）。这一轮 Agent 会先把问题拆成几个小问题、每个用几种说法去查、要紧的地方看整节，最后写成有结构的回答并单列"资料里没有覆盖到的"；步数上限翻倍（最多 60 步），所以会慢一些。
- **反馈和标准答案**：每条回答下面有赞、踩。点踩后可以填"正确的答案是……"并"记成标准答案"；以后问到同样的问题（不分大小写、全半角、末尾的问号），检索结果第一条就是它，Agent 按它回答。标准答案在"知识库 → 标准答案"里列着，可以改、删。

后来又加了三样（规格和实际情况在 `docs/借鉴开源项目.md`）：

- **文档卡片**：知识库页点开一份文档，能看到它的摘要、目录、标签。目录是程序算的；摘要由模型在后台慢慢写（前台有对话时让路），模型不可用时先用文档开头，标"开头摘录"。摘要和标签都可以手工改。有了它，可以问"这份制度讲什么""哪些文件涉及审批"。
- **只在某几份文件里找**：Hspace 输入框下面的范围菜单可以选资料库、选标签、"指定文件…"；也可以在输入框里打 `#` 点名文件。标签默认取文件所在的前两层文件夹名（比如"财务部""报销"）。
- **资料运营**（知识库页）：
  - "没答上来的问题"：资料里没找到的、回答被点了"不对"的，自动记在这里。可以直接写答案（存成标准答案）、导出成表格。
  - 标准答案显示用过几次。
  - "检索评测"：列几道"问题 → 应该找到哪份文件"，改了资料或设置后点"跑一遍"，看前 1、3、5 条的命中率和上次比是升是降。只测检索，不用模型。

### 在内网起向量模型（可选）

用 vLLM 起 Qwen3-Embedding（OpenAI 兼容的 `/v1/embeddings`）。vLLM 新版本：

```bash
vllm serve Qwen/Qwen3-Embedding-0.6B --runner pooling --port 8001
```

老版本（没有 `--runner` 的）：

```bash
vllm serve Qwen/Qwen3-Embedding-0.6B --task embed --port 8001
```

重排模型（可选，能让排序更准）：

```bash
vllm serve Qwen/Qwen3-Reranker-0.6B --runner pooling --port 8002     # 老版本：--task score
```

然后在"设置 → 高级 → 语义检索"里填向量模型的地址（例如 `http://内网地址:8001/v1`）和模型名，点"测试"能算出向量长度就好了；重排模型同样填地址（可不填）。也可以写在 `settings.json` 里：

```json
"embedding": {"engine": "openai", "base_url": "http://内网地址:8001/v1", "model": "Qwen/Qwen3-Embedding-0.6B"},
"rerank":    {"engine": "vllm",   "base_url": "http://内网地址:8002/v1", "model": "Qwen/Qwen3-Reranker-0.6B"}
```

向量默认存在数据目录的账本里，用 numpy 算（自动安装会装好）；装齐了 `chromadb`、`langchain-chroma`、`langchain-classic`、`langchain-openai` 时改用 LangChain 的 Chroma（`kb.engine`：`auto` / `builtin` / `langchain`）。换了向量模型，旧向量会作废并在后台重算。

## 公司共享目录

没有服务器。公司在共享盘上放一个文件夹，每台电脑在"设置 → 公司共享目录"里填上它的路径，就跟着公司的走：

```text
<共享目录>/
  companyagent.json   {"version": 1, "name": "公司名", "contact": "IT 服务台"}
  settings.json       {"defaults": {…公司给的默认值…}, "locked": {…公司定死的…}}
  skills/<名字>/SKILL.md
  templates.json      任务模板，格式同 agent/templates.json
  libraries/<库名>/   资料库索引包，由"导出给大家用"生成，不要手工改
  inbox/gaps/         各人点"交给管资料的人"交上来的问题
```

- **设置**：`locked` 里的本机改不了，界面上置灰；`defaults` 里的，本机改过的以本机为准。只认这些键：`endpoints`、`embedding`、`rerank`、`asr`、`web`、`browser.allowed_domains`、`browser.blocked_domains`、`kb.top_k`、`kb.min_cover`、`kb.min_sim`、`kb.cards`、`tools.lazy`、`ui.theme`（只能放 defaults）、`approval_mode`（只能是 `manual` 或 `auto`）。别的键会被忽略，设置页里会列出来并说明原因。
- **资料库**：管资料的人在自己电脑上建库、收录，在资料库页点"导出给大家用"。其他人的电脑启动时和每隔 30 分钟看一次，有新版本就导入。导入的库带"公司"标记，只读，可以停用。
- **连不上共享盘时**：用上次同步的内容，设置页里写着上次同步的时间。
- **这个文件夹要设成只读**（只有 IT 和管资料的人能写）。能写它的人可以改技能、改模型地址，等同于这套程序的管理员。程序发现你有写权限时会提醒。
- 没在 Windows 和真的共享盘上试过，只在 Linux 的普通文件夹上验证过。

## 工具按需加载

模型的上下文窗口小于 64K 时，程序不会把全部工具的定义每次都发给模型，只带这次用得上的几组（按模式、按这句话里的线索、按以前用过的）。没带上的，模型可以自己加载，时间线上会多一步"加载工具：…"。`/状态` 里能看到带了哪几组。想关掉：设置 → 高级 → 工具按需加载选"关"。

模型连续三次发出完全一样的调用、结果也一样时，第三次会被拦下，时间线上标"重复的调用，已拦下"。

## 目录

```text
CompanyAgent/
  启动.command           macOS 双击启动
  start.sh / start.bat   启动脚本：先自动检查和安装，再启动
  requirements.txt       全部依赖（引用下面两个清单）
  setup/
    install.sh / install.ps1     自动安装（macOS、Linux / Windows）
    check_env.py                 环境检查，写 环境检查结果.txt
    requirements-core.txt        必需的依赖
    requirements-extra.txt       可选的依赖（操作网页、向量库）
    mirror.env.example           公司代理、内部软件源的配置样例
  .venv/                 自动安装建的虚拟环境（装好后才有）
  .tools/                自动下载的 Python、uv 和安装状态（需要时才有）
  agent/                 运行时（Python）
    lg/                  执行内核：LangChain / LangGraph（create_agent、中间件、检查点）
    __main__.py          入口：python3 -m agent
    server.py            本机 HTTP 接口和事件流（只监听 127.0.0.1，带令牌）
    runtime.py           任务线程、批准、恢复；按设置选择内核
    loop.py context.py   过渡内核：执行循环、上下文组装和压缩（新内核验证通过后退役）
    store.py             SQLite 账本
    modes.py             两种模式（CariAi、Hspace）的全部差别
    kb/  docparse.py     资料库：收录、按文档类型切片、关键词加向量的混合检索、文档卡片、资料运营
    toolsets.py          工具分组、按需加载、重复调用检测
    shared.py            公司共享目录
    api_*.py             后加的接口，各在自己的模块里（server.py 末尾载入）
    tools/               工具：文件、Office、资料库、计算、记忆、计划、定时、技能
    templates.json       新任务页上按岗位分组的示例任务（每条带 mode）
  ui/                    界面：index.html、app.css、app.js（不用框架，不引外部资源）
  skills/                内置技能，每个一个文件夹加一份 SKILL.md
  tests/                 测试；tests/realmodel/ 是真实模型探测脚本
  docs/                  实施说明
```

数据目录（默认 `~/.companyagent`）里有：`ledger.db`（账本：任务、事件、动作、审计）、`settings.json`、`token`（本次启动的令牌，只有当前用户能读）、`skills/`（放你自己的技能）。
工作文件夹里会多一个 `.agent/` 目录：被覆盖文件的旧版本、回收站、暂存的长结果。

## 技能和示例任务

- **技能**：`skills/<名字>/SKILL.md`，开头用 `---` 包起来写 `name` 和 `description`，正文写这类事怎么做好。
  内置六个：`excel-reconcile`（对账）、`report-writing`（汇报材料）、`slides`（PPT）、`doc-qa`（基于资料回答）、`data-analysis`（数据分析）、`meeting-brief`（会前简报）。
  公司或个人的技能放在数据目录的 `skills/` 下，重启后生效。和内置技能重名的不会盖掉内置的。
- **让 Agent 自己存技能**：做完一件步骤多的事，界面会提示“可以存成技能”；Agent 用 `skill_save` 保存前一定先问你（任何批准模式下都问）。
  存下的技能在“技能”页标为“我的”，可以查看正文、删除。
- **示例任务**：编辑 `agent/templates.json`，每条是 `{"role": 岗位, "title": 标题, "prompt": 要填进输入框的话}`。

**带空的任务模板**：模板里可以用 `{{月份:month}}`、`{{部门:choice=销售|市场|财务}}`、`{{备注?}}` 这样的空，点模板时弹一个小表单，填完文字放进输入框（不会自动发送）。类型有 `text`（默认）、`month`、`date`、`number`、`file`、`library`、`choice`，名字后面加 `?` 表示可以不填。自己的模板在"设置 → 我的模板"里建；公司的模板放在共享目录的 `templates.json`。

## 记忆和以前的任务

- **长期记忆**：两个有上限的本子，“关于我”（所有工作区通用）和“本工作区笔记”。你说“记住……”时 Agent 用 `memory` 工具记下；
  对话停下 2 分钟后，后台会自己整理一遍值得记住的内容（“记忆”页可以关掉“自动记忆”）。每次改动都留底，可以撤销。
  每次任务开始时整本记忆写进系统提示。密码、密钥和“忽略之前的指令”之类的话记不进去。
- **查以前的任务**：Agent 用 `session_search` 在本工作区以前的任务里查说过的话；侧栏的搜索框也能查。
- **导出记录**：任务页的“导出记录”把这个任务存成一份 Markdown（不含工具结果的正文）。
- **工作区说明文件**：工作文件夹里的 `AGENT.md`、`AGENTS.md`、`CLAUDE.md`（按这个顺序，用找到的第一个）会写进系统提示。
- 规格见 `docs/记忆设计.md`。
## 界面快捷键

| 键 | 作用 |
|---|---|
| 回车 | 发送 |
| Shift + 回车 | 换行 |
| Ctrl + K（Mac 上 ⌘ + K） | 查找和跳转：搜任务、切换文件夹、打开页面 |
| Esc | 关闭弹出的面板、菜单、预览 |

## 跑测试

```bash
cd CompanyAgent
python3 -m unittest discover -s tests -v     # 运行时和接口（用假模型，不需要网络）；没装 LangChain 时新内核的测试显示为跳过，没装 playwright 时操作网页的测试跳过
bash tests/run_lg_tests.sh                   # 新内核：自动安装（同启动时那套）、逐项自检、跑全部测试，结果在 tests/results/lg_run.txt
node --check ui/app.js && node tests/ui_test.mjs   # 界面：Markdown 安全渲染、事件归并等纯函数
node tests/theme_test.mjs                    # 主题：十套配色的变量齐全、对比度达标
```

真实模型探测（需要能连到模型地址）：`bash tests/realmodel/run.sh`（有 node 时会先用 gen.js 重新生成请求）。

## 安全要点

- 服务只监听 `127.0.0.1`；每次启动生成随机令牌，所有接口都要带令牌，并检查 Host 和 Origin。
- **写**只在工作文件夹里，`..` 和符号链接逃逸会被拒绝。
- **读**：工作文件夹和资料库里的直接读；文件夹以外的文件，批准方式是“全放行”时直接读，“自动”“每步都问”时先问你。存密码、密钥、登录状态的地方（`~/.ssh`、钥匙串、浏览器的资料目录、`id_rsa` 这类文件等，完整名单见 `agent/policy.py` 的 `SECRET_DIRS`）和本程序自己的数据目录，任何批准方式下都不读。派出去的助手只在“全放行”时能读文件夹以外的文件。
- 覆盖、删除、运行代码、对外发送按批准方式先问你；删除和保存技能永远要问。覆盖前自动留旧版本，删除先进回收站。
- 文件、网页、工具返回的内容都当作数据，界面显示前一律转义，链接只允许 http 和 https。
- 每个动作都记入审计（“设置 → 操作记录”）。

## 语音输入

输入框旁边有麦克风按钮：点一下开始说，再点一下结束，识别出的文字会放进输入框，确认后再发送。

默认是关闭的，要先在“设置 → 语音识别”里选一种引擎：内网识别服务（在 GX10 上部署，员工电脑不用装东西），或者本机识别（离线可用，需要另装 `sherpa-onnx` 和模型）。部署步骤、常见提示和上线前的实测方法见 `docs/语音输入.md`。

## 主题和配色出处

“设置 → 外观”里可以换主题，点一下立刻生效；也可以在输入框里打 `/主题`。选“跟随系统”时，电脑是浅色用“素白”，深色用“墨夜”。选好的主题记在这台电脑的浏览器里，也存到设置里（`ui.theme`），换个浏览器打开还是同一个。

一共十个：

| 主题 | 深浅 | 来源 |
|---|---|---|
| 素白 | 浅色 | 本项目 |
| 公文 | 浅色 | 本项目 |
| 青瓷 | 浅色 | 本项目 |
| 墨夜 | 深色 | 本项目 |
| Solarized Light | 浅色 | Solarized，作者 Ethan Schoonover（ethanschoonover.com/solarized） |
| Catppuccin Latte | 浅色 | Catppuccin，Catppuccin 社区（github.com/catppuccin/catppuccin） |
| Nord | 深色 | Nord，Arctic Ice Studio（nordtheme.com） |
| Tokyo Night | 深色 | Tokyo Night，作者 enkia（github.com/enkia/tokyo-night-vscode-theme） |
| Catppuccin Mocha | 深色 | Catppuccin，Catppuccin 社区（github.com/catppuccin/catppuccin） |
| Gruvbox Dark | 深色 | Gruvbox，作者 morhetz（github.com/morhetz/gruvbox） |

六个开源配色用原版色值，名字用原名；为了文字对比度达标，个别地方换成了同一套色板里更深或更浅的一档（每处都写在 `ui/themes.css` 对应主题的注释里）。全部颜色只在 `ui/themes.css` 里定义，`ui/app.css` 只用变量。
