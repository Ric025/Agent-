"""操作网页的三个工具：browser_open、browser_look、browser_do（docs/操控电脑.md 2.2 到 2.4）。

工具少而简单，名字和参数说明都短（三个加起来不超过 500 token）。动作自带新的页面概况，模型不用再单独看一次。
风险在执行前、拿到元素信息后算（risk 是函数）；不该做的（密码框、验证码、本机地址……）风险记为 none、
由处理函数拒绝并说明，这样两个内核都不用为"拒绝"另开一条路。"""
from __future__ import annotations
import json, os, re

from . import tool, S, ToolError
from .. import pageview, policy
from .. import browser as bmod

ACTIONS = ("click", "type", "select", "key", "back", "upload")
NEED_REF = ("click", "type", "select", "upload")
NEED_TEXT = ("type", "select", "key", "upload")
ROLE_CN = {"button": "按钮", "link": "链接", "textbox": "输入框", "searchbox": "搜索框", "checkbox": "勾选框",
           "radio": "单选项", "combobox": "下拉框", "listbox": "列表", "option": "选项", "tab": "标签",
           "menuitem": "菜单项", "switch": "开关", "spinbutton": "数字框", "cell": "单元格", "row": "行"}
DELETE_CN = ("删除", "移除", "作废", "注销", "支付", "付款", "转账", "购买", "下单")
DELETE_EN = re.compile(r"\b(delete|remove|pay|payment|purchase|buy|checkout|transfer)\b", re.I)
SUBMIT_CN = ("提交", "发送", "确认", "保存", "发布", "批准", "同意", "驳回", "审批通过")
SUBMIT_EN = re.compile(r"\b(submit|send|confirm|save|publish|approve|reject)\b", re.I)
BUTTONISH = ("button", "link", "menuitem", "tab")      # 按名字判断"提交"只看这些（勾选框叫"我确认…"不算提交）
CAPTCHA = re.compile(r"(验证码|校验码|动态码|动态密码|短信码|captcha|otp|one[- ]?time|verification code|2fa|totp)", re.I)
PASSWORD_TEXT = "密码要由用户自己输入。请用 ask_user 请用户在网页面板里输入（面板就是对话右边显示的这个网页），输完告诉你再继续。"
CAPTCHA_TEXT = "验证码要由用户自己输入。请用 ask_user 请用户在网页面板里输入，输完告诉你再继续。"
# 整页的人机验证（Cloudflare、滑块、点选图片等）：不自己做，交给用户（docs/操控电脑.md 9.5）
HUMAN_CHECK = re.compile(r"(人机验证|安全验证|请完成验证|完成下方验证|拖动滑块|滑动验证|向右滑动|点击按钮进行验证|请按顺序点击|确认您?是真人|"
                         r"验证您?是真人|我不是机器人|verify you are human|are you a robot|i'?m not a robot|checking your browser|"
                         r"just a moment|recaptcha|hcaptcha|turnstile|captcha)", re.I)
HUMAN_TEXT = ("【这个页面在要求人机验证】不要自己尝试点选、拖动或输入验证内容。用 ask_user 请用户在网页面板里完成验证"
              "（选项写\"我验证好了，继续\"和\"先不做了\"），用户说好了再用 browser_look 看一下接着做。")
LOOP_TEXT = "同一个动作已经连做 3 次，页面没有变化。换个办法（先用 browser_look 看一下），或者问用户。"


def _enabled(ctx):
    from .. import capability
    return capability.get(ctx.rt).ok("browser")


def _vision(ctx):
    from .. import capability
    try:
        return capability.get(ctx.rt).ok("browser_vision")
    except Exception:
        return False


def _mgr(ctx):
    return bmod.get(ctx.rt)


def _should_stop(ctx):
    def f():
        rt = ctx.rt
        if getattr(rt, "closing", False):
            return True
        runner = getattr(ctx, "runner", None)
        lv = runner.live() if runner is not None and hasattr(runner, "live") else None
        return bool(lv is not None and (lv.cancel or lv.pause))
    return f


def _counters(ctx):
    runner = getattr(ctx, "runner", None)
    return getattr(runner, "counters", None)


