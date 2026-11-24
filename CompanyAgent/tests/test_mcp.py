"""连接器（第 10 节第 14 项）：对假 MCP 服务完成握手、列工具、调用；接进执行循环。"""
from __future__ import annotations
import os, sys, time, unittest
from helpers import Case, HERE, wait_until
from agent.mcp import MCPClient, MCPError, tool_name, wrap_tools

FAKE = [sys.executable, os.path.join(HERE, "fake_mcp_server.py")]


class Client(unittest.TestCase):
    def test_handshake_list_call(self):
        c = MCPClient("demo", FAKE, timeout=5).start()
        try:
            self.assertEqual(c.server_info.get("name"), "fake")
            self.assertEqual([t["name"] for t in c.tools], ["echo", "add", "fail", "slow.tool"])   # 两页都拿到了
            self.assertEqual(c.call_tool("echo", {"text": "你好"}), "回声：你好")
            self.assertEqual(c.call_tool("add", {"a": 2, "b": 3}), "5")
            with self.assertRaises(MCPError) as cm:
                c.call_tool("fail", {})
            self.assertIn("没有权限", str(cm.exception))
            with self.assertRaises(MCPError):
                c.call_tool("nope", {})
            with self.assertRaises(MCPError) as cm:
                c.request("tools/call", {"name": "slow.tool", "arguments": {}}, timeout=0.5)
            self.assertIn("超时", str(cm.exception))
            self.assertTrue(c.alive())
        finally:
            c.close()
        self.assertFalse(c.alive())

    def test_bad_command(self):
        with self.assertRaises(MCPError):
            MCPClient("bad", ["/no/such/程序"]).start()
        with self.assertRaises(MCPError):      # 进程起来就退出
            MCPClient("bad", [sys.executable, "-c", "import sys; sys.exit(3)"], timeout=3).start()

    def test_names(self):
        self.assertEqual(tool_name("工单", "get-ticket"), "mcp____get_ticket")
        self.assertEqual(tool_name("crm", "slow.tool"), "mcp_crm_slow_tool")
        self.assertEqual(len(tool_name("s", "x" * 100)), 64)
        self.assertEqual(tool_name("s", "a.b", {"mcp_s_a_b"}), "mcp_s_a_b_2")


class InLoop(Case):
    def test_mcp_tool_in_task(self):
        tc = lambda *c: {"content": "", "tool_calls": list(c)}   # noqa: E731
        seen = {}

        def first(messages, tools):
            seen["tools"] = [t["function"]["name"] for t in tools]
            return tc(("mcp_demo_echo", {"text": "查工单 123"}), ("mcp_demo_add", {"a": 1, "b": None}))
        # 这里测的是"连接器的工具全部交给模型"：关掉按需加载（按需加载时连接器是单独一组，见 tests/test_toolsets.py）
        e = self.env([first, {"content": "工单查到了"}], mcp_servers={"demo": {"command": FAKE}, "broken": {"command": ["/no/such"]}},
                     tools={"lazy": "off"})
        sid = e.start("查一下工单")
        self.assertTrue(e.wait_status(sid, "waiting_approval", timeout=10))    # 连接器属于对外，自动模式要问
        self.assertIn("mcp_demo_echo", seen["tools"])
        self.assertIn("mcp_demo_slow_tool", seen["tools"])
        a = e.rt.store.actions(sid, states=("awaiting",))[0]
        self.assertEqual((a["tool"], a["risk"]), ("mcp_demo_echo", "external"))
        e.rt.decide(a["id"], True)
        self.assertTrue(wait_until(lambda: e.rt.store.actions(sid, states=("awaiting",)), 5))    # 第二个调用也要问
        e.rt.decide(e.rt.store.actions(sid, states=("awaiting",))[0]["id"], True)
        self.assertTrue(e.rt.wait_idle(sid, 10))
        self.assertEqual(e.status(sid), "done")
        self.assertEqual([r["content"] for r in e.results(sid)], ["回声：查工单 123", "1"])
        notices = [x["payload"]["text"] for x in e.events(sid, ("notice",))]
        self.assertTrue(any("broken" in n and "启动失败" in n for n in notices), notices)   # 坏的连接器跳过，不影响任务

    def test_no_servers(self):
        e = self.env([])
        self.assertEqual(e.rt.mcp_tools(None), [])


if __name__ == "__main__":
    unittest.main()
