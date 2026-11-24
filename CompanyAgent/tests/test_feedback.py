"""反馈和标准答案（docs/两种模式.md 9.5）：反馈的增改取消和校验、标准答案的增改删查和去重、检索时排最前且带标记、
限定范围时仍参与、内置库不能改删；走完整一轮（核对数字 → 点踩 → 记成标准答案 → 再问时第一条是标准答案）；
以及弱命中按没找到处理（主控 2026-11-21 提的检索问题）。"""
from __future__ import annotations
import os

from helpers import Case, ApiCase
from agent import kb as kbm, feedback, export
from agent.kb import retrieve
from agent.tools import REGISTRY
from agent.loop import Runner
from test_kb import make_docx, SOP, write

Q = "总监级在一线城市住宿每晚不超过多少钱？"


def call(name, args):
    return {"content": "", "tool_calls": [(name, args)]}


def build_lib(e, name="公司 SOP"):
    d = os.path.join(e.tmp, "SOP-" + name)
    os.makedirs(d)
    make_docx(os.path.join(d, SOP))
    lib = kbm.create_library(e.rt.store, name)
    kbm.add_source(e.rt.store, lib["id"], d)
    e.rt.kb.indexer.enqueue(lib["id"])
    assert e.rt.kb.indexer.wait_idle(20)
    return lib


