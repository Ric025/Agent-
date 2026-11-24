"""一个任务一次运行的"内核"：给 create_agent 的中间件调用。

LangGraph 负责流程（问模型 → 批准 → 执行工具 → 再问模型）和检查点；这里负责框架不管的业务部分：
  - 批准判定（gate）：按"新建、覆盖、删除"动态分级，结果记在账本上，恢复时重算得到同样的答案；
  - 执行工具（run_tool）：参数校验、路径守卫、执行前后在 actions 表记账、"结果未知时不重做"、截断长结果、审计；
  - 问模型（call_model）：可被用户插话或暂停打断，丢弃作废的回复，写 assistant_message；
  - 账本事件：界面靠账本里的事件显示，事件类型和载荷与旧内核相同。

账本（events/actions 表）是界面和审计的事实来源；LangGraph 检查点里的消息是给模型看的上下文。两边由这里保持一致。"""
from __future__ import annotations
import contextvars, json, os, threading, time, traceback

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from .. import context, policy
from ..loop import (UNKNOWN_TEXT, VOID_TEXT, DISABLED_TEXT, DENIED_TEXT, MISMATCH_TEXT, TERMINAL, Counters,
                    action_for, Shutdown, with_display)
from .. import capability, pageview
from ..llm import LLMError
from ..store import digest_of, new_id
from ..tools import ToolError, validate, available, to_text, REGISTRY
from .models import text_of

TODO_TOOL = "write_todos"          # TodoListMiddleware 的工具名
TODO_LEDGER = "todo_write"         # 账本和界面里沿用旧名字（界面按这个名字把计划步骤藏起来，改用右栏显示）
TODO_STATUS = {"pending": "pending", "in_progress": "doing", "completed": "done"}

STALE_TEXT = "未执行：执行前情况有变（例如文件已经存在），这一步需要先征求用户同意。如果仍要做，请再调用一次。"
NOT_APPROVED_TEXT = "未执行：这个操作没有得到用户批准。"
NO_TOOL_TEXT = "这个工具现在不可用"
SUMMARY_HEAD = "Here is a summary of the conversation to date:"     # SummarizationMiddleware 生成的摘要消息开头


class Abort(Exception):
    """中止这次图的运行。kind：
    pause / cancel / shutdown —— 用户暂停、取消，或程序关闭；
    stale —— 子代理所属的主任务改了要求；
    restart —— 用户插话（或要求变了）：丢掉这次模型回复，带上新消息重新开始；
    stop —— 预算或连续失败，按 status/reason 停下。"""

    def __init__(self, kind, status=None, reason=None):
        super().__init__(kind if not reason else "%s：%s" % (kind, reason))
        self.kind, self.status, self.reason = kind, status, reason


class ModelUnavailable(Exception):
    """模型调用失败（重试和换台之后仍然失败）。任务按"模型不可用"暂停。"""


LIMIT_ERRORS = ("ModelCallLimitExceededError", "ToolCallLimitExceededError", "GraphRecursionError")


def describe_model_error(e):
    if isinstance(e, LLMError):
        return str(e)
    mod = type(e).__module__ or ""
    if mod.startswith(("openai", "httpx", "httpcore")) or isinstance(e, (ConnectionError, TimeoutError)):
        return "%s: %s" % (type(e).__name__, str(e)[:500])
    return "模型返回了无法处理的内容：%s: %s" % (type(e).__name__, str(e)[:500])


class LGCtx:
    """交给工具处理函数的上下文，属性和旧内核的 Ctx 一样。"""

    def __init__(self, k):
        self.kernel = k
        self.runner = k
        self.rt, self.store, self.cfg = k.rt, k.store, k.cfg
        self.sid, self.ws = k.sid, k.ws
        self.root = k.ws["path"]
        self.is_sub = k.sub
        from .. import modes
        self.mode = modes.of_session(k.store, k.sid)      # 对话的模式（docs/两种模式.md 1.3）
        self.call_id = None
        self.action_id = None

    def emit(self, etype, payload):
        return self.rt.emit(self.sid, etype, payload)

    def ask(self, question, options=None):
        """ask_user 的回答已经在图的 after_model 那一步通过 interrupt() 拿到并记在账本里，这里直接取。"""
        for e in reversed(self.store.events(self.sid, types=("answer",))):
            if e["payload"].get("call_id") == self.call_id:
                return e["payload"].get("text") or ""
        raise ToolError("没有拿到用户的回答，请重新提问。")


