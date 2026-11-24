"""带参数的任务模板（docs/借鉴开源项目.md 第 6 节）。只用标准库，接口在 agent/api_kernel.py。

格式：prompt 里用 {{…}} 留空
  {{月份}}                    文本框，标签就是"月份"
  {{月份:month}}              类型：text（默认）、month、date、number、file、library、choice
  {{部门:choice=销售|市场|财务}} 下拉
  {{页数:number=8}}           "=" 后面对 choice 是选项，对其他类型是默认值（规格没写，这里补的）
  {{备注?}}                   名字后面加问号：可以不填；不填时连同它前面紧挨着的一个标点一起去掉
也可以显式写 params: [{name, label?, type?, options?, default?, required?}]，有 params 的以它为准。
全角的冒号、问号、竖线也认。写法不对的空不算参数，原样留在文字里，并在 errors 里说明。

来源（每条带 source）：builtin（agent/templates.json）、user（数据目录的 templates.json）、shared（公司共享目录，
由"后端·共享"用 register_source 接进来）。同一个 id 出现在几个来源里时：本机 > 公司 > 内置。"""
from __future__ import annotations
import hashlib, json, os, re, threading

from . import modes
from .store import new_id

TYPES = ("text", "month", "date", "number", "file", "library", "choice")
SOURCE_NAMES = {"builtin": "内置", "user": "本机", "shared": "公司"}
PRIORITY = {"user": 3, "shared": 2, "builtin": 1}
NAME_MAX = 20
TITLE_MAX = 40
PROMPT_MAX = 4000
ROLE_MAX = 20
VALUE_MAX = 500
OPTIONS_MAX = 30
USER_MAX = 200
USER_FILE = "templates.json"

PLACEHOLDER = re.compile(r"\{\{([^{}]*)\}\}")
PUNCT = "，,、；;：:。.！!？?"
_lock = threading.Lock()


class TemplateError(ValueError):
    pass


# ---------------- 解析 ----------------

def _half(s):
    return s.replace("：", ":").replace("？", "?").replace("｜", "|").replace("＝", "=")


def parse_one(inner):
    """{{ }} 里面的文字 → (参数 dict, 错误说明)。参数：{name, label, type, options?, default?, required}。"""
    raw = _half(inner).strip()
    if not raw:
        return None, "空的 {{}}：要写一个名字，例如 {{月份}}"
    name, _, spec = raw.partition(":")
    name, spec = name.strip(), spec.strip()
    required = True
    if name.endswith("?"):
        required, name = False, name[:-1].strip()
    if spec.endswith("?"):              # {{备注:text?}} 也当作可以不填
        required, spec = False, spec[:-1].strip()
    if not name:
        return None, "{{%s}}：冒号前面要有名字" % inner
    if len(name) > NAME_MAX:
        return None, "{{%s}}：名字最多 %d 个字" % (inner, NAME_MAX)
    if any(c in name for c in "=|\n"):
        return None, "{{%s}}：名字里不能有 = 或 |" % inner
    typ, eq, rest = spec.partition("=")
    typ = (typ.strip() or "text").lower()
    if typ not in TYPES:
        return None, "{{%s}}：不认识的类型 %s（可以用 %s）" % (inner, typ, "、".join(TYPES))
    p = {"name": name, "label": name, "type": typ, "required": required}
    rest = rest.strip()
    if typ == "choice":
        opts = []
        for o in rest.split("|"):
            o = o.strip()
            if o and o not in opts:
                opts.append(o)
        if not opts:
            return None, "{{%s}}：下拉要写选项，例如 {{部门:choice=销售|市场}}" % inner
        p["options"] = opts[:OPTIONS_MAX]
    elif eq:
        if rest:
            err = check_value(p, rest)[1]
            if err:
                return None, "{{%s}}：默认值%s" % (inner, err)
            p["default"] = rest
    return p, None


