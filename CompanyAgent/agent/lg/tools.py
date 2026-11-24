"""工具：把 agent/tools 注册表里的工具逐个包成 LangChain 工具；连接器用 langchain-mcp-adapters。

参数格式直接用现有的 JSON Schema（StructuredTool 的 args_schema 接受 dict，此时不做校验、原样传参，
见 langchain_core/tools/base.py）。参数校验、批准、记账都在 kernel.TaskKernel.run_tool 里，
这里的函数只负责"真的去做"。处理函数的签名和 ctx 语义不变：handler(ctx, **args)。"""
from __future__ import annotations
import asyncio, json, os, time

from langchain_core.tools import StructuredTool

from ..tools import Tool, ToolError, to_text


def to_langchain(kernel):
    """内核这次运行能用的全部工具 → StructuredTool 列表（按需加载时也全部登记给工具节点，给模型看哪些在
    kernel.filter_tools 里过滤，见 docs/借鉴开源项目.md 3.7）。"""
    return [as_langchain(kernel, t) for t in kernel.tools.values()]


def as_langchain(kernel, t):
    return StructuredTool(name=t.name, description=t.desc or t.name, args_schema=t.schema(), func=_runner(kernel, t))


def _runner(kernel, t):
    def run(**kwargs):
        # 返回完整文字；截断和"完整内容存成文件"由内核做（它知道动作 id）
        out = t.handler(kernel.ctx, **kwargs)
        img = getattr(out, "image", None)
        if img and kernel.ctx.call_id:
            kernel.images[kernel.ctx.call_id] = img       # 截图：记进 tool_result 的 image 字段
        ext = getattr(out, "extra", None)
        if ext and kernel.ctx.call_id:
            kernel.extras[kernel.ctx.call_id] = ext       # kb_search 的 hits、display：并进 tool_result 的 payload
        return to_text(out, 1 << 60)[0]
    run.__name__ = "run_" + t.name
    return run


# ---------------- 连接器（MCP） ----------------

def run_async(coro, timeout=None):
    """在当前线程里跑一个协程。我们的任务线程和 ToolNode 的工作线程里都没有事件循环。"""
    loop = asyncio.new_event_loop()
    try:
        if timeout:
            coro = asyncio.wait_for(coro, timeout)
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        loop.close()


def _connection(conf):
    cmd = conf.get("command") or []
    if isinstance(cmd, str):
        cmd = [cmd]
    if not cmd:
        raise ValueError("没有写启动命令（command）")
    # 适配器的 env：不写时只传一部分环境变量；写了就只用写的那些（连 PATH 都没有）。
    # 旧内核是"当前环境 + 配置里的"，这里保持一致。
    env = dict(os.environ)
    env.update({str(k): str(v) for k, v in (conf.get("env") or {}).items()})
    conn = {"transport": "stdio", "command": str(cmd[0]), "args": [str(x) for x in cmd[1:]], "env": env}
    if conf.get("cwd"):
        conn["cwd"] = conf["cwd"]
    return conn


def load_server(name, conf):
    """连一个 MCP 服务，返回它的 LangChain 工具列表（适配器生成的工具只有异步实现）。"""
    from langchain_mcp_adapters.client import MultiServerMCPClient
    conns = {name: _connection(conf)}
    try:
        client = MultiServerMCPClient(conns, handle_tool_errors=False)   # 让 isError 抛 ToolException，我们记为失败
    except TypeError:
        client = MultiServerMCPClient(conns)
    tools = run_async(client.get_tools(), timeout=float(conf.get("timeout") or 30))
    return client, list(tools)


def _mcp_text(out):
    if isinstance(out, tuple) and len(out) == 2:      # response_format="content_and_artifact"
        out = out[0]
    if isinstance(out, str):
        return out
    if isinstance(out, list):
        parts = []
        for b in out:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict):
                t = b.get("type")
                if t == "text":
                    parts.append(b.get("text") or "")
                elif t == "image":
                    parts.append("[图片]")
                else:
                    parts.append(json.dumps(b, ensure_ascii=False, default=str)[:2000])
            else:
                parts.append(str(getattr(b, "text", b)))
        return "\n".join(parts)
    return json.dumps(out, ensure_ascii=False, default=str)


def wrap_mcp(server, lc_tools, taken, timeout=60.0):
    """把适配器给的工具包成我们自己的 Tool：这样批准（风险一律"对外"）、记账、审计和业务工具走同一条路。"""
    from ..mcp import tool_name
    from langchain_core.tools import ToolException
    out = []
    for lt in lc_tools:
        schema = lt.args_schema if isinstance(lt.args_schema, dict) else {}
        props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        required = [r for r in (schema.get("required") or []) if isinstance(r, str)]
        name = tool_name(server, lt.name, taken)
        taken.add(name)
        desc = "[连接器 %s] %s" % (server, (lt.description or lt.name).strip())

        def handler(*pos, _lt=lt, **args):
            try:
                return _mcp_text(run_async(_lt.ainvoke(args), timeout=timeout))
            except ToolException as e:
                raise ToolError("连接器「%s」报错：%s" % (server, e))
            except (asyncio.TimeoutError, TimeoutError):
                raise ToolError("连接器「%s」响应超时（%d 秒）" % (server, timeout))
        out.append(Tool(name, desc[:1000], props, required, handler, "external", False, None))
    return out


def mcp_tools(host, ctx):
    """Runtime.mcp_tools 的新内核版本。启动失败的服务跳过并记一条 notice，一分钟内不重试。"""
    servers = host.rt.cfg.get("mcp_servers") or {}
    if not servers:
        return []
    out, taken = [], set()
    for name, conf in servers.items():
        if not isinstance(conf, dict):
            continue
        key = json.dumps(conf, sort_keys=True, ensure_ascii=False)
        with host.lock:
            ent = host.mcp.get(name)
            if ent and ent["key"] == key and ent["tools"] is None and time.time() - ent["failed_at"] < 60:
                continue
            cached = ent["tools"] if ent and ent["key"] == key else None
        if cached is None:
            try:
                _client, lts = load_server(name, conf)
                cached = lts
                with host.lock:
                    host.mcp[name] = {"key": key, "tools": lts, "failed_at": 0}
            except Exception as e:
                with host.lock:
                    host.mcp[name] = {"key": key, "tools": None, "failed_at": time.time()}
                if ctx is not None:
                    host.rt.emit(ctx.sid, "notice", {"text": "连接器「%s」启动失败，已跳过：%s: %s" % (name, type(e).__name__, e)})
                continue
        out += wrap_mcp(name, cached, taken, float(conf.get("timeout") or 60))
    return out
