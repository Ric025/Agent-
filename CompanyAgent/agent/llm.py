"""模型接入：OpenAI 兼容的 Chat Completions。端点池、失败换台、流式、工具调用。
服务端不支持原生工具调用时，自动退到"JSON 模式"：让模型每次输出一个 JSON 对象。"""
from __future__ import annotations
import json, time, urllib.request, urllib.error, socket, threading


class LLMError(Exception):
    def __init__(self, msg, kind="error"):
        super().__init__(msg)
        self.kind = kind          # unreachable | http | bad_response


class Cancelled(Exception):
    pass


class _ToolsUnsupported(Exception):
    pass


VISION_HINTS = ("image", "vision", "multimodal", "multi-modal", "图片", "图像")


class ChatResult:
    def __init__(self, content="", tool_calls=None, finish="stop", usage=None, reasoning="", endpoint=""):
        self.content = content or ""
        self.tool_calls = tool_calls or []   # [{"id","name","arguments":dict|None,"raw":str,"error":str|None}]
        self.finish = finish
        self.usage = usage or {}
        self.reasoning = reasoning or ""
        self.endpoint = endpoint
        self.model = ""


def _has_images(messages):
    for m in messages or []:
        c = m.get("content") if isinstance(m, dict) else None
        if isinstance(c, list) and any(isinstance(p, dict) and p.get("type") == "image_url" for p in c):
            return True
    return False


def parse_args(raw):
    """把模型给的参数字符串解析成 dict。返回 (dict|None, 错误信息|None)。"""
    if isinstance(raw, dict):
        return raw, None
    s = (raw or "").strip()
    if not s:
        return {}, None
    for cand in (s, s[s.find("{"): s.rfind("}") + 1] if "{" in s and "}" in s else s):
        try:
            v = json.loads(cand)
            if isinstance(v, dict):
                return v, None
        except Exception:
            continue
    return None, "参数不是合法的 JSON 对象"


JSON_MODE_RULES = """
你不能直接调用工具。每次回复必须只输出一个 JSON 对象，不要输出别的文字：
- 要用工具时：{"tool": "工具名", "args": {参数}}
- 可以直接回答用户时：{"final": "给用户的回答"}
一次只能用一个工具。可用工具如下（name：说明；参数为 JSON Schema）：
"""


def _tools_as_text(tools):
    lines = []
    for t in tools:
        f = t["function"]
        lines.append("- %s：%s 参数：%s" % (f["name"], f["description"],
                     json.dumps(f["parameters"], ensure_ascii=False, separators=(",", ":"))))
    return "\n".join(lines)


def to_json_mode(messages, tools):
    """把带工具调用的消息历史改写成纯文本协议。"""
    out = []
    for m in messages:
        role = m["role"]
        if role == "system":
            out.append({"role": "system", "content": m["content"] + "\n" + JSON_MODE_RULES + _tools_as_text(tools)})
        elif role == "assistant" and m.get("tool_calls"):
            c = m["tool_calls"][0]["function"]
            try:
                args = json.loads(c["arguments"])
            except Exception:
                args = {}
            out.append({"role": "assistant", "content": json.dumps({"tool": c["name"], "args": args}, ensure_ascii=False)})
            for extra in m["tool_calls"][1:]:
                pass  # JSON 模式下一次只有一个工具调用
        elif role == "assistant":
            out.append({"role": "assistant", "content": json.dumps({"final": m.get("content") or ""}, ensure_ascii=False)})
        elif role == "tool":
            out.append({"role": "user", "content": "【工具结果】\n" + (m.get("content") or "")})
        else:
            out.append(m)
    return out


