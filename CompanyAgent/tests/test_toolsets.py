"""工具按需加载和重复调用检测（docs/借鉴开源项目.md 第 3 节），过渡内核。新内核的对应测试在 test_lg_toolsets.py。

规格 3.6 的三个数字在 Numbers 里量：全带时的总字符数、两个模式默认各多少（目标 ≤ 7,000）、tool_load 自己（目标 ≤ 500）。"""
from __future__ import annotations
import importlib.util, json, os, sys, unittest
from types import SimpleNamespace
from helpers import Case, ApiCase, HERE, wait_until
from agent import toolsets
from agent.toolsets import Toolset, LoopGuard, signals, spec_chars, GUARD_TEXT, TOOL_LOAD
from agent.tools import REGISTRY, load_all
from agent.loop import Runner

load_all()
HAS_PW = importlib.util.find_spec("playwright") is not None
FAKE_MCP = [sys.executable, os.path.join(HERE, "fake_mcp_server.py")]


def tc(*calls, content=""):
    return {"content": content, "tool_calls": list(calls)}


def names(tools):
    return [t["function"]["name"] for t in tools or []]


def capture(seen, reply):
    """假模型的一步：记下这次看到的工具名，再按 reply 回。"""
    def fn(messages, tools):
        seen.append(names(tools))
        return reply
    return fn


class Groups(unittest.TestCase):
    def test_every_tool_in_one_group(self):
        ids = [g["id"] for g in toolsets.GROUPS]
        self.assertEqual(len(ids), len(set(ids)))
        listed = [n for g in toolsets.GROUPS for n in g["tools"]]
        self.assertEqual(len(listed), len(set(listed)))           # 一个工具只在一组
        missing = [n for n in REGISTRY if n not in toolsets.TOOL_GROUP]
        self.assertEqual(missing, [], "这些工具没写进 agent/toolsets.py 的分组")
        for mode, gs in toolsets.MODE_DEFAULT.items():
            self.assertIn("core", gs)
            self.assertTrue(all(g in toolsets.GROUP_BY_ID for g in gs))
        for sk, gs in toolsets.SKILL_GROUPS.items():
            self.assertTrue(all(g in toolsets.GROUP_BY_ID for g in gs), sk)

    def test_mcp_group(self):
        from agent.tools import Tool
        t = Tool("mcp_crm_get", "[连接器 crm] 查客户", {}, [], None, "external", False, None)
        self.assertEqual(toolsets.group_of(t), "mcp:crm")
        self.assertEqual(toolsets.group_info("mcp:crm", 3), {"id": "mcp:crm", "name": "连接器 crm", "desc": "连接器 crm（3 个工具）"})
        self.assertEqual(toolsets.group_of("browser_open"), "browser")


def _all_tools_toolset(mode):
    """全部 36 个工具都能用时（最坏情况：tool_load 的说明里列的组最多）。"""
    rt = SimpleNamespace(store=None, cfg=None, skills=None, cv=None)
    return Toolset(rt, None, mode, list(REGISTRY.values()), lazy=True)


class Numbers(Case):
    """规格 3.6：量出来的数字写进测试。"""

    def test_three_numbers(self):
        total = spec_chars([t.spec() for t in REGISTRY.values()])
        self.assertGreater(total, 12000)       # 规格写的是 12,941；"后端·资料"给 kb_* 加了参数后会多一点
        for mode in ("cariai", "hspace"):
            ts = _all_tools_toolset(mode)
            self.assertEqual(ts.chars_all(), total)
            # CariAi 守 7,000；Hspace 放到 7,500：memory 工具挪回了常驻（见 agent/toolsets.py 里的说明）
            cap = 7000 if mode == "cariai" else 7500
            self.assertLessEqual(ts.chars(), cap, "%s 默认带的工具定义超过 %d 字符：%d" % (mode, cap, ts.chars()))
            tl = spec_chars([ts.load_tool().spec()])
            self.assertLessEqual(tl, 500, "tool_load 的定义超过 500 字符：%d" % tl)
            self.assertIn(TOOL_LOAD, ts.visible_names())
            # 默认带的组：CariAi 是 core + files + excel + docs；Hspace 是 core + kb + excel + docs
            self.assertEqual(sorted(ts.loaded), sorted(toolsets.MODE_DEFAULT[mode]))

    def test_in_runtime(self):
        e = self.env([])
        for mode in ("cariai", "hspace"):
            sid = e.rt.store.add_session(e.ws["id"], mode=mode)["id"]
            r = Runner(e.rt, sid, e.mock)
            self.assertTrue(r.toolset.lazy)                    # 测试环境是 32K 窗口，auto → 开
            self.assertLessEqual(spec_chars(r.specs), 7000 if mode == "cariai" else 7500)
            self.assertLess(spec_chars(r.specs), r.toolset.chars_all())
            self.assertEqual(set(r.tools) - {TOOL_LOAD}, {t.name for t in r.toolset.all.values()})   # 校验、执行按全部工具


