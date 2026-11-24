"""新内核：带 files 的用户发言给模型看时，文字后面加一行附件说明（docs/聊天为主的界面.md 1.2）。没装包时跳过。"""
from __future__ import annotations
import unittest
from helpers import Case, HAS_LG, LG_SKIP

NOTE = "（用户附上了文件：上传/a.xlsx、上传/b.pdf。先用 read_file、excel_inspect 等工具查看，再动手。）"


def capture(seen, reply="看过了"):
    def fn(messages, tools):
        seen.append(messages)
        return {"content": reply}
    return fn


def text_of(content):
    if isinstance(content, list):
        return "\n".join(b.get("text") or "" for b in content if isinstance(b, dict) and b.get("type") == "text")
    return content or ""


@unittest.skipUnless(HAS_LG, LG_SKIP)
class LGFilesNote(Case):
    def test_human_from_event(self):
        from agent.lg import models
        m = models.human_from_event({"text": "看看", "files": ["上传/a.xlsx", "上传/b.pdf"]})
        self.assertEqual(m.content, "看看\n" + NOTE)
        m = models.human_from_event({"text": "", "files": ["上传/a.xlsx", "上传/b.pdf"]})
        self.assertEqual(m.content, NOTE)
        m = models.human_from_event({"text": "看图", "images": ["data:image/png;base64,AA"], "files": ["上传/a.xlsx", "上传/b.pdf"]})
        self.assertEqual(m.content[0], {"type": "text", "text": "看图\n" + NOTE})
        self.assertEqual(m.content[1]["type"], "image_url")
        self.assertEqual(models.human_from_event({"text": "只有字"}).content, "只有字")

    def test_first_message_and_followup(self):
        seen = []
        e = self.lg_env([capture(seen), capture(seen)])
        self.assertEqual(e.rt.kernel_name, "langgraph")
        sid = e.new_session()
        e.rt.send_message(sid, "汇总一下", files=["上传/a.xlsx", "上传/b.pdf"])
        self.assertTrue(e.rt.wait_idle(sid, 20))
        self.assertEqual(e.status(sid), "done")
        users = [text_of(m["content"]) for m in seen[0] if m["role"] == "user"]
        self.assertEqual(users[-1], "汇总一下\n" + NOTE)
        # 下一轮：历史里那句话仍然带着附件说明（走账本投影这条路）
        e.rt.send_message(sid, "好的")
        self.assertTrue(e.rt.wait_idle(sid, 20))
        users = [text_of(m["content"]) for m in seen[1] if m["role"] == "user"]
        self.assertIn("汇总一下\n" + NOTE, users)
        self.assertEqual(users[-1], "好的")

    def test_restart_projection(self):
        # 重启后从账本重建对话（context.project → from_openai）也带附件说明
        seen = []
        e = self.lg_env([capture(seen)])
        sid = e.new_session()
        e.rt.send_message(sid, "汇总一下", files=["上传/a.xlsx", "上传/b.pdf"])
        self.assertTrue(e.rt.wait_idle(sid, 20))
        from agent.llm import MockLLM
        seen2 = []
        rt = e.restart(MockLLM([capture(seen2)]))
        rt.send_message(sid, "继续")
        self.assertTrue(rt.wait_idle(sid, 20))
        users = [text_of(m["content"]) for m in seen2[0] if m["role"] == "user"]
        self.assertIn("汇总一下\n" + NOTE, users)


if __name__ == "__main__":
    unittest.main()
