"""核对数字（docs/两种模式.md 9.3）：抽数字、规整、不算的那几类、按完整数字比、多个出处、不存在的编号、
没标出处的数字、代码块、CariAi 和 Hspace 的差别、事件写进账本且 msg_seq 对。"""
from __future__ import annotations
import os, time, unittest

from helpers import Case
from agent import grounding as g, kb as kbm
from test_kb import make_docx, SOP

PARTS = {
    1: {"n": 1, "text": "总监级在一线城市每晚不超过 800 元，其他城市不超过 600 元。经理级在一线城市每晚不超过 500 元。",
        "title": "差旅管理办法（2026 版）.docx", "heading": "第四章 住宿 > 4.2 住宿标准", "loc": "4.2 住宿标准"},
    2: {"n": 2, "text": "华东区销售额 1.2 亿，同比增长 12.40%；费用合计 3,860 万元。审批通过后五个工作日内到账。",
        "title": "三季度经营汇报.pptx", "heading": "", "loc": "第 2 页 · 华东区销售回顾"},
}


def nums(text):
    return [x[0] for x in g.numbers_in(text)]


class Extract(unittest.TestCase):
    def test_numbers_and_units(self):
        self.assertEqual(nums("每晚 800 元，增长 12.4%，5 个工作日，1.2 亿元，3,860 万元"),
                         ["800 元", "12.4%", "5 个工作日", "1.2 亿元", "3,860 万元"])
        self.assertEqual(nums("全角：８００元、１２．４％"), ["800元", "12.4%"])
        self.assertEqual(nums("9:30 开会"), ["9:30"])

    def test_not_counted(self):
        # 书名号、指位置的、型号
        self.assertEqual(nums("《差旅管理办法（2026 版）》第 3 章 3.2 节 第 7 页 第 2–40 行 步骤 4 的规定"), [])
        self.assertEqual(nums("5G 网络和 V3 版本"), [])
        # 行首的列表序号
        self.assertEqual(g.check("1. 经理级 500 元 [1]\n2、总监级 800 元 [1]\n（3）其他城市 600 元 [1]", PARTS)["checks"],
                         [{"n": 1, "numbers": ["500 元", "800 元", "600 元"], "missing": []}])
        # 出处编号本身
        r = g.check("每晚不超过 800 元 [1]。", PARTS)
        self.assertEqual(r["checks"][0]["numbers"], ["800 元"])

    def test_norm(self):
        self.assertEqual(g.norm_number("12.40%"), "12.4")
        self.assertEqual(g.norm_number("3,860"), "3860")
        self.assertEqual(g.norm_number("800.00"), "800")
        self.assertEqual(g.norm_number("０５"), "5")
        self.assertEqual(g.norm_number("１２．５"), "12.5")


