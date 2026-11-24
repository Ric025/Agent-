"""公司共享目录（docs/借鉴开源项目.md 第 5 节）。归"后端·共享"管。

没有服务器：共享盘上的一个文件夹（设置 shared.dir，只能在本机填）就是"公司"——

    <共享目录>/
      companyagent.json        {"version": 1, "name": "某某公司", "contact": "IT 服务台", "updated": "2026-11-21"}
      settings.json            下发的设置 {"defaults": {…}, "locked": {…}}（5.3）
      skills/<名字>/SKILL.md   共享的技能（5.4）
      templates.json           共享的任务模板（5.4，格式同 agent/templates.json）
      libraries/<库名>/        资料库索引包：manifest.json + index.db（5.5）
      inbox/gaps/<用户名>.csv  各人交上来的"没答上来的问题"（5.6）

这里的东西都来自别人，一律当不可信的输入：
- 只读约定好的文件、约定好的键；类型不对、太长、缺字段都不让程序崩，状态里说清楚是哪一个文件、哪一项。
- 下发的设置只认白名单（WHITELIST），值过和本机保存时同样的校验（直接调 agent/server.py 里的校验函数）。
- index.db 先复制到本机数据目录、核对 manifest 里的 sha256，再以只读、immutable 方式打开；只读约定的表和列，
  行数和每个字段的长度有上限，库 id、文档 id、相对路径都查过；导入在一个事务里，失败了本机原来的库原样还在。
- 技能、模板、公司名这些文字是数据：只存、只显示，程序不照着做（技能是用户点名或模型加载时才进上下文，和本机技能一样）。

共享目录读不到时用本机上次同步的缓存（<数据目录>/shared_cache/<目录的哈希>/）：下发的设置、技能、模板都在那里；
资料库本来就导进了本机的账本。缓存按共享目录的路径分开，换了目录不会串。

后台线程：程序启动时看一遍，之后每 shared.sync_minutes 分钟看一遍（0 表示只在启动和手动"检查"时看）；
程序关闭时 stop()：线程在两个步骤之间、复制文件和导入的每一批之间看一眼停止标志，最多等 5 秒。"""
from __future__ import annotations
import copy, csv, getpass, hashlib, io, json, os, re, shutil, sqlite3, tempfile, threading, time, traceback
from urllib.parse import quote

from .store import now

FORMAT = 1
JSON_MAX = 256 * 1024            # companyagent.json、settings.json、templates.json 的大小上限
MANIFEST_MAX = 64 * 1024
DB_MAX = 1024 * 1024 * 1024      # index.db 最大 1 GB
COPY_CHUNK = 1 << 20

SKILLS_MAX = 50                  # 共享技能最多几个
SKILL_MD_MAX = 64 * 1024
SKILL_FILES_MAX = 20             # 一个技能文件夹里最多复制几个文件（含 SKILL.md）
SKILL_FILE_MAX = 256 * 1024
TEMPLATES_MAX = 100
LIBS_MAX = 30
SETTINGS_KEYS_MAX = 200          # settings.json 里最多看多少个键

DOCS_MAX = 20000
SECTIONS_MAX = 400000
PARTS_MAX = 400000
SECTION_TEXT_MAX = 200000
PART_TEXT_MAX = 20000
HEADING_MAX = 2000
LOC_MAX = 500
PATH_MAX = 1000
TITLE_MAX = 300
VEC_DIMS_MAX = 8192
BATCH = 500

ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}\Z")
HEX_RE = re.compile(r"^[0-9a-f]{64}\Z")
CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
BAD_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

WRITABLE_NOTE = "这个共享目录你有写权限。能写的人都能改技能和设置，建议让 IT 设成只读。"
LOCKED_MSG = "这一项由公司统一设置，不能在这里改。"


# ---------------- 小工具 ----------------

def _clean(s, cap):
    """公司名、联系人这类给人看的短文字：去控制字符、并空白、截断。"""
    if not isinstance(s, str):
        return ""
    return " ".join(CTRL_RE.sub("", s).split())[:cap]


def _ts_text(t):
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(t)))
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def read_json(path, cap=JSON_MAX):
    """读一个不可信的 JSON 文件：(值, 出错原因)。文件不存在时 (None, "")。"""
    try:
        if not os.path.exists(path):
            return None, ""
        if os.path.islink(path) or not os.path.isfile(path):
            return None, "不是普通文件"
        if os.path.getsize(path) > cap:
            return None, "文件太大（超过 %d KB）" % (cap // 1024)
        with open(path, "rb") as f:
            raw = f.read(cap + 1)
    except OSError as e:
        return None, "读不出来：%s" % e
    try:
        return json.loads(raw.decode("utf-8-sig")), ""
    except UnicodeDecodeError:
        return None, "不是 UTF-8 编码"
    except (ValueError, RecursionError) as e:
        return None, "不是合法的 JSON：%s" % str(e)[:120]


def _write_atomic(path, data, public=False):
    """先写临时名字再改名（同一个文件夹里，改名是原子的）。
    public：写到共享目录里的文件，别人要能读（临时文件默认只有自己能读，改成 0644）。"""
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=d)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        if public:
            _readable(tmp)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _readable(p):
    try:
        os.chmod(p, 0o644)
    except OSError:
        pass


def _json_bytes(v):
    return json.dumps(v, ensure_ascii=False, indent=2).encode("utf-8")


def safe_folder(name, fallback):
    """库名 → 共享目录里的文件夹名：去掉 Windows 不认的字符、首尾的点和空白、不许 ..。"""
    n = BAD_NAME_CHARS.sub("_", str(name or "")).strip(" .")[:60]
    if not n or n in (".", "..") or ".." in n:
        n = fallback
    return n


def _ok_folder(name):
    """共享目录里读到的文件夹名（技能、库）：不是隐藏的，不含路径分隔符和 ..。"""
    return (isinstance(name, str) and 0 < len(name) <= 80 and not name.startswith(".") and ".." not in name
            and "/" not in name and "\\" not in name and "\x00" not in name)


def _abs_ok(p):
    return isinstance(p, str) and "\x00" not in p and "\n" not in p and (
        os.path.isabs(p) or p.startswith("\\\\") or p.startswith("//"))


class _Gone(Exception):
    """检查到一半，共享目录读不到了。"""


class BadInput(ValueError):
    """共享目录里的内容不合格（只用在这个模块里，消息是给人看的原因）。"""


def _http_msg(e):
    return getattr(e, "msg", None) or str(e)


# ---------------- 5.3 下发的设置 ----------------

def _url_ok(v):
    return isinstance(v, str) and len(v) <= 500 and (v == "" or re.match(r"^https?://[^\s]+\Z", v.strip()) is not None)


ENDPOINT_KEYS = {"name": str, "base_url": str, "model": str, "api_key": str, "kind": str, "internal": bool,
                 "thinking": bool, "context_window": int, "max_model_len": int, "caps": dict}


def _check_endpoints(cfg, v):
    """端点列表。server.py 里端点的校验是写在 POST /api/settings 里面的（不是单独的函数），这里照着它查
    （带 base_url 的对象列表、context_window 4096 以上的整数、caps 用 server._caps_patch），另外只认已知的几个键、
    base_url 必须是 http(s) 地址。"""
    from . import server as srv
    if not isinstance(v, list) or not v or len(v) > 10:
        raise BadInput("endpoints 应该是 1 到 10 个端点的列表")
    out = []
    for i, e in enumerate(v):
        if not isinstance(e, dict) or not e.get("base_url"):
            raise BadInput("endpoints 应该是带 base_url 的对象列表")
        bad = [k for k in e if k not in ENDPOINT_KEYS]
        if bad:
            raise BadInput("第 %d 个端点里有不认识的项：%s" % (i + 1, "、".join(str(k) for k in bad)[:200]))
        ep = {}
        for k, val in e.items():
            want = ENDPOINT_KEYS[k]
            if val is None:
                continue
            if want is int:
                if isinstance(val, bool) or not isinstance(val, int) or not 4096 <= val <= 10_000_000:
                    raise BadInput("端点的 %s 应该是 4096 以上的整数" % k)
            elif want is dict:
                try:
                    val = srv._caps_patch(val)
                except srv.HTTPError as ex:
                    raise BadInput(_http_msg(ex))
            elif not isinstance(val, want):
                raise BadInput("端点的 %s 类型不对" % k)
            elif want is str and len(val) > 500:
                raise BadInput("端点的 %s 太长" % k)
            ep[k] = val
        if not _url_ok(ep["base_url"]) or not ep["base_url"].strip():
            raise BadInput("端点的 base_url 应该是 http:// 或 https:// 开头的地址")
        out.append(ep)
    return out


DICT_LEAVES = {
    "embedding": ("engine", "base_url", "model", "api_key", "dims", "batch", "query_prefix"),
    "rerank": ("engine", "base_url", "model", "api_key"),
    "asr": ("engine", "base_url", "model", "api_key", "language"),
    "web": ("enabled", "search_url"),
}
# 白名单（5.3）。值是"一项"的粒度：对象类的四项（embedding、rerank、asr、web）按子键叠加
WHITELIST = {("endpoints",), ("embedding",), ("rerank",), ("asr",), ("web",),
             ("browser", "allowed_domains"), ("browser", "blocked_domains"),
             ("kb", "top_k"), ("kb", "min_cover"), ("kb", "min_sim"), ("kb", "cards"),
             ("tools", "lazy"), ("ui", "theme"), ("approval_mode",)}
