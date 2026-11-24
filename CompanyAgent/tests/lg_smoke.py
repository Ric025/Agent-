"""新内核逐项自检：一步一步验证对 LangChain / LangGraph 接口的假设。

用法（在 CompanyAgent 目录下，装好 requirements.txt 之后）：
    python tests/lg_smoke.py
一般不用单独运行：tests/run_lg_tests.sh 会建虚拟环境、装依赖、跑这个脚本和全部单元测试。

每一步打印"[通过]"或"[失败] + 具体原因"，一步失败不影响后面的步骤。"[信息]"是供开发者参考的观察结果。
每一步对应 docs/内核迁移说明.md 里"没有运行验证过的地方"的一条。"""
from __future__ import annotations
import json, os, sys, tempfile, threading, time, traceback

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)

RESULTS = []


def step(name):
    def deco(fn):
        def run():
            t0 = time.time()
            try:
                info = fn()
                RESULTS.append((name, True, ""))
                print("[通过] %s（%.1f 秒）" % (name, time.time() - t0))
                if info:
                    for line in str(info).splitlines():
                        print("       [信息] " + line)
            except Exception as e:      # noqa: BLE001 —— 自检要接住一切，继续下一步
                tb = traceback.format_exc().strip().splitlines()
                RESULTS.append((name, False, "%s: %s" % (type(e).__name__, e)))
                print("[失败] %s：%s: %s" % (name, type(e).__name__, e))
                for line in tb[-8:]:
                    print("       " + line)
            sys.stdout.flush()
        run.__name__ = fn.__name__
        STEPS.append(run)
        return run
    return deco


STEPS = []


def check(cond, why):
    if not cond:
        raise AssertionError(why)


# ---------------- 公用的小零件 ----------------

def fake(script):
    """假模型：agent.llm.MockLLM 的脚本 + agent.lg.models.ScriptedChatModel。"""
    from agent.llm import MockLLM
    from agent.lg.models import ScriptedChatModel
    m = MockLLM(script)
    return ScriptedChatModel(m), m


def tc(*calls, content=""):
    return {"content": content, "tool_calls": list(calls)}


def echo_tool(log=None):
    from langchain_core.tools import StructuredTool

    def echo(text=""):
        if log is not None:
            log.append((threading.current_thread().name, text, time.time()))
        time.sleep(0.05)
        return "回声：" + text
    return StructuredTool(name="echo", description="原样返回文字",
                          args_schema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
                          func=echo)


def mem_saver():
    from langgraph.checkpoint.memory import InMemorySaver
    return InMemorySaver()


def cfg(tid):
    return {"configurable": {"thread_id": tid}, "recursion_limit": 1000}


def interrupts_of(graph, config):
    snap = graph.get_state(config)
    out = list(getattr(snap, "interrupts", None) or ())
    for t in getattr(snap, "tasks", None) or ():
        for i in getattr(t, "interrupts", None) or ():
            if all(getattr(i, "id", None) != getattr(x, "id", None) for x in out):
                out.append(i)
    return snap, out


# ---------------- 步骤 ----------------

@step("01 Python 版本和已安装的包")
def s01():
    from agent import lg
    v = lg.versions()
    check(sys.version_info >= (3, 11), "需要 Python 3.11 或更新，现在是 %s" % sys.version.split()[0])
    return "Python %s\n%s" % (sys.version.split()[0], json.dumps(v, ensure_ascii=False))


