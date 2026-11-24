"""宿主：运行时里新内核的入口。管 SQLite 检查点、任务线程的主循环、子代理、连接器缓存。

任务线程的主循环（Runner.step）每一圈先看检查点和账本，再决定做什么：
  - 有用户插话（账本里有还没交给图的 user_message）→ 补齐没结果的调用，带上新消息开始一次新的运行；
  - 停在中断上（等批准 / 等回答）→ 等用户在界面上做决定，然后用 Command(resume=...) 接着跑；
  - 上次运行没跑完（暂停、模型不可用、程序重启）→ invoke(None) 从检查点接着跑；
  - 都没有 → 看最后一条消息决定 done / idle。
暂停、取消、改要求由 Runtime 设标志，图里的中间件看到后抛 Abort 结束这次运行，回到这里处理。"""
from __future__ import annotations
import inspect, os, sqlite3, threading, time, traceback

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command

from .. import context
from ..loop import (VOID_TEXT, DENIED_TEXT, UNKNOWN_TEXT, TERMINAL, Counters, Shutdown, action_for, cancel_pending,
                    pending_calls, write_result)
from . import models, tools as lgtools, middleware
from .kernel import TaskKernel, Abort, ModelUnavailable, LIMIT_ERRORS

RECURSION_LIMIT = 100000      # 步数由 ModelCallLimitMiddleware 管；LangGraph 默认的 25 个超步不够一圈圈跑


