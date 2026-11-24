"""工具按需加载和重复调用检测（docs/借鉴开源项目.md 第 3 节）。只用标准库，两个内核共用。

- 分组（GROUPS）、模式的默认组（MODE_DEFAULT）、信号词（SIGNAL_WORDS 等）、技能到组的对应（SKILL_GROUPS）都写在这一处，
  按工具的名字写，不去改各个工具文件。
- Toolset：一次运行里"带了哪些组"。运行开始时由程序定（不调模型）：模式的默认组 ∪ 这个对话以前用过的组
  （sessions.meta.toolset）∪ 这次发言里的信号 ∪ 点名的技能要用的组。功能开关不允许的工具本来就不在 available() 里。
  中途：模型调用 tool_load 加载一组；模型直接调用了没带的工具，就加载它所在的组并照常执行。
- LoopGuard：同一个调用、同样的结果连续出现，第 N 次不执行（N = tools.loop_guard，默认 3，0 表示不拦）。
"""
from __future__ import annotations
import hashlib, json, re, threading

TOOL_LOAD = "tool_load"
LAZY_CONTEXT = 64000          # tools.lazy = auto 时：上下文窗口小于它就开
META_KEY = "toolset"          # sessions.meta 里记"用过的组"的键

# ---------------- 分组 ----------------
# 和规格 3.2 的初步分法相比做了三处调整（为了让两个模式的默认都不超过 7,000 字符，见笔记）：
#   session_search 从 core 挪到新的一组 memory（memory 工具本身后来挪回了 core，见下）；excel_query、excel_diff 从 excel 挪到新的一组 analysis；
#   kb_add 从 kb 挪到新的一组 kb_add（Hspace 默认带 kb，给"后端·资料"给 kb_* 加参数留出余量）。
GROUPS = [
    {"id": "core", "name": "常用工具", "desc": "常驻",
     "tools": ["ask_user", "todo_write", "list_dir", "read_file", "search_files", "write_file", "edit_file", "skill_load",
               "memory", "system_info", TOOL_LOAD]},
    {"id": "files", "name": "整理文件", "desc": "移动、删除文件", "tools": ["move_file", "delete_file"]},
    {"id": "excel", "name": "读写 Excel", "desc": "读 Excel、生成 Excel", "tools": ["excel_inspect", "excel_read", "write_xlsx"]},
    {"id": "analysis", "name": "查询和比对表格", "desc": "按条件查询汇总、比对两张表",
     "tools": ["excel_query", "excel_diff"]},
    {"id": "docs", "name": "生成文档", "desc": "生成 Word 和 PPT，读 PPT 的结构", "tools": ["write_docx", "write_pptx", "pptx_outline"]},
    {"id": "kb", "name": "查资料库", "desc": "查公司资料库", "tools": ["kb_search", "kb_open", "kb_list"]},
    {"id": "kb_add", "name": "收录资料", "desc": "把文件或文件夹加进资料库", "tools": ["kb_add"]},
    # memory 工具本身常驻（主控联调后挪回 core）：系统提示要求模型"该记的时候用 memory 记下"，提示里点了名的工具
    # 不在清单里，小模型容易犯糊涂。代价是 Hspace 默认约 7,350 字符，超过 7,000 的目标一点。这一组只剩查历史。
    {"id": "memory", "name": "查以前的对话", "desc": "查以前的对话记录", "tools": ["session_search"]},
    {"id": "browser", "name": "操作网页", "desc": "打开和操作网页", "tools": ["browser_open", "browser_look", "browser_do"]},
    {"id": "desktop", "name": "操作桌面", "desc": "操作桌面程序", "tools": ["desktop_open", "desktop_look", "desktop_do"]},
    {"id": "web", "name": "联网搜索", "desc": "联网搜索和抓取", "tools": ["web_search", "web_fetch"]},
    {"id": "code", "name": "运行代码", "desc": "运行 Python 做分析", "tools": ["run_python"]},
    {"id": "auto", "name": "自动化", "desc": "定时任务、存成技能、派子代理", "tools": ["schedule_create", "skill_save", "spawn_agent"]},
]
GROUP_BY_ID = {g["id"]: g for g in GROUPS}
TOOL_GROUP = {n: g["id"] for g in GROUPS for n in g["tools"]}
MCP_PREFIX = "mcp:"

# 模式的默认组（规格 3.2 末尾）
MODE_DEFAULT = {"cariai": ["core", "files", "excel", "docs"], "hspace": ["core", "kb", "excel", "docs"]}

