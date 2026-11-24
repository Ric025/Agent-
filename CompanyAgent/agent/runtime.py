"""运行时：管任务线程、事件推送、批准、恢复、定时器和连接器。"""
from __future__ import annotations
import json, os, queue, threading, time, traceback
from .store import Store
from .skills import Skills
from .scheduler import Scheduler
from . import tools as toolreg
from .loop import Runner, Counters, Shutdown, Interrupt, cancel_pending, open_question, write_result


class Live(Counters):
    """一个任务在内存里的运行状态。标志位和计数只在本进程有效，重启后从账本重新开始。"""

    def __init__(self):
        super().__init__()
        self.alive = False        # 是否有线程在跑
        self.thread = None
        self.kick = False         # 线程在跑时又有新情况（新消息、继续）：结束前要再看一眼账本
        self.pause = False
        self.cancel = False
        self.llm_cancel = None    # 当前模型调用的取消事件
        self.llm = None           # 一个任务用一个模型客户端，粘在同一个端点上
        self.compacting = False   # 正在后台手动整理（docs/指令和主题.md 1.3）
        self.start_after = False  # 整理期间来了新消息或"继续"：整理完再启动
        self.toolset = None       # 正在跑的那次运行带了哪些工具组（agent/toolsets.py），/状态 用


class Busy(Exception):
    """对话正在运行或正在整理，不能做这个操作（接口回 409）。"""