@step("02 逐个导入用到的类和函数")
def s02():
    names = [
        ("langchain.agents", "create_agent"),
        ("langchain.agents.middleware", "AgentMiddleware"), ("langchain.agents.middleware", "ModelRequest"),
        ("langchain.agents.middleware", "ModelResponse"), ("langchain.agents.middleware", "InterruptOnConfig"),
        ("langchain.agents.middleware", "HumanInTheLoopMiddleware"), ("langchain.agents.middleware", "SummarizationMiddleware"),
        ("langchain.agents.middleware", "TodoListMiddleware"), ("langchain.agents.middleware", "ModelFallbackMiddleware"),
        ("langchain.agents.middleware", "ModelRetryMiddleware"), ("langchain.agents.middleware", "ToolRetryMiddleware"),
        ("langchain.agents.middleware", "ModelCallLimitMiddleware"), ("langchain.agents.middleware", "ToolCallLimitMiddleware"),
        ("langchain_core.messages", "AIMessage"), ("langchain_core.messages", "ToolMessage"),
        ("langchain_core.messages", "convert_to_openai_messages"), ("langchain_core.tools", "StructuredTool"),
        ("langchain_core.tools", "ToolException"), ("langchain_core.callbacks", "BaseCallbackHandler"),
        ("langchain_core.language_models.fake_chat_models", "GenericFakeChatModel"),
        ("langchain_core.utils.function_calling", "convert_to_openai_tool"),
        ("langgraph.types", "interrupt"), ("langgraph.types", "Command"),
        ("langgraph.checkpoint.sqlite", "SqliteSaver"), ("langgraph.checkpoint.memory", "InMemorySaver"),
        ("langchain_openai", "ChatOpenAI"),
        ("agent.lg.models", "ScriptedChatModel"), ("agent.lg.kernel", "TaskKernel"), ("agent.lg.middleware", "build"),
        ("agent.lg.host", "Host"), ("agent.lg.tools", "to_langchain"),
    ]
    bad = []
    import importlib
    for mod, name in names:
        try:
            m = importlib.import_module(mod)
            getattr(m, name)
        except Exception as e:
            bad.append("%s.%s（%s: %s）" % (mod, name, type(e).__name__, e))
    check(not bad, "导入失败：" + "；".join(bad))
    from agent import lg
    ok, why = lg.available()
    check(ok, "agent.lg.available() 说不能用：" + why)


@step("03 批准中间件支持按条件批准（InterruptOnConfig.when）")
def s03():
    from langchain.agents.middleware import InterruptOnConfig
    keys = sorted(getattr(InterruptOnConfig, "__annotations__", {}))
    check("when" in keys, "InterruptOnConfig 没有 when 字段，现有字段：%s" % keys)
    return "字段：%s" % keys


@step("04 假模型：bind_tools、工具调用、解析失败的参数")
def s04():
    from langchain_core.messages import HumanMessage, AIMessage
    model, mock = fake([tc(("echo", {"text": "hi"})), tc(("echo", "{坏的 JSON"))])
    bound = model.bind_tools([echo_tool()])
    ai = bound.invoke([HumanMessage(content="你好")])
    check(isinstance(ai, AIMessage), "invoke 返回的不是 AIMessage：%r" % type(ai))
    check(ai.tool_calls and ai.tool_calls[0]["name"] == "echo", "没有工具调用：%r" % ai)
    check(mock.calls[0][-1] == {"role": "user", "content": "你好"} or mock.calls[0][-1].get("content") == "你好",
          "假模型收到的消息不对：%r" % mock.calls[0])
    names = [t["function"]["name"] for t in (model.specs or [])]
    check(names == ["echo"], "bind_tools 后工具清单不对：%r" % names)
    ai2 = bound.invoke([HumanMessage(content="再来")])
    check(not ai2.tool_calls and ai2.invalid_tool_calls, "解析失败的参数应该进 invalid_tool_calls：%r" % ai2)
    # 官方文档推荐的写法也试一下：GenericFakeChatModel 子类 + bind_tools 返回 self
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    class Doc(GenericFakeChatModel):
        def bind_tools(self, tools, **kw):
            return self
    d = Doc(messages=iter([AIMessage(content="", tool_calls=[{"name": "echo", "args": {"text": "x"}, "id": "c1", "type": "tool_call"}])]))
    out = d.bind_tools([echo_tool()]).invoke("hi")
    check(out.tool_calls, "文档写法的假模型没有返回工具调用")