# ---------------- 信号（规格 3.3 第 3 条） ----------------
# 文件扩展名：后面不能紧跟字母数字（中文紧跟着也算，例如"销售.xlsx里"）
EXT_SIGNALS = [
    (re.compile(r"\.(xlsx|xlsm|xls|csv)(?![A-Za-z0-9])", re.I), "excel"),
    (re.compile(r"\.(pptx|ppt|docx|doc)(?![A-Za-z0-9])", re.I), "docs"),
]
URL_RE = re.compile(r"https?://\S+|www\.[A-Za-z0-9-]+\.[A-Za-z]{2,}"
                    r"|(?<![A-Za-z0-9@._-])[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.(?:com|cn|net|org|io|gov|edu|top|xyz|info|biz)"
                    r"(?![A-Za-z0-9])", re.I)
# 词表：一组十来个词，容易改。英文词不分大小写
SIGNAL_WORDS = {
    "excel": ["Excel", "表格", "工作表", "电子表"],
    "analysis": ["对账", "核对", "比对", "对比", "对一下", "比一下", "差异", "两版", "同比", "环比", "排名", "分组汇总", "透视"],
    "docs": ["Word", "PPT", "幻灯片", "演示文稿", "文档", "汇报材料", "报告", "简报"],
    "browser": ["网页", "网站", "浏览器", "官网", "登录", "填表", "网址", "链接"],
    "kb": ["资料库", "知识库", "制度", "规定", "SOP", "流程", "办法", "规范", "手册", "政策"],
    "kb_add": ["加进资料库", "加到资料库", "导入资料库", "导入知识库", "收录", "建资料库", "建一个资料库"],
    "auto": ["每天", "每周", "每月", "定时", "到点", "提醒我", "存成技能", "存为技能", "子代理"],
    "web": ["搜一下", "上网查", "网上查", "网上搜", "最新的", "联网", "搜索一下", "新闻"],
    "code": ["算一下", "统计", "画图", "图表", "跑个脚本", "Python", "脚本", "计算"],
    "memory": ["记住", "记下来", "记一下", "别忘了", "忘掉", "以后都", "上次", "之前聊", "以前聊", "那次", "上回"],
    "files": ["移动", "挪到", "删除", "删掉", "重命名", "改名", "归档", "整理文件夹"],
    "desktop": ["桌面程序", "桌面应用", "操作软件", "打开软件", "本机程序"],
}
# 这些词一出现就带上全部连接器组；另外连接器的名字出现在话里时带上那一个
MCP_WORDS = ["连接器"]

# 点名的技能要用的组（技能自己在开头写了 tools: 的以它为准，见 skill_groups）
SKILL_GROUPS = {
    "data-analysis": ["excel", "analysis", "code"],
    "doc-qa": ["kb"],
    "excel-reconcile": ["excel", "analysis"],
    "kb-research": ["kb"],
    "learn-from-deck": ["docs", "kb"],
    "meeting-brief": ["docs", "excel"],
    "report-writing": ["docs"],
    "slides": ["docs"],
    "sop-qa": ["kb"],
}

_MCP_DESC = re.compile(r"^\[连接器 (.+?)\]")


def mcp_server(tool):
    """连接器工具所属的服务名（两个内核包出来的连接器工具，说明都以"[连接器 名字]"开头）；不是连接器返回 None。"""
    name = getattr(tool, "name", "") or ""
    if name in TOOL_GROUP:
        return None
    m = _MCP_DESC.match(getattr(tool, "desc", "") or "")
    return m.group(1) if m else None


def group_of(tool):
    """工具 → 组 id。登记表里没写到的内置工具算 core（新加的工具不会被悄悄藏起来；测试会检查每个工具都写到了）。"""
    name = tool if isinstance(tool, str) else getattr(tool, "name", "")
    g = TOOL_GROUP.get(name)
    if g:
        return g
    if not isinstance(tool, str):
        srv = mcp_server(tool)
        if srv:
            return MCP_PREFIX + srv
    return "core"


def group_info(gid, ntools=0):
    """组的展示信息：{id, name, desc}。连接器组：名字是"连接器 X"，一句话写工具数。"""
    if gid.startswith(MCP_PREFIX):
        srv = gid[len(MCP_PREFIX):]
        return {"id": gid, "name": "连接器 %s" % srv, "desc": "连接器 %s（%d 个工具）" % (srv, ntools)}
    g = GROUP_BY_ID.get(gid) or {"id": gid, "name": gid, "desc": ""}
    return {"id": g["id"], "name": g["name"], "desc": g["desc"]}


