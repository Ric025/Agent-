"""核对数字（docs/两种模式.md 9.3）：回答说完之后，程序自己把回答里的数字和它引用的出处对一遍，不靠模型。

- 只标记，不拦回答、不改回答、不告诉模型；结果是一条 grounding 事件（写进账本，界面刷新后还原）：
  {"msg_seq", "cited", "unknown_refs", "checks": [{"n", "numbers", "missing"}], "uncited_numbers", "ok"}
- 什么时候做：一次运行里最后那条助手回答（不带工具调用）写进账本之后（Runtime.emit 里挂的钩子，两个内核共用）。
  核对完再交给 kb.ops.after_answer：标准答案的命中次数、"没答上来的问题"（docs/借鉴开源项目.md 第 4 节）。
  Hspace 每条都做；CariAi 只在回答里出现了 [n]、而且这个对话里检索过资料时做。子代理不做。
- 纯 Python，不调模型。一条普通回答几毫秒。

规则：
- 先去掉代码块和行内代码；按句拆（。？！；?!; 和换行；列表的每一项算一句）。出处标在句末（包括句号后面）时管这一整句；
  一句里有几组出处时，每组管它前面到上一组为止的那一段，最后一组之后的话归最后一组。
- 认 [1]、[1][2]、[1, 2]、[1，2]、[1、2]。
- 抽数字：阿拉伯数字，含小数、千分位、百分号，和紧跟的单位一起记（"800 元""12.4%""5 个工作日""1.2 亿元"）。
  不算：出处编号；行首的列表序号（"1." "2、" "（1）"）；《》里的；"第 3 章""3.2 节""第 7 页""第 2–40 行""步骤 4"这类指位置的；
  "出处：……"这一行；"以下不是资料里的内容"之后的话（只对"没标出处"那一项）。
- 比：数字规整后（去千分位、全角转半角、去掉小数末尾的零）按完整的数字比（800 不会对上 1800、8000、800.5）；
  比数字本身不比单位（原文"1.2 亿"、回答"1.2 亿元"算对上；原文"12000 万"、回答"1.2 亿"算没对上，不做换算）。
  先找 hit["text"]，再找标题、标题路径，最后找整节全文。"""
from __future__ import annotations
import re, unicodedata

from . import modes

# ---------------- 拆句、出处 ----------------

REF_RE = re.compile(r"\[\s*(\d{1,4}(?:\s*[,，、]\s*\d{1,4})*)\s*\]")
REF_RUN_RE = re.compile(r"(?:\[\s*\d{1,4}(?:\s*[,，、]\s*\d{1,4})*\s*\]\s*)+")
FENCE_RE = re.compile(r"```.*?(?:```|\Z)|~~~.*?(?:~~~|\Z)", re.S)
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
SENT_END = "。？！；?!;"
LIST_HEAD_RE = re.compile(r"^\s*(?:#{1,6}\s*)?(?:>\s*)?(?:[-*+•·]\s+)?(?:\d{1,3}[.、)）](?!\d)|[(（]\d{1,3}[)）])?\s*")
SOURCE_LINE_RE = re.compile(r"^\s*(?:[-*+]\s*)?(?:\*\*)?(?:出处|来源|参考资料|参考|依据|引用)(?:\*\*)?\s*[:：]")
GENERAL_MARK = "以下不是资料里的内容"

# ---------------- 数字 ----------------

_FW = {ord(c): ord(c) - 0xFEE0 for c in "０１２３４５６７８９．％"}
_FW[0xFF0C] = 0xFF0C            # 全角逗号不动（中文句子里的逗号）

