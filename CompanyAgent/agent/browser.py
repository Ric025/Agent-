"""操作网页的浏览器层（docs/操控电脑.md 第 2 节）。

分三层：
  - 驱动接口 Driver：开页面、取结构、查元素、点、填、选、按键、后退、上传、截图、关闭。以后要换成 Playwright MCP、
    或者接到用户已经开着的浏览器，只换这一层。现在的实现是 PlaywrightDriver（Python 版 Playwright，直接用）。
  - 专用线程 Worker：Playwright 同步接口的对象只能在创建它的线程里用，所以所有浏览器操作都在这一个线程里做；
    别的线程（每个对话的运行线程、HTTP 线程）通过队列把命令交给它、等结果。每个操作都有超时，
    等待期间看一眼暂停、取消，不会被一个卡住的页面拖住。
  - BrowserManager：每个对话一个标签页、页面概况的缓存、"原地打转"的检测、空闲关闭、地址守卫。
  - 网页面板（docs/操控电脑.md 第 9 节）：界面右边实时显示这个对话的标签页（Chrome 的 screencast 推画面），
    用户在面板里点、滚、打字、输网址，都在专用线程里转成 Playwright 的鼠标键盘操作。默认 browser.view = "panel"：
    浏览器不开窗口（无界面模式），只在面板里看；"window" 是以前那样单独开一个看得见的窗口。

playwright 是可选的：没装时这个模块照样能导入，"操作网页"按缺组件处理。"""
from __future__ import annotations
import base64, glob, hashlib, importlib.util, ipaddress, os, queue, shutil, sys, threading, time
from urllib.parse import urlsplit

from . import pageview

ACTION_TIMEOUT = 10.0       # 点、填这类动作
OPEN_TIMEOUT = 30.0         # 打开页面
SETTLE = 0.4                # 动作之后等页面稳定一下
LOCAL_NAMES = ("localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback")
STALE_TEXT = "页面已经变了，先用 browser_look 看一下。"
TICK = 0.05                 # 有人在看面板时，专用线程空闲下来每隔多久处理一次画面（秒）
DIALOG_WAIT = 120.0         # 用户在面板里操作时弹出的确认框，最多等用户多久（之后按默认处理：提示框确定、确认框取消）
USER_KEYS = {"Enter", "Backspace", "Delete", "Tab", "Escape", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight",
             "Home", "End", "PageUp", "PageDown", "ControlOrMeta+a", "ControlOrMeta+z", "ControlOrMeta+Shift+z",
             "Shift+Tab", "Shift+Enter"}


class BrowserError(Exception):
    """预期内的失败：原样告诉模型。"""


class Stopped(BrowserError):
    pass


# ---------------- 组件 ----------------

_comp_cache = {}


def _bundled_dirs():
    out = []
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env and env != "0":
        out.append(env)
    home = os.path.expanduser("~")
    out += [os.path.join(home, ".cache", "ms-playwright"), os.path.join(home, "Library", "Caches", "ms-playwright")]
    if os.environ.get("LOCALAPPDATA"):
        out.append(os.path.join(os.environ["LOCALAPPDATA"], "ms-playwright"))
    return out


def bundled_chromium():
    for d in _bundled_dirs():
        if glob.glob(os.path.join(d, "chromium-*")) or glob.glob(os.path.join(d, "chromium_headless_shell-*")):
            return True
    return False


def _first_file(paths):
    for p in paths:
        if p and os.path.isfile(p):
            return p
    return None