def setup_smoke_failed(app_dir):
    """setup/check_env.py --smoke 记下的新内核自检结果（.tools/setup-state.json）。
    没通过、而且是用现在这个 Python 版本测的：返回一句话的原因；通过了、没测过、或者测的是别的 Python：返回 ''。"""
    import platform
    try:
        with open(os.path.join(app_dir, ".tools", "setup-state.json"), encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        return ""
    if not isinstance(st, dict) or st.get("lg_smoke") not in ("fail", "timeout", "error"):
        return ""
    if st.get("lg_smoke_python") and st.get("lg_smoke_python") != platform.python_version():
        return ""
    return str(st.get("lg_smoke_detail") or st.get("lg_smoke"))[:200]

class Runtime:
    def __init__(self, cfg, llm_factory=None, app_dir=None, review_llm_factory=None):
        self.cfg = cfg
        self.app_dir = app_dir or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.store = Store(cfg.home)
        toolreg.load_all()
        # 公司共享目录（docs/借鉴开源项目.md 第 5 节）：先用本机上次同步的缓存把下发的设置叠加上（不碰共享盘，很快），
        # 再起技能库；真正去共享目录里看在下面 shared.start() 的后台线程里做
        from .shared import SharedService
        self.shared = SharedService(self)
        # 技能三个来源：内置 · 公司（共享目录在本机的缓存）· 本机。同名时内置的优先（原来的规矩），其余后面的盖前面的（本机 > 公司）
        self.skills = Skills([(os.path.join(self.app_dir, "skills"), "builtin"),
                              (self.shared.skills_dir(), "shared"),
                              (os.path.join(cfg.home, "skills"), "user")])
        self.llm_factory_custom = llm_factory is not None     # 测试和演示模式自带假模型
        if llm_factory is None:
            from .llm import OpenAIClient
            llm_factory = lambda: OpenAIClient(cfg, self)   # noqa: E731
        self.llm_factory = llm_factory
        # 后台回顾用的模型：传了就用；调用方自带了模型却没给回顾的，回顾不运行（免得吃掉测试的假模型脚本）；
        # 两个都没传，用默认的 OpenAIClient（新内核用端点配置建 ChatOpenAI）
        self.review_custom = review_llm_factory is not None
        if review_llm_factory is None and not self.llm_factory_custom:
            from .llm import OpenAIClient
            review_llm_factory = lambda: OpenAIClient(cfg)   # noqa: E731
        self.review_llm_factory = review_llm_factory
        self._choose_kernel()
        self.mock = False
        # 一把锁管所有任务的状态切换；等待批准、等待回答都在它的 Condition 上睡
        self.cv = threading.Condition(threading.RLock())
        self.lives = {}
        self.closing = False
        self._subs = []
        self._sub_lock = threading.Lock()
        self._mcp = {}
        self._mcp_lock = threading.Lock()
        # 模型能力和功能开关（docs/操控电脑.md 第 1、7 节）；浏览器（第 2 节）用到时才启动
        from .capability import Capabilities
        self.caps = Capabilities(self)
        self.browser = None
        self.http_port = None       # 服务起来后由 server 填上：浏览器永远不许打开这个端口
        self.caps.start()
        # 资料库（docs/两种模式.md 第 2 节）：有资料库时起后台收录线程（接着做没做完的，并同步一遍）
        from .kb import KBService
        self.kb = KBService(self)
        self.kb.start()
        self.shared.start()           # 配了共享目录才起后台线程：启动时看一遍，之后每 shared.sync_minutes 分钟看一遍
        from .review import Reviewer
        self.reviewer = Reviewer(self)
        self.scheduler = Scheduler(self)
        self.scheduler.start()

    # ---------------- 默认空间 ----------------
    DEFAULT_WS_NAME = "默认"

    def ensure_default_workspace(self):
        """没有默认空间就建一个（docs/聊天为主的界面.md 1.1），返回它。
        只由 agent/__main__.py 在启动服务前调用；测试直接构造 Runtime 时不会多出一个工作区。"""
        from . import config as config_mod
        store = self.store
        fallback = os.path.join(self.cfg.home, "files")
        ws = store.default_workspace()
        if ws is not None:
            conf = self.cfg.get("default_folder")
            want = config_mod.default_folder(self.cfg) if isinstance(conf, str) and conf.strip() else None
            # 设置里另指了默认文件夹：换过去（建不了就保持原样）
            if want and not _same_path(want, ws["path"]) and _make_dir(want):
                store.x("UPDATE workspaces SET path=? WHERE id=?", (os.path.realpath(want), ws["id"]))
                return store.workspace(ws["id"])
            _make_dir(ws["path"])          # 文件夹被删了就补建；建不了也不影响启动
            return ws
        path = config_mod.default_folder(self.cfg)
        if not _make_dir(path):
            path = fallback
            if not _make_dir(path):
                raise RuntimeError("建不了默认文件夹：%s" % path)
        return store.add_workspace(self.DEFAULT_WS_NAME, os.path.realpath(path), is_default=True)

    # ---------------- 内核 ----------------
    def _choose_kernel(self):
        """按配置 kernel 选执行内核：auto（默认）装了 LangChain / LangGraph 就用新内核，否则用过渡内核；
        langgraph 用不了就报错；builtin 一直用过渡内核。界面读 /api/state 的 kernel 和 kernel_notice
        （提示只在设置页和 /状态 里显示，不再往对话里发）。"""
        want = str(self.cfg.get("kernel") or "auto").strip().lower()
        if want not in ("auto", "langgraph", "builtin"):
            want = "auto"
        self.kernel_name = "builtin"
        self.kernel_notice = None
        self.kernel_why = ""
        self.lg = None
        if want == "builtin":
            return
        from . import lg
        ok, why = lg.available()
        if ok and want == "auto":
            # setup/install.sh 装完会在这台电脑上跑一遍新内核自检（setup/check_env.py --smoke）。没通过就先用过渡内核：
            # 新内核还没在真机上验证过，不能让它一出问题就整个用不了。设置里明确写 kernel=langgraph 时不管这个
            bad = setup_smoke_failed(self.app_dir)
            if bad:
                self.kernel_notice = ("新内核在这台电脑上的自检没通过（%s），先用过渡内核。详情见 setup/新内核自检.txt；"
                                      "修好以后运行 bash setup/install.sh --force 重新检查。" % bad)
                self.kernel_why = bad
                return
        if ok:
            from .lg.host import Host
            self.lg = Host(self)
            self.kernel_name = "langgraph"
            return
        if want == "langgraph":
            raise RuntimeError("设置里指定使用 LangChain / LangGraph 内核，但它现在用不了：%s。重新自动安装：bash start.sh --setup（Windows：start.bat --setup）" % why)
        self.kernel_notice = lg.NOTICE
        self.kernel_why = why

    # ---------------- 事件 ----------------
    def emit(self, sid, etype, payload, store=True, revision=None):
        """写账本并推给订阅者；store=False 只推不写（流式片段）。"""
        if store:
            ev = self.store.add_event(sid, etype, payload, revision)
            if etype in ("user_message", "assistant_message", "status"):
                self.store.x("UPDATE sessions SET updated=? WHERE id=?", (time.time(), sid))
            if etype == "assistant_message" and not (payload or {}).get("tool_calls"):
                self._push(ev)
                # docs/两种模式.md 9.3：最后那条回答写进账本之后核对数字（两个内核都经这里写回答）
                from . import grounding
                grounding.after_answer(self, ev)
                return ev
        else:
            ev = {"seq": None, "session_id": sid, "revision": None, "type": etype, "payload": payload, "ts": time.time()}
        self._push(ev)
        return ev

    def _push(self, ev):
        with self._sub_lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(ev)
            except queue.Full:
                pass
        return ev

    def subscribe(self):
        q = queue.Queue(maxsize=20000)
        with self._sub_lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q):
        with self._sub_lock:
            if q in self._subs:
                self._subs.remove(q)

    def set_status(self, sid, status, reason=None):
        with self.cv:
            s = self.store.session(sid)
            if not s:
                return
            meta = s["meta"]
            changed = s["status"] != status or meta.get("reason") != reason
            if reason:
                meta["reason"] = reason
            else:
                meta.pop("reason", None)
            self.store.set_session(sid, status=status, meta=meta)
            if changed:
                p = {"status": status}
                if reason:
                    p["reason"] = reason
                self.emit(sid, "status", p)

    # ---------------- 线程管理 ----------------
    def _live(self, sid):
        with self.cv:
            lv = self.lives.get(sid)
            if lv is None:
                lv = self.lives[sid] = Live()
            return lv

    def _start(self, sid):
        """启动或"踢醒"任务线程。清掉暂停、取消标志，预算重新计算。"""
        with self.cv:
            if self.closing:
                return
            lv = self._live(sid)
            lv.pause = lv.cancel = False
            lv.reset()
            if lv.compacting:
                # 正在整理：整理完再启动（_compact_run 结束时调 _start）
                lv.start_after = True
                self.set_status(sid, "running")
                return
            if lv.alive:
                lv.kick = True
                self.cv.notify_all()
                return
            lv.alive, lv.kick = True, False
            self.set_status(sid, "running")
            t = threading.Thread(target=self._run, args=(sid,), name="task-" + sid, daemon=True)
            lv.thread = t
            t.start()

    def _run(self, sid):
        lv = self._live(sid)
        try:
            if self.lg is not None:
                self.lg.run_task(sid, lv)
                return
            if lv.llm is None:
                lv.llm = self.llm_factory()
            Runner(self, sid, lv.llm, counters=lv).run()
        except Shutdown:
            with self.cv:
                lv.alive = False
                self.cv.notify_all()
        except Exception as e:
            traceback.print_exc()
            with self.cv:
                lv.alive = False
                self.cv.notify_all()
            if not self.closing:
                try:
                    self.emit(sid, "error", {"text": "运行时内部错误：%s: %s" % (type(e).__name__, e)})
                    self.set_status(sid, "failed")
                except Exception:
                    pass

    def _end(self, sid, status, reason=None):
        """线程准备结束。如果这期间被踢过，就不结束，再转一圈。"""
        with self.cv:
            lv = self._live(sid)
            if lv.kick and not self.closing:
                lv.kick = False
                return False
            self.set_status(sid, status, reason)
            lv.alive = False
            lv.llm_cancel = None
            self.cv.notify_all()
        if status == "done" and not self.closing:
            try:
                self._suggest_skill(sid)
            except Exception:
                traceback.print_exc()
            self.reviewer.request(sid, "end")     # 攒够了几句发言就排一次后台回顾
        return True

    SKILL_SUGGESTION = "这次的做法可以存成技能，下次同类的事直接照做。"

    def _suggest_skill(self, sid):
        """一次运行以 done 结束时，做的步骤够多就提示"可以存成技能"（docs/记忆设计.md 9.2）。
        两个内核都经 _end 结束，所以都走这里。这条事件不进模型上下文。"""
        try:
            n = int(self.cfg.get("skills", "suggest_after", default=8) or 0)
        except (TypeError, ValueError):
            n = 8
        if n <= 0:
            return False
        s = self.store.session(sid)
        if not s or s["kind"] != "task":
            return False
        events = self.store.events(sid, types=("user_message", "tool_result", "suggestion"))
        ok = 0
        for e in events:
            p = e["payload"] or {}
            if e["type"] == "suggestion" and p.get("kind") == "save_skill":
                return False                      # 这个任务里已经提示过
            if e["type"] == "tool_result" and p.get("ok") and p.get("tool") == "skill_save":
                return False                      # 已经存过技能
            if e["type"] == "user_message":
                ok = 0
            elif e["type"] == "tool_result" and p.get("ok"):
                ok += 1
        if ok < n:
            return False
        self.emit(sid, "suggestion", {"kind": "save_skill", "text": self.SKILL_SUGGESTION})
        return True

    # ---------------- 对外接口 ----------------
    def send_message(self, sid, text, images=None, files=None, skill=None, depth=None):
        """files：用户附上的文件（工作区里的相对路径，调用方已核实存在）。
        skill：用户点名要用的技能名（调用方已核实存在），存进 user_message 的 payload。"""
        files = [f for f in (files or []) if isinstance(f, str) and f]
        skill = skill if isinstance(skill, str) and skill else None
        with self.cv:
            s = self.store.session(sid)
            if not s:
                raise KeyError("没有这个任务")
            lv = self._live(sid)
            events = self.store.events(sid)
            q = open_question(events)
            if q is not None:
                # 正在等回答：这句话就是答案（附了文件就把文件说明接在答案后面，模型才知道）
                if files:
                    from .context import files_note
                    text = (text.rstrip() + "\n" if text.strip() else "") + files_note(files)
                if skill:
                    from .context import skill_note
                    text = (text.rstrip() + "\n" if text.strip() else "") + skill_note(skill)
                self.emit(sid, "answer", {"call_id": q["payload"]["call_id"], "text": text})
                if lv.alive:
                    lv.kick = True
                    self.cv.notify_all()
                else:
                    self._start(sid)
                return
            payload = {"text": text}
            if images:
                payload["images"] = list(images)
            if files:
                payload["files"] = list(files)
            if skill:
                payload["skill"] = skill
            if depth in ("normal", "deep"):
                payload["depth"] = depth          # docs/两种模式.md 9.4：这一轮只原样存下，行为由后续实现
            if lv.alive and s["status"] in ("running", "waiting_approval"):
                # 中途改要求：修订号加一，旧修订下没执行的动作和批准全部作废
                self.store.bump_revision(sid)
                for a in self.store.actions(sid, states=("awaiting", "approved", "proposed")):
                    self.store.set_action(a["id"], "voided")
                    self.store.audit(sid, a, "作废", "用户更改了要求")
                self.emit(sid, "user_message", payload)
                lv.reset()
                # 线程可能已经决定"完成"、正等着拿锁落状态：踢一下，让它结束前再看一眼账本
                lv.kick = True
                if lv.llm_cancel is not None:
                    lv.llm_cancel.set()
                self.cv.notify_all()
                return
            if not s["title"]:
                title = (text.strip().replace("\n", " ")[:20] or (os.path.basename(files[0])[:20] if files else "")
                         or (skill or "")[:20])
                self.store.set_session(sid, title=title)
            self.emit(sid, "user_message", payload)
            self._start(sid)

    def control(self, sid, op):
        with self.cv:
            s = self.store.session(sid)
            if not s:
                raise KeyError("没有这个任务")
            lv = self._live(sid)
            if op in ("pause", "cancel") and lv.start_after and not lv.alive:
                lv.start_after = False      # 整理期间排着的启动不做了
            if op == "pause":
                if lv.alive:
                    lv.pause = True
                    if lv.llm_cancel is not None:
                        lv.llm_cancel.set()
                    self.cv.notify_all()
                elif s["status"] in ("running", "waiting_approval", "waiting_input"):
                    self.set_status(sid, "paused", "用户暂停")
            elif op == "cancel":
                if lv.alive:
                    lv.cancel = True
                    if lv.llm_cancel is not None:
                        lv.llm_cancel.set()
                    self.cv.notify_all()
                else:
                    cancel_pending(self, sid)
                    self.set_status(sid, "cancelled")
            elif op == "resume":
                self._start(sid)
            else:
                raise ValueError("不认识的操作：%s" % op)

    # ---------------- 手动整理（/整理） ----------------
    def compact(self, sid, focus=None):
        """用户手动整理上下文（docs/指令和主题.md 1.3）。对话在运行或正在整理时抛 Busy。
        没有可压的内容：{"ok": True, "compacted": False}（立即返回）。
        新内核做不到让检查点失效：{"ok": False, "error": …}。
        否则在后台线程里压缩，立即返回 {"ok": True, "started": True}；
        结果用 compact 事件告诉界面：{"state": "done", "compacted": true/false} 或 {"state": "failed", "error": …}。"""
        from . import context
        focus = (focus or "").strip() or None
        with self.cv:
            s = self.store.session(sid)
            if not s:
                raise KeyError("没有这个任务")
            lv = self._live(sid)
            if lv.compacting:
                raise Busy("正在整理，请稍等")
            if lv.alive:
                raise Busy("对话正在运行，等它停下再整理")
            if not context.compressible(self.store.events(sid)):
                return {"ok": True, "compacted": False}
            if self.lg is not None:
                why = self.lg.cannot_reset()
                if why:
                    return {"ok": False, "error": why}
            if self.closing:
                return {"ok": False, "error": "程序正在关闭"}
            lv.compacting, lv.start_after = True, False
            p = {"state": "started"}
            if focus:
                p["focus"] = focus
            self.emit(sid, "compact", p, store=False)
            t = threading.Thread(target=self._compact_run, args=(sid, lv, focus), name="compact-" + sid, daemon=True)
            t.start()
        return {"ok": True, "started": True}

    def _compact_llm(self, lv):
        if self.lg is not None:
            return self.lg.compact_llm()
        if lv.llm is None:
            lv.llm = self.llm_factory()
        return lv.llm

    def _compact_run(self, sid, lv, focus):
        from . import context
        from .llm import Cancelled
        ev = threading.Event()
        result = {"state": "done", "compacted": False}
        try:
            with self.cv:
                lv.llm_cancel = ev
            llm = self._compact_llm(lv)
            budget = max(4000, self.context_usage(sid, tools=False)["limit"])
            text = context.compress(self, sid, llm, budget, cancel=ev, focus=focus, force=True)
            if text is not None:
                result["compacted"] = True
                if self.lg is not None:
                    # 新内核：检查点作废，下次运行从账本（摘要 + 之后的发言）重新装载
                    self.lg.reset_thread(sid)
        except Cancelled:
            result = {"state": "failed", "error": "整理被中断了"}
        except Exception as e:
            traceback.print_exc()
            result = {"state": "failed", "error": "整理失败：%s" % e}
        with self.cv:
            if lv.llm_cancel is ev:
                lv.llm_cancel = None
            lv.compacting = False
            again = lv.start_after and not self.closing
            lv.start_after = False
            self.cv.notify_all()
        if not self.closing:
            try:
                self.emit(sid, "compact", result)
            except Exception:
                pass
        if again:
            self._start(sid)

    # ---------------- 状态（/状态） ----------------
    def _status_tools(self, ws, sid):
        """估算用的工具清单（工具定义）：按需加载时是这次会带上的那几组（docs/借鉴开源项目.md 3.9）。"""
        return self.status_toolset(sid, ws).specs()

    def _status_tool_objs(self, ws, sid):
        """内置工具按模式和开关过滤；连接器只算已经启动的（不为了估算去启动连接器）。"""
        from types import SimpleNamespace
        from . import modes
        mode = modes.of_session(self.store, sid)
        ctx = SimpleNamespace(rt=self, store=self.store, cfg=self.cfg, sid=sid, ws=ws, root=ws.get("path"),
                              is_sub=False, runner=None, mode=mode)
        out = []
        for t in toolreg.REGISTRY.values():
            if not modes.tool_allowed(mode, t.name):
                continue
            try:
                if t.enabled and not t.enabled(ctx):
                    continue
            except Exception:
                continue
            if self.store.tool_setting(t.name) == "deny":
                continue
            out.append(t)
        if modes.mcp_allowed(mode):
            out += self._started_mcp_tools()
        return out, mode

    def _started_mcp_tools(self):
        """已经启动的连接器的工具（不去启动新的）。"""
        out = []
        try:
            if self.lg is not None:
                from .lg import tools as lgtools
                taken = set()
                with self.lg.lock:
                    ents = [(n, e) for n, e in self.lg.mcp.items() if e.get("tools")]
                for name, ent in ents:
                    out += lgtools.wrap_mcp(name, ent["tools"], taken)
            else:
                from .mcp import wrap_tools
                with self._mcp_lock:
                    ents = [(n, e.get("client")) for n, e in self._mcp.items()]
                for name, c in ents:
                    if c is not None and c.alive():
                        out += wrap_tools(name, c, self)
        except Exception:
            traceback.print_exc()
        return out

    def status_toolset(self, sid, ws=None):
        """这个对话现在带了哪些工具组：正在跑就用那次运行的；否则按"下一次运行会带哪些"现算
        （模式的默认组 ∪ 用过的组 ∪ 这一轮发言里的信号）。"""
        from . import toolsets
        lv = self.lives.get(sid)
        if lv is not None and lv.alive and getattr(lv, "toolset", None) is not None and lv.toolset.sid == sid:
            return lv.toolset
        if ws is None:
            s = self.store.session(sid)
            ws = self.store.workspace(s["workspace_id"]) if s else None
        tools, mode = self._status_tool_objs(ws or {}, sid)
        return toolsets.Toolset(self, sid, mode, tools)

    def context_usage(self, sid, tools=True):
        """估算下一次请求大概会发多少 token：系统提示 + 工具定义 + 账本投影出的对话（从最后一条摘要开始）。
        limit 是输入预算（上下文窗口 - 最大输出 - 1024），超过它就会自动压缩。"""
        from . import context
        limit = int(context.input_budget(self.cfg))
        s = self.store.session(sid)
        ws = self.store.workspace(s["workspace_id"]) if s else None
        used = 0
        if ws:
            from . import modes
            system = context.system_prompt(self, ws, mode=modes.norm((s or {}).get("mode")))
            used += context.estimate([{"role": "system", "content": system}])
            if tools:
                used += context.estimate_tools(self._status_tools(ws, sid))
                if self.lg is not None:
                    used += 400       # 新内核的 write_todos（和 lg/kernel.py 的 fixed_tokens 一致）
        used += context.estimate(context.project(self.store.events(sid)))
        return {"used": int(used), "limit": limit}

    def session_status(self, sid):
        """GET /api/sessions/<id>/status 的内容（docs/指令和主题.md 1.3）。"""
        from . import memory
        s = self.store.session(sid)
        if not s:
            raise KeyError("没有这个任务")
        eps = [e for e in (self.cfg.get("endpoints") or []) if isinstance(e, dict)]
        model = "mock" if self.mock else ((eps[0].get("model") or "") if eps else "")
        lv = self.lives.get(sid)
        idx = getattr(lv.llm, "idx", None) if lv is not None and lv.llm is not None and not self.mock else None
        if isinstance(idx, int) and 0 <= idx < len(eps):
            model = eps[idx].get("model") or model     # 过渡内核的客户端换过台：报它现在用的那个端点
        endpoint = ""
        last = self.store.one("SELECT payload FROM events WHERE session_id=? AND type='assistant_message' "
                              "ORDER BY seq DESC LIMIT 1", (sid,))
        if last:
            try:
                endpoint = (json.loads(last["payload"]) if isinstance(last["payload"], str) else last["payload"]).get("endpoint") or ""
            except Exception:
                endpoint = ""
        if self.mock:
            endpoint = endpoint or "mock"
        else:
            hit = next((e for e in eps if endpoint and (e.get("name") == endpoint or e.get("base_url") == endpoint
                                                       or e.get("model") == endpoint)), None)
            if hit is not None:
                model = hit.get("model") or model
                endpoint = hit.get("name") or endpoint
            elif not endpoint and eps:
                endpoint = eps[0].get("name") or ""
        steps = self.store.one("SELECT COUNT(*) AS n FROM events WHERE session_id=? AND type='tool_result'", (sid,))
        mem = {}
        for target, wid in (("user", None), ("workspace", s["workspace_id"])):
            u = memory.usage(self.store, self.cfg, wid, target)
            mem[target] = {"used": u["used"], "limit": u["budget"]}
        from . import browser as bmod
        ws = self.store.workspace(s["workspace_id"])
        ts = self.status_toolset(sid, ws).status() if ws else None
        return {"model": model, "endpoint": endpoint, "kernel": self.kernel_name, "mode": s.get("mode") or "cariai",
                "context": self.context_usage(sid),
                "toolset": ts,
                "browser": bmod.get(self).status(sid) if self.browser is not None else {"open": False},
                "steps": int((steps or {}).get("n") or 0), "memory": mem,
                "approval_mode": self.cfg.get("approval_mode"),
                "compacting": bool(lv is not None and lv.compacting)}

    def decide(self, action_id, allow, always=False):
        with self.cv:
            a = self.store.action(action_id)
            if not a:
                raise KeyError("没有这个操作")
            if a["state"] != "awaiting":
                raise ValueError("这个操作已经处理过了，或者已经失效")
            self.emit(a["session_id"], "approval_decided", {"action_id": action_id, "allow": bool(allow), "always": bool(always)})
            self.store.set_action(action_id, "approved" if allow else "denied")
            if allow and always and a["risk"] != "delete":
                if a["tool"].startswith("browser_"):
                    # 浏览器的"以后都允许"是允许这个网站（加进 browser.allowed_domains），不是让工具总是放行
                    self._allow_site(a)
                else:
                    self.store.set_tool_setting(a["tool"], "allow")
            # 任务暂停时只记录决定，用户点"继续"才接着跑
            self.cv.notify_all()

    def _allow_site(self, a):
        from . import browser as bmod
        host = ""
        for e in reversed(self.store.events(a["session_id"], types=("approval_requested",))):
            if e["payload"].get("action_id") == a["id"]:
                host = ((e["payload"].get("display") or {}).get("host") or "")
                break
        if not host and a["tool"] == "browser_open":
            host = bmod.host_of(bmod.check_url((a["args"] or {}).get("url"), self.cfg)[0])
        host = bmod.norm_host(host)
        if not host:
            return
        cur = [str(x) for x in (self.cfg.get("browser", "allowed_domains") or [])]
        if not bmod.domain_match(host, cur):
            self.cfg.update({"browser": {"allowed_domains": cur + [host]}})

    def run_subagent(self, ctx, task):
        """派一个只读子代理，在当前线程里同步跑同一个循环，返回它的结论。"""
        if self.lg is not None:
            return self.lg.run_subagent(ctx, task)
        parent = ctx.sid
        sub = self.store.add_session(ctx.ws["id"], "子任务：" + task.strip()[:20], kind="sub", parent_id=parent)
        self.emit(sub["id"], "user_message", {"text": task})
        self.set_status(sub["id"], "running")
        llm = getattr(ctx.runner, "llm", None) or self.llm_factory()
        try:
            status = Runner(self, sub["id"], llm, sub=True, ctl=getattr(ctx.runner, "ctl", parent)).run()
        except Interrupt as e:
            self.set_status(sub["id"], "cancelled" if e.kind != "pause" else "paused",
                            {"pause": "用户暂停", "cancel": None, "stale": "主任务的要求已变更"}.get(e.kind))
            raise
        last = None
        for e in reversed(self.store.events(sub["id"], types=("assistant_message",))):
            last = e
            break
        content = (last["payload"].get("content") or "").strip() if last else ""
        if status == "done" and content:
            return content
        reason = (self.store.session(sub["id"])["meta"] or {}).get("reason") or status
        return "子代理没有得出结论（%s）。%s" % (reason, ("它最后说：" + content) if content else "")

    def delete_session(self, sid):
        """删除任务记录（运行中的先取消）。不删用户文件。"""
        with self.cv:
            lv = self.lives.get(sid)
            if lv and lv.alive:
                self.control(sid, "cancel")
        self.wait_idle(sid, 10)
        ids = [sid] + [r["id"] for r in self.store.q("SELECT id FROM sessions WHERE parent_id=?", (sid,))]
        for x in ids:
            self.store.delete_fts(x)
            self.store.x("DELETE FROM events WHERE session_id=?", (x,))
            self.store.x("DELETE FROM actions WHERE session_id=?", (x,))
            self.store.x("DELETE FROM sessions WHERE id=?", (x,))
        if self.lg is not None:
            self.lg.forget(ids)
        with self.cv:
            self.lives.pop(sid, None)

    def recover(self):
        """启动时调用：上次没跑完的任务一律暂停，等用户点继续（两步续跑）。
        执行到一半的动作不在这里处理，留给循环判为"结果未知"。"""
        rows = self.store.q("SELECT id, kind FROM sessions WHERE status IN ('running','waiting_approval','waiting_input')")
        for r in rows:
            self.set_status(r["id"], "paused", "程序重启")
            if r["kind"] == "task":
                self.emit(r["id"], "notice", {"text": "程序重新启动过，这个任务已暂停。已完成的步骤都在下面，点继续接着做。"})
        return [r["id"] for r in rows]

    def wait_idle(self, sid, timeout=10):
        with self.cv:
            return self.cv.wait_for(lambda: not self._live(sid).alive and not self._live(sid).compacting, timeout)

    def shutdown(self):
        """关闭：通知所有线程退出，但不改账本里的任务状态（下次启动由 recover 处理）。"""
        with self.cv:
            self.closing = True
            for lv in self.lives.values():
                if lv.llm_cancel is not None:
                    lv.llm_cancel.set()
            self.cv.notify_all()
            threads = [lv.thread for lv in self.lives.values() if lv.thread is not None]
        self.scheduler.stop.set()
        self.reviewer.stop()
        self.caps.stop()
        sh = getattr(self, "shared", None)
        if sh is not None:
            sh.stop()
        kb = getattr(self, "kb", None)
        if kb is not None:
            kb.close()
        self.close_browser()
        for t in threads:
            if t is not threading.current_thread():
                t.join(5)
        with self._mcp_lock:
            for ent in self._mcp.values():
                if ent.get("client"):
                    ent["client"].close()
            self._mcp.clear()
        if self.lg is not None:
            self.lg.close()
        with self._sub_lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(None)      # 让事件流连接结束
            except queue.Full:
                pass
        self.store.close()

    def close_browser(self):
        """关掉浏览器和它的专用线程（设置页的"关闭浏览器"、程序退出）。"""
        b = self.browser
        if b is not None:
            b.close()

    # ---------------- 连接器 ----------------
    def mcp_tools(self, ctx):
        servers = self.cfg.get("mcp_servers") or {}
        if not servers:
            return []
        if self.lg is not None:
            return self.lg.mcp_tools(ctx)
        from .mcp import wrap_tools
        out = []
        for name, conf in servers.items():
            client = self._mcp_client(name, conf, ctx)
            if client is not None:
                out += wrap_tools(name, client, self)
        return out

    def _mcp_client(self, name, conf, ctx=None):
        from .mcp import MCPClient
        key = json.dumps(conf, sort_keys=True, ensure_ascii=False)
        with self._mcp_lock:
            ent = self._mcp.get(name)
            if ent and ent["key"] == key:
                c = ent.get("client")
                if c is not None and c.alive():
                    return c
                if c is None and time.time() - ent["failed_at"] < 60:
                    return None        # 刚失败过，一分钟内不重试，免得每个任务都卡在启动上
            if ent and ent.get("client"):
                ent["client"].close()
            try:
                c = MCPClient(name, conf.get("command") or [], conf.get("env") or {}, cwd=conf.get("cwd"))
                c.start()
                self._mcp[name] = {"key": key, "client": c, "failed_at": 0}
                return c
            except Exception as e:
                self._mcp[name] = {"key": key, "client": None, "failed_at": time.time()}
                if ctx is not None:
                    self.emit(ctx.sid, "notice", {"text": "连接器「%s」启动失败，已跳过：%s" % (name, e)})
                return None

    def reset_mcp(self):
        """设置里改了连接器配置后调用：关掉旧进程，下个任务按新配置启动。"""
        if self.lg is not None:
            self.lg.reset_mcp()
        with self._mcp_lock:
            for ent in self._mcp.values():
                if ent.get("client"):
                    ent["client"].close()
            self._mcp.clear()


def _make_dir(path):
    """不存在就创建。成功（或本来就是目录）返回 True。"""
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        return False
    return os.path.isdir(path)


def _same_path(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))