@step("05 StructuredTool 直接用 JSON Schema（dict）做参数格式")
def s05():
    from langchain_core.messages import ToolMessage
    t = echo_tool()
    out = t.invoke({"name": "echo", "args": {"text": "你好"}, "id": "c1", "type": "tool_call"})
    check(isinstance(out, ToolMessage), "用 ToolCall 调用应返回 ToolMessage，实际 %r" % type(out))
    check(out.content == "回声：你好", "内容不对：%r" % out.content)
    out2 = t.invoke({"text": "直接"})
    check(out2 == "回声：直接", "用参数 dict 调用应返回字符串，实际 %r" % out2)


@step("06 create_agent 建图并跑通：模型 → 工具 → 模型")
def s06():
    from langchain.agents import create_agent
    from langchain_core.messages import AIMessage, ToolMessage, HumanMessage
    model, mock = fake([tc(("echo", {"text": "a"})), {"content": "好了"}])
    g = create_agent(model=model, tools=[echo_tool()], system_prompt="你是测试", checkpointer=mem_saver())
    out = g.invoke({"messages": [HumanMessage(content="开始")]}, cfg("t06"))
    msgs = out["messages"]
    kinds = [type(m).__name__ for m in msgs]
    check(kinds == ["HumanMessage", "AIMessage", "ToolMessage", "AIMessage"], "消息序列不对：%s" % kinds)
    check(msgs[2].content == "回声：a", "工具结果不对：%r" % msgs[2].content)
    check(mock.calls[0][0]["role"] == "system", "系统提示没有放在最前：%r" % mock.calls[0][0])
    return "节点：%s" % sorted(getattr(g, "nodes", {}).keys())


@step("07 中间件钩子：wrap_model_call / wrap_tool_call / after_model 收到的对象")
def s07():
    from langchain.agents import create_agent
    from langchain.agents.middleware import AgentMiddleware, ModelResponse
    from langchain_core.messages import HumanMessage, ToolMessage, SystemMessage
    seen = {}

    class Probe(AgentMiddleware):
        def wrap_model_call(self, request, handler):
            seen["req_fields"] = sorted(k for k in ("model", "messages", "system_message", "tools", "state", "runtime",
                                                     "tool_choice", "model_settings") if hasattr(request, k))
            seen["sys_type"] = type(request.system_message).__name__
            seen["has_override"] = hasattr(request, "override")
            resp = handler(request)
            seen["resp_type"] = type(resp).__name__
            seen["resp_result"] = [type(m).__name__ for m in getattr(resp, "result", [])]
            return resp

        def wrap_tool_call(self, request, handler):
            seen["tool_req"] = {"tool_call": dict(request.tool_call), "tool": getattr(request.tool, "name", None),
                                "state_type": type(getattr(request, "state", None)).__name__}
            out = handler(request)
            seen["tool_out"] = type(out).__name__
            return out

        def after_model(self, state, runtime):
            seen.setdefault("after", 0)
            seen["after"] += 1
            return None
    model, _ = fake([tc(("echo", {"text": "a"})), {"content": "好"}])
    g = create_agent(model=model, tools=[echo_tool()], system_prompt="测试", middleware=[Probe()], checkpointer=mem_saver())
    g.invoke({"messages": [HumanMessage(content="开始")]}, cfg("t07"))
    check(seen.get("resp_type") == "ModelResponse", "handler 返回的不是 ModelResponse：%r" % seen.get("resp_type"))
    check(seen.get("resp_result") == ["AIMessage"], "ModelResponse.result 不对：%r" % seen.get("resp_result"))
    check(seen.get("has_override"), "ModelRequest 没有 override()")
    check(seen.get("tool_out") == "ToolMessage", "工具 handler 返回的不是 ToolMessage：%r" % seen.get("tool_out"))
    check(seen.get("tool_req", {}).get("tool") == "echo", "ToolCallRequest.tool 不对：%r" % seen.get("tool_req"))
    check(seen.get("after") == 2, "after_model 应该调用 2 次，实际 %r" % seen.get("after"))
    return json.dumps(seen, ensure_ascii=False, default=str)