class OpenAIClient:
    """一个任务用一个实例，这样它会粘在同一个端点上。

    端点列表里可以是不同的模型（docs/操控电脑.md 第 7 节）：每次请求按需要（工具、图片、大小）选第一个活着、够格的端点；
    一次运行里只往上换、不来回切（begin_run 时重新从头选）；换了模型在 note 里留一句话，由调用方发 notice。
    失败换台只在够格的端点之间换。rt 为 None 时（不知道能力）退回"同一个模型的故障池"。"""

    def __init__(self, cfg, rt=None):
        self.cfg = cfg
        self.rt = rt
        self.idx = 0
        self.json_mode = cfg.get("model", "tool_mode") == "json"
        self.begin_run()

    def begin_run(self):
        """新的一次运行：从头选端点，换模型的提示重新算。"""
        self.sticky = 0
        self.noted = False
        self.note = None
        self.corrected = []        # 这次发现实际不支持看图的端点名（调用方发 notice）

    def take_note(self):
        """取走一条要告诉用户的话（换了模型、发现某个模型看不了图）。没有返回 None。"""
        out = []
        for name in self.corrected:
            out.append("模型「%s」实际不支持看图，已记下，以后不再把图片发给它。" % name)
        self.corrected = []
        if self.note:
            out.append(self.note)
            self.note = None
        return "\n".join(out) or None

    def _caps(self):
        if self.rt is None:
            return None
        from . import capability
        return capability.get(self.rt)

    def plan(self, messages, tools=None):
        """这次请求会用哪个端点（不发请求）。没有够格的端点时抛 LLMError(kind="no_vision")。"""
        caps = self._caps()
        eps = [e for e in (self.cfg.get("endpoints") or []) if isinstance(e, dict)]
        if caps is None or not eps:
            return eps[self.idx] if 0 <= self.idx < len(eps) else (eps[0] if eps else None)
        order, why = self._order(caps, messages, tools)
        if not order:
            raise LLMError(why, "no_vision")
        return eps[order[0]]

    def _order(self, caps, messages, tools):
        from . import capability, context
        size = context.estimate(messages) + context.estimate_tools(tools)
        return caps.candidates(need_tools=bool(tools), need_vision=capability.has_images(messages), size=size,
                               sticky=self.sticky, max_output=int(self.cfg.get("model", "max_output", default=4096) or 4096))

    def _endpoints(self):
        eps = self.cfg.get("endpoints") or []
        if not eps:
            raise LLMError("没有配置模型端点", "unreachable")
        return eps[self.idx:] + eps[:self.idx]

    def chat(self, messages, tools=None, on_delta=None, cancel=None, max_tokens=None, temperature=None):
        caps = self._caps()
        if caps is None:
            return self._chat_pool(self._endpoints(), messages, tools, on_delta, cancel, max_tokens, temperature)
        from . import capability
        all_eps = [e for e in (self.cfg.get("endpoints") or []) if isinstance(e, dict)]
        if not all_eps:
            raise LLMError("没有配置模型端点", "unreachable")
        tried = set()
        last = None
        while True:
            order, why = self._order(caps, messages, tools)
            order = [i for i in order if i not in tried]
            if not order:
                if why:
                    raise LLMError(why, "no_vision")
                raise last or LLMError("所有模型端点都不可用", "unreachable")
            base = (caps.candidates()[0] or [0])[0]
            for i in order:
                ep = all_eps[i]
                tried.add(i)
                try:
                    res = self._chat_pool([ep], messages, tools, on_delta, cancel, max_tokens, temperature)
                except LLMError as e:
                    last = e
                    if e.kind == "vision":
                        caps.correct(ep, "vision", False)
                        self.corrected.append(capability.ep_name(ep, i))
                        break               # 重新选：也许还有别的能看图的端点
                    if e.kind == "http":
                        raise
                    caps.mark_alive(ep, False)
                    continue
                caps.mark_alive(ep, True)
                if i != base and not self.noted:
                    self.noted = True
                    self.note = self._switch_note(caps, all_eps, base, i, messages, tools)
                self.sticky = i
                self.idx = i
                return res
            else:
                continue

    def _switch_note(self, caps, eps, base, i, messages, tools):
        from . import capability, context
        name = capability.ep_name(eps[i], i)
        b = eps[base] if 0 <= base < len(eps) else None
        if b is not None and caps.is_alive(b) is False:
            return "模型「%s」连不上，已改用 %s。" % (capability.ep_name(b, base), name)
        if capability.has_images(messages) and b is not None and not caps.qualifies(b, base, vision=True):
            return "这一步需要看图，已改用 %s。" % name
        if tools and b is not None and not caps.qualifies(b, base, tools=True):
            return "这一步需要调用工具，已改用 %s。" % name
        size = context.estimate(messages) + context.estimate_tools(tools)
        if b is not None and caps.context_of(b) - int(self.cfg.get("model", "max_output", default=4096) or 4096) - 1024 < size:
            return "这次的内容较多，已改用上下文更大的 %s。" % name
        return "已改用 %s。" % name

    def _chat_pool(self, eps, messages, tools, on_delta, cancel, max_tokens, temperature):
        """在给定的端点里依次试（每个试两次）。"""
        mode = self.cfg.get("model", "tool_mode", default="auto")
        last = None
        for n, ep in enumerate(eps):
            for attempt in range(2):
                try:
                    use_json = bool(tools) and (self.json_mode or mode == "json")
                    try:
                        res = self._call(ep, messages, tools, on_delta, cancel, max_tokens, temperature, use_json)
                    except _ToolsUnsupported:
                        if mode == "native":
                            raise LLMError("模型服务不支持工具调用", "http")
                        self.json_mode = True
                        res = self._call(ep, messages, tools, on_delta, cancel, max_tokens, temperature, True)
                    eps_all = self.cfg.get("endpoints")
                    self.idx = eps_all.index(ep) if ep in eps_all else 0
                    return res
                except Cancelled:
                    raise
                except LLMError as e:
                    last = e
                    if e.kind in ("http", "vision"):
                        raise
                    time.sleep(0.3 * (attempt + 1))
        raise last or LLMError("所有模型端点都不可用", "unreachable")

    def _call(self, ep, messages, tools, on_delta, cancel, max_tokens, temperature, use_json):
        mc = self.cfg.get("model") or {}
        body = {"model": ep["model"], "stream": True, "stream_options": {"include_usage": True},
                "temperature": mc.get("temperature", 0.3) if temperature is None else temperature,
                "max_tokens": max_tokens or mc.get("max_output", 4096)}
        if ep.get("kind", "vllm") == "vllm" and mc.get("thinking") is not None:
            body["chat_template_kwargs"] = {"enable_thinking": bool(mc.get("thinking"))}
        if tools and use_json:
            body["messages"] = to_json_mode(messages, tools)
            body["response_format"] = {"type": "json_object"}
        else:
            body["messages"] = messages
            if tools:
                body["tools"] = tools
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if ep.get("api_key"):
            headers["Authorization"] = "Bearer " + ep["api_key"]
        req = urllib.request.Request(ep["base_url"].rstrip("/") + "/chat/completions",
                                     data=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers=headers)
        # 内网端点不走系统代理
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if ep.get("internal", True) \
            else urllib.request.build_opener()
        try:
            resp = opener.open(req, timeout=mc.get("timeout", 180))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:600]
            low = detail.lower()
            if 400 <= e.code < 500 and _has_images(messages) and any(h in low for h in VISION_HINTS):
                raise LLMError("模型服务不支持图片（HTTP %d）：%s" % (e.code, detail[:200]), "vision")
            if e.code == 400 and tools and not use_json and ("tool" in low):
                raise _ToolsUnsupported()
            if e.code in (429, 500, 502, 503, 504):
                raise LLMError("模型服务暂时不可用（HTTP %d）" % e.code, "unreachable")
            raise LLMError("模型服务返回错误（HTTP %d）：%s" % (e.code, detail), "http")
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as e:
            raise LLMError("连不上模型服务 %s：%s" % (ep.get("name") or ep["base_url"], e), "unreachable")
        content, reasoning, calls, usage, finish = [], [], {}, {}, "stop"
        try:
            ctype = resp.headers.get("Content-Type", "")
            if "event-stream" not in ctype:
                j = json.loads(resp.read().decode("utf-8", "replace"))
                msg = j["choices"][0]["message"]
                content.append(msg.get("content") or "")
                reasoning.append(msg.get("reasoning_content") or msg.get("reasoning") or "")
                for i, c in enumerate(msg.get("tool_calls") or []):
                    calls[i] = {"id": c.get("id") or "", "name": c["function"]["name"], "raw": c["function"].get("arguments") or ""}
                usage, finish = j.get("usage") or {}, j["choices"][0].get("finish_reason") or "stop"
            else:
                for line in resp:
                    if cancel is not None and cancel.is_set():
                        raise Cancelled()
                    line = line.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        j = json.loads(data)
                    except Exception:
                        continue
                    if j.get("usage"):
                        usage = j["usage"]
                    ch = (j.get("choices") or [None])[0]
                    if not ch:
                        continue
                    if ch.get("finish_reason"):
                        finish = ch["finish_reason"]
                    d = ch.get("delta") or {}
                    r = d.get("reasoning_content") or d.get("reasoning")
                    if r:
                        reasoning.append(r)
                    if d.get("content"):
                        content.append(d["content"])
                        if on_delta and not use_json:
                            on_delta(d["content"])
                    for c in d.get("tool_calls") or []:
                        o = calls.setdefault(c.get("index", 0), {"id": "", "name": "", "raw": ""})
                        if c.get("id"):
                            o["id"] = c["id"]
                        fn = c.get("function") or {}
                        if fn.get("name"):
                            o["name"] += fn["name"]
                        if fn.get("arguments"):
                            o["raw"] += fn["arguments"]
        except Cancelled:
            raise
        except (socket.timeout, ConnectionError, OSError) as e:
            raise LLMError("模型回复中途断开：%s" % e, "unreachable")
        finally:
            try:
                resp.close()
            except Exception:
                pass
        text = "".join(content)
        tool_calls = []
        if tools and use_json:
            obj, _ = parse_args(text)
            if obj and isinstance(obj.get("tool"), str):
                args = obj.get("args")
                tool_calls.append({"id": "j%d" % int(time.time() * 1000), "name": obj["tool"],
                                   "arguments": args if isinstance(args, dict) else None, "raw": json.dumps(args, ensure_ascii=False),
                                   "error": None if isinstance(args, dict) else "args 必须是对象"})
                text = ""
            elif obj and "final" in obj:
                text = str(obj["final"])
                if on_delta:
                    on_delta(text)
        else:
            for i in sorted(calls):
                c = calls[i]
                args, err = parse_args(c["raw"])
                tool_calls.append({"id": c["id"] or "c%d_%d" % (int(time.time() * 1000), i), "name": c["name"],
                                   "arguments": args, "raw": c["raw"], "error": err})
        res = ChatResult(text, tool_calls, finish, usage, "".join(reasoning), ep.get("name") or ep["base_url"])
        res.model = ep.get("model") or ""
        return res

    def ping(self):
        """逐个测试端点，返回 [{name, ok, detail}]。"""
        out = []
        for ep in self.cfg.get("endpoints") or []:
            try:
                req = urllib.request.Request(ep["base_url"].rstrip("/") + "/models",
                                             headers={"Authorization": "Bearer " + ep["api_key"]} if ep.get("api_key") else {})
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if ep.get("internal", True) \
                    else urllib.request.build_opener()
                j = json.loads(opener.open(req, timeout=6).read().decode("utf-8", "replace"))
                ids = [d.get("id") for d in j.get("data", [])]
                out.append({"name": ep.get("name"), "ok": True, "detail": "可用模型：" + "、".join(ids[:5]),
                            "model_found": ep["model"] in ids})
            except Exception as e:
                out.append({"name": ep.get("name"), "ok": False, "detail": str(e)[:200]})
        return out