class Lazy(Case):
    def test_setting(self):
        e = self.env([])
        sid = e.new_session()
        self.assertEqual(toolsets.lazy_setting(e.cfg), "auto")
        self.assertTrue(toolsets.lazy_on(e.rt))                            # 32K < 64K
        e.cfg.update({"model": {"context_window": 131072}})
        self.assertFalse(toolsets.lazy_on(e.rt))                           # 128K：auto 不开
        r = Runner(e.rt, sid, e.mock)
        self.assertFalse(r.toolset.lazy)
        self.assertNotIn(TOOL_LOAD, names(r.specs))
        self.assertEqual(len(r.specs), len(r.toolset.all))
        e.cfg.update({"tools": {"lazy": "on"}})
        self.assertTrue(toolsets.lazy_on(e.rt))
        e.cfg.update({"tools": {"lazy": "off"}, "model": {"context_window": 32768}})
        self.assertFalse(toolsets.lazy_on(e.rt))
        e.cfg.update({"model": {"context_window": 32768}, "endpoints": [{"name": "A", "base_url": "http://127.0.0.1:9/v1", "model": "m",
                                                                          "context_window": 200000}],
                      "tools": {"lazy": "auto"}})
        self.assertFalse(toolsets.lazy_on(e.rt))                           # 问 capability：用端点自己的 context_window

    def test_off_gives_everything(self):
        seen = []
        e = self.env([capture(seen, {"content": "好"})], tools={"lazy": "off"})
        sid = e.start("你好")
        self.assertTrue(e.rt.wait_idle(sid, 5))
        self.assertNotIn(TOOL_LOAD, seen[0])
        self.assertIn("browser_open", seen[0]) if REGISTRY["browser_open"].enabled(SimpleNamespace(rt=e.rt)) else None
        self.assertIn("run_python", seen[0])
        self.assertIn("memory", seen[0])

    def test_sub_agent_unchanged(self):
        e = self.env([])
        sid = e.new_session()
        sub = e.rt.store.add_session(e.ws["id"], kind="sub", parent_id=sid)["id"]
        r = Runner(e.rt, sub, e.mock, sub=True)
        self.assertFalse(r.toolset.lazy)
        self.assertEqual(sorted(names(r.specs)), sorted(n for n, t in REGISTRY.items() if t.sub_ok and n in r.tools))