@step("08 同一条回复里的多个工具调用：是否并行、按什么顺序开始")
def s08():
    from langchain.agents import create_agent
    from langchain_core.messages import HumanMessage
    log = []
    model, _ = fake([tc(("echo", {"text": "1"}), ("echo", {"text": "2"}), ("echo", {"text": "3"})), {"content": "好"}])
    g = create_agent(model=model, tools=[echo_tool(log)], checkpointer=mem_saver())
    out = g.invoke({"messages": [HumanMessage(content="开始")]}, cfg("t08"))
    order = [x[1] for x in sorted(log, key=lambda x: x[2])]
    threads = sorted({x[0] for x in log})
    tools = [m.tool_call_id for m in out["messages"] if type(m).__name__ == "ToolMessage"]
    calls = [c["id"] for c in out["messages"][1].tool_calls]
    check(len(log) == 3, "应该执行 3 次，实际 %d" % len(log))
    return "开始顺序 %s；线程 %s；结果顺序与调用顺序一致：%s" % (order, threads, tools == calls)


def _hitl_graph(model, saver, log=None, when=None):
    from langchain.agents import create_agent
    from langchain.agents.middleware import HumanInTheLoopMiddleware
    conf = {"allowed_decisions": ["approve", "reject"], "description": "需要批准"}
    if when:
        conf["when"] = when
    return create_agent(model=model, tools=[echo_tool(log)], checkpointer=saver,
                        middleware=[HumanInTheLoopMiddleware(interrupt_on={"echo": conf})])


@step("09 批准：when 条件、中断内容、批准后恢复")
def s09():
    from langchain_core.messages import HumanMessage
    from langgraph.types import Command
    log, asked = [], []

    def when(req):
        asked.append(dict(req.tool_call))
        return req.tool_call["args"].get("text") == "危险"
    model, mock = fake([tc(("echo", {"text": "安全"}), ("echo", {"text": "危险"})), {"content": "好"}])
    g = _hitl_graph(model, mem_saver(), log, when)
    c = cfg("t09")
    out = g.invoke({"messages": [HumanMessage(content="开始")]}, c)
    check("__interrupt__" in out, "invoke 的结果里没有 __interrupt__，键：%s" % list(out))
    snap, intrs = interrupts_of(g, c)
    check(len(intrs) == 1, "应该有 1 个中断，实际 %d" % len(intrs))
    v = intrs[0].value
    check(isinstance(v, dict) and len(v.get("action_requests") or []) == 1, "中断内容不对：%r" % v)
    check(v["action_requests"][0]["args"] == {"text": "危险"}, "被拦下的不是那个危险调用：%r" % v)
    check(not log, "批准前不应执行任何工具（批准中间件在 after_model 统一判定），实际执行了 %r" % log)
    check(getattr(intrs[0], "id", None), "Interrupt 没有 id")
    info = "snapshot.interrupts 存在：%s；next=%s；when 调用次数=%d" % (hasattr(snap, "interrupts"), snap.next, len(asked))
    g.invoke(Command(resume={intrs[0].id: {"decisions": [{"type": "approve"}]}}), c)
    check(sorted(x[1] for x in log) == ["危险", "安全"], "批准后两个调用都应执行：%r" % log)
    return info + "；恢复后 when 又被调用（总 %d 次）" % len(asked)


@step("10 批准：拒绝后模型收到说明并再被调用")
def s10():
    from langchain_core.messages import HumanMessage, ToolMessage
    from langgraph.types import Command
    log = []
    model, mock = fake([tc(("echo", {"text": "x"})), {"content": "好的，不做了"}])
    g = _hitl_graph(model, mem_saver(), log, lambda r: True)
    c = cfg("t10")
    g.invoke({"messages": [HumanMessage(content="开始")]}, c)
    _, intrs = interrupts_of(g, c)
    out = g.invoke(Command(resume={intrs[0].id: {"decisions": [{"type": "reject", "message": "用户拒绝了"}]}}), c)
    check(not log, "拒绝后工具不应执行")
    tm = [m for m in out["messages"] if isinstance(m, ToolMessage)]
    check(tm and tm[0].status == "error" and "用户拒绝了" in tm[0].content, "拒绝的 ToolMessage 不对：%r" % tm)
    check(len(mock.calls) == 2, "拒绝后应该再问一次模型，实际模型调用 %d 次" % len(mock.calls))
    return "拒绝文字：%s" % tm[0].content


