"""测试用的假 MCP 服务：stdio 上按行收发 JSON-RPC。
检查握手顺序（initialize → notifications/initialized → 其他请求），提供 echo、add、fail、slow 四个工具。"""
import json, sys, time

state = {"init": False, "ready": False}
TOOLS = [
    {"name": "echo", "description": "原样返回文字", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "add", "description": "两数相加", "inputSchema": {"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": ["number", "null"]}}}},
    {"name": "fail", "description": "总是失败", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "slow.tool", "description": "很慢", "inputSchema": {"type": "object", "properties": {}}},
]


def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


print("假服务启动中（这一行不是 JSON，客户端应忽略）", flush=True)
sys.stderr.write("fake mcp stderr log\n")
sys.stderr.flush()
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if method == "initialize":
        state["init"] = True
        send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": params.get("protocolVersion"), "capabilities": {"tools": {}},
                                                      "serverInfo": {"name": "fake", "version": "1"}}})
        continue
    if method == "notifications/initialized":
        state["ready"] = state["init"]
        send({"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info", "data": "hi"}})
        continue
    if mid is None:
        continue
    if not state["ready"]:
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32002, "message": "握手没完成"}})
        continue
    if method == "tools/list":
        # 分两页返回，测试翻页
        if params.get("cursor") == "p2":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS[2:]}})
        else:
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS[:2], "nextCursor": "p2"}})
    elif method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if name == "echo":
            send({"jsonrpc": "2.0", "id": 9999, "method": "ping"})     # 服务端反过来 ping 客户端
            send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": "回声：" + args.get("text", "")}]}})
        elif name == "add":
            send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": str(args.get("a", 0) + (args.get("b") or 0))}]}})
        elif name == "fail":
            send({"jsonrpc": "2.0", "id": mid, "result": {"isError": True, "content": [{"type": "text", "text": "后台系统报错：没有权限"}]}})
        elif name == "slow.tool":
            time.sleep(3)
            send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": "慢"}]}})
        else:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "没有这个工具"}})
    else:
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "不支持"}})