class Signals(unittest.TestCase):
    """规格 3.3 第 3 条：每条信号规则一个例子（笔记里列的就是这些）。"""

    def groups(self, text, files=(), mcp=()):
        return [g for g, _ in signals(text, files, mcp)]

    def test_rules(self):
        self.assertIn("excel", self.groups("看一下 销售.xlsx里的数"))         # 扩展名后面紧跟中文也认
        self.assertIn("excel", self.groups("把附件汇总一下", files=["上传/明细.csv"]))
        self.assertIn("docs", self.groups("改一下 方案.docx"))
        self.assertIn("docs", self.groups("做一份 PPT"))
        self.assertIn("browser", self.groups("打开网页 https://oa.example.com/login"))
        self.assertIn("browser", self.groups("去 oa.example.com 看看"))
        self.assertIn("browser", self.groups("打开公司官网"))
        self.assertIn("browser", self.groups("帮我在报销系统里填表"))
        self.assertIn("kb", self.groups("差旅制度里住宿标准是多少"))
        self.assertIn("kb", self.groups("报销的 sop 是什么"))
        self.assertIn("kb_add", self.groups("先把“制度”文件夹导入资料库"))
        self.assertIn("auto", self.groups("每周一早上提醒我交周报"))
        self.assertIn("web", self.groups("上网查一下最新的汇率"))
        self.assertIn("code", self.groups("算一下平均客单价"))
        self.assertIn("code", self.groups("用 python 画图"))
        self.assertIn("analysis", self.groups("两张表对账"))
        self.assertIn("memory", self.groups("记住：报告先写结论"))
        self.assertIn("memory", self.groups("上次那份报价单发给谁了"))
        self.assertIn("files", self.groups("把旧文件挪到归档文件夹"))
        self.assertIn("desktop", self.groups("帮我操作软件导出数据"))
        self.assertEqual(self.groups("工单查一下", mcp=["demo"]), [])
        self.assertIn("mcp:demo", self.groups("用 demo 查一下工单", mcp=["demo"]))
        self.assertIn("mcp:demo", self.groups("用连接器查", mcp=["demo"]))
        self.assertEqual(self.groups("你好，今天怎么样"), [])
        self.assertNotIn("excel", self.groups("文件名是 a.xlsxx"))             # 扩展名后面跟字母不算

    def test_skill_groups(self):
        skills = SimpleNamespace(get=lambda n: {"tools": ["browser", "run_python"]} if n == "自定义" else None)
        rt = SimpleNamespace(skills=skills)
        self.assertEqual(toolsets.skill_groups(rt, "data-analysis"), ["excel", "analysis", "code"])
        self.assertEqual(toolsets.skill_groups(rt, "自定义"), ["browser", "code"])      # 技能自己写了 tools: 以它为准
        self.assertEqual(toolsets.skill_groups(rt, "没有的"), [])
        self.assertEqual(toolsets.parse_tools_meta("[excel, code]"), ["excel", "code"])
        self.assertEqual(toolsets.parse_tools_meta("excel_query，run_python"), ["excel_query", "run_python"])
        self.assertIsNone(toolsets.parse_tools_meta(""))