@step("11 一个节点里两次 interrupt()，按中断 id 逐个恢复")
def s11():
    from langchain.agents import create_agent
    from langchain.agents.middleware import AgentMiddleware
    from langchain_core.messages import HumanMessage
    from langgraph.types import interrupt, Command
    got = []

    class Ask(AgentMiddleware):
        def after_model(self, state, runtime):
            a = interrupt({"q": 1})
            b = interrupt({"q": 2})
            got.append((a, b))
            return None
    model, _ = fake([{"content": "好"}])
    g = create_agent(model=model, tools=[], middleware=[Ask()], checkpointer=mem_saver())
    c = cfg("t11")
    g.invoke({"messages": [HumanMessage(content="开始")]}, c)
    _, i1 = interrupts_of(g, c)
    check(i1 and i1[0].value == {"q": 1}, "第一个中断不对：%r" % i1)
    g.invoke(Command(resume={i1[0].id: "A"}), c)
    _, i2 = interrupts_of(g, c)
    check(i2 and i2[0].value == {"q": 2}, "第二个中断不对：%r" % i2)
    g.invoke(Command(resume={i2[0].id: "B"}), c)
    check(got == [("A", "B")], "两次恢复值没有按顺序拿到：%r" % got)
    return "两个中断的 id 相同：%s" % (i1[0].id == i2[0].id)


@step("12 SQLite 检查点：关掉连接再打开，中断还在，能接着恢复")
def s12():
    import sqlite3
    from langchain_core.messages import HumanMessage
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.types import Command
    d = tempfile.mkdtemp()
    path = os.path.join(d, "ck.db")
    log = []
    model, _ = fake([tc(("echo", {"text": "x"})), {"content": "好"}])
    conn = sqlite3.connect(path, check_same_thread=False)
    saver = SqliteSaver(conn)
    if hasattr(saver, "setup"):
        saver.setup()
    g = _hitl_graph(model, saver, log, lambda r: True)
    c = cfg("t12")
    g.invoke({"messages": [HumanMessage(content="开始")]}, c)
    conn.close()
    conn2 = sqlite3.connect(path, check_same_thread=False)
    saver2 = SqliteSaver(conn2)
    g2 = _hitl_graph(model, saver2, log, lambda r: True)
    snap, intrs = interrupts_of(g2, c)
    check(intrs, "重新打开后找不到中断；next=%s" % (snap.next,))
    g2.invoke(Command(resume={intrs[0].id: {"decisions": [{"type": "approve"}]}}), c)
    check(log, "恢复后工具没有执行")
    snap = g2.get_state(c)
    check(snap.next == (), "应该跑完了，next=%s" % (snap.next,))


@step("13 停在中断上时给新输入：中断被丢弃，带新消息重新开始")
def s13():
    from langchain_core.messages import HumanMessage, ToolMessage
    log = []
    model, mock = fake([tc(("echo", {"text": "x"})), {"content": "按新要求来"}])
    g = _hitl_graph(model, mem_saver(), log, lambda r: True)
    c = cfg("t13")
    g.invoke({"messages": [HumanMessage(content="开始")]}, c)
    ai = g.get_state(c).values["messages"][-1]
    cid = ai.tool_calls[0]["id"]
    g.invoke({"messages": [ToolMessage(content="未执行：用户更改了要求", tool_call_id=cid, name="echo", status="error"),
                           HumanMessage(content="改了")]}, c)
    snap, intrs = interrupts_of(g, c)
    check(not intrs, "旧中断还在：%r" % intrs)
    check(not log, "旧调用不应执行")
    check(len(mock.calls) == 2 and mock.calls[1][-1]["content"] == "改了", "模型没有收到新消息：%r" % mock.calls[-1:])
    from agent.context import validate_messages
    check(validate_messages(mock.calls[1]) == [], "消息序列不合法：%s" % validate_messages(mock.calls[1]))


