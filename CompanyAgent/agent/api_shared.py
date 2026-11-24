"""公司共享目录：状态、检查更新、资料库索引包的导出和导入 的接口（docs/借鉴开源项目.md 第 5 节）。归"后端·共享"管。

接口用 route 登记；要改 server.py 里已有接口的行为，用 replace_route 换掉它，不要去改 server.py。
设置里新加的键用 SETTINGS_EXT / KB_KEYS_EXT 登记校验函数，保存之后要做的事放进 SETTINGS_AFTER。

新接口：
- GET /api/shared、POST /api/shared/check（5.7）
- POST /api/libraries/<id>/export（5.5）：{dir?, source_root?} → {dir, path, version, docs, parts, vectors, source_root}
- POST /api/kb/gaps/submit（5.6）→ {path, count}

换掉的接口（原来的行为原样保留，只多了共享目录的部分）：
- GET /api/settings：多 locked（被公司锁住的项）。打码照原来的函数做（叠加后的值里的密钥照样打码）。
- POST /api/settings：先查有没有改被锁住的项（改了 400），和锁住的值一样的项从请求里去掉（不写进本机的文件），其余交给原函数。
- PATCH /api/libraries/<id>：多收 enabled（停用 / 启用）；公司共享的库只收 enabled。
- DELETE /api/libraries/<id>：公司共享的库不能删，除非共享目录里已经没有它了。
- POST /api/libraries/<id>/reindex、DELETE /api/kb/<文档 id>：公司共享的库 400。
- GET /api/kb/docs/<id>/file：公司共享的库的原文件用 source_root 加相对路径；连不上时 404 并说明。"""
from __future__ import annotations
import os

from .server import route, replace_route, HTTPError, SETTINGS_EXT, KB_KEYS_EXT, SETTINGS_AFTER  # noqa: F401
from . import server as srv
from . import kb as kbm
from . import shared as shm
from .api_kbops import _call

SYNC_MIN, SYNC_MAX = 5, 1440


def _sh(h):
    s = getattr(h.rt, "shared", None)
    if s is None:
        raise HTTPError(503, "公司共享目录的服务没有启动")
    return s


# ---------------- 设置：shared.dir、shared.sync_minutes ----------------

def _shared_patch(h, v):
    """SETTINGS_EXT["shared"]：只收 dir 和 sync_minutes。dir 空着表示不用；否则是一个文件夹的完整路径（不要求现在连得上）。"""
    if not isinstance(v, dict):
        raise HTTPError(400, "shared 应该是对象")
    bad = [k for k in v if k not in ("dir", "sync_minutes")]
    if bad:
        raise HTTPError(400, "公司共享目录的设置只能改 dir 和 sync_minutes：%s" % "、".join(str(x) for x in bad))
    out = {}
    if "dir" in v:
        d = v["dir"]
        if d is None:
            d = ""
        if not isinstance(d, str) or len(d) > 1000:
            raise HTTPError(400, "shared.dir 应该是文字（文件夹的完整路径）")
        d = d.strip()
        if d and not shm._abs_ok(d):
            raise HTTPError(400, "shared.dir 应该是文件夹的完整路径，例如 \\\\fs\\公司共享\\Agent 或 Z:\\Agent")
        out["dir"] = d
    if "sync_minutes" in v:
        n = v["sync_minutes"]
        if isinstance(n, bool) or not isinstance(n, int) or not (n == 0 or SYNC_MIN <= n <= SYNC_MAX):
            raise HTTPError(400, "shared.sync_minutes 应该是 %d 到 %d 的整数（0 表示只在启动和点「检查」时看）"
                            % (SYNC_MIN, SYNC_MAX))
        out["sync_minutes"] = n
    return out


SETTINGS_EXT["shared"] = _shared_patch


def _after_settings(h, changed):
    if isinstance(changed, dict) and "shared" in changed:
        try:
            _sh(h).settings_changed()
        except Exception:
            pass


SETTINGS_AFTER.append(_after_settings)


# ---------------- GET / POST /api/settings：locked ----------------

