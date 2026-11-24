"""网页面板的接口（docs/操控电脑.md 第 9 节）：在程序里看 Agent 正在操作的网页，也可以自己点、滚、打字、输网址。

- GET  /api/browser/view?session_id=…   画面：multipart/x-mixed-replace 的 JPEG 流（MJPEG），界面直接放在 <img> 里
- GET  /api/browser/state?session_id=…  开没开、网址、标题、等你回答的弹框、显示方式（panel / window）
- POST /api/browser/input   {session_id, kind: click|dblclick|move|wheel|text|key, x, y, dx, dy, text, key}
- POST /api/browser/nav     {session_id, action: go|back|forward|reload|stop, url}
- POST /api/browser/dialog  {session_id, accept: bool, text}
- POST /api/browser/size    {session_id, width, height}：面板画面区域的大小，网页按它排版（宽至少 1024）
状态变化另外通过事件流推 browser_view（不写账本）。用户在面板里打的字不写账本、不进审计、不给模型看。"""
from __future__ import annotations
import time

from .server import route, HTTPError, SETTINGS_AFTER
from . import browser as bmod

FRAME_KEEPALIVE = 10.0      # 这么久没有新画面就把上一帧再发一次（顺便发现界面已经关了）
FLUSH_AFTER = 0.12          # 发完一帧，这么久没有新的就把它重发一次，让浏览器马上画出来
MAX_TEXT = 2000


def _mgr(h):
    from . import capability
    feat = capability.get(h.rt).feature("browser")
    if not feat.get("ok"):
        raise HTTPError(409, feat.get("reason") or "「操作网页」现在用不了")
    return bmod.get(h.rt)


def _sid(h, qs=None, body=None):
    sid = str(((body or {}).get("session_id") if body is not None else (qs.get("session_id") or [""])[0]) or "").strip()
    if not sid or h.rt.store.session(sid) is None:
        raise HTTPError(404, "没有这个对话")
    return sid


@route("GET", "/api/browser/state")
def api_browser_state(h, qs):
    sid = _sid(h, qs)
    from . import capability
    feat = capability.get(h.rt).feature("browser")
    if not feat.get("ok"):
        h.send_json({"available": False, "reason": feat.get("reason") or "", "open": False})
        return
    v = bmod.get(h.rt).view_state(sid)
    v["available"] = True
    h.send_json(v)


@route("GET", "/api/browser/view")
def api_browser_view(h, qs):
    sid = _sid(h, qs)
    m = _mgr(h)
    h.send_response(200)
    h.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
    h.send_header("Cache-Control", "no-store")
    h.send_header("X-Accel-Buffering", "no")
    h.end_headers()
    m.watch(sid)
    last = 0

    def part(data):
        h.wfile.write(b"Content-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(data))
        h.wfile.write(data)
        h.wfile.write(b"\r\n--frame\r\n")
        h.wfile.flush()
    try:
        h.wfile.write(b"--frame\r\n")
        h.wfile.flush()
        f = None
        while not h.server.stopping.is_set():
            if f is None:
                f = m.wait_frame(sid, last, FRAME_KEEPALIVE) or m.frames.get(sid)
                if f is None:
                    continue
            last = f["seq"]
            part(f["jpeg"])
            # Chrome 要等下一帧开始来了才把这一帧画出来：短时间内没有新的一帧，就把这一帧再发一次
            nxt = m.wait_frame(sid, last, FLUSH_AFTER)
            if nxt is None:
                part(f["jpeg"])
            f = nxt
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    finally:
        m.unwatch(sid)


def _num(v, name, lo=None, hi=None):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise HTTPError(400, "%s 应该是数字" % name)
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        raise HTTPError(400, "%s 超出范围" % name)
    return float(v)


@route("POST", "/api/browser/input")
def api_browser_input(h, qs):
    b = h.body()
    sid = _sid(h, body=b)
    kind = b.get("kind")
    kw = {}
    if kind in ("click", "dblclick", "move", "wheel"):
        kw["x"] = _num(b.get("x"), "x", 0, 1)
        kw["y"] = _num(b.get("y"), "y", 0, 1)
        if kind == "wheel":
            kw["dx"] = _num(b.get("dx", 0), "dx", -5000, 5000)
            kw["dy"] = _num(b.get("dy", 0), "dy", -5000, 5000)
    elif kind == "text":
        t = b.get("text")
        if not isinstance(t, str) or not t or len(t) > MAX_TEXT:
            raise HTTPError(400, "text 应该是 1 到 %d 个字" % MAX_TEXT)
        kw["text"] = t
    elif kind == "key":
        k = b.get("key")
        if k not in bmod.USER_KEYS:
            raise HTTPError(400, "不支持这个按键：%s" % k)
        kw["key"] = k
    else:
        raise HTTPError(400, "kind 只能是 click、dblclick、move、wheel、text、key")
    try:
        h.send_json(_mgr(h).user_input(sid, kind, **kw))
    except bmod.BrowserError as e:
        raise HTTPError(409, str(e))


@route("POST", "/api/browser/nav")
def api_browser_nav(h, qs):
    b = h.body()
    sid = _sid(h, body=b)
    action = b.get("action")
    if action not in ("go", "back", "forward", "reload", "stop"):
        raise HTTPError(400, "action 只能是 go、back、forward、reload、stop")
    url = b.get("url") or ""
    if action == "go" and (not isinstance(url, str) or not url.strip() or len(url) > 4000):
        raise HTTPError(400, "要打开的地址不对")
    try:
        h.send_json(_mgr(h).user_nav(sid, action, url))
    except bmod.BrowserError as e:
        raise HTTPError(409, str(e))


@route("POST", "/api/browser/size")
def api_browser_size(h, qs):
    """面板画面区域的大小（CSS 像素）：网页按这个比例排版，画面正好铺满面板。"""
    b = h.body()
    sid = _sid(h, body=b)
    w = _num(b.get("width"), "width", 100, 10000)
    hh = _num(b.get("height"), "height", 100, 10000)
    h.send_json(_mgr(h).set_size(sid, w, hh))


@route("POST", "/api/browser/dialog")
def api_browser_dialog(h, qs):
    b = h.body()
    sid = _sid(h, body=b)
    if not isinstance(b.get("accept"), bool):
        raise HTTPError(400, "accept 应该是 true 或 false")
    text = b.get("text")
    if text is not None and (not isinstance(text, str) or len(text) > MAX_TEXT):
        raise HTTPError(400, "text 不对")
    try:
        h.send_json({"ok": bool(_mgr(h).answer_dialog(sid, b["accept"], text))})
    except bmod.BrowserError as e:
        raise HTTPError(409, str(e))


def _after_settings(h, b):
    """换了显示方式（面板 / 单独窗口）：关掉浏览器，下次用的时候按新的方式开。登录状态在资料目录里，不会丢。"""
    if isinstance(b.get("browser"), dict) and "view" in b["browser"]:
        try:
            h.rt.close_browser()
        except Exception:
            pass


SETTINGS_AFTER.append(_after_settings)