class InRun(Case):
    def test_signal_and_skill_at_start(self):
        seen = []
        e = self.env([capture(seen, {"content": "好"})])
        sid = e.new_session()
        e.rt.send_message(sid, "每天八点算一下 销售.xlsx 的合计", skill="slides")
        self.assertTrue(e.rt.wait_idle(sid, 5))
        for n in ("schedule_create", "run_python", "excel_read", "write_pptx"):
            self.assertIn(n, seen[0])
        self.assertNotIn("browser_open", seen[0])
        self.assertNotIn("kb_search", seen[0])
        self.assertIn(TOOL_LOAD, seen[0])
        self.assertEqual(e.rt.store.session(sid)["meta"].get("toolset"), None)      # 信号带上的不记，用过才记

    def test_direct_call_autoloads_and_remembers(self):
        """模型直接调用了没带的工具：加载它所在的组并照常执行，不回"没有这个工具"。"""
        seen = []
        e = self.env([capture(seen, tc(("session_search", {"query": "周报"}))), capture(seen, {"content": "查了"}),
                      capture(seen, {"content": "又好了"})])
        sid = e.start("帮我看看")
        self.assertTrue(e.rt.wait_idle(sid, 5))
        self.assertEqual(e.status(sid), "done")
        self.assertNotIn("session_search", seen[0])
        r = e.results(sid)[0]
        self.assertTrue(r["ok"], r)
        self.assertNotIn("没有这个工具", r["content"])
        self.assertIn("session_search", seen[1])                    # 从下一次调用起带上
        self.assertIn("memory", seen[1])                            # 整组一起
        self.assertEqual(e.rt.store.session(sid)["meta"]["toolset"], ["memory"])
        e.rt.send_message(sid, "再来")                              # 下一次运行：用过的组一直带着
        self.assertTrue(e.rt.wait_idle(sid, 5))
        self.assertIn("session_search", seen[2])

    def test_unknown_tool_still_unknown(self):
        e = self.env([tc(("no_such_tool", {})), {"content": "好"}])
        sid = e.start("x")
        self.assertTrue(e.rt.wait_idle(sid, 5))
        self.assertIn("没有这个工具：no_such_tool", e.results(sid)[0]["content"])

    def test_denied_tool_not_autoloaded(self):
        e = self.env([tc(("run_python", {"code": "print(1)"})), {"content": "好"}])
        e.rt.store.set_tool_setting("run_python", "deny")
        sid = e.start("x")
        self.assertTrue(e.rt.wait_idle(sid, 5))
        self.assertIn("没有这个工具：run_python", e.results(sid)[0]["content"])      # 禁用的照旧不给

    def test_tool_load(self):
        seen = []
        e = self.env([capture(seen, tc(("tool_load", {"group": "code"}))), capture(seen, tc(("tool_load", {"group": "飞行"}))),
                      capture(seen, {"content": "好"})], mode="manual")
        sid = e.start("x")
        self.assertTrue(e.rt.wait_idle(sid, 5))
        self.assertEqual(e.status(sid), "done")                     # 风险"无"：每步都问的模式下也不问
        self.assertFalse(e.events(sid, ("approval_requested",)))
        r = e.results(sid)
        self.assertEqual(r[0]["content"], "已加载：运行代码（run_python）")
        self.assertFalse(r[1]["ok"])
        self.assertIn("没有这个组：飞行", r[1]["content"])
        self.assertNotIn("run_python", seen[0])
        self.assertIn("run_python", seen[1])
        st = [x["payload"] for x in e.events(sid, ("tool_started",))][0]
        self.assertEqual(st["display"], {"kind": "tool_load", "title": "加载工具：运行代码", "group": "code"})
        self.assertEqual(e.rt.store.session(sid)["meta"]["toolset"], ["code"])
        # tool_load 的说明里只列没带的组
        call0 = e.mock.calls[0]
        self.assertTrue(call0)

    def test_tool_load_desc_lists_unloaded(self):
        e = self.env([])
        sid = e.new_session()
        r = Runner(e.rt, sid, e.mock)
        d = r.toolset.load_tool().desc
        self.assertIn("- browser：打开和操作网页", d) if "browser" in r.toolset.groups else None
        self.assertIn("- code：运行 Python 做分析", d)
        self.assertNotIn("- excel：", d)
        for g in list(r.toolset.unloaded()):
            r.toolset.load(g, "tool_load")
        self.assertNotIn(TOOL_LOAD, names(r.specs))                     # 全带上了就不给
        self.assertEqual(spec_chars(r.specs), r.toolset.chars_all())

    def test_kick_adds_signals(self):
        e = self.env([])
        sid = e.new_session()
        e.rt.emit(sid, "user_message", {"text": "你好"})
        r = Runner(e.rt, sid, e.mock)
        self.assertNotIn("browser", r.toolset.loaded)
        e.rt.emit(sid, "user_message", {"text": "打开 https://a.example.com"})
        r.guard.record("list_dir", {"path": "."}, True, "x")
        r._new_turn()
        if "browser" in r.toolset.groups:
            self.assertIn("browser", r.toolset.loaded)
        self.assertEqual(r.guard.hist, [])                               # 新的一轮：重复调用检测清零

    def test_skill_load_brings_groups(self):
        seen = []
        e = self.env([capture(seen, tc(("skill_load", {"name": "data-analysis"}))), capture(seen, {"content": "好"})])
        sid = e.start("x")
        self.assertTrue(e.rt.wait_idle(sid, 5))
        self.assertNotIn("run_python", seen[0])
        self.assertIn("run_python", seen[1])
        self.assertIn("excel_query", seen[1])

    def test_mcp_lazy(self):
        seen = []
        e = self.env([capture(seen, tc(("mcp_demo_echo", {"text": "查工单"}))), capture(seen, {"content": "好"}),
                      capture(seen, {"content": "又好"})], mcp_servers={"demo": {"command": FAKE_MCP}}, mode="full")
        sid = e.start("查一下工单")
        self.assertTrue(e.rt.wait_idle(sid, 15))
        self.assertNotIn("mcp_demo_echo", seen[0])                        # 连接器单独一组，默认不带
        self.assertEqual(e.results(sid)[0]["content"], "回声：查工单")      # 直接调用：加载并执行
        self.assertIn("mcp_demo_add", seen[1])
        self.assertEqual(e.rt.store.session(sid)["meta"]["toolset"], ["mcp:demo"])
        sid2 = e.start("用 demo 查")                                       # 话里有连接器的名字：一开始就带上
        self.assertTrue(e.rt.wait_idle(sid2, 15))
        self.assertIn("mcp_demo_echo", seen[2])


