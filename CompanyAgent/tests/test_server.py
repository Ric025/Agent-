"""HTTP 接口（第 15 项）：令牌、Host、Origin、建工作区、建任务发消息、读任务、批准、事件流、文件访问边界。"""
from __future__ import annotations
import http.client, json, os, stat, threading, time, unittest
from urllib.parse import quote
from helpers import Case, wait_until
from agent.server import make_server


def tc(*calls):
    return {"content": "", "tool_calls": list(calls)}


class Api(Case):
    def setUp(self):
        super().setUp()
        self.e = self.env([tc(("write_file", {"path": "a.txt", "content": "新内容"})), {"content": "改好了"}])
        self.e.cfg.update({"endpoints": [{"name": "A", "base_url": "http://10.0.0.1/v1", "model": "m", "api_key": "sk-secret-123456"}]})
        self.srv = make_server(self.e.rt, 0)
        self.srv.start()
        self.port = self.srv.port
        self.tok = self.srv.token

    def tearDown(self):
        self.srv.stop()
        super().tearDown()

    def req(self, method, path, body=None, token=True, host=None, origin=None, raw=False):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.putrequest(method, path, skip_host=True)
        c.putheader("Host", host or "127.0.0.1:%d" % self.port)
        if token:
            c.putheader("X-Token", self.tok if token is True else token)
        if origin:
            c.putheader("Origin", origin)
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            c.putheader("Content-Type", "application/json")
            c.putheader("Content-Length", str(len(data)))
        c.endheaders(data)
        r = c.getresponse()
        payload = r.read()
        c.close()
        if raw:
            return r.status, payload, dict(r.getheaders())
        return r.status, (json.loads(payload.decode("utf-8")) if payload else None)

    def test_security(self):
        self.assertEqual(self.req("GET", "/api/state", token=False)[0], 403)
        self.assertEqual(self.req("GET", "/api/state", token="wrong")[0], 403)
        self.assertEqual(self.req("GET", "/api/state", host="evil.com")[0], 403)
        self.assertEqual(self.req("GET", "/api/state", host="127.0.0.1:1")[0], 403)
        self.assertEqual(self.req("GET", "/api/state", origin="http://evil.com")[0], 403)
        self.assertEqual(self.req("GET", "/api/state", origin="http://localhost:%d" % self.port)[0], 200)
        self.assertEqual(self.req("GET", "/api/state", host="localhost:%d" % self.port)[0], 200)
        self.assertEqual(self.req("GET", "/api/state?t=" + self.tok, token=False)[0], 200)     # 事件流、下载链接用查询参数
        self.assertEqual(self.req("GET", "/", token=False, host="evil.com")[0], 403)
        st, body = self.req("GET", "/api/nope")
        self.assertEqual((st, "error" in body), (404, True))
        mode = stat.S_IMODE(os.stat(os.path.join(self.e.home, "token")).st_mode)
        self.assertEqual(mode, 0o600)
        with open(os.path.join(self.e.home, "token")) as f:
            self.assertEqual(f.read(), self.tok)

    def test_static(self):
        st, body, hdr = self.req("GET", "/?t=x", token=False, raw=True)
        if os.path.isfile(os.path.join(self.e.rt.app_dir, "ui", "index.html")):
            self.assertEqual(st, 200)
            self.assertIn("text/html", hdr["Content-Type"])
        else:
            self.assertEqual(st, 404)
        self.assertEqual(self.req("GET", "/secret.txt", token=False)[0], 404)
        self.assertEqual(self.req("GET", "/../agent/server.py", token=False)[0], 404)

    def test_full_flow_with_events(self):
        # 事件流：先连上，收集事件
        got, ready = [], threading.Event()

        def listen():
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            c.request("GET", "/api/events?since=0&t=" + self.tok)
            r = c.getresponse()
            ready.set()
            try:
                while True:
                    line = r.fp.readline()
                    if not line:
                        break
                    line = line.decode("utf-8").strip()
                    if line.startswith("data: "):
                        got.append(json.loads(line[6:]))
            except Exception:
                pass
        threading.Thread(target=listen, daemon=True).start()
        self.assertTrue(ready.wait(5))

        st, body = self.req("POST", "/api/workspaces", {"name": "x", "path": "/no/such/dir"})
        self.assertEqual(st, 400)
        os.makedirs(os.path.join(self.e.tmp, "ws2"))
        with open(os.path.join(self.e.tmp, "ws2", "a.txt"), "w", encoding="utf-8") as f:
            f.write("旧内容")
        st, ws = self.req("POST", "/api/workspaces", {"name": "财务", "path": os.path.join(self.e.tmp, "ws2")})
        self.assertEqual(st, 200)
        st, ws2 = self.req("PATCH", "/api/workspaces/" + ws["id"], {"instructions": "金额用万元", "egress": "open"})
        self.assertEqual((ws2["instructions"], ws2["egress"]), ("金额用万元", "open"))
        self.assertEqual(self.req("PATCH", "/api/workspaces/" + ws["id"], {"egress": "everywhere"})[0], 400)
        st, state = self.req("GET", "/api/state")
        self.assertEqual(len(state["workspaces"]), 2)
        self.assertEqual(state["last_seq"], self.e.rt.store.last_seq())
        self.assertEqual(state["endpoints"], [{"name": "A", "base_url": "http://10.0.0.1/v1", "model": "m"}])
        self.assertNotIn("sk-secret", json.dumps(state))

        st, s = self.req("POST", "/api/sessions", {"workspace_id": ws["id"], "text": "把 a.txt 改成新内容"})
        self.assertEqual(st, 200)
        sid = s["id"]
        self.assertEqual(s["title"], "把 a.txt 改成新内容")
        self.assertTrue(wait_until(lambda: self.req("GET", "/api/sessions/" + sid)[1]["session"]["status"] == "waiting_approval"))
        st, detail = self.req("GET", "/api/sessions/" + sid)
        self.assertEqual(len(detail["pending"]), 1)
        aid = detail["pending"][0]["id"]
        self.assertIn("approval_requested", [x["type"] for x in detail["events"]])
        st, lst = self.req("GET", "/api/sessions?workspace=" + ws["id"])
        self.assertEqual([x["id"] for x in lst], [sid])
        self.assertEqual(self.req("POST", "/api/actions/%s/decide" % aid, {"allow": "yes"})[0], 400)
        self.assertEqual(self.req("POST", "/api/actions/%s/decide" % aid, {"allow": True})[0], 200)
        self.assertEqual(self.req("POST", "/api/actions/%s/decide" % aid, {"allow": True})[0], 409)
        self.assertTrue(self.e.rt.wait_idle(sid, 5))
        st, detail = self.req("GET", "/api/sessions/" + sid)
        self.assertEqual(detail["session"]["status"], "done")
        self.assertEqual(detail["artifacts"][0]["path"], "a.txt")
        st, arts = self.req("GET", "/api/artifacts?workspace=" + ws["id"])
        self.assertEqual([(a["path"], a["version"]) for a in arts], [("a.txt", 1)])
        # 继续对话、控制
        self.e.mock.script.append({"content": "还有什么？"})
        self.assertEqual(self.req("POST", "/api/sessions/%s/message" % sid, {"text": "谢谢"})[0], 200)
        self.assertEqual(self.req("POST", "/api/sessions/%s/message" % sid, {"text": "  "})[0], 400)
        self.assertTrue(self.e.rt.wait_idle(sid, 5))
        self.assertEqual(self.req("POST", "/api/sessions/%s/control" % sid, {"op": "explode"})[0], 400)
        self.assertEqual(self.req("POST", "/api/sessions/%s/control" % sid, {"op": "pause"})[0], 200)
        # 事件流收到了已存事件和实时事件
        self.assertTrue(wait_until(lambda: sum(1 for x in got if x["type"] == "assistant_message" and x["session_id"] == sid) >= 3, 5))
        types = [x["type"] for x in got if x["session_id"] == sid]
        for t in ("user_message", "status", "approval_requested", "approval_decided", "tool_started", "tool_result", "artifact"):
            self.assertIn(t, types)
        self.assertTrue(any(x["type"] == "assistant_delta" and x["seq"] is None for x in got))
        seqs = [x["seq"] for x in got if x["seq"] is not None]
        self.assertEqual(seqs, sorted(set(seqs)))       # 不重复、不乱序
        # 断线重连：从某个 seq 之后补发
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        c.request("GET", "/api/events?since=%d&t=%s" % (seqs[-3], self.tok))
        r = c.getresponse()
        replay = []
        while len(replay) < 2:
            line = r.fp.readline().decode("utf-8").strip()
            if line.startswith("data: "):
                replay.append(json.loads(line[6:])["seq"])
        c.close()
        self.assertEqual(replay, seqs[-2:])
        # 删除任务
        self.assertEqual(self.req("DELETE", "/api/sessions/" + sid)[0], 200)
        self.assertEqual(self.req("GET", "/api/sessions/" + sid)[0], 404)

    def test_files_and_boundaries(self):
        wid = self.e.ws["id"]
        self.e.write("报告/q3.md", "# 三季度\n收入 100")
        self.e.write("page.html", "<script>alert(1)</script>")
        with open(os.path.join(self.e.tmp, "secret.txt"), "w") as f:
            f.write("机密数据")
        os.symlink(os.path.join(self.e.tmp, "secret.txt"), os.path.join(self.e.root, "link.txt"))
        st, d = self.req("GET", "/api/files?workspace=%s&path=." % wid)
        names = {i["name"]: i for i in d["items"]}
        self.assertTrue(names["报告"]["dir"])
        st, body, hdr = self.req("GET", "/api/file?workspace=%s&path=%s" % (wid, quote("报告/q3.md")), raw=True)
        self.assertEqual((st, body.decode("utf-8")), (200, "# 三季度\n收入 100"))
        self.assertIn("text/plain", hdr["Content-Type"])
        st, body, hdr = self.req("GET", "/api/file?workspace=%s&path=page.html" % wid, raw=True)
        self.assertIn("text/plain", hdr["Content-Type"])           # 网页文件不在界面的源下执行
        st, body, hdr = self.req("GET", "/api/file?workspace=%s&path=%s&download=1" % (wid, quote("报告/q3.md")), raw=True)
        self.assertIn("attachment", hdr["Content-Disposition"])
        for bad in ("../secret.txt", quote(os.path.join(self.e.tmp, "secret.txt")), "link.txt", "/etc/passwd"):
            st, body, _ = self.req("GET", "/api/file?workspace=%s&path=%s" % (wid, bad), raw=True)
            self.assertNotEqual(st, 200, bad)
            self.assertNotIn("机密数据", body.decode("utf-8", "replace"))
        self.assertEqual(self.req("GET", "/api/files?workspace=%s&path=.." % wid)[0], 400)
        st, pv = self.req("GET", "/api/preview?workspace=%s&path=%s" % (wid, quote("报告/q3.md")))
        self.assertEqual((pv["kind"], pv["truncated"]), ("text", False))
        self.assertIn("收入 100", pv["text"])
        self.assertEqual(self.req("GET", "/api/preview?workspace=%s&path=../secret.txt" % wid)[0], 400)
        self.assertEqual(self.req("GET", "/api/file?path=a.txt")[0], 400)

    def test_memories_schedules_kb_settings(self):
        wid = self.e.ws["id"]
        st, m = self.req("POST", "/api/memories", {"text": "汇报用万元", "workspace_id": wid})
        st, g = self.req("POST", "/api/memories", {"text": "我是财务总监", "workspace_id": None})
        st, ms = self.req("GET", "/api/memories?workspace=" + wid)
        self.assertEqual(sorted(x["text"] for x in ms["memories"]), ["我是财务总监", "汇报用万元"])
        self.assertEqual(self.req("DELETE", "/api/memories/" + m["id"])[0], 200)
        self.assertEqual(len(self.req("GET", "/api/memories?workspace=" + wid)[1]["memories"]), 1)

        st, body = self.req("POST", "/api/schedules", {"workspace_id": wid, "prompt": "写日报", "when": "明天早上"})
        self.assertEqual(st, 400)
        self.assertIn("看不懂时间规则", body["error"])
        st, sc = self.req("POST", "/api/schedules", {"workspace_id": wid, "prompt": "写日报", "when": "每天 18:00"})
        self.assertEqual((st, sc["enabled"]), (200, True))
        self.assertGreater(sc["next_run"], time.time())
        st, sc2 = self.req("PATCH", "/api/schedules/" + sc["id"], {"enabled": False})
        self.assertFalse(sc2["enabled"])
        self.assertEqual(len(self.req("GET", "/api/schedules?workspace=" + wid)[1]), 1)
        self.assertEqual(self.req("DELETE", "/api/schedules/" + sc["id"])[0], 200)

        self.e.write("资料/手册.txt", "错误码 E-42 表示电源故障。")
        st, r = self.req("POST", "/api/kb", {"workspace_id": wid, "path": "资料"})
        self.assertEqual(r["indexed"], 1)
        st, docs = self.req("GET", "/api/kb?workspace=" + wid)
        self.assertEqual(docs[0]["path"], "资料/手册.txt")
        self.assertEqual(self.req("DELETE", "/api/kb/" + docs[0]["id"])[0], 200)
        self.assertEqual(self.req("GET", "/api/kb?workspace=" + wid)[1], [])
        self.assertEqual(self.req("POST", "/api/kb", {"workspace_id": wid, "path": "../"})[0], 400)

        st, s = self.req("GET", "/api/settings")
        self.assertTrue(s["config"]["endpoints"][0]["api_key"].startswith("••••"))
        self.assertNotIn("sk-secret", json.dumps(s))
        tools = {t["name"]: t for t in s["tools"]}
        self.assertEqual((tools["delete_file"]["risk"], tools["delete_file"]["setting"]), ("delete", "default"))
        # 回传打码的密钥：保留原值
        ep = dict(s["config"]["endpoints"][0], model="m2")
        self.assertEqual(self.req("POST", "/api/settings", {"endpoints": [ep], "approval_mode": "full"})[0], 200)
        self.assertEqual(self.e.rt.cfg.get("endpoints")[0]["api_key"], "sk-secret-123456")
        self.assertEqual(self.e.rt.cfg.get("endpoints")[0]["model"], "m2")
        self.assertEqual(self.e.rt.cfg.get("approval_mode"), "full")
        self.assertEqual(self.req("POST", "/api/settings", {"approval_mode": "yolo"})[0], 400)
        self.assertEqual(self.req("POST", "/api/settings", {"home": "/tmp"})[0], 400)
        self.assertEqual(self.req("POST", "/api/settings/tool", {"tool": "run_python", "setting": "deny"})[0], 200)
        self.assertEqual(self.e.rt.store.tool_setting("run_python"), "deny")
        self.assertEqual(self.req("POST", "/api/settings/tool", {"tool": "run_python", "setting": "maybe"})[0], 400)
        st, ping = self.req("POST", "/api/settings/test")
        self.assertEqual((st, ping[0]["ok"]), (200, True))
        self.assertEqual(self.req("GET", "/api/skills")[0], 200)
        st, tpl = self.req("GET", "/api/templates")
        self.assertEqual(st, 200)
        self.assertIsInstance(tpl, list)
        st, audit = self.req("GET", "/api/audit?limit=5")
        self.assertIsInstance(audit, list)
        self.assertEqual(self.req("DELETE", "/api/workspaces/" + wid)[0], 200)
        self.assertTrue(os.path.isdir(self.e.root))           # 只删记录


if __name__ == "__main__":
    unittest.main()