FORBIDDEN = {
    "mcp_servers": "不能由公司下发：里面是要在这台电脑上执行的命令",
    "default_folder": "不能由公司下发：默认文件夹只能在本机设置",
    "shared": "不能由公司下发：共享目录只能在本机设置",
    "model.caps": "不能由公司下发：模型能力只能写在本机的配置文件里",
}


def _leaf_name(path):
    return path[0] if path[0] in DICT_LEAVES else ".".join(path)


def _check_leaf(cfg, section, path, v):
    """校验一项下发的值：返回 [(路径元组, 值)]（对象类的按子键拆开），不合格抛 BadInput。"""
    from . import server as srv
    key = path[0]
    try:
        if path == ("endpoints",):
            return [(path, _check_endpoints(cfg, v))]
        if key in ("embedding", "rerank", "asr", "web"):
            if not isinstance(v, dict) or not v:
                raise BadInput("%s 应该是对象" % key)
            if key in ("embedding", "rerank"):
                out = srv._model_service_patch(cfg, key, v)
            elif key == "asr":
                out = srv._asr_patch(cfg, v)
            else:
                out = {}
                if "enabled" in v:
                    if not isinstance(v["enabled"], bool):
                        raise BadInput("web.enabled 应该是 true 或 false")
                    out["enabled"] = v["enabled"]
                if "search_url" in v:
                    if not _url_ok(v["search_url"]):
                        raise BadInput("web.search_url 应该是 http:// 或 https:// 开头的地址")
                    out["search_url"] = v["search_url"].strip()
            if "base_url" in out and not _url_ok(out["base_url"]):
                raise BadInput("%s.base_url 应该是 http:// 或 https:// 开头的地址" % key)
            for sk in ("api_key", "model", "query_prefix"):
                if isinstance(out.get(sk), str) and len(out[sk]) > 500:
                    raise BadInput("%s.%s 太长" % (key, sk))
            if isinstance(out.get("api_key"), str) and out["api_key"].startswith(srv.MASK):
                raise BadInput("%s.api_key 是打了码的值" % key)
            return [((key, sk), val) for sk, val in out.items()]
        if key == "browser":
            out = srv._browser_patch({path[1]: v})
            return [(path, out[path[1]])]
        if key == "kb":
            out = srv._kb_patch({path[1]: v})
            return [(path, out[path[1]])]
        if path == ("tools", "lazy"):
            fn = srv.SETTINGS_EXT.get("tools")
            if fn is not None:
                return [(path, fn(None, {"lazy": v})["lazy"])]
            if v not in ("auto", "on", "off"):
                raise BadInput("tools.lazy 只能是 auto、on 或 off")
            return [(path, v)]
        if path == ("ui", "theme"):
            if section == "locked":
                raise BadInput("ui.theme 只能放在 defaults 里（主题由各人自己选）")
            if not (isinstance(v, str) and srv.THEME_RE.match(v)):
                raise BadInput("主题名只能是 1 到 32 个小写字母、数字或短横线")
            return [(path, v)]
        if path == ("approval_mode",):
            if v == "full":
                raise BadInput("approval_mode 不能下发 full（那样只有删除才问），只能是 manual 或 auto")
            if v not in ("manual", "auto"):
                raise BadInput("approval_mode 只能是 manual 或 auto")
            return [(path, v)]
    except srv.HTTPError as e:
        raise BadInput(_http_msg(e))
    raise BadInput("不在可以下发的范围里")


def parse_overlay(cfg, raw):
    """settings.json 的内容 → (overlay, report, problem)。
    overlay = {"defaults": {路径: 值}, "locked": {路径: 值}}；report = {"locked": [键], "defaults": [键], "ignored": [{key, why}]}；
    problem 是整个文件的问题（空字符串表示没有）。同一项在 locked 和 defaults 里都有时按 locked。"""
    overlay = {"defaults": {}, "locked": {}}
    report = {"locked": [], "defaults": [], "ignored": []}
    if raw is None:
        return overlay, report, ""
    if not isinstance(raw, dict):
        return overlay, report, "settings.json 应该是一个对象：{\"defaults\": {…}, \"locked\": {…}}"
    seen = [0]

    def ignore(key, why):
        if len(report["ignored"]) < 100:
            report["ignored"].append({"key": str(key)[:100], "why": why})

    for top in raw:
        if top not in ("defaults", "locked"):
            ignore(top, "settings.json 只认 defaults 和 locked 两段")
    for section in ("locked", "defaults"):
        obj = raw.get(section)
        if obj is None:
            continue
        if not isinstance(obj, dict):
            ignore(section, "%s 应该是对象" % section)
            continue

        def walk(o, prefix):
            for k, v in o.items():
                seen[0] += 1
                if seen[0] > SETTINGS_KEYS_MAX:
                    if seen[0] == SETTINGS_KEYS_MAX + 1:
                        ignore("…", "键太多，后面的没有看（最多 %d 个）" % SETTINGS_KEYS_MAX)
                    return
                if not isinstance(k, str) or not k or len(k) > 100:
                    ignore(k, "键名不合格")
                    continue
                path = prefix + tuple(x for x in k.split(".") if x)
                name = ".".join(path)
                if not path:
                    ignore(k, "键名不合格")
                    continue
                if path[0] in FORBIDDEN or name in FORBIDDEN:
                    ignore(name, FORBIDDEN.get(name) or FORBIDDEN[path[0]])
                    continue
                if len(path) == 2 and path[0] in DICT_LEAVES:     # "embedding.base_url": "…" 这种写法
                    path, v = (path[0],), {path[1]: v}
                if path in WHITELIST:
                    vv = v
                    if path[0] in DICT_LEAVES and isinstance(v, dict):
                        allowed = DICT_LEAVES[path[0]]
                        for sk in [x for x in v if x not in allowed]:
                            ignore("%s.%s" % (path[0], sk), "不在可以下发的范围里")
                        vv = {sk: x for sk, x in v.items() if sk in allowed}
                    try:
                        pairs = _check_leaf(cfg, section, path, vv)
                    except BadInput as e:
                        ignore(_leaf_name(path), "值不合格：%s" % e)
                        continue
                    added = 0
                    for p, val in pairs:
                        if section == "defaults" and p in overlay["locked"]:
                            continue
                        overlay[section][p] = val
                        added += 1
                    n = _leaf_name(path)
                    if added and n not in report[section]:
                        report[section].append(n)
                elif isinstance(v, dict) and any(w[:len(path)] == path for w in WHITELIST):
                    walk(v, path)
                else:
                    ignore(name, "不在可以下发的范围里")
        walk(obj, ())
    return overlay, report, ""


# ---------------- 5.4 技能和模板 ----------------