class MockLLM:
    """假模型：按脚本回放固定回复，用来测试状态、批准、断线和恢复。"""

    def __init__(self, script=None, delay=0.0):
        self.script = list(script or [])
        self.calls = []            # 每次请求时的消息快照
        self.delay = delay
        self.lock = threading.Lock()
        self.json_mode = False

    def chat(self, messages, tools=None, on_delta=None, cancel=None, max_tokens=None, temperature=None):
        with self.lock:
            self.calls.append(json.loads(json.dumps(messages, ensure_ascii=False)))
            item = self.script.pop(0) if self.script else {"content": "（假模型脚本结束）"}
        if callable(item):
            item = item(messages, tools)
        end = time.time() + (item.get("delay", self.delay) if isinstance(item, dict) else self.delay)
        while time.time() < end:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            time.sleep(0.01)
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        if item.get("error"):
            raise LLMError(item["error"], item.get("kind", "unreachable"))
        text = item.get("content", "")
        if text and on_delta:
            on_delta(text)
        calls = []
        for i, tc in enumerate(item.get("tool_calls") or []):
            name, args = tc[0], tc[1]
            raw = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
            parsed, err = parse_args(raw)
            calls.append({"id": "m%d_%d" % (len(self.calls), i), "name": name, "arguments": parsed, "raw": raw, "error": err})
        return ChatResult(text, calls, "tool_calls" if calls else "stop", {"prompt_tokens": 0, "completion_tokens": 0}, "", "mock")

    def ping(self):
        return [{"name": "mock", "ok": True, "detail": "假模型"}]