@step("14 节点抛异常后 invoke(None) 重跑失败的节点")
def s14():
    from langchain_core.messages import HumanMessage
    from agent.llm import LLMError
    n = {"i": 0}

    def flaky(messages, tools):
        n["i"] += 1
        if n["i"] == 1:
            raise LLMError("连不上")
        return {"content": "好了"}
    model, mock = fake([flaky, flaky])
    from langchain.agents import create_agent
    g = create_agent(model=model, tools=[], checkpointer=mem_saver())
    c = cfg("t14")
    try:
        g.invoke({"messages": [HumanMessage(content="开始")]}, c)
        raise AssertionError("第一次应该抛异常")
    except LLMError:
        pass
    snap = g.get_state(c)
    info = "失败后 next=%s" % (snap.next,)
    out = g.invoke(None, c)
    check(out["messages"][-1].content == "好了", "重跑后没有拿到回复：%r" % out["messages"][-1])
    return info


@step("15 模型调用上限：exit_behavior=error 抛异常，invoke(None) 后计数重置")
def s15():
    from langchain.agents import create_agent
    from langchain.agents.middleware import ModelCallLimitMiddleware
    from langchain_core.messages import HumanMessage
    model, mock = fake([tc(("echo", {"text": "1"}))] * 5 + [{"content": "完"}])
    g = create_agent(model=model, tools=[echo_tool()], checkpointer=mem_saver(),
                     middleware=[ModelCallLimitMiddleware(run_limit=2, exit_behavior="error")])
    c = cfg("t15")
    name = None
    try:
        g.invoke({"messages": [HumanMessage(content="开始")]}, c)
    except Exception as e:
        name = type(e).__name__
    check(name == "ModelCallLimitExceededError", "应抛 ModelCallLimitExceededError，实际 %r" % name)
    check(len(mock.calls) == 2, "到上限前应调用 2 次，实际 %d" % len(mock.calls))
    try:
        g.invoke(None, c)
    except Exception as e:
        name = type(e).__name__
    check(len(mock.calls) == 4, "invoke(None) 后应再调用 2 次（单次运行的计数重置），实际共 %d" % len(mock.calls))


@step("16 压缩：SummarizationMiddleware 的触发、摘要消息格式、自定义 token_counter")
def s16():
    from langchain.agents import create_agent
    from langchain.agents.middleware import SummarizationMiddleware
    from langchain_core.messages import HumanMessage
    sm, smock = fake([{"content": "这是摘要"}] * 5)
    model, mock = fake([tc(("echo", {"text": "x" * 400}))] * 3 + [{"content": "完"}])
    counted = []

    def counter(msgs):
        msgs = list(msgs)
        counted.append(len(msgs))
        return sum(len(str(getattr(m, "content", ""))) for m in msgs) // 3
    mw = SummarizationMiddleware(model=sm, trigger=("tokens", 200), keep=("tokens", 80), token_counter=counter,
                                 summary_prompt="总结：\n{messages}")
    g = create_agent(model=model, tools=[echo_tool()], middleware=[mw], checkpointer=mem_saver())
    out = g.invoke({"messages": [HumanMessage(content="开始")]}, cfg("t16"))
    check(smock.calls, "摘要模型没有被调用；token_counter 被调用 %d 次" % len(counted))
    first = out["messages"][0]
    check(type(first).__name__ == "HumanMessage" and (first.additional_kwargs or {}).get("lc_source") == "summarization",
          "压缩后第一条应是带 lc_source=summarization 的 HumanMessage：%r" % first)
    from agent.context import validate_messages
    for i, call in enumerate(mock.calls):
        check(validate_messages(call) == [], "第 %d 次请求消息序列不合法：%s" % (i + 1, validate_messages(call)))
    return "摘要消息开头：%r；摘要请求：%r" % (str(first.content)[:60], str(smock.calls[0])[:120])