# ---------------- 开关 ----------------

def lazy_setting(cfg):
    v = str(cfg.get("tools", "lazy", default="auto") or "auto").strip().lower()
    return v if v in ("auto", "on", "off") else "auto"


def context_window(rt):
    """当前模型的上下文窗口：问 agent/capability.py（排在最前面、活着的端点的；没有端点时用 model.context_window）。"""
    try:
        from . import capability
        return int(capability.get(rt).summary()["context"])
    except Exception:
        return int(rt.cfg.get("model", "context_window", default=32768) or 32768)


def lazy_on(rt):
    """tools.lazy：on 开；off 关（全带）；auto（默认）上下文窗口小于 64K 时开。"""
    v = lazy_setting(rt.cfg)
    if v == "on":
        return True
    if v == "off":
        return False
    return context_window(rt) < LAZY_CONTEXT


def guard_limit(cfg):
    v = cfg.get("tools", "loop_guard", default=3)
    if isinstance(v, bool) or not isinstance(v, int):
        return 3
    return max(0, v)


# ---------------- 信号 ----------------

def signals(text, files=(), mcp_names=()):
    """这次发言里的信号 → [(组 id, 命中的词)]，按命中顺序，每组只记第一次。程序判断，不调模型。"""
    text = text or ""
    blob = "\n".join([text] + [str(f) for f in (files or [])])
    out, seen = [], set()

    def hit(g, why):
        if g not in seen:
            seen.add(g)
            out.append((g, why))
    for rx, g in EXT_SIGNALS:
        m = rx.search(blob)
        if m:
            hit(g, m.group(0))
    m = URL_RE.search(text)
    if m:
        hit("browser", m.group(0))
    low = blob.lower()
    for g, words in SIGNAL_WORDS.items():
        for w in words:
            if w.lower() in low:
                hit(g, w)
                break
    for srv in mcp_names or ():
        if srv and srv.lower() in low:
            hit(MCP_PREFIX + srv, srv)
    if any(w in blob for w in MCP_WORDS):
        for srv in mcp_names or ():
            hit(MCP_PREFIX + srv, MCP_WORDS[0])
    return out


def parse_tools_meta(v):
    """技能开头的 tools: 一行（"[excel, code]"、"excel_query, run_python" 或列表）→ 名字列表；没写返回 None。"""
    if v is None or v == "":
        return None
    if isinstance(v, (list, tuple)):
        items = [str(x) for x in v]
    else:
        items = str(v).strip().strip("[]").replace("，", ",").split(",")
    return [x.strip().strip("'\"") for x in items if x.strip().strip("'\"")]


def skill_groups(rt, name):
    """点名的技能要用的组。技能自己写了 tools:（组名或工具名都行）以它为准，否则查 SKILL_GROUPS。"""
    if not name:
        return []
    sk = None
    try:
        sk = rt.skills.get(name) if getattr(rt, "skills", None) is not None else None
    except Exception:
        sk = None
    declared = (sk or {}).get("tools") if isinstance(sk, dict) else None
    if declared:
        out = []
        for x in declared:
            g = x if (x in GROUP_BY_ID or x.startswith(MCP_PREFIX)) else TOOL_GROUP.get(x)
            if g and g not in out:
                out.append(g)
        return out
    return list(SKILL_GROUPS.get(name, []))


def _tool_load_desc(lines):
    return "加载一组工具，从下一步起可以用。现在没带的组：\n" + "\n".join(lines)


# ---------------- 一次运行带哪些组 ----------------