@replace_route("GET", "/api/settings")
def api_settings(h, qs):
    """原来的返回多一个 locked：被公司锁住的项（界面据此置灰）。"""
    obj, code = _call(srv.api_settings, h, qs)
    if isinstance(obj, dict):
        sh = getattr(h.rt, "shared", None)
        obj["locked"] = sh.locked_names() if sh is not None else []
        # config.shared 是本机的（只能在本机填），叠加不会改它；这里照原样
    h.send_json(obj, code)


def _flat(b):
    """请求体 → [(路径元组, 值)]，粒度和下发设置的叠加一样（对象类的按子键）。"""
    out = []
    for k, v in b.items():
        if k in shm.DICT_LEAVES or k in ("browser", "kb", "tools", "ui"):
            if isinstance(v, dict):
                out += [((k, sk), x) for sk, x in v.items()]
                continue
        out.append(((k,), v))
    return out


@replace_route("POST", "/api/settings")
def api_settings_set(h, qs):
    """改被公司锁住的项：400"这一项由公司统一设置，不能在这里改。"（带 locked: [键]）。
    和锁住的值一样的项（界面整段回传时）从请求里去掉，不写进本机的文件。其余交给原函数。"""
    b = h.body()
    cfg = h.rt.cfg
    if getattr(cfg, "overlay", None):
        hit, drop = [], []
        for path, v in _flat(b):
            locked, lv = cfg.locked_value(path)
            if not locked:
                continue
            same = v == lv or (path[-1] == "api_key" and isinstance(v, str) and v.startswith(srv.MASK))
            if path == ("endpoints",) and isinstance(v, list) and isinstance(lv, list) and len(v) == len(lv):
                same = all(isinstance(a, dict) and {k: x for k, x in a.items() if k != "api_key"} ==
                           {k: x for k, x in e.items() if k != "api_key"} and
                           (not isinstance(a.get("api_key"), str) or a["api_key"].startswith(srv.MASK) or a["api_key"] == e.get("api_key"))
                           for a, e in zip(v, lv))
            (drop if same else hit).append(path)
        if hit:
            return h.send_json({"error": shm.LOCKED_MSG, "locked": [".".join(p) for p in hit]}, 400)
        for path in drop:
            if len(path) == 1:
                b.pop(path[0], None)
            else:
                sub = b.get(path[0])
                if isinstance(sub, dict):
                    sub.pop(path[1], None)
                    if not sub:
                        b.pop(path[0], None)
    obj, code = _call(srv.api_settings_set, h, qs, body=b)
    h.send_json(obj, code)


# ---------------- 5.7 /api/shared ----------------

@route("GET", "/api/shared")
def api_shared(h, qs):
    h.send_json(_sh(h).public())


@route("POST", "/api/shared/check")
def api_shared_check(h, qs):
    """立刻看一遍（正在后台看的那次最多等 120 秒，等不到就返回现在的状态，checking 为 true）。"""
    h.send_json(_sh(h).check(timeout=120))


# ---------------- 5.5 导出 ----------------

@route("POST", "/api/libraries/([\\w-]+)/export")
def api_library_export(h, qs, lid):
    """{dir?, source_root?} → {dir, path, version, docs, parts, vectors, source_root}。
    不给 dir 写到共享目录的 libraries/<库名>/；给了 dir 就写进那个文件夹（索引包本身的文件夹）。
    source_root（规格外，可选）：别的电脑上看原文件所在的根，例如 \\\\fs\\制度；不给按本机的来源文件夹算。"""
    b = h.body()
    d = b.get("dir")
    if d is not None and not isinstance(d, str):
        raise HTTPError(400, "dir 应该是文字")
    d = d.strip() if isinstance(d, str) and d.strip() else None
    h.send_json(_sh(h).export_library(lid, d, b.get("source_root")))


# ---------------- 5.6 交给管资料的人 ----------------