@step("17 计划：TodoListMiddleware 的 write_todos 返回 Command，状态取值")
def s17():
    from langchain.agents import create_agent
    from langchain.agents.middleware import AgentMiddleware, TodoListMiddleware
    from langchain_core.messages import HumanMessage
    seen = {}

    class Probe(AgentMiddleware):
        def wrap_tool_call(self, request, handler):
            out = handler(request)
            seen["type"] = type(out).__name__
            seen["update"] = getattr(out, "update", None)
            return out
    todos = [{"content": "看表", "status": "in_progress"}, {"content": "写报告", "status": "pending"}]
    model, mock = fake([tc(("write_todos", {"todos": todos})), {"content": "好"}])
    g = create_agent(model=model, tools=[], middleware=[Probe(), TodoListMiddleware(system_prompt="计划提示", tool_description="写计划")],
                     checkpointer=mem_saver())
    out = g.invoke({"messages": [HumanMessage(content="开始")]}, cfg("t17"))
    check(seen.get("type") == "Command", "write_todos 返回的不是 Command：%r" % seen.get("type"))
    check(isinstance(seen.get("update"), dict) and seen["update"].get("todos"), "Command.update 里没有 todos：%r" % seen.get("update"))
    check(out.get("todos") and out["todos"][0]["status"] == "in_progress", "状态里的 todos 不对：%r" % out.get("todos"))
    sysmsg = mock.calls[0][0]
    check("计划提示" in json.dumps(sysmsg, ensure_ascii=False), "系统提示里没有计划中间件的说明")
    return "系统提示格式：%s" % type(sysmsg.get("content")).__name__


@step("18 流式：graph.stream(stream_mode=[updates, messages]) 的块结构（只是观察，新内核不依赖它）")
def s18():
    from langchain.agents import create_agent
    from langchain_core.messages import HumanMessage
    model, mock = fake([{"content": "一二三"}])
    g = create_agent(model=model, tools=[], checkpointer=mem_saver())
    kinds = []
    for chunk in g.stream({"messages": [HumanMessage(content="开始")]}, cfg("t18"), stream_mode=["updates", "messages"]):
        kinds.append(type(chunk).__name__ + ":" + (str(chunk[0]) if isinstance(chunk, tuple) else "?"))
    check(kinds, "stream 没有产出任何块")
    return "stream 块：%s" % kinds[:8]


@step("18b 流式片段：模型的 callbacks 里挂 DeltaHandler，invoke 时收到 on_llm_new_token")
def s18b():
    from langchain_core.messages import HumanMessage
    from agent.lg.models import DeltaHandler

    class K:
        sid = "s"
        worker = None

        class rt:
            got = []

            @staticmethod
            def emit(sid, t, p, store=True):
                K.rt.got.append(p["text"])
    k = K()
    h = DeltaHandler(k)
    k.worker = threading.current_thread()
    m2, _ = fake([{"content": "片段"}])
    m2 = m2.model_copy(update={"callbacks": [h]})
    m2.invoke([HumanMessage(content="x")])
    check("".join(K.rt.got) == "片段", "DeltaHandler 收到的片段：%r" % K.rt.got)


@step("19 ChatOpenAI：按我们的参数建对象（不联网）")
def s19():
    from agent.config import Config
    from agent.lg.models import chat_openai
    d = tempfile.mkdtemp()
    c = Config(d)
    c.update({"model": {"thinking": False}})
    ep = {"name": "x", "base_url": "http://127.0.0.1:9/v1", "model": "m", "api_key": "", "internal": True}
    m = chat_openai(c, ep, None)
    check(getattr(m, "extra_body", None) == {"chat_template_kwargs": {"enable_thinking": False}}, "extra_body 不对：%r" % getattr(m, "extra_body", None))
    check(getattr(m, "streaming", None) is True, "streaming 不是 True")
    check(getattr(m, "http_client", None) is not None, "http_client 没有设置")
    b = m.bind_tools([{"type": "function", "function": {"name": "f", "description": "d", "parameters": {"type": "object", "properties": {}}}}])
    return "max_tokens=%r timeout=%r use_responses_api=%r bind_tools→%s" % (
        getattr(m, "max_tokens", None), getattr(m, "request_timeout", None), getattr(m, "use_responses_api", None), type(b).__name__)