class Host:
    def __init__(self, rt):
        self.rt = rt
        self.lock = threading.RLock()
        self._saver = None
        self._conn = None
        self._inflight = set()        # (任务 id, 调用 id)：正在本进程里执行的工具调用
        self.mcp = {}
        self._durability = None

    # ---------------- 检查点 ----------------
    def saver(self):
        """SqliteSaver(sqlite3.Connection)。连接跨线程使用，所以 check_same_thread=False。"""
        with self.lock:
            if self._saver is None:
                from langgraph.checkpoint.sqlite import SqliteSaver
                path = os.path.join(self.rt.cfg.home, "checkpoints.db")
                self._conn = sqlite3.connect(path, check_same_thread=False)
                try:
                    self._conn.execute("PRAGMA journal_mode=WAL")
                except sqlite3.Error:
                    pass
                self._saver = SqliteSaver(self._conn)
                if hasattr(self._saver, "setup"):
                    self._saver.setup()
            return self._saver

    def close(self):
        with self.lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
            self._conn = self._saver = None

    def forget(self, thread_ids):
        """删任务时顺带删它的检查点（失败不影响删除）。"""
        if self._saver is None:
            return
        for t in thread_ids:
            try:
                self._saver.delete_thread(t)
            except Exception:
                pass

    # ---------------- 手动整理（docs/指令和主题.md 1.3） ----------------
    def cannot_reset(self):
        """手动整理后要删掉这个任务的检查点。做不到时返回原因（中文），做得到返回 ""。"""
        try:
            saver = self.saver()
        except Exception as e:
            return "新内核的检查点打不开，不能手动整理：%s" % e
        if not callable(getattr(saver, "delete_thread", None)):
            return "新内核的检查点不支持删除（langgraph-checkpoint 版本太旧），不能手动整理"
        return ""

    def reset_thread(self, sid):
        """账本里写下摘要之后调用：删掉检查点，下次运行走 Runner.first_run，从账本（摘要 + 之后的发言）重新装载。
        和 forget 不同，这里先打开检查点（程序刚启动、还没跑过任务时 _saver 还是 None），删除失败向外抛。"""
        self.saver().delete_thread(sid)

    def compact_llm(self):
        """手动整理用的模型客户端（要有 agent.llm 的 chat 接口，context.compress 用它）。
        运行时带了自定义 llm_factory（测试、--mock）时：给的是 chat 接口的对象就直接用，LangChain 模型包一层；
        否则用过渡内核的 OpenAIClient（按端点配置，带换台）。"""
        if getattr(self.rt, "llm_factory_custom", False):
            obj = self.rt.llm_factory()
            if callable(getattr(obj, "chat", None)):
                return obj
            return models.ChatAdapter(obj)
        from ..llm import OpenAIClient
        return OpenAIClient(self.rt.cfg)

    # ---------------- 执行中的调用 ----------------
    def set_inflight(self, sid, cid, on):
        with self.rt.cv:
            if on:
                self._inflight.add((sid, cid))
            else:
                self._inflight.discard((sid, cid))
            self.rt.cv.notify_all()

    def is_inflight(self, sid, cid):
        return (sid, cid) in self._inflight

    def wait_inflight(self, sid, cid, kernel):
        with self.rt.cv:
            while (sid, cid) in self._inflight:
                if self.rt.closing:
                    raise Abort("shutdown")
                self.rt.cv.wait(0.2)

    # ---------------- 建图 ----------------
    def build_graph(self, kernel, checkpointer):
        from langchain.agents import create_agent
        main, fallbacks, summary = models.build(self.rt, kernel)
        return create_agent(model=main, tools=lgtools.to_langchain(kernel), system_prompt=kernel.system,
                            middleware=middleware.build(kernel, main, fallbacks, summary),
                            checkpointer=checkpointer, name="sub_agent" if kernel.sub else "agent")

    def invoke_kwargs(self, graph):
        """durability="sync"：每一步的检查点写完再往下走（LangGraph 1.x 的参数；没有就不传）。"""
        if self._durability is None:
            try:
                self._durability = "durability" in inspect.signature(graph.invoke).parameters
            except (TypeError, ValueError):
                self._durability = False
        return {"durability": "sync"} if self._durability else {}

    # ---------------- 入口 ----------------
    def run_task(self, sid, lv):
        try:
            r = Runner(self, sid, lv)
        except models.NoModel as e:
            self.rt.emit(sid, "error", {"text": str(e)})
            self.rt._end(sid, "paused", "模型不可用：%s" % e)
            return
        r.run()

    def mcp_tools(self, ctx):
        return lgtools.mcp_tools(self, ctx)

    def reset_mcp(self):
        with self.lock:
            self.mcp.clear()

    def run_review(self, system, user_text, apply):
        """后台回顾（docs/记忆设计.md 5.3）：create_agent 只带 memory 一个工具，最多问 4 次模型，不带检查点。
        apply(action, target, content, old_text) -> str 由 review.py 提供，两个内核共用；错误文字原样回给模型。
        模型调用失败时异常向外抛，由 review.py 记失败次数。"""
        from langchain.agents import create_agent
        from langchain.agents.middleware import ModelCallLimitMiddleware
        from langchain_core.tools import StructuredTool
        from ..review import MAX_ROUNDS
        from ..tools import REGISTRY, validate
        spec = REGISTRY["memory"]

        def run(**kwargs):
            extra = [k for k in kwargs if k not in spec.props]
            err = ("不认识的参数 %s" % "、".join(extra)) if extra else validate(spec.schema(), kwargs)
            if err:
                return "参数有误：%s。请改正后重试。" % err
            return apply(kwargs.get("action"), kwargs.get("target"), kwargs.get("content"), kwargs.get("old_text"))
        run.__name__ = "run_memory_review"
        tool = StructuredTool(name="memory", description=spec.desc, args_schema=spec.schema(), func=run)
        # exit_behavior="end"：到了次数就正常结束这次运行（不抛错），和过渡内核的小循环一致
        graph = create_agent(model=models.review_model(self.rt), tools=[tool], system_prompt=system,
                             middleware=[ModelCallLimitMiddleware(run_limit=MAX_ROUNDS, exit_behavior="end")],
                             name="memory_review")
        graph.invoke({"messages": [HumanMessage(content=user_text)]}, {"recursion_limit": 4 * MAX_ROUNDS + 4})

    def run_subagent(self, ctx, task):
        """子代理作为工具（LangChain 文档推荐的写法）：另建一个只有只读工具的 create_agent，
        在当前线程里同步跑完，返回它最后的回复。子代理在账本里是一个 kind="sub" 的任务。"""
        rt, store = self.rt, self.rt.store
        if ctx.is_sub:
            from ..tools import ToolError
            raise ToolError("子代理不能再派子代理")
        parent_k = getattr(ctx, "kernel", None)
        parent = ctx.sid
        sub = store.add_session(ctx.ws["id"], "子任务：" + task.strip()[:20], kind="sub", parent_id=parent)
        sid = sub["id"]
        rt.emit(sid, "user_message", {"text": task})
        rt.set_status(sid, "running")
        status, reason = "done", None
        try:
            k = TaskKernel(self, sid, sub=True, ctl=parent_k.ctl if parent_k else parent, counters=Counters())
            graph = self.build_graph(k, None)
            graph.invoke({"messages": [HumanMessage(content=task)]}, {"recursion_limit": RECURSION_LIMIT},
                         **self.invoke_kwargs(graph))
        except Abort as e:
            if e.kind == "stop":
                status, reason = e.status or "paused", e.reason
            elif e.kind in ("restart",):
                status, reason = "cancelled", "主任务的要求已变更"
            else:
                if e.kind != "shutdown":
                    rt.set_status(sid, "paused" if e.kind == "pause" else "cancelled",
                                  {"pause": "用户暂停", "stale": "主任务的要求已变更"}.get(e.kind))
                raise
        except (ModelUnavailable, models.NoModel) as e:
            rt.emit(sid, "error", {"text": str(e)})
            status, reason = "paused", "模型不可用：%s" % e
        except Exception as e:
            if type(e).__name__ in LIMIT_ERRORS:
                status, reason = "paused", "步数用尽"
            else:
                raise
        rt.set_status(sid, status, reason)
        last = None
        for e in reversed(store.events(sid, types=("assistant_message",))):
            last = e
            break
        content = (last["payload"].get("content") or "").strip() if last else ""
        if status == "done" and content:
            return content
        why = (store.session(sid)["meta"] or {}).get("reason") or status
        return "子代理没有得出结论（%s）。%s" % (why, ("它最后说：" + content) if content else "")