def parse(prompt):
    """prompt → {params: [...], errors: ["…"]}。同一个名字出现几次算一个空（按第一次的写法）。"""
    prompt = prompt if isinstance(prompt, str) else ""
    params, errors, seen = [], [], {}
    for m in PLACEHOLDER.finditer(prompt):
        p, err = parse_one(m.group(1))
        if err:
            errors.append(err)
            continue
        old = seen.get(p["name"])
        if old is not None:
            if {k: v for k, v in old.items()} != p:
                errors.append("「%s」前后写法不一样，按第一次的" % p["name"])
            continue
        seen[p["name"]] = p
        params.append(p)
    rest = PLACEHOLDER.sub("", prompt)
    if "{{" in rest or "}}" in rest:
        errors.append("有没配对的 {{ 或 }}，那一段原样保留")
    return {"params": params, "errors": errors}


def norm_params(raw):
    """显式写的 params → 规整后的列表。不合格的抛 TemplateError。"""
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise TemplateError("params 应该是列表")
    out, names = [], set()
    for x in raw:
        if not isinstance(x, dict) or not isinstance(x.get("name"), str) or not x["name"].strip():
            raise TemplateError("params 里每一项要有 name")
        name = x["name"].strip()
        if len(name) > NAME_MAX or any(c in name for c in "=|{}\n"):
            raise TemplateError("参数名「%s」不合格：最多 %d 个字，不能有 = | { }" % (name, NAME_MAX))
        if name in names:
            raise TemplateError("参数名「%s」重复了" % name)
        names.add(name)
        typ = str(x.get("type") or "text").lower()
        if typ not in TYPES:
            raise TemplateError("参数「%s」的类型 %s 不认识（可以用 %s）" % (name, typ, "、".join(TYPES)))
        label = x.get("label") if isinstance(x.get("label"), str) and x["label"].strip() else name
        p = {"name": name, "label": label.strip()[:NAME_MAX * 2], "type": typ,
             "required": bool(x.get("required", True))}
        if typ == "choice":
            opts = x.get("options")
            if not isinstance(opts, list) or not [o for o in opts if isinstance(o, (str, int, float))]:
                raise TemplateError("参数「%s」是下拉，要给 options" % name)
            p["options"] = []
            for o in opts:
                o = str(o).strip()
                if o and o not in p["options"]:
                    p["options"].append(o)
            p["options"] = p["options"][:OPTIONS_MAX]
        if x.get("default") not in (None, ""):
            d = str(x["default"]).strip()
            err = check_value(p, d)[1]
            if err:
                raise TemplateError("参数「%s」的默认值%s" % (name, err))
            p["default"] = d
        out.append(p)
    return out


# ---------------- 填空 ----------------

_MONTH = re.compile(r"^(\d{4})\s*[-/.年]\s*(\d{1,2})\s*月?$")
_DATE = re.compile(r"^(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?$")


def check_value(p, v):
    """按类型检查一个值。返回 (规整后的值, 错误说明或 None)。"""
    t = p.get("type") or "text"
    if len(v) > VALUE_MAX:
        return v, "太长了（最多 %d 个字）" % VALUE_MAX
    if t == "month":
        m = _MONTH.match(v)
        if not m or not 1 <= int(m.group(2)) <= 12:
            return v, "应该是年月，例如 2026-10"
        return "%s-%02d" % (m.group(1), int(m.group(2))), None
    if t == "date":
        m = _DATE.match(v)
        if not m or not 1 <= int(m.group(2)) <= 12 or not 1 <= int(m.group(3)) <= 31:
            return v, "应该是日期，例如 2026-10-08"
        return "%s-%02d-%02d" % (m.group(1), int(m.group(2)), int(m.group(3))), None
    if t == "number":
        try:
            float(v.replace(",", ""))
        except ValueError:
            return v, "应该是数字"
        return v, None
    if t == "choice":
        if v not in (p.get("options") or []):
            return v, "只能是 %s 之一" % "、".join(p.get("options") or [])
        return v, None
    if t in ("file", "library") and "\n" in v:
        return v, "不能有换行"
    return v, None


