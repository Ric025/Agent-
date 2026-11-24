"""JSON 模式回退（第 12 项）和定时任务（第 13 项）。"""
from __future__ import annotations
import json, threading, time, unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from helpers import Case, wait_until
from agent.llm import to_json_mode, OpenAIClient, parse_args, LLMError, JSON_MODE_RULES
from agent.scheduler import next_run, is_once

TOOLS = [{"type": "function", "function": {"name": "list_dir", "description": "列目录",
                                           "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}]


class JsonMode(unittest.TestCase):
    def test_rewrite(self):
        msgs = [{"role": "system", "content": "系统"}, {"role": "user", "content": "看看"},
                {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function",
                                                                        "function": {"name": "list_dir", "arguments": '{"path": "."}'}}]},
                {"role": "tool", "tool_call_id": "c1", "content": "a.txt"},
                {"role": "assistant", "content": "有 a.txt"}]
        out = to_json_mode(msgs, TOOLS)
        self.assertEqual([m["role"] for m in out], ["system", "user", "assistant", "user", "assistant"])
        self.assertIn(JSON_MODE_RULES.strip()[:10], out[0]["content"])
        self.assertIn("list_dir：列目录", out[0]["content"])
        self.assertEqual(json.loads(out[2]["content"]), {"tool": "list_dir", "args": {"path": "."}})
        self.assertEqual(out[3]["content"], "【工具结果】\na.txt")
        self.assertEqual(json.loads(out[4]["content"]), {"final": "有 a.txt"})
        self.assertTrue(all("tool_calls" not in m for m in out))

    def test_parse_args(self):
        self.assertEqual(parse_args('{"a": 1}'), ({"a": 1}, None))
        self.assertEqual(parse_args('好的 {"a": 1} 完'), ({"a": 1}, None))
        self.assertEqual(parse_args(""), ({}, None))
        self.assertEqual(parse_args("[1]")[0], None)


class FakeOpenAI(BaseHTTPRequestHandler):
    """假的 OpenAI 兼容服务：带 tools 的请求回 400（模拟不支持工具调用），否则按队列回复内容。"""
    replies, bodies = [], []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8"))
        FakeOpenAI.bodies.append(body)
        if "tools" in body:
            data = json.dumps({"error": {"message": "tool choice requires --enable-auto-tool-choice"}}).encode()
            self.send_response(400)
        else:
            text = FakeOpenAI.replies.pop(0)
            data = json.dumps({"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                               "usage": {"prompt_tokens": 1}}).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class Cfg:
    def __init__(self, data):
        self.data = data

    def get(self, *keys, default=None):
        cur = self.data
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur


class JsonFallback(unittest.TestCase):
    def setUp(self):
        self.srv = HTTPServer(("127.0.0.1", 0), FakeOpenAI)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        FakeOpenAI.replies[:] = []
        FakeOpenAI.bodies[:] = []
        url = "http://127.0.0.1:%d/v1" % self.srv.server_address[1]
        self.cfg = Cfg({"endpoints": [{"name": "fake", "base_url": url, "model": "m", "internal": True}],
                        "model": {"tool_mode": "auto", "max_output": 100, "timeout": 5}})

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def test_auto_falls_back_and_parses(self):
        FakeOpenAI.replies[:] = ['{"tool": "list_dir", "args": {"path": "报告"}}', '{"final": "看完了，有 3 个文件。"}']
        c = OpenAIClient(self.cfg)
        deltas = []
        r = c.chat([{"role": "system", "content": "s"}, {"role": "user", "content": "看看"}], tools=TOOLS, on_delta=deltas.append)
        self.assertTrue(c.json_mode)                       # 400 之后切到 JSON 模式
        self.assertEqual(len(r.tool_calls), 1)
        self.assertEqual((r.tool_calls[0]["name"], r.tool_calls[0]["arguments"]), ("list_dir", {"path": "报告"}))
        self.assertEqual(r.content, "")
        self.assertEqual(FakeOpenAI.bodies[-1]["response_format"], {"type": "json_object"})
        self.assertNotIn("tools", FakeOpenAI.bodies[-1])
        r2 = c.chat([{"role": "user", "content": "继续"}], tools=TOOLS, on_delta=deltas.append)
        self.assertEqual(r2.content, "看完了，有 3 个文件。")
        self.assertEqual(r2.tool_calls, [])
        self.assertEqual(deltas, ["看完了，有 3 个文件。"])
        self.assertEqual(len(FakeOpenAI.bodies), 3)        # 第二次直接用 JSON 模式，不再先试原生

    def test_native_mode_reports_error(self):
        self.cfg.data["model"]["tool_mode"] = "native"
        with self.assertRaises(LLMError):
            OpenAIClient(self.cfg).chat([{"role": "user", "content": "x"}], tools=TOOLS)

    def test_unreachable(self):
        self.cfg.data["endpoints"][0]["base_url"] = "http://127.0.0.1:1/v1"
        with self.assertRaises(LLMError) as cm:
            OpenAIClient(self.cfg).chat([{"role": "user", "content": "x"}])
        self.assertEqual(cm.exception.kind, "unreachable")


class Rules(unittest.TestCase):
    def test_next_run(self):
        base = time.mktime((2026, 11, 20, 10, 0, 0, 0, 0, -1))      # 2026-11-20 周五 10:00
        self.assertEqual(next_run("每30分钟", base), base + 1800)
        self.assertEqual(next_run("每2小时", base), base + 7200)
        self.assertEqual(time.localtime(next_run("每天 09:00", base))[:5], (2026, 11, 21, 9, 0))
        self.assertEqual(time.localtime(next_run("每天 11:30", base))[:5], (2026, 11, 20, 11, 30))
        self.assertEqual(time.localtime(next_run("每周一 09:00", base))[:5], (2026, 11, 23, 9, 0))
        self.assertEqual(time.localtime(next_run("每周五 09:00", base))[:5], (2026, 11, 27, 9, 0))
        self.assertEqual(time.localtime(next_run("2026-12-01 09:00", base))[:5], (2026, 12, 1, 9, 0))
        self.assertTrue(is_once("2026-12-01 09:00"))
        self.assertFalse(is_once("每天 09:00"))
        for bad in ("明天早上", "每0分钟", "2020-01-01 09:00", "每周八 09:00"):
            with self.assertRaises(ValueError):
                next_run(bad, base)


class Schedules(Case):
    def test_fire_due(self):
        e = self.env([{"content": "日报写好了"}, {"content": "提醒完成"}])
        st = e.rt.store
        past = time.time() - 5
        st.x("INSERT INTO schedules(id,workspace_id,prompt,spec,next_run,enabled,created) VALUES('t1',?,?,?,?,1,?)",
             (e.ws["id"], "整理今天的日报", "每天 18:00", past, past))
        st.x("INSERT INTO schedules(id,workspace_id,prompt,spec,next_run,enabled,created) VALUES('t2',?,?,?,?,1,?)",
             (e.ws["id"], "提醒我交报表", "2099-01-01 09:00", past, past))
        st.x("INSERT INTO schedules(id,workspace_id,prompt,spec,next_run,enabled,created) VALUES('t3',?,?,?,?,0,?)",
             (e.ws["id"], "停用的", "每天 18:00", past, past))
        fired = e.rt.scheduler.fire_due()
        self.assertEqual(len(fired), 2)
        for sid in fired:
            self.assertTrue(e.rt.wait_idle(sid, 5))
            self.assertEqual(e.status(sid), "done")
        prompts = sorted(e.events(sid, ("user_message",))[0]["payload"]["text"] for sid in fired)
        self.assertEqual(prompts, ["提醒我交报表", "整理今天的日报"])
        t1 = st.one("SELECT * FROM schedules WHERE id='t1'")
        t2 = st.one("SELECT * FROM schedules WHERE id='t2'")
        self.assertEqual(t1["enabled"], 1)
        self.assertGreater(t1["next_run"], time.time())
        self.assertEqual(t2["enabled"], 0)                  # 一次性任务触发后停用
        self.assertEqual(e.rt.scheduler.fire_due(), [])     # 不会重复触发

    def test_schedule_tool(self):
        e = self.env([{"content": "", "tool_calls": [("schedule_create", {"prompt": "每周汇总", "when": "每周一 09:00"})]},
                      {"content": "", "tool_calls": [("schedule_create", {"prompt": "x", "when": "随便什么时候"})]},
                      {"content": "设好了"}])
        sid = e.start("每周一提醒我")
        e.rt.wait_idle(sid, 5)
        res = e.results(sid)
        self.assertTrue(res[0]["ok"])
        self.assertFalse(res[1]["ok"])
        self.assertIn("看不懂时间规则", res[1]["content"])
        self.assertEqual(len(e.rt.store.q("SELECT * FROM schedules")), 1)


if __name__ == "__main__":
    unittest.main()