def system_browser(kind):
    """电脑上已有的浏览器的可执行文件。kind：chrome | msedge。"""
    if kind == "msedge":
        if sys.platform == "win32":
            return _first_file([os.path.join(os.environ.get(k, ""), "Microsoft", "Edge", "Application", "msedge.exe")
                                for k in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")])
        if sys.platform == "darwin":
            return _first_file(["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"])
        return shutil.which("microsoft-edge") or shutil.which("microsoft-edge-stable")
    if sys.platform == "win32":
        return _first_file([os.path.join(os.environ.get(k, ""), "Google", "Chrome", "Application", "chrome.exe")
                            for k in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")])
    if sys.platform == "darwin":
        return _first_file(["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                            os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")])
    return (shutil.which("google-chrome") or shutil.which("google-chrome-stable")
            or _first_file(["/opt/google/chrome/chrome"]) or shutil.which("chromium") or shutil.which("chromium-browser"))


def pick_browser(cfg):
    """按设置选浏览器。返回 (可执行文件路径或 None 表示用 Playwright 自带的 Chromium, 说明) 或抛 BrowserError。"""
    exe = str(cfg.get("browser", "executable_path", default="") or "").strip()
    if exe:
        if os.path.isfile(exe):
            return exe, exe
        raise BrowserError("设置里指定的浏览器不存在：%s" % exe)
    ch = str(cfg.get("browser", "channel", default="auto") or "auto").strip().lower()
    if ch in ("chrome", "msedge"):
        p = system_browser(ch)
        if p:
            return p, ch
        raise BrowserError("找不到 %s 浏览器" % ("Chrome" if ch == "chrome" else "Edge"))
    if ch == "chromium":
        if bundled_chromium():
            return None, "chromium"
        raise BrowserError("没有下载 Playwright 自带的 Chromium")
    order = ("msedge", "chrome") if sys.platform == "win32" else ("chrome",)
    for k in order:
        p = system_browser(k)
        if p:
            return p, k
    if bundled_chromium():
        return None, "chromium"
    raise BrowserError("找不到可用的浏览器（Chrome 或 Edge）")


def headless_mode(cfg):
    """浏览器开不开窗口：网页面板模式（默认）不开，只在程序里的面板看；单独窗口模式开（测试可以用 headless 强制不开）。"""
    if str(cfg.get("browser", "view", default="panel") or "panel") != "window":
        return True
    return bool(cfg.get("browser", "headless", default=False))


def component_ok(cfg):
    """(装了浏览器组件吗, 原因)。结果缓存 60 秒。"""
    key = (str(cfg.get("browser", "channel", default="auto")), str(cfg.get("browser", "executable_path", default="")))
    hit = _comp_cache.get(key)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    if sys.version_info < (3, 10):
        res = (False, "Python %d.%d 上装不了 playwright（需要 3.10 以上）" % sys.version_info[:2])
    elif importlib.util.find_spec("playwright") is None:
        res = (False, "没有安装 playwright")
    else:
        try:
            pick_browser(cfg)
            res = (True, "")
        except BrowserError as e:
            res = (False, str(e))
    _comp_cache[key] = (time.time(), res)
    return res


# ---------------- 地址守卫 ----------------

def norm_host(h):
    return (h or "").strip().strip(".").lower()


def host_of(url):
    try:
        return norm_host(urlsplit(url).hostname or "")
    except ValueError:
        return ""


def domain_match(host, names):
    """名单里写 example.com，a.example.com 也算。"""
    host = norm_host(host)
    for n in names or []:
        n = norm_host(str(n))
        if n and (host == n or host.endswith("." + n)):
            return True
    return False


def is_local(host):
    host = norm_host(host).strip("[]")
    if host in LOCAL_NAMES or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_loopback or ip.is_unspecified
    except ValueError:
        return False


def url_port(u):
    try:
        p = u.port
    except ValueError:
        return None
    if p is None:
        return 443 if u.scheme == "https" else 80
    return p


def check_url(url, cfg, own_port=None):
    """打开前的检查。返回 (规范化的网址, 拒绝的原因或 None)。"""
    url = (url or "").strip()
    if not url:
        return url, "缺少网址"
    if "://" not in url and not url.lower().startswith(("about:", "data:", "javascript:", "file:", "chrome:")):
        url = "https://" + url
    try:
        u = urlsplit(url)
    except ValueError:
        return url, "网址格式不对：%s" % url
    if u.scheme.lower() not in ("http", "https"):
        return url, "只能打开 http 或 https 的网址。"
    host = norm_host(u.hostname or "")
    if not host:
        return url, "网址里没有域名：%s" % url
    if is_local(host):
        if own_port and url_port(u) == int(own_port):
            return url, "不能打开这个程序自己的界面。"
        if not cfg.get("browser", "allow_local", default=False):
            return url, "不能打开本机地址（%s）。" % host
    if domain_match(host, cfg.get("browser", "blocked_domains") or []):
        return url, "这个网站在禁止名单里：%s" % host
    return url, None


def request_blocked(url, cfg, own_port=None):
    """浏览器发出的每个请求（含跳转、内嵌页面）都过一遍：自己的端口永远拒绝，本机地址默认拒绝，禁止名单拒绝。"""
    try:
        u = urlsplit(url)
    except ValueError:
        return False
    if u.scheme.lower() not in ("http", "https", "ws", "wss"):
        return False
    host = norm_host(u.hostname or "")
    if is_local(host):
        if own_port and url_port(u) == int(own_port):
            return True
        if not cfg.get("browser", "allow_local", default=False):
            return True
    return domain_match(host, cfg.get("browser", "blocked_domains") or [])


# ---------------- 专用线程 ----------------

class Worker:
    """一个专用线程。call(fn) 把 fn 交给它执行并等结果；超时、或者 should_stop() 为真时不再等。"""

    def __init__(self, on_idle=None, name="browser", on_tick=None, busy=None):
        self.q = queue.Queue()
        self.on_idle = on_idle
        self.on_tick = on_tick        # 有人在看网页面板时（busy() 为真），空闲时每 TICK 秒调一次：推画面、看网址变没变
        self.busy = busy
        self.thread = threading.Thread(target=self._loop, name=name, daemon=True)
        self.stopped = False
        self.thread.start()

    def _loop(self):
        last_idle = time.time()
        while True:
            fast = False
            try:
                fast = bool(self.busy and self.busy())
            except Exception:
                pass
            try:
                item = self.q.get(timeout=TICK if fast else 5)
            except queue.Empty:
                if fast and self.on_tick:
                    try:
                        self.on_tick()
                    except Exception:
                        pass
                if self.on_idle and time.time() - last_idle >= 5:
                    last_idle = time.time()
                    try:
                        self.on_idle()
                    except Exception:
                        pass
                continue
            if item is None:
                return
            if item == "wake":
                continue
            fn, box, done = item
            if box.get("abandoned"):
                done.set()
                continue
            try:
                box["r"] = fn()
            except BaseException as e:      # noqa: B902 —— 带回调用线程
                box["e"] = e
            finally:
                done.set()

    def call(self, fn, timeout, should_stop=None):
        if self.stopped:
            raise Stopped("浏览器已经关闭")
        box, done = {}, threading.Event()
        self.q.put((fn, box, done))
        end = time.time() + timeout
        while not done.wait(0.1):
            if should_stop is not None and should_stop():
                box["abandoned"] = True
                raise Stopped("操作被中断了，页面可能已经变了，先用 browser_look 看一下。")
            if time.time() > end:
                box["abandoned"] = True
                raise BrowserError("浏览器 %d 秒没有响应，这一步没有完成。可以稍后用 browser_look 看一下页面。" % int(timeout))
        if "e" in box:
            raise box["e"]
        return box.get("r")

    def wake(self):
        """有人开始看面板：别再等满 5 秒才开始推画面。"""
        self.q.put("wake")

    def stop(self, timeout=15):
        self.stopped = True
        self.q.put(None)
        if self.thread is not threading.current_thread():
            self.thread.join(timeout)


# ---------------- 驱动接口 ----------------

class Driver:
    """浏览器驱动的小接口。所有方法只在专用线程里调用。tab 是驱动自己的页面句柄。"""

    name = "base"

    def new_tab(self, owner):
        raise NotImplementedError

    def open(self, tab, url):
        raise NotImplementedError

    def snapshot(self, tab):
        """返回 {"text": aria 快照, "title", "url"}。"""
        raise NotImplementedError

    def inspect(self, tab, ref):
        """元素信息 {role, name, tag, type, autocomplete, label, in_form, submit_button}；编号失效返回 None。"""
        raise NotImplementedError

    def act(self, tab, action, ref, text, accept_dialogs):
        raise NotImplementedError

    def screenshot(self, tab):
        raise NotImplementedError

    def close_tab(self, tab):
        raise NotImplementedError

    def close(self):
        raise NotImplementedError


INSPECT_JS = """el => {
  const lab = [];
  if (el.labels) for (const l of el.labels) lab.push(l.innerText || '');
  const f = el.form || (el.closest ? el.closest('form') : null);
  const tag = (el.tagName || '').toLowerCase();
  const type = (el.getAttribute('type') || (tag === 'button' && f ? 'submit' : '')).toLowerCase();
  return {tag, type, autocomplete: (el.getAttribute('autocomplete') || '').toLowerCase(),
          label: [el.getAttribute('aria-label') || '', el.getAttribute('placeholder') || '', el.getAttribute('name') || '',
                  el.id || '', el.getAttribute('title') || ''].concat(lab).join(' '),
          in_form: !!f, submit_button: !!f && ((tag === 'button' && type === 'submit') || (tag === 'input' && (type === 'submit' || type === 'image')))};
}"""


class PlaywrightDriver(Driver):
    """Python 版 Playwright。单独的资料目录（登录状态留在这里），用电脑上已有的浏览器。"""

    name = "playwright"

    def __init__(self, cfg, profile_dir, request_filter=None, on_download=None):
        self.cfg = cfg
        self.profile_dir = profile_dir
        self.request_filter = request_filter
        self.pw = None
        self.ctx = None
        self.tabs = []

    def _ensure(self):
        if self.ctx is not None:
            return
        from playwright.sync_api import sync_playwright
        exe, _ = pick_browser(self.cfg)
        os.makedirs(self.profile_dir, exist_ok=True)
        args = []
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            args.append("--no-sandbox")
        self.pw = sync_playwright().start()
        self.headless = headless_mode(self.cfg)
        kw = {"headless": self.headless, "args": args,
              "accept_downloads": True, "viewport": {"width": 1280, "height": 860}}
        if exe:
            kw["executable_path"] = exe
        try:
            self.ctx = self.pw.chromium.launch_persistent_context(self.profile_dir, **kw)
        except Exception:
            self.pw.stop()
            self.pw = None
            raise
        self.ctx.set_default_timeout(ACTION_TIMEOUT * 1000)
        self.ctx.set_default_navigation_timeout(OPEN_TIMEOUT * 1000)
        if self.request_filter is not None:
            flt = self.request_filter

            def guard(route):
                try:
                    if flt(route.request.url):
                        route.abort("blockedbyclient")
                    else:
                        route.continue_()
                except Exception:
                    pass
            self.ctx.route("**/*", guard)
        self.ctx.on("page", self._on_page)
        # 持久化资料目录启动时自带一个空白页：留着给第一个对话用
        self.spare = list(self.ctx.pages)

    def _on_page(self, page):
        """新开的页面算在正在做动作的那个标签页头上（target=_blank 默认 noopener，opener() 拿不到）。"""
        t = getattr(self, "acting", None) or getattr(self, "user_acting", None)
        if t is None:
            for x in self.tabs:
                try:
                    if page.opener() is x["page"]:
                        t = x
                        break
                except Exception:
                    pass
        if t is not None:
            t["new"].append(page)
            self._hook(t, page)

    def _hook(self, t, page):
        def on_dialog(d):
            cb = getattr(self, "dialog_cb", None)
            if getattr(self, "acting", None) is not t and t.get("user_dialogs") and d.type != "beforeunload" and cb:
                # 用户自己在面板里操作时弹出来的：问面板里的用户（弹框挡着页面，点的那一下要等它答完才算做完，
                # 所以在这里等；回答从 HTTP 线程直接送到这里，不经过专用线程）
                try:
                    ans = cb(t, {"type": d.type, "message": d.message, "default": d.default_value})
                except Exception:
                    ans = None
                if ans is not None:
                    accept, text = ans
                    try:
                        if not accept:
                            d.dismiss()
                        elif text is not None and d.type == "prompt":
                            d.accept(text)
                        else:
                            d.accept()
                        return
                    except Exception:
                        return
            t["dialogs"].append({"type": d.type, "message": d.message})
            try:
                if d.type in ("alert", "beforeunload") or t.get("accept"):
                    d.accept()
                else:
                    d.dismiss()
            except Exception:
                pass

        def on_download(dl):
            t["downloads"].append(dl)
        page.on("dialog", on_dialog)
        page.on("download", on_download)

    def new_tab(self, owner):
        self._ensure()
        page = None
        while self.spare:
            p = self.spare.pop()
            if not p.is_closed():
                page = p
                break
        if page is None:
            page = self.ctx.new_page()
        t = {"page": page, "new": [], "dialogs": [], "downloads": [], "accept": False, "owner": owner}
        self._hook(t, page)
        self.tabs.append(t)
        return t

    def alive(self, tab):
        return tab is not None and not tab["page"].is_closed()

    def open(self, tab, url):
        page = tab["page"]
        page.goto(url, wait_until="domcontentloaded", timeout=OPEN_TIMEOUT * 1000)
        try:
            page.wait_for_load_state("load", timeout=5000)
        except Exception:
            pass

    def snapshot(self, tab):
        page = tab["page"]
        try:
            text = page.aria_snapshot(mode="ai", timeout=ACTION_TIMEOUT * 1000)
        except TypeError:
            text = page.locator("body").aria_snapshot()
        return {"text": text or "", "title": page.title(), "url": page.url}

    def inspect(self, tab, ref):
        loc = tab["page"].locator("aria-ref=%s" % ref)
        try:
            if loc.count() != 1:
                return None
            return loc.evaluate(INSPECT_JS, timeout=ACTION_TIMEOUT * 1000)
        except Exception:
            return None

    def act(self, tab, action, ref, text, accept_dialogs=False):
        """做一个动作。返回 (新开的标签页数, 弹框列表, 下载对象列表)。"""
        page = tab["page"]
        tab["accept"] = bool(accept_dialogs)
        tab["dialogs"], tab["new"], tab["downloads"] = [], [], []
        self.acting = tab
        loc = page.locator("aria-ref=%s" % ref) if ref else None
        if loc is not None and loc.count() != 1:
            raise BrowserError(STALE_TEXT)
        try:
            if action == "click":
                loc.click(timeout=ACTION_TIMEOUT * 1000)
            elif action == "type":
                loc.fill(text or "", timeout=ACTION_TIMEOUT * 1000)
            elif action == "select":
                try:
                    loc.select_option(label=text, timeout=ACTION_TIMEOUT * 1000)
                except Exception:
                    loc.select_option(text, timeout=ACTION_TIMEOUT * 1000)
            elif action == "key":
                if loc is not None:
                    loc.press(text, timeout=ACTION_TIMEOUT * 1000)
                else:
                    page.keyboard.press(text)
            elif action == "enter":
                loc.press("Enter", timeout=ACTION_TIMEOUT * 1000)
            elif action == "back":
                page.go_back(wait_until="domcontentloaded", timeout=OPEN_TIMEOUT * 1000)
            elif action == "upload":
                loc.set_input_files(text, timeout=ACTION_TIMEOUT * 1000)
            elif action == "check":
                loc.check(timeout=ACTION_TIMEOUT * 1000)
            else:
                raise BrowserError("不认识的动作：%s" % action)
            page.wait_for_timeout(SETTLE * 1000)     # 不能用 time.sleep：Playwright 只在等待它自己的调用时处理事件
            try:
                page.wait_for_load_state("load", timeout=5000)
            except Exception:
                pass
            if action in ("click", "key", "enter") and not tab["new"]:
                page.wait_for_timeout(300)        # 新标签页的事件可能稍晚一点到
        finally:
            tab["accept"] = False
            self.acting = None
        new = [p for p in tab["new"] if not p.is_closed()]
        if new:
            tab["page"] = new[-1]
            try:
                new[-1].wait_for_load_state("load", timeout=10000)
            except Exception:
                pass
        return len(new), list(tab["dialogs"]), list(tab["downloads"])

    def save_download(self, dl, path):
        dl.save_as(path)
        return path

    # ---- 网页面板 ----
    def cast(self, tab, on_frame, size=None):
        """把这个标签页的画面推给 on_frame(jpeg 字节, 元数据)。已经在推同一个页面、大小也对就不动。返回是否在推。
        size：面板要的网页大小 (宽, 高)，和现在的不一样就先改（新开的页面也照这个大小）。"""
        page = tab["page"]
        if size and not page.is_closed():
            vp = page.viewport_size or {}
            if (vp.get("width"), vp.get("height")) != tuple(size):
                page.set_viewport_size({"width": int(size[0]), "height": int(size[1])})
                self.uncast(tab)
        c = tab.get("cast")
        if c is not None and c["page"] is page and not page.is_closed():
            return True
        self.uncast(tab)
        if page.is_closed():
            return False
        cdp = page.context.new_cdp_session(page)

        def on(ev):
            try:
                cdp.send("Page.screencastFrameAck", {"sessionId": ev.get("sessionId")})
            except Exception:
                pass
            try:
                on_frame(base64.b64decode(ev.get("data") or ""), ev.get("metadata") or {})
            except Exception:
                pass
        cdp.on("Page.screencastFrame", on)
        vp = page.viewport_size or {"width": 1280, "height": 860}
        cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 70, "maxWidth": vp["width"],
                                          "maxHeight": vp["height"], "everyNthFrame": 1})
        tab["cast"] = {"page": page, "cdp": cdp}
        if not getattr(self, "headless", True):
            try:
                page.bring_to_front()          # 有窗口时只有最前面的标签页在画
            except Exception:
                pass
        return True

    def uncast(self, tab):
        c = tab.pop("cast", None)
        if not c:
            return
        for fn in (lambda: c["cdp"].send("Page.stopScreencast"), lambda: c["cdp"].detach()):
            try:
                fn()
            except Exception:
                pass

    def pump(self, ms):
        """让 Playwright 处理一会儿事件（画面、弹框、下载）。Playwright 只在执行它自己的调用时处理事件。"""
        for t in self.tabs:
            p = t["page"]
            if not p.is_closed():
                p.wait_for_timeout(ms)
                return True
        return False

    def info(self, tab):
        page = tab["page"]
        return {"url": page.url, "title": page.title()}

    def user_input(self, tab, kind, x=0.0, y=0.0, dx=0.0, dy=0.0, text="", key=""):
        """用户在面板里的操作。x、y 是在画面上的位置（0 到 1）。返回有没有换到新开的页面。"""
        page = tab["page"]
        tab["user_dialogs"] = True
        vp = page.viewport_size or {"width": 1280, "height": 860}
        px = max(0.0, min(1.0, float(x))) * vp["width"]
        py = max(0.0, min(1.0, float(y))) * vp["height"]
        tab["new"] = []
        self.user_acting = tab           # 点了 target=_blank 的链接：新页面算在这个标签页头上
        try:
            if kind == "click":
                page.mouse.click(px, py)
            elif kind == "dblclick":
                page.mouse.dblclick(px, py)
            elif kind == "move":
                page.mouse.move(px, py)
            elif kind == "wheel":
                page.mouse.move(px, py)
                page.mouse.wheel(float(dx), float(dy))
            elif kind == "text":
                page.keyboard.insert_text(text)
            elif kind == "key":
                page.keyboard.press(key)
            else:
                raise BrowserError("不认识的操作：%s" % kind)
            page.wait_for_timeout(80 if kind in ("click", "dblclick", "key") else 10)
        finally:
            self.user_acting = None
        new = [p for p in tab["new"] if not p.is_closed()]
        if new:
            self.uncast(tab)
            tab["page"] = new[-1]
            return True
        return False

    def user_nav(self, tab, action, url=""):
        page = tab["page"]
        tab["user_dialogs"] = True
        try:
            if action == "go":
                page.goto(url, wait_until="commit", timeout=OPEN_TIMEOUT * 1000)
            elif action == "back":
                page.go_back(wait_until="commit", timeout=OPEN_TIMEOUT * 1000)
            elif action == "forward":
                page.go_forward(wait_until="commit", timeout=OPEN_TIMEOUT * 1000)
            elif action == "reload":
                page.reload(wait_until="commit", timeout=OPEN_TIMEOUT * 1000)
            elif action == "stop":
                page.evaluate("window.stop()")
            else:
                raise BrowserError("不认识的操作：%s" % action)
        except BrowserError:
            raise
        except Exception as e:
            raise BrowserError("没打开：%s" % str(e).splitlines()[0][:160])

    def take_downloads(self, tab):
        out, tab["downloads"] = list(tab.get("downloads") or []), []
        return out

    def screenshot(self, tab):
        return tab["page"].screenshot(type="jpeg", quality=60, timeout=ACTION_TIMEOUT * 1000)

    def close_tab(self, tab):
        self.uncast(tab)
        if tab in self.tabs:
            self.tabs.remove(tab)
        for p in [tab["page"]] + list(tab.get("new") or []):
            try:
                if not p.is_closed():
                    p.close()
            except Exception:
                pass

    def close(self):
        try:
            if self.ctx is not None:
                self.ctx.close()
        except Exception:
            pass
        try:
            if self.pw is not None:
                self.pw.stop()
        except Exception:
            pass
        self.ctx = self.pw = None
        self.tabs = []


# ---------------- 管理 ----------------

class Tab:
    """一个对话的标签页：缓存它的网址、标题、给模型看过的那份快照。"""

    def __init__(self, sid, handle):
        self.sid, self.handle = sid, handle
        self.url = ""
        self.title = ""
        self.snap = ""            # 最近一次的原始快照
        self.refs = {}            # 编号 → (role, name)：最近一次给模型看的那份
        self.more = []            # 被截掉、留给 more 的行
        self.version = 0          # 每取一次快照、做一次动作加一（风险判定的缓存靠它）
        self.last_used = time.time()
        self.history = []         # [(动作键, 动作前的页面指纹, 动作后的页面指纹)]
        self.inspect_cache = {}

    def digest(self):
        return hashlib.sha1((self.url + "\n" + self.snap).encode("utf-8", "replace")).hexdigest()


class BrowserManager:
    def __init__(self, rt, driver_factory=None):
        self.rt = rt
        self.lock = threading.RLock()
        self.tabs = {}
        self.worker = None
        self.driver = None
        self.driver_factory = driver_factory
        self.assess_cache = {}
        # 网页面板：谁在看、最新一帧画面、上次推给界面的状态
        self.viewers = {}
        self.frames = {}
        self.frame_cv = threading.Condition()
        self.views = {}
        self._info_at = 0.0
        self.dialogs = {}           # sid → 等用户回答的弹框 {"info", "event", "answer"}
        self.sizes = {}             # sid → 面板要的网页大小 (宽, 高)

    @property
    def cfg(self):
        return self.rt.cfg

    # ---- 线程和驱动 ----
    def _own_port(self):
        return getattr(self.rt, "http_port", None)

    def _make_driver(self):
        if self.driver_factory is not None:
            return self.driver_factory(self)
        return PlaywrightDriver(self.cfg, os.path.join(self.cfg.home, "browser-profile"),
                                request_filter=lambda u: request_blocked(u, self.cfg, self._own_port()))

    def _ensure_worker(self):
        with self.lock:
            if self.rt.closing:
                raise Stopped("程序正在关闭")
            if self.worker is None or self.worker.stopped:
                self.worker = Worker(on_idle=self._idle, name="browser", on_tick=self._tick, busy=lambda: bool(self.viewers))
                self.driver = None
            return self.worker

    def call(self, fn, timeout=ACTION_TIMEOUT + 5, should_stop=None):
        w = self._ensure_worker()

        def run():
            if self.driver is None:
                self.driver = self._make_driver()
                self.driver.dialog_cb = self._user_dialog
            return fn(self.driver)
        return w.call(run, timeout, should_stop)

    def _idle(self):
        """空闲 browser.idle_minutes 分钟的标签页关掉（在专用线程里调用）。"""
        try:
            minutes = float(self.cfg.get("browser", "idle_minutes", default=30) or 30)
        except (TypeError, ValueError):
            minutes = 30
        now = time.time()
        with self.lock:
            old = [t for t in self.tabs.values() if now - t.last_used > minutes * 60 and not self.viewers.get(t.sid)]
            for t in old:
                self.tabs.pop(t.sid, None)
        for t in old:
            if self.driver is not None:
                try:
                    self.driver.close_tab(t.handle)
                except Exception:
                    pass

    # ---- 标签页 ----
    def tab(self, sid):
        with self.lock:
            return self.tabs.get(sid)

    def status(self, sid):
        t = self.tab(sid)
        if t is None:
            return {"open": False}
        return {"open": True, "url": t.url, "title": t.title}

    def _get_or_new(self, d, sid):
        t = self.tab(sid)
        if t is not None and getattr(d, "alive", lambda h: True)(t.handle):
            return t
        h = d.new_tab(sid)
        t = Tab(sid, h)
        with self.lock:
            self.tabs[sid] = t
        return t

    def _snap(self, d, t):
        s = d.snapshot(t.handle)
        t.snap, t.title, t.url = s.get("text") or "", s.get("title") or "", s.get("url") or ""
        t.version += 1
        t.inspect_cache = {}
        t.last_used = time.time()
        return t

    # ---- 对外：在调用方线程里调用 ----
    def open(self, sid, url, should_stop=None):
        def run(d):
            t = self._get_or_new(d, sid)
            d.open(t.handle, url)
            return self._snap(d, t)
        return self.call(run, OPEN_TIMEOUT + 10, should_stop)

    def look(self, sid, should_stop=None):
        t = self.tab(sid)
        if t is None:
            raise BrowserError("还没有打开网页，先用 browser_open 打开一个网址。")

        def run(d):
            if not getattr(d, "alive", lambda h: True)(t.handle):
                raise BrowserError("这个对话的标签页已经关了，先用 browser_open 重新打开。")
            return self._snap(d, t)
        return self.call(run, ACTION_TIMEOUT + 5, should_stop)

    def inspect(self, sid, ref):
        """元素信息（风险判定用）。编号不在最近给模型看的那份里、或者页面已经变了：返回 None。"""
        t = self.tab(sid)
        if t is None:
            return None
        key = (t.version, ref)
        if key in t.inspect_cache:
            return t.inspect_cache[key]
        if ref not in t.refs:
            return None

        def run(d):
            s = d.snapshot(t.handle)
            now = pageview.refs(pageview.parse(s.get("text") or ""))
            if now.get(ref) != t.refs.get(ref) or (s.get("url") or "") != t.url:
                return None
            info = d.inspect(t.handle, ref)
            if info is None:
                return None
            info = dict(info)
            info["role"], info["name"] = t.refs[ref]
            return info
        try:
            info = self.call(run, ACTION_TIMEOUT + 5)
        except BrowserError:
            info = None
        t.inspect_cache[key] = info
        return info

    def act(self, sid, action, ref, text, accept_dialogs=False, should_stop=None):
        t = self.tab(sid)
        if t is None:
            raise BrowserError("还没有打开网页，先用 browser_open 打开一个网址。")

        def run(d):
            n_new, dialogs, downloads = d.act(t.handle, action, ref, text, accept_dialogs)
            saved = []
            for dl in downloads:
                saved.append(dl)
            self._snap(d, t)
            return {"new_tabs": n_new, "dialogs": dialogs, "downloads": saved}
        return self.call(run, OPEN_TIMEOUT + 15, should_stop)

    def save_downloads(self, downloads, folder):
        """把下载存到工作区的 下载/ 目录。返回保存的路径列表。"""
        if not downloads:
            return []

        def run(d):
            out = []
            os.makedirs(folder, exist_ok=True)
            for dl in downloads:
                name = os.path.basename(getattr(dl, "suggested_filename", "") or "下载的文件") or "下载的文件"
                path = _unique(os.path.join(folder, name))
                d.save_download(dl, path)
                out.append(path)
            return out
        return self.call(run, OPEN_TIMEOUT + 30)

    def screenshot(self, sid):
        t = self.tab(sid)
        if t is None:
            raise BrowserError("还没有打开网页，先用 browser_open 打开一个网址。")
        data = self.call(lambda d: d.screenshot(t.handle), ACTION_TIMEOUT + 5)
        return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")

    # ---- 网页面板（docs/操控电脑.md 第 9 节） ----
    def watch(self, sid):
        """界面开始看这个对话的标签页（每个看的连接调一次，看完调 unwatch）。"""
        with self.lock:
            self.viewers[sid] = self.viewers.get(sid, 0) + 1
            w = self.worker
        if w is not None and not w.stopped:
            w.wake()

    def unwatch(self, sid):
        with self.lock:
            n = self.viewers.get(sid, 0) - 1
            if n > 0:
                self.viewers[sid] = n
            else:
                self.viewers.pop(sid, None)

    def _frame(self, sid, data, meta):
        with self.frame_cv:
            old = self.frames.get(sid) or {}
            self.frames[sid] = {"seq": (old.get("seq") or 0) + 1, "jpeg": data, "ts": time.time(),
                                "w": meta.get("deviceWidth"), "h": meta.get("deviceHeight")}
            self.frame_cv.notify_all()

    def wait_frame(self, sid, after, timeout):
        """等这个对话比 after 新的一帧；等不到返回 None。"""
        end = time.time() + timeout
        with self.frame_cv:
            while True:
                f = self.frames.get(sid)
                if f is not None and f["seq"] > after:
                    return f
                left = end - time.time()
                if left <= 0:
                    return None
                self.frame_cv.wait(left)

    def _tick(self):
        """专用线程里、有人在看面板时：给在看的标签页推画面，没人看的停掉；处理事件；网址、标题、弹框变了告诉界面；
        用户自己点出来的下载存到工作区的 下载/ 里。"""
        d = self.driver
        if d is None or not hasattr(d, "cast"):
            return
        with self.lock:
            items = list(self.tabs.items())
            watched = {sid for sid, _ in items if self.viewers.get(sid)}
        for sid, t in items:
            alive = getattr(d, "alive", lambda h: True)(t.handle)
            if sid in watched and alive:
                t.last_used = time.time()
                try:
                    d.cast(t.handle, lambda b, m, sid=sid: self._frame(sid, b, m), self.sizes.get(sid))
                except Exception:
                    pass
            else:
                try:
                    d.uncast(t.handle)
                except Exception:
                    pass
        try:
            d.pump(30)
        except Exception:
            pass
        if time.time() - self._info_at < 0.4:
            return
        self._info_at = time.time()
        for sid, t in items:
            if sid not in watched or not getattr(d, "alive", lambda h: True)(t.handle):
                continue
            try:
                info = d.info(t.handle)
            except Exception:
                continue
            note = ""
            dls = d.take_downloads(t.handle) if hasattr(d, "take_downloads") else []
            if dls:
                note = self._save_user_downloads(d, sid, dls)
            self._publish(sid, dict(info, open=True), note)

    def _publish(self, sid, info, note=""):
        view = {"open": bool(info.get("open")), "url": info.get("url") or "", "title": info.get("title") or "",
                "dialog": info.get("dialog")}
        if view == self.views.get(sid) and not note:
            return
        self.views[sid] = view
        try:
            self.rt.emit(sid, "browser_view", dict(view, note=note) if note else view, store=False)
        except Exception:
            pass

    def _save_user_downloads(self, d, sid, dls):
        try:
            s = self.rt.store.session(sid)
            ws = self.rt.store.workspace(s["workspace_id"]) if s else None
            folder = os.path.join(ws["path"], "下载") if ws else None
        except Exception:
            folder = None
        if not folder:
            return ""
        saved = []
        os.makedirs(folder, exist_ok=True)
        for dl in dls:
            name = os.path.basename(getattr(dl, "suggested_filename", "") or "下载的文件") or "下载的文件"
            path = _unique(os.path.join(folder, name))
            try:
                d.save_download(dl, path)
                saved.append("下载/" + os.path.basename(path))
            except Exception:
                pass
        return ("下载好了：%s" % "、".join(saved)) if saved else ""

    def _user_dialog(self, tab, info):
        """专用线程里（弹框事件）：有人在看这个对话的面板，就把弹框交给面板里的用户，等他选。没人看返回 None（按默认处理）。"""
        sid = tab.get("owner")
        if not sid or not self.viewers.get(sid):
            return None
        p = {"info": info, "event": threading.Event(), "answer": None}
        self.dialogs[sid] = p
        self._publish(sid, dict(self.views.get(sid) or {"open": True}, dialog=info))
        try:
            p["event"].wait(DIALOG_WAIT)
        finally:
            if self.dialogs.get(sid) is p:
                self.dialogs.pop(sid, None)
            self._publish(sid, dict(self.views.get(sid) or {"open": True}, dialog=None))
        return p["answer"]

    def view_state(self, sid):
        """面板要显示的：开没开、网址、标题、等用户回答的弹框、浏览器的显示方式。"""
        t = self.tab(sid)
        v = dict(self.views.get(sid) or {})
        if t is None:
            v = {"open": False, "url": "", "title": "", "dialog": None}
        elif not v:
            v = {"open": True, "url": t.url, "title": t.title, "dialog": None}
        p = self.dialogs.get(sid)
        v["dialog"] = p["info"] if p else None
        f = self.frames.get(sid)
        v["frame"] = f["seq"] if f else 0
        v["w"], v["h"] = (f.get("w"), f.get("h")) if f else (None, None)     # 网页的宽高（CSS 像素）：滚轮换算用
        v["view"] = "window" if str(self.cfg.get("browser", "view", default="panel")) == "window" else "panel"
        return v

    def set_size(self, sid, width, height):
        """面板的大小变了（界面量的画面区域，CSS 像素）：网页按这个比例排版，宽至少 1024（再窄很多网站会换成手机版），
        最宽 1920；高按面板的比例。下一次推画面时生效。"""
        w = max(1024, min(1920, int(round(width))))
        h = max(600, min(2000, int(round(w * float(height) / max(1.0, float(width))))))
        self.sizes[sid] = (w, h)
        wk = self.worker
        if wk is not None and not wk.stopped:
            wk.wake()
        return {"width": w, "height": h}

    def _user_changed(self, t):
        """用户自己在面板里点了、换了页面：Agent 手里那份页面概况作废，下次要先 browser_look。"""
        t.refs, t.snap, t.more = {}, "", []
        t.version += 1
        t.inspect_cache = {}
        t.last_used = time.time()

    def user_input(self, sid, kind, **kw):
        t = self.tab(sid)
        if t is None:
            raise BrowserError("这个对话还没有打开网页。")

        def run(d):
            moved = d.user_input(t.handle, kind, **kw)
            if moved or kind in ("click", "dblclick") or (kind == "key" and kw.get("key") in ("Enter", "Shift+Enter")):
                self._user_changed(t)
            return {"ok": True}
        return self.call(run, ACTION_TIMEOUT + 5 + DIALOG_WAIT)       # 点出了确认框：要等用户在面板里答完

    def user_nav(self, sid, action, url=""):
        if action == "go":
            url = normalize_user_url(url, self.cfg)
            if request_blocked(url, self.cfg, self._own_port()):
                raise BrowserError("这个地址不能在这里打开：%s" % url)

        def run(d):
            t = self.tab(sid)
            if t is None or not getattr(d, "alive", lambda h: True)(t.handle):
                if action != "go":
                    raise BrowserError("这个对话还没有打开网页。")
                t = self._get_or_new(d, sid)
            d.user_nav(t.handle, action, url)
            self._user_changed(t)
            try:
                self._publish(sid, dict(d.info(t.handle), open=True))
            except Exception:
                pass
            return {"ok": True, "url": url}
        r = self.call(run, OPEN_TIMEOUT + 5)
        w = self.worker
        if w is not None:
            w.wake()
        return r

    def answer_dialog(self, sid, accept, text=None):
        """用户在面板里答了弹框（HTTP 线程）：直接交给正在等的那个弹框事件，不经过专用线程（它正等着呢）。"""
        p = self.dialogs.get(sid)
        if p is None:
            return False
        p["answer"] = (bool(accept), text)
        p["event"].set()
        return True

    def close_tab(self, sid):
        with self.lock:
            t = self.tabs.pop(sid, None)
        if t is not None and self.worker is not None and not self.worker.stopped:
            try:
                self.call(lambda d: d.close_tab(t.handle), 10)
            except Exception:
                pass

    def close(self, timeout=15):
        """关掉浏览器和专用线程（设置页的"关闭浏览器"、程序退出、测试结束）。"""
        for p in list(self.dialogs.values()):
            p["event"].set()
        with self.lock:
            w, self.worker = self.worker, None
            self.tabs = {}
            self.assess_cache = {}
            self.views = {}
        with self.frame_cv:
            self.frames = {}
            self.frame_cv.notify_all()
        if w is None or w.stopped:
            return

        def shut():
            if self.driver is not None:
                self.driver.close()
                self.driver = None
        try:
            w.call(shut, timeout)
        except Exception:
            pass
        w.stop(timeout)


def normalize_user_url(text, cfg=None):
    """用户在面板地址栏里输的：像网址的补上 https://；不像网址的当成要搜的词（browser.search_url）。"""
    q = (text or "").strip()
    if not q:
        raise BrowserError("地址是空的")
    low = q.lower()
    if low.startswith(("http://", "https://")):
        return q
    if " " not in q and ("." in q or low.startswith("localhost") or ":" in q) and not low.startswith(("javascript:", "file:", "data:")):
        return "https://" + q if not (low.startswith("localhost") or low.split("/")[0].replace(".", "").split(":")[0].isdigit()) else "http://" + q
    from urllib.parse import quote
    tpl = str((cfg.get("browser", "search_url", default="") if cfg else "") or "https://www.baidu.com/s?wd={q}")
    return tpl.replace("{q}", quote(q))


def _unique(path):
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 2
    while os.path.exists("%s(%d)%s" % (base, i, ext)):
        i += 1
    return "%s(%d)%s" % (base, i, ext)


def get(rt):
    m = getattr(rt, "browser", None)
    if m is None:
        m = BrowserManager(rt)
        try:
            rt.browser = m
        except Exception:
            pass
    return m
