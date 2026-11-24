"""记忆和历史检索的接口（docs/记忆设计.md 第 6 节，以及 memory_undo 事件）。"""
from __future__ import annotations
import unittest
from urllib.parse import quote
from helpers import ApiCase
from agent import memory as mem


class MemoryApi(ApiCase):
    def test_list_add_edit_delete(self):
        wid = self.e.ws["id"]
        st, m = self.req("POST", "/api/memories", {"text": "我是财务总监", "target": "user"})
        self.assertEqual((st, m["target"], m["origin"]), (200, "user", "user"))
        self.assertIsInstance(m["log_id"], int)
        st, w = self.req("POST", "/api/memories", {"text": "对账表放在 对账/ 目录", "target": "workspace", "workspace_id": wid})
        self.assertEqual((st, w["target"]), (200, "workspace"))
        st, old = self.req("POST", "/api/memories", {"text": "汇报用万元", "workspace_id": wid})    # 旧写法：没有 target
        self.assertEqual(old["target"], "workspace")
        st, old2 = self.req("POST", "/api/memories", {"text": "喜欢简短的回答", "workspace_id": None})
        self.assertEqual(old2["target"], "user")
        st, d = self.req("GET", "/api/memories?workspace=" + wid)
        self.assertEqual(st, 200)
        self.assertEqual(sorted(x["text"] for x in d["memories"]), ["喜欢简短的回答", "对账表放在 对账/ 目录", "我是财务总监", "汇报用万元"])
        self.assertEqual(set(d["memories"][0]), {"id", "target", "text", "origin", "session_id", "created", "updated"})
        self.assertEqual(set(d["usage"]), {"user", "workspace"})
        self.assertEqual(set(d["usage"]["user"]), {"used", "budget", "pct"})
        self.assertEqual(d["settings"], {"enabled": True, "auto": True})
        st, d = self.req("GET", "/api/memories")                       # 不带工作区：只有 user 的
        self.assertEqual({x["target"] for x in d["memories"]}, {"user"})
        self.assertEqual(list(d["usage"]), ["user"])
        st, r = self.req("PATCH", "/api/memories/" + m["id"], {"text": "我是财务总监，负责华东区"})
        self.assertEqual((st, r["text"]), (200, "我是财务总监，负责华东区"))
        st, r = self.req("DELETE", "/api/memories/" + w["id"])
        self.assertEqual(st, 200)
        st, lg = self.req("GET", "/api/memories/log?workspace=%s&limit=2" % wid)
        self.assertEqual([x["op"] for x in lg["log"]], ["remove", "replace"])
        self.assertTrue({"id", "ts", "op", "target", "before", "after", "origin", "session_id", "undone"} <= set(lg["log"][0]))
        st, u = self.req("POST", "/api/memories/undo", {"log_id": r["log_id"]})
        self.assertEqual(st, 200)
        self.assertIn("对账表放在 对账/ 目录", [x["text"] for x in mem.entries(self.e.rt.store, wid, "workspace")])

    def test_errors(self):
        wid = self.e.ws["id"]
        st, b = self.req("POST", "/api/memories", {"text": "密码是 abc12345", "target": "user"})
        self.assertEqual((st, b["code"]), (400, "blocked"))
        self.assertTrue(b["error"])
        self.req("POST", "/api/memories", {"text": "报告先给结论", "target": "user"})
        st, b = self.req("POST", "/api/memories", {"text": "报告先给结论", "target": "user"})
        self.assertEqual((st, b["code"]), (400, "duplicate"))
        self.assertEqual(self.req("POST", "/api/memories", {"text": "x", "target": "workspace"})[0], 400)    # 没给工作区
        self.assertEqual(self.req("POST", "/api/memories", {"text": "x", "target": "nope"})[0], 400)
        self.assertEqual(self.req("POST", "/api/memories", {"text": 1, "target": "user"})[0], 400)
        self.assertEqual(self.req("POST", "/api/memories", {"text": "x", "workspace_id": "w_nope"})[0], 404)
        self.assertEqual(self.req("GET", "/api/memories?workspace=w_nope")[0], 404)
        st, b = self.req("PATCH", "/api/memories/m_nope", {"text": "x"})
        self.assertEqual((st, b["code"]), (400, "not_found"))
        self.assertEqual(self.req("PATCH", "/api/memories/m_nope", {})[0], 400)
        self.assertEqual(self.req("DELETE", "/api/memories/m_nope")[1]["code"], "not_found")
        self.assertEqual(self.req("POST", "/api/memories/undo", {"log_id": "1"})[0], 400)
        self.assertEqual(self.req("GET", "/api/memories/log?limit=abc")[0], 400)
        st, m = self.req("POST", "/api/memories", {"text": "第二条不一样的", "target": "user"})
        self.req("PATCH", "/api/memories/" + m["id"], {"text": "第二条改过了"})
        st, b = self.req("POST", "/api/memories/undo", {"log_id": m["log_id"]})       # 之后又改过
        self.assertEqual((st, b["code"]), (409, "conflict"))
        self.e.cfg.update({"memory": {"enabled": False}})
        st, b = self.req("POST", "/api/memories", {"text": "第三条", "target": "user"})
        self.assertEqual((st, b["code"]), (400, "disabled"))
        self.assertEqual(self.req("GET", "/api/memories")[1]["settings"]["enabled"], False)

    def test_no_token(self):
        for method, path in (("GET", "/api/memories"), ("POST", "/api/memories"), ("PATCH", "/api/memories/m_1"),
                             ("DELETE", "/api/memories/m_1"), ("POST", "/api/memories/undo"), ("GET", "/api/memories/log"),
                             ("GET", "/api/search?q=x&workspace=w"), ("GET", "/api/skills"), ("GET", "/api/skills/slides"),
                             ("DELETE", "/api/skills/slides"), ("GET", "/api/sessions/s_1/export")):
            self.assertEqual(self.req(method, path, token=False)[0], 403, path)

    def test_undo_event(self):
        rt, wid = self.e.rt, self.e.ws["id"]
        sid = rt.store.add_session(wid, "任务")["id"]
        r = mem.add(rt.store, self.e.cfg, wid, "user", "报告先给结论", "chat", sid)
        st, _ = self.req("POST", "/api/memories/undo", {"log_id": r["log_id"]})
        self.assertEqual(st, 200)
        evs = [e["payload"] for e in rt.store.events(sid, types=("memory_undo",))]
        self.assertEqual(evs, [{"log_id": r["log_id"], "memory_id": r["entry"]["id"]}])
        # 任务删掉了：照样撤销，不记事件
        sid2 = rt.store.add_session(wid, "任务2")["id"]
        r2 = mem.add(rt.store, self.e.cfg, wid, "user", "另一条不同的内容", "chat", sid2)
        rt.delete_session(sid2)
        self.assertEqual(self.req("POST", "/api/memories/undo", {"log_id": r2["log_id"]})[0], 200)
        self.assertFalse(rt.store.q("SELECT 1 FROM events WHERE type='memory_undo' AND session_id=?", (sid2,)))
        # 记忆页里写的（没有任务）：不记事件
        st, m = self.req("POST", "/api/memories", {"text": "第三条再不同", "target": "user"})
        self.req("POST", "/api/memories/undo", {"log_id": m["log_id"]})
        self.assertEqual(len(rt.store.q("SELECT 1 FROM events WHERE type='memory_undo'")), 1)
        from agent.context import CHAT_TYPES
        self.assertNotIn("memory_undo", CHAT_TYPES)

    def test_settings(self):
        st, d = self.req("GET", "/api/settings")
        self.assertEqual(d["config"]["memory"]["idle_minutes"], 2)
        self.assertEqual(self.req("POST", "/api/settings", {"memory": {"auto": False}})[0], 200)
        self.assertFalse(mem.settings(self.e.cfg)["auto"])
        self.assertTrue(mem.settings(self.e.cfg)["enabled"])
        self.assertEqual(self.req("POST", "/api/settings", {"memory": {"user_budget": 9}})[0], 400)
        self.assertEqual(self.req("POST", "/api/settings", {"memory": {"enabled": "yes"}})[0], 400)
        tools = {t["name"]: t for t in d["tools"]}
        self.assertEqual((tools["memory"]["risk"], tools["skill_save"]["risk"]), ("memory", "skill"))

    def test_search(self):
        rt, wid = self.e.rt, self.e.ws["id"]
        s = rt.store.add_session(wid, "十月对账")["id"]
        rt.emit(s, "user_message", {"text": "核对十月的应收账款"})
        st, d = self.req("GET", "/api/search?q=%s&workspace=%s" % (quote("应收账款"), wid))
        self.assertEqual(st, 200)
        self.assertEqual(d["hits"][0]["session_id"], s)
        self.assertEqual(set(d["hits"][0]), {"session_id", "title", "date", "matches"})
        st, d = self.req("GET", "/api/search?q=%s&workspace=%s" % (quote("没有的词"), wid))
        self.assertEqual(d["hits"], [])
        self.assertEqual(self.req("GET", "/api/search?workspace=" + wid)[0], 400)
        self.assertEqual(self.req("GET", "/api/search?q=x")[0], 400)
        self.assertEqual(self.req("GET", "/api/search?q=x&workspace=w_nope")[0], 404)


if __name__ == "__main__":
    unittest.main()