def _count_action(ctx):
    c = _counters(ctx)
    if c is None:
        return
    limit = int(ctx.cfg.get("browser", "max_actions", default=40) or 40)
    n = getattr(c, "browser_actions", 0)
    if n >= limit:
        raise ToolError("这次运行里浏览器动作已经做了 %d 次，先停下来，把进展告诉用户。" % n)
    c.browser_actions = n + 1


def _limit(ctx, after=False):
    from .. import capability
    try:
        cw = capability.get(ctx.rt).feature_context("browser")
    except Exception:
        cw = ctx.cfg.get("model", "context_window", default=32768)
    return pageview.budget(cw, after)


def _view(ctx, t, after=False):
    text, rest = pageview.render(t.snap, t.title, t.url, _limit(ctx, after))
    t.more = rest
    t.refs = pageview.refs(pageview.parse(t.snap))
    if human_check(t):
        text += "\n" + HUMAN_TEXT
    return text


def human_check(t):
    """页面像是在要求人机验证：看标题和页面开头（不看整页，免得正文里提到"验证码"就误判）。"""
    head = (t.title or "") + "\n" + "\n".join((t.snap or "").splitlines()[:60])
    return bool(HUMAN_CHECK.search(head))


def _allowed(ctx, host):
    return bmod.domain_match(host, ctx.cfg.get("browser", "allowed_domains") or [])


def _display(title, t=None, host=None, text=""):
    return {"kind": "browser", "title": title, "page": (t.title if t else "") or "",
            "host": host if host is not None else (bmod.host_of(t.url) if t else ""), "text": text or ""}


def _el_desc(info, ref):
    if not info:
        return "元素 %s" % ref
    role = ROLE_CN.get(info.get("role"), "元素")
    name = info.get("name") or ""
    return "%s「%s」" % (role, name) if name else "%s %s" % (role, ref)


# ---------------- 风险判定（带缓存：同一页面、同样参数，判定结果不变） ----------------

def assess_open(ctx, args):
    url, why = bmod.check_url(args.get("url"), ctx.cfg, getattr(ctx.rt, "http_port", None))
    host = bmod.host_of(url)
    disp = _display("打开 %s" % (host or url), None, host)
    if why:
        return {"risk": "none", "refuse": why, "display": disp, "url": url}
    return {"risk": "read" if _allowed(ctx, host) else "external", "refuse": None, "display": disp, "url": url}


def _words(info):
    return " ".join(str(info.get(k) or "") for k in ("name", "label"))


def assess_do(ctx, args):
    m = _mgr(ctx)
    t = m.tab(ctx.sid)
    key = (ctx.sid, t.version if t else -1, json.dumps(args, sort_keys=True, ensure_ascii=False))
    hit = m.assess_cache.get(key)
    if hit is not None:
        return hit
    res = _assess_do(ctx, args, m, t)
    if len(m.assess_cache) > 200:
        m.assess_cache.clear()
    m.assess_cache[key] = res
    return res


def _assess_do(ctx, args, m, t):
    action = args.get("action")
    ref = (args.get("ref") or "").strip() or None
    text = args.get("text")
    submit = bool(args.get("submit"))

    def refuse(msg, title="网页操作"):
        return {"risk": "none", "refuse": msg, "display": _display(title, t), "info": None}
    if action not in ACTIONS:
        return refuse("action 只能是 %s 之一。" % "、".join(ACTIONS))
    if action in NEED_REF and not ref:
        return refuse("%s 需要 ref：要操作的元素编号（页面概况里的 [ref=…]）。" % action)
    if action in NEED_TEXT and not (isinstance(text, str) and text.strip() if action != "type" else isinstance(text, str)):
        need = {"type": "要填的文字", "select": "要选的选项", "key": "按键名，例如 Enter", "upload": "工作区里的文件路径"}[action]
        return refuse("%s 需要 text：%s。" % (action, need))
    if t is None:
        return refuse("还没有打开网页，先用 browser_open 打开一个网址。")
    info = None
    if ref:
        info = m.inspect(ctx.sid, ref)
        if info is None:
            return refuse(bmod.STALE_TEXT)
    desc = _el_desc(info, ref)
    shown = ""
    if action == "click":
        title = "点击" + desc
    elif action == "type":
        title, shown = "在%s里填写" % desc, text
    elif action == "select":
        title, shown = "在%s里选择「%s」" % (desc, text), text
    elif action == "key":
        title = "按键 %s" % text + ("（在%s上）" % desc if ref else "")
    elif action == "back":
        title = "后退"
    else:
        title, shown = "上传文件到%s" % desc, text
    disp = _display(title, t, text=shown)
    if info is not None and action in ("type", "key"):
        if info.get("type") == "password" or "password" in (info.get("autocomplete") or ""):
            return {"risk": "none", "refuse": PASSWORD_TEXT, "display": disp, "info": info}
        if CAPTCHA.search(_words(info)) or "one-time-code" in (info.get("autocomplete") or ""):
            return {"risk": "none", "refuse": CAPTCHA_TEXT, "display": disp, "info": info}
    name = (info or {}).get("name") or ""
    if info is not None and (any(w in name for w in DELETE_CN) or DELETE_EN.search(name)):
        risk = "delete"
    elif (submit or action == "upload"
          or (action == "key" and str(text).strip().lower() == "enter"
              and (info is None or (info.get("in_form") and info.get("tag") == "input")))
          or (action == "click" and info is not None
              and (info.get("submit_button")
                   or (info.get("role") in BUTTONISH and (any(w in name for w in SUBMIT_CN) or SUBMIT_EN.search(name)))))):
        risk = "submit"
    else:
        risk = "web"
    if risk == "web" and not _allowed(ctx, bmod.host_of(t.url)):
        risk = "external"           # 跳到了允许名单以外的网站：至少按"对外"
    return {"risk": risk, "refuse": None, "display": disp, "info": info}