class Guard(unittest.TestCase):
    def test_same_call_same_result(self):
        g = LoopGuard(3)
        a = {"path": "."}
        self.assertEqual(g.check("list_dir", a), 0)
        g.record("list_dir", a, True, "x")
        self.assertEqual(g.check("list_dir", {"path": " . "}), 0)
        g.record("list_dir", {"path": " . "}, True, "x")                 # 规范化：去首尾空白
        self.assertEqual(g.check("list_dir", {"path": "."}), 3)
        self.assertFalse(g.block("c1", "list_dir", a))
        self.assertFalse(g.block("c1", "list_dir", a))                    # 同一个调用（重跑）不算又发了一次
        self.assertTrue(g.block("c2", "list_dir", a))

    def test_result_changed_not_repeat(self):
        g = LoopGuard(3)
        g.record("kb_open", {"ref": "1", "more": True}, True, "第一段")
        g.record("kb_open", {"ref": "1", "more": True}, True, "第二段")
        self.assertEqual(g.check("kb_open", {"more": True, "ref": "1"}), 0)

    def test_alternating(self):
        g = LoopGuard(3)
        for _ in range(2):
            g.record("a", {}, True, "1")
            g.record("b", {"x": 1}, True, "2")
        self.assertEqual(g.check("a", {}), 3)
        self.assertEqual(g.check("b", {"x": 1}), 3)
        self.assertEqual(g.check("c", {}), 0)
        g2 = LoopGuard(3)
        for r in ("1", "2"):
            g2.record("a", {}, True, r)
            g2.record("b", {}, True, "2")
        self.assertEqual(g2.check("a", {}), 0)                            # 甲的结果变了

    def test_off_and_exempt(self):
        g = LoopGuard(0)
        for _ in range(5):
            g.record("x", {}, True, "1")
        self.assertEqual(g.check("x", {}), 0)
        g = LoopGuard(3)
        for _ in range(3):
            g.record("ask_user", {"question": "?"}, True, "1")
        self.assertEqual(g.check("ask_user", {"question": "?"}), 0)


class GuardInRun(Case):
    def test_block_then_stop(self):
        same = tc(("list_dir", {"path": "."}))
        e = self.env([same, same, same, same, {"content": "好"}])
        sid = e.start("看看")
        self.assertTrue(e.rt.wait_idle(sid, 5))
        self.assertEqual(e.status(sid), "paused")
        self.assertEqual(e.rt.store.session(sid)["meta"].get("reason"), "重复调用")
        r = e.results(sid)
        self.assertTrue(r[0]["ok"] and r[1]["ok"])
        self.assertEqual((r[2]["ok"], r[2]["content"]), (False, GUARD_TEXT))
        self.assertEqual((r[3]["ok"], r[3]["content"]), (False, GUARD_TEXT))
        lg = [x["payload"] for x in e.events(sid, ("loop_guard",))]
        calls = [c["id"] for m in e.events(sid, ("assistant_message",)) for c in m["payload"]["tool_calls"]]
        self.assertEqual(lg, [{"tool": "list_dir", "count": 3, "call_id": calls[2]}, {"tool": "list_dir", "count": 3, "call_id": calls[3]}])
        notices = [x["payload"]["text"] for x in e.events(sid, ("notice",))]
        self.assertIn(toolsets.GUARD_STOP_TEXT, notices)
        self.assertEqual(len([x for x in e.events(sid, ("tool_started",)) if x["payload"]["tool"] == "list_dir"]), 2)   # 被拦的没执行
        # 被拦下时不弹批准卡片（覆盖文件也一样）
        types = e.types(sid)
        self.assertNotIn("approval_requested", types)
        # 用户点继续：接着问模型，模型看到被拦下的结果
        e.rt.control(sid, "resume")
        self.assertTrue(e.rt.wait_idle(sid, 5))
        self.assertEqual(e.status(sid), "done")
        self.assertIn(GUARD_TEXT, json.dumps(e.mock.calls[-1], ensure_ascii=False))

    def test_changing_results_pass(self):
        e = self.env([tc(("read_file", {"path": "a.txt"})), tc(("write_file", {"path": "a.txt", "content": "2"})),
                      tc(("read_file", {"path": "a.txt"})), tc(("read_file", {"path": "a.txt"})), {"content": "好"}], mode="full")
        e.write("a.txt", "1")
        sid = e.start("x")
        self.assertTrue(e.rt.wait_idle(sid, 5))
        self.assertEqual(e.status(sid), "done")
        self.assertFalse(e.events(sid, ("loop_guard",)))                # 中间结果变了，不算连续重复

    def test_off(self):
        same = tc(("list_dir", {"path": "."}))
        e = self.env([same] * 5 + [{"content": "好"}], tools={"loop_guard": 0})
        sid = e.start("x")
        self.assertTrue(e.rt.wait_idle(sid, 5))
        self.assertEqual(e.status(sid), "done")
        self.assertFalse(e.events(sid, ("loop_guard",)))

    def test_new_turn_resets(self):
        same = tc(("list_dir", {"path": "."}))
        e = self.env([same, same, {"content": "好"}, same, {"content": "又好"}])
        sid = e.start("x")
        self.assertTrue(e.rt.wait_idle(sid, 5))
        e.rt.send_message(sid, "再看一次")
        self.assertTrue(e.rt.wait_idle(sid, 5))
        self.assertEqual(e.status(sid), "done")
        self.assertFalse(e.events(sid, ("loop_guard",)))                # 新的一轮，重新数


