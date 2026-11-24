"""斜杠指令要的后端接口（docs/指令和主题.md 1.3）：点名技能、手动整理、改标题、主题设置、
主题静态文件、内核提示不进对话、对话状态。过渡内核；新内核的在 test_lg_slash.py。"""
from __future__ import annotations
import json, os, shutil, tempfile, unittest
from helpers import ApiCase, Case, HAS_LG, wait_until
from agent import context
from agent.context import COMPRESS_PROMPT, SUMMARY_PREFIX

NOTE = "（用户点名要用技能「slides」：先用 skill_load 读它的说明，再照着做。）"


def text_of(content):
    if isinstance(content, list):
        return "\n".join(b.get("text") or "" for b in content if isinstance(b, dict) and b.get("type") == "text")
    return content or ""


def is_compress(messages):
    return bool(messages) and messages[0].get("role") == "system" and str(messages[0].get("content") or "").startswith(COMPRESS_PROMPT)


class SkillNote(Case):
    def test_user_text(self):
        self.assertEqual(context.user_text({"text": "做 5 页", "skill": "slides"}), "做 5 页\n" + NOTE)
        self.assertEqual(context.user_text({"text": "", "skill": "slides"}), NOTE)
        t = context.user_text({"text": "看看", "files": ["a.xlsx"], "skill": "slides"})
        self.assertEqual(t, "看看\n" + context.files_note(["a.xlsx"]) + "\n" + NOTE)
        self.assertEqual(context.user_text({"text": "只有字"}), "只有字")


class SkillApi(ApiCase):
    script = [{"content": "好的"}, {"content": "再做"}]

    def test_new_session_with_skill(self):
        st, s = self.req("POST", "/api/sessions", {"workspace_id": self.e.ws["id"], "text": "做 5 页", "skill": "slides"})
        self.assertEqual(st, 200)
        self.assertTrue(self.e.rt.wait_idle(s["id"], 5))
        ev = self.e.events(s["id"], ("user_message",))[0]
        self.assertEqual(ev["payload"], {"text": "做 5 页", "skill": "slides"})
        users = [text_of(m["content"]) for m in self.e.mock.calls[0] if m["role"] == "user"]
        self.assertEqual(users[-1], "做 5 页\n" + NOTE)

    def test_skill_only_message(self):
        sid = self.e.new_session()
        st, _ = self.req("POST", "/api/sessions/%s/message" % sid, {"text": "", "skill": "slides"})
        self.assertEqual(st, 200)
        self.assertTrue(self.e.rt.wait_idle(sid, 5))
        self.assertEqual(self.e.events(sid, ("user_message",))[0]["payload"], {"text": "", "skill": "slides"})
        self.assertEqual(self.e.rt.store.session(sid)["title"], "slides")
        users = [text_of(m["content"]) for m in self.e.mock.calls[0] if m["role"] == "user"]
        self.assertEqual(users[-1], NOTE)
        # 只带 skill 的新对话也算有效消息
        st, s = self.req("POST", "/api/sessions", {"workspace_id": self.e.ws["id"], "skill": "slides"})
        self.assertEqual(st, 200)
        self.assertTrue(self.e.rt.wait_idle(s["id"], 5))
        self.assertEqual(len(self.e.events(s["id"], ("user_message",))), 1)

    def test_bad_skill(self):
        sid = self.e.new_session()
        st, b = self.req("POST", "/api/sessions/%s/message" % sid, {"text": "做", "skill": "没有的技能"})
        self.assertEqual(st, 400)
        self.assertIn("没有这个技能", b["error"])
        st, b = self.req("POST", "/api/sessions/%s/message" % sid, {"text": "做", "skill": 3})
        self.assertEqual(st, 400)
        n = len(self.e.rt.store.sessions(self.e.ws["id"], kind="task"))
        st, b = self.req("POST", "/api/sessions", {"workspace_id": self.e.ws["id"], "text": "做", "skill": "nope"})
        self.assertEqual(st, 400)
        self.assertEqual(len(self.e.rt.store.sessions(self.e.ws["id"], kind="task")), n)     # 没有留下空对话
        self.assertEqual(self.e.events(sid, ("user_message",)), [])

    def test_skill_in_answer(self):
        # 正在等回答时，点名技能接在答案后面
        self.e.mock.script[:] = [{"content": "", "tool_calls": [("ask_user", {"question": "做几页？"})]}, {"content": "好"}]
        sid = self.e.start("做个演示")
        self.assertTrue(self.e.wait_status(sid, "waiting_input"))
        st, _ = self.req("POST", "/api/sessions/%s/message" % sid, {"text": "5 页", "skill": "slides"})
        self.assertEqual(st, 200)
        self.assertTrue(self.e.rt.wait_idle(sid, 5))
        self.assertEqual(self.e.events(sid, ("answer",))[0]["payload"]["text"], "5 页\n" + NOTE)