class Toolset:
    """一个任务一次运行（一个任务线程）里带了哪些组。owner 是 loop.Runner 或 lg.kernel.TaskKernel（它们有 rt、store、sid）。

    tools：available() 给出的全部工具（模式、功能开关、禁用都已经过滤过）。不按需加载时（子代理、tools.lazy 关、上下文够大）
    visible() 就是全部，不带 tool_load。"""

    def __init__(self, rt, sid, mode, tools, sub=False, lazy=None):
        from . import modes
        self.rt, self.store, self.sid = rt, rt.store, sid
        self.mode = modes.norm(mode)
        self.all = {}
        for t in tools:
            if t.name != TOOL_LOAD:
                self.all.setdefault(t.name, t)
        self.sub = sub
        self.lazy = (not sub) and (lazy_on(rt) if lazy is None else bool(lazy))
        self.groups = {}                  # 组 id → [工具名]（只有这次能用的工具）
        for n, t in self.all.items():
            self.groups.setdefault(group_of(t), []).append(n)
        self.loaded = []                  # 按加载先后
        self.why = {}                     # 组 id → 为什么带上（mode / used / signal:词 / skill:名 / tool_load / auto）
        self.seen_seq = 0                 # 处理到第几条用户发言（运行中插话时补算信号）
        self.lock = threading.RLock()
        self.version = 0                  # 每加载一组加一（内核据此知道工具清单变了）
        self._load_tool = None
        if self.lazy:
            self._initial()

    # ---- 运行开始时 ----
    def _initial(self):
        for g in MODE_DEFAULT.get(self.mode, MODE_DEFAULT["cariai"]):
            self._add(g, "mode")
        self._add("core", "mode")
        for g in self.persisted():
            self._add(g, "used")
        self.refresh(initial=True)

    def persisted(self):
        s = self.store.session(self.sid) if self.sid else None
        v = ((s or {}).get("meta") or {}).get(META_KEY)
        return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []

    def mcp_names(self):
        return [g[len(MCP_PREFIX):] for g in self.groups if g.startswith(MCP_PREFIX)]

    def turn_messages(self, initial=False):
        """要算信号的用户发言：运行开始时取"这一轮"的（最后一条最终回答之后的全部发言）；之后只取新来的。"""
        evs = self.store.events(self.sid, types=("user_message", "assistant_message")) if self.sid else []
        if initial:
            start = 0
            for e in evs:
                if e["type"] == "assistant_message" and not (e["payload"] or {}).get("tool_calls"):
                    start = e["seq"]
            return [e for e in evs if e["type"] == "user_message" and e["seq"] > start]
        return [e for e in evs if e["type"] == "user_message" and e["seq"] > self.seen_seq]

    def refresh(self, initial=False):
        """把还没算过的用户发言的信号加进来。返回 True 表示来了新的用户发言（新的一轮：重复调用检测要清零）。"""
        if not self.sid:
            return False
        with self.lock:
            new = self.turn_messages(initial)
            if not new:
                if initial:
                    last = self.store.events(self.sid, types=("user_message",))
                    self.seen_seq = last[-1]["seq"] if last else 0
                return False
            self.seen_seq = max(self.seen_seq, max(e["seq"] for e in new))
            if self.lazy:
                for e in new:
                    p = e["payload"] or {}
                    for g, w in signals(p.get("text") or "", p.get("files") or [], self.mcp_names()):
                        self._add(g, "signal:" + w)
                    if p.get("skill"):
                        for g in skill_groups(self.rt, p["skill"]):
                            self._add(g, "skill:" + p["skill"])
                    if p.get("depth") == "deep":
                        self._add("kb", "deep")         # 深入研究照技能 kb-research 做
            return True

    def _add(self, g, why):
        """带上一组（这次能用的工具里没有这一组就忽略）。返回 True 表示新加的。"""
        if g not in self.groups or g in self.loaded:
            return False
        self.loaded.append(g)
        self.why[g] = why
        self.version += 1
        return True

    # ---- 给模型看的 ----
    def unloaded(self):
        if not self.lazy:
            return []
        return [g for g in self.groups if g not in self.loaded]

    def visible(self):
        """这一次模型调用给模型看的工具（按登记顺序，保证同样的组合得到同样的清单）。"""
        if not self.lazy:
            return list(self.all.values())
        out = [t for n, t in self.all.items() if group_of(t) in self.loaded]
        if self.unloaded():
            out.append(self.load_tool())
        return out

    def visible_names(self):
        return [t.name for t in self.visible()]

    def specs(self):
        return [t.spec() for t in self.visible()]

    def catalog_lines(self):
        out = []
        for g in self.unloaded():
            info = group_info(g, len(self.groups[g]))
            out.append("- %s：%s" % (g, info["desc"]))
        return out

    def load_tool(self):
        """tool_load 的定义：说明里列出这次没带的组，每组一行。没带的组变了就重建。"""
        from .tools import Tool, S
        desc = _tool_load_desc(self.catalog_lines())
        t = self._load_tool
        if t is None or t.desc != desc:
            t = Tool(TOOL_LOAD, desc, {"group": S("组名，例如 browser")}, ["group"], _tool_load_handler, "none", False, None)
            t.display = _tool_load_display
            self._load_tool = t
        return t

    # ---- 中途加载 ----
    def load(self, g, why):
        """加载一组并记进 sessions.meta.toolset。返回这一组的工具名列表；没有这一组返回 None。"""
        with self.lock:
            if g not in self.groups:
                return None
            self._add(g, why)
            self.remember(g)
            return list(self.groups[g])

    def resolve_group(self, value):
        """tool_load 的参数：组 id、组名（"操作网页"）、或其中某个工具的名字都认。"""
        v = str(value or "").strip()
        if not v:
            return None
        if v in self.groups:
            return v
        if MCP_PREFIX + v in self.groups:
            return MCP_PREFIX + v
        for g in self.groups:
            info = group_info(g, len(self.groups[g]))
            if v in (info["name"], info["desc"]):
                return g
        if v in self.all:
            return group_of(self.all[v])
        return None

    def ensure(self, name):
        """模型调用了一个工具：它存在、模式和功能开关都允许（在 available() 里）→ 没带就加载它所在的组，并记下用过。
        返回这个工具；不存在返回 None（照旧回"没有这个工具"）。"""
        t = self.all.get(name)
        if t is None:
            return None
        g = group_of(t)
        with self.lock:
            if self.lazy and g not in self.loaded:
                self._add(g, "auto")
            self.remember(g)
        return t

    def remember(self, g):
        """把组记进 sessions.meta.toolset（用过的组以后一直带着）。core 和模式默认的组不用记。"""
        if not self.sid or g == "core" or g in MODE_DEFAULT.get(self.mode, ()):
            return
        rt = self.rt
        with rt.cv:
            s = self.store.session(self.sid)
            if not s:
                return
            meta = s["meta"] or {}
            cur = meta.get(META_KEY) if isinstance(meta.get(META_KEY), list) else []
            if g in cur:
                return
            meta[META_KEY] = cur + [g]
            self.store.set_session(self.sid, meta=meta)

    def after_skill_load(self, name):
        """模型用 skill_load 读了一个技能：带上这个技能要用的组（和点名技能一样，不记进 meta）。"""
        if not self.lazy:
            return []
        added = []
        with self.lock:
            for g in skill_groups(self.rt, name):
                if self._add(g, "skill:" + str(name)):
                    added.append(g)
        return added

    # ---- 状态 ----
    def chars(self):
        return spec_chars([t.spec() for t in self.visible()])

    def chars_all(self):
        return spec_chars([t.spec() for t in self.all.values()])

    def status(self):
        """GET /api/sessions/<id>/status 的 toolset。"""
        groups = []
        for g, names in self.groups.items():
            info = group_info(g, len(names))
            info.update({"tools": list(names), "loaded": (g in self.loaded) if self.lazy else True,
                         "why": self.why.get(g, "") if self.lazy else "",
                         "chars": spec_chars([self.all[n].spec() for n in names])})
            groups.append(info)
        return {"lazy": self.lazy, "loaded": list(self.loaded) if self.lazy else list(self.groups),
                "unloaded": self.unloaded(), "chars": self.chars(), "chars_all": self.chars_all(), "groups": groups}


