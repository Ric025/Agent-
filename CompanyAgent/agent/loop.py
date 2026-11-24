"""执行循环：一个任务一个线程。每一圈先看账本再决定做什么，所以进程重启后能自然接上。

一圈：读任务和事件 → 有待处理的工具调用就逐个处理（校验、批准、执行、记结果）→
否则组装上下文问模型 → 把回复写进账本 → 回到开头。"""
from __future__ import annotations
import json, os, threading, time
from . import context, policy
from .llm import Cancelled, LLMError
from .store import digest_of, new_id
from .tools import ToolError, validate, available, to_text, REGISTRY

UNKNOWN_TEXT = "此动作在执行中途程序中断，结果未知。不要重做。先核实现场（例如检查文件是否已经生成），或者询问用户。"
VOID_TEXT = "未执行：用户更改了要求"
CANCEL_TEXT = "未执行：用户取消了任务"
DISABLED_TEXT = "这个工具已被管理员或用户禁用。"
DENIED_TEXT = "用户拒绝了这个操作。不要换一种方式重试，问用户想怎么做。"
MISMATCH_TEXT = "未执行：批准的内容和要执行的内容不一致。"

TERMINAL = ("done", "failed", "unknown", "denied", "voided")
# 这些工具"执行中"其实是在等人，没有副作用；中断后可以安全地重新进入
REENTRANT = ("ask_user",)


class Interrupt(Exception):
    """等人期间被暂停或取消。由循环按暂停或取消处理。"""

    def __init__(self, kind):
        super().__init__(kind)
        self.kind = kind      # pause | cancel | stale（主任务改了要求，子代理该停了）


class Shutdown(Exception):
    """运行时正在关闭：线程直接退出，不改账本里的状态（下次启动由 recover 处理）。"""


class Counters:
    """预算计数。主任务用 Runtime 里的 Live（用户继续时清零）；子代理用自己的。"""

    def __init__(self):
        self.reset()

    def reset(self):
        self.steps = 0
        self.failures = 0
        self.started = time.time()
        self.waited = 0.0      # 等人批准、等人回答的时间不算进总时长
        self.browser_actions = 0   # 这次运行里做了几次浏览器动作（browser.max_actions）


class Ctx:
    """交给工具处理函数的上下文。"""

    def __init__(self, runner):
        self.runner = runner
        self.rt = runner.rt
        self.store = runner.store
        self.cfg = runner.cfg
        self.sid = runner.sid
        self.ws = runner.ws
        self.root = runner.ws["path"]
        self.is_sub = runner.sub
        from . import modes
        self.mode = modes.of_session(runner.store, runner.sid)      # 对话的模式（docs/两种模式.md 1.3）
        self.call_id = None
        self.action_id = None

    def emit(self, etype, payload):
        return self.rt.emit(self.sid, etype, payload)

    def ask(self, question, options=None):
        return self.runner.ask(question, options or [])


# ---------------- 从账本读状态的小函数 ----------------

def pending_calls(events):
    """最后一条 assistant_message 里还没有 tool_result 的调用。返回 (那条事件, [调用])。"""
    amsg = None
    for e in reversed(events):
        if e["type"] == "assistant_message":
            amsg = e
            break
    if not amsg or not amsg["payload"].get("tool_calls"):
        return None, []
    ids = {c["id"] for c in amsg["payload"]["tool_calls"]}
    done = {e["payload"].get("call_id") for e in events
            if e["type"] == "tool_result" and e["seq"] > amsg["seq"] and e["payload"].get("call_id") in ids}
    return amsg, [c for c in amsg["payload"]["tool_calls"] if c["id"] not in done]


def last_chat_event(events):
    for e in reversed(events):
        if e["type"] in context.CHAT_TYPES:
            return e
    return None


def open_question(events):
    """还没回答、调用也还没出结果的提问。"""
    answered = {e["payload"].get("call_id") for e in events if e["type"] in ("answer", "tool_result")}
    for e in reversed(events):
        if e["type"] == "question" and e["payload"].get("call_id") not in answered:
            return e
    return None