def two_turns():
    return [{"content": "第一轮回答：" + "季度数据。" * 40}, {"content": "第二轮回答"}]


class Compact(ApiCase):
    script = two_turns()

    def _two_turns(self):
        sid = self.e.start("第一件事：" + "说明。" * 40)
        self.assertTrue(self.e.rt.wait_idle(sid, 5))
        self.e.rt.send_message(sid, "第二件事")
        self.assertTrue(self.e.rt.wait_idle(sid, 5))
        return sid

    def test_compact_with_focus(self):
        sid = self._two_turns()
        self.e.mock.script[:] = [{"content": "摘要：第一件事做完了，金额 1234。"}, {"content": "接着做"}]
        st, b = self.req("POST", "/api/sessions/%s/control" % sid, {"op": "compact", "focus": "保留金额"})
        self.assertEqual((st, b), (200, {"ok": True, "started": True}))
        self.assertTrue(self.e.rt.wait_idle(sid, 5))
        call = self.e.mock.calls[-1]
        self.assertTrue(is_compress(call))
        self.assertIn("用户特别要求保留：保留金额", call[0]["content"])
        self.assertIn("第一件事", call[1]["content"])
        self.assertNotIn("第二件事", call[1]["content"])      # 最近一轮留着不压
        sums = self.e.events(sid, ("summary",))
        self.assertEqual(len(sums), 1)
        self.assertEqual(sums[0]["payload"]["text"], "摘要：第一件事做完了，金额 1234。")
        done = self.e.events(sid, ("compact",))
        self.assertEqual([d["payload"] for d in done], [{"state": "done", "compacted": True}])
        self.assertEqual(self.e.status(sid), "done")             # 整理不改对话状态
        # 下一次请求从摘要开始，最近一轮还在
        self.e.rt.send_message(sid, "第三件事")
        self.assertTrue(self.e.rt.wait_idle(sid, 5))
        msgs = self.e.mock.calls[-1]
        users = [text_of(m["content"]) for m in msgs if m["role"] == "user"]
        self.assertTrue(users[0].startswith(SUMMARY_PREFIX + "摘要：第一件事"))
        self.assertIn("第二件事", users[0])                     # 摘要和后面那句用户发言合在一条里
        self.assertNotIn("说明。说明。", "\n".join(users))

    def test_nothing_to_compact(self):
        sid = self.e.new_session()
        st, b = self.req("POST", "/api/sessions/%s/control" % sid, {"op": "compact"})
        self.assertEqual((st, b), (200, {"ok": True, "compacted": False}))
        self.assertEqual(self.e.events(sid, ("compact",)), [])
        st, _ = self.req("POST", "/api/sessions/nope/control", {"op": "compact"})
        self.assertEqual(st, 404)
        st, _ = self.req("POST", "/api/sessions/%s/control" % sid, {"op": "compact", "focus": 3})
        self.assertEqual(st, 400)
        st, _ = self.req("POST", "/api/sessions/%s/control" % sid, {"op": "squash"})
        self.assertEqual(st, 400)

    def test_compact_after_compact(self):
        # 压过一次之后只剩最近一轮：还能再压（把它之前的那条也收进摘要）；最后没东西可压时回 compacted: false
        sid = self._two_turns()
        self.e.mock.script[:] = [{"content": "摘要一"}, {"content": "摘要二"}, {"content": "摘要三"}]
        for _ in range(3):
            st, b = self.req("POST", "/api/sessions/%s/control" % sid, {"op": "compact"})
            self.assertEqual(st, 200)
            self.assertTrue(self.e.rt.wait_idle(sid, 5))
            if b.get("compacted") is False:
                break
        self.assertEqual(b, {"ok": True, "compacted": False})
        self.assertGreaterEqual(len(self.e.events(sid, ("summary",))), 1)

    def test_busy_409(self):
        self.e.mock.script[:] = [{"content": "慢慢想", "delay": 3}]
        sid = self.e.start("开始")
        self.assertTrue(wait_until(lambda: self.e.rt._live(sid).alive, 2))
        st, b = self.req("POST", "/api/sessions/%s/control" % sid, {"op": "compact"})
        self.assertEqual(st, 409)
        self.assertIn("正在运行", b["error"])
        self.e.rt.control(sid, "cancel")
        self.assertTrue(self.e.rt.wait_idle(sid, 5))

    def test_message_during_compact(self):
        # 整理期间来了新消息：先发着"运行中"，整理完再启动；再点整理回 409
        sid = self._two_turns()
        self.e.mock.script[:] = [{"content": "摘要", "delay": 0.6}, {"content": "第三轮回答"}]
        st, b = self.req("POST", "/api/sessions/%s/control" % sid, {"op": "compact"})
        self.assertEqual(b, {"ok": True, "started": True})
        st, b = self.req("POST", "/api/sessions/%s/control" % sid, {"op": "compact"})
        self.assertEqual(st, 409)
        st, _ = self.req("POST", "/api/sessions/%s/message" % sid, {"text": "第三件事"})
        self.assertEqual(st, 200)
        self.assertEqual(self.e.status(sid), "running")
        self.assertTrue(wait_until(lambda: self.e.events(sid, ("compact",)), 5))
        self.assertTrue(wait_until(lambda: self.e.status(sid) == "done", 5))
        self.assertTrue(self.e.rt.wait_idle(sid, 5))
        last = self.e.mock.calls[-1]
        self.assertFalse(is_compress(last))
        users = [text_of(m["content"]) for m in last if m["role"] == "user"]
        self.assertTrue(users[0].startswith(SUMMARY_PREFIX + "摘要"))
        self.assertEqual(users[-1].split("\n\n")[-1], "第三件事")

    def test_model_failure_falls_back(self):
        # 模型压缩失败时用兜底摘要，照样算压了
        sid = self._two_turns()
        self.e.mock.script[:] = [{"error": "连不上"}]
        st, b = self.req("POST", "/api/sessions/%s/control" % sid, {"op": "compact"})
        self.assertEqual(b, {"ok": True, "started": True})
        self.assertTrue(self.e.rt.wait_idle(sid, 5))
        self.assertEqual(self.e.events(sid, ("compact",))[-1]["payload"], {"state": "done", "compacted": True})
        self.assertIn("用户说过的话", self.e.events(sid, ("summary",))[-1]["payload"]["text"])


