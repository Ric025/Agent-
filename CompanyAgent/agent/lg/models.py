"""模型接入：真实模型用 langchain-openai 的 ChatOpenAI；测试和演示模式用假模型。
另外负责两件小事：把模型的流式片段转成界面的 assistant_delta；在 LangChain 消息和 OpenAI 格式之间互转。"""
from __future__ import annotations
import json, threading
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, SystemMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult as LCChatResult


# ---------------- 流式片段 ----------------

class DeltaHandler(BaseCallbackHandler):
    """收到模型的流式片段就推给界面。

    只转发"当前这次模型调用"所在线程产生的片段：被用户打断而丢下的旧调用还在后台跑，
    它的片段不能混进新回复里。见 kernel.TaskKernel.call_model。"""

    def __init__(self, kernel):
        super().__init__()
        self.kernel = kernel

    def on_llm_new_token(self, token, **kwargs):     # 签名：token, *, chunk=None, run_id, parent_run_id=None, **kwargs
        k = self.kernel
        if not token or not isinstance(token, str):
            return
        if k.worker is None or threading.current_thread() is not k.worker:
            return
        k.rt.emit(k.sid, "assistant_delta", {"text": token, "base": getattr(k, "delta_base", None)}, store=False)


# ---------------- 消息格式 ----------------