class Check(unittest.TestCase):
    def test_match_and_missing(self):
        r = g.check("总监级一线城市每晚不超过 800 元 [1]。华东区销售额 1.2 亿元，同比增长 12.4%[2]。", PARTS)
        self.assertEqual(r["cited"], [1, 2])
        self.assertEqual(r["checks"], [{"n": 1, "numbers": ["800 元"], "missing": []},
                                       {"n": 2, "numbers": ["1.2 亿元", "12.4%"], "missing": []}])
        self.assertTrue(r["ok"])
        r = g.check("总监级每晚不超过 900 元。[1]", PARTS)     # 句号后面的出处也管这一句
        self.assertEqual(r["checks"], [{"n": 1, "numbers": ["900 元"], "missing": ["900 元"]}])
        self.assertFalse(r["ok"])

    def test_whole_number_compare(self):
        # 800 在原文里，但 1800、8000、80 不能算对上
        r = g.check("一线城市 1800 元 [1]；其他 8000 元 [1]；再其他 80 元 [1]；经理 500 元 [1]", PARTS)
        self.assertEqual(r["checks"][0]["missing"], ["1800 元", "8000 元", "80 元"])
        # 千分位、小数末尾的零规整后比
        r = g.check("费用合计 3860 万元，增长 12.4% [2]", PARTS)
        self.assertEqual(r["checks"][0]["missing"], [])
        # 不做单位换算：原文"1.2 亿"，回答"12000 万"算没对上
        r = g.check("销售额 12000 万 [2]", PARTS)
        self.assertEqual(r["checks"][0]["missing"], ["12000 万"])
        # 原文的中文数字（五个工作日）也认
        self.assertEqual(g.check("5 个工作日内到账 [2]", PARTS)["checks"][0]["missing"], [])

    def test_several_refs_and_clauses(self):
        # 一组出处里任何一个出处有这个数字就算对上
        r = g.check("总监 800 元，销售额 1.2 亿 [1][2]", PARTS)
        self.assertEqual(r["checks"], [{"n": 1, "numbers": ["800 元", "1.2 亿"], "missing": []},
                                       {"n": 2, "numbers": ["800 元", "1.2 亿"], "missing": []}])
        for t in ("总监 800 元 [1, 2]", "总监 800 元 [1，2]", "总监 800 元 [1、2]"):
            self.assertEqual(g.check(t, PARTS)["cited"], [1, 2], t)
        # 一句里几组出处：每组管它前面那一段
        r = g.check("总监 800 元 [1]，销售额 1.2 亿 [2]。", PARTS)
        self.assertEqual(r["checks"], [{"n": 1, "numbers": ["800 元"], "missing": []},
                                       {"n": 2, "numbers": ["1.2 亿"], "missing": []}])
        # 同一个编号在几句里出现：合并
        r = g.check("总监 800 元 [1]。经理 550 元 [1]。", PARTS)
        self.assertEqual(r["checks"], [{"n": 1, "numbers": ["800 元", "550 元"], "missing": ["550 元"]}])

    def test_paragraph_final_ref(self):
        # "A。B。[1]"：段末的出处管这一段里前面没标出处的句子；换了行就不管
        r = g.check("总监 800 元。其他城市 600 元。经理 550 元。[1]", PARTS)
        self.assertEqual((r["checks"][0]["numbers"], r["checks"][0]["missing"], r["uncited_numbers"]),
                         (["800 元", "600 元", "550 元"], ["550 元"], []))
        r = g.check("总监 800 元。\n其他城市 600 元 [1]", PARTS)
        self.assertEqual(r["uncited_numbers"], ["800 元"])
        r = g.check("总监 800 元 [1]。其他 700 元。", PARTS)
        self.assertEqual(r["uncited_numbers"], ["700 元"])

    def test_unknown_refs(self):
        r = g.check("总监 800 元 [1][7]", PARTS)
        self.assertEqual((r["cited"], r["unknown_refs"], r["ok"]), ([1], [7], False))
        r = g.check("见 [9]", PARTS)
        self.assertEqual((r["cited"], r["unknown_refs"], r["checks"]), ([], [9], []))

    def test_uncited_numbers_hspace_only(self):
        t = "总监 800 元 [1]。另外合计 3,860 元。"
        r = g.check(t, PARTS, "hspace")
        self.assertEqual(r["uncited_numbers"], ["3,860 元"])
        self.assertFalse(r["ok"])
        r = g.check(t, PARTS, "cariai")
        self.assertEqual(r["uncited_numbers"], [])
        self.assertTrue(r["ok"])
        # "出处："那一行、"以下不是资料里的内容"之后的不算
        t = "总监 800 元 [1]\n\n出处：《差旅》· 第四章 住宿 > 4.2 住宿标准。\n\n以下不是资料里的内容：\n一般酒店 300 元左右。"
        self.assertEqual(g.check(t, PARTS)["uncited_numbers"], [])

    def test_code_blocks(self):
        t = "总监 800 元 [1]\n```python\nx = 1800  # [1]\n```\n用 `limit=900` 算 [1]"
        r = g.check(t, PARTS)
        self.assertEqual(r["checks"], [{"n": 1, "numbers": ["800 元"], "missing": []}])
        self.assertEqual(r["uncited_numbers"], [])

    def test_no_refs_no_numbers(self):
        r = g.check("你好，有什么可以帮你？", {}, "hspace")
        self.assertEqual(r, {"cited": [], "unknown_refs": [], "checks": [], "uncited_numbers": [], "ok": True})

    def test_deep_lookup_section(self):
        # 片段原文里没有、整节里有：去整节找（part_detail）
        class FakeStore:
            pass
        parts = {1: {"n": 1, "text": "总监级每晚不超过 800 元。", "part_id": 42}}
        orig = kbm.part_detail
        kbm.part_detail = lambda store, pid: {"section": {"text": "经理级每晚不超过 500 元。"}, "part": {"text": ""}} if pid == 42 else None
        try:
            r = g.check("经理 500 元 [1]", parts, store=FakeStore())
        finally:
            kbm.part_detail = orig
        self.assertEqual(r["checks"][0]["missing"], [])

    def test_fast(self):
        t = ("总监级一线城市每晚不超过 800 元 [1]。华东区销售额 1.2 亿元，同比增长 12.4%[2]。\n" * 20)
        t0 = time.time()
        for _ in range(20):
            g.check(t, PARTS)
        self.assertLess((time.time() - t0) / 20, 0.05)