def action_for(store, sid, call_id):
    r = store.one("SELECT id FROM actions WHERE session_id=? AND call_id=? ORDER BY created DESC LIMIT 1", (sid, call_id))
    return store.action(r["id"]) if r else None


def write_result(rt, sid, call, ok, content, action_id=None, tool=None, truncated=False, image=None, extra=None):
    p = {"call_id": call["id"], "action_id": action_id, "tool": tool or call.get("name"), "ok": bool(ok), "content": content}
    if truncated:
        p["truncated"] = True
    if image:
        p["image"] = image        # 截图（data URL）：只有最近一次给模型看（context.project_events）
    if extra:
        for k, v in extra.items():        # 工具给界面的结构化数据（kb_search 的 hits、display 等，docs/两种模式.md 3.1）
            p.setdefault(k, v)
    return rt.emit(sid, "tool_result", p)


def with_display(payload, tool, ctx, args):
    """tool_started 和 approval_requested 里加一个给界面用的 display（docs/操控电脑.md 2.4）。"""
    d = tool.display_of(ctx, args) if tool is not None and hasattr(tool, "display_of") else None
    if d:
        payload["display"] = d
    return payload


def cancel_pending(rt, sid):
    """任务取消：给还没结果的调用补上"已取消"，没做完的动作作废。"""
    store = rt.store
    _, pend = pending_calls(store.events(sid))
    for c in pend:
        a = action_for(store, sid, c["id"])
        if a and a["state"] not in TERMINAL:
            store.set_action(a["id"], "voided")
            store.audit(sid, a, "取消", "未执行")
        write_result(rt, sid, c, False, CANCEL_TEXT, a["id"] if a else None)
    for a in store.actions(sid, states=("proposed", "awaiting", "approved")):
        store.set_action(a["id"], "voided")


def _args_digest(args):
    s = json.dumps(args, ensure_ascii=False, sort_keys=True)
    return s if len(s) <= 300 else s[:300] + "…"


# ---------------- 执行循环 ----------------