def _risk_open(ctx, args):
    return assess_open(ctx, args)["risk"]


def _disp_open(ctx, args):
    return assess_open(ctx, args)["display"]


def _risk_do(ctx, args):
    return assess_do(ctx, args)["risk"]


def _disp_do(ctx, args):
    return assess_do(ctx, args)["display"]


def _disp_look(ctx, args):
    t = _mgr(ctx).tab(ctx.sid)
    f = (args.get("find") or "").strip() if isinstance(args.get("find"), str) else ""
    return _display("在页面里找「%s」" % f if f else "看页面", t)


def _look_variant(ctx, t):
    """看网页截图可用时，browser_look 多一个 image 参数。"""
    if not _vision(ctx):
        return t
    from . import Tool
    props = dict(t.props)
    props["image"] = {"type": "boolean", "description": "附上截图"}
    nt = Tool(t.name, t.desc, props, t.required, t.handler, t.risk, t.sub_ok, t.enabled)
    nt.display = t.display
    nt.variant = None
    return nt


# ---------------- 工具 ----------------

@tool("browser_open", "打开网址，返回页面概况", {"url": S("网址")}, risk=_risk_open, enabled=_enabled, display=_disp_open)
def browser_open(ctx, url):
    a = assess_open(ctx, {"url": url})
    if a["refuse"]:
        raise ToolError(a["refuse"])
    _count_action(ctx)
    m = _mgr(ctx)
    try:
        t = m.open(ctx.sid, a["url"], _should_stop(ctx))
    except bmod.BrowserError as e:
        raise ToolError(str(e))
    except Exception as e:
        raise ToolError("打开失败：%s" % _short(e))
    return "已打开 %s。\n%s" % (bmod.host_of(t.url) or t.url, _view(ctx, t))


@tool("browser_look", "看当前页面。不带参数返回概况；find 在整页找文字；more 接着往下看",
      {"find": S("要找的文字"), "more": {"type": "boolean", "description": "接着上次往下看"}},
      required=[], enabled=_enabled, display=_disp_look, variant=_look_variant)
def browser_look(ctx, find=None, more=False, image=False):
    m = _mgr(ctx)
    t = m.tab(ctx.sid)
    if t is None:
        raise ToolError("还没有打开网页，先用 browser_open 打开一个网址。")
    _count_action(ctx)
    if more and not find:
        if not t.more:
            return "已经看到页面末尾了，没有更多内容。"
        text, rest = pageview.cut(["页面：%s" % t.title, "网址：%s（接着上次往下）" % t.url], t.more, _limit(ctx))
        t.more = rest
        return "接着往下看。\n" + text
    try:
        m.look(ctx.sid, _should_stop(ctx))
    except bmod.BrowserError as e:
        raise ToolError(str(e))
    if find:
        hits, total = pageview.find(t.snap, find)
        t.refs = pageview.refs(pageview.parse(t.snap))
        if not hits:
            return "页面里没有找到「%s」。" % find
        head = "在页面里找到「%s」%d 处%s。" % (find, total, "，只列前 %d 处" % len(hits) if total > len(hits) else "")
        return head + "\n" + pageview.wrap(["页面：%s" % t.title, "网址：%s" % t.url], hits)
    out = "看了页面。\n" + _view(ctx, t)
    if image and _vision(ctx):
        try:
            from . import WithImage
            return WithImage(out, m.screenshot(ctx.sid))
        except bmod.BrowserError:
            return out + "\n（截图没有成功）"
    return out