_URL_RE = None


def _demo_url(text):
    """用户的话里有"打开网页"加一个网址时，取出网址。"""
    global _URL_RE
    import re
    if _URL_RE is None:
        _URL_RE = re.compile(r"(https?://[^\s，。、“”\"'<>）)]+|(?:[a-z0-9-]+\.)+[a-z]{2,}(?::\d+)?(?:/[^\s，。、“”\"'<>）)]*)?)", re.I)
    if "打开网页" not in (text or ""):
        return None
    m = _URL_RE.search(text)
    return m.group(1) if m else None


def _user_text(m):
    c = m.get("content")
    if isinstance(c, list):
        return "\n".join(p.get("text") or "" for p in c if isinstance(p, dict))
    return c or ""


def demo_script(messages, tools):
    """--mock 启动时用的演示逻辑：不连模型也能看界面。
    用户说"打开网页 + 网址"、而且模型看得到浏览器工具时：打开 → 看一眼 → 回一句话（演示操作网页的步骤和批准卡片）。"""
    last = messages[-1]
    names = {((t.get("function") or {}).get("name")) for t in (tools or [])}
    hspace = bool(messages) and messages[0].get("role") == "system" and "你在 Hspace 里" in str(messages[0].get("content") or "")
    if hspace and "kb_search" in names:
        # Hspace（docs/两种模式.md）：先在资料里查，再回一句带出处编号的话；查不到就说资料里没有找到
        if last["role"] == "user":
            q = _user_text(last).strip().split("\n")[0][:60] or "资料"
            return {"content": "", "tool_calls": [("kb_search", {"query": q})]}
        if last["role"] == "tool" and _prev_tool(messages) == "kb_search":
            return {"content": _demo_kb_answer(last.get("content") or "")}
    if last["role"] == "user":
        url = _demo_url(_user_text(last))
        if url and "browser_open" in names:
            return {"content": "", "tool_calls": [("browser_open", {"url": url})]}
        return {"content": "", "tool_calls": [("list_dir", {"path": "."})]}
    if last["role"] == "tool":
        prev = None
        for m in reversed(messages):
            if m.get("role") == "assistant" and m.get("tool_calls"):
                prev = m["tool_calls"][-1]["function"]["name"]
                break
        if prev == "browser_open":
            if (last.get("content") or "").startswith("已打开") and "browser_look" in names:
                return {"content": "", "tool_calls": [("browser_look", {})]}
            return {"content": "这是演示模式。网页没有打开：%s" % (last.get("content") or "")[:200]}
        if prev in ("browser_open", "browser_look"):
            return {"content": "这是演示模式（没有连接真实模型）。网页已经打开，页面内容在上面的步骤里。接上模型后就可以让我接着填表、点按钮。"}
    return {"content": "这是演示模式（没有连接真实模型）。我看了一下工作区，上面是文件列表。接上模型后就可以真正干活了。"}


def _prev_tool(messages):
    for m in reversed(messages):
        if m.get("role") == "assistant" and m.get("tool_calls"):
            return m["tool_calls"][-1]["function"]["name"]
    return None


def _demo_kb_answer(content):
    """演示模式的 Hspace 回答：引用检索结果里的第一条。"""
    import re
    m = re.search(r"^\[(\d+)\] (.+)$\n(.+)$", content, re.M)
    if not m:
        return "资料里没有找到相关的内容。（这是演示模式，没有连接真实模型。）"
    n, where, body = m.group(1), m.group(2), m.group(3).strip()
    # 问答对、标准答案的片段是"问：…\n答：…"：摘"答"那一行，不要只把问题念一遍
    qa = re.search(r"^\[%s\] .+$\n问：.*$\n答：(.+)$" % n, content, re.M)
    if qa:
        body = qa.group(1).strip()
    if len(body) > 80:
        body = body[:80] + "……"
    return "资料里是这么写的：%s [%s]\n\n出处：%s。（这是演示模式，没有连接真实模型，只是把检索到的第一条摘出来。）" % (body, n, where)