MAG = "万亿千百"
UNITS = sorted("""元 块 角 分钱 美元 港元 港币 欧元 日元 英镑 人民币 元人民币 万元 亿元 千元 百元
个工作日 工作日 个自然日 自然日 个月 个季度 季度 个小时 小时 分钟 秒钟 秒 天 日 周 星期 年 月 晚 夜 次 回 遍
人 名 位 家 户 个 项 条 份 套 台 张 页 篇 本 件 款 种 类 级 层 栋 间 辆 架 部 批 场 期 届
个百分点 百分点 倍 成 岁 公里 千米 米 厘米 毫米 平方米 平米 亩 公顷 吨 千克 公斤 克 升 毫升 度 瓦 千瓦 兆瓦
km kg KG GB MB TB KB m cm mm kW kWh h""".split(), key=len, reverse=True)
_UNIT_ALT = "|".join(re.escape(u) for u in UNITS)
# 数字：千分位 / 普通；可带小数；时间 9:30 算一个
NUM_CORE = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?::\d{2})*"
NUM_RE = re.compile(r"(?<![A-Za-z0-9_.])(" + NUM_CORE + r")(?![0-9])"
                    r"(\s*(?:%|‰|(?:[" + MAG + r"]{0,2}(?:" + _UNIT_ALT + r"))|[" + MAG + r"]{1,2}))?")
SRC_NUM_RE = re.compile(r"(?<![0-9.])(" + NUM_CORE + r")(?![0-9])")
# 指位置的：第 3 章、第 7 页、第 2–40 行、3.2 节、步骤 4、Step 4
POS_RES = [re.compile(r"第\s*\d+(?:\s*[-–—~～至到]\s*\d+)?\s*[章节条款页版部篇项行列步段张册卷期届次]"),
           re.compile(r"(?<![0-9.])\d+(?:\.\d+)+\s*[章节条款](?![0-9])"),
           re.compile(r"(?<![0-9.])\d+\s*[章节](?![0-9])"),
           re.compile(r"(?:步骤|Step|step|STEP)\s*\d+")]
BOOK_RE = re.compile(r"《[^《》\n]*》")
CN_NUM_RE = re.compile(r"[零〇一二两三四五六七八九十百千万]+")
_CN_DIGIT = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNIT = {"十": 10, "百": 100, "千": 1000}


def halfwidth(s):
    """全角数字、全角小数点、全角百分号转半角（一对一，长度不变）。"""
    return (s or "").translate(_FW)


def norm_number(s):
    """规整：去千分位、全角转半角、整数部分去前导零、小数去末尾的零。'12.40%' → '12.4'，'3,860' → '3860'。"""
    s = halfwidth(s or "").strip().rstrip("%‰").strip()
    m = re.match(r"^(\d[\d,]*)(\.\d+)?((?::\d{2})*)", s)
    if not m:
        return s
    ip = m.group(1).replace(",", "").lstrip("0") or "0"
    fp = (m.group(2) or "").rstrip("0")
    if fp == ".":
        fp = ""
    return ip + fp + (m.group(3) or "")


def _blank(text, rx):
    return rx.sub(lambda m: " " * len(m.group(0)), text)


def _cn_value(run):
    """简单的中文数字（到千位）：'五' → 5，'十二' → 12，'三百' → 300。认不出返回 None。"""
    total, cur = 0, None
    for ch in run:
        if ch in _CN_DIGIT:
            cur = _CN_DIGIT[ch]
        elif ch in _CN_UNIT:
            total += (1 if cur is None else cur) * _CN_UNIT[ch]
            cur = None
        else:
            return None
    return total + (cur or 0)


def numbers_in(text):
    """一段话（回答里的一句）里的数字：[(显示的样子, 规整后的数字)]。指位置的、书名号里的已经去掉。"""
    t = halfwidth(text or "")
    t = _blank(t, BOOK_RE)
    for rx in POS_RES:
        t = _blank(t, rx)
    out = []
    for m in NUM_RE.finditer(t):
        # 紧跟字母（5G、2B）而不是认得的单位：像型号代码，不算
        end = m.end()
        if not m.group(2) and end < len(t) and re.match(r"[A-Za-z]", t[end]):
            continue
        shown = re.sub(r"\s+", " ", m.group(0).strip())
        out.append((shown, norm_number(m.group(1))))
    return out