def spec_chars(specs):
    """工具定义的字符数：每个定义按 JSON（不转义中文）算，加起来（规格 3.6 的 12,941 就是这么量的）。"""
    return sum(len(json.dumps(s, ensure_ascii=False)) for s in specs or [])


def _runner_toolset(ctx):
    return getattr(getattr(ctx, "runner", None), "toolset", None)


def _tool_load_handler(ctx, group):
    from .tools import ToolError
    ts = _runner_toolset(ctx)
    if ts is None or not ts.lazy:
        return "现在所有工具都已经带着，不用加载。"
    g = ts.resolve_group(group)
    if g is None:
        lines = ts.catalog_lines()
        raise ToolError("没有这个组：%s。%s" % (group, ("可以加载的组：\n" + "\n".join(lines)) if lines else "所有组都已经带着了。"))
    if g in ts.loaded:
        names = ts.groups[g]
        return "已经带着：%s（%s），直接调用即可。" % (group_info(g, len(names))["name"], "、".join(names))
    names = ts.load(g, "tool_load")
    return "已加载：%s（%s）" % (group_info(g, len(names))["name"], "、".join(names))


def _tool_load_display(ctx, args):
    """时间线上显示成一步"加载工具：操作网页"。"""
    ts = _runner_toolset(ctx)
    raw = (args or {}).get("group")
    g = ts.resolve_group(raw) if ts is not None else None
    name = group_info(g, len((ts.groups.get(g) if ts else None) or []))["name"] if g else str(raw or "")
    return {"kind": "tool_load", "title": "加载工具：%s" % name, "group": g or str(raw or "")}