class Api(ApiCase):
    env_kw = {"mode": "full"}
    script = [{"content": "每晚 800 元 [1]"}, {"content": "第二条回答"}]

    def setUp(self):
        super().setUp()
        self.lib = build_lib(self.e)
        st, s = self.req("POST", "/api/sessions", {"workspace_id": self.e.ws["id"], "mode": "hspace", "text": "住宿标准"})
        self.sid = s["id"]
        self.assertTrue(self.e.rt.wait_idle(self.sid, 10))
        self.ans = self.e.events(self.sid, ("assistant_message",))[-1]["seq"]

    def fb(self, body, sid=None):
        return self.req("POST", "/api/sessions/%s/feedback" % (sid or self.sid), body)

    def test_feedback(self):
        st, p = self.fb({"seq": self.ans, "rating": "up"})
        self.assertEqual((st, p), (200, {"seq": self.ans, "rating": "up", "note": ""}))
        self.fb({"seq": self.ans, "rating": "down", "note": "应该是 900 元"})
        self.assertEqual(feedback.current(self.e.rt.store, self.sid), {self.ans: {"rating": "down", "note": "应该是 900 元",
                                                                                   "ts": feedback.current(self.e.rt.store, self.sid)[self.ans]["ts"]}})
        evs = self.e.events(self.sid, ("feedback",))
        self.assertEqual([x["payload"] for x in evs][-1], {"seq": self.ans, "rating": "down", "note": "应该是 900 元"})
        # 导出带上反馈
        st, raw, _ = self.req("GET", "/api/sessions/%s/export" % self.sid, raw=True)
        self.assertIn("- 反馈：没用（应该是 900 元）", raw.decode("utf-8"))
        # 取消
        self.assertEqual(self.fb({"seq": self.ans, "rating": None})[0], 200)
        self.assertEqual(feedback.current(self.e.rt.store, self.sid), {})
        self.assertNotIn("- 反馈：", export.session_markdown(self.e.rt.store, self.sid))

    def test_feedback_validation(self):
        user_seq = self.e.events(self.sid, ("user_message",))[0]["seq"]
        for body in ({"seq": user_seq, "rating": "up"}, {"seq": 999999, "rating": "up"}, {"seq": str(self.ans), "rating": "up"},
                     {"seq": True, "rating": "up"}, {"seq": self.ans}, {"seq": self.ans, "rating": "good"},
                     {"seq": self.ans, "rating": "up", "note": "x" * 2001}, {"seq": self.ans, "rating": "up", "note": 3}):
            st, r = self.fb(body)
            self.assertEqual(st, 400, body)
            self.assertIn("error", r)
        # 别的对话的回答不行
        other = self.e.rt.store.add_session(self.e.ws["id"], mode="hspace")["id"]
        self.assertEqual(self.fb({"seq": self.ans, "rating": "up"}, other)[0], 400)
        self.assertEqual(self.fb({"seq": self.ans, "rating": "up"}, "nope")[0], 404)
        self.assertEqual(self.fb({"seq": self.ans, "rating": "up", "note": "x" * 2000})[0], 200)

    def test_answers_crud(self):
        self.assertEqual(self.req("GET", "/api/kb/answers"), (200, []))
        self.assertIsNone(kbm.library(self.e.rt.store, kbm.ANSWERS_ID))           # 用到时才建
        st, a = self.req("POST", "/api/kb/answers", {"question": "  报销多久到账？ ", "answer": "审批后 3 个工作日。"})
        self.assertEqual(st, 200)
        # hits、last_hit：docs/借鉴开源项目.md 4.2（标准答案被引用了几次）
        self.assertEqual(set(a), {"id", "question", "answer", "created", "updated", "hits", "last_hit"})
        self.assertEqual((a["question"], a["answer"]), ("报销多久到账？", "审批后 3 个工作日。"))
        # 规整后相同的问题：更新而不是新加
        st, b = self.req("POST", "/api/kb/answers", {"question": "报销多久到账?", "answer": "审批后 5 个工作日。"})
        self.assertEqual(b["id"], a["id"])
        st, b = self.req("POST", "/api/kb/answers", {"question": "ＢＩＧ 报销多久到账", "answer": "x"})
        st, c = self.req("POST", "/api/kb/answers", {"question": "big  报销多久到账？？", "answer": "y"})
        self.assertEqual(b["id"], c["id"])
        st, lst = self.req("GET", "/api/kb/answers")
        self.assertEqual(len(lst), 2)
        self.assertEqual(lst[0]["answer"], "审批后 5 个工作日。")
        # 改
        st, d = self.req("PATCH", "/api/kb/answers/%s" % a["id"], {"answer": "审批后 7 个工作日。"})
        self.assertEqual((st, d["answer"], d["question"]), (200, "审批后 7 个工作日。", "报销多久到账?"))
        self.assertEqual(self.req("PATCH", "/api/kb/answers/%s" % a["id"], {"question": "BIG 报销多久到账"})[0], 400)   # 和另一条重了
        self.assertEqual(self.req("PATCH", "/api/kb/answers/%s" % a["id"], {})[0], 400)
        self.assertEqual(self.req("PATCH", "/api/kb/answers/nope", {"answer": "x"})[0], 404)
        # 校验
        for body in ({"question": "", "answer": "x"}, {"question": "x", "answer": ""}, {"question": "x" * 301, "answer": "y"},
                     {"question": "x", "answer": "y" * 4001}, {"question": 1, "answer": "y"},
                     {"question": "x", "answer": "y", "session_id": self.sid, "seq": 999999},
                     {"question": "x", "answer": "y", "session_id": "nope", "seq": self.ans}):
            self.assertEqual(self.req("POST", "/api/kb/answers", body)[0], 400, body)
        # 删
        self.assertEqual(self.req("DELETE", "/api/kb/answers/%s" % c["id"]), (200, {"ok": True}))
        self.assertEqual(self.req("DELETE", "/api/kb/answers/%s" % c["id"])[0], 404)
        self.assertEqual(len(self.req("GET", "/api/kb/answers")[1]), 1)
        self.assertEqual(self.e.rt.store.one("SELECT COUNT(*) AS n FROM kb_parts WHERE library_id=?", (kbm.ANSWERS_ID,))["n"], 1)

    def test_builtin_library(self):
        self.req("POST", "/api/kb/answers", {"question": "报销多久到账？", "answer": "审批后 3 个工作日。"})
        st, libs = self.req("GET", "/api/libraries")
        a = [x for x in libs if x["id"] == kbm.ANSWERS_ID][0]
        self.assertEqual((a["name"], a["builtin"], a["sources"], a["docs"], a["parts"]), ("标准答案", True, [], 1, 1))
        self.assertFalse([x for x in libs if x["id"] != kbm.ANSWERS_ID][0]["builtin"])
        for m, path, body in (("PATCH", "", {"name": "别的"}), ("DELETE", "", None),
                              ("POST", "/sources", {"path": self.e.tmp})):
            st, r = self.req(m, "/api/libraries/%s%s" % (kbm.ANSWERS_ID, path), body)
            self.assertEqual(st, 400, (m, path))
            self.assertIn("内置", r["error"])
        # 收录线程扫它时只补向量，不会把标准答案当成"文件没了"删掉
        self.e.rt.kb.indexer.enqueue(kbm.ANSWERS_ID)
        self.assertTrue(self.e.rt.kb.indexer.wait_idle(10))
        self.assertEqual(len(self.req("GET", "/api/kb/answers")[1]), 1)
        # kb_list 不列标准答案
        ctx = Runner(self.e.rt, self.sid, self.e.mock).ctx
        files = REGISTRY["kb_list"].handler(ctx)["files"]
        self.assertFalse([f for f in files if f["name"].startswith("标准答案")])

    def test_search_first_and_marked_and_scope(self):
        st, a = self.req("POST", "/api/kb/answers", {"question": Q, "answer": "按 2026 版：每晚不超过 900 元（总裁办通知）。",
                                                     "session_id": self.sid, "seq": self.ans})
        # 顺手记了一条 down 的反馈
        self.assertEqual(feedback.current(self.e.rt.store, self.sid)[self.ans]["rating"], "down")
        self.assertEqual(self.req("GET", "/api/kb/answers")[1][0]["source"], {"session_id": self.sid, "seq": self.ans})
        # 已经有反馈时不再记
        self.fb({"seq": self.ans, "rating": "up"})
        self.req("POST", "/api/kb/answers", {"question": Q, "answer": "每晚不超过 900 元。", "session_id": self.sid, "seq": self.ans})
        self.assertEqual(feedback.current(self.e.rt.store, self.sid)[self.ans]["rating"], "up")
        # 检索测试接口：第一条是标准答案
        st, r = self.req("POST", "/api/kb/search", {"query": Q, "libraries": [self.lib["id"]]})
        self.assertEqual(r["hits"][0]["standard"], True)
        self.assertEqual(r["hits"][0]["kind"], "qa")
        self.assertIn("900", r["hits"][0]["text"])
        self.assertFalse(any(h["standard"] for h in r["hits"][1:]))
        # 对话限定了范围：标准答案仍然参与；给模型看的文字里标"（标准答案）"
        s2 = self.e.rt.store.add_session(self.e.ws["id"], mode="hspace", meta={"libraries": [self.lib["id"]]})["id"]
        ctx = Runner(self.e.rt, s2, self.e.mock).ctx
        res = REGISTRY["kb_search"].handler(ctx, query=Q)
        self.assertTrue(res.extra["hits"][0]["standard"])
        self.assertIn("[1] 《标准答案》· %s（标准答案）" % Q, res.text if hasattr(res, "text") else str(res))
        # 按文件类型筛选也不把它筛掉
        res = REGISTRY["kb_search"].handler(ctx, query=Q, kind="doc")
        self.assertTrue(res.extra["hits"][0]["standard"])
        # 问题对不上的：标准答案不排第一（同样受覆盖率门槛约束）
        res = REGISTRY["kb_search"].handler(ctx, query="超标怎么审批")
        self.assertFalse(any(h["standard"] for h in res.extra["hits"]))
        # 删掉之后就查不到了
        self.req("DELETE", "/api/kb/answers/%s" % a["id"])
        st, r = self.req("POST", "/api/kb/search", {"query": Q})
        self.assertFalse(any(h["standard"] for h in r["hits"]))