class Events(Case):
    """走一轮：事件写进账本、msg_seq 对；CariAi 只在带 [n] 而且检索过时做；子代理不做。"""

    def _env(self, script):
        e = self.env(script, mode="full")
        d = os.path.join(e.tmp, "SOP")
        os.makedirs(d)
        make_docx(os.path.join(d, SOP))
        lib = kbm.create_library(e.rt.store, "公司 SOP")
        kbm.add_source(e.rt.store, lib["id"], d)
        e.rt.kb.indexer.enqueue(lib["id"])
        self.assertTrue(e.rt.kb.indexer.wait_idle(20))
        return e

    def _run(self, e, mode, text="问"):
        sid = e.rt.store.add_session(e.ws["id"], mode=mode)["id"]
        e.rt.send_message(sid, text)
        self.assertTrue(e.rt.wait_idle(sid, 20))
        return sid

    def test_hspace_event(self):
        e = self._env([{"tool_calls": [("kb_search", {"query": "住宿标准"})]},
                       {"content": "总监级在一线城市每晚不超过 800 元 [1]，经理级 550 元 [1][9]。另外 3,860 元。"}])
        sid = self._run(e, "hspace")
        ans = e.events(sid, ("assistant_message",))[-1]
        gr = e.events(sid, ("grounding",))
        self.assertEqual(len(gr), 1)
        p = gr[0]["payload"]
        self.assertEqual(p["msg_seq"], ans["seq"])
        self.assertGreater(gr[0]["seq"], ans["seq"])
        self.assertEqual(p["cited"], [1])
        self.assertEqual(p["unknown_refs"], [9])
        self.assertEqual(p["checks"], [{"n": 1, "numbers": ["800 元", "550 元"], "missing": ["550 元"]}])
        self.assertEqual(p["uncited_numbers"], ["3,860 元"])
        self.assertFalse(p["ok"])
        self.assertEqual(set(p), {"msg_seq", "cited", "unknown_refs", "checks", "uncited_numbers", "ok"})
        # 只标记：回答原样，没有发给模型
        self.assertIn("550 元", ans["payload"]["content"])
        self.assertFalse(any("grounding" in str(m) for msgs in e.mock.calls for m in msgs))

    def test_hspace_without_refs_still_emits(self):
        e = self._env([{"content": "你好，我能帮你查公司资料。"}])
        sid = self._run(e, "hspace", "你好")
        p = e.events(sid, ("grounding",))[0]["payload"]
        self.assertEqual((p["cited"], p["checks"], p["ok"]), ([], [], True))

    def test_cariai_only_with_refs(self):
        e = self._env([{"content": "好的，每晚 800 元。"},
                       {"tool_calls": [("kb_search", {"query": "住宿标准"})]}, {"content": "每晚 800 元 [1]。合计 3,860 元。"},
                       {"content": "第一步见 [1]"}])
        sid = self._run(e, "cariai")
        self.assertEqual(e.events(sid, ("grounding",)), [])
        sid2 = self._run(e, "cariai")
        p = e.events(sid2, ("grounding",))[0]["payload"]
        self.assertEqual((p["cited"], p["uncited_numbers"], p["ok"]), ([1], [], True))
        # 没检索过资料的对话里的 [1]：不做
        sid3 = self._run(e, "cariai")
        self.assertEqual(e.events(sid3, ("grounding",)), [])

    def test_tool_call_messages_and_subagents_skipped(self):
        e = self._env([{"content": "先查 800 元 [1]", "tool_calls": [("kb_search", {"query": "住宿标准"})]},
                       {"content": "800 元 [1]"}])
        sid = self._run(e, "hspace")
        gr = e.events(sid, ("grounding",))
        self.assertEqual(len(gr), 1)                 # 带工具调用的那条不做
        sub = e.rt.store.add_session(e.ws["id"], kind="sub", parent_id=sid)
        e.rt.emit(sub["id"], "assistant_message", {"content": "800 元 [1]", "tool_calls": []})
        self.assertEqual(e.events(sub["id"], ("grounding",)), [])
        self.assertEqual(g.latest(e.rt.store, sid)[gr[0]["payload"]["msg_seq"]]["ok"], True)


if __name__ == "__main__":
    unittest.main()