class TaskKernel:
    def __init__(self, host, sid, sub=False, ctl=None, counters=None):
        self.host, self.rt = host, host.rt
        self.store, self.cfg = self.rt.store, self.rt.cfg
        self.sid, self.sub = sid, sub
        self.ctl = ctl or sid            # 暂停、取消标志看哪个任务（子代理跟随主任务）
        s = self.store.session(sid)
        self.ws = self.store.workspace(s["workspace_id"]) if s else None
        if not self.ws:
            raise RuntimeError("任务所在的工作区不存在了")
        self.counters = counters if counters is not None else Counters()
        self.parent_rev = None
        if sub and s.get("parent_id"):
            p = self.store.session(s["parent_id"])
            self.parent_rev = p["revision"] if p else None
        self.ctx = LGCtx(self)
        # 系统提示在一次运行中保持不变。计划改由 TodoListMiddleware 的 write_todos 提供。
        # 工具按需加载（docs/借鉴开源项目.md 3.7）：self.tools 是这次能用的全部工具，全部登记给工具节点；
        # 每次调模型时在 wrap_model_call 里按 self.toolset 过滤给模型看的那些（官方工具挑选中间件的做法）
        self.tools = {}
        for t in available(self.ctx, sub):
            if t.name == TODO_LEDGER:
                continue
            self.tools.setdefault(t.name, t)
        from .. import toolsets
        self.toolset = toolsets.Toolset(self.rt, sid, self.ctx.mode, list(self.tools.values()), sub=sub)
        if self.toolset.lazy:
            self.tools[toolsets.TOOL_LOAD] = self.toolset.load_tool()
        self.guard = toolsets.LoopGuard(0 if sub else toolsets.guard_limit(self.cfg))
        self.guard_stop = False          # 被拦下的调用又发了一次：下一次调模型前停下来
        if not sub and hasattr(self.counters, "toolset"):
            self.counters.toolset = self.toolset      # /状态 读运行中的那一份
        system = context.system_prompt(self.rt, self.ws, sub, mode=self.ctx.mode)
        self.system = system.replace(TODO_LEDGER, TODO_TOOL) if not sub else system
        self.exec_lock = threading.Lock()
        self.worker = None               # 当前模型调用所在的线程：流式片段只认它
        self.images = {}                 # 调用 id → 截图（data URL）
        self.extras = {}                 # 调用 id → 工具给界面的结构化数据（kb_search 的 hits、display，docs/两种模式.md 3.1）
        self.endpoint_models = None      # 端点下标 → ChatOpenAI（models.build 填；测试的假模型时为 None）
        self.endpoint_callbacks = None
        self.sticky = 0                  # 这次运行换到的端点：只往上换（docs/操控电脑.md 7.3）
        self.switch_noted = False

    # ================= 小工具 =================
    def live(self):
        return self.rt._live(self.ctl)

    def budget(self, key, default=None):
        v = self.cfg.get("budgets", key)
        return default if v is None else v

    def session(self):
        return self.store.session(self.sid)

    def notice(self, text):
        self.rt.emit(self.sid, "notice", {"text": text})

    def notify(self):
        with self.rt.cv:
            self.rt.cv.notify_all()

    def parent_changed(self):
        if self.parent_rev is None:
            return False
        s = self.session()
        p = self.store.session(s["parent_id"]) if s else None
        return bool(p) and p["revision"] != self.parent_rev

    def check_flags(self, model=False):
        """每次要做下一件事之前看一眼：关闭、取消、暂停、预算。需要停就抛 Abort。"""
        rt = self.rt
        if rt.closing:
            raise Abort("shutdown")
        lv = self.live()
        if lv.cancel:
            raise Abort("cancel")
        if lv.pause:
            raise Abort("pause")
        if self.sub and self.parent_changed():
            raise Abort("stale")
        c = self.counters
        if c.failures >= (self.budget("max_failures") or 4):
            raise Abort("stop", "paused", "连续失败")
        if model and not self.sub:
            minutes = self.budget("max_minutes") or 30
            if time.time() - c.started - c.waited > minutes * 60:
                self.notice("这次已经运行超过 %d 分钟，先停下来。需要的话点继续。" % minutes)
                raise Abort("stop", "paused", "超时")

    # ---- 账本查询 ----
    def fed_seq(self):
        s = self.session()
        return int(((s or {}).get("meta") or {}).get("lg_fed") or 0)

    def set_fed(self, seq):
        with self.rt.cv:
            s = self.session()
            meta = s["meta"] or {}
            meta["lg_fed"] = int(seq)
            self.store.set_session(self.sid, meta=meta)

    def unfed(self):
        """还没交给图的用户消息（用户插话、改要求）。"""
        if self.sub:
            return []
        return self.store.events(self.sid, since=self.fed_seq(), types=("user_message",))

    def call_info(self, cid):
        """在账本里找到这个调用：所在的 assistant_message、序号、调用顺序、修订号。"""
        for e in reversed(self.store.events(self.sid, types=("assistant_message",))):
            calls = e["payload"].get("tool_calls") or []
            for i, c in enumerate(calls):
                if c.get("id") == cid:
                    return {"event": e, "index": i, "entry": c, "order": [x.get("id") for x in calls],
                            "revision": e["revision"] or 0}
        return None

    def result_of(self, cid):
        for e in self.store.events(self.sid, types=("tool_result",)):
            if e["payload"].get("call_id") == cid:
                return e["payload"]
        return None

    def asked(self, cid):
        """这个调用是否发出过批准请求。批准中间件的 when 靠它保证"恢复时重算得到同样的答案"。"""
        for e in self.store.events(self.sid, types=("approval_requested",)):
            if e["payload"].get("call_id") == cid:
                return True
        return False

    def seen_ids(self):
        out = set()
        for e in self.store.events(self.sid, types=("assistant_message",)):
            out |= {c.get("id") for c in e["payload"].get("tool_calls") or []}
        return out

    # ---- 记结果 ----
    def write_result(self, cid, tool, ok, content, action_id=None, truncated=False, extra=None):
        p = {"call_id": cid, "action_id": action_id, "tool": tool, "ok": bool(ok), "content": content}
        if truncated:
            p["truncated"] = True
        img = self.images.pop(cid, None)
        if img and ok:
            p["image"] = img
        ext = self.extras.pop(cid, None) or extra
        if ext and ok:
            for k, v in ext.items():
                p.setdefault(k, v)
        self.rt.emit(self.sid, "tool_result", p)
        self.notify()

    def finish(self, call, ok, text, a=None, truncated=False, count=False, ledger_name=None, extra=None):
        """记一条 tool_result，返回给 LangGraph 的 ToolMessage。count=True 时计入连续失败。"""
        name = ledger_name or call.get("name") or ""
        self.write_result(call.get("id"), name, ok, text, a["id"] if a else None, truncated, extra)
        if count:
            self.after(ok)
        return ToolMessage(content=text, tool_call_id=call.get("id"), name=call.get("name") or name,
                           status="success" if ok else "error")

    def after(self, ok):
        """记连续失败次数，到上限发提示；真正停下在下一次 check_flags。"""
        if ok:
            self.counters.failures = 0
            return
        self.counters.failures += 1
        limit = self.budget("max_failures") or 4
        if self.counters.failures == limit:
            self.notice("连续 %d 次操作没有成功，先停下来。看一下错误，需要的话点继续。" % self.counters.failures)

    # ================= 批准判定 =================
    def validate_call(self, tool, call, info):
        """和旧内核一样的校验和错误文字。返回错误说明或 None。"""
        name = call.get("name")
        if not tool:
            return "没有这个工具：%s。可用的工具有：%s" % (name, "、".join(self.tools))
        entry = (info or {}).get("entry") or {}
        args = entry.get("arguments") if info else call.get("args")
        if entry.get("error") or not isinstance(args, dict):
            return "参数解析失败：%s。请输出合法的 JSON 对象作为参数。" % (entry.get("error") or "参数必须是对象")
        if tool.name in REGISTRY:
            extra = [k for k in args if k not in tool.props]
            if extra:
                return "参数有误：不认识的参数 %s。" % "、".join(extra)
        try:
            err = validate(tool.schema(), args)
        except Exception:
            err = None         # 外部连接器的参数格式可能超出我们的校验范围，交给对方校验
        if err:
            return "参数有误：%s。请改正后重试。" % err
        # 资料库只读（docs/两种模式.md 2.1）：写工具指向资料库里的路径，不问用户，直接拒绝
        from .. import kb as kbm
        return kbm.write_violation(self.ctx, tool.name, args, self.risk(tool, args))

    def risk(self, tool, args):
        try:
            return tool.risk_of(self.ctx, args)
        except Exception:
            return "overwrite"

    def decision(self, tool, args):
        setting = self.store.tool_setting(tool.name)
        if self.sub:
            return "deny" if setting == "deny" else "allow"     # 子代理只有只读工具，不需要批准
        return policy.decide(self.risk(tool, args), self.cfg.get("approval_mode") or "auto", setting)

    def gate_request(self, request):
        """HumanInTheLoopMiddleware 的 when：收到 ToolCallRequest（tool=None），返回 True 表示要停下来问人。"""
        return self.gate(request.tool_call)

    def gate(self, call):
        cid = call.get("id")
        if self.asked(cid):
            return True
        if action_for(self.store, self.sid, cid) is not None or self.result_of(cid) is not None:
            return False
        info = self.call_info(cid)
        if info is None:
            return False
        tool = self.tools.get(call.get("name"))
        if self.validate_call(tool, call, info):
            return False                      # 不合法的调用不问人，执行那一步把错误告诉模型
        args = info["entry"].get("arguments") or {}
        if self.guard.check(tool.name, args):
            return False                      # 重复的调用会被拦下（run_tool 里），不问人
        risk = self.risk(tool, args)
        d = self.decision(tool, args)
        with self.rt.cv:
            if self.asked(cid):
                return True
            if action_for(self.store, self.sid, cid) is not None:
                return False
            a = self.store.add_action(self.sid, info["revision"], cid, tool.name, args, risk)
            if d == "deny":
                self.store.set_action(a["id"], "denied", {"by": "setting"})
                self.store.audit(self.sid, a, "已禁用", "未执行")
                return False
            if d == "ask":
                self.store.set_action(a["id"], "awaiting")
                self.rt.emit(self.sid, "approval_requested", with_display(
                    {"action_id": a["id"], "call_id": cid, "tool": tool.name, "args": args, "risk": risk}, tool, self.ctx, args))
                return True
            self.store.set_action(a["id"], "approved")
            return False

    # ================= 执行工具 =================
    def wait_turn(self, info):
        """同一条回复里的多个调用，LangGraph 会并行执行（每个调用一个 Send 任务）。
        我们按模型给出的顺序一个一个做（例如先写文件再读同一个文件）。"""
        earlier = info["order"][:info["index"]]
        if not earlier:
            return
        last_key, last_change = None, time.time()
        with self.rt.cv:
            while True:
                self.check_flags()
                pending = [c for c in earlier if self.result_of(c) is None]
                if not pending:
                    return
                key = tuple(pending)
                if key != last_key:
                    last_key, last_change = key, time.time()
                busy = any(self.host.is_inflight(self.sid, c) for c in pending)
                if not busy and time.time() - last_change > 10:
                    # 前面的调用迟迟没有开始（理论上不会发生）：不再等，避免卡死
                    print("[lg] 等待前序调用超时，继续执行 %s" % (info["entry"].get("id"),), flush=True)
                    return
                self.rt.cv.wait(0.2)

    def run_tool(self, request, handler):
        """wrap_tool_call：一个工具调用从校验到记账的全过程。返回 ToolMessage（或 write_todos 的 Command）。"""
        call = dict(request.tool_call)
        cid, name = call.get("id"), call.get("name")
        info = self.call_info(cid)
        if info is None:
            return ToolMessage(content="内部错误：账本里没有这个调用", tool_call_id=cid, name=name or "", status="error")
        self.wait_turn(info)
        with self.exec_lock:
            if name == TODO_TOOL:
                return self.run_todos(request, handler, call)
            prev = self.result_of(cid)
            if prev is not None:
                # 已经有结果（例如节点被 LangGraph 重跑、或者取消时已补上）：照抄，不再执行
                return ToolMessage(content=prev.get("content") or "", tool_call_id=cid, name=name or "",
                                   status="success" if prev.get("ok") else "error")
            store, sid = self.store, self.sid
            s = self.session()
            a = action_for(store, sid, cid)
            # 1. 用户改了要求：没执行的一律作废
            stale = (info["revision"] < s["revision"]) if not self.sub else self.parent_changed()
            if stale:
                if a and a["state"] not in TERMINAL and a["state"] != "running":
                    store.set_action(a["id"], "voided")
                    store.audit(sid, a, "作废", "用户更改了要求")
                if not (a and a["state"] in ("running", "done", "failed", "unknown")):
                    return self.finish(call, False, VOID_TEXT, a)
            # 2. 账本里已有动作：多半是恢复后 LangGraph 把这个节点重跑了一遍
            if a is not None:
                st = a["state"]
                if st == "running" and self.host.is_inflight(sid, cid):
                    self.host.wait_inflight(sid, cid, self)        # 还在另一个线程里执行：等它做完
                    a = store.action(a["id"])
                    st = a["state"]
                if st in ("running", "unknown"):
                    if st == "running":
                        store.set_action(a["id"], "unknown")
                        store.audit(sid, a, "恢复", "结果未知")
                    return self.finish(call, False, UNKNOWN_TEXT, a, count=True)
                if st in ("done", "failed"):
                    r = a["result"] or {}
                    return self.finish(call, r.get("ok", st == "done"), r.get("content", ""), a,
                                       truncated=bool(r.get("truncated")), extra=r.get("extra"))
                if st == "denied":
                    by_setting = isinstance(a.get("result"), dict) and a["result"].get("by") == "setting"
                    return self.finish(call, False, DISABLED_TEXT if by_setting else DENIED_TEXT, a)
                if st == "voided":
                    return self.finish(call, False, VOID_TEXT, a)
                if st == "awaiting":
                    store.set_action(a["id"], "voided")
                    store.audit(sid, a, "作废", "没有得到批准")
                    return self.finish(call, False, NOT_APPROVED_TEXT, a)
                if st == "approved":
                    return self.execute(a, call, request, handler, "用户批准" if self.asked(cid) else "自动放行")
                # proposed：登记后、判定前中断过，按新调用处理
            # 3. 校验
            tool = self.tools.get(name)
            err = self.validate_call(tool, call, info)
            if err:
                return self.finish(call, False, err, count=True)
            args = info["entry"].get("arguments") or {}
            # 没带的工具：存在、模式和开关都允许 → 加载它所在的组，照常执行（docs/借鉴开源项目.md 3.4）
            if not self.sub and tool.name != "tool_load":
                self.toolset.ensure(tool.name)
            # 重复调用检测（3.8）
            n = self.guard.check(tool.name, args)
            if n:
                return self.blocked(call, tool.name, args, n)
            # 4. 判定（主任务正常情况下已在批准中间件的 when 里判过，这里是兜底）
            d = self.decision(tool, args)
            if a is None:
                a = store.add_action(sid, info["revision"], cid, tool.name, args, self.risk(tool, args))
            if d == "deny":
                store.set_action(a["id"], "denied", {"by": "setting"})
                store.audit(sid, a, "已禁用", "未执行")
                return self.finish(call, False, DISABLED_TEXT, a)
            if d == "ask":
                store.set_action(a["id"], "voided")
                store.audit(sid, a, "作废", "没有经过批准流程")
                return self.finish(call, False, STALE_TEXT, a)
            store.set_action(a["id"], "approved")
            return self.execute(store.action(a["id"]), call, request, handler, "自动放行")

    def blocked(self, call, name, args, n):
        """拦下重复的调用：给模型一条结果，记 loop_guard 事件。被拦之后又发同样的调用：下一次调模型前停下来问用户。"""
        from .. import toolsets
        if self.guard.block(call.get("id"), name, args):
            self.guard_stop = True
        self.rt.emit(self.sid, "loop_guard", {"tool": name, "count": n, "call_id": call.get("id")})
        return self.finish(call, False, toolsets.GUARD_TEXT)

    def execute(self, a, call, request, handler, decision):
        store, sid, cid = self.store, self.sid, call.get("id")
        s = self.session()
        if not self.sub and a["revision"] != s["revision"]:
            store.set_action(a["id"], "voided")
            store.audit(sid, a, decision, "作废")
            return self.finish(call, False, VOID_TEXT, a)
        args = (self.call_info(cid) or {}).get("entry", {}).get("arguments")
        if a["digest"] != digest_of(a["tool"], a["args"], a["revision"]) or a["tool"] != call.get("name") or a["args"] != args:
            store.set_action(a["id"], "voided")
            store.audit(sid, a, decision, "作废：批准内容不一致")
            return self.finish(call, False, MISMATCH_TEXT, a)
        tool = self.tools.get(a["tool"])
        if tool is None:
            store.set_action(a["id"], "failed", {"ok": False, "content": NO_TOOL_TEXT})
            return self.finish(call, False, NO_TOOL_TEXT, a, count=True)
        # 自动放行的调用：执行前再判一次（例如同一条回复里先新建、再写同一个文件，第二次就成了覆盖）
        if not self.asked(cid):
            d = self.decision(tool, a["args"])
            if d == "deny":
                store.set_action(a["id"], "denied", {"by": "setting"})
                store.audit(sid, a, "已禁用", "未执行")
                return self.finish(call, False, DISABLED_TEXT, a)
            if d == "ask":
                store.set_action(a["id"], "voided")
                store.audit(sid, a, decision, "作废：执行前情况有变")
                return self.finish(call, False, STALE_TEXT, a)
        store.set_action(a["id"], "running")          # 先落盘"正在做"，崩溃后据此判断结果未知
        self.rt.emit(sid, "tool_started", with_display({"action_id": a["id"], "call_id": cid, "tool": a["tool"],
                                                        "args": a["args"]}, tool, self.ctx, a["args"]))
        self.host.set_inflight(sid, cid, True)
        self.ctx.call_id, self.ctx.action_id = cid, a["id"]
        limit = self.budget("tool_result_chars") or 6000
        if getattr(tool, "display", None) is not None:
            limit = max(limit, 60000)        # 页面概况自己按预算裁剪过
        try:
            out = handler(request)
            ok, full = self._parse_tool_output(out)
        except Abort as e:
            if e.kind == "stale":
                store.set_action(a["id"], "voided")
                store.audit(sid, a, decision, "作废：主任务的要求已变更")
                return self.finish(call, False, VOID_TEXT, a)
            store.set_action(a["id"], "approved")      # 只有派子代理会走到这里：它只读，恢复后可以重新做
            raise
        except (ToolError, policy.PathError) as e:
            ok, full = False, str(e)
        except Exception as e:
            ok, full = False, "工具内部错误：%s: %s" % (type(e).__name__, e)
        finally:
            self.ctx.call_id = self.ctx.action_id = None
            self.host.set_inflight(sid, cid, False)
        truncated = len(full) > limit
        text = full[:limit] if truncated else full
        if truncated:
            text += "\n（结果过长已截断，完整内容在 %s，需要时用 read_file 读取）" % self._save_full(a["id"], full)
        res = {"ok": ok, "content": text, "truncated": truncated}
        if ok and self.extras.get(cid):
            res["extra"] = self.extras[cid]
        store.set_action(a["id"], "done" if ok else "failed", res)
        msg = self.finish(call, ok, text, a, truncated=truncated)
        self.guard.record(a["tool"], a["args"], ok, text)
        if ok and a["tool"] == "skill_load":
            self.toolset.after_skill_load((a["args"] or {}).get("name"))
        dig = json.dumps(a["args"], ensure_ascii=False, sort_keys=True)
        dig = dig if len(dig) <= 300 else dig[:300] + "…"
        store.audit(sid, store.action(a["id"]), decision, "成功" if ok else "失败", "%s → %s" % (dig, text[:150]))
        self.after(ok)
        return msg

    @staticmethod
    def _parse_tool_output(out):
        if isinstance(out, ToolMessage):
            return getattr(out, "status", "success") != "error", text_of(out.content)
        if isinstance(out, str):
            return True, out
        return True, to_text(out, 1 << 60)[0]

    def _save_full(self, aid, full):
        d = os.path.join(self.ws["path"], policy.INTERNAL_DIR, "scratch", self.sid)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, aid + ".txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write(full)
        return policy.relpath(self.ws["path"], p)

    def run_todos(self, request, handler, call):
        """TodoListMiddleware 的 write_todos：照常执行（它返回 Command 更新图里的 todos），
        同时转成我们的 todo 事件，界面右栏靠它显示计划。没有副作用，重跑无妨。"""
        cid = call.get("id")
        items = todo_items((call.get("args") or {}).get("todos"))
        self.rt.emit(self.sid, "tool_started", {"action_id": None, "call_id": cid, "tool": TODO_LEDGER, "args": {"items": items}})
        try:
            out = handler(request)
        except Exception as e:
            return self.finish(call, False, "工具内部错误：%s: %s" % (type(e).__name__, e), ledger_name=TODO_LEDGER, count=True)
        upd = getattr(out, "update", None)
        if isinstance(upd, dict) and upd.get("todos") is not None:
            items = todo_items(upd.get("todos"))
            with self.rt.cv:
                s = self.session()
                meta = s["meta"] or {}
                meta["todo"] = items
                self.store.set_session(self.sid, meta=meta)
            self.rt.emit(self.sid, "todo", {"items": items})
            mark = {"pending": "[ ]", "doing": "[>]", "done": "[x]"}
            text = "当前计划：\n" + "\n".join("%s %s" % (mark.get(i["status"], "[ ]"), i["text"]) for i in items)
            self.write_result(cid, TODO_LEDGER, True, text)
            self.after(True)
            return out
        ok, text = self._parse_tool_output(out)
        self.write_result(cid, TODO_LEDGER, ok, text)
        self.after(ok)
        return out

    # ================= 问模型 =================
    def model_call(self, request, handler):
        """wrap_model_call（最外层）：检查标志 → 补记账 → 问模型（可打断）→ 校正回复 → 写 assistant_message。"""
        self.check_flags(model=True)
        self.sync_results(request.messages)
        if self.guard_stop:
            from .. import toolsets
            self.guard_stop = False
            self.notice(toolsets.GUARD_STOP_TEXT)
            raise Abort("stop", "paused", toolsets.GUARD_REASON)
        with self.rt.cv:
            rev = self.session()["revision"]
            if self.unfed():
                raise Abort("restart")
        if not self.sub and self.toolset.refresh():
            self.guard.reset()             # 新的一轮（用户发言）：补算信号，重复调用检测清零
        req = self.filter_tools(self.prune_pages(self.prune_images(self.order_tool_messages(request))))
        told = self.vision_gate(req, rev)
        if told is not None:
            return told
        req = self.choose_model(req)
        while True:
            resp = self.call_model(handler, req)
            ai = _find_ai(resp)
            if ai is not None and (text_of(ai.content).strip() or ai.tool_calls or ai.invalid_tool_calls):
                break
            # 空回复不写账本，算一次失败再问一次
            self.counters.failures += 1
            if self.counters.failures >= (self.budget("max_failures") or 4):
                self.rt.emit(self.sid, "error", {"text": "模型连续返回空回复"})
                raise Abort("stop", "paused", "连续失败")
            self.check_flags(model=True)
        ai2, entries = self.normalize(ai)
        with self.rt.cv:
            if self.session()["revision"] != rev or self.unfed():
                raise Abort("restart")          # 用户改了要求：这次回复作废
            if self.sub and self.parent_changed():
                raise Abort("stale")
            u = getattr(ai, "usage_metadata", None) or {}
            usage = {"prompt_tokens": u.get("input_tokens", 0), "completion_tokens": u.get("output_tokens", 0)} if u else {}
            endpoint = (getattr(ai, "response_metadata", None) or {}).get("model_name") or ""
            self.rt.emit(self.sid, "assistant_message",
                         {"content": text_of(ai.content), "tool_calls": entries, "usage": usage, "endpoint": endpoint},
                         revision=rev)
            self.counters.steps += 1
        return _replace_ai(resp, ai, ai2)

    def call_model(self, handler, request):
        """在一个工作线程里调用模型，本线程每 50 毫秒看一眼取消事件。
        LangGraph 的同步运行没法从外面打断一个正在执行的节点，所以用这种办法：被打断时丢下工作线程
        （它的结果和流式片段都会被忽略），直接抛 Abort 结束这次图的运行。"""
        ev = threading.Event()
        lv = self.live()
        with self.rt.cv:
            self.check_flags(model=True)
            lv.llm_cancel = ev
        # 流式片段带上"这次调用开始时账本写到了第几条"（见 loop.py 同名说明）
        try:
            self.delta_base = self.rt.store.last_seq()
        except Exception:
            self.delta_base = None
        box, done = {}, threading.Event()
        cvctx = contextvars.copy_context()       # 带上 LangGraph 放在 contextvar 里的配置和运行信息

        order = list(getattr(self, "model_order", None) or []) if self.endpoint_models is not None else []

        def attempt():
            # 失败换台只在够格的端点之间换（ModelRetryMiddleware 在里层，每个端点先各自重试）
            if len(order) <= 1:
                return handler(request)
            last = None
            for n, i in enumerate(order):
                eps = [e for e in (self.cfg.get("endpoints") or []) if isinstance(e, dict)]
                try:
                    r = handler(request if n == 0 else request.override(model=self.model_for(i)))
                    capability.get(self.rt).mark_alive(eps[i], True)
                    self.sticky = i
                    return r
                except Exception as e:
                    if type(e).__name__ in ("GraphInterrupt", "GraphBubbleUp", "ParentCommand"):
                        raise
                    last = e
                    from ..llm import VISION_HINTS
                    low = str(e).lower()
                    if any(self._has_image(m) for m in request.messages) and any(h in low for h in VISION_HINTS) \
                            and "40" in low:
                        # 运行中纠正：服务回了"不支持图像"之类的 4xx
                        capability.get(self.rt).correct(eps[i], "vision", False, self.sid)
                        continue
                    capability.get(self.rt).mark_alive(eps[i], False)
            raise last

        def work():
            try:
                box["r"] = cvctx.run(attempt)
            except BaseException as e:          # noqa: B902 —— 要把所有异常带回调用线程
                box["e"] = e
            finally:
                done.set()
        th = threading.Thread(target=work, name="model-" + self.sid, daemon=True)
        self.worker = th
        th.start()
        try:
            while not done.wait(0.05):
                if ev.is_set() or self.rt.closing:
                    self.worker = None
                    self.check_flags(model=False)
                    raise Abort("restart")
        finally:
            self.worker = None
            with self.rt.cv:
                if lv.llm_cancel is ev:
                    lv.llm_cancel = None
        if "e" in box:
            e = box["e"]
            if isinstance(e, Abort) or type(e).__name__ in ("GraphInterrupt", "GraphBubbleUp", "ParentCommand"):
                raise e
            if not isinstance(e, Exception):
                raise e
            raise ModelUnavailable(describe_model_error(e)) from e
        return box["r"]

    def normalize(self, ai):
        """校正模型回复：解析失败的调用也当作调用（让执行那一步把错误告诉模型），调用 id 在任务内去重。
        返回 (新的 AIMessage, 账本里 tool_calls 的写法)。"""
        seen = self.seen_ids()
        raw = []
        for tc in ai.tool_calls or []:
            args = tc.get("args")
            raw.append((tc.get("id"), tc.get("name") or "", args if isinstance(args, dict) else None,
                        json.dumps(args, ensure_ascii=False) if isinstance(args, dict) else str(args), None))
        for ic in getattr(ai, "invalid_tool_calls", None) or []:
            raw.append((ic.get("id"), ic.get("name") or "", None, ic.get("args") or "", "参数不是合法的 JSON 对象"))
        lc_calls, entries = [], []
        for cid, name, args, rawtext, err in raw:
            if not cid or cid in seen:
                cid = new_id("call_")
            seen.add(cid)
            lc_calls.append({"name": name, "args": args if isinstance(args, dict) else {}, "id": cid, "type": "tool_call"})
            if name == TODO_TOOL:
                entries.append({"id": cid, "name": TODO_LEDGER, "arguments": {"items": todo_items((args or {}).get("todos"))},
                                "raw": rawtext, "error": err})
            else:
                entries.append({"id": cid, "name": name, "arguments": args, "raw": rawtext, "error": err})
        ai2 = ai.model_copy(update={"tool_calls": lc_calls, "invalid_tool_calls": []})
        return ai2, entries

    def filter_tools(self, request):
        """按需加载：这次只给模型看带上的那几组（官方 LLMToolSelectorMiddleware 也是在 wrap_model_call 里
        request.override(tools=…) 过滤）。不是我们的工具（write_todos）原样保留。tool_load 换成说明最新的那个
        （说明里列的是现在没带的组）；全带上了就不给。只改这一次请求。"""
        ts = self.toolset
        if not ts.lazy:
            return request
        from .. import toolsets
        from .tools import as_langchain
        vis = set(ts.visible_names())
        out, changed = [], False
        for t in list(request.tools or []):
            n = _tool_name(t)
            if n == toolsets.TOOL_LOAD:
                changed = True
                if n in vis:
                    out.append(as_langchain(self, ts.load_tool()))
                continue
            if n in ts.all and n not in vis:
                changed = True
                continue
            out.append(t)
        return request.override(tools=out) if changed else request

    def prune_images(self, request):
        """只给模型看最近 3 张图片，更早的换成文字（只改这次请求，不改检查点）。"""
        msgs = list(request.messages)
        left, changed = context.KEEP_IMAGES, False
        for i in range(len(msgs) - 1, -1, -1):
            m = msgs[i]
            if not isinstance(m, HumanMessage) or not isinstance(m.content, list):
                continue
            parts = []
            for b in reversed(m.content):
                if isinstance(b, dict) and b.get("type") in ("image_url", "image"):
                    if left > 0:
                        left -= 1
                        parts.append(b)
                    else:
                        parts.append({"type": "text", "text": "[图片已省略]"})
                        changed = True
                else:
                    parts.append(b)
            parts.reverse()
            msgs[i] = m.model_copy(update={"content": parts})
        return request.override(messages=msgs) if changed else request

    def prune_pages(self, request):
        """浏览器和桌面的结果只留最近一次的全文，更早的换成一行；最近一次带截图时，后面跟一条带图的用户消息。
        只改这次请求，不改检查点（docs/操控电脑.md 2.3）。"""
        msgs = list(request.messages)
        # 资料检索的结果：只有最近两次是全文，更早的换成一行（docs/两种模式.md 3.2）
        kb_idx = [i for i, m in enumerate(msgs) if isinstance(m, ToolMessage) and pageview.is_kb_tool(m.name)
                  and pageview.has_kb_content(text_of(m.content))]
        kb_old = set(kb_idx[:-pageview.KB_KEEP]) if len(kb_idx) > pageview.KB_KEEP else set()
        kb_changed = False
        for i in kb_old:
            msgs[i] = msgs[i].model_copy(update={"content": pageview.KB_OMIT})
            kb_changed = True
        idx = [i for i, m in enumerate(msgs) if isinstance(m, ToolMessage) and pageview.is_page_tool(m.name)]
        if not idx:
            return request.override(messages=msgs) if kb_changed else request
        out, changed = [], kb_changed
        for i, m in enumerate(msgs):
            if i in idx[:-1]:
                body = text_of(m.content)
                short = pageview.omitted(body)
                if short != body:
                    m = m.model_copy(update={"content": short})
                    changed = True
            out.append(m)
            if i == idx[-1]:
                r = self.result_of(m.tool_call_id) or {}
                if r.get("image"):
                    j = i + 1
                    while j < len(msgs) and isinstance(msgs[j], ToolMessage):
                        j += 1
                    # 截图放在这一组工具结果之后（插在下一条消息之前）
                    self._pending_shot = (j, r["image"])
        shot = getattr(self, "_pending_shot", None)
        self._pending_shot = None
        if shot:
            j, url = shot
            out.insert(j, HumanMessage(content=[{"type": "text", "text": "（上面那一步的截图）"},
                                                {"type": "image_url", "image_url": {"url": url}}]))
            changed = True
        return request.override(messages=out) if changed else request

    def strip_images(self, request):
        msgs = []
        for m in request.messages:
            if isinstance(m, HumanMessage) and self._has_image(m):
                m = m.model_copy(update={"content": [b if not (isinstance(b, dict) and b.get("type") in ("image_url", "image"))
                                                     else {"type": "text", "text": "[图片已省略]"} for b in m.content]})
            msgs.append(m)
        return request.override(messages=msgs)

    @staticmethod
    def _has_image(m):
        return isinstance(getattr(m, "content", None), list) and any(
            isinstance(b, dict) and b.get("type") in ("image_url", "image") for b in m.content)

    def vision_gate(self, request, rev):
        """需要看图而没有任何端点能看：最新的用户消息带图 → 不发请求，直接用原话回答；更早的图换成文字。"""
        msgs = list(request.messages)
        if not any(self._has_image(m) for m in msgs):
            return None
        order, why = capability.get(self.rt).candidates(need_vision=True)
        if order:
            return None
        last_h = next((m for m in reversed(msgs) if isinstance(m, (HumanMessage, AIMessage))), None)
        if not (isinstance(last_h, HumanMessage) and self._has_image(last_h)):
            return None
        if isinstance(last_h, HumanMessage) and self._has_image(last_h):
            from langchain.agents.middleware import ModelResponse
            text = why + capability.FALLBACK["image_input"]
            with self.rt.cv:
                self.rt.emit(self.sid, "assistant_message", {"content": text, "tool_calls": [], "usage": {}, "endpoint": ""},
                             revision=rev)
                self.counters.steps += 1
            return ModelResponse(result=[AIMessage(content=text)], structured_response=None)
        return None

    def choose_model(self, request):
        """每次调模型时选端点（docs/操控电脑.md 7.3）：按这次的需要找第一个活着、够格的端点；只往上换；换了提示一次。
        测试和演示用的假模型（endpoint_models 为 None）不选。"""
        if self.endpoint_models is None:
            return request
        eps = [e for e in (self.cfg.get("endpoints") or []) if isinstance(e, dict)]
        if not eps:
            return request
        caps = capability.get(self.rt)
        last_h = next((m for m in reversed(request.messages) if isinstance(m, (HumanMessage, AIMessage))), None)
        need_v = isinstance(last_h, HumanMessage) and self._has_image(last_h)
        if not need_v and any(self._has_image(m) for m in request.messages):
            # 只有更早的消息带图：要用的端点看不了就换成文字（平时还是排在前面的模型在做）
            o, _ = caps.candidates(need_tools=bool(request.tools), sticky=self.sticky)
            if o and not caps.qualifies(eps[o[0]], o[0], vision=True):
                request = self.strip_images(request)
        size = self.count_tokens(list(request.messages)) + self.fixed_tokens()
        order, why = caps.candidates(need_tools=bool(request.tools), need_vision=need_v, size=size, sticky=self.sticky,
                                     max_output=int(self.cfg.get("model", "max_output", default=4096) or 4096))
        if not order:
            return request
        self.model_order = order
        i = order[0]
        base = (caps.candidates()[0] or [0])[0]
        if i != base and not self.switch_noted and not self.sub:
            self.switch_noted = True
            name = capability.ep_name(eps[i], i)
            self.notice(("这一步需要看图，已改用 %s。" % name) if need_v else "已改用 %s。" % name)
        self.sticky = i
        return request.override(model=self.model_for(i))

    def model_for(self, i):
        from .models import chat_openai
        eps = [e for e in (self.cfg.get("endpoints") or []) if isinstance(e, dict)]
        m = self.endpoint_models.get(i)
        if m is None:
            m = self.endpoint_models[i] = chat_openai(self.cfg, eps[i], self.endpoint_callbacks)
        return m

    @staticmethod
    def order_tool_messages(request):
        """每条带调用的 AIMessage 后面的 ToolMessage 按调用顺序排（只改这次请求）。
        批准中间件的拒绝结果在 after_model 就加进来了，比执行结果早，顺序会和调用顺序不一致。"""
        msgs = list(request.messages)
        out, i, changed = [], 0, False
        while i < len(msgs):
            m = msgs[i]
            out.append(m)
            i += 1
            if isinstance(m, AIMessage) and m.tool_calls:
                j = i
                while j < len(msgs) and isinstance(msgs[j], ToolMessage):
                    j += 1
                block = msgs[i:j]
                pos = {c.get("id"): n for n, c in enumerate(m.tool_calls)}
                ordered = sorted(block, key=lambda t: pos.get(t.tool_call_id, len(pos)))
                if [id(x) for x in ordered] != [id(x) for x in block]:
                    changed = True
                out += ordered
                i = j
        return request.override(messages=out) if changed else request

    def sync_results(self, messages):
        """图里有、账本里没有的工具结果补记到账本（批准中间件的拒绝、计划中间件的并行报错等不经过 run_tool）。"""
        idx = None
        for i in range(len(messages) - 1, -1, -1):
            if isinstance(messages[i], AIMessage) and messages[i].tool_calls:
                idx = i
                break
        if idx is None:
            return
        for m in messages[idx + 1:]:
            if not isinstance(m, ToolMessage) or self.result_of(m.tool_call_id) is not None:
                continue
            info = self.call_info(m.tool_call_id)
            name = info["entry"].get("name") if info else (m.name or "")
            a = action_for(self.store, self.sid, m.tool_call_id)
            self.write_result(m.tool_call_id, name, getattr(m, "status", "success") != "error", text_of(m.content),
                              a["id"] if a else None)

    def after_model(self, state):
        """after_model：补记账；对 ask_user 调用用 LangGraph 的 interrupt() 停下来等用户回答。

        一个节点里有多次 interrupt() 时，恢复值按调用次序匹配，所以这里对每个提问按固定顺序调用，
        拿到的值不是给自己的就再调用一次（下一次会真正停下来）。"""
        from langgraph.types import interrupt
        msgs = state.get("messages") or []
        self.sync_results(msgs)
        if self.sub:
            return None
        ai, idx = None, None
        for i in range(len(msgs) - 1, -1, -1):
            if isinstance(msgs[i], AIMessage):
                ai, idx = msgs[i], i
                break
        if ai is None or not ai.tool_calls:
            return None
        have = {m.tool_call_id for m in msgs[idx + 1:] if isinstance(m, ToolMessage)}
        tool = self.tools.get("ask_user")
        for c in ai.tool_calls:
            cid = c.get("id")
            if c.get("name") != "ask_user" or tool is None or cid in have or self.result_of(cid) is not None:
                continue
            info = self.call_info(cid)
            if info is None or self.validate_call(tool, c, info):
                continue
            a = action_for(self.store, self.sid, cid)
            if a is not None and a["state"] in ("denied", "voided"):
                continue
            args = info["entry"].get("arguments") or {}
            q, opts = str(args.get("question") or ""), [str(x) for x in (args.get("options") or [])]
            if not any(e["payload"].get("call_id") == cid for e in self.store.events(self.sid, types=("question",))):
                self.rt.emit(self.sid, "question", {"call_id": cid, "question": q, "options": opts})
            while True:
                v = interrupt({"kind": "question", "call_id": cid, "question": q, "options": opts})
                if isinstance(v, dict) and v.get("kind") == "answer" and v.get("call_id") == cid:
                    break
        return None

    # ================= 恢复和插话时补齐 =================
    def reconcile(self, messages):
        """最后一条带调用的 AIMessage 里，图中还没有 ToolMessage 的调用：按账本补上结果。
        用于带新消息重新开始（用户插话）或取消之后继续。保证给模型的消息序列合法。"""
        idx = None
        for i in range(len(messages) - 1, -1, -1):
            if isinstance(messages[i], AIMessage):
                idx = i
                break
        if idx is None or not messages[idx].tool_calls:
            return []
        have = {m.tool_call_id for m in messages[idx + 1:] if isinstance(m, ToolMessage)}
        out = []
        for c in messages[idx].tool_calls:
            cid = c.get("id")
            if cid in have:
                continue
            r = self.result_of(cid)
            if r is None:
                a = action_for(self.store, self.sid, cid)
                if a and a["state"] == "running" and self.host.is_inflight(self.sid, cid):
                    self.host.wait_inflight(self.sid, cid, None)
                    a = self.store.action(a["id"])
                info = self.call_info(cid)
                name = info["entry"].get("name") if info else c.get("name")
                if a and a["state"] == "running":
                    self.store.set_action(a["id"], "unknown")
                    self.store.audit(self.sid, a, "恢复", "结果未知")
                    self.write_result(cid, name, False, UNKNOWN_TEXT, a["id"])
                elif a and a["state"] == "unknown":
                    self.write_result(cid, name, False, UNKNOWN_TEXT, a["id"])
                elif a and a["state"] in ("done", "failed"):
                    rr = a["result"] or {}
                    self.write_result(cid, name, rr.get("ok", a["state"] == "done"), rr.get("content", ""), a["id"],
                                      bool(rr.get("truncated")), rr.get("extra"))
                else:
                    if a and a["state"] not in TERMINAL:
                        self.store.set_action(a["id"], "voided")
                        self.store.audit(self.sid, a, "作废", "用户更改了要求")
                    self.write_result(cid, name, False, VOID_TEXT, a["id"] if a else None)
                r = self.result_of(cid)
            out.append(ToolMessage(content=(r or {}).get("content") or "", tool_call_id=cid, name=c.get("name") or "",
                                   status="success" if (r or {}).get("ok") else "error"))
        return out

    # ================= 压缩 =================
    def fixed_tokens(self):
        """系统提示和这一次给模型看的工具定义的估算 token（压缩阈值要扣掉这部分）。write_todos 的定义按 400 估。"""
        specs = self.toolset.specs()
        return (context.estimate([{"role": "system", "content": self.system}]) + context.estimate_tools(specs)
                + (0 if self.sub else 400))

    def summary_tokens(self, messages):
        """给 SummarizationMiddleware 的 token_counter：消息的估算，加上"建图以后又加载的工具定义"。
        压缩的触发线是建图时按当时的工具定义算的（构造参数），中途加载了一组之后靠这里把多出来的部分算进去。"""
        base = getattr(self, "_fixed_at_build", None)
        extra = (self.fixed_tokens() - base) if base is not None else 0
        return self.count_tokens(messages) + max(0, extra)

    def count_tokens(self, messages):
        """SummarizationMiddleware 的 token_counter：沿用旧内核的估算（中日韩字符 0.75，其余 3.3 字符 1 个）。"""
        from .models import to_openai
        lc = [m for m in messages if hasattr(m, "type")]
        try:
            oa = to_openai(lc)
        except Exception:
            oa = [{"role": "user", "content": text_of(getattr(m, "content", m))} for m in lc]
        return context.estimate(oa)

    def on_summary(self, new_messages):
        for m in new_messages:
            if isinstance(m, HumanMessage) and (m.additional_kwargs or {}).get("lc_source") == "summarization":
                text = text_of(m.content)
                if text.startswith(SUMMARY_HEAD):
                    text = text[len(SUMMARY_HEAD):].strip()
                last = self.store.one("SELECT MAX(seq) AS m FROM events WHERE session_id=?", (self.sid,))
                self.rt.emit(self.sid, "summary", {"text": text, "upto_seq": int((last or {}).get("m") or 0)})
                # 压缩前保存：排一次后台回顾（子代理的任务不回顾，request 里会跳过）
                reviewer = getattr(self.rt, "reviewer", None)
                if reviewer is not None:
                    reviewer.request(self.sid, "compress")
                return


