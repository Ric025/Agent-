"""深入研究（docs/两种模式.md 9.4）：那段做法只加在 deep 的那句发言后面（不进系统提示）、这一轮步数上限翻倍（不超过 60）、
压缩成摘要 / 导出 / 后台回顾里标"（深入研究）"。新内核的部分没装包时跳过。"""
from __future__ import annotations
import unittest

from helpers import Case, HAS_LG, LG_SKIP
from agent import context, export, review


def call(name, args):
    return {"content": "", "tool_calls": [(name, args)]}


class Note(unittest.TestCase):
    def test_short_and_names_skill(self):
        self.assertLessEqual(context.estimate_text(context.DEEP_NOTE), 200)
        for w in ("kb-research", "skill_load", "todo_write", "kb_search", "queries", "kb_open", "spawn_agent", "资料里没有覆盖到的"):
            self.assertIn(w, context.DEEP_NOTE)

    def test_user_text(self):
        self.assertEqual(context.user_text({"text": "查一下"}), "查一下")
        self.assertEqual(context.user_text({"text": "查一下", "depth": "normal"}), "查一下")
        t = context.user_text({"text": "查一下", "depth": "deep", "skill": "sop-qa"})
        self.assertTrue(t.startswith("查一下\n"))
        self.assertTrue(t.endswith(context.DEEP_NOTE))
        self.assertIn("sop-qa", t)

    def test_step_limit(self):
        self.assertEqual(context.step_limit(40, False), 40)
        self.assertEqual(context.step_limit(40, True), 60)
        self.assertEqual(context.step_limit(20, True), 40)
        self.assertEqual(context.step_limit(80, True), 80)        # 本来就超过 60 的不降


class Builtin(Case):
    def test_note_only_on_that_turn(self):
        e = self.env([{"content": "好"}, {"content": "好的"}, {"content": "嗯"}])
        sid = e.new_session()
        e.rt.send_message(sid, "普通的一问")
        self.assertTrue(e.rt.wait_idle(sid, 10))
        e.rt.send_message(sid, "把差旅制度查全", depth="deep")
        self.assertTrue(e.rt.wait_idle(sid, 10))
        e.rt.send_message(sid, "再普通地问一句")
        self.assertTrue(e.rt.wait_idle(sid, 10))
        calls = e.mock.calls
        for msgs in calls:
            self.assertNotIn(context.DEEP_NOTE, msgs[0]["content"])        # 不进系统提示
        self.assertFalse(any(context.DEEP_NOTE in str(m.get("content")) for m in calls[0]))
        users = [m for m in calls[1] if m["role"] == "user"]
        self.assertTrue(users[-1]["content"].endswith(context.DEEP_NOTE))
        self.assertNotIn(context.DEEP_NOTE, users[0]["content"])
        # 第三轮：新发言不带；之前那句仍然带着（和附件、技能一样跟着那句发言走，前缀不变，缓存不破）
        users3 = [m for m in calls[2] if m["role"] == "user"]
        self.assertNotIn(context.DEEP_NOTE, users3[-1]["content"])
        self.assertEqual([m["content"] for m in calls[2][:len(calls[1])] if m["role"] == "user"],
                         [m["content"] for m in calls[1] if m["role"] == "user"])
        self.assertEqual(e.events(sid, ("user_message",))[1]["payload"]["depth"], "deep")

    def _steps(self, depth, base=3):
        script = [call("todo_write", {"items": [{"text": "x", "status": "doing"}]}) for _ in range(10)]
        e = self.env(script, budgets={"max_steps": base})
        sid = e.new_session()
        e.rt.send_message(sid, "查", depth=depth)
        self.assertTrue(e.wait_status(sid, "paused", timeout=10))
        return len(e.events(sid, ("assistant_message",))), e

    def test_step_limit_doubles(self):
        n, _ = self._steps(None)
        self.assertEqual(n, 3)
        n, e = self._steps("deep")
        self.assertEqual(n, 6)

    def test_cap_60(self):
        script = [call("todo_write", {"items": [{"text": "x", "status": "doing"}]}) for _ in range(70)]
        e = self.env(script, budgets={"max_steps": 40})
        sid = e.new_session()
        e.rt.send_message(sid, "查", depth="deep")
        self.assertTrue(e.wait_status(sid, "paused", timeout=30))
        self.assertEqual(len(e.events(sid, ("assistant_message",))), 60)

    def test_marks_in_summary_export_review(self):
        e = self.env([{"content": "结论 [1]"}])
        sid = e.new_session()
        e.rt.send_message(sid, "把差旅制度查全", depth="deep")
        self.assertTrue(e.rt.wait_idle(sid, 10))
        evs = e.events(sid)
        tr = context._transcript(None, evs, 10000)
        self.assertIn("用户：把差旅制度查全（深入研究）", tr)
        self.assertNotIn(context.DEEP_NOTE, tr)
        self.assertIn("把差旅制度查全（深入研究）", context.mechanical_summary(None, evs, evs))
        md = export.session_markdown(e.rt.store, sid)
        self.assertIn("（深入研究）", md)
        self.assertNotIn(context.DEEP_NOTE, md)
        self.assertIn("用户：把差旅制度查全（深入研究）", str(review.transcript(evs)[0]))


@unittest.skipUnless(HAS_LG, LG_SKIP)
class LG(Case):
    def test_note_and_step_limit(self):
        script = [call("todo_write", {"items": [{"text": "x", "status": "doing"}]}) for _ in range(10)]
        e = self.lg_env(script, budgets={"max_steps": 3})
        sid = e.new_session()
        e.rt.send_message(sid, "查", depth="deep")
        self.assertTrue(e.wait_status(sid, "paused", timeout=20))
        self.assertEqual(len(e.events(sid, ("assistant_message",))), 6)
        users = [m for m in e.mock.calls[0] if m.get("role") == "user"]
        self.assertTrue(str(users[-1]["content"]).endswith(context.DEEP_NOTE))


if __name__ == "__main__":
    unittest.main()
