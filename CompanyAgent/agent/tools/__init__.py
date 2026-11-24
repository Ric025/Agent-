"""工具注册表。每个工具有：名字、说明、参数格式、风险级别、处理函数。"""
from __future__ import annotations
import json

REGISTRY = {}


class ToolError(Exception):
    """预期内的失败：原样告诉模型，让它换个做法。"""


class Tool:
    def __init__(self, name, desc, props, required, handler, risk, sub_ok, enabled):
        self.name, self.desc, self.props, self.required = name, desc, props, required
        self.handler, self.risk, self.sub_ok, self.enabled = handler, risk, sub_ok, enabled
        self.display = None       # display(ctx, args) -> 给界面的 {"kind", "title", …}（docs/操控电脑.md 2.4），没有就 None
        self.variant = None       # variant(ctx, tool) -> 这次任务里实际给模型的版本（例如按能力多一个参数）

    def schema(self):
        return {"type": "object", "properties": self.props, "required": self.required}

    def spec(self):
        return {"type": "function", "function": {"name": self.name, "description": self.desc, "parameters": self.schema()}}

    def risk_of(self, ctx, args):
        return self.risk(ctx, args) if callable(self.risk) else self.risk

    def display_of(self, ctx, args):
        """给界面用的 display；没有或出错时 None（事件里就不带这个字段）。"""
        if not callable(self.display):
            return None
        try:
            d = self.display(ctx, args if isinstance(args, dict) else {})
            return d if isinstance(d, dict) else None
        except Exception:
            return None


class WithImage(str):
    """带一张图片的工具结果（例如网页截图）：文字照常给模型，图片记在 tool_result 的 image 字段里。"""

    def __new__(cls, text, image):
        o = str.__new__(cls, text)
        o.image = image
        return o


class WithData(str):
    """带结构化数据的工具结果（例如 kb_search 的 hits、display）：文字照常给模型，extra 里的键并进 tool_result 的 payload。"""

    def __new__(cls, text, extra):
        o = str.__new__(cls, text)
        o.extra = dict(extra or {})
        return o


def tool(name, desc, props=None, required=None, risk="read", sub_ok=False, enabled=None, display=None, variant=None):
    props = props or {}

    def deco(fn):
        t = Tool(name, desc, props, list(props) if required is None else required, fn, risk, sub_ok, enabled)
        t.display, t.variant = display, variant
        REGISTRY[name] = t
        return fn
    return deco


# 参数格式的简写
def S(d, enum=None):
    o = {"type": "string", "description": d}
    if enum:
        o["enum"] = enum
    return o


def I(d):
    return {"type": "integer", "description": d}


def A(d, items):
    return {"type": "array", "description": d, "items": items}


def O(props, required=None):
    return {"type": "object", "properties": props, "required": list(props) if required is None else required}


_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list, "object": dict,
          "null": type(None)}


def validate(schema, value, path="参数"):
    """最小的 JSON Schema 校验：类型、必填、枚举、数组元素、嵌套对象。返回错误信息或 None。"""
    t = schema.get("type")
    if isinstance(t, list):
        # 联合类型（如 ["number", "null"]，连接器的参数格式里常见）：满足其一即可
        if not t or any(validate(dict(schema, type=x), value, path) is None for x in t):
            return None
        return "%s 应该是 %s" % (path, " 或 ".join(map(str, t)))
    if t:
        py = _TYPES.get(t)
        if t == "integer" and isinstance(value, float) and value.is_integer():
            value = int(value)
        if py and (not isinstance(value, py) or (t in ("integer", "number") and isinstance(value, bool))):
            return "%s 应该是 %s" % (path, t)
    if "enum" in schema and value not in schema["enum"]:
        return "%s 只能是 %s 之一" % (path, "、".join(map(str, schema["enum"])))
    if t == "object":
        for r in schema.get("required", []):
            if r not in value:
                return "%s 缺少必填字段 %s" % (path, r)
        for k, v in value.items():
            if k in schema.get("properties", {}):
                err = validate(schema["properties"][k], v, "%s.%s" % (path, k))
                if err:
                    return err
    if t == "array" and "items" in schema:
        for i, v in enumerate(value):
            err = validate(schema["items"], v, "%s[%d]" % (path, i))
            if err:
                return err
    return None


def available(ctx, sub=False):
    """这次任务里模型能看到的工具。一次任务中保持不变。按对话的模式过滤（agent/modes.py）。"""
    from .. import modes
    mode = modes.of_ctx(ctx)
    out = []
    for t in REGISTRY.values():
        if sub and not t.sub_ok:
            continue
        if not modes.tool_allowed(mode, t.name):
            continue
        if t.enabled and not t.enabled(ctx):
            continue
        if ctx.store.tool_setting(t.name) == "deny":
            continue
        if callable(t.variant):
            t = t.variant(ctx, t)
        out.append(t)
    return out + (ctx.rt.mcp_tools(ctx) if not sub and ctx.rt and modes.mcp_allowed(mode) else [])


def to_text(result, limit):
    """把工具结果变成给模型看的文字，并限制长度。返回 (文字, 是否截断)。"""
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def load_all():
    from . import fs, office, kb, compute, misc, web, history, browser, desktop, system  # noqa: F401
