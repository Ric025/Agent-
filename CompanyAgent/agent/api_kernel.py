"""工具按需加载的状态、带参数的任务模板 的接口（docs/借鉴开源项目.md）。归"后端·内核"管。

接口用 route 登记；要改 server.py 里已有接口的行为，用 replace_route 换掉它，不要去改 server.py。
设置里新加的键用 SETTINGS_EXT / KB_KEYS_EXT 登记校验函数，保存之后要做的事放进 SETTINGS_AFTER。

- GET /api/toolsets：工具分组（设置页展示用，第 3.9 节）
- 设置 tools：只收 lazy（auto / on / off）和 loop_guard（0 到 10 的整数）
- GET /api/templates（换掉原来的）、POST /api/templates、PATCH / DELETE /api/templates/<id>、
  POST /api/templates/render、POST /api/templates/parse（第 6 节；parse 是规格外加的，给"我的模板"实时显示解析结果）"""
from __future__ import annotations

from .server import route, replace_route, HTTPError, SETTINGS_EXT, KB_KEYS_EXT, SETTINGS_AFTER  # noqa: F401
from . import modes, templates as tplm, toolsets


# ---------------- 工具分组 ----------------

@route("GET", "/api/toolsets")
def api_toolsets(h, qs):
    """[{id, name, desc, tools: ["…"]}]。连接器每个一组（mcp:<名字>），tools 是已经连上的那些工具名。"""
    rt = h.rt
    mcp = {}
    try:
        for t in rt._started_mcp_tools():
            srv = toolsets.mcp_server(t)
            if srv:
                mcp.setdefault(srv, []).append(t.name)
    except Exception:
        mcp = {}
    h.send_json(toolsets.catalog(rt, mcp))


def _tools_patch(h, v):
    if not isinstance(v, dict):
        raise HTTPError(400, "tools 应该是对象")
    bad = [k for k in v if k not in ("lazy", "loop_guard")]
    if bad:
        raise HTTPError(400, "tools 在这里只能改 lazy 和 loop_guard：%s" % "、".join(bad))
    if "lazy" in v and v["lazy"] not in ("auto", "on", "off"):
        raise HTTPError(400, "tools.lazy 只能是 auto、on 或 off")
    if "loop_guard" in v:
        n = v["loop_guard"]
        if isinstance(n, bool) or not isinstance(n, int) or not 0 <= n <= 10:
            raise HTTPError(400, "tools.loop_guard 应该是 0 到 10 的整数（0 表示不拦）")
    return dict(v)


SETTINGS_EXT["tools"] = _tools_patch


# ---------------- 任务模板 ----------------

def _mode_q(qs):
    v = (qs.get("mode") or [""])[0]
    if v and not modes.valid(v):
        raise HTTPError(400, "mode 只能是 %s" % " 或 ".join(modes.IDS))
    return v or None


@replace_route("GET", "/api/templates")
def api_templates(h, qs):
    """每项：{id, title, prompt, role, mode, params, source}（本机模板解析有问题时多 errors）。按 mode 过滤。"""
    h.send_json(tplm.listing(h.rt, _mode_q(qs)))


@route("POST", "/api/templates")
def api_templates_add(h, qs):
    try:
        item = tplm.add_user(h.rt, h.body())
    except tplm.TemplateError as e:
        raise HTTPError(400, str(e))
    h.send_json(item)


def _user_only(h, tid):
    item = tplm.find(h.rt, tid)
    if item is None:
        raise HTTPError(404, "没有这个模板")
    if item.get("source") != "user":
        raise HTTPError(403, "这条模板不是本机的（%s），不能在这里改或删" % tplm.SOURCE_NAMES.get(item.get("source"), item.get("source")))
    return item


@route("PATCH", "/api/templates/([\\w-]+)")
def api_templates_patch(h, qs, tid):
    _user_only(h, tid)
    try:
        item = tplm.update_user(h.rt, tid, h.body())
    except tplm.TemplateError as e:
        raise HTTPError(400, str(e))
    h.send_json(item)


@route("DELETE", "/api/templates/([\\w-]+)")
def api_templates_delete(h, qs, tid):
    _user_only(h, tid)
    tplm.delete_user(h.rt, tid)
    h.send_json({"ok": True})


@route("POST", "/api/templates/render")
def api_templates_render(h, qs):
    """{id, values} → {text}。也可以不给 id、直接给 prompt（和可选的 params），给"我的模板"预览用。
    缺必填的：400，{"error": …, "missing": ["月份"]}；值不合格式的：400，{"error": …, "invalid": [{name, why}]}。"""
    b = h.body()
    values = b.get("values") if b.get("values") is not None else {}
    if not isinstance(values, dict):
        raise HTTPError(400, "values 应该是对象")
    if b.get("id"):
        item = tplm.find(h.rt, str(b["id"]))
        if item is None:
            raise HTTPError(404, "没有这个模板")
    elif isinstance(b.get("prompt"), str):
        try:
            explicit = tplm.norm_params(b.get("params"))
        except tplm.TemplateError as e:
            raise HTTPError(400, str(e))
        item = {"prompt": b["prompt"], "params": explicit, "_explicit": explicit is not None}
    else:
        raise HTTPError(400, "要给 id（或者 prompt）")
    r = tplm.render_item(item, values)
    if r["missing"] or r["invalid"]:
        parts = []
        if r["missing"]:
            parts.append("还没填：%s" % "、".join(r["missing"]))
        if r["invalid"]:
            parts.append("；".join("%s %s" % (x["name"], x["why"]) for x in r["invalid"]))
        h.send_json({"error": "。".join(parts), "missing": r["missing"], "invalid": r["invalid"]}, 400)
        return
    h.send_json({"text": r["text"]})


@route("POST", "/api/templates/parse")
def api_templates_parse(h, qs):
    """{prompt} → {params, errors}。新建模板时界面实时显示解析出了哪几个空（规格外加的小接口，不存任何东西）。"""
    b = h.body()
    if not isinstance(b.get("prompt"), str):
        raise HTTPError(400, "prompt 应该是文字")
    p = tplm.parse(b["prompt"])
    h.send_json({"params": p["params"], "errors": p["errors"]})
