"""本机 HTTP 接口 + 事件流（SSE）+ 静态界面文件。只监听 127.0.0.1，所有 /api 请求要带令牌。"""
from __future__ import annotations
import copy, hmac, json, mimetypes, os, queue, re, secrets, shutil, threading, time, unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs, quote
from . import __version__, policy
from .store import new_id, now
from .tools import REGISTRY, ToolError
from .tools import fs as fstools
from .tools import kb as kbtools
from .tools import history as historytools
from .scheduler import next_run
from . import voice, memory, export, picker, modes, feedback
from . import kb as kbm
from . import config as config_mod

STATIC = {"/": ("index.html", "text/html; charset=utf-8"), "/index.html": ("index.html", "text/html; charset=utf-8"),
          "/app.css": ("app.css", "text/css; charset=utf-8"), "/app.js": ("app.js", "application/javascript; charset=utf-8"),
          "/theme.js": ("theme.js", "application/javascript; charset=utf-8"),
          "/themes.css": ("themes.css", "text/css; charset=utf-8")}
INLINE_IMAGES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
MAX_BODY = 60 * 1024 * 1024       # 图片以 data URL 传，留足空间
PREVIEW_CHARS = 20000
MAX_FILES = 20                    # 一条发言最多附几个文件
SETTINGS_KEYS = ("display_name", "user_location", "endpoints", "model", "approval_mode", "budgets", "web", "mcp_servers", "asr", "memory", "ui",
                 "browser", "embedding", "rerank", "kb")
DOMAIN_RE = re.compile(r"^[a-z0-9.-]{1,253}\Z")
CAPS_KEYS = ("tools", "vision", "pointing")
THEME_RE = re.compile(r"^[a-z0-9-]{1,32}\Z")      # \Z：不放过末尾的换行
TITLE_MAX = 60
ASR_STR_KEYS = ("base_url", "model", "api_key", "sherpa_model_dir")
MAX_AUDIO_BODY = voice.MAX_BYTES + 64 * 1024
MASK = "••••"


class HTTPError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code, self.msg = code, msg


def mask_key(k):
    if not k:
        return ""
    return MASK + (k[-4:] if len(k) > 8 else "")


def write_token(home):
    tok = secrets.token_urlsafe(24)
    p = os.path.join(home, "token")
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(tok)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return tok


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # Windows 上 SO_REUSEADDR 允许抢占正在使用的端口，所以关掉
    allow_reuse_address = False

    def __init__(self, rt, port=0, token=None):
        self.rt = rt
        self.token = token or write_token(rt.cfg.home)
        self.stopping = threading.Event()
        self.pick_lock = threading.Lock()     # 选文件夹的窗口同一时间只弹一个
        super().__init__(("127.0.0.1", port), Handler)
        self.port = self.server_address[1]
        rt.http_port = self.port        # 浏览器永远不许打开这个程序自己的端口（docs/操控电脑.md 2.4）
        self.allowed_hosts = {"127.0.0.1:%d" % self.port, "localhost:%d" % self.port}
        self.allowed_origins = {"http://" + h for h in self.allowed_hosts}

    def url(self):
        return "http://127.0.0.1:%d/?t=%s" % (self.port, self.token)

    def start(self):
        t = threading.Thread(target=self.serve_forever, daemon=True, name="http")
        t.start()
        return t

    def stop(self):
        self.stopping.set()
        self.shutdown()
        self.server_close()


def make_server(rt, port=8765, tries=50, token=None):
    """端口被占用时往后找。"""
    last = None
    for p in range(port, port + tries) if port else [0]:
        try:
            return Server(rt, p, token)
        except OSError as e:
            last = e
    raise OSError("找不到可用端口（从 %d 开始试了 %d 个）：%s" % (port, tries, last))


ROUTES = []


def route(method, pattern):
    rx = re.compile("^" + pattern + "$")

    def deco(fn):
        ROUTES.append((method, rx, fn))
        return fn
    return deco


def replace_route(method, pattern):
    """换掉一个已经登记的接口（同样的方法和路径）。给别的模块用：要改本文件里已有接口的行为时，
    在自己的模块里重新实现一遍并用它登记，不要回来改本文件（见 docs/借鉴开源项目.md 0.3）。"""
    rx = re.compile("^" + pattern + "$")

    def deco(fn):
        hit = [i for i, (m, r, _) in enumerate(ROUTES) if m == method and r.pattern == rx.pattern]
        if not hit:
            raise RuntimeError("replace_route：没有这个接口 %s %s" % (method, pattern))
        ROUTES[hit[0]] = (method, rx, fn)
        return fn
    return deco


# 别的模块往设置里加自己的键（docs/借鉴开源项目.md 0.3）：
#   SETTINGS_EXT["键"] = fn(h, 值) → 校验后要保存的值（不对就抛 HTTPError(400, …)）
#   KB_KEYS_EXT["键"]  = fn(值)    → 同上，用于 kb 这个对象下面新加的键
#   SETTINGS_AFTER 里的函数在保存之后被调用：fn(h, 这次改了的内容)
SETTINGS_EXT = {}
KB_KEYS_EXT = {}
SETTINGS_AFTER = []