def cite_first(messages, tools):
    """假模型：引用最近一次检索结果的第一条。"""
    import re
    body = [m for m in messages if m.get("role") == "tool"][-1]["content"]
    n = re.search(r"^\[(\d+)\]", body, re.M).group(1)
    return {"content": "按标准答案，每晚不超过 900 元 [%s]。" % n}


class FullRound(Case):
    """MockLLM 脚本、过渡内核：问 → 回答带 [1] → grounding 对上；脚本答一个出处里没有的数 → missing；点踩记成标准答案 →
    再问 → 检索结果第一条是标准答案。"""

    def test_round(self):
        e = self.env([call("kb_search", {"query": Q}), {"content": "总监级在一线城市每晚不超过 800 元 [1]。"},
                      call("kb_search", {"query": "经理级住宿标准"}), {"content": "经理级在一线城市每晚不超过 650 元 [1]。"},
                      call("kb_search", {"query": Q}), cite_first], mode="full")
        build_lib(e)
        sid = e.rt.store.add_session(e.ws["id"], mode="hspace")["id"]
        e.rt.send_message(sid, Q)
        self.assertTrue(e.rt.wait_idle(sid, 10))
        gr = e.events(sid, ("grounding",))[-1]["payload"]
        self.assertEqual(gr["checks"], [{"n": 1, "numbers": ["800 元"], "missing": []}])
        self.assertTrue(gr["ok"])
        e.rt.send_message(sid, "经理级呢？")
        self.assertTrue(e.rt.wait_idle(sid, 10))
        gr = e.events(sid, ("grounding",))[-1]["payload"]
        self.assertEqual(gr["checks"][0]["missing"], ["650 元"])
        self.assertFalse(gr["ok"])
        bad = gr["msg_seq"]
        # 点踩、记成标准答案
        feedback.record(e.rt, sid, {"seq": bad, "rating": "down", "note": "应该是 500 元"})
        feedback.save_answer(e.rt, {"question": Q, "answer": "每晚不超过 900 元（2026 年 11 月起）。", "session_id": sid, "seq": bad})
        self.assertEqual(feedback.current(e.rt.store, sid)[bad]["note"], "应该是 500 元")     # 已有反馈，不覆盖
        e.rt.send_message(sid, Q)
        self.assertTrue(e.rt.wait_idle(sid, 10))
        res = e.results(sid)[-1]
        self.assertTrue(res["hits"][0]["standard"])
        self.assertIn("（标准答案）", res["content"].split("\n")[1])
        gr = e.events(sid, ("grounding",))[-1]["payload"]
        self.assertEqual(gr["checks"], [{"n": res["hits"][0]["n"], "numbers": ["900 元"], "missing": []}])