class Runner:
    def __init__(self, rt, sid, llm, sub=False, ctl=None, counters=None):
        self.rt, self.store, self.cfg = rt, rt.store, rt.cfg
        self.sid, self.llm, self.sub = sid, llm, sub
        self.ctl = ctl or sid           # 暂停、取消标志看哪个任务（子代理跟随主任务）
        s = self.store.session(sid)
        self.ws = self.store.workspace(s["workspace_id"]) if s else None
        if not self.ws:
            raise RuntimeError("任务所在的工作区不存在了")
        self.counters = counters or Counters()
        self.parent_rev = None
        if sub and s.get("parent_id"):
            p = self.store.session(s["parent_id"])
            self.parent_rev = p["revision"] if p else None
        self.ctx = Ctx(self)
        # 系统提示在一次运行中保持不变。工具清单（Manus 的经验：中途变化会让模型困惑、缓存失效）只在加载新的一组时变
        # （docs/借鉴开源项目.md 3.4）：self.tools 是这次能用的全部工具（校验、执行都按它），
        # 给模型看的是 self.toolset.visible()（按需加载时是带上的那几组 + tool_load）
        self.tools = {}
        for t in available(self.ctx, sub):
            self.tools.setdefault(t.name, t)
        from . import toolsets
        self.toolset = toolsets.Toolset(rt, sid, self.ctx.mode, list(self.tools.values()), sub=sub)
        if self.toolset.lazy:
            self.tools[toolsets.TOOL_LOAD] = self.toolset.load_tool()
        self.guard = toolsets.LoopGuard(0 if sub else toolsets.guard_limit(self.cfg))
        if not sub and counters is not None and hasattr(counters, "toolset"):
            counters.toolset = self.toolset          # /状态 读运行中的那一份
        self.system = context.system_prompt(rt, self.ws, sub, mode=self.ctx.mode)
        if not sub and hasattr(llm, "begin_run"):
            llm.begin_run()        # 每次运行从头选端点（docs/操控电脑.md 7.3）

    @property
    def specs(self):
        """这一次模型调用给模型看的工具定义。"""
        return self.toolset.specs()

    def _new_turn(self):
        """运行中来了新的用户发言（插话）：补算信号；新的一轮，重复调用检测清零。"""
        if self.toolset.refresh():
            self.guard.reset()

    # ---- 小工具 ----
    def budget(self, key):
        return self.cfg.get("budgets", key)

    def live(self):
        return self.rt._live(self.ctl)

    def set_status(self, status, reason=None):
        self.rt.set_status(self.sid, status, reason)

    def notice(self, text):
        self.rt.emit(self.sid, "notice", {"text": text})

    def _check_flags(self):
        """每次要做下一件事之前看一眼：关闭、取消、暂停。"""
        if self.rt.closing:
            raise Shutdown()
        lv = self.live()
        if self.sub:
            if lv.cancel:
                raise Interrupt("cancel")
            if lv.pause:
                raise Interrupt("pause")
            if self._parent_changed():
                raise Interrupt("stale")
            return None
        if lv.cancel:
            return "cancel"
        if lv.pause:
            return "pause"
        return None

    def _parent_changed(self):
        if self.parent_rev is None:
            return False
        s = self.store.session(self.store.session(self.sid)["parent_id"])
        return bool(s) and s["revision"] != self.parent_rev

    # ---- 主循环 ----
    def run(self):
        """跑到需要停下为止，返回最终状态。主任务通过 rt._end 落状态（期间有人"踢"一下就再转一圈）。"""
        while True:
            r = self.step()
            if r is None:
                continue
            status, reason = r
            if self.sub:
                self.set_status(status, reason)
                return status
            if self.rt._end(self.sid, status, reason):
                return status

    def step(self):
        flag = self._check_flags()
        if flag == "cancel":
            cancel_pending(self.rt, self.sid)
            return ("cancelled", None)
        if flag == "pause":
            return ("paused", "用户暂停")
        s = self.store.session(self.sid)
        events = self.store.events(self.sid)
        amsg, pend = pending_calls(events)
        if pend:
            return self.handle_call(s, amsg, pend)
        last = last_chat_event(events)
        if last is None:
            return ("idle", None)
        if last["type"] == "assistant_message" and not last["payload"].get("tool_calls"):
            return ("done", None)
        c = self.counters
        max_steps = self.budget("sub_agent_steps" if self.sub else "max_steps") or 40
        if not self.sub:
            # docs/两种模式.md 9.4：深入研究的那一轮步数上限翻倍（不超过 60）
            max_steps = context.step_limit(max_steps, context.turn_is_deep(events))
        if c.steps >= max_steps:
            self.notice("已经连续执行了 %d 步，先停下来。看一下进展，需要的话点继续。" % c.steps)
            return ("paused", "步数用尽")
        minutes = self.budget("max_minutes") or 30
        if time.time() - c.started - c.waited > minutes * 60:
            self.notice("这次已经运行超过 %d 分钟，先停下来。需要的话点继续。" % minutes)
            return ("paused", "超时")
        return self.think(s["revision"])

    # ---- 问模型 ----
    def think(self, rev):
        if not self.sub:
            self._new_turn()
        ev = threading.Event()
        lv = self.live()
        with self.rt.cv:
            if self.rt.closing or lv.cancel or lv.pause:
                return None
            lv.llm_cancel = ev
        try:
            events = self.store.events(self.sid)
            try:
                messages = self.build(events, ev)
            except LLMError as e:
                if e.kind != "no_vision":
                    raise
                return self._tell(rev, str(e))
            # 片段不落账、没有序号。带上"这次调用开始时账本写到了第几条"，界面装载对话时
            # 如果已经装进了这次调用的最终回答，就能认出这些片段是过时的，不会把同一段话再显示一遍
            base = self.store.last_seq()
            try:
                res = self.llm.chat(messages, tools=self.specs or None,
                                    on_delta=lambda t: self.rt.emit(self.sid, "assistant_delta", {"text": t, "base": base}, store=False),
                                    cancel=ev, max_tokens=self.cfg.get("model", "max_output"))
            except (Cancelled, LLMError):
                raise
            except Exception as e:
                # 模型服务回了格式不对的内容等意外情况：同样按"模型不可用"暂停，而不是让任务出错
                raise LLMError("模型返回了无法处理的内容：%s: %s" % (type(e).__name__, e), "bad_response")
        except Cancelled:
            self._notes()
            return None
        except LLMError as e:
            self._notes()
            if e.kind == "no_vision":
                return self._tell(rev, str(e))
            self.rt.emit(self.sid, "error", {"text": str(e)})
            return ("paused", "模型不可用：%s" % e)
        finally:
            with self.rt.cv:
                if lv.llm_cancel is ev:
                    lv.llm_cancel = None
        self._notes()
        # 判断"要求变了没有"和写入回复放在同一把锁里，避免和用户插话交错
        with self.rt.cv:
            s = self.store.session(self.sid)
            if s["revision"] != rev:
                return None              # 用户改了要求：这次回复作废
            if self.sub and self._parent_changed():
                return None
            if not (res.content or "").strip() and not res.tool_calls:
                self.counters.failures += 1
                if self.counters.failures >= (self.budget("max_failures") or 4):
                    self.rt.emit(self.sid, "error", {"text": "模型连续返回空回复"})
                    return ("paused", "连续失败")
                return None
            calls = self._unique_ids(res.tool_calls, self.store.events(self.sid))
            self.rt.emit(self.sid, "assistant_message",
                         {"content": res.content or "", "tool_calls": calls, "usage": res.usage, "endpoint": res.endpoint},
                         revision=rev)
            self.counters.steps += 1
        return None

    def _notes(self):
        """模型客户端留下的话（换了模型、发现某个模型看不了图）：发成 notice。"""
        take = getattr(self.llm, "take_note", None)
        note = take() if callable(take) else None
        if note:
            self.notice(note)

    def _tell(self, rev, text):
        """程序自己能判断的事（需要看图而没有模型能看）：不发请求，直接用原话告诉用户（docs/操控电脑.md 1.3、7.3）。"""
        from . import capability
        fb = capability.FALLBACK.get("image_input", "")
        with self.rt.cv:
            if self.store.session(self.sid)["revision"] != rev:
                return None
            self.rt.emit(self.sid, "assistant_message", {"content": text + fb, "tool_calls": [], "usage": {}, "endpoint": ""},
                         revision=rev)
        return None

    def _unique_ids(self, calls, events):
        """调用 id 在一个任务里必须唯一（动作按 call_id 查找）。模型或假模型重启后可能给出重复 id，这里改掉。"""
        seen = set()
        for e in events:
            if e["type"] == "assistant_message":
                seen |= {c["id"] for c in e["payload"].get("tool_calls") or []}
        out = []
        for c in calls:
            c = dict(c)
            if not c.get("id") or c["id"] in seen:
                c["id"] = new_id("call_")
            seen.add(c["id"])
            out.append(c)
        return out

    def build(self, events, cancel):
        """组装消息；超出预算时先压缩再组装。预算按这一次实际会用的端点算（docs/操控电脑.md 7.3）。
        需要看图而没有任何端点能看：最新一条用户发言带图时抛 LLMError(no_vision)，更早的图换成文字。"""
        fixed = context.estimate([{"role": "system", "content": self.system}]) + context.estimate_tools(self.specs)
        msgs = self._vision_filter(context.project(events))
        system = [{"role": "system", "content": self.system}]
        plan = getattr(self.llm, "plan", None)
        ep = plan(system + msgs, self.specs or None) if callable(plan) else None
        budget = context.input_budget(self.cfg, ep)
        if fixed + context.estimate(msgs) > budget:
            if context.compress(self.rt, self.sid, self.llm, budget - fixed, cancel) is not None:
                msgs = self._vision_filter(context.project(self.store.events(self.sid)))
        return system + msgs

    def _vision_filter(self, msgs):
        """图片只在需要时让模型看（docs/操控电脑.md 7.3）：
        - 最新一条用户消息（含截图）带图：要能看图；没有任何端点能看 → 抛 LLMError(no_vision)；
        - 只有更早的消息带图：这次要用的端点看得了就留着，看不了就换成文字（平时还是排在前面的模型在做）。"""
        from . import capability
        if not capability.has_images(msgs):
            return msgs
        caps = capability.get(self.rt)
        if capability.last_user_has_images(msgs):
            order, why = caps.candidates(need_vision=True)
            if not order:
                raise LLMError(why, "no_vision")
            return msgs
        order, _ = caps.candidates(need_tools=bool(self.specs), sticky=getattr(self.llm, "sticky", 0) or 0)
        eps = caps.endpoints()
        if order and order[0] < len(eps) and caps.qualifies(eps[order[0]], order[0], vision=True):
            return msgs
        return capability.strip_images(msgs, "[图片已省略]")

    # ---- 处理一个工具调用 ----
    def handle_call(self, s, amsg, pend):
        rt, store, sid = self.rt, self.store, self.sid
        call = pend[0]
        # 1. 用户改了要求：这条回复里还没执行的调用全部作废
        if (amsg["revision"] or 0) < s["revision"]:
            for c in pend:
                a = action_for(store, sid, c["id"])
                if a and a["state"] not in TERMINAL:
                    store.set_action(a["id"], "voided")
                    store.audit(sid, a, "作废", "用户更改了要求")
                write_result(rt, sid, c, False, VOID_TEXT, a["id"] if a else None)
            return None
        # 2. 账本里已有这个调用的动作：多半是重启后接着做
        a = action_for(store, sid, call["id"])
        if a:
            st = a["state"]
            if st == "running" and a["tool"] in REENTRANT:
                store.set_action(a["id"], "approved")
                return self.execute(store.action(a["id"]), call, "恢复后继续")
            if st in ("running", "unknown"):
                if st == "running":
                    store.set_action(a["id"], "unknown")
                    store.audit(sid, a, "恢复", "结果未知")
                write_result(rt, sid, call, False, UNKNOWN_TEXT, a["id"])
                return self.after(False)
            if st in ("done", "failed"):
                r = a["result"] or {}
                write_result(rt, sid, call, r.get("ok", st == "done"), r.get("content", ""), a["id"], truncated=r.get("truncated"),
                             extra=r.get("extra"))
                return None
            if st == "denied":
                write_result(rt, sid, call, False, DENIED_TEXT if self._was_user_denied(a) else DISABLED_TEXT, a["id"])
                return None
            if st == "voided":
                write_result(rt, sid, call, False, VOID_TEXT, a["id"])
                return None
            if st == "awaiting":
                return self.wait_approval(a, call)
            if st == "approved":
                return self.execute(a, call, "用户批准")
            # proposed：上次在登记后、判定前中断，按新调用继续
        # 3. 校验
        tool = self.tools.get(call.get("name"))
        args = call.get("arguments")
        err = None
        if not tool:
            err = "没有这个工具：%s。可用的工具有：%s" % (call.get("name"), "、".join(self.tools))
        elif call.get("error") or not isinstance(args, dict):
            err = "参数解析失败：%s。请输出合法的 JSON 对象作为参数。" % (call.get("error") or "参数必须是对象")
        else:
            if tool.name in REGISTRY:
                extra = [k for k in args if k not in tool.props]
                if extra:
                    err = "参数有误：不认识的参数 %s。" % "、".join(extra)
            if not err:
                try:
                    err = validate(tool.schema(), args)
                except Exception:
                    err = None       # 外部连接器的参数格式可能超出我们的校验范围，交给对方校验
                if err:
                    err = "参数有误：%s。请改正后重试。" % err
        if err:
            write_result(rt, sid, call, False, err)
            return self.after(False)
        # 没带的工具：存在、模式和开关都允许 → 加载它所在的组，照常执行（docs/借鉴开源项目.md 3.4）
        self._touch(tool.name)
        # 重复调用检测（3.8）：同一个调用、同样的结果连续出现，这一次不执行
        n = self.guard.check(tool.name, args)
        if n:
            return self._blocked(call, tool.name, args, n)
        # 4. 定风险、判定
        try:
            risk = tool.risk_of(self.ctx, args)
        except Exception:
            risk = "overwrite"
        # 资料库只读（docs/两种模式.md 2.1）：写工具指向资料库里的路径，不问用户，直接拒绝
        from . import kb as kbm
        ro = kbm.write_violation(self.ctx, tool.name, args, risk)
        if ro:
            write_result(rt, sid, call, False, ro)
            return self.after(False)
        setting = store.tool_setting(tool.name)
        if self.sub:
            decision = "deny" if setting == "deny" else "allow"     # 子代理只有只读工具，不需要批准
        else:
            decision = policy.decide(risk, self.cfg.get("approval_mode") or "auto", setting)
        if a is None:
            a = store.add_action(sid, s["revision"], call["id"], tool.name, args, risk)
        if decision == "deny":
            store.set_action(a["id"], "denied", {"by": "setting"})
            store.audit(sid, a, "已禁用", "未执行")
            write_result(rt, sid, call, False, DISABLED_TEXT, a["id"])
            return None
        if decision == "ask":
            store.set_action(a["id"], "awaiting")
            rt.emit(sid, "approval_requested", with_display({"action_id": a["id"], "call_id": call["id"], "tool": tool.name,
                                                             "args": args, "risk": risk}, tool, self.ctx, args))
            return self.wait_approval(store.action(a["id"]), call)
        store.set_action(a["id"], "approved")
        return self.execute(store.action(a["id"]), call, "自动放行")

    def _touch(self, name):
        """不按需加载时只记"用过的组"（之后打开按需加载也照样带着）；子代理不记。"""
        if name != "tool_load" and not self.sub:
            self.toolset.ensure(name)

    def _blocked(self, call, name, args, n):
        """拦下重复的调用：给模型一条结果，记 loop_guard 事件。被拦之后又发同样的调用：停下来问用户。"""
        from . import toolsets
        again = self.guard.block(call["id"], name, args)
        self.rt.emit(self.sid, "loop_guard", {"tool": name, "count": n, "call_id": call["id"]})
        write_result(self.rt, self.sid, call, False, toolsets.GUARD_TEXT)
        if again:
            self.notice(toolsets.GUARD_STOP_TEXT)
            return ("paused", toolsets.GUARD_REASON)
        return None

    def _was_user_denied(self, a):
        return not (isinstance(a.get("result"), dict) and a["result"].get("by") == "setting")

    def wait_approval(self, a, call):
        """等用户批准。批准 → 执行；拒绝 → 告诉模型；改要求、取消 → 回循环开头；暂停 → 线程结束，动作保持等待。"""
        rt, store = self.rt, self.store
        self.set_status("waiting_approval")
        t0 = time.time()
        try:
            with rt.cv:
                while True:
                    if rt.closing:
                        raise Shutdown()
                    lv = self.live()
                    cur = store.action(a["id"])
                    s = store.session(self.sid)
                    if lv.cancel:
                        return None
                    if cur["state"] == "approved" and cur["revision"] == s["revision"]:
                        break
                    if cur["state"] == "denied":
                        break
                    if cur["state"] != "awaiting" or s["revision"] != cur["revision"]:
                        return None
                    if lv.pause:
                        return ("paused", "用户暂停")
                    rt.cv.wait(30)
        finally:
            self.counters.waited += time.time() - t0
        if cur["state"] == "denied":
            store.audit(self.sid, cur, "用户拒绝", "未执行")
            self.set_status("running")
            write_result(rt, self.sid, call, False, DENIED_TEXT, cur["id"])
            return None
        self.set_status("running")
        return self.execute(cur, call, "用户批准")

    def execute(self, a, call, decision):
        rt, store, sid = self.rt, self.store, self.sid
        s = store.session(sid)
        # 批准绑定具体内容：状态、修订号、参数摘要都要对得上
        if a["state"] != "approved" or a["revision"] != s["revision"]:
            store.set_action(a["id"], "voided")
            store.audit(sid, a, decision, "作废")
            return None
        if a["digest"] != digest_of(a["tool"], a["args"], a["revision"]) or a["tool"] != call.get("name"):
            store.set_action(a["id"], "voided")
            store.audit(sid, a, decision, "作废：批准内容不一致")
            write_result(rt, sid, call, False, MISMATCH_TEXT, a["id"])
            return None
        tool = self.tools.get(a["tool"])
        if tool is None:
            store.set_action(a["id"], "failed", {"ok": False, "content": "这个工具现在不可用"})
            write_result(rt, sid, call, False, "这个工具现在不可用", a["id"])
            return self.after(False)
        args = a["args"]
        store.set_action(a["id"], "running")       # 先落盘"正在做"，崩溃后据此判断结果未知
        rt.emit(sid, "tool_started", with_display({"action_id": a["id"], "call_id": call["id"], "tool": a["tool"], "args": args},
                                                  tool, self.ctx, args))
        self.ctx.call_id, self.ctx.action_id = call["id"], a["id"]
        limit = self.budget("tool_result_chars") or 6000
        if hasattr(tool, "display") and tool.display is not None:
            limit = max(limit, 60000)     # 页面概况自己按预算裁剪过，这里不再截
        truncated = False
        image = None
        extra = None
        try:
            result = tool.handler(self.ctx, **args)
            image = getattr(result, "image", None)
            extra = getattr(result, "extra", None)
            text, truncated = to_text(result, limit)
            if truncated:
                full, _ = to_text(result, 1 << 60)
                text += "\n（结果过长已截断，完整内容在 %s，需要时用 read_file 读取）" % self._save_full(a["id"], full)
            ok = True
        except Interrupt as e:
            if e.kind == "pause" or (e.kind == "stale" and not self.sub):
                store.set_action(a["id"], "approved")   # 等人的工具没有副作用，恢复后重新进入
            return None
        except (ToolError, policy.PathError) as e:
            ok, text = False, str(e)
        except Shutdown:
            raise
        except Exception as e:
            ok, text = False, "工具内部错误：%s: %s" % (type(e).__name__, e)
        finally:
            self.ctx.call_id = self.ctx.action_id = None
        res = {"ok": ok, "content": text, "truncated": truncated}
        if ok and extra:
            res["extra"] = extra
        store.set_action(a["id"], "done" if ok else "failed", res)
        write_result(rt, sid, call, ok, text, a["id"], truncated=truncated, image=image if ok else None,
                     extra=extra if ok else None)
        self.guard.record(a["tool"], args, ok, text)
        if ok and a["tool"] == "skill_load":
            self.toolset.after_skill_load((args or {}).get("name"))
        store.audit(sid, store.action(a["id"]), decision, "成功" if ok else "失败", ("%s → %s" % (_args_digest(args), text[:150])))
        return self.after(ok)

    def _save_full(self, aid, full):
        d = os.path.join(self.ws["path"], policy.INTERNAL_DIR, "scratch", self.sid)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, aid + ".txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write(full)
        return policy.relpath(self.ws["path"], p)

    def after(self, ok):
        """记连续失败次数，到上限就停下来问人。"""
        if ok:
            self.counters.failures = 0
            return None
        self.counters.failures += 1
        limit = self.budget("max_failures") or 4
        if self.counters.failures >= limit:
            self.notice("连续 %d 次操作没有成功，先停下来。看一下错误，需要的话点继续。" % self.counters.failures)
            return ("paused", "连续失败")
        return None

    # ---- 向用户提问 ----
    def ask(self, question, options):
        rt, store, sid = self.rt, self.store, self.sid
        call_id = self.ctx.call_id
        evs = store.events(sid, types=("question", "answer"))
        for e in reversed(evs):
            if e["type"] == "answer" and e["payload"].get("call_id") == call_id:
                return e["payload"].get("text") or ""
        if not any(e["type"] == "question" and e["payload"].get("call_id") == call_id for e in evs):
            rt.emit(sid, "question", {"call_id": call_id, "question": question, "options": list(options or [])})
        self.set_status("waiting_input")
        t0 = time.time()
        try:
            with rt.cv:
                while True:
                    if rt.closing:
                        raise Shutdown()
                    lv = self.live()
                    if lv.cancel:
                        raise Interrupt("cancel")
                    if lv.pause:
                        raise Interrupt("pause")
                    for e in reversed(store.events(sid, types=("answer",))):
                        if e["payload"].get("call_id") == call_id:
                            self.set_status("running")
                            return e["payload"].get("text") or ""
                    rt.cv.wait(30)
        finally:
            self.counters.waited += time.time() - t0