class Title(ApiCase):
    def test_rename(self):
        sid = self.e.new_session()
        st, s = self.req("PATCH", "/api/sessions/%s" % sid, {"title": "  季度汇报  "})
        self.assertEqual(st, 200)
        self.assertEqual(s["title"], "季度汇报")
        self.assertEqual(self.e.rt.store.session(sid)["title"], "季度汇报")
        st, s = self.req("PATCH", "/api/sessions/%s" % sid, {"title": "字" * 60})
        self.assertEqual(st, 200)
        for bad in ("", "   ", "字" * 61, 5, None):
            st, b = self.req("PATCH", "/api/sessions/%s" % sid, {"title": bad})
            self.assertEqual(st, 400, bad)
            self.assertIn("标题", b["error"])
        self.assertEqual(self.req("PATCH", "/api/sessions/%s" % sid, {})[0], 400)
        self.assertEqual(self.req("PATCH", "/api/sessions/nope", {"title": "x"})[0], 404)
        self.assertEqual(self.e.rt.store.session(sid)["title"], "字" * 60)


class Theme(ApiCase):
    def test_theme_setting(self):
        st, b = self.req("GET", "/api/settings")
        self.assertEqual(b["config"]["ui"]["theme"], "auto")
        st, b = self.req("POST", "/api/settings", {"ui": {"theme": "nord"}})
        self.assertEqual((st, b), (200, {"ok": True}))
        self.assertEqual(self.req("GET", "/api/settings")[1]["config"]["ui"]["theme"], "nord")
        with open(os.path.join(self.e.home, "settings.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["ui"]["theme"], "nord")
        self.assertEqual(self.req("POST", "/api/settings", {"ui": {"theme": "catppuccin-mocha"}})[0], 200)
        for bad in ({"ui": {"theme": "Nord"}}, {"ui": {"theme": ""}}, {"ui": {"theme": "a" * 33}},
                    {"ui": {"theme": "nord\n"}}, {"ui": {"theme": 1}}, {"ui": {"font": "x"}}, {"ui": "nord"}):
            st, b = self.req("POST", "/api/settings", bad)
            self.assertEqual(st, 400, bad)
            self.assertTrue(b["error"])
        self.assertEqual(self.req("GET", "/api/settings")[1]["config"]["ui"]["theme"], "catppuccin-mocha")
        # 别的设置照常改，不动主题
        self.assertEqual(self.req("POST", "/api/settings", {"approval_mode": "manual"})[0], 200)
        self.assertEqual(self.e.rt.cfg.get("ui", "theme"), "catppuccin-mocha")


class ThemeStatic(ApiCase):
    def test_static_files(self):
        tmp = tempfile.mkdtemp(prefix="ca_ui_")
        self.addCleanup(shutil.rmtree, tmp, True)
        os.makedirs(os.path.join(tmp, "ui"))
        self.e.rt.app_dir = tmp
        self.assertEqual(self.req("GET", "/theme.js", token=False)[0], 404)
        self.assertEqual(self.req("GET", "/themes.css", token=False)[0], 404)
        with open(os.path.join(tmp, "ui", "theme.js"), "w") as f:
            f.write("document.documentElement.dataset.theme='auto';")
        with open(os.path.join(tmp, "ui", "themes.css"), "w") as f:
            f.write(":root{--bg:#fff}")
        st, body, hdr = self.req("GET", "/theme.js", token=False, raw=True)
        self.assertEqual((st, hdr["Content-Type"]), (200, "application/javascript; charset=utf-8"))
        self.assertIn(b"dataset.theme", body)
        st, body, hdr = self.req("GET", "/themes.css", token=False, raw=True)
        self.assertEqual((st, hdr["Content-Type"]), (200, "text/css; charset=utf-8"))
        self.assertEqual(body, b":root{--bg:#fff}")


class KernelNotice(ApiCase):
    env_kw = {"kernel": "auto"}
    script = [{"content": "好"}]

    def test_no_notice_in_session(self):
        rt = self.e.rt
        st, b = self.req("GET", "/api/state")
        self.assertEqual(b["kernel"], rt.kernel_name)
        self.assertIn("kernel_notice", b)
        if HAS_LG:
            self.assertEqual(b["kernel"], "langgraph")
            self.assertIsNone(b["kernel_notice"])
        else:
            self.assertEqual(b["kernel"], "builtin")
            self.assertIn("LangChain", b["kernel_notice"])
        sid = self.e.start("你好")
        self.assertTrue(rt.wait_idle(sid, 20))
        self.assertEqual(self.e.events(sid, ("notice",)), [])

    def test_builtin_has_null_notice(self):
        self.e.rt.kernel_notice = None
        self.assertIsNone(self.req("GET", "/api/state")[1]["kernel_notice"])


class Status(ApiCase):
    script = [{"content": "", "tool_calls": [("list_dir", {"path": "."})]}, {"content": "看过了"}]

    def test_status(self):
        sid = self.e.new_session()
        st, b = self.req("GET", "/api/sessions/%s/status" % sid)
        self.assertEqual(st, 200)
        self.assertEqual(set(b), {"model", "endpoint", "kernel", "context", "steps", "memory", "approval_mode", "compacting", "browser", "toolset",
                                  "mode"})       # mode：docs/两种模式.md 1.5
        self.assertEqual(b["kernel"], "builtin")
        self.assertEqual(b["steps"], 0)
        self.assertEqual(b["approval_mode"], "auto")
        self.assertEqual(b["context"]["limit"], context.input_budget(self.e.cfg))
        self.assertGreater(b["context"]["used"], 100)          # 空对话也有系统提示和工具定义
        self.assertEqual(set(b["memory"]), {"user", "workspace"})
        for k in ("user", "workspace"):
            self.assertEqual(set(b["memory"][k]), {"used", "limit"})
        self.assertEqual(b["memory"]["user"]["limit"], 500)
        self.assertEqual(b["memory"]["workspace"]["limit"], 800)
        self.assertIsInstance(b["model"], str)
        empty = b["context"]["used"]
        self.e.rt.send_message(sid, "看看文件夹" + "内容" * 200)
        self.assertTrue(self.e.rt.wait_idle(sid, 5))
        b = self.req("GET", "/api/sessions/%s/status" % sid)[1]
        self.assertEqual(b["steps"], 1)
        self.assertGreater(b["context"]["used"], empty + 300)
        # 和真正发出去的请求差不多（估算同一套办法）
        nxt = self.e.mock.calls[-1] + [{"role": "assistant", "content": "看过了"}]
        expect = context.estimate(nxt) + context.estimate_tools(self.e.rt._status_tools(self.e.ws, sid))
        self.assertLessEqual(abs(b["context"]["used"] - expect), 5)

    def test_memory_usage(self):
        from agent import memory
        sid = self.e.new_session()
        before = self.req("GET", "/api/sessions/%s/status" % sid)[1]["memory"]
        self.assertEqual(before["user"]["used"], 0)
        st, _ = self.req("POST", "/api/memories", {"target": "user", "text": "我是财务部的经理，汇报先给结论"})
        self.assertEqual(st, 200)
        after = self.req("GET", "/api/sessions/%s/status" % sid)[1]["memory"]
        self.assertGreater(after["user"]["used"], 0)
        st, m = self.req("GET", "/api/memories?workspace=%s" % self.e.ws["id"])
        self.assertEqual(after["user"]["used"], m["usage"]["user"]["used"])
        self.assertEqual(after["workspace"]["used"], m["usage"]["workspace"]["used"])
        self.assertEqual(self.req("GET", "/api/sessions/nope/status")[0], 404)


if __name__ == "__main__":
    unittest.main()