# ---------------- /api/toolsets 和 /状态 ----------------

def catalog(rt, mcp=None):
    """GET /api/toolsets：[{id, name, desc, tools}]。连接器按配置列出（tools 是已经连上的那些，没连上时为空）。"""
    from .tools import REGISTRY
    out = []
    for g in GROUPS:
        names = [n for n in g["tools"] if n in REGISTRY or n == TOOL_LOAD]
        out.append({"id": g["id"], "name": g["name"], "desc": g["desc"], "tools": names})
    mcp = mcp or {}
    for srv in (rt.cfg.get("mcp_servers") or {}):
        names = list(mcp.get(srv) or [])
        info = group_info(MCP_PREFIX + srv, len(names))
        info["tools"] = names
        out.append(info)
    return out


# ---------------- 重复调用检测（规格 3.8） ----------------

GUARD_TEXT = "这个调用和前两次完全一样，结果也一样，不再重复执行。换一种做法：改参数、换工具，或者把现在知道的告诉用户。"
GUARD_STOP_TEXT = "同一个调用拦下之后又发了一次，先停下来。看一下进展，需要的话点继续。"
GUARD_REASON = "重复调用"
# 不算的：提问要等人；计划清单没有副作用（新内核里 write_todos 走官方中间件，也不经过这里）；
# browser_do 有自己更细的检测（同一个动作连做 3 次、页面没有变化才拦，agent/tools/browser.py 的 LOOP_TEXT）
GUARD_EXEMPT = ("ask_user", "todo_write", "write_todos", "browser_do")


def _norm(v):
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, dict):
        return {str(k): _norm(x) for k, x in v.items() if x is not None}
    if isinstance(v, (list, tuple)):
        return [_norm(x) for x in v]
    return v


def call_key(name, args):
    """工具名 + 规范化后的参数（键排序、去掉值为 null 的键、字符串去首尾空白）。"""
    try:
        a = json.dumps(_norm(args if isinstance(args, dict) else {}), ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        a = str(args)
    return "%s %s" % (name, a)


def result_digest(ok, content):
    return hashlib.sha1(("%s|%s" % (bool(ok), content or "")).encode("utf-8", "replace")).hexdigest()[:16]


class LoopGuard:
    """一次运行里最近的调用：(调用键, 结果摘要)。只记真正执行过的调用；被拦下的不记。"""

    def __init__(self, limit=3):
        self.limit = int(limit or 0)
        self.hist = []
        self.blocked = {}          # 调用键 → 被拦过的调用 id

    def reset(self):
        self.hist = []
        self.blocked = {}

    def check(self, name, args):
        """这次调用会不会是第 limit 次重复。是就返回 limit（事件里的 count），否则 0。不改状态，可以重复调用。"""
        n = self.limit
        if n < 2 or name in GUARD_EXEMPT:
            return 0
        k, h = call_key(name, args), self.hist
        # 同一个调用连续 n-1 次、结果都一样
        tail = h[-(n - 1):]
        if len(tail) == n - 1 and all(x[0] == k for x in tail) and len({x[1] for x in tail}) == 1:
            return n
        # 两个调用来回交替（甲乙甲乙……），各自的结果都不变：最近 2(n-1) 次里两个都各出现了 n-1 次
        m = 2 * (n - 1)
        w = h[-m:]
        if len(w) == m:
            alt = all(w[i][0] != w[i + 1][0] for i in range(m - 1)) and all(w[i][0] == w[i + 2][0] for i in range(m - 2))
            if alt and k in (w[0][0], w[1][0]) and len({x[1] for x in w[0::2]}) == 1 and len({x[1] for x in w[1::2]}) == 1:
                return n
        return 0

    def record(self, name, args, ok, content):
        if name in GUARD_EXEMPT:
            return
        self.hist.append((call_key(name, args), result_digest(ok, content)))
        if len(self.hist) > 50:
            self.hist = self.hist[-50:]

    def block(self, cid, name, args):
        """记下这次拦截。返回 True 表示这个调用之前已经被拦过一次（又发了同样的调用：该停下来问用户）。"""
        ids = self.blocked.setdefault(call_key(name, args), [])
        again = any(x != cid for x in ids)
        if cid not in ids:
            ids.append(cid)
        return again