@tool("browser_do", "在页面上做一个动作，返回结果和新的页面概况",
      {"action": S("click、type、select、key、back、upload 之一", list(ACTIONS)), "ref": S("元素编号，如 e13"),
       "text": S("要填的字、选项、按键名或工作区文件路径"), "submit": {"type": "boolean", "description": "这一步是提交"}},
      required=["action"], risk=_risk_do, display=_disp_do, enabled=_enabled)
def browser_do(ctx, action, ref=None, text=None, submit=False):
    args = {"action": action}
    for k, v in (("ref", ref), ("text", text), ("submit", submit)):
        if v not in (None, False, ""):
            args[k] = v
    m = _mgr(ctx)
    t = m.tab(ctx.sid)
    # 执行前再判一次（页面可能变了）：密码框、验证码、失效的编号都在这里拦住
    a = _assess_do(ctx, args, m, t)
    if a["refuse"]:
        raise ToolError(a["refuse"])
    ref = (ref or "").strip() or None
    key = json.dumps([action, ref, text], ensure_ascii=False)
    before = t.digest()
    h = t.history
    if len(h) >= 2 and all(x[0] == key and x[1] == x[2] == before for x in h[-2:]):
        raise ToolError(LOOP_TEXT)
    _count_action(ctx)
    path = None
    if action == "upload":
        try:
            path = policy.resolve(ctx.root, text)
        except policy.PathError as e:
            raise ToolError(str(e))
        if not os.path.isfile(path):
            raise ToolError("工作区里没有这个文件：%s" % text)
    act = action
    payload = path if action == "upload" else text
    if action == "type" and submit:
        act = "type"
    try:
        r = m.act(ctx.sid, act, ref, payload, accept_dialogs=bool(submit), should_stop=_should_stop(ctx))
        if action == "type" and submit:
            r2 = m.act(ctx.sid, "enter", ref, None, accept_dialogs=True, should_stop=_should_stop(ctx))
            r = {"new_tabs": r["new_tabs"] + r2["new_tabs"], "dialogs": r["dialogs"] + r2["dialogs"],
                 "downloads": r["downloads"] + r2["downloads"]}
    except bmod.BrowserError as e:
        raise ToolError(str(e))
    except Exception as e:
        raise ToolError("没有做成：%s" % _short(e))
    t = m.tab(ctx.sid) or t
    t.history = (h + [(key, before, t.digest())])[-6:]
    notes = []
    if r["new_tabs"]:
        notes.append("点击后打开了新标签页，已经切换过去。")
    for d in r["dialogs"]:
        if d["type"] in ("confirm", "prompt"):
            if submit:
                notes.append("页面弹出确认框「%s」，已经点了确定。" % d["message"])
            else:
                notes.append("页面弹出确认框「%s」，没有替用户点确定。如果确实要确定，再做一次同样的动作并带上 submit=true（会先征求用户同意）。"
                             % d["message"])
        elif d["type"] == "alert":
            notes.append("页面弹出提示「%s」，已关闭。" % d["message"])
    if r["downloads"]:
        from .fs import publish
        try:
            folder = policy.resolve(ctx.root, "下载", write=True)
            for p in m.save_downloads(r["downloads"], folder):
                publish(ctx, p, "从网页下载")
                notes.append("下载了文件：%s" % policy.relpath(ctx.root, p))
        except Exception as e:
            notes.append("下载没有保存成功：%s" % _short(e))
    head = "已" + a["display"]["title"] + "。"
    if action == "type":
        head = "已%s「%s」%s。" % (a["display"]["title"], text, "并按了回车" if submit else "")
    return "\n".join([head] + notes + [_view(ctx, t, after=True)])


def _short(e):
    s = str(e).strip().splitlines()
    return (s[0] if s else type(e).__name__)[:200]