@route("POST", "/api/kb/gaps/submit")
def api_gaps_submit(h, qs):
    """把本机"没答上来的问题"里还开着的写成 <共享目录>/inbox/gaps/<用户名>.csv（覆盖自己上次交的）→ {path, count}。
    共享目录没配、读不到或写不进去：400 并说明。"""
    h.send_json(_sh(h).submit_gaps())


# ---------------- 资料库：共享库只读、停用 ----------------

def _lib_or_404(h, lid):
    lib = kbm.library(h.rt.store, lid)
    if not lib:
        raise HTTPError(404, "没有这个资料库")
    return lib


@replace_route("PATCH", "/api/libraries/([\\w-]+)")
def api_library_patch(h, qs, lid):
    """多收 enabled（true / false）：停用的库不参与检索。公司共享的库只能改 enabled。"""
    lib = _lib_or_404(h, lid)
    b = h.body()
    rest = {k: v for k, v in b.items() if k != "enabled"}
    if "enabled" in b:
        if not isinstance(b["enabled"], bool):
            raise HTTPError(400, "enabled 应该是 true 或 false")
        if lid == kbm.ANSWERS_ID:
            raise HTTPError(400, kbm.BUILTIN_MSG)
        if lib.get("shared") and any(k in rest for k in ("name", "note")):
            raise HTTPError(400, kbm.SHARED_MSG % lib["name"])
        h.rt.store.x("UPDATE kb_libraries SET enabled=? WHERE id=?", (1 if b["enabled"] else 0, lid))
    if rest or "enabled" not in b:
        obj, code = _call(srv.api_library_patch, h, qs, lid, body=rest)
        return h.send_json(obj, code)
    h.send_json(kbm.summary(kbm.get(h.rt), kbm.library(h.rt.store, lid)))


@replace_route("DELETE", "/api/libraries/([\\w-]+)")
def api_library_del(h, qs, lid):
    lib = _lib_or_404(h, lid)
    if lib.get("shared"):
        if not _sh(h).orphan(lid):
            raise HTTPError(400, kbm.SHARED_MSG % lib["name"])
        kbm.delete_library(kbm.get(h.rt), lid, allow_shared=True)
        return h.send_json({"ok": True})
    obj, code = _call(srv.api_library_del, h, qs, lid)
    h.send_json(obj, code)


@replace_route("POST", "/api/libraries/([\\w-]+)/reindex")
def api_library_reindex(h, qs, lid):
    lib = _lib_or_404(h, lid)
    if lib.get("shared"):
        raise HTTPError(400, kbm.SHARED_MSG % lib["name"])
    obj, code = _call(srv.api_library_reindex, h, qs, lid)
    h.send_json(obj, code)


@replace_route("DELETE", "/api/kb/([\\w-]+)")
def api_kb_del(h, qs, did):
    d = h.rt.store.one("SELECT library_id FROM kb_docs WHERE id=?", (did,))
    if d and kbm.is_shared(h.rt.store, d.get("library_id")):
        lib = kbm.library(h.rt.store, d["library_id"]) or {}
        raise HTTPError(400, kbm.SHARED_MSG % lib.get("name"))
    obj, code = _call(srv.api_kb_del, h, qs, did)
    h.send_json(obj, code)


@replace_route("GET", "/api/kb/docs/([\\w-]+)/file")
def api_kb_doc_file(h, qs, did):
    """公司共享的库：原文件 = source_root + 相对路径。连不上时 404："原文件在共享盘上，现在连不上"（片段文字照样能看）。"""
    d = h.rt.store.one("SELECT * FROM kb_docs WHERE id=?", (did,))
    if d and kbm.is_shared(h.rt.store, d.get("library_id")):
        p = kbm.doc_abs(h.rt.store, d)
        try:
            ok = bool(p) and os.path.isfile(p)
        except OSError:
            ok = False
        if not ok:
            raise HTTPError(404, "原文件在共享盘上，现在连不上（%s）。片段的文字照样能看，它在索引里。" % (p or d.get("path")))
    # 共享库的文件找得到、或者不是共享库：交给原来的函数（它发文件，不用截返回）
    srv.api_kb_doc_file(h, qs, did)