class WeakHits(Case):
    """弱命中按没找到处理：问题里要紧的词（外汇、套期、保值、海外）库里一次都没出现，只靠"公司""子公司"沾上的不算找到。"""

    def _env(self, script=None):
        e = self.env(script or [], mode="full")
        d = os.path.join(e.tmp, "制度")
        os.makedirs(d)
        write(d, "差旅管理办法（2026 版）.md",
              "# 差旅管理办法\n\n## 第一章 总则\n\n为规范公司差旅行为，制定本办法。本办法适用于集团及各子公司全体员工的国内出差。\n\n"
              "## 第四章 住宿\n\n### 4.2 住宿标准\n\n总监级在一线城市每晚不超过 800 元，其他城市不超过 600 元。\n\n"
              "### （一）超标处理\n\n超出标准的部分由个人承担，确需超标的须事先经分管副总审批。\n")
        write(d, "季度汇报.md", "# 三季度经营汇报\n\n公司三季度销售额 1.2 亿元，同比增长 12.4%。各子公司均完成目标。\n")
        lib = kbm.create_library(e.rt.store, "公司制度")
        kbm.add_source(e.rt.store, lib["id"], d)
        e.rt.kb.indexer.enqueue(lib["id"])
        self.assertTrue(e.rt.kb.indexer.wait_idle(20))
        return e, lib

    def test_cover(self):
        e, lib = self._env()
        svc = e.rt.kb
        r = retrieve.search(svc, ["公司对海外子公司的外汇套期保值有什么规定？"], [lib["id"]], semantic=False)
        self.assertEqual(r["hits"], [])
        self.assertTrue(r["weak"])
        self.assertTrue(all(h["cover"] < 0.35 for h in r["weak"]))
        r = retrieve.search(svc, ["总监住宿标准"], [lib["id"]], semantic=False)
        self.assertEqual(r["hits"][0]["loc"], "4.2 住宿标准")
        self.assertEqual(r["hits"][0]["cover"], 1.0)
        r = retrieve.search(svc, ["超标怎么审批"], [lib["id"]], semantic=False)
        self.assertIn("超标处理", r["hits"][0]["loc"])
        # 工具：hits 为空、文字是没找到
        sid = e.rt.store.add_session(e.ws["id"], mode="hspace")["id"]
        res = REGISTRY["kb_search"].handler(Runner(e.rt, sid, e.mock).ctx, query="公司对海外子公司的外汇套期保值有什么规定？")
        self.assertEqual(res.extra["hits"], [])
        self.assertEqual(res.text if hasattr(res, "text") else str(res), retrieve.NOTHING)
        # 门槛可以配
        e.cfg.update({"kb": {"min_cover": 0.1}})
        r = retrieve.search(svc, ["公司对海外子公司的外汇套期保值有什么规定？"], [lib["id"]], semantic=False)
        self.assertTrue(r["hits"])

    def test_demo_says_not_found(self):
        from agent.llm import MockLLM, demo_script
        e, lib = self._env()
        e.mock = MockLLM([demo_script] * 10)
        e.rt.llm_factory = lambda: e.mock
        sid = e.rt.store.add_session(e.ws["id"], mode="hspace")["id"]
        e.rt.send_message(sid, "公司对海外子公司的外汇套期保值有什么规定？")
        self.assertTrue(e.rt.wait_idle(sid, 20))
        ans = e.events(sid, ("assistant_message",))[-1]["payload"]["content"]
        self.assertIn("资料里没有找到", ans)
        # 有答案的照样答，并且有 grounding 事件、数字对上
        e.rt.send_message(sid, "总监级在一线城市住宿每晚不超过多少钱？")
        self.assertTrue(e.rt.wait_idle(sid, 20))
        ans = e.events(sid, ("assistant_message",))[-1]
        self.assertIn("[1]", ans["payload"]["content"])
        gr = e.events(sid, ("grounding",))[-1]["payload"]
        self.assertEqual(gr["msg_seq"], ans["seq"])
        self.assertTrue(gr["checks"] and not gr["checks"][0]["missing"])


