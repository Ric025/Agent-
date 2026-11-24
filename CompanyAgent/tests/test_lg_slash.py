"""新内核：点名技能、手动整理、对话状态（docs/指令和主题.md 1.3）。没装包时跳过。"""
from __future__ import annotations
import unittest
from helpers import Case, HAS_LG, LG_SKIP
from agent.context import COMPRESS_PROMPT, SUMMARY_PREFIX

NOTE = "（用户点名要用技能「slides」：先用 skill_load 读它的说明，再照着做。）"


def text_of(content):
    if isinstance(content, list):
        return "\n".join(b.get("text") or "" for b in content if isinstance(b, dict) and b.get("type") == "text")
    return content or ""


def is_compress(messages):
    return bool(messages) and messages[0].get("role") == "system" and str(messages[0].get("content") or "").startswith(COMPRESS_PROMPT)


def capture(seen, reply):
    def fn(messages, tools):
        seen.append(messages)
        return {"content": reply}
    return fn


@unittest.skipUnless(HAS_LG, LG_SKIP)
class LGSkill(Case):
    def test_human_from_event(self):
        from agent.lg import models
        self.assertEqual(models.human_from_event({"text": "做 5 页", "skill": "slides"}).content, "做 5 页\n" + NOTE)
        self.assertEqual(models.human_from_event({"text": "", "skill": "slides"}).content, NOTE)

    def test_skill_message(self):
        seen = []
        e = self.lg_env([capture(seen, "好"), capture(seen, "好")])
        sid = e.new_session()
        e.rt.send_message(sid, "做 5 页", skill="slides")
        self.assertTrue(e.rt.wait_idle(sid, 20))
        self.assertEqual(e.status(sid), "done")
        users = [text_of(m["content"]) for m in seen[0] if m["role"] == "user"]
        self.assertEqual(users[-1], "做 5 页\n" + NOTE)
        e.rt.send_message(sid, "", skill="slides")
        self.assertTrue(e.rt.wait_idle(sid, 20))
        users = [text_of(m["content"]) for m in seen[1] if m["role"] == "user"]
        self.assertEqual(users[-1], NOTE)


@unittest.skipUnless(HAS_LG, LG_SKIP)
class LGCompact(Case):
    def test_compact_resets_checkpoint(self):
        seen = []
        e = self.lg_env([capture(seen, "第一轮回答"), capture(seen, "第二轮回答")])
        sid = e.new_session()
        e.rt.send_message(sid, "第一件事：" + "说明。" * 40)
        self.assertTrue(e.rt.wait_idle(sid, 20))
        e.rt.send_message(sid, "第二件事")
        self.assertTrue(e.rt.wait_idle(sid, 20))
        self.assertEqual(e.status(sid), "done")
        saver = e.rt.lg.saver()
        self.assertIsNotNone(saver.get_tuple({"configurable": {"thread_id": sid}}))
        e.mock.script[:] = [capture(seen, "摘要：第一件事做完了。"), capture(seen, "第三轮回答")]
        r = e.rt.compact(sid, "保留金额")
        self.assertEqual(r, {"ok": True, "started": True})
        self.assertTrue(e.rt.wait_idle(sid, 20))
        self.assertTrue(is_compress(seen[2]))
        self.assertIn("用户特别要求保留：保留金额", seen[2][0]["content"])
        self.assertEqual([x["payload"] for x in e.events(sid, ("compact",))], [{"state": "done", "compacted": True}])
        self.assertEqual(e.events(sid, ("summary",))[-1]["payload"]["text"], "摘要：第一件事做完了。")
        # 检查点已经作废，下次运行从账本重新装载：第一条是摘要，最近一轮还在，压掉的原文不在
        self.assertIsNone(saver.get_tuple({"configurable": {"thread_id": sid}}))
        e.rt.send_message(sid, "第三件事")
        self.assertTrue(e.rt.wait_idle(sid, 20))
        self.assertEqual(e.status(sid), "done")
        users = [text_of(m["content"]) for m in seen[3] if m["role"] == "user"]
        self.assertTrue(users[0].startswith(SUMMARY_PREFIX + "摘要：第一件事"))
        joined = "\n".join(users)
        self.assertIn("第二件事", joined)
        self.assertIn("第三件事", joined)
        self.assertNotIn("说明。说明。", joined)

    def test_nothing_to_compact(self):
        e = self.lg_env([])
        sid = e.new_session()
        self.assertEqual(e.rt.compact(sid), {"ok": True, "compacted": False})

    def test_status(self):
        e = self.lg_env([{"content": "好"}])
        sid = e.start("你好")
        self.assertTrue(e.rt.wait_idle(sid, 20))
        st = e.rt.session_status(sid)
        self.assertEqual(st["kernel"], "langgraph")
        self.assertGreater(st["context"]["used"], 100)
        self.assertEqual(st["steps"], 0)


if __name__ == "__main__":
    unittest.main()