def render(prompt, values, explicit=None):
    """填空。values：{名字: 值}。返回 {text, missing: [名字], invalid: [{name, why}]}。
    填进去的值是普通文字，不再解析里面的 {{}}。可以不填的空没填：连同它前面紧挨着的一个标点（和空白）一起去掉。"""
    prompt = prompt if isinstance(prompt, str) else ""
    values = values if isinstance(values, dict) else {}
    defs = {}
    if explicit:
        defs = {p["name"]: p for p in explicit}
    else:
        for p in parse(prompt)["params"]:
            defs[p["name"]] = p
    filled, missing, invalid = {}, [], []
    for name, p in defs.items():
        v = values.get(name)
        v = "" if v is None else (str(v) if not isinstance(v, bool) else "")
        v = v.strip()
        if not v and p.get("default"):
            v = p["default"]
        if not v:
            if p.get("required", True) and _appears(prompt, name):
                missing.append(name)
            continue
        v2, err = check_value(p, v)
        if err:
            invalid.append({"name": name, "why": err})
            continue
        filled[name] = v2
    if missing or invalid:
        return {"text": "", "missing": missing, "invalid": invalid}
    out, pos = [], 0
    for m in PLACEHOLDER.finditer(prompt):
        p, err = parse_one(m.group(1))
        name = p["name"] if p else None
        if err or name not in defs:
            continue                              # 写法不对的、显式 params 里没有的：原样留着
        out.append(prompt[pos:m.start()])
        if name in filled:
            out.append(filled[name])
        else:
            _drop_punct(out)
        pos = m.end()
    out.append(prompt[pos:])
    return {"text": "".join(out), "missing": [], "invalid": []}


def _appears(prompt, name):
    for m in PLACEHOLDER.finditer(prompt):
        p, err = parse_one(m.group(1))
        if p and p["name"] == name:
            return True
    return False


def _drop_punct(out):
    """去掉已经拼好的文字末尾紧挨着的一个标点（连同它前后的空白）。"""
    s = "".join(out).rstrip()
    if s and s[-1] in PUNCT:
        s = s[:-1].rstrip()
    out[:] = [s]


# ---------------- 来源 ----------------

def _load_json_list(path):
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            v = json.load(f)
        return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []
    except (OSError, ValueError):
        return []


def load_builtin(rt):
    return _load_json_list(os.path.join(rt.app_dir, "agent", "templates.json"))


def user_path(rt):
    return os.path.join(rt.cfg.home, USER_FILE)


def load_user(rt):
    return _load_json_list(user_path(rt))


# 合并的口子：[(来源, 读取函数(rt) -> [原始模板 dict])]。公司共享目录接进来时：
#   from agent import templates; templates.register_source("shared", 读共享目录的函数)
SOURCES = [("builtin", load_builtin), ("user", load_user)]


def register_source(source, loader):
    """加一个来源（同名的替换掉）。loader 出错时这个来源当作空的。"""
    for i, (s, _) in enumerate(SOURCES):
        if s == source:
            SOURCES[i] = (source, loader)
            return
    SOURCES.append((source, loader))


def _stable_id(source, raw):
    if isinstance(raw.get("id"), str) and re.match(r"^[\w-]{1,64}$", raw["id"]):
        return raw["id"]
    h = hashlib.sha1(("%s|%s|%s" % (source, raw.get("title"), raw.get("mode"))).encode("utf-8")).hexdigest()[:8]
    return {"builtin": "b_", "shared": "s_", "user": "u_"}.get(source, "x_") + h


def normalize(raw, source):
    """原始模板 → 接口里的一项：{id, title, prompt, role, mode, params, source}（解析有问题时多 errors）。"""
    prompt = raw.get("prompt") if isinstance(raw.get("prompt"), str) else ""
    item = {"id": _stable_id(source, raw), "title": str(raw.get("title") or "")[:TITLE_MAX * 2], "prompt": prompt,
            "role": str(raw.get("role") or ""), "source": source}
    if modes.valid(raw.get("mode")):
        item["mode"] = raw["mode"]
    errors = []
    explicit = None
    if raw.get("params") is not None:
        try:
            explicit = norm_params(raw.get("params"))
        except TemplateError as e:
            errors.append(str(e))
    if explicit is not None:
        item["params"] = explicit
        item["_explicit"] = True
    else:
        p = parse(prompt)
        item["params"] = p["params"]
        errors += p["errors"]
    if errors:
        item["errors"] = errors
    return item