def text_of(content):
    """消息内容可能是字符串，也可能是内容块列表。取出其中的文字。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for b in content:
            if isinstance(b, str):
                out.append(b)
            elif isinstance(b, dict):
                if b.get("type") in ("text", None) and isinstance(b.get("text"), str):
                    out.append(b["text"])
            else:
                t = getattr(b, "text", None)
                if isinstance(t, str):
                    out.append(t)
        return "".join(out)
    return str(content)


def _norm_content(c):
    """全是文字的内容块列表合成一个字符串（假模型和估算都按字符串处理）。"""
    if isinstance(c, list) and all(isinstance(b, dict) and b.get("type") == "text" for b in c):
        return "".join(b.get("text") or "" for b in c)
    return c


def to_openai(messages):
    """LangChain 消息 → OpenAI Chat Completions 格式的 dict 列表。"""
    from langchain_core.messages import convert_to_openai_messages
    out = []
    for m in convert_to_openai_messages(list(messages)):
        m = dict(m)
        if "content" in m:
            m["content"] = _norm_content(m["content"])
        if m.get("role") == "assistant" and m.get("tool_calls") and not m.get("content"):
            m["content"] = None
        out.append(m)
    return out


def from_openai(m):
    """OpenAI 格式的一条消息（来自 context.project）→ LangChain 消息。用于把旧内核留下的任务接过来。"""
    role = m.get("role")
    if role == "user":
        return HumanMessage(content=m.get("content") or "")
    if role == "assistant":
        calls = []
        for tc in m.get("tool_calls") or []:
            f = tc.get("function") or {}
            try:
                args = json.loads(f.get("arguments") or "{}")
            except Exception:
                args = {}
            calls.append({"name": f.get("name") or "", "args": args if isinstance(args, dict) else {},
                          "id": tc.get("id"), "type": "tool_call"})
        return AIMessage(content=m.get("content") or "", tool_calls=calls)
    if role == "tool":
        return ToolMessage(content=m.get("content") or "", tool_call_id=m.get("tool_call_id") or "")
    if role == "system":
        return SystemMessage(content=m.get("content") or "")
    raise ValueError("不认识的消息角色：%s" % role)


def human_from_event(p):
    """账本里的 user_message → HumanMessage。图片用 OpenAI 的 image_url 内容块；附了文件时文字后面加一行文件说明。"""
    from ..context import user_text
    text = user_text(p)
    imgs = p.get("images") or []
    if not imgs:
        return HumanMessage(content=text)
    parts = [{"type": "text", "text": text}]
    for url in imgs:
        parts.append({"type": "image_url", "image_url": {"url": url}})
    return HumanMessage(content=parts)


# ---------------- 假模型 ----------------

class ScriptedChatModel(GenericFakeChatModel):
    """LangChain 自带的 GenericFakeChatModel 不实现 bind_tools（官方测试文档原话），这里写一个最小的子类。

    它把请求转成 OpenAI 格式交给一个"后端"对象（agent/llm.py 里的 MockLLM，或任何有同样 chat() 方法的对象），
    再把后端的回复转成 AIMessage。这样旧内核的测试脚本、--mock 演示模式在新内核上原样可用，
    假模型收到的每次请求也照样记在 MockLLM.calls 里，可以检查消息顺序是否合法。"""

    backend: Any = None
    specs: Any = None          # 最近一次 bind_tools 绑定的工具（OpenAI 格式）

    def __init__(self, backend, **kwargs):
        kwargs.setdefault("disable_streaming", True)     # 不走父类的 _stream（它读 messages 迭代器）
        super().__init__(messages=iter(()), backend=backend, specs=None, **kwargs)

    @property
    def _llm_type(self):
        return "scripted-fake-chat-model"

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        from langchain_core.utils.function_calling import convert_to_openai_tool
        self.specs = [convert_to_openai_tool(t) for t in tools] if tools else None
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        oa = to_openai(messages)

        def on_delta(t):
            if run_manager is not None and t:
                run_manager.on_llm_new_token(t)
        res = self.backend.chat(oa, tools=self.specs or None, on_delta=on_delta)
        return LCChatResult(generations=[ChatGeneration(message=result_to_ai(res))])


def result_to_ai(res):
    """agent.llm.ChatResult → AIMessage。解析失败的参数放进 invalid_tool_calls（和 ChatOpenAI 的行为一致）。"""
    calls, bad = [], []
    for c in res.tool_calls or []:
        if isinstance(c.get("arguments"), dict) and not c.get("error"):
            calls.append({"name": c.get("name") or "", "args": c["arguments"], "id": c.get("id"), "type": "tool_call"})
        else:
            bad.append({"name": c.get("name") or "", "args": c.get("raw") or "", "id": c.get("id"),
                        "error": c.get("error") or "参数不是合法的 JSON 对象", "type": "invalid_tool_call"})
    u = res.usage or {}
    pi, po = int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)
    return AIMessage(content=res.content or "", tool_calls=calls, invalid_tool_calls=bad,
                     usage_metadata={"input_tokens": pi, "output_tokens": po, "total_tokens": pi + po},
                     response_metadata={"model_name": res.endpoint or ""})


# ---------------- 真实模型 ----------------

def chat_openai(cfg, ep, callbacks=None, streaming=True):
    """按一个端点的配置建 ChatOpenAI。参数名见 reference.langchain.com 的 ChatOpenAI 页面。"""
    from langchain_openai import ChatOpenAI
    mc = cfg.get("model") or {}
    kw = dict(model=ep["model"], base_url=ep["base_url"], api_key=ep.get("api_key") or "EMPTY",
              temperature=mc.get("temperature", 0.3), timeout=mc.get("timeout", 180),
              max_tokens=mc.get("max_output", 4096), max_retries=0,       # 重试交给 ModelRetryMiddleware
              streaming=bool(streaming), use_responses_api=False)        # vLLM 只有 Chat Completions
    if streaming:
        kw["stream_usage"] = True
    if ep.get("kind", "vllm") == "vllm" and mc.get("thinking") is not None:
        # vLLM 的私有参数必须放 extra_body（官方文档的警告：不要放 model_kwargs）
        kw["extra_body"] = {"chat_template_kwargs": {"enable_thinking": bool(mc.get("thinking"))}}
    if ep.get("internal", True):
        # 内网端点不走系统代理（和旧内核一致）。httpx.Client(trust_env=False) 不读 HTTP(S)_PROXY 环境变量
        import httpx
        kw["http_client"] = httpx.Client(trust_env=False, timeout=mc.get("timeout", 180))
    if callbacks:
        kw["callbacks"] = list(callbacks)
    return ChatOpenAI(**kw)


class NoModel(Exception):
    pass


class ChatAdapter:
    """把 LangChain 模型包成 agent.llm 的 chat 接口（手动整理时 context.compress 用；不带工具、不流式）。"""

    def __init__(self, model):
        self.model = model

    def chat(self, messages, tools=None, on_delta=None, cancel=None, max_tokens=None, temperature=None):
        from ..llm import Cancelled
        from types import SimpleNamespace
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        ai = self.model.invoke([from_openai(m) for m in messages])
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        return SimpleNamespace(content=text_of(getattr(ai, "content", "")), tool_calls=[], usage={}, endpoint="")


def review_model(rt):
    """后台回顾用的模型（docs/记忆设计.md 5.3）。运行时带了 review_llm_factory 时用它
    （LangChain 模型直接用，MockLLM 一类包成 ScriptedChatModel）；否则按第一个端点建一个不流式的 ChatOpenAI。"""
    if getattr(rt, "review_custom", False):
        obj = rt.review_llm_factory()
        return obj if isinstance(obj, BaseChatModel) else ScriptedChatModel(obj)
    eps = [e for e in (rt.cfg.get("endpoints") or []) if e.get("base_url") and e.get("model")]
    if not eps:
        raise NoModel("没有配置模型端点")
    return chat_openai(rt.cfg, eps[0], None, streaming=False)


def build(rt, kernel):
    """返回 (主模型, [后备模型], 压缩用的模型)。

    运行时带了自定义 llm_factory（测试、--mock）时：工厂给的是 LangChain 模型就直接用，
    否则（MockLLM 一类）包成 ScriptedChatModel。"""
    cbs = [DeltaHandler(kernel)]
    if getattr(rt, "llm_factory_custom", False):
        obj = rt.llm_factory()
        if isinstance(obj, BaseChatModel):
            return obj.model_copy(update={"callbacks": cbs}), [], obj
        return ScriptedChatModel(obj, callbacks=cbs), [], ScriptedChatModel(obj)
    eps = [e for e in (rt.cfg.get("endpoints") or []) if e.get("base_url") and e.get("model")]
    if not eps:
        raise NoModel("没有配置模型端点")
    main = chat_openai(rt.cfg, eps[0], cbs)
    # 端点可以是不同的模型（docs/操控电脑.md 第 7 节）：换台和"按需要选端点"都在 kernel.model_call 里做，
    # 只在够格的端点之间换，所以这里不再给 ModelFallbackMiddleware 后备模型
    kernel.endpoint_models = {}
    kernel.endpoint_callbacks = cbs
    summary = chat_openai(rt.cfg, eps[0], None, streaming=False)
    return main, [], summary