def todo_items(todos):
    """TodoListMiddleware 的条目 {content, status: pending|in_progress|completed} → 我们的 {text, status: pending|doing|done}。"""
    out = []
    for t in todos or []:
        if isinstance(t, dict):
            out.append({"text": str(t.get("content") or t.get("text") or ""),
                        "status": TODO_STATUS.get(t.get("status"), t.get("status") if t.get("status") in ("pending", "doing", "done") else "pending")})
    return out


def _tool_name(t):
    if isinstance(t, dict):
        return t.get("name") or (t.get("function") or {}).get("name")
    return getattr(t, "name", None)


def _find_ai(resp):
    msgs = getattr(resp, "result", None)
    if msgs is None and hasattr(resp, "model_response"):
        msgs = getattr(resp.model_response, "result", None)
    if msgs is None and isinstance(resp, AIMessage):
        return resp
    for m in reversed(msgs or []):
        if isinstance(m, AIMessage):
            return m
    return None


def _replace_ai(resp, ai, ai2):
    from langchain.agents.middleware import ModelResponse
    if isinstance(resp, AIMessage):
        return ai2
    if hasattr(resp, "model_response"):          # ExtendedModelResponse
        inner = _replace_ai(resp.model_response, ai, ai2)
        try:
            return type(resp)(model_response=inner, command=getattr(resp, "command", None))
        except TypeError:
            resp.model_response = inner
            return resp
    msgs = [ai2 if m is ai else m for m in (resp.result or [])]
    return ModelResponse(result=msgs, structured_response=getattr(resp, "structured_response", None))