class ApiWeak(ApiCase):
    env_kw = {"mode": "full"}

    def test_weak_field(self):
        d = os.path.join(self.e.tmp, "制度")
        os.makedirs(d)
        write(d, "总则.md", "# 总则\n\n本办法适用于集团及各子公司全体员工的国内出差。\n")
        lib = kbm.create_library(self.e.rt.store, "公司制度")
        kbm.add_source(self.e.rt.store, lib["id"], d)
        self.e.rt.kb.indexer.enqueue(lib["id"])
        self.assertTrue(self.e.rt.kb.indexer.wait_idle(20))
        st, r = self.req("POST", "/api/kb/search", {"query": "海外子公司的外汇套期保值"})
        self.assertEqual(r["hits"], [])
        self.assertEqual(r["weak"][0]["title"], "总则.md")
        self.assertLess(r["weak"][0]["cover"], 0.35)
        st, r = self.req("POST", "/api/kb/search", {"query": "子公司员工国内出差"})
        self.assertTrue(r["hits"])
        self.assertEqual(r["weak"], [])
        self.assertEqual(self.req("POST", "/api/settings", {"kb": {"min_cover": 2}})[0], 400)
        self.assertEqual(self.req("POST", "/api/settings", {"kb": {"min_cover": 0.4, "min_sim": 0.25}})[0], 200)