def _copy_skill(src_dir, dst_dir):
    """复制一个共享技能：只复制普通文件（不跟符号链接、不要隐藏文件），最多两层、20 个、每个 256 KB。"""
    from .skills import parse
    md = os.path.join(src_dir, "SKILL.md")
    if os.path.islink(md) or not os.path.isfile(md):
        raise BadInput("没有 SKILL.md")
    if os.path.getsize(md) > SKILL_MD_MAX:
        raise BadInput("SKILL.md 太大（超过 %d KB）" % (SKILL_MD_MAX // 1024))
    with open(md, "rb") as f:
        raw = f.read(SKILL_MD_MAX + 1)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise BadInput("SKILL.md 不是 UTF-8 编码")
    meta, body = parse(text)
    name = meta.get("name") or os.path.basename(src_dir)
    if not _ok_folder(name) or len(name) > 64:
        raise BadInput("技能名不合格：%s" % name[:40])
    if not body.strip():
        raise BadInput("SKILL.md 没有正文")
    os.makedirs(dst_dir, exist_ok=True)
    with open(os.path.join(dst_dir, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write(CTRL_RE.sub("", text))
    n = 1
    for d, dirs, files in os.walk(src_dir):
        rel = os.path.relpath(d, src_dir)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        dirs[:] = sorted(x for x in dirs if _ok_folder(x) and not os.path.islink(os.path.join(d, x)) and depth < 2)
        for fn in sorted(files):
            if n >= SKILL_FILES_MAX:
                return name
            if (rel == "." and fn == "SKILL.md") or not _ok_folder(fn):
                continue
            sp = os.path.join(d, fn)
            if os.path.islink(sp) or not os.path.isfile(sp) or os.path.getsize(sp) > SKILL_FILE_MAX:
                continue
            tp = os.path.join(dst_dir, rel, fn) if rel != "." else os.path.join(dst_dir, fn)
            os.makedirs(os.path.dirname(tp), exist_ok=True)
            shutil.copyfile(sp, tp)
            n += 1
    return name


def clean_templates(raw):
    """templates.json → (合格的模板列表, 问题列表)。只留认识的键；不合格的一条跳过并说明。"""
    from . import modes
    from . import templates as tplm
    if raw is None:
        return [], []
    if not isinstance(raw, list):
        return [], ["templates.json 应该是一个列表"]
    out, problems = [], []
    for i, t in enumerate(raw[:TEMPLATES_MAX]):
        where = "templates.json 第 %d 条" % (i + 1)
        if not isinstance(t, dict):
            problems.append("%s：应该是对象" % where)
            continue
        title, prompt = t.get("title"), t.get("prompt")
        if not isinstance(title, str) or not title.strip() or len(title) > tplm.TITLE_MAX:
            problems.append("%s：title 应该是 1 到 %d 个字" % (where, tplm.TITLE_MAX))
            continue
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > tplm.PROMPT_MAX:
            problems.append("%s（%s）：prompt 应该是 1 到 %d 个字" % (where, title[:20], tplm.PROMPT_MAX))
            continue
        item = {"title": CTRL_RE.sub("", title.strip()), "prompt": CTRL_RE.sub("", prompt)}
        if isinstance(t.get("id"), str) and ID_RE.match(t["id"]):
            item["id"] = t["id"]
        if isinstance(t.get("role"), str):
            item["role"] = _clean(t["role"], tplm.ROLE_MAX)
        if t.get("mode") is not None:
            if not modes.valid(t.get("mode")):
                problems.append("%s（%s）：mode 不认识，两个模式都显示" % (where, title[:20]))
            else:
                item["mode"] = t["mode"]
        if t.get("params") is not None:
            try:
                item["params"] = tplm.norm_params(t["params"])
            except tplm.TemplateError as e:
                problems.append("%s（%s）：params 不合格（%s），改为从 prompt 里解析" % (where, title[:20], e))
            except Exception:
                problems.append("%s（%s）：params 不合格，改为从 prompt 里解析" % (where, title[:20]))
        out.append(item)
    if len(raw) > TEMPLATES_MAX:
        problems.append("templates.json 超过 %d 条，后面的没有用" % TEMPLATES_MAX)
    return out, problems


def _templates_loader(rt):
    sh = getattr(rt, "shared", None)
    return sh.templates() if sh is not None else []


def _register_templates():
    from . import templates as tplm
    tplm.register_source("shared", _templates_loader)


_register_templates()


# ---------------- 5.5 资料库索引包 ----------------

PACK_SCHEMA = """
CREATE TABLE info(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE docs(id TEXT PRIMARY KEY, path TEXT, title TEXT, kind TEXT, size INTEGER, mtime REAL, sha TEXT, tokens INTEGER,
  meta TEXT, family TEXT, version_label TEXT, effective REAL, outline TEXT, summary TEXT, keywords TEXT, summary_by TEXT,
  card_state TEXT, card_at REAL, lang TEXT, tags TEXT, added REAL);
CREATE TABLE sections(id INTEGER PRIMARY KEY, doc_id TEXT, seq INTEGER, loc TEXT, heading TEXT, text TEXT, tokens INTEGER);
CREATE TABLE parts(id INTEGER PRIMARY KEY, doc_id TEXT, seq INTEGER, parent INTEGER, loc TEXT, heading TEXT, text TEXT,
  tokens INTEGER);
CREATE TABLE vectors(part_id INTEGER PRIMARY KEY, dims INTEGER, vec BLOB);
"""
PACK_COLS = {
    "docs": ("id", "path", "title", "kind", "size", "mtime", "sha", "tokens", "meta", "family", "version_label", "effective",
             "outline", "summary", "keywords", "summary_by", "card_state", "card_at", "lang", "tags", "added"),
    "sections": ("id", "doc_id", "seq", "loc", "heading", "text", "tokens"),
    "parts": ("id", "doc_id", "seq", "parent", "loc", "heading", "text", "tokens"),
    "vectors": ("part_id", "dims", "vec"),
}


def _txt(v, cap, what):
    if v is None:
        return ""
    if not isinstance(v, str):
        raise BadInput("%s 应该是文字" % what)
    if len(v) > cap:
        raise BadInput("%s 太长（超过 %d 字）" % (what, cap))
    return v.replace("\x00", "")


def _int(v, what, lo=0, hi=2 ** 62, default=0):
    if v is None:
        return default
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise BadInput("%s 应该是 %d 到 %d 的整数" % (what, lo, hi))
    return v


def _num(v, what):
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or abs(v) > 1e13:
        raise BadInput("%s 应该是数" % what)
    return float(v)


def _rel_path(v):
    """文档相对 source_root 的路径：不能是绝对路径、不能有 ..、不能带盘符。统一成 / 分隔。"""
    p = _txt(v, PATH_MAX, "文档路径").replace("\\", "/")
    segs = [x for x in p.split("/") if x not in ("", ".")]
    # 带冒号的一律不要：第一段是盘符（C:），后面的段在 Windows 上是 NTFS 的备用数据流（a.docx:x）
    if not segs or p.startswith("/") or any(x == ".." or ":" in x for x in segs):
        raise BadInput("文档路径不合格：%s" % p[:80])
    return "/".join(segs)


def _json_col(v, cap, kind, default):
    if v is None or v == "":
        return default
    if not isinstance(v, str) or len(v) > cap:
        return default
    try:
        x = json.loads(v)
    except (ValueError, RecursionError):
        return default
    return x if isinstance(x, kind) else default


def clean_manifest(raw):
    """manifest.json → 干净的 dict；不合格抛 BadInput。"""
    if not isinstance(raw, dict):
        raise BadInput("manifest.json 应该是一个对象")
    if raw.get("format") != FORMAT:
        raise BadInput("manifest.json 的 format 应该是 %d（这个包是别的版本的程序做的）" % FORMAT)
    lid = raw.get("id")
    if not isinstance(lid, str) or not ID_RE.match(lid):
        raise BadInput("manifest.json 的 id 不合格（只能是 1 到 64 个字母、数字、下划线、短横线）")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise BadInput("manifest.json 缺 name")
    from . import kb as kbm
    try:
        name = kbm.clean_name(CTRL_RE.sub("", name))
    except ValueError as e:
        raise BadInput("manifest.json 的 name 不合格：%s" % e)
    out = {"format": FORMAT, "id": lid, "name": name, "note": _clean(raw.get("note") if isinstance(raw.get("note"), str) else "", 500),
           "version": _int(raw.get("version"), "manifest.json 的 version", 1, 10 ** 9, default=None)}
    if out["version"] is None:
        raise BadInput("manifest.json 缺 version")
    out["built"] = _num(raw.get("built"), "manifest.json 的 built") or 0.0
    out["docs"] = _int(raw.get("docs"), "manifest.json 的 docs", 0, DOCS_MAX)
    out["parts"] = _int(raw.get("parts"), "manifest.json 的 parts", 0, PARTS_MAX)
    emb = raw.get("embedding")
    if emb is None:
        out["embedding"] = None
    elif isinstance(emb, dict) and isinstance(emb.get("model"), str) and len(emb["model"]) <= 200:
        out["embedding"] = {"model": emb["model"], "dims": _int(emb.get("dims"), "embedding.dims", 0, VEC_DIMS_MAX),
                            "text_version": _int(emb.get("text_version"), "embedding.text_version", 0, 1000),
                            "vec_dims": _int(emb.get("vec_dims"), "embedding.vec_dims", 0, VEC_DIMS_MAX)}
    else:
        raise BadInput("manifest.json 的 embedding 应该是 {model, dims, text_version} 或 null")
    sr = raw.get("source_root")
    out["source_root"] = sr if isinstance(sr, str) and len(sr) <= PATH_MAX and "\x00" not in sr and "\n" not in sr else ""
    sha = raw.get("sha256")
    out["sha256"] = sha if isinstance(sha, str) and HEX_RE.match(sha) else ""
    return out


def local_vec_sig(cfg):
    """本机现在的向量签名（和 manifest 的 embedding 比）：(model, dims, text_version) 或 None（没配向量模型）。"""
    from .kb import embed as emb
    if not emb.configured(cfg):
        return None
    c = emb.conf(cfg)
    return (c["model"], c["dims"], emb.TEXT_VERSION)


def _vec_match(cfg, man):
    """(能不能用包里的向量, 原因)。"""
    from .kb import vectors as vmod
    e = man.get("embedding")
    if not e:
        return False, "包里没有向量"
    mine = local_vec_sig(cfg)
    if mine is None:
        return False, "本机没有配向量模型"
    if (e["model"], e["dims"], e["text_version"]) != mine:
        return False, "包里的向量是用 %s（长度 %s，文字版本 %s）算的，和本机（%s，%s，%s）不一样" % (
            e["model"], e["dims"] or "默认", e["text_version"], mine[0], mine[1] or "默认", mine[2])
    if vmod.engine_name(cfg) != "builtin":
        return False, "本机的向量存在 LangChain 的向量库里，包里的向量导不进去"
    return True, ""


class SharedService:
    """运行时上的一份：共享目录的状态、下发设置的叠加、技能和模板的缓存、索引包的导入导出、后台定时检查。"""

    def __init__(self, rt):
        self.rt = rt
        self.lock = threading.Lock()            # 同一时间只做一次检查（接口和后台线程）
        self.stop_ev = threading.Event()
        self.wake = threading.Event()
        self.thread = None
        self.report = {"locked": [], "defaults": [], "ignored": []}
        self._templates = []
        self.status = {}          # 上一次检查完的状态（GET /api/shared 读它）
        self._cur = None          # 正在做的那次检查的状态（做完换上去，免得别人读到做了一半的）
        self._load_cache()

    # ---- 路径 ----
    @property
    def cfg(self):
        return self.rt.cfg

    @property
    def store(self):
        return self.rt.store

    def dir(self):
        d = self.cfg.local.get("shared", {}).get("dir") if isinstance(self.cfg.local.get("shared"), dict) else ""
        return d.strip() if isinstance(d, str) else ""

    def minutes(self):
        v = (self.cfg.local.get("shared") or {}).get("sync_minutes") if isinstance(self.cfg.local.get("shared"), dict) else 30
        return v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 1440 else 30

    def cache_root(self, d=None):
        d = self.dir() if d is None else d
        h = hashlib.sha1(d.encode("utf-8")).hexdigest()[:12] if d else "none"
        return os.path.join(self.cfg.home, "shared_cache", h)

    def skills_dir(self):
        return os.path.join(self.cache_root(), "skills")

    def templates(self):
        return [dict(t) for t in self._templates] if self.dir() else []

    # ---- 缓存 ----
    def _load_cache(self):
        """启动时（不碰共享盘）：用本机缓存把下发的设置叠上、读出模板和上次的状态。"""
        d = self.dir()
        self.status = self._blank(d)
        if not d:
            self.cfg.set_overlay(None)
            return
        root = self.cache_root(d)
        st, _ = read_json(os.path.join(root, "state.json"), 4 * JSON_MAX)
        if isinstance(st, dict) and st.get("dir") == d:
            for k in ("checked", "synced", "name", "contact", "writable", "manifests", "problems"):
                if k in st:
                    self.status[k] = st[k]
        raw, _ = read_json(os.path.join(root, "settings.json"))
        try:
            self._apply_settings(raw, from_cache=True)
        except Exception:
            traceback.print_exc()
        tl, _ = read_json(os.path.join(root, "templates.json"))
        self._templates, _ = clean_templates(tl)
        self._refresh_status_counts()

    def _blank(self, d):
        return {"dir": d, "configured": bool(d), "ok": False, "error": "", "name": "", "contact": "", "writable": False,
                "checked": None, "synced": None, "skills": 0, "templates": 0,
                "settings": {"locked": [], "defaults": [], "ignored": []}, "libraries": [], "notes": [], "problems": [],
                "manifests": {}, "checking": False}

    def _save_state(self):
        st = self.status
        keep = {k: st.get(k) for k in ("dir", "checked", "synced", "name", "contact", "writable", "manifests", "problems")}
        try:
            _write_atomic(os.path.join(self.cache_root(st["dir"]), "state.json"), _json_bytes(keep))
        except OSError:
            pass

    # ---- 下发的设置 ----
    def _apply_settings(self, raw, from_cache=False):
        from .kb import embed as emb
        cfg = self.cfg
        old_sig = emb.signature(cfg)
        old_ep = (cfg.get("endpoints"), cfg.get("model"))
        overlay, report, problem = parse_overlay(cfg, raw)
        overlay = {k: {p: v for p, v in d.items()} for k, d in overlay.items()}
        cfg.set_overlay(overlay if (overlay["defaults"] or overlay["locked"]) else None)
        self.report = report
        if problem:
            self._problem("settings.json", problem)
        if from_cache:
            return
        try:
            if emb.signature(cfg) != old_sig:
                from . import kb as kbm
                svc = kbm.get(self.rt)
                svc.embed_alive = None
                svc.indexer.enqueue_all()             # 换了向量模型：旧向量作废，后台重算（和本机改设置一样）
            if (cfg.get("endpoints"), cfg.get("model")) != old_ep and getattr(self.rt, "caps", None) is not None:
                self.rt.caps.reprobe_async()
        except Exception:
            pass

    def locked_names(self):
        return list(self.report.get("locked") or []) if self.cfg.overlay else []

    def _problem(self, file, why):
        st = self._cur if self._cur is not None else self.status
        ps = st.setdefault("problems", [])
        if len(ps) < 50:
            ps.append({"file": file, "why": str(why)[:300]})

    # ---- 状态 ----
    def _refresh_status_counts(self, st=None):
        st = self.status if st is None else st
        st["templates"] = len(self.templates())
        sd = self.skills_dir()
        n = 0
        if st.get("configured") and os.path.isdir(sd):
            n = sum(1 for x in os.listdir(sd) if os.path.isfile(os.path.join(sd, x, "SKILL.md")))
        st["skills"] = n
        st["settings"] = {"locked": list(self.report.get("locked") or []), "defaults": list(self.report.get("defaults") or []),
                          "ignored": list(self.report.get("ignored") or [])}
        st["libraries"] = self._libs_status(st)
        notes = []
        if st.get("configured") and not st.get("ok") and st.get("error"):
            if st.get("synced"):
                notes.append("连不上共享目录，现在用的是本机上次同步的内容（%s）。" % _ts_text(st["synced"]))
            else:
                notes.append("连不上共享目录，也还没有同步过，现在一切照本机的设置。")
        if st.get("writable"):
            notes.append(WRITABLE_NOTE)
        for p in st.get("problems") or []:
            notes.append("%s：%s" % (p.get("file"), p.get("why")))
        st["notes"] = notes[:60]

    def _vec_why(self, lid, info):
        """导入过的共享库：向量现在是什么情况（按本机现在的设置算，不用导入那时记下的原因）。"""
        if info.get("vectors") == "ok":
            return ""
        if not info.get("embedding"):
            why = "包里没有向量"
        else:
            usable, why = _vec_match(self.cfg, {"embedding": info.get("embedding")})
            if usable:
                return "本机的向量模型和包里的一样了，下次同步时换上包里的向量"
        from .kb import embed as emb
        if emb.configured(self.cfg):
            n = self.store.one("SELECT COUNT(*) AS n FROM kb_vectors WHERE library_id=?", (lid,)) or {}
            p = self.store.one("SELECT COUNT(*) AS n FROM kb_parts WHERE library_id=?", (lid,)) or {}
            why += "；本机在用自己的向量模型算（%d / %d）" % (int(n.get("n") or 0), int(p.get("n") or 0))
        else:
            why += "；只用关键词检索"
        return why

    def _libs_status(self, st=None):
        """[{id, name, version, local_version, state, docs, parts, vectors, built, error}]：共享目录里有的 + 本机导入过的。"""
        st = self.status if st is None else st
        mans = st.get("manifests") or {}
        local = {r["id"]: r for r in self.store.q("SELECT * FROM kb_libraries WHERE COALESCE(shared,0)=1 ORDER BY created")}
        out = []
        for lid, m in mans.items():
            row = local.get(lid)
            info = _json_col((row or {}).get("shared_info"), 4096, dict, {})
            lv = int((row or {}).get("version") or 0)
            state = m.get("state") or ("ok" if row and lv >= int(m.get("version") or 0) else "outdated")
            if m.get("error"):
                state = "error"
            if m.get("own"):            # 本机自己的库导出的包：本机的版本就是它上次导出的版本
                info = {"vectors": "ok" if m.get("embedding") else "none"}
                own = self.store.one("SELECT version FROM kb_libraries WHERE id=?", (lid,)) or {}
                lv = int(own.get("version") or 0)
                state = "ok" if lv >= int(m.get("version") or 0) else "outdated"
            out.append({"id": lid, "name": m.get("name") or (row or {}).get("name") or lid, "version": m.get("version"),
                        "local_version": lv, "state": state, "docs": m.get("docs", 0), "parts": m.get("parts", 0),
                        "vectors": info.get("vectors") if (row or m.get("own")) else ("none" if not m.get("embedding") else "mismatch"),
                        "vectors_why": self._vec_why(lid, info) if row else "", "built": m.get("built"),
                        "error": m.get("error") or "", "enabled": bool(row.get("enabled", 1)) if row else True})
        for lid, row in local.items():
            if lid in mans:
                continue
            info = _json_col(row.get("shared_info"), 4096, dict, {})
            n = self.store.one("SELECT COUNT(*) AS n FROM kb_docs WHERE library_id=?", (lid,)) or {}
            p = self.store.one("SELECT COUNT(*) AS n FROM kb_parts WHERE library_id=?", (lid,)) or {}
            gone = bool(st.get("ok"))
            out.append({"id": lid, "name": row["name"], "version": int(row.get("version") or 0),
                        "local_version": int(row.get("version") or 0), "state": "error" if gone else "ok",
                        "docs": int(n.get("n") or 0), "parts": int(p.get("n") or 0), "vectors": info.get("vectors", "none"),
                        "vectors_why": self._vec_why(lid, info), "built": row.get("built"),
                        "error": "共享目录里已经没有这个库了，本机留着上次同步的内容，可以删掉。" if gone else "",
                        "enabled": bool(row.get("enabled", 1))})
        return out

    def public(self):
        """GET /api/shared 的返回。"""
        if self.status.get("configured"):
            self._refresh_status_counts()
        st = copy.deepcopy(self.status)
        cur = self._cur
        if cur is not None and cur.get("libs_phase"):
            st["libraries"] = self._libs_status(cur)       # 正在同步资料库：显示哪个在 updating
        elif not st.get("configured"):
            st["libraries"] = self._libs_status()
        st["checking"] = cur is not None
        out = {k: st.get(k) for k in ("dir", "configured", "ok", "error", "name", "contact", "writable", "checked", "synced",
                                       "skills", "templates", "settings", "libraries", "notes", "problems")}
        out["checking"] = bool(st.get("checking"))
        out["sync_minutes"] = self.minutes()
        return out

    def orphan(self, lid):
        """这个共享库是不是已经不在共享目录里了（共享目录没配，或者连上了而且里面没有它）：这时允许删。"""
        if not self.dir():
            return True
        return bool(self.status.get("ok")) and lid not in (self.status.get("manifests") or {})

    # ---- 后台线程 ----
    def start(self):
        if not self.dir() or self.stop_ev.is_set() or getattr(self.rt, "closing", False):
            return
        if self.thread is not None and self.thread.is_alive():
            self.wake.set()
            return
        self.thread = threading.Thread(target=self._run, name="shared-sync", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_ev.set()
        self.wake.set()
        t = self.thread
        if t is not None and t is not threading.current_thread():
            t.join(5)

    def _stopped(self):
        return self.stop_ev.is_set() or getattr(self.rt, "closing", False)

    def _run(self):
        while not self._stopped():
            if not self.dir():
                return
            try:
                self.check()
            except Exception:
                if not self._stopped():
                    traceback.print_exc()
            m = self.minutes()
            self.wake.wait(m * 60 if m > 0 else None)
            self.wake.clear()

    def settings_changed(self):
        """shared.dir 或 sync_minutes 改了（SETTINGS_AFTER）：换缓存、叠加的设置、技能目录，后台重新看一遍。"""
        with self.lock:
            if self.status.get("dir") != self.dir():       # 换了目录（或清空）：换成那个目录的缓存
                self._load_cache()
                self._set_skills_dir()
        if self.dir():
            self.start()
        self.wake.set()             # 清空了目录：后台线程醒来看到没配就退出

    def _set_skills_dir(self):
        sk = getattr(self.rt, "skills", None)
        if sk is None:
            return
        want = self.skills_dir()
        dirs = [x for x in sk.dirs if x[1] != "shared"]
        pos = 1 if dirs and dirs[0][1] == "builtin" else 0
        dirs.insert(pos, (want, "shared"))
        sk.dirs = dirs
        try:
            sk.reload()
        except Exception:
            traceback.print_exc()

    # ---- 检查一遍 ----
    def check(self, timeout=None):
        """去共享目录里看一遍：公司信息、下发的设置、技能、模板、资料库。返回 public()。
        timeout：等正在做的那次检查最多等几秒（接口用）；等不到就直接返回现在的状态。"""
        got = self.lock.acquire(timeout=timeout) if timeout is not None else self.lock.acquire()
        if not got:
            return self.public()
        try:
            self._check_locked()
        finally:
            self.lock.release()
        return self.public()

    def _check_locked(self):
        d = self.dir()
        if not d:
            self._load_cache()
            self._set_skills_dir()
            return
        prev = self.status if self.status.get("dir") == d else self._blank(d)
        st = self._blank(d)
        for k in ("synced", "name", "contact", "manifests"):
            st[k] = prev.get(k)
        st["checked"] = int(now())
        self._cur = st
        try:
            self._check_into(d, st, prev)
        finally:
            self._cur = None
            if st.get("done"):
                st.pop("done", None)
                st.pop("libs_phase", None)
                self.status = st
            else:
                self.status["checked"] = st["checked"]

    def _check_into(self, d, st, prev):
        try:
            self._check_reachable(d, st, prev)
        except _Gone:
            # 看到一半共享目录没了：已经读到的不算数（"文件不在"不能当成"公司把它删了"），按读不到处理
            st2 = self._blank(d)
            for k in ("synced", "name", "contact", "manifests", "checked"):
                st2[k] = st.get(k) if k == "checked" else prev.get(k)
            st.clear()
            st.update(st2)
            self._unreachable(d, st, prev)

    def _still(self, d):
        """读到"某个文件不在"时先确认共享目录本身还在：不在了抛 _Gone。"""
        try:
            ok = os.path.isdir(d)
        except OSError:
            ok = False
        if not ok:
            raise _Gone()

    def _unreachable(self, d, st, prev):
        st["ok"] = False
        st["error"] = "读不到共享目录：%s（不存在、没连上共享盘，或者没有权限）" % d
        st["problems"] = prev.get("problems") or []
        st["writable"] = False
        self._refresh_status_counts(st)
        st["done"] = True
        self.status = st
        self._save_state()

    def _check_reachable(self, d, st, prev):
        root = self.cache_root(d)
        try:
            reachable = os.path.isdir(d)
        except OSError:
            reachable = False
        if not reachable:
            self._unreachable(d, st, prev)
            return
        # companyagent.json
        info, why = read_json(os.path.join(d, "companyagent.json"))
        if why:
            self._problem("companyagent.json", why)
        elif info is None:
            self._still(d)
            self._problem("companyagent.json", "没有这个文件，这个文件夹可能不是公司共享目录")
        elif not isinstance(info, dict):
            self._problem("companyagent.json", "应该是一个对象")
        else:
            st["name"] = _clean(info.get("name"), 60)
            st["contact"] = _clean(info.get("contact"), 120)
            if info.get("version") not in (None, 1):
                self._problem("companyagent.json", "version 是 %s，这个程序认的是 1，按 1 读" % str(info.get("version"))[:20])
        # settings.json：读得出、格式对就换上并存进缓存；读不出时沿用缓存里上次的
        raw, why = read_json(os.path.join(d, "settings.json"))
        if raw is None and not why:
            self._still(d)
        if why:
            self._problem("settings.json", why + "；沿用上次同步的设置")
            raw, _ = read_json(os.path.join(root, "settings.json"))
            self._apply_settings(raw)
        else:
            self._apply_settings(raw)
            try:
                _write_atomic(os.path.join(root, "settings.json"), _json_bytes(raw))
            except OSError:
                pass
        if self._stopped():
            return
        self._sync_skills(d, root)
        self._sync_templates(d, root)
        st["writable"] = self._writable(d)
        if self._stopped():
            return
        self._sync_libraries(d, st)
        if self._stopped():
            return
        st["ok"] = True
        st["error"] = ""
        st["synced"] = int(now())
        self._refresh_status_counts(st)
        st["done"] = True
        self.status = st
        self._save_state()

    def _writable(self, d):
        """当前用户能不能往共享目录里写：先看 os.access，再真建一个空的探测文件（建了马上删掉自己建的这个）。"""
        try:
            if not os.access(d, os.W_OK):
                return False
            fd, p = tempfile.mkstemp(prefix=".companyagent-write-test-", dir=d)
        except OSError:
            return False
        try:
            os.close(fd)
            os.remove(p)
        except OSError:
            pass
        return True

    def _sync_skills(self, d, root):
        src = os.path.join(d, "skills")
        dst = os.path.join(root, "skills")
        if not os.path.isdir(src):
            self._still(d)
            if os.path.exists(src):
                self._problem("skills", "不是文件夹")
            else:
                self._swap_dir(root, dst, None)
            self._set_skills_dir()
            return
        os.makedirs(root, exist_ok=True)
        new = tempfile.mkdtemp(prefix="skills.new-", dir=root)
        builtin = set()
        try:
            from .skills import Skills
            builtin = set(Skills([(os.path.join(self.rt.app_dir, "skills"), "builtin")]).names())
        except Exception:
            pass
        n = 0
        try:
            names = sorted(os.listdir(src))
        except OSError as e:
            self._problem("skills", "读不出来：%s；沿用上次同步的技能" % e)
            shutil.rmtree(new, ignore_errors=True)
            self._set_skills_dir()
            return
        for name in names:
            sp = os.path.join(src, name)
            if not _ok_folder(name) or os.path.islink(sp) or not os.path.isdir(sp):
                continue
            if n >= SKILLS_MAX:
                self._problem("skills", "技能超过 %d 个，后面的没有用" % SKILLS_MAX)
                break
            try:
                key = _copy_skill(sp, os.path.join(new, name))
                n += 1
                if key in builtin:
                    self._problem("skills/%s" % name, "和内置技能「%s」重名，用的是内置的" % key)
            except (BadInput, OSError, UnicodeError) as e:
                shutil.rmtree(os.path.join(new, name), ignore_errors=True)
                self._problem("skills/%s" % name, str(e))
        # 复制到一半共享目录没了：复制失败是因为读不到，不能拿这份不全的换掉上次同步的技能
        try:
            self._still(d)
        except _Gone:
            shutil.rmtree(new, ignore_errors=True)
            raise
        self._swap_dir(root, dst, new)
        self._set_skills_dir()

    def _swap_dir(self, root, dst, new):
        """把新复制好的技能文件夹换上（旧的是本程序自己的缓存，换下来就删掉）。new 为 None 表示共享目录里没有技能。"""
        old = None
        if os.path.isdir(dst):
            old = dst + ".old-%d" % int(time.time() * 1000)
            try:
                os.replace(dst, old)
            except OSError:
                old = None
        if new is not None:
            try:
                os.replace(new, dst)
            except OSError:
                shutil.rmtree(new, ignore_errors=True)
        if old:
            shutil.rmtree(old, ignore_errors=True)

    def _sync_templates(self, d, root):
        raw, why = read_json(os.path.join(d, "templates.json"))
        if raw is None and not why:
            self._still(d)
        if why:
            self._problem("templates.json", why + "；沿用上次同步的模板")
            return
        items, problems = clean_templates(raw)
        for p in problems:
            self._problem("templates.json", p)
        self._templates = items
        try:
            _write_atomic(os.path.join(root, "templates.json"), _json_bytes(items))
        except OSError:
            pass

    # ---- 资料库：看 manifest，有新版本就导入 ----
    def _sync_libraries(self, d, st):
        base = os.path.join(d, "libraries")
        old = st.get("manifests") or {}
        mans = {}
        st["manifests"] = mans
        st["libs_phase"] = True
        if not os.path.isdir(base):
            self._still(d)
            return
        try:
            names = sorted(os.listdir(base))
        except OSError as e:
            self._problem("libraries", "读不出来：%s" % e)
            st["manifests"] = old
            return
        count = 0
        for folder in names:
            if self._stopped():
                return
            fp = os.path.join(base, folder)
            if not _ok_folder(folder) or os.path.islink(fp) or not os.path.isdir(fp):
                continue
            count += 1
            if count > LIBS_MAX:
                self._problem("libraries", "资料库超过 %d 个，后面的没有看" % LIBS_MAX)
                break
            where = "libraries/%s/manifest.json" % folder
            raw, why = read_json(os.path.join(fp, "manifest.json"), MANIFEST_MAX)
            if raw is None:
                self._problem(where, why or "没有 manifest.json（可能还在导出）")
                continue
            try:
                man = clean_manifest(raw)
            except BadInput as e:
                self._problem(where, str(e))
                continue
            if man["id"] in mans:
                self._problem(where, "和 libraries/%s 的 id 重复，跳过" % mans[man["id"]]["folder"])
                continue
            ent = {"folder": folder, "name": man["name"], "version": man["version"], "docs": man["docs"],
                   "parts": man["parts"], "built": man["built"], "embedding": man["embedding"], "error": "", "state": ""}
            mans[man["id"]] = ent
            mine = self.store.one("SELECT * FROM kb_libraries WHERE id=?", (man["id"],))
            if mine and not mine.get("shared"):
                ent["state"] = "ok"            # 本机自己的库导出的包（管资料的人自己的电脑）：不再导入一份
                ent["own"] = True
                continue
            need = not mine or int(mine.get("version") or 0) < man["version"]
            if mine and not need:
                info = _json_col(mine.get("shared_info"), 4096, dict, {})
                usable, _ = _vec_match(self.cfg, man)
                if man["embedding"] and usable and info.get("vectors") != "ok":
                    need = True               # 本机的向量模型现在和包里的一样了：重新导入，用上包里的向量
            if not need:
                continue
            ent["state"] = "updating"
            try:
                self.import_package(fp, man)
                ent["state"] = ""
            except BadInput as e:
                ent["error"] = "没有导入：%s" % e
                self._problem("libraries/%s" % folder, ent["error"])
            except Exception as e:
                ent["error"] = "没有导入：%s: %s" % (type(e).__name__, str(e)[:200])
                self._problem("libraries/%s" % folder, ent["error"])
                if not self._stopped():
                    traceback.print_exc()

    def _copy_in(self, src, dst, want_sha):
        """把 index.db 复制到本机（分块，边复制边算 sha256，超过上限就停）。"""
        if os.path.islink(src) or not os.path.isfile(src):
            raise BadInput("没有 index.db")
        if os.path.getsize(src) > DB_MAX:
            raise BadInput("index.db 太大（超过 %d MB）" % (DB_MAX // 1024 // 1024))
        h = hashlib.sha256()
        n = 0
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(src, "rb") as fi, open(dst, "wb") as fo:
            while True:
                if self._stopped():
                    raise BadInput("程序正在关闭")
                b = fi.read(COPY_CHUNK)
                if not b:
                    break
                n += len(b)
                if n > DB_MAX:
                    raise BadInput("index.db 太大")
                h.update(b)
                fo.write(b)
        if want_sha and h.hexdigest() != want_sha:
            raise BadInput("index.db 和 manifest.json 对不上（可能正在导出新版本），下次再试")
        with open(dst, "rb") as f:
            if f.read(16) != b"SQLite format 3\x00":
                raise BadInput("index.db 不是 SQLite 文件")

    def import_package(self, folder, man):
        """把一个索引包导进本机的账本（5.5）。先复制到本机、只读打开、全部校验一遍，再在一个事务里先删旧行、插新行。"""
        tmpdir = self._tmp_root()
        local = os.path.join(tmpdir, "%s-%d.db" % (man["id"], int(time.time() * 1000)))
        try:
            self._copy_in(os.path.join(folder, "index.db"), local, man.get("sha256"))
            con = sqlite3.connect("file:%s?mode=ro&immutable=1" % quote(os.path.abspath(local)), uri=True)
            try:
                for pragma in ("PRAGMA trusted_schema=OFF", "PRAGMA query_only=ON"):
                    try:
                        con.execute(pragma)
                    except sqlite3.DatabaseError:
                        pass
                ok = con.execute("PRAGMA quick_check").fetchone()
                if not ok or ok[0] != "ok":
                    raise BadInput("index.db 损坏了")
                pack = self._read_pack(con, man)
            except sqlite3.DatabaseError as e:
                raise BadInput("index.db 读不出来：%s" % e)
            finally:
                con.close()
            self._write_pack(man, pack)
        finally:
            try:
                os.remove(local)          # 本机临时复制的那份（本程序自己建的）
            except OSError:
                pass

    def _read_pack(self, con, man):
        """校验并读出包里的行（不改本机）。不合格抛 BadInput，说明是哪张表、哪一项。"""
        tables = {r[0]: r[1] for r in con.execute("SELECT name, type FROM sqlite_master")}
        for t in ("docs", "sections", "parts"):
            if tables.get(t) != "table":
                raise BadInput("index.db 里没有 %s 表" % t)
        has_vec = tables.get("vectors") == "table"
        for t, cols in PACK_COLS.items():
            if t == "vectors" and not has_vec:
                continue
            have = {r[1] for r in con.execute("PRAGMA table_info(%s)" % t)}
            miss = [c for c in cols if c not in have]
            if miss:
                raise BadInput("index.db 的 %s 表缺列：%s" % (t, "、".join(miss)))

        def rows(t, order, cap):
            cur = con.execute("SELECT %s FROM %s ORDER BY %s LIMIT %d" % (",".join(PACK_COLS[t]), t, order, cap + 1))
            n = 0
            while True:
                if self._stopped():
                    raise BadInput("程序正在关闭")
                got = cur.fetchmany(BATCH)
                if not got:
                    return
                for r in got:
                    n += 1
                    if n > cap:
                        raise BadInput("index.db 的 %s 表超过 %d 行" % (t, cap))
                    yield dict(zip(PACK_COLS[t], r))

        from .kb import cards
        docs, ids = [], set()
        for r in rows("docs", "id", DOCS_MAX):
            did = r["id"]
            if not isinstance(did, str) or not ID_RE.match(did) or did in ids:
                raise BadInput("docs 表里有不合格或重复的文档 id")
            ids.add(did)
            title = _txt(r["title"], TITLE_MAX, "文档标题").strip()
            if not title or "/" in title or "\\" in title:
                raise BadInput("文档 %s 的标题不合格" % did)
            tags = _json_col(r["tags"], 4096, list, [])
            try:
                tags = cards.clean_tags([t for t in tags if isinstance(t, str)][:10])
            except ValueError:
                tags = []
            kws = [CTRL_RE.sub("", k)[:20] for k in _json_col(r["keywords"], 4096, list, []) if isinstance(k, str) and k.strip()][:6]
            outline = []
            for it in _json_col(r["outline"], 65536, list, [])[:60]:
                if isinstance(it, dict) and isinstance(it.get("title"), str):
                    lv = it.get("level")
                    outline.append({"level": lv if isinstance(lv, int) and not isinstance(lv, bool) and 1 <= lv <= 9 else 1,
                                    "title": CTRL_RE.sub("", it["title"])[:200],
                                    "loc": CTRL_RE.sub("", it.get("loc"))[:200] if isinstance(it.get("loc"), str) else "",
                                    "section_id": it.get("section_id") if isinstance(it.get("section_id"), int) else None})
            by = r["summary_by"] if r["summary_by"] in ("model", "extract", "user") else ""
            summary = cards.clean_summary(_txt(r["summary"], 2000, "摘要"))
            meta = _json_col(r["meta"], 8192, dict, {})
            docs.append({
                "id": did, "path": _rel_path(r["path"]), "title": title,
                "kind": _txt(r["kind"], 40, "文档类型"), "size": _int(r["size"], "文档大小"),
                "mtime": _num(r["mtime"], "修改时间"), "sha": _txt(r["sha"], 128, "sha"), "tokens": _int(r["tokens"], "tokens"),
                "meta": json.dumps(meta, ensure_ascii=False), "family": _txt(r["family"], TITLE_MAX, "family"),
                "version_label": _txt(r["version_label"], 100, "version_label"), "effective": _num(r["effective"], "effective"),
                "outline": outline, "summary": summary, "keywords": kws, "summary_by": by if summary else "",
                # 共享库不在本机写摘要：包里还没写的（outline）也算定了，不然界面上一直显示"后台在写"
                "card_state": "failed" if r["card_state"] == "failed" else "ready",
                "card_at": _num(r["card_at"], "card_at"), "lang": r["lang"] if r["lang"] in ("zh", "en", "mixed") else "",
                "tags": tags, "added": _num(r["added"], "added") or now()})
        if ids:
            clash = []
            lst = sorted(ids)
            for i in range(0, len(lst), BATCH):
                chunk = lst[i:i + BATCH]
                clash += [x["id"] for x in self.store.q("SELECT id FROM kb_docs WHERE library_id<>? AND id IN (%s)"
                                                        % ",".join("?" * len(chunk)), tuple([man["id"]] + chunk))]
            if clash:
                raise BadInput("有 %d 份文档的 id 和本机别的资料库里的重复（例如 %s）" % (len(clash), clash[0]))
        sections, sec_ids = [], set()
        for r in rows("sections", "doc_id, seq, id", SECTIONS_MAX):
            sid = _int(r["id"], "节的 id", 1)
            if r["doc_id"] not in ids or sid in sec_ids:
                raise BadInput("sections 表里有对不上文档或重复的行")
            sec_ids.add(sid)
            sections.append({"id": sid, "doc_id": r["doc_id"], "seq": _int(r["seq"], "节的序号"),
                             "loc": _txt(r["loc"], LOC_MAX, "节的位置"), "heading": _txt(r["heading"], HEADING_MAX, "节的标题"),
                             "text": _txt(r["text"], SECTION_TEXT_MAX, "节的正文"), "tokens": _int(r["tokens"], "节的 tokens")})
        parts, part_ids = [], set()
        for r in rows("parts", "doc_id, seq, id", PARTS_MAX):
            pid = _int(r["id"], "片段的 id", 1)
            parent = r["parent"]
            if r["doc_id"] not in ids or pid in part_ids or (parent is not None and parent not in sec_ids):
                raise BadInput("parts 表里有对不上文档、节或重复的行")
            part_ids.add(pid)
            text = _txt(r["text"], PART_TEXT_MAX, "片段正文")
            parts.append({"id": pid, "doc_id": r["doc_id"], "seq": _int(r["seq"], "片段的序号"), "parent": parent,
                          "loc": _txt(r["loc"], LOC_MAX, "片段的位置"), "heading": _txt(r["heading"], HEADING_MAX, "片段的标题"),
                          "text": text, "tokens": _int(r["tokens"], "片段的 tokens")})
        vectors = []
        usable, why = _vec_match(self.cfg, man)
        if has_vec and usable:
            for r in rows("vectors", "part_id", PARTS_MAX):
                dims = _int(r["dims"], "向量长度", 1, VEC_DIMS_MAX)
                if r["part_id"] not in part_ids or not isinstance(r["vec"], bytes) or len(r["vec"]) != dims * 4:
                    raise BadInput("vectors 表里有对不上片段或长度不对的行")
                vectors.append((r["part_id"], dims, r["vec"]))
        elif man.get("embedding") and not has_vec:
            usable, why = False, "manifest 说有向量，index.db 里却没有"
        return {"docs": docs, "sections": sections, "parts": parts, "vectors": vectors,
                "vec_state": "ok" if vectors else ("mismatch" if man.get("embedding") else "none"),
                "vec_why": "" if vectors else why}

    def _write_pack(self, man, pack):
        """在一个事务里：删这个库的旧行，插新行。任何一步出错整个回滚，本机原来的库原样还在。"""
        from . import kb as kbm
        from .kb import cards
        from .kb.ingest import fts_columns
        store = self.store
        svc = kbm.get(self.rt)
        lid = man["id"]
        mine = store.one("SELECT * FROM kb_libraries WHERE id=?", (lid,))
        name = man["name"]
        i = 1
        while True:
            other = store.one("SELECT id FROM kb_libraries WHERE name=? AND id<>?", (name, lid))
            if not other:
                break
            i += 1
            suffix = "（公司）" if i == 2 else "（公司%d）" % i
            name = man["name"][:kbm.NAME_MAX - len(suffix)] + suffix
        info = {"folder": man.get("folder") or "", "vectors": pack["vec_state"], "vectors_why": pack["vec_why"],
                "embedding": man.get("embedding"), "imported": now(), "docs": len(pack["docs"]), "parts": len(pack["parts"])}
        old_parts = []
        try:
            if getattr(svc.vectors(), "name", "") == "langchain":
                old_parts = [str(r["part_id"]) for r in store.q("SELECT part_id FROM kb_vectors WHERE library_id=?", (lid,))]
        except Exception:
            pass
        t = now()
        part_count = {}
        for p in pack["parts"]:
            part_count[p["doc_id"]] = part_count.get(p["doc_id"], 0) + 1
        with svc.indexer.ingest_lock:
            with store.tx():
                db = store.db
                db.execute("DELETE FROM kb_fts WHERE rowid IN (SELECT id FROM kb_parts WHERE library_id=?)", (lid,))
                for tname in ("kb_parts", "kb_sections", "kb_vectors", "kb_docfts", "kb_docs"):
                    db.execute("DELETE FROM %s WHERE library_id=?" % tname, (lid,))
                if mine:
                    db.execute("UPDATE kb_libraries SET name=?, note=?, updated=?, synced=?, shared=1, version=?, source_root=?, "
                               "built=?, shared_info=?, workspace_id=NULL WHERE id=?",
                               (name, man["note"], t, t, man["version"], man["source_root"], man["built"],
                                json.dumps(info, ensure_ascii=False), lid))
                else:
                    db.execute("INSERT INTO kb_libraries(id,name,note,created,updated,synced,shared,version,enabled,source_root,built,"
                               "shared_info) VALUES(?,?,?,?,?,?,1,?,1,?,?,?)",
                               (lid, name, man["note"], t, t, t, man["version"], man["source_root"], man["built"],
                                json.dumps(info, ensure_ascii=False)))
                sec_map = {}
                for s in pack["sections"]:
                    cur = db.execute("INSERT INTO kb_sections(doc_id,library_id,seq,loc,heading,text,tokens) VALUES(?,?,?,?,?,?,?)",
                                     (s["doc_id"], lid, s["seq"], s["loc"], s["heading"], s["text"], s["tokens"]))
                    sec_map[s["id"]] = cur.lastrowid
                for d in pack["docs"]:
                    outline = [dict(it, section_id=sec_map.get(it["section_id"])) for it in d["outline"]]
                    db.execute("INSERT INTO kb_docs(id,path,title,mtime,chunks,added,library_id,source_id,size,sha,status,error,kind,"
                               "tokens,meta,family,version_label,effective,outline,summary,keywords,summary_by,card_state,card_at,"
                               "lang,tags,tags_manual) VALUES(?,?,?,?,?,?,?,NULL,?,?,'ok','',?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                               (d["id"], d["path"], d["title"], d["mtime"], part_count.get(d["id"], 0), d["added"], lid,
                                d["size"], d["sha"], d["kind"], d["tokens"], d["meta"], d["family"], d["version_label"],
                                d["effective"], json.dumps(outline, ensure_ascii=False), d["summary"],
                                json.dumps(d["keywords"], ensure_ascii=False), d["summary_by"], d["card_state"], d["card_at"],
                                d["lang"], json.dumps(d["tags"], ensure_ascii=False)))
                part_map = {}
                for n, p in enumerate(pack["parts"]):
                    if n % BATCH == 0 and self._stopped():
                        raise BadInput("程序正在关闭")
                    cur = db.execute("INSERT INTO kb_parts(doc_id,library_id,seq,parent,loc,heading,text,tokens) VALUES(?,?,?,?,?,?,?,?)",
                                     (p["doc_id"], lid, p["seq"], sec_map.get(p["parent"]), p["loc"], p["heading"], p["text"],
                                      p["tokens"]))
                    nid = cur.lastrowid
                    part_map[p["id"]] = nid
                    db.execute("INSERT INTO kb_fts(rowid,text,heading,part_id,library_id) VALUES(?,?,?,?,?)",
                               (nid,) + fts_columns(p) + (nid, lid))
                for pid, dims, vec in pack["vectors"]:
                    db.execute("INSERT INTO kb_vectors(part_id,library_id,doc_id,dims,vec) VALUES(?,?,"
                               "(SELECT doc_id FROM kb_parts WHERE id=?),?,?)", (part_map[pid], lid, part_map[pid], dims, vec))
        # 事务外：文档级索引（规格 1.2 的 kb_docfts）按"后端·资料"的函数重建；向量库的缓存作废
        for d in pack["docs"]:
            try:
                cards.docfts_update(store, d["id"])
            except Exception:
                pass
        try:
            vs = svc.vectors()
            if old_parts and getattr(vs, "name", "") == "langchain":
                try:
                    vs.db().delete(ids=old_parts)
                except Exception:
                    pass
            lk = getattr(vs, "lock", None)
            if lk is not None and hasattr(vs, "ver"):
                with lk:
                    vs.ver += 1
        except Exception:
            pass
        if not pack["vectors"]:
            try:
                from .kb import embed as emb
                if emb.configured(self.cfg):
                    svc.indexer.enqueue(lid)          # 包里的向量没用上：本机配了向量模型就后台自己算
            except Exception:
                pass

    # ---- 导出（管资料的人做） ----
    def export_library(self, lid, target=None, source_root=None):
        """把本机的一个库写成索引包。返回 {dir, path, version, docs, parts, vectors}。
        先在本机临时目录建好 index.db，再复制到目标文件夹（先写临时名字再改名），manifest.json 最后写。"""
        from . import kb as kbm
        from .kb import embed as emb, vectors as vmod
        store = self.store
        lib = kbm.library(store, lid)
        if not lib:
            raise KeyError("没有这个资料库")
        if lid == kbm.ANSWERS_ID:
            raise ValueError("「标准答案」不能导出")
        if lib.get("shared"):
            raise ValueError("「%s」本身就是从共享目录同步来的，不能再导出" % lib["name"])
        if not ID_RE.match(lid):
            raise ValueError("这个资料库的 id 不合格，不能导出")
        if target is None:
            d = self.dir()
            if not d:
                raise ValueError("还没有设置公司共享目录：到设置页「公司共享目录」里填好，或者在请求里给 dir")
            if not os.path.isdir(d):
                raise ValueError("读不到共享目录：%s" % d)
            target = os.path.join(d, "libraries", safe_folder(lib["name"], lid))
        elif not _abs_ok(target):
            raise ValueError("dir 应该是一个文件夹的完整路径")
        if source_root is not None and (not isinstance(source_root, str) or len(source_root) > PATH_MAX or "\x00" in source_root
                                        or "\n" in source_root):
            raise ValueError("source_root 应该是一个文件夹的完整路径")
        prev, _ = read_json(os.path.join(target, "manifest.json"), MANIFEST_MAX)
        prev_ver = 0
        if isinstance(prev, dict) and prev.get("id") not in (None, lid):
            raise ValueError("这个文件夹里已经是另一个资料库的包了（%s），换一个文件夹或先让 IT 挪走" %
                             _clean(prev.get("name") if isinstance(prev.get("name"), str) else "", 40))
        if isinstance(prev, dict) and isinstance(prev.get("version"), int) and not isinstance(prev.get("version"), bool):
            prev_ver = prev["version"]
        version = max(prev_ver, int(lib.get("version") or 0)) + 1
        tmpdir = tempfile.mkdtemp(prefix="export-", dir=self._tmp_root())
        try:
            dbp = os.path.join(tmpdir, "index.db")
            svc = kbm.get(self.rt)
            with svc.indexer.ingest_lock:
                docs = store.q("SELECT * FROM kb_docs WHERE library_id=? AND status='ok' ORDER BY path", (lid,))
                secs = store.q("SELECT id, doc_id, seq, loc, heading, text, tokens FROM kb_sections WHERE library_id=? "
                               "ORDER BY doc_id, seq", (lid,))
                parts = store.q("SELECT id, doc_id, seq, parent, loc, heading, text, tokens FROM kb_parts WHERE library_id=? "
                                "ORDER BY doc_id, seq", (lid,))
                vec_ok = emb.configured(self.cfg) and vmod.engine_name(self.cfg) == "builtin"
                vecs = store.q("SELECT part_id, dims, vec FROM kb_vectors WHERE library_id=? AND vec IS NOT NULL", (lid,)) \
                    if vec_ok else []
            if not docs:
                raise ValueError("「%s」里还没有收录好的文件，没有可导出的" % lib["name"])
            root, rels = self._paths_for_export(docs)
            if source_root is not None:
                root = source_root.strip()
            okdocs = {d["id"] for d in docs}
            secs = [s for s in secs if s["doc_id"] in okdocs]
            parts = [p for p in parts if p["doc_id"] in okdocs]
            pids = {p["id"] for p in parts}
            vecs = [v for v in vecs if v["part_id"] in pids]
            con = sqlite3.connect(dbp)
            try:
                con.executescript(PACK_SCHEMA)
                with con:
                    for d in docs:
                        vals = dict(d, path=rels[d["id"]])
                        con.execute("INSERT INTO docs(%s) VALUES(%s)" % (",".join(PACK_COLS["docs"]), ",".join("?" * len(PACK_COLS["docs"]))),
                                    tuple(vals.get(c) for c in PACK_COLS["docs"]))
                    con.executemany("INSERT INTO sections(%s) VALUES(?,?,?,?,?,?,?)" % ",".join(PACK_COLS["sections"]),
                                    [tuple(s[c] for c in PACK_COLS["sections"]) for s in secs])
                    con.executemany("INSERT INTO parts(%s) VALUES(?,?,?,?,?,?,?,?)" % ",".join(PACK_COLS["parts"]),
                                    [tuple(p[c] for c in PACK_COLS["parts"]) for p in parts])
                    con.executemany("INSERT INTO vectors(part_id,dims,vec) VALUES(?,?,?)",
                                    [(v["part_id"], v["dims"], v["vec"]) for v in vecs])
                    con.execute("INSERT INTO info(key,value) VALUES('format', ?)", (str(FORMAT),))
                con.execute("PRAGMA journal_mode=DELETE")
            finally:
                con.close()
            h = hashlib.sha256()
            with open(dbp, "rb") as f:
                for b in iter(lambda: f.read(COPY_CHUNK), b""):
                    h.update(b)
            embinfo = None
            if vecs:
                c = emb.conf(self.cfg)
                dims = max(set(v["dims"] for v in vecs), key=lambda x: sum(1 for v in vecs if v["dims"] == x))
                embinfo = {"model": c["model"], "dims": c["dims"], "text_version": emb.TEXT_VERSION, "vec_dims": dims}
            manifest = {"format": FORMAT, "id": lid, "name": lib["name"], "note": lib.get("note") or "", "version": version,
                        "built": int(now()), "docs": len(docs), "parts": len(parts), "embedding": embinfo,
                        "source_root": root, "sha256": h.hexdigest()}
            try:
                os.makedirs(target, exist_ok=True)
                fd, tmp = tempfile.mkstemp(prefix=".index.db.tmp-", dir=target)
                os.close(fd)
                try:
                    shutil.copyfile(dbp, tmp)
                    _readable(tmp)
                    os.replace(tmp, os.path.join(target, "index.db"))
                except BaseException:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                    raise
                _write_atomic(os.path.join(target, "manifest.json"), _json_bytes(manifest), public=True)
            except OSError as e:
                raise ValueError("写不进去：%s（%s）" % (target, e))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        store.x("UPDATE kb_libraries SET version=? WHERE id=?", (version, lid))
        return {"dir": target, "path": target, "version": version, "docs": len(docs), "parts": len(parts), "vectors": bool(vecs),
                "source_root": root}

    def _tmp_root(self):
        p = os.path.join(self.cfg.home, "shared_tmp")
        os.makedirs(p, exist_ok=True)
        return p

    def _paths_for_export(self, docs):
        """(source_root, {doc_id: 相对 source_root 的路径})。几个来源时取它们共同的上级文件夹；
        算不出（比如在不同的盘上）时 source_root 为空，路径用相对各自来源的。"""
        from . import kb as kbm
        store = self.store
        abs_of = {d["id"]: kbm.doc_abs(store, d) for d in docs}
        bases = []
        for s in store.q("SELECT DISTINCT s.* FROM kb_sources s JOIN kb_docs d ON d.source_id=s.id WHERE d.id IN (%s)"
                         % ",".join("?" * len(docs)), tuple(d["id"] for d in docs)) if docs else []:
            bases.append(s["path"] if s["kind"] == "dir" else os.path.dirname(s["path"]))
        root = ""
        if bases:
            try:
                root = os.path.commonpath(bases)
            except ValueError:
                root = ""
        rels = {}
        for d in docs:
            p = abs_of.get(d["id"])
            r = None
            if root and p:
                try:
                    r = os.path.relpath(p, root)
                except ValueError:
                    r = None
            if r is None or r.startswith(".."):
                root = ""
                break
            rels[d["id"]] = r.replace("\\", "/")
        if not root:
            rels = {d["id"]: (d.get("path") or d.get("title") or d["id"]).replace("\\", "/") for d in docs}
        return root, rels

    # ---- 5.6 把问题交给管资料的人 ----
    def submit_gaps(self):
        from .kb import ops
        d = self.dir()
        if not d:
            raise ValueError("还没有设置公司共享目录，没法交上去。可以先用「导出」下载成表格。")
        if not os.path.isdir(d):
            raise ValueError("读不到共享目录：%s" % d)
        rows = ops.gaps(self.store, "open")
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["问题", "原因", "次数", "第一次", "最近一次", "状态", "备注"])
        for g in rows:
            w.writerow([_cell(g["question"]), ops.REASON_TEXT.get(g["reason"], g["reason"]), g["count"], ops._ts(g["first_ts"]),
                        ops._ts(g["last_ts"]), ops.STATUS_TEXT.get(g["status"], g["status"]), _cell(g.get("note") or "")])
        path = os.path.join(d, "inbox", "gaps", user_file_name() + ".csv")
        try:
            _write_atomic(path, ("\ufeff" + buf.getvalue()).encode("utf-8"), public=True)
        except OSError as e:
            raise ValueError("共享目录写不进去（%s）。请找 IT 给 inbox/gaps 这个文件夹开写权限。" % e)
        return {"path": path, "count": len(rows)}


def _cell(v):
    """CSV 单元格：以 = + - @ 开头的在前面加一个 '，免得 Excel 当成公式。"""
    s = str(v or "")
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def user_file_name():
    try:
        u = getpass.getuser()
    except Exception:
        u = ""
    u = re.sub(r"[^\w.-]", "_", u or "")[:64].strip("._")
    return u or "user"