class Api(ApiCase):
    def test_status_toolsets_settings(self):
        wid = self.e.ws["id"]
        st, s = self.req("POST", "/api/sessions", {"workspace_id": wid, "mode": "hspace"})
        st, b = self.req("GET", "/api/sessions/%s/status" % s["id"])
        self.assertEqual(st, 200)
        ts = b["toolset"]
        self.assertEqual(set(ts), {"lazy", "loaded", "unloaded", "chars", "chars_all", "groups"})
        self.assertTrue(ts["lazy"])
        self.assertEqual(sorted(ts["loaded"]), sorted(toolsets.MODE_DEFAULT["hspace"]))
        self.assertIn("code", ts["unloaded"])
        self.assertLessEqual(ts["chars"], 7500)      # Hspace：见 Numbers 里的说明
        self.assertGreater(ts["chars_all"], ts["chars"])
        g = {x["id"]: x for x in ts["groups"]}
        self.assertEqual(set(g["kb"]), {"id", "name", "desc", "tools", "loaded", "why", "chars"})
        self.assertEqual((g["kb"]["loaded"], g["kb"]["why"]), (True, "mode"))
        # context 的估算按这次带的工具算
        self.assertLess(b["context"]["used"], 7000)
        st, lst = self.req("GET", "/api/toolsets")
        self.assertEqual(st, 200)
        self.assertEqual([x["id"] for x in lst][:3], ["core", "files", "excel"])
        self.assertTrue(all(set(x) == {"id", "name", "desc", "tools"} for x in lst))
        self.assertIn(TOOL_LOAD, lst[0]["tools"])
        self.assertEqual(self.req("POST", "/api/settings", {"tools": {"lazy": "off", "loop_guard": 5}})[0], 200)
        self.assertEqual((self.e.cfg.get("tools", "lazy"), self.e.cfg.get("tools", "loop_guard")), ("off", 5))
        st, b = self.req("GET", "/api/sessions/%s/status" % s["id"])
        self.assertEqual((b["toolset"]["lazy"], b["toolset"]["unloaded"]), (False, []))
        self.assertEqual(b["toolset"]["chars"], b["toolset"]["chars_all"])
        for bad in ({"lazy": "maybe"}, {"loop_guard": -1}, {"loop_guard": 11}, {"loop_guard": True}, {"other": 1}, "on"):
            self.assertEqual(self.req("POST", "/api/settings", {"tools": bad})[0], 400, bad)


@unittest.skipUnless(HAS_PW, "没有安装 playwright")
class Demo(Case):
    """规格 3.10：演示模式下"打开网页 https://…"照样能走通（靠信号带上 browser 组）。"""

    def test_demo_open_page(self):
        from websites import Site
        from agent.llm import demo_script
        from agent import capability
        site = Site()
        try:
            e = self.env([demo_script] * 10, mode="full",
                         browser={"allow_local": True, "headless": True, "allowed_domains": ["127.0.0.1"]})
            if not capability.get(e.rt).ok("browser"):
                self.skipTest("这台电脑上操作网页不可用")
            sid = e.new_session()
            r = Runner(e.rt, sid, e.mock)
            self.assertTrue(r.toolset.lazy)
            self.assertNotIn("browser", r.toolset.loaded)                 # 默认不带
            e.rt.send_message(sid, "打开网页 %s 看看" % site.url("/form"))
            self.assertTrue(e.rt.wait_idle(sid, 60))
            self.assertEqual(e.status(sid), "done")
            self.assertEqual([x["payload"]["tool"] for x in e.events(sid, ("tool_started",))], ["browser_open", "browser_look"])
            self.assertIn("演示模式", e.events(sid, ("assistant_message",))[-1]["payload"]["content"])
            e.rt.close_browser()
        finally:
            site.close()


if __name__ == "__main__":
    unittest.main()
