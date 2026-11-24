"""连接器：最小的 MCP stdio 客户端。

启动子进程，按行收发 JSON-RPC：initialize → notifications/initialized → tools/list → tools/call。
读线程按 id 把响应交给等待的请求；每个请求最多等 30 秒。"""
from __future__ import annotations
import json, os, re, subprocess, threading
from collections import deque
from .tools import Tool, ToolError

PROTOCOL_VERSION = "2025-06-18"


class MCPError(Exception):
    pass


class MCPClient:
    def __init__(self, name, command, env=None, cwd=None, timeout=30.0):
        if isinstance(command, str):
            command = [command]
        if not command:
            raise MCPError("没有配置启动命令")
        self.name, self.command, self.cwd, self.timeout = name, list(command), cwd, timeout
        self.env = dict(os.environ)
        self.env.update({str(k): str(v) for k, v in (env or {}).items()})
        self.proc = None
        self.tools = []
        self.server_info = {}
        self._pending = {}
        self._next = 0
        self._lock = threading.Lock()
        self._wlock = threading.Lock()
        self._stderr = deque(maxlen=20)
        self._dead = False
        self._threads = []

    # ---- 生命周期 ----
    def start(self):
        try:
            self.proc = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         env=self.env, cwd=self.cwd, bufsize=0)
        except OSError as e:
            raise MCPError("无法启动 %s：%s" % (self.command[0], e))
        self._threads = [threading.Thread(target=self._read, daemon=True, name="mcp-" + self.name),
                         threading.Thread(target=self._drain, daemon=True, name="mcp-err-" + self.name)]
        for t in self._threads:
            t.start()
        try:
            init = self.request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                               "clientInfo": {"name": "CompanyAgent", "version": "0.1.0"}})
            self.server_info = init.get("serverInfo") or {}
            self.notify("notifications/initialized")
            self.tools = self.list_tools()
        except Exception:
            self.close()
            raise
        return self

    def alive(self):
        return self.proc is not None and self.proc.poll() is None and not self._dead

    def close(self):
        p = self.proc
        if p is None:
            return
        try:
            if p.stdin:
                p.stdin.close()
        except Exception:
            pass
        try:
            p.terminate()
            p.wait(2)
        except Exception:
            try:
                p.kill()
                p.wait(2)
            except Exception:
                pass
        for t in self._threads:
            t.join(1)
        for f in (p.stdout, p.stderr):
            try:
                if f:
                    f.close()
            except Exception:
                pass
        self._fail_all("连接器已关闭")

    # ---- 收发 ----
    def _read(self):
        try:
            for raw in self.proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue            # 服务把日志打到了 stdout：忽略
                if not isinstance(msg, dict):
                    continue
                if "method" in msg and "id" in msg:
                    # 服务端发来的请求：只回应 ping，其他一律说不支持
                    if msg["method"] == "ping":
                        self._send({"jsonrpc": "2.0", "id": msg["id"], "result": {}}, quiet=True)
                    else:
                        self._send({"jsonrpc": "2.0", "id": msg["id"],
                                    "error": {"code": -32601, "message": "client does not support " + str(msg["method"])}}, quiet=True)
                    continue
                if "id" in msg and ("result" in msg or "error" in msg):
                    with self._lock:
                        slot = self._pending.pop(msg["id"], None)
                    if slot:
                        slot["msg"] = msg
                        slot["ev"].set()
                # 通知（没有 id）不需要处理
        except Exception:
            pass
        self._dead = True
        self._fail_all("连接器进程已退出")

    def _drain(self):
        try:
            for raw in self.proc.stderr:
                self._stderr.append(raw.decode("utf-8", "replace").rstrip())
        except Exception:
            pass

    def _fail_all(self, why):
        with self._lock:
            slots = list(self._pending.values())
            self._pending.clear()
        for s in slots:
            s["why"] = why
            s["ev"].set()

    def _send(self, obj, quiet=False):
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            with self._wlock:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
        except (OSError, ValueError, AttributeError) as e:
            if not quiet:
                raise MCPError("连接器「%s」不可用：%s" % (self.name, e))

    def _tail(self):
        t = " | ".join(x for x in list(self._stderr)[-3:] if x)
        return ("（%s）" % t[:300]) if t else ""

    def request(self, method, params=None, timeout=None):
        if self._dead:
            raise MCPError("连接器「%s」已退出%s" % (self.name, self._tail()))
        with self._lock:
            self._next += 1
            rid = self._next
            slot = {"ev": threading.Event(), "msg": None}
            self._pending[rid] = slot
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        try:
            self._send(msg)
        except MCPError:
            with self._lock:
                self._pending.pop(rid, None)
            raise
        if not slot["ev"].wait(timeout or self.timeout):
            with self._lock:
                self._pending.pop(rid, None)
            raise MCPError("连接器「%s」响应超时（%d 秒）" % (self.name, timeout or self.timeout))
        m = slot["msg"]
        if m is None:
            raise MCPError("连接器「%s」：%s%s" % (self.name, slot.get("why") or "没有响应", self._tail()))
        if "error" in m:
            err = m["error"] or {}
            raise MCPError("连接器「%s」报错：%s" % (self.name, err.get("message") or err))
        return m.get("result") or {}

    def notify(self, method, params=None):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)

    # ---- MCP 方法 ----
    def list_tools(self):
        out, cursor = [], None
        for _ in range(20):         # 分页，最多 20 页
            r = self.request("tools/list", {"cursor": cursor} if cursor else {})
            out += r.get("tools") or []
            cursor = r.get("nextCursor")
            if not cursor:
                break
        return out

    def call_tool(self, name, arguments):
        r = self.request("tools/call", {"name": name, "arguments": arguments or {}})
        parts = []
        for c in r.get("content") or []:
            t = c.get("type")
            if t == "text":
                parts.append(c.get("text") or "")
            elif t == "resource":
                res = c.get("resource") or {}
                parts.append(res.get("text") or "[资源 %s]" % res.get("uri", ""))
            elif t == "image":
                parts.append("[图片]")
            else:
                parts.append("[%s]" % t)
        text = "\n".join(parts)
        if not text and r.get("structuredContent") is not None:
            text = json.dumps(r["structuredContent"], ensure_ascii=False)
        if r.get("isError"):
            raise MCPError(text or "连接器返回了错误")
        return text