def source_numbers(text):
    """出处原文里的数字（规整后的集合）。不去任何东西；千分位的也拆开各算一份，中文数字也换算一份，宁可宽一点。"""
    t = halfwidth(text or "")
    got = set()
    for m in SRC_NUM_RE.finditer(t):
        raw = m.group(1)
        got.add(norm_number(raw))
        if "," in raw:
            for piece in raw.split(","):
                got.add(norm_number(piece))
        if ":" in raw:
            got.add(norm_number(raw.split(":")[0]))
    for m in CN_NUM_RE.finditer(t):
        v = _cn_value(m.group(0).rstrip("万"))
        if v:
            got.add(str(v))
    return got


# ---------------- 拆句 ----------------

def strip_code(text):
    t = _blank(text or "", FENCE_RE)
    return _blank(t, INLINE_CODE_RE)


def _refs(s):
    out = []
    for m in REF_RE.finditer(s):
        for x in re.split(r"\s*[,，、]\s*", m.group(1)):
            if x.strip().isdigit():
                n = int(x)
                if n not in out:
                    out.append(n)
    return out


def sentences(text):
    """[(句子原文, 是不是"出处"行, 在"以下不是资料里的内容"之后, 第几行)]。句号后面紧跟的 [n] 归前一句。"""
    out = []
    general = False
    for li, line in enumerate(strip_code(text).split("\n")):
        if not line.strip():
            continue
        if GENERAL_MARK in line:
            general = True
        src_line = bool(SOURCE_LINE_RE.match(line))
        body = LIST_HEAD_RE.sub(lambda m: " " * len(m.group(0)), line, count=1)
        cur = ""
        i = 0
        pieces = []
        while i < len(body):
            ch = body[i]
            cur += ch
            i += 1
            if ch in SENT_END:
                # 句号后面紧跟的出处编号（可以隔着空格、右括号、引号）归这一句
                m = re.match(r"[\s)）」”\"']*" + REF_RUN_RE.pattern, body[i:])
                if m:
                    cur += m.group(0)
                    i += m.end()
                pieces.append(cur)
                cur = ""
        if cur.strip():
            pieces.append(cur)
        for p in pieces:
            if p.strip():
                out.append((p, src_line, general, li))
    return out


def clauses(sentence):
    """一句话按出处分段：[(这一段的文字, [编号])]。没有出处的句子返回 [(整句, [])]。"""
    runs = list(REF_RUN_RE.finditer(sentence))
    if not runs:
        return [(sentence, [])]
    out, start = [], 0
    for m in runs:
        out.append([sentence[start:m.start()], _refs(m.group(0))])
        start = m.end()
    tail = sentence[start:]
    if tail.strip():
        out[-1][0] += tail
    return [(a, b) for a, b in out]


# ---------------- 核对 ----------------

def _hit_numbers(store, hit, cache):
    """一个出处的数字：先片段原文，再标题、标题路径，再整节全文（要时才查）。返回 (浅的集合, 取整节的函数)。"""
    key = id(hit)
    if key not in cache:
        cache[key] = {"shallow": source_numbers("\n".join(str(hit.get(k) or "") for k in ("text", "title", "heading", "loc"))),
                      "deep": None}
    ent = cache[key]

    def deep():
        if ent["deep"] is None:
            ent["deep"] = set()
            if store is not None and hit.get("part_id") is not None:
                try:
                    from . import kb as kbm
                    det = kbm.part_detail(store, hit["part_id"])
                    if det:
                        ent["deep"] = source_numbers(((det.get("section") or {}).get("text") or "") + "\n"
                                                     + ((det.get("part") or {}).get("text") or ""))
                except Exception:
                    pass
        return ent["deep"]
    return ent["shallow"], deep