@step("20 graph.invoke 有 durability 参数")
def s20():
    import inspect
    from langchain.agents import create_agent
    model, _ = fake([])
    g = create_agent(model=model, tools=[])
    params = list(inspect.signature(g.invoke).parameters)
    return "durability 参数存在：%s；参数：%s" % ("durability" in params, params)


@step("21 连接器：langchain-mcp-adapters 连假 MCP 服务，列工具、调用、报错")
def s21():
    from agent.lg.tools import load_server, wrap_mcp
    from agent.tools import ToolError
    conf = {"command": [sys.executable, os.path.join(HERE, "fake_mcp_server.py")]}
    _client, lts = load_server("demo", conf)
    names = [t.name for t in lts]
    check("echo" in names, "没有列出 echo：%r" % names)
    tools = {t.name: t for t in wrap_mcp("demo", lts, set())}
    out = tools["mcp_demo_echo"].handler(None, text="你好")
    check(out == "回声：你好", "调用结果不对：%r" % out)
    try:
        tools["mcp_demo_fail"].handler(None)
        raise AssertionError("fail 工具应该报错")
    except ToolError as e:
        check("没有权限" in str(e), "错误内容不对：%s" % e)
    return "工具：%s；args_schema 类型：%s" % (names, type(lts[0].args_schema).__name__)


@step("22 端到端：运行时用新内核跑一个要批准的任务")
def s22():
    from agent.config import Config
    from agent.runtime import Runtime
    from agent.llm import MockLLM
    d = tempfile.mkdtemp()
    ws = os.path.join(d, "ws")
    os.makedirs(ws)
    with open(os.path.join(ws, "a.txt"), "w", encoding="utf-8") as f:
        f.write("旧")
    c = Config(os.path.join(d, "home"))
    c.update({"kernel": "langgraph", "model": {"retries": 0}})
    mock = MockLLM([tc(("write_file", {"path": "a.txt", "content": "新"})), {"content": "改好了"}])
    rt = Runtime(c, lambda: mock)
    try:
        check(rt.kernel_name == "langgraph", "运行时没有选新内核：%s" % rt.kernel_name)
        w = rt.store.add_workspace("t", ws)
        sid = rt.store.add_session(w["id"])["id"]
        rt.send_message(sid, "改 a.txt")
        end = time.time() + 15
        while time.time() < end and rt.store.session(sid)["status"] != "waiting_approval":
            time.sleep(0.05)
        check(rt.store.session(sid)["status"] == "waiting_approval", "没有停下来等批准：%s" % rt.store.session(sid)["status"])
        a = rt.store.actions(sid, states=("awaiting",))[0]
        rt.decide(a["id"], True)
        check(rt.wait_idle(sid, 15), "任务没有结束")
        s = rt.store.session(sid)
        check(s["status"] == "done", "最终状态 %s（%s）" % (s["status"], s["meta"]))
        with open(os.path.join(ws, "a.txt"), encoding="utf-8") as f:
            check(f.read() == "新", "文件没有改成功")
        return "事件：%s" % [e["type"] for e in rt.store.events(sid)]
    finally:
        rt.shutdown()


def main():
    print("新内核逐项自检  %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    print("Python %s  %s" % (sys.version.split()[0], sys.executable))
    print("-" * 60)
    for fn in STEPS:
        fn()
    print("-" * 60)
    bad = [r for r in RESULTS if not r[1]]
    print("共 %d 步，通过 %d，失败 %d" % (len(RESULTS), len(RESULTS) - len(bad), len(bad)))
    for name, _, why in bad:
        print("  失败：%s —— %s" % (name, why))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