def pending_interrupts(snap):
    """StateSnapshot 上的中断。新版本有 snap.interrupts；老版本在 snap.tasks[i].interrupts 里。"""
    out, seen = [], set()
    items = list(getattr(snap, "interrupts", None) or ())
    for t in getattr(snap, "tasks", None) or ():
        items += list(getattr(t, "interrupts", None) or ())
    for it in items:
        key = getattr(it, "id", None) or id(it)
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def resume_command(intr, value):
    iid = getattr(intr, "id", None)
    return Command(resume={iid: value}) if iid else Command(resume=value)


class Runner:
    """一个任务线程。和旧内核的 loop.Runner 一样由 Runtime._run 启动，结束时通过 rt._end 落状态。"""

    def __init__(self, host, sid, lv):
        self.host, self.rt, self.store = host, host.rt, host.rt.store
        self.sid, self.lv = sid, lv
        self.kernel = TaskKernel(host, sid, counters=lv)
        self.graph = host.build_graph(self.kernel, host.saver())
        self.config = {"configurable": {"thread_id": sid}, "recursion_limit": RECURSION_LIMIT}
        self._last = None
        self._same = 0

    def run(self):
        while True:
            r = self.step()
            if r is None:
                continue
            status, reason = r
            if self.rt._end(self.sid, status, reason):
                return status

    # ---------------- 一圈 ----------------
    def step(self):
        rt, lv, sid = self.rt, self.lv, self.sid
        if rt.closing:
            raise Shutdown()
        if lv.cancel:
            cancel_pending(rt, sid)
            return ("cancelled", None)
        if lv.pause:
            return ("paused", "用户暂停")
        snap = self.graph.get_state(self.config)
        values = getattr(snap, "values", None) or {}
        msgs = values.get("messages") or []
        nxt = tuple(getattr(snap, "next", ()) or ())
        intrs = pending_interrupts(snap)
        # 防空转：同一个检查点、同样的待办连续出现，说明没有进展
        key = (str(((getattr(snap, "config", None) or {}).get("configurable") or {}).get("checkpoint_id")), nxt,
               tuple(getattr(i, "id", "") for i in intrs), len(self.kernel.unfed()))
        self._same = self._same + 1 if key == self._last else 0
        self._last = key
        if self._same >= 3:
            rt.emit(sid, "error", {"text": "新内核没有进展（检查点停在 %s），先停下来。" % (nxt,)})
            return ("paused", "内核状态异常")
        if not msgs and not nxt and not intrs:
            return self.first_run()
        if self.kernel.unfed():
            return self.invoke(self.feed(msgs))
        if intrs:
            return self.on_interrupt(intrs[0], msgs)
        if nxt:
            return self.invoke(None)
        last = msgs[-1]
        if isinstance(last, AIMessage) and not last.tool_calls:
            return ("done", None)
        fill = self.kernel.reconcile(msgs)
        if fill:
            return self.invoke({"messages": fill})
        # 图已经结束，但最后一条不是模型的最终回复（理论上不会发生）：停下来让人看到，而不是悄悄结束
        rt.emit(sid, "error", {"text": "新内核停在了一条%s消息之后，没有继续问模型。点继续会重新问一次。" % getattr(last, "type", "")})
        return ("paused", "内核状态异常")

    def first_run(self):
        """这个任务还没有检查点：新任务，或者旧内核留下的任务。把账本投影成消息作为第一次的输入。"""
        events = self.store.events(self.sid)
        if not any(e["type"] in context.CHAT_TYPES for e in events):
            return ("idle", None)
        unfed = self.kernel.unfed()
        last = None
        for e in reversed(events):
            if e["type"] in context.CHAT_TYPES:
                last = e
                break
        if not unfed and last["type"] == "assistant_message" and not last["payload"].get("tool_calls"):
            return ("done", None)
        self.settle_legacy(events)
        events = self.store.events(self.sid)
        msgs = [models.from_openai(m) for m in context.project(events)]
        users = [e["seq"] for e in events if e["type"] == "user_message"]
        if users:
            self.kernel.set_fed(max(users))
        if not msgs:
            return ("idle", None)
        return self.invoke({"messages": msgs})

    def settle_legacy(self, events):
        """旧内核留下的、没有结果的调用：按旧规则补上结果（执行中 → 结果未知；做完了 → 照抄；其他 → 作废）。"""
        store, sid = self.store, self.sid
        _, pend = pending_calls(events)
        for c in pend:
            a = action_for(store, sid, c["id"])
            if a and a["state"] == "running":
                store.set_action(a["id"], "unknown")
                store.audit(sid, a, "恢复", "结果未知")
                write_result(self.rt, sid, c, False, UNKNOWN_TEXT, a["id"])
            elif a and a["state"] in ("done", "failed"):
                r = a["result"] or {}
                write_result(self.rt, sid, c, r.get("ok", a["state"] == "done"), r.get("content", ""), a["id"],
                             truncated=r.get("truncated"))
            else:
                if a and a["state"] not in TERMINAL:
                    store.set_action(a["id"], "voided")
                write_result(self.rt, sid, c, False, VOID_TEXT, a["id"] if a else None)

    def feed(self, msgs):
        """带上新消息开始一次新的运行。之前停在半路的节点（等批准、等回答、模型调用被打断）由 LangGraph 丢弃，
        没结果的调用先按账本补齐，保证消息序列合法。"""
        unfed = self.kernel.unfed()
        out = self.kernel.reconcile(msgs)
        out += [models.human_from_event(e["payload"]) for e in unfed]
        if unfed:
            self.kernel.set_fed(max(e["seq"] for e in unfed))
        return {"messages": out}

    def invoke(self, inp):
        rt = self.rt
        if rt.store.session(self.sid)["status"] != "running":
            rt.set_status(self.sid, "running")
        try:
            self.graph.invoke(inp, self.config, **self.host.invoke_kwargs(self.graph))
        except Abort as e:
            if e.kind == "shutdown":
                raise Shutdown()
            if e.kind == "stop":
                return (e.status or "paused", e.reason)
            return None              # pause / cancel / restart：回到 step 开头按标志和账本处理
        except (ModelUnavailable, models.NoModel) as e:
            rt.emit(self.sid, "error", {"text": str(e)})
            return ("paused", "模型不可用：%s" % e)
        except Exception as e:
            if type(e).__name__ in LIMIT_ERRORS:
                steps = context.step_limit(self.kernel.budget("max_steps") or 40,
                                           context.turn_is_deep(self.store.events(self.sid)))
                rt.emit(self.sid, "notice", {"text": "已经连续执行了 %d 步，先停下来。看一下进展，需要的话点继续。" % steps})
                return ("paused", "步数用尽")
            raise
        return None

    # ---------------- 中断 ----------------
    def on_interrupt(self, intr, msgs):
        v = getattr(intr, "value", None)
        if isinstance(v, dict) and v.get("kind") == "question":
            return self.on_question(intr, v, msgs)
        if isinstance(v, dict) and "action_requests" in v:
            return self.on_approval(intr, v, msgs)
        self.rt.emit(self.sid, "error", {"text": "新内核停在一个不认识的中断上：%r" % (v,)})
        return ("paused", "内核状态异常")

    def _wait(self, status, ready):
        """等人：批准或回答。ready() 为真就返回 True；暂停、取消、插话返回 False（回到 step 开头处理）。"""
        rt, lv = self.rt, self.lv
        t0 = time.time()
        try:
            with rt.cv:
                while True:
                    if rt.closing:
                        raise Shutdown()
                    if lv.cancel or lv.pause or self.kernel.unfed():
                        return False
                    if ready():
                        return True
                    if rt.store.session(self.sid)["status"] != status:
                        rt.set_status(self.sid, status)
                    rt.cv.wait(30)
        finally:
            lv.waited += time.time() - t0

    def on_approval(self, intr, v, msgs):
        k, store, sid = self.kernel, self.store, self.sid
        ai = None
        for m in reversed(msgs):
            if isinstance(m, AIMessage):
                ai = m
                break
        gated = [c for c in (ai.tool_calls if ai else []) if k.asked(c.get("id"))]
        reqs = v.get("action_requests") or []
        if len(gated) != len(reqs):
            self.rt.emit(sid, "error", {"text": "批准状态和检查点对不上（%d 对 %d），先停下来。" % (len(gated), len(reqs))})
            return ("paused", "内核状态异常")
        if all(k.result_of(c["id"]) is not None for c in gated):
            # 这些调用已经有结果（例如任务被取消过）：不再恢复这次中断，补齐后带着结果接着问模型
            return self.invoke({"messages": k.reconcile(msgs)})

        def decided():
            acts = [action_for(store, sid, c["id"]) for c in gated]
            for a in acts:
                # 同一批里用户对某个工具点了"以后都允许"：同工具的其他待批准一并放行（删除除外）。
                # 旧内核逐个处理，后面的那个根本不会问；这里批准请求是一起发出的，所以补上这一步
                if a is not None and a["state"] == "awaiting" and a["risk"] != "delete" \
                        and store.tool_setting(a["tool"]) == "allow":
                    store.set_action(a["id"], "approved")
                    self.rt.emit(sid, "approval_decided", {"action_id": a["id"], "allow": True, "always": False})
                    a["state"] = "approved"
            return all(a is not None and a["state"] != "awaiting" for a in acts)
        if not self._wait("waiting_approval", decided):
            return None
        s = store.session(sid)
        decisions = []
        for c in gated:
            a = action_for(store, sid, c["id"])
            if a["state"] == "approved" and a["revision"] == s["revision"]:
                decisions.append({"type": "approve"})
                continue
            text = DENIED_TEXT if a["state"] == "denied" else VOID_TEXT
            if a["state"] == "denied":
                store.audit(sid, a, "用户拒绝", "未执行")
            elif a["state"] not in TERMINAL:
                store.set_action(a["id"], "voided")
                store.audit(sid, a, "作废", "批准已失效")
            if k.result_of(c["id"]) is None:
                info = k.call_info(c["id"])
                k.write_result(c["id"], info["entry"].get("name") if info else c.get("name"), False, text, a["id"])
            decisions.append({"type": "reject", "message": text})
        return self.invoke(resume_command(intr, {"decisions": decisions}))

    def on_question(self, intr, v, msgs):
        k, store, sid = self.kernel, self.store, self.sid
        cid = v.get("call_id")
        if k.result_of(cid) is not None:
            return self.invoke({"messages": k.reconcile(msgs)})
        box = {}

        def answered():
            for e in reversed(store.events(sid, types=("answer",))):
                if e["payload"].get("call_id") == cid:
                    box["text"] = e["payload"].get("text") or ""
                    return True
            return False
        if not self._wait("waiting_input", answered):
            return None
        return self.invoke(resume_command(intr, {"kind": "answer", "call_id": cid, "text": box["text"]}))