def check(text, parts, mode=modes.HSPACE, store=None):
    """核对一条回答。parts 是 {编号: hit}（kb.cited_parts 的结果）。返回 grounding 事件的 payload（不含 msg_seq）。"""
    hspace = modes.norm(mode) == modes.HSPACE
    cited, unknown = [], []
    checks = {}                 # n -> {"numbers": [...], "missing": [...]}
    uncited = []
    cache = {}
    sents = sentences(text or "")
    # 一段（一行）的最后一句标了出处、前面几句没标："A。B。[1]" 这种写法里段末的出处管这一整段
    inherit = {}
    for i, (sent, src_line, general, li) in enumerate(sents):
        last = i + 1 == len(sents) or sents[i + 1][3] != li
        if last and REF_RUN_RE.search(sent):
            refs = clauses(sent)[0][1] if len(clauses(sent)) == 1 else []
            j = i - 1
            while refs and j >= 0 and sents[j][3] == li and not REF_RUN_RE.search(sents[j][0]):
                inherit[j] = refs
                j -= 1
    for i, (sent, src_line, general, li) in enumerate(sents):
        segs = clauses(sent)
        if i in inherit:
            segs = [(sent, inherit[i])]
        for seg, refs in segs:
            # 出处编号本身不算数字：从这一段里去掉
            seg_text = _blank(seg, REF_RE)
            nums = [] if src_line and not refs else numbers_in(seg_text)
            if not refs:
                if hspace and not general and not src_line:
                    for shown, _ in nums:
                        if shown not in uncited:
                            uncited.append(shown)
                continue
            known = [n for n in refs if n in parts]
            for n in refs:
                if n in parts:
                    if n not in cited:
                        cited.append(n)
                elif n not in unknown:
                    unknown.append(n)
            missing = []
            for shown, val in nums:
                found = False
                for n in known:
                    shallow, deep = _hit_numbers(store, parts[n], cache)
                    if val in shallow:
                        found = True
                        break
                if not found:
                    for n in known:
                        _, deep = _hit_numbers(store, parts[n], cache)
                        if val in deep():
                            found = True
                            break
                if not found and known:
                    missing.append(shown)
            for n in known:
                c = checks.setdefault(n, {"numbers": [], "missing": []})
                for shown, _ in nums:
                    if shown not in c["numbers"]:
                        c["numbers"].append(shown)
                for shown in missing:
                    if shown not in c["missing"]:
                        c["missing"].append(shown)
    out_checks = [{"n": n, "numbers": checks[n]["numbers"], "missing": checks[n]["missing"]} for n in sorted(checks)]
    ok = not unknown and not uncited and not any(c["missing"] for c in out_checks)
    return {"cited": sorted(cited), "unknown_refs": sorted(unknown), "checks": out_checks,
            "uncited_numbers": uncited if hspace else [], "ok": ok}


def has_refs(text):
    return bool(REF_RE.search(strip_code(text or "")))


# ---------------- 挂在运行时上 ----------------

def after_answer(rt, ev):
    """Runtime.emit 写完一条 assistant_message 之后调用：是最后那条回答（不带工具调用）就核对并写 grounding 事件。
    出任何错都吞掉（核对不能影响回答）。返回写下的事件或 None。"""
    try:
        p = ev.get("payload") or {}
        if p.get("tool_calls"):
            return None
        sid = ev.get("session_id")
        store = rt.store
        s = store.session(sid) if sid else None
        if not s or s.get("kind") == "sub":
            return None
        mode = modes.norm(s.get("mode"))
        text = p.get("content") or ""
        from . import kb as kbm
        if mode != modes.HSPACE:
            if not has_refs(text):
                return None
            parts = kbm.cited_parts(store, sid, upto_seq=ev["seq"])
            if not parts:
                return None             # 这个对话里没检索过资料：[1] 多半是别的意思（脚注、列表）
        else:
            parts = kbm.cited_parts(store, sid, upto_seq=ev["seq"]) if has_refs(text) else {}
        res = check(text, parts, mode, store)
        payload = {"msg_seq": ev["seq"]}
        payload.update(res)
        out = rt.emit(sid, "grounding", payload)
        # docs/借鉴开源项目.md 第 4 节：标准答案被引用的次数；Hspace 里"没答上来的问题"
        try:
            from .kb import ops
            ops.after_answer(rt, ev, res, parts, mode)
        except Exception:
            import traceback
            traceback.print_exc()
        return out
    except Exception:
        import traceback
        traceback.print_exc()
        return None


def latest(store, sid):
    """{msg_seq: grounding payload}：这个对话里每条回答的核对结果（同一条以最后一次为准）。"""
    out = {}
    for e in store.events(sid, types=("grounding",)):
        out[(e["payload"] or {}).get("msg_seq")] = e["payload"]
    return out