class Handler(BaseHTTPRequestHandler):
    server_version = "CompanyAgent"
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *args):   # 安静：不往终端刷访问日志
        pass

    # ---- 基础 ----
    @property
    def rt(self):
        return self.server.rt

    def _headers_common(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._headers_common()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_bytes(self, data, ctype, code=200, disposition=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if disposition:
            self.send_header("Content-Disposition", disposition)
        self._headers_common()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise HTTPError(413, "请求太大")
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            v = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise HTTPError(400, "请求内容不是合法的 JSON")
        if not isinstance(v, dict):
            raise HTTPError(400, "请求内容应该是 JSON 对象")
        return v

    def _guard(self, path, qs):
        """Host、Origin、令牌检查。不满足一律 403。"""
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in self.server.allowed_hosts:
            raise HTTPError(403, "拒绝访问：地址不对")
        origin = self.headers.get("Origin")
        if origin is not None and origin.strip().lower() not in self.server.allowed_origins:
            raise HTTPError(403, "拒绝访问：来源不对")
        if path.startswith("/api/"):
            tok = self.headers.get("X-Token") or (qs.get("t") or [""])[0]
            if not tok or not hmac.compare_digest(tok.encode(), self.server.token.encode()):
                raise HTTPError(403, "拒绝访问：缺少或错误的令牌")

    def _dispatch(self):
        u = urlsplit(self.path)
        path, qs = u.path, parse_qs(u.query)
        try:
            self._guard(path, qs)
            if not path.startswith("/api/"):
                return self._static(path)
            for method, rx, fn in ROUTES:
                m = rx.match(path)
                if m and method == self.command:
                    return fn(self, qs, *m.groups())
            if any(rx.match(path) for _, rx, _ in ROUTES):
                raise HTTPError(405, "不支持这个请求方法")
            raise HTTPError(404, "没有这个接口")
        except HTTPError as e:
            self._safe_error(e.code, e.msg)
        except memory.MemoryError as e:
            self._safe_error(409 if e.code == "conflict" else 400, str(e), e.code)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except KeyError as e:
            self._safe_error(404, str(e.args[0]) if e.args else "找不到")
        except (ValueError, ToolError, policy.PathError) as e:
            self._safe_error(400, str(e))
        except Exception as e:
            self._safe_error(500, "服务内部错误：%s" % e)

    def _safe_error(self, code, msg, err_code=None):
        try:
            self.send_json({"error": msg, "code": err_code} if err_code else {"error": msg}, code)
        except Exception:
            pass

    do_GET = do_POST = do_PATCH = do_DELETE = do_HEAD = lambda self: self._dispatch()

    def _static(self, path):
        if self.command not in ("GET", "HEAD") or path not in STATIC:
            raise HTTPError(404, "找不到")
        name, ctype = STATIC[path]
        p = os.path.join(self.rt.app_dir, "ui", name)
        if not os.path.isfile(p):
            raise HTTPError(404, "界面文件还没有：ui/%s" % name)
        with open(p, "rb") as f:
            self.send_bytes(f.read(), ctype)

    # ---- 参数小工具 ----
    def ws_from(self, qs=None, wid=None):
        if wid is None and qs is not None:
            wid = _q1(qs, "workspace")
        if not wid or not isinstance(wid, str):
            raise HTTPError(400, "缺少工作区 id")
        ws = self.rt.store.workspace(wid)
        if not ws:
            raise HTTPError(404, "没有这个工作区")
        return ws


def _q1(qs, k, default=None):
    v = qs.get(k)
    return v[0] if v else default


def _session_or_404(rt, sid):
    s = rt.store.session(sid)
    if not s:
        raise HTTPError(404, "没有这个任务")
    return s


# ---------------- 状态与工作区 ----------------

@route("GET", "/api/state")
def api_state(h, qs):
    cfg = h.rt.cfg
    asr = voice.asr_conf(cfg)
    dws = h.rt.store.default_workspace()
    h.send_json({"name": cfg.get("display_name"), "version": __version__, "approval_mode": cfg.get("approval_mode"),
                 "mock": bool(getattr(h.rt, "mock", False)), "web_enabled": bool(cfg.get("web", "enabled")),
                 "endpoints": [{"name": e.get("name"), "base_url": e.get("base_url"), "model": e.get("model")}
                               for e in cfg.get("endpoints") or []],
                 "workspaces": h.rt.store.workspaces(), "last_seq": h.rt.store.last_seq(),
                 "default_workspace_id": (dws or {}).get("id"), "upload_max_mb": _mb_num(config_mod.upload_max_bytes(cfg)),
                 "asr_engine": asr["engine"], "asr_max_seconds": asr["max_seconds"],
                 "kernel": getattr(h.rt, "kernel_name", "builtin"),
                 "kernel_notice": getattr(h.rt, "kernel_notice", None) or None,
                 "features": _caps(h.rt).flags(), "modes": modes.public()})


def _caps(rt):
    from . import capability
    return capability.get(rt)


@route("GET", "/api/capabilities")
def api_capabilities(h, qs):
    """模型能力和各功能的状态（docs/操控电脑.md 1.4、7.5）。"""
    h.send_json(_caps(h.rt).report())


@route("POST", "/api/capabilities/probe")
def api_capabilities_probe(h, qs):
    """立刻重探一遍（同步，最多约 20 秒；端点并行探）。"""
    h.send_json(_caps(h.rt).probe_all(force=True, timeout=25))


@route("GET", "/api/location")
def api_location(h, qs):
    """用户在哪：设置里填的，或者按电脑时区推断的（agent/whereami.py）。设置页用。"""
    from . import whereami
    w = whereami.where(h.rt.cfg)
    w["now"] = whereami.now_text()
    h.send_json(w)


@route("POST", "/api/browser/close")
def api_browser_close(h, qs):
    h.rt.close_browser()
    h.send_json({"ok": True})


@route("POST", "/api/workspaces")
def api_ws_add(h, qs):
    b = h.body()
    path = os.path.expanduser(str(b.get("path") or "").strip())
    if not path or not os.path.isdir(path):
        raise HTTPError(400, "文件夹不存在：%s" % (b.get("path") or "（空）"))
    real = os.path.realpath(path)
    # 已经连过同一个文件夹（包括默认空间）：直接返回那个，不重复建
    key = os.path.normcase(real)
    for w in h.rt.store.workspaces():
        if os.path.normcase(os.path.realpath(w["path"])) == key:
            return h.send_json(dict(w, existing=True))
    name = str(b.get("name") or "").strip() or os.path.basename(os.path.abspath(path)) or path
    h.send_json(h.rt.store.add_workspace(name, real))


@route("PATCH", "/api/workspaces/([\\w-]+)")
def api_ws_patch(h, qs, wid):
    ws = h.ws_from(wid=wid)
    b = h.body()
    fields = {}
    if "name" in b:
        if not str(b["name"]).strip():
            raise HTTPError(400, "名称不能为空")
        fields["name"] = str(b["name"]).strip()
    if "instructions" in b:
        fields["instructions"] = str(b["instructions"] or "")
    if "egress" in b:
        if b["egress"] not in ("intranet", "open"):
            raise HTTPError(400, "出站策略只能是 intranet 或 open")
        fields["egress"] = b["egress"]
    if fields:
        keys = ",".join("%s=?" % k for k in fields)
        h.rt.store.x("UPDATE workspaces SET %s WHERE id=?" % keys, tuple(fields.values()) + (ws["id"],))
    h.send_json(h.rt.store.workspace(ws["id"]))


@route("DELETE", "/api/workspaces/([\\w-]+)")
def api_ws_del(h, qs, wid):
    ws = h.ws_from(wid=wid)
    if ws.get("is_default"):
        raise HTTPError(400, "默认空间不能删除：没连文件夹时的对话都放在这里。可以改名或改指令。")
    h.rt.store.x("DELETE FROM workspaces WHERE id=?", (wid,))   # 只删记录，不动用户文件
    h.send_json({"ok": True})


# ---------------- 任务 ----------------

@route("GET", "/api/sessions")
def api_sessions(h, qs):
    wid = _q1(qs, "workspace")
    mode = _q1(qs, "mode")
    if mode and not modes.valid(mode):
        raise HTTPError(400, "mode 只能是 %s" % " 或 ".join(modes.IDS))
    h.send_json(h.rt.store.sessions(wid or None, kind="task", mode=mode or None))


def check_mode(v):
    """POST /api/sessions 的 mode：不写就是 cariai，不认识的值回 400（docs/两种模式.md 1.3）。"""
    if v is None or v == "":
        return modes.DEFAULT
    if not modes.valid(v):
        raise HTTPError(400, "不认识的模式：%s。只能是 %s" % (v, " 或 ".join(modes.IDS)))
    return v


def check_libraries(rt, v):
    """对话的检索范围：资料库 id 的列表；空或不写表示全部。"""
    if v is None:
        return None
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise HTTPError(400, "libraries 应该是资料库 id 的列表")
    out = []
    for x in v:
        if not kbm.library(rt.store, x):
            raise HTTPError(400, "没有这个资料库：%s" % x)
        if x not in out:
            out.append(x)
    return out


@route("POST", "/api/sessions")
def api_session_new(h, qs):
    b = h.body()
    ws = h.ws_from(wid=b.get("workspace_id"))
    mode = check_mode(b.get("mode"))
    libs = check_libraries(h.rt, b.get("libraries"))
    text = str(b.get("text") or "")
    images = _check_images(b.get("images"), h.rt)
    files = check_files(ws, b.get("files"))
    skill = check_skill(h.rt, b.get("skill"))
    s = h.rt.store.add_session(ws["id"], mode=mode, meta={"libraries": libs} if libs else None)
    if text.strip() or images or files or skill:
        h.rt.send_message(s["id"], text, images, files, skill, _depth(b))
    h.send_json(h.rt.store.session(s["id"]))


@route("GET", "/api/sessions/([\\w-]+)")
def api_session(h, qs, sid):
    rt = h.rt
    s = _session_or_404(rt, sid)
    events = rt.store.events(sid)
    pending = rt.store.actions(sid, states=("awaiting",))
    disp = {e["payload"].get("action_id"): e["payload"].get("display") for e in events
            if e["type"] == "approval_requested" and e["payload"].get("display")}
    for a in pending:
        if disp.get(a["id"]):
            a["display"] = disp[a["id"]]
    h.send_json({"session": s, "events": events,
                 "pending": pending,
                 "artifacts": rt.store.q("SELECT * FROM artifacts WHERE session_id=? ORDER BY created", (sid,))})


@route("DELETE", "/api/sessions/([\\w-]+)")
def api_session_del(h, qs, sid):
    _session_or_404(h.rt, sid)
    h.rt.delete_session(sid)
    h.send_json({"ok": True})


@route("GET", "/api/sessions/([\\w-]+)/export")
def api_session_export(h, qs, sid):
    """任务记录导出成 Markdown，按附件下载（做法同 /api/file?download=1）。"""
    s = _session_or_404(h.rt, sid)
    data = export.session_markdown(h.rt.store, sid).encode("utf-8")
    h.send_bytes(data, "text/markdown; charset=utf-8",
                 disposition="attachment; filename*=UTF-8''%s" % quote(export.file_name(s["title"])))


@route("POST", "/api/sessions/([\\w-]+)/message")
def api_message(h, qs, sid):
    s = _session_or_404(h.rt, sid)
    b = h.body()
    text = str(b.get("text") or "")
    images = _check_images(b.get("images"), h.rt)
    ws = h.rt.store.workspace(s["workspace_id"])
    if b.get("files") and not ws:
        raise HTTPError(400, "这个对话所在的工作区已经删除，不能附文件")
    files = check_files(ws, b.get("files")) if ws else []
    skill = check_skill(h.rt, b.get("skill"))
    if not text.strip() and not images and not files and not skill:
        raise HTTPError(400, "消息不能为空")
    h.rt.send_message(sid, text, images, files, skill, _depth(b))
    h.send_json({"ok": True})


def _depth(b):
    """9.4 的 depth：normal / deep 原样存进 user_message；别的值忽略（不拒绝）。"""
    v = b.get("depth")
    return v if v in ("normal", "deep") else None


def check_skill(rt, name):
    """发消息时点名的技能（docs/指令和主题.md 1.3）：可以不给；给了必须是存在的技能名。"""
    if name is None or name == "":
        return None
    if not isinstance(name, str):
        raise HTTPError(400, "skill 应该是技能名")
    name = name.strip()
    if not name:
        return None
    if rt.skills.get(name) is None:
        rt.skills.reload()           # 可能是刚存的技能
        if rt.skills.get(name) is None:
            raise HTTPError(400, "没有这个技能：%s" % name)
    return name


@route("PATCH", "/api/sessions/([\\w-]+)")
def api_session_patch(h, qs, sid):
    """改标题：{"title": "…"}，去掉首尾空白后 1 到 60 个字符；改检索范围：{"libraries": [id, …]}（空表示全部）。
    返回改过的对话记录。"""
    s0 = _session_or_404(h.rt, sid)
    b = h.body()
    if "libraries" in b:
        libs = check_libraries(h.rt, b["libraries"]) or []
        meta = dict(s0.get("meta") or {})
        meta["libraries"] = libs
        h.rt.store.set_session(sid, meta=meta)
        if "title" not in b:
            return h.send_json(h.rt.store.session(sid))
    if "title" not in b:
        raise HTTPError(400, "缺少 title")
    t = b["title"]
    if not isinstance(t, str):
        raise HTTPError(400, "标题应该是文字")
    t = " ".join(t.strip().splitlines()).strip()
    if not t:
        raise HTTPError(400, "标题不能为空")
    if len(t) > TITLE_MAX:
        raise HTTPError(400, "标题最多 %d 个字" % TITLE_MAX)
    h.rt.store.set_session(sid, title=t)
    h.send_json(h.rt.store.session(sid))


@route("GET", "/api/sessions/([\\w-]+)/status")
def api_session_status(h, qs, sid):
    """给 /状态 用：模型、内核、上下文用量、工具步数、记忆用量、批准方式。"""
    _session_or_404(h.rt, sid)
    h.send_json(h.rt.session_status(sid))


def _check_images(images, rt=None):
    images = images or None
    if images is not None and (not isinstance(images, list) or not all(isinstance(x, str) and x.startswith("data:image/") for x in images)):
        raise HTTPError(400, "images 应该是图片 data URL 的列表")
    if images and rt is not None:
        # 程序自己能判断的，不经过模型：看图为"否"时直接回那句原话（"未知"时放行）
        f = _caps(rt).feature("image_input")
        if not f["ok"]:
            raise HTTPError(400, f["reason"])
    return images


@route("POST", "/api/sessions/([\\w-]+)/control")
def api_control(h, qs, sid):
    _session_or_404(h.rt, sid)
    b = h.body()
    op = b.get("op")
    if op not in ("pause", "cancel", "resume", "compact"):
        raise HTTPError(400, "op 只能是 pause、cancel、resume 或 compact")
    if op == "compact":
        focus = b.get("focus")
        if focus is not None and not isinstance(focus, str):
            raise HTTPError(400, "focus 应该是文字")
        if focus and len(focus) > 500:
            raise HTTPError(400, "要保留的重点最多 500 个字")
        from .runtime import Busy
        try:
            return h.send_json(h.rt.compact(sid, focus))
        except Busy as e:
            raise HTTPError(409, str(e))
    h.rt.control(sid, op)
    h.send_json({"ok": True})


@route("POST", "/api/actions/([\\w-]+)/decide")
def api_decide(h, qs, aid):
    b = h.body()
    if not isinstance(b.get("allow"), bool):
        raise HTTPError(400, "allow 必须是 true 或 false")
    if not h.rt.store.action(aid):
        raise HTTPError(404, "没有这个操作")
    try:
        h.rt.decide(aid, b["allow"], bool(b.get("always")))
    except ValueError as e:
        raise HTTPError(409, str(e))
    h.send_json({"ok": True})


# ---------------- 事件流 ----------------

@route("GET", "/api/events")
def api_events(h, qs):
    rt = h.rt
    try:
        since = int(_q1(qs, "since") or -1)
    except ValueError:
        raise HTTPError(400, "since 应该是整数")
    q = rt.subscribe()           # 先订阅再补发，中间产生的事件不会丢
    try:
        if since < 0:
            since = rt.store.last_seq()      # 没给 since：只推之后的实时事件
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream; charset=utf-8")
        h.send_header("Cache-Control", "no-store")
        h.send_header("X-Accel-Buffering", "no")
        h.end_headers()
        last = since

        def out(ev):
            data = json.dumps(ev, ensure_ascii=False, default=str)
            line = ("id: %d\n" % ev["seq"] if ev.get("seq") else "") + "data: " + data + "\n\n"
            h.wfile.write(line.encode("utf-8"))
            h.wfile.flush()

        while True:
            rows = rt.store.events_all(last, 500)
            for ev in rows:
                out(ev)
                last = ev["seq"]
            if len(rows) < 500:
                break
        while not h.server.stopping.is_set():
            try:
                ev = q.get(timeout=15)
            except queue.Empty:
                h.wfile.write(b": ping\n\n")
                h.wfile.flush()
                continue
            if ev is None:
                break
            if ev.get("seq") is not None:
                if ev["seq"] <= last:
                    continue
                last = ev["seq"]
            out(ev)
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    finally:
        rt.unsubscribe(q)


# ---------------- 成品与文件 ----------------

@route("GET", "/api/artifacts")
def api_artifacts(h, qs):
    ws = h.ws_from(qs)
    h.send_json(h.rt.store.q("SELECT * FROM artifacts WHERE workspace_id=? ORDER BY path, version DESC", (ws["id"],)))


@route("GET", "/api/files")
def api_files(h, qs):
    ws = h.ws_from(qs)
    rel = _q1(qs, "path") or "."
    p = policy.resolve(ws["path"], rel)
    if not os.path.isdir(p):
        raise HTTPError(400, "不是目录：%s" % rel)
    items = []
    for name in sorted(os.listdir(p)):
        if name == policy.INTERNAL_DIR or name.startswith("~$"):
            continue
        fp = os.path.join(p, name)
        try:
            st = os.stat(fp)
        except OSError:
            continue
        isdir = os.path.isdir(fp)
        items.append({"name": name, "dir": isdir, "size": None if isdir else st.st_size, "modified": st.st_mtime})
    h.send_json({"path": policy.relpath(ws["path"], p), "items": items})


@route("GET", "/api/file")
def api_file(h, qs):
    ws = h.ws_from(qs)
    rel = _q1(qs, "path") or ""
    p = policy.resolve(ws["path"], rel)
    if not os.path.isfile(p):
        raise HTTPError(404, "找不到文件：%s" % rel)
    ext = os.path.splitext(p)[1].lower()
    name = os.path.basename(p)
    with open(p, "rb") as f:
        data = f.read()
    attach = "attachment; filename*=UTF-8''%s" % quote(name)
    if _q1(qs, "download") == "1":
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        return h.send_bytes(data, ctype, disposition=attach)
    if ext in fstools.TEXT_EXT:
        # 文本一律按纯文本返回（html 也不例外），免得工作区里的网页在界面的源下执行
        return h.send_bytes(fstools._decode(data).encode("utf-8"), "text/plain; charset=utf-8")
    if ext in INLINE_IMAGES:
        return h.send_bytes(data, INLINE_IMAGES[ext])
    h.send_bytes(data, mimetypes.guess_type(name)[0] or "application/octet-stream", disposition=attach)


@route("GET", "/api/preview")
def api_preview(h, qs):
    ws = h.ws_from(qs)
    rel = _q1(qs, "path") or ""
    p = policy.resolve(ws["path"], rel)
    if not os.path.isfile(p):
        raise HTTPError(404, "找不到文件：%s" % rel)
    ext = os.path.splitext(p)[1].lower()
    if ext in INLINE_IMAGES:
        return h.send_json({"kind": "image", "text": "", "truncated": False})
    try:
        segs = fstools.extract(p)
    except ToolError as e:
        return h.send_json({"kind": "unsupported", "text": str(e) + "，可以下载后查看。", "truncated": False})
    except Exception as e:
        return h.send_json({"kind": "error", "text": "预览失败：%s" % e, "truncated": False})
    text = "\n\n".join(("【%s】\n%s" % (loc, t)) if loc else t for loc, t in segs)
    kind = "text" if ext in fstools.TEXT_EXT or not ext else ext.lstrip(".")
    h.send_json({"kind": kind, "text": text[:PREVIEW_CHARS], "truncated": len(text) > PREVIEW_CHARS})


# ---------------- 上传文件、选文件夹（docs/聊天为主的界面.md 1.2、1.3） ----------------

UPLOAD_DIR = "上传"
UPLOAD_CHUNK = 64 * 1024
UPLOAD_DRAIN = 1024 * 1024        # 出错时最多读掉这么多再回话，免得客户端还在发就被断开、收不到说明
UPLOAD_DRAIN_WAIT = 1.0           # 读掉时最多等几秒没有新数据就不等了
NAME_BYTES = 200                  # 文件名最长多少字节（UTF-8），各平台的上限是 255
WIN_BAD = set('<>:"|?*')          # Windows 文件名不能用的字符（":" 在 NTFS 上还会写进备用数据流）
WIN_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {"COM%d" % i for i in range(1, 10)} | {"LPT%d" % i for i in range(1, 10)}


def clean_upload_name(name):
    """上传的文件名清洗：只留最后一段，去掉控制字符、路径分隔符和 Windows 不认的字符。
    清洗后是空的、或者只剩点，返回 None。"""
    if not isinstance(name, str):
        return None
    name = unicodedata.normalize("NFC", name)
    name = re.split(r"[\\/]", name)[-1]
    out = []
    for ch in name:
        cat = unicodedata.category(ch)
        if cat in ("Cc", "Cf", "Zl", "Zp"):       # 控制字符、看不见的格式字符（含文字方向覆盖）
            continue
        out.append("_" if ch in WIN_BAD else ch)
    name = "".join(out).strip().rstrip(". ")
    if not name.strip("."):
        return None
    stem, ext = os.path.splitext(name)
    if stem.split(".")[0].upper() in WIN_RESERVED:
        stem = "_" + stem
    # 太长就截短主名，保留扩展名
    if len(ext.encode("utf-8")) > 20:
        stem, ext = stem + ext, ""
    while stem and len((stem + ext).encode("utf-8")) > NAME_BYTES:
        stem = stem[:-1]
    name = (stem + ext).strip()
    return name if name.strip(".") else None


def _numbered(name, i):
    stem, ext = os.path.splitext(name)
    return "%s(%d)%s" % (stem, i, ext) if i > 1 else name


def _reserve_upload(root, name):
    """在工作区的 上传/ 里占一个不重名的位置：先用 O_EXCL 建一个空文件占住名字（不会覆盖已有文件），
    返回 (相对路径, 绝对路径)。每个候选路径都过路径守卫，而且必须正好落在 上传/ 里。"""
    updir = policy.resolve(root, UPLOAD_DIR, write=True)
    os.makedirs(updir, exist_ok=True)
    updir = policy.resolve(root, UPLOAD_DIR, write=True)       # 建好后再查一遍（防止中途换成符号链接）
    if not os.path.isdir(updir):
        raise HTTPError(400, "工作区里的「%s」不是文件夹" % UPLOAD_DIR)
    for i in range(1, 1000):
        cand = _numbered(name, i)
        rel = UPLOAD_DIR + "/" + cand
        full = os.path.join(updir, cand)
        if os.path.lexists(full):
            continue
        real = policy.resolve(root, rel, write=True)
        if os.path.dirname(real) != updir:
            raise policy.PathError("文件名不合法：%s" % cand)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        try:
            fd = os.open(real, flags, 0o644)
        except FileExistsError:
            continue
        os.close(fd)
        return rel, real
    raise HTTPError(409, "同名文件太多了，换个文件名再传")


def _discard(path):
    try:
        os.remove(path)
    except OSError:
        pass


@route("POST", "/api/upload")
def api_upload(h, qs):
    """请求体是文件的原始字节，存到工作区的 上传/ 里。不覆盖已有文件，重名时加 (2)、(3)。"""
    raw_len = h.headers.get("Content-Length")
    if raw_len is None:
        raise HTTPError(411, "请求里缺少文件长度（Content-Length）")
    try:
        n = int(raw_len)
    except ValueError:
        n = -1
    if n < 0:
        raise HTTPError(400, "文件长度不对")
    box = {"left": n}         # 请求体还剩多少没读
    try:
        res = _save_upload(h, qs, n, box)
    except BaseException:
        # 出错时把没读的请求体读掉一小段再回话，客户端还在发的时候被断开会收不到说明
        # 只等很短的时间：对方不再发了就不等（连接随后关闭）
        left = min(box["left"], UPLOAD_DRAIN)
        try:
            h.connection.settimeout(UPLOAD_DRAIN_WAIT)
            while left > 0:
                chunk = h.rfile.read1(min(UPLOAD_CHUNK, left)) if hasattr(h.rfile, "read1") else h.rfile.read(min(UPLOAD_CHUNK, left))
                if not chunk:
                    break
                left -= len(chunk)
        except (OSError, ValueError):
            pass
        finally:
            try:
                h.connection.settimeout(None)
            except OSError:
                pass
        raise
    h.send_json(res)


def _save_upload(h, qs, n, box):
    ws = h.ws_from(qs)
    limit = config_mod.upload_max_bytes(h.rt.cfg)
    if n > limit:
        # 按声明的长度先拒绝，一个字节都不落盘
        raise HTTPError(413, "文件太大：上限是 %s MB。" % _mb(limit))
    name = clean_upload_name(_q1(qs, "name"))
    if not name:
        raise HTTPError(400, "文件名不能为空，也不能只有点")
    root = ws["path"]
    if not os.path.isdir(root):
        raise HTTPError(400, "工作区的文件夹不存在了：%s" % root)
    rel, final = _reserve_upload(root, name)
    tmp = None
    got = 0
    try:
        tmpdir = policy.resolve(root, policy.INTERNAL_DIR + "/tmp")
        os.makedirs(tmpdir, exist_ok=True)
        tmp = os.path.join(tmpdir, "upload-%s.part" % secrets.token_hex(8))
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o644)
        with os.fdopen(fd, "wb") as f:
            while got < n:
                # 最多读到声明的长度（它已经不超过上限）；读的时候再数一遍，超了立刻停
                chunk = h.rfile.read(min(UPLOAD_CHUNK, n - got))
                if not chunk:
                    break
                got += len(chunk)
                box["left"] = n - got
                if got > limit:
                    raise HTTPError(413, "文件太大：上限是 %s MB。" % _mb(limit))
                f.write(chunk)
        if got != n:
            raise HTTPError(400, "文件没有传完整，请重试")
        os.replace(tmp, final)       # 替换的是上面占位的空文件
        tmp = None
    except BaseException:
        if tmp:
            _discard(tmp)
        _discard(final)
        raise
    h.rt.store.audit_note("upload", "create", "用户上传", "成功", "%s：%s（%d 字节）" % (ws["name"], rel, got))
    return {"path": rel, "name": os.path.basename(rel), "size": got}


def _mb_num(nbytes):
    v = nbytes / 1048576.0
    return int(v) if v == int(v) else round(v, 2)


def _mb(nbytes):
    return str(_mb_num(nbytes))


def check_files(ws, files):
    """发言附带的文件：工作区里真实存在的相对路径列表。返回清理后的相对路径（去重、统一用 /）。"""
    if files is None:
        return []
    if not isinstance(files, list) or not all(isinstance(x, str) for x in files):
        raise HTTPError(400, "files 应该是文件路径（文字）的列表")
    if len(files) > MAX_FILES:
        raise HTTPError(400, "一次最多附 %d 个文件" % MAX_FILES)
    out = []
    for rel in files:
        r = rel.strip().replace("\\", "/")
        if not r or r.startswith("/") or re.match(r"^[A-Za-z]:", r):
            raise HTTPError(400, "附件路径要是工作区里的相对路径：%s" % rel)
        try:
            p = policy.resolve(ws["path"], r)
        except policy.PathError:
            raise HTTPError(400, "附件不在工作区里：%s" % rel)
        if not os.path.isfile(p):
            raise HTTPError(400, "附件不存在：%s" % rel)
        norm = policy.relpath(ws["path"], p)
        if norm not in out:
            out.append(norm)
    return out


@route("POST", "/api/pick_folder")
def api_pick_folder(h, qs):
    """弹系统的"选择文件夹"窗口。同一时间只弹一个，已有一个开着时回 409。"""
    lock = h.server.pick_lock
    if not lock.acquire(False):
        raise HTTPError(409, "已经打开了一个选择文件夹的窗口，先在那个窗口里选好或取消")
    try:
        try:
            res = picker.pick_folder()
        except Exception:
            res = {"supported": False}
    finally:
        lock.release()
    if not isinstance(res, dict) or not res.get("supported"):
        return h.send_json({"supported": False})
    path = res.get("path")
    if not isinstance(path, str) or not path:
        return h.send_json({"supported": True, "path": None})
    name = os.path.basename(path.rstrip("/\\")) or path
    h.send_json({"supported": True, "path": path, "name": name})


# ---------------- 记忆 ----------------
# 规格见 docs/记忆设计.md 第 6 节。MemoryError 由 _dispatch 统一转成 400（撤销冲突 409），JSON 里带 error 和 code。

def _memory_ws(h, qs):
    wid = _q1(qs, "workspace")
    if wid:
        h.ws_from(wid=wid)
    return wid or None


@route("GET", "/api/memories")
def api_memories(h, qs):
    store, cfg = h.rt.store, h.rt.cfg
    wid = _memory_ws(h, qs)
    targets = ("user", "workspace") if wid else ("user",)
    items, use = [], {}
    for t in targets:
        items += memory.entries(store, wid, t)
        use[t] = memory.usage(store, cfg, wid, t)
    st = memory.settings(cfg)
    h.send_json({"memories": items, "usage": use, "settings": {"enabled": st["enabled"], "auto": st["auto"]}})


@route("POST", "/api/memories")
def api_memory_add(h, qs):
    b = h.body()
    wid = b.get("workspace_id") or None
    if wid is not None:
        if not isinstance(wid, str):
            raise HTTPError(400, "workspace_id 应该是文字")
        h.ws_from(wid=wid)
    target = b.get("target")
    if target is None:
        target = "workspace" if wid else "user"       # 兼容旧写法：只给 workspace_id
    if target not in ("user", "workspace"):
        raise HTTPError(400, "target 只能是 user 或 workspace")
    if target == "workspace" and not wid:
        raise HTTPError(400, "记到本工作区笔记时要给 workspace_id")
    text = b.get("text")
    if not isinstance(text, str):
        raise HTTPError(400, "text 应该是文字")
    r = memory.add(h.rt.store, h.rt.cfg, wid, target, text, "user")
    h.send_json(dict(r["entry"], log_id=r["log_id"]))


@route("POST", "/api/memories/undo")
def api_memory_undo(h, qs):
    b = h.body()
    lid = b.get("log_id")
    if isinstance(lid, bool) or not isinstance(lid, int):
        raise HTTPError(400, "log_id 应该是整数")
    r = memory.undo(h.rt.store, h.rt.cfg, lid)
    # 那次改动是某个任务里记下的：在那个任务里记一笔，时间线刷新后不再显示"撤销"按钮。不进模型上下文
    sid = r.get("session_id")
    if sid and h.rt.store.session(sid):
        h.rt.emit(sid, "memory_undo", {"log_id": r["log_id"], "memory_id": r["memory_id"]})
    h.send_json(r)


@route("GET", "/api/memories/log")
def api_memory_log(h, qs):
    wid = _memory_ws(h, qs)
    try:
        limit = max(1, min(int(_q1(qs, "limit") or 50), 500))
    except ValueError:
        raise HTTPError(400, "limit 应该是整数")
    h.send_json({"log": memory.log(h.rt.store, wid, limit)})


@route("PATCH", "/api/memories/([\\w-]+)")
def api_memory_edit(h, qs, mid):
    b = h.body()
    if not isinstance(b.get("text"), str):
        raise HTTPError(400, "text 应该是文字")
    r = memory.edit(h.rt.store, h.rt.cfg, mid, b["text"], "user")
    h.send_json(dict(r["entry"], log_id=r["log_id"]))


@route("DELETE", "/api/memories/([\\w-]+)")
def api_memory_del(h, qs, mid):
    r = memory.delete(h.rt.store, h.rt.cfg, mid, "user")
    h.send_json({"ok": True, "log_id": r["log_id"]})


# ---------------- 搜索以前的任务 ----------------

@route("GET", "/api/search")
def api_search(h, qs):
    ws = h.ws_from(qs)
    q = (_q1(qs, "q") or "").strip()
    if not q:
        raise HTTPError(400, "缺少搜索词 q")
    mode = _q1(qs, "mode")
    if mode and not modes.valid(mode):
        raise HTTPError(400, "mode 只能是 %s" % " 或 ".join(modes.IDS))
    h.send_json({"hits": historytools.search(h.rt.store, ws["id"], q, mode=mode or None)})


# ---------------- 定时 ----------------

@route("GET", "/api/schedules")
def api_schedules(h, qs):
    wid = _q1(qs, "workspace")
    if wid:
        rows = h.rt.store.q("SELECT * FROM schedules WHERE workspace_id=? ORDER BY created", (wid,))
    else:
        rows = h.rt.store.q("SELECT * FROM schedules ORDER BY created")
    for r in rows:
        r["enabled"] = bool(r["enabled"])
    h.send_json(rows)


def _schedule(h, tid):
    r = h.rt.store.one("SELECT * FROM schedules WHERE id=?", (tid,))
    if not r:
        raise HTTPError(404, "没有这个定时任务")
    r["enabled"] = bool(r["enabled"])
    return r


@route("POST", "/api/schedules")
def api_schedule_add(h, qs):
    b = h.body()
    ws = h.ws_from(wid=b.get("workspace_id"))
    prompt = str(b.get("prompt") or "").strip()
    when = str(b.get("when") or "").strip()
    if not prompt:
        raise HTTPError(400, "要执行的指令不能为空")
    try:
        nxt = next_run(when, time.time())
    except ValueError as e:
        raise HTTPError(400, str(e))
    tid = new_id("t_")
    h.rt.store.x("INSERT INTO schedules(id,workspace_id,prompt,spec,next_run,enabled,created) VALUES(?,?,?,?,?,1,?)",
                 (tid, ws["id"], prompt, when, nxt, now()))
    h.send_json(_schedule(h, tid))


@route("PATCH", "/api/schedules/([\\w-]+)")
def api_schedule_patch(h, qs, tid):
    sc = _schedule(h, tid)
    b = h.body()
    if "enabled" in b:
        on = bool(b["enabled"])
        nxt = sc["next_run"]
        if on and (not nxt or nxt <= time.time()):
            try:
                nxt = next_run(sc["spec"], time.time())     # 重新启用时从现在算下一次，不补跑错过的
            except ValueError as e:
                raise HTTPError(400, str(e))
        h.rt.store.x("UPDATE schedules SET enabled=?, next_run=? WHERE id=?", (1 if on else 0, nxt, tid))
    h.send_json(_schedule(h, tid))


@route("DELETE", "/api/schedules/([\\w-]+)")
def api_schedule_del(h, qs, tid):
    _schedule(h, tid)
    h.rt.store.x("DELETE FROM schedules WHERE id=?", (tid,))
    h.send_json({"ok": True})


# ---------------- 技能、模板 ----------------

@route("GET", "/api/skills")
def api_skills(h, qs):
    h.send_json(h.rt.skills.index())      # 每项 {name, description, source: builtin | user}


def _skill_or_404(h, name):
    sk = h.rt.skills.get(name)
    if not sk:
        raise HTTPError(404, "没有这个技能：%s" % name)
    return sk


@route("GET", "/api/skills/([\\w.-]+)")
def api_skill(h, qs, name):
    sk = _skill_or_404(h, name)
    h.send_json({"name": sk["name"], "description": sk["description"], "source": sk["source"], "body": sk["body"]})


@route("DELETE", "/api/skills/([\\w.-]+)")
def api_skill_del(h, qs, name):
    sk = _skill_or_404(h, name)
    if sk["source"] != "user":
        raise HTTPError(400, "内置技能不能删除")
    root = h.rt.skills.dir_of("user")
    d = os.path.realpath(sk["dir"])
    # 只删数据目录 skills/ 下面的那一个技能文件夹
    if not root or os.path.dirname(d) != os.path.realpath(root):
        raise HTTPError(400, "这个技能不在数据目录的 skills 文件夹里，不能从这里删除")
    shutil.rmtree(d)
    h.rt.skills.reload()
    h.send_json({"ok": True})


@route("GET", "/api/templates")
def api_templates(h, qs):
    p = os.path.join(h.rt.app_dir, "agent", "templates.json")
    items = []
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as f:
                v = json.load(f)
            items = v if isinstance(v, list) else []
        except (OSError, ValueError):
            items = []
    mode = _q1(qs, "mode")
    if mode:
        if not modes.valid(mode):
            raise HTTPError(400, "mode 只能是 %s" % " 或 ".join(modes.IDS))
        items = [t for t in items if isinstance(t, dict) and modes.norm(t.get("mode")) == mode]
    h.send_json(items)


# ---------------- 资料库（docs/两种模式.md 第 5 节） ----------------

def _svc(h):
    return kbm.get(h.rt)


def _lib_or_404(h, lid):
    lib = kbm.library(h.rt.store, lid)
    if not lib:
        raise HTTPError(404, "没有这个资料库")
    return lib


@route("GET", "/api/libraries")
def api_libraries(h, qs):
    svc = _svc(h)
    h.send_json([kbm.summary(svc, lib) for lib in kbm.libraries(h.rt.store)])


@route("POST", "/api/libraries")
def api_library_add(h, qs):
    b = h.body()
    note = b.get("note") or ""
    if not isinstance(note, str):
        raise HTTPError(400, "note 应该是文字")
    lib = kbm.create_library(h.rt.store, b.get("name"), note)
    h.send_json(kbm.summary(_svc(h), lib))


@route("PATCH", "/api/libraries/([\\w-]+)")
def api_library_patch(h, qs, lid):
    _lib_or_404(h, lid)
    b = h.body()
    lib = kbm.update_library(h.rt.store, lid, b.get("name"), b.get("note"))
    h.send_json(kbm.summary(_svc(h), lib))


@route("DELETE", "/api/libraries/([\\w-]+)")
def api_library_del(h, qs, lid):
    _lib_or_404(h, lid)
    kbm.delete_library(_svc(h), lid)          # 只删索引，不动文件
    h.send_json({"ok": True})


@route("POST", "/api/libraries/([\\w-]+)/sources")
def api_library_source_add(h, qs, lid):
    _lib_or_404(h, lid)
    b = h.body()
    kbm.add_source(h.rt.store, lid, b.get("path"))
    svc = _svc(h)
    svc.indexer.enqueue(lid)                  # 随即开始收录（后台）
    h.send_json(kbm.summary(svc, kbm.library(h.rt.store, lid)))


@route("DELETE", "/api/libraries/([\\w-]+)/sources/([\\w-]+)")
def api_library_source_del(h, qs, lid, sid):
    _lib_or_404(h, lid)
    svc = _svc(h)
    kbm.remove_source(svc, lid, sid)
    h.send_json(kbm.summary(svc, kbm.library(h.rt.store, lid)))


@route("POST", "/api/libraries/([\\w-]+)/reindex")
def api_library_reindex(h, qs, lid):
    _lib_or_404(h, lid)
    svc = _svc(h)
    svc.indexer.enqueue(lid)
    h.send_json(kbm.summary(svc, kbm.library(h.rt.store, lid)))


def _doc_row(d):
    return {"id": d["id"], "title": d["title"], "path": d["path"], "kind": d.get("kind") or "", "size": d.get("size"),
            "parts": d.get("chunks") or 0, "status": d.get("status") or "ok", "error": d.get("error") or "",
            "mtime": d.get("mtime"), "version_label": d.get("version_label") or ""}


@route("GET", "/api/libraries/([\\w-]+)/docs")
def api_library_docs(h, qs, lid):
    _lib_or_404(h, lid)
    sql, args = "SELECT * FROM kb_docs WHERE library_id=?", [lid]
    st = _q1(qs, "status")
    if st:
        if st not in ("ok", "failed", "skipped"):
            raise HTTPError(400, "status 只能是 ok、failed 或 skipped")
        sql += " AND status=?"
        args.append(st)
    rows = h.rt.store.q(sql + " ORDER BY path", tuple(args))
    q = (_q1(qs, "q") or "").strip().lower()
    if q:
        rows = [r for r in rows if q in (r["path"] or "").lower() or q in (r["title"] or "").lower()]
    h.send_json([_doc_row(r) for r in rows])


def _doc_or_404(h, did):
    d = h.rt.store.one("SELECT * FROM kb_docs WHERE id=?", (did,))
    if not d:
        raise HTTPError(404, "没有这个文档")
    return d


@route("GET", "/api/kb/docs/([\\w-]+)/parts")
def api_kb_doc_parts(h, qs, did):
    """切片预览：这份文档切成了哪些片段（带所在的节）。"""
    _doc_or_404(h, did)
    h.send_json(h.rt.store.q("SELECT id, seq, parent, loc, heading, text, tokens FROM kb_parts WHERE doc_id=? ORDER BY seq",
                             (did,)))


@route("GET", "/api/kb/docs/([\\w-]+)/file")
def api_kb_doc_file(h, qs, did):
    """把原文件发给浏览器：文本按纯文本、图片内嵌，其余按附件下载（和 /api/file 一样）。"""
    d = _doc_or_404(h, did)
    p = kbm.doc_abs(h.rt.store, d)
    if not p or not os.path.isfile(p):
        raise HTTPError(404, "原文件已经不在了：%s" % d["path"])
    name = os.path.basename(p)
    with open(p, "rb") as f:
        data = f.read()
    ext = os.path.splitext(p)[1].lower()
    attach = "attachment; filename*=UTF-8''%s" % quote(name)
    if _q1(qs, "download") != "1" and ext in fstools.TEXT_EXT:
        return h.send_bytes(fstools._decode(data).encode("utf-8"), "text/plain; charset=utf-8",
                            disposition="inline; filename*=UTF-8''%s" % quote(name))
    if _q1(qs, "download") != "1" and ext == ".pdf":
        return h.send_bytes(data, "application/pdf", disposition="inline; filename*=UTF-8''%s" % quote(name))
    h.send_bytes(data, OFFICE_TYPES.get(ext) or mimetypes.guess_type(name)[0] or "application/octet-stream", disposition=attach)


OFFICE_TYPES = {".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
                ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation"}


@route("GET", "/api/kb/parts/([\\w-]+)")
def api_kb_part(h, qs, pid):
    d = kbm.part_detail(h.rt.store, pid)
    if not d:
        raise HTTPError(404, "没有这个片段")
    h.send_json(d)


@route("POST", "/api/kb/search")
def api_kb_search(h, qs):
    """检索测试：{"query", "libraries"?: [id], "top_k"?} → {"hits", "weak", "used", "ms"}。编号从 1 开始，不占对话的编号。"""
    from .kb import retrieve
    b = h.body()
    q = b.get("query")
    if not isinstance(q, str) or not q.strip():
        raise HTTPError(400, "缺少 query")
    libs = kbm.with_answers(h.rt.store, check_libraries(h.rt, b.get("libraries")) or
                            [r["id"] for r in kbm.libraries(h.rt.store)])          # 9.5：标准答案总是参与
    k = b.get("top_k", h.rt.cfg.get("kb", "top_k", default=5))
    if isinstance(k, bool) or not isinstance(k, int) or not 1 <= k <= 20:
        raise HTTPError(400, "top_k 应该是 1 到 20 的整数")
    svc = _svc(h)
    caps = _caps(h.rt)
    eps = caps.endpoints()
    cw = caps.context_of(eps[0]) if eps else 32768
    res = retrieve.search(svc, [q], libs, top_k=k, context_window=cw)
    hits = retrieve.expand(h.rt.store, res["hits"])
    for i, x in enumerate(hits, 1):
        x["n"] = i
    _, kept = retrieve.render(hits, retrieve.budget(cw))
    # 弱命中（覆盖率低于 kb.min_cover）不算找到，另放在 weak 里给管理员看"差点匹配上什么"
    weak = retrieve.expand(h.rt.store, res.get("weak") or [])
    for i, x in enumerate(weak, 1):
        x["n"] = i
    h.send_json({"hits": [retrieve.public(x) for x in kept], "weak": [retrieve.public(x) for x in weak],
                 "used": res["used"], "ms": res["ms"]})


# ---------------- 反馈和标准答案（docs/两种模式.md 9.5） ----------------

@route("POST", "/api/sessions/([\\w-]+)/feedback")
def api_feedback(h, qs, sid):
    """{"seq", "rating": "up" | "down" | null, "note"?} → 写一条 feedback 事件，返回它的 payload {"seq", "rating", "note"}。"""
    _session_or_404(h.rt, sid)
    h.send_json(feedback.record(h.rt, sid, h.body()))


@route("GET", "/api/kb/answers")
def api_answers(h, qs):
    h.send_json(feedback.list_answers(h.rt.store))


@route("POST", "/api/kb/answers")
def api_answer_add(h, qs):
    item, _ = feedback.save_answer(h.rt, h.body())
    h.send_json(item)


@route("PATCH", "/api/kb/answers/([\\w-]+)")
def api_answer_patch(h, qs, aid):
    h.send_json(feedback.update_answer(h.rt, aid, h.body()))


@route("DELETE", "/api/kb/answers/([\\w-]+)")
def api_answer_del(h, qs, aid):
    feedback.delete_answer(h.rt, aid)
    h.send_json({"ok": True})


# 旧的 /api/kb（按工作区）保留一版，内部转到新的实现：工作区对应的资料库
@route("GET", "/api/kb")
def api_kb(h, qs):
    ws = h.ws_from(qs)
    store = h.rt.store
    lib = store.one("SELECT * FROM kb_libraries WHERE workspace_id=? ORDER BY created LIMIT 1", (ws["id"],))
    if not lib:
        return h.send_json([])
    out = []
    for d in store.q("SELECT * FROM kb_docs WHERE library_id=? ORDER BY added DESC", (lib["id"],)):
        p = kbm.doc_abs(store, d)
        rel = policy.relpath(ws["path"], p) if p else d["path"]
        out.append({"id": d["id"], "workspace_id": ws["id"], "path": rel, "title": d["title"], "mtime": d["mtime"],
                    "chunks": d["chunks"], "added": d["added"], "library_id": lib["id"], "status": d.get("status")})
    h.send_json(out)


@route("POST", "/api/kb")
def api_kb_add(h, qs):
    b = h.body()
    ws = h.ws_from(wid=b.get("workspace_id"))
    p = policy.resolve(ws["path"], str(b.get("path") or ""))
    if not os.path.exists(p):
        raise HTTPError(404, "找不到：%s" % b.get("path"))
    h.send_json(kbtools.add_path(h.rt.store, ws, p, _svc(h)))


@route("DELETE", "/api/kb/([\\w-]+)")
def api_kb_del(h, qs, did):
    if not h.rt.store.one("SELECT id FROM kb_docs WHERE id=?", (did,)):
        raise HTTPError(404, "没有这个文档")
    svc = _svc(h)
    with svc.indexer.ingest_lock:
        try:
            svc.vectors().delete_docs([did])
        except Exception:
            pass
        with h.rt.store.tx():
            kbm.delete_doc_rows(h.rt.store, [did])
    h.send_json({"ok": True})


# ---------------- 设置与审计 ----------------

def _risk_name(t):
    if isinstance(t.risk, str):
        return t.risk
    return getattr(t.risk, "shown", None) or "overwrite"   # 按情况判定的：读文件按"只读"显示，新建或覆盖按较高的一级显示


@route("GET", "/api/settings")
def api_settings(h, qs):
    conf = copy.deepcopy(h.rt.cfg.data)
    for e in conf.get("endpoints") or []:
        e["api_key"] = mask_key(e.get("api_key"))
    for k in ("asr", "embedding", "rerank"):
        if isinstance(conf.get(k), dict):
            conf[k]["api_key"] = mask_key(conf[k].get("api_key"))
    tools = [{"name": t.name, "desc": t.desc, "risk": _risk_name(t), "setting": h.rt.store.tool_setting(t.name) or "default"}
             for t in REGISTRY.values()]
    h.send_json({"config": conf, "tools": tools})


@route("POST", "/api/settings")
def api_settings_set(h, qs):
    b = h.body()
    bad = [k for k in b if k not in SETTINGS_KEYS and k not in SETTINGS_EXT]
    if bad:
        raise HTTPError(400, "不能修改这些设置：%s" % "、".join(bad))
    for k, fn in list(SETTINGS_EXT.items()):
        if k in b:
            b[k] = fn(h, b[k])
    if "approval_mode" in b and b["approval_mode"] not in ("manual", "auto", "full"):
        raise HTTPError(400, "批准模式只能是 manual、auto 或 full")
    if "user_location" in b:
        from . import whereami
        try:
            b["user_location"] = whereami.clean_location(b["user_location"])
        except ValueError as e:
            raise HTTPError(400, str(e))
    if "model" in b:
        if not isinstance(b["model"], dict):
            raise HTTPError(400, "model 应该是对象")
        if "caps" in b["model"]:
            raise HTTPError(400, "model.caps 只能写在配置文件里")
    if "browser" in b:
        b["browser"] = _browser_patch(b["browser"])
    if "endpoints" in b:
        if not isinstance(b["endpoints"], list) or not all(isinstance(e, dict) and e.get("base_url") for e in b["endpoints"]):
            raise HTTPError(400, "endpoints 应该是带 base_url 的对象列表")
        for e in b["endpoints"]:
            if "context_window" in e and e["context_window"] is not None:
                v = e["context_window"]
                if not isinstance(v, int) or isinstance(v, bool) or v < 4096:
                    raise HTTPError(400, "端点的 context_window 应该是 4096 以上的整数")
            if e.get("context_window") is None:
                e.pop("context_window", None)
            if "caps" in e and e["caps"] is not None:
                e["caps"] = _caps_patch(e["caps"])
            if e.get("caps") is None:
                e.pop("caps", None)
        old = h.rt.cfg.get("endpoints") or []
        for i, e in enumerate(b["endpoints"]):
            k = e.get("api_key")
            if isinstance(k, str) and k.startswith(MASK):
                # 界面回传的是打码后的值：沿用原来的密钥
                prev = next((o for o in old if o.get("base_url") == e.get("base_url") and o.get("name") == e.get("name")), None)
                if prev is None and i < len(old):
                    prev = old[i]
                e["api_key"] = (prev or {}).get("api_key", "")
    if "mcp_servers" in b and not isinstance(b["mcp_servers"], dict):
        raise HTTPError(400, "mcp_servers 应该是对象")
    if "asr" in b:
        b["asr"] = _asr_patch(h.rt.cfg, b["asr"])
    if "memory" in b:
        m = b["memory"]
        if not isinstance(m, dict):
            raise HTTPError(400, "memory 应该是对象")
        bad = [k for k in m if k not in ("enabled", "auto")]
        if bad:
            raise HTTPError(400, "记忆设置在这里只能改 enabled 和 auto，其余写在配置文件里：%s" % "、".join(bad))
        if not all(isinstance(v, bool) for v in m.values()):
            raise HTTPError(400, "memory.enabled、memory.auto 应该是 true 或 false")
    if "ui" in b:
        u = b["ui"]
        if not isinstance(u, dict):
            raise HTTPError(400, "ui 应该是对象")
        bad = [k for k in u if k != "theme"]
        if bad:
            raise HTTPError(400, "界面设置只能改 theme：%s" % "、".join(bad))
        if "theme" in u and not (isinstance(u["theme"], str) and THEME_RE.match(u["theme"])):
            raise HTTPError(400, "主题名只能是 1 到 32 个小写字母、数字或短横线")
    for k in ("embedding", "rerank"):
        if k in b:
            b[k] = _model_service_patch(h.rt.cfg, k, b[k])
    if "kb" in b:
        b["kb"] = _kb_patch(b["kb"])
    old_sig = _embed_sig(h.rt.cfg)
    h.rt.cfg.update(b)
    if _embed_sig(h.rt.cfg) != old_sig:
        svc = _svc(h)
        svc.embed_alive = None
        svc.indexer.enqueue_all()           # 换了向量模型：旧向量作废，后台重新算（收录线程开头会检查）
    if "mcp_servers" in b:
        h.rt.reset_mcp()
    if "endpoints" in b or "model" in b:
        _caps(h.rt).reprobe_async()       # 改了端点或模型：后台重探
    for fn in list(SETTINGS_AFTER):
        fn(h, b)
    h.send_json({"ok": True})


def _embed_sig(cfg):
    from .kb import embed as emb
    return emb.signature(cfg)


EMBED_KEYS = {"embedding": ("engine", "base_url", "model", "api_key", "dims", "batch", "query_prefix"),
              "rerank": ("engine", "base_url", "model", "api_key")}
ENGINES = {"embedding": ("off", "openai"), "rerank": ("off", "vllm")}


def _model_service_patch(cfg, key, p):
    """向量模型、重排模型的设置（docs/两种模式.md 2.4）。回传打码的密钥时沿用原来的。"""
    if not isinstance(p, dict):
        raise HTTPError(400, "%s 应该是对象" % key)
    bad = [k for k in p if k not in EMBED_KEYS[key]]
    if bad:
        raise HTTPError(400, "不能修改这些设置：%s" % "、".join("%s.%s" % (key, k) for k in bad))
    out = dict(p)
    if "engine" in out and out["engine"] not in ENGINES[key]:
        raise HTTPError(400, "%s.engine 只能是 %s" % (key, " 或 ".join(ENGINES[key])))
    for k in ("base_url", "model", "api_key", "query_prefix"):
        if k in out:
            if out[k] is None:
                out[k] = ""
            if not isinstance(out[k], str):
                raise HTTPError(400, "%s.%s 应该是文字" % (key, k))
            if k in ("base_url", "model"):
                out[k] = out[k].strip()
    if "dims" in out:
        v = out["dims"]
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 65536:
            raise HTTPError(400, "embedding.dims 应该是 0 以上的整数（0 表示用模型给的长度）")
    if "batch" in out:
        v = out["batch"]
        if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 256:
            raise HTTPError(400, "embedding.batch 应该是 1 到 256 的整数")
    if isinstance(out.get("api_key"), str) and out["api_key"].startswith(MASK):
        out["api_key"] = (cfg.get(key) or {}).get("api_key", "")
    return out


def _kb_patch(p):
    if not isinstance(p, dict):
        raise HTTPError(400, "kb 应该是对象")
    bad = [k for k in p if k not in ("top_k", "min_cover", "min_sim") and k not in KB_KEYS_EXT]
    if bad:
        raise HTTPError(400, "资料库的设置在这里只能改 top_k、min_cover、min_sim，其余写在配置文件里：%s" % "、".join(bad))
    p = dict(p)
    for k, fn in list(KB_KEYS_EXT.items()):
        if k in p:
            p[k] = fn(p[k])
    v = p.get("top_k")
    if "top_k" in p and (isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 20):
        raise HTTPError(400, "kb.top_k 应该是 1 到 20 的整数")
    for k in ("min_cover", "min_sim"):
        v = p.get(k)
        if k in p and (isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1):
            raise HTTPError(400, "kb.%s 应该是 0 到 1 之间的数" % k)
    return dict(p)


@route("POST", "/api/settings/test_embedding")
def api_settings_test_embedding(h, qs):
    """试着算一个向量：{"ok", "dims", "ms", "error"}。请求体可带 {embedding: {...}} 测还没保存的设置（不会保存）。"""
    from .kb import embed as emb
    b = h.body()
    override = None
    raw = b.get("embedding") if isinstance(b.get("embedding"), dict) else {k: b[k] for k in EMBED_KEYS["embedding"] if k in b}
    if raw:
        override = _model_service_patch(h.rt.cfg, "embedding", raw)
        override["engine"] = "openai"
    res = emb.probe(h.rt.cfg, override)
    cur, tried = emb.conf(h.rt.cfg), emb.conf(h.rt.cfg, override)
    if all(cur[k] == tried[k] for k in ("base_url", "model", "dims")):       # 测的就是现在用的那个：记下在不在线
        svc = _svc(h)
        svc.mark_embed(res["ok"], res["error"])
        if res["ok"]:
            svc.embed_dims = res["dims"]
    h.send_json(res)


def _domains(v, key):
    if not isinstance(v, list):
        raise HTTPError(400, "browser.%s 应该是域名列表" % key)
    out = []
    for x in v:
        if not isinstance(x, str):
            raise HTTPError(400, "browser.%s 里应该都是域名" % key)
        d = x.strip().strip(".").strip().lower()
        if not DOMAIN_RE.match(d) or ".." in d:
            raise HTTPError(400, "不是合法的域名：%s" % x)
        if d not in out:
            out.append(d)
    return out


def _browser_patch(p):
    """操作网页的设置（docs/操控电脑.md 2.5、第 9 节）：这里只能改 enabled、allowed_domains、blocked_domains、view，
    其余写在配置文件里。"""
    if not isinstance(p, dict):
        raise HTTPError(400, "browser 应该是对象")
    bad = [k for k in p if k not in ("enabled", "allowed_domains", "blocked_domains", "view")]
    if bad:
        raise HTTPError(400, "操作网页的设置在这里只能改 enabled、allowed_domains、blocked_domains、view，其余写在配置文件里：%s"
                        % "、".join(bad))
    out = {}
    if "view" in p:
        if p["view"] not in ("panel", "window"):
            raise HTTPError(400, "browser.view 只能是 panel（在程序里的网页面板看）或 window（单独的浏览器窗口）")
        out["view"] = p["view"]
    if "enabled" in p:
        if not isinstance(p["enabled"], bool):
            raise HTTPError(400, "browser.enabled 应该是 true 或 false")
        out["enabled"] = p["enabled"]
    for k in ("allowed_domains", "blocked_domains"):
        if k in p:
            out[k] = _domains(p[k], k)
    return out


def _caps_patch(c):
    """端点自己的 caps：{"tools": bool, "vision": bool, "pointing": "pixel" | "norm1000" | false}，值可以是 null（不写）。"""
    if not isinstance(c, dict):
        raise HTTPError(400, "caps 应该是对象")
    bad = [k for k in c if k not in CAPS_KEYS]
    if bad:
        raise HTTPError(400, "caps 只认 tools、vision、pointing：%s" % "、".join(bad))
    out = {}
    for k, v in c.items():
        if v is None:
            continue
        if k == "pointing":
            if v not in ("pixel", "norm1000", False):
                raise HTTPError(400, "caps.pointing 只能是 pixel、norm1000 或 false")
        elif not isinstance(v, bool):
            raise HTTPError(400, "caps.%s 应该是 true 或 false" % k)
        out[k] = v
    return out


@route("POST", "/api/settings/tool")
def api_settings_tool(h, qs):
    b = h.body()
    name, setting = b.get("tool"), b.get("setting")
    if setting not in ("allow", "ask", "deny", "default"):
        raise HTTPError(400, "setting 只能是 allow、ask、deny 或 default")
    if not isinstance(name, str) or not name:
        raise HTTPError(400, "缺少 tool")
    if name not in REGISTRY and not name.startswith("mcp_"):
        raise HTTPError(404, "没有这个工具：%s" % name)
    h.rt.store.set_tool_setting(name, setting)
    h.send_json({"ok": True})


@route("POST", "/api/settings/test")
def api_settings_test(h, qs):
    llm = h.rt.llm_factory()
    ping = getattr(llm, "ping", None)
    h.send_json(ping() if ping else [])


def _asr_patch(cfg, p):
    """校验语音识别设置的补丁。回传打码的密钥时沿用原来的密钥。"""
    if not isinstance(p, dict):
        raise HTTPError(400, "asr 应该是对象")
    bad = [k for k in p if k not in ("engine", "language", "max_seconds") + ASR_STR_KEYS]
    if bad:
        raise HTTPError(400, "不能修改这些语音识别设置：%s" % "、".join(bad))
    out = dict(p)
    if "engine" in out and out["engine"] not in voice.ENGINES:
        raise HTTPError(400, "识别方式只能是 off、vllm 或 sherpa")
    if "language" in out:
        lang = voice.norm_language(out["language"])
        if lang is None:
            raise HTTPError(400, "语言只能是自动、zh 或 en")
        out["language"] = lang
    if "max_seconds" in out:
        v = out["max_seconds"]
        if isinstance(v, bool) or not isinstance(v, int) or not 5 <= v <= 600:
            raise HTTPError(400, "max_seconds 应该是 5 到 600 之间的整数")
    for k in ASR_STR_KEYS:
        if k in out:
            if out[k] is None:
                out[k] = ""
            if not isinstance(out[k], str):
                raise HTTPError(400, "%s 应该是文字" % k)
            out[k] = out[k].strip() if k != "api_key" else out[k]
    if isinstance(out.get("api_key"), str) and out["api_key"].startswith(MASK):
        out["api_key"] = (cfg.get("asr") or {}).get("api_key", "")
    return out


@route("POST", "/api/transcribe")
def api_transcribe(h, qs):
    """请求体是原始 WAV 字节。音频只在内存里处理，不落盘、不进账本和审计。"""
    ctype = (h.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if ctype not in ("audio/wav", "audio/x-wav", "audio/wave", "audio/vnd.wave"):
        raise HTTPError(415, "录音要以 audio/wav 格式发送")
    try:
        n = int(h.headers.get("Content-Length") or 0)
    except ValueError:
        raise HTTPError(400, "请求长度不对")
    if n <= 0:
        raise HTTPError(400, "没有收到录音。")
    if n > MAX_AUDIO_BODY:
        raise HTTPError(400, "录音太大（超过 20 MB），请分几段说。")
    data = h.rfile.read(n)
    lang = _q1(qs, "lang")
    try:
        res = voice.transcribe(h.rt.cfg, data, lang)
    except voice.ASRError as e:
        raise HTTPError(e.code, e.msg)
    finally:
        data = None
    h.send_json(res)


@route("POST", "/api/settings/test_asr")
def api_settings_test_asr(h, qs):
    """用 1 秒静音走一遍识别引擎。请求体可带 {asr: {...}} 测试还没保存的设置（不会保存）。"""
    b = h.body()
    conf = dict(h.rt.cfg.get("asr") or {})
    if b.get("asr") is not None:
        conf.update(_asr_patch(h.rt.cfg, b["asr"]))
    h.send_json(voice.probe_engine({"asr": conf}))


@route("GET", "/api/audit")
def api_audit(h, qs):
    try:
        limit = max(1, min(int(_q1(qs, "limit") or 100), 1000))
    except ValueError:
        raise HTTPError(400, "limit 应该是整数")
    h.send_json(h.rt.store.q("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)))


# ---- 第十轮（docs/借鉴开源项目.md）新加的接口各在自己的模块里 ----
# 模块里用 route / replace_route 登记接口，用 SETTINGS_EXT 等登记设置；这里只负责把它们载进来。放在文件最后：
# 那时上面的名字都已经定义好了，模块里可以放心 from .server import …
from . import api_kbops, api_kernel, api_shared, api_browser  # noqa: E402,F401