def all_items(rt):
    """三个来源合并。同一个 id：本机 > 公司 > 内置；顺序按来源登记的先后、来源里的先后。"""
    out, where = [], {}
    for source, loader in list(SOURCES):
        try:
            raws = loader(rt) or []
        except Exception:
            raws = []
        for raw in raws:
            if not isinstance(raw, dict):
                continue
            it = normalize(raw, source)
            k = it["id"]
            if k in where:
                if PRIORITY.get(source, 0) >= PRIORITY.get(out[where[k]]["source"], 0):
                    out[where[k]] = it
                continue
            where[k] = len(out)
            out.append(it)
    return out


def public(item):
    return {k: v for k, v in item.items() if not k.startswith("_")}


def listing(rt, mode=None):
    """GET /api/templates。给了 mode 就只要这个模式的；模板没写 mode 时两个模式都有（内置的都写了 mode，行为和原来一样）。"""
    out = []
    for it in all_items(rt):
        if mode and it.get("mode") and it["mode"] != mode:
            continue
        out.append(public(it))
    return out


def find(rt, tid):
    for it in all_items(rt):
        if it["id"] == tid:
            return it
    return None


def render_item(item, values):
    return render(item.get("prompt") or "", values, item.get("params") if item.get("_explicit") else None)


# ---------------- 本机模板的增删改 ----------------

def _clean(body, partial=False):
    if not isinstance(body, dict):
        raise TemplateError("请求内容应该是对象")
    bad = [k for k in body if k not in ("title", "prompt", "role", "mode", "params")]
    if bad:
        raise TemplateError("不认识的字段：%s" % "、".join(bad))
    out = {}
    if "title" in body or not partial:
        t = body.get("title")
        if not isinstance(t, str) or not t.strip():
            raise TemplateError("title 不能为空")
        t = " ".join(t.split())
        if len(t) > TITLE_MAX:
            raise TemplateError("标题最多 %d 个字" % TITLE_MAX)
        out["title"] = t
    if "prompt" in body or not partial:
        p = body.get("prompt")
        if not isinstance(p, str) or not p.strip():
            raise TemplateError("prompt 不能为空")
        if len(p) > PROMPT_MAX:
            raise TemplateError("内容最多 %d 个字" % PROMPT_MAX)
        out["prompt"] = p.strip()
    if "role" in body:
        r = body.get("role")
        if r is not None and not isinstance(r, str):
            raise TemplateError("role 应该是文字")
        out["role"] = (r or "").strip()[:ROLE_MAX]
    if "mode" in body:
        m = body.get("mode")
        if m not in (None, "") and not modes.valid(m):
            raise TemplateError("mode 只能是 %s" % " 或 ".join(modes.IDS))
        out["mode"] = m or None
    if "params" in body:
        out["params"] = norm_params(body.get("params")) if body.get("params") is not None else None
    return out


def _save_user(rt, items):
    path = user_path(rt)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _strip_none(d):
    return {k: v for k, v in d.items() if v is not None}


def add_user(rt, body):
    data = _clean(body)
    with _lock:
        items = load_user(rt)
        if len(items) >= USER_MAX:
            raise TemplateError("本机模板最多 %d 条" % USER_MAX)
        raw = _strip_none(dict(data, id=new_id("u_")))
        items.append(raw)
        _save_user(rt, items)
    return public(normalize(raw, "user"))


def update_user(rt, tid, body):
    data = _clean(body, partial=True)
    with _lock:
        items = load_user(rt)
        for i, raw in enumerate(items):
            if _stable_id("user", raw) == tid:
                new = dict(raw)
                new.update(data)
                new = _strip_none(new)
                new["id"] = tid
                items[i] = new
                _save_user(rt, items)
                return public(normalize(new, "user"))
    raise TemplateError("没有这条本机模板")


def delete_user(rt, tid):
    with _lock:
        items = load_user(rt)
        keep = [x for x in items if _stable_id("user", x) != tid]
        if len(keep) == len(items):
            return False
        _save_user(rt, keep)
        return True