def tool_name(server, name, taken=None):
    """mcp_<服务名>_<工具名>，只保留字母数字下划线，最长 64。"""
    base = re.sub(r"[^A-Za-z0-9_]", "_", "mcp_%s_%s" % (server, name))[:64]
    n, out = 1, base
    while taken is not None and out in taken:
        n += 1
        suffix = "_%d" % n
        out = base[:64 - len(suffix)] + suffix
    return out


def wrap_tools(server, client, rt=None):
    """把一个 MCP 服务的工具包成我们的 Tool。风险一律算"对外"，要走批准。"""
    out, taken = [], set()
    for t in client.tools:
        if not isinstance(t, dict) or not t.get("name"):
            continue
        schema = t.get("inputSchema") or {}
        props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        required = [r for r in (schema.get("required") or []) if isinstance(r, str)]
        name = tool_name(server, t["name"], taken)
        taken.add(name)

        desc = "[连接器 %s] %s" % (server, (t.get("description") or t["name"]).strip())
        out.append(Tool(name, desc[:1000], props, required, _handler(client, t["name"]), "external", False, None))
    return out


def _handler(client, orig):
    # 第一个位置参数是 ctx；用 *pos 接收，避免和工具自己的参数名冲突
    def handler(*pos, **args):
        try:
            return client.call_tool(orig, args)
        except MCPError as e:
            raise ToolError(str(e))
    return handler
