"""把一个任务的记录导出成 Markdown（docs/记忆设计.md 9.3）。

导出：标题、工作区、日期；按顺序的用户发言、助手答复；每次工具调用一行（显示名、参数摘要、成功或失败）；
压缩摘要；成品清单；每条回答现在的反馈（9.5）；深入研究的发言标"（深入研究）"。工具结果的正文不导出（里面可能有大段文件内容）。"""
from __future__ import annotations
import json, re, time

# 工具显示名，和界面 ui/app.js 的 TOOLS 一致
TOOL_NAMES = {
    "list_dir": "查看文件夹", "read_file": "读取文件", "write_file": "写入文件", "edit_file": "修改文件",
    "search_files": "搜索文件", "move_file": "移动文件", "delete_file": "删除文件", "excel_inspect": "查看表格结构",
    "excel_read": "读取表格", "excel_diff": "对账两张表", "write_xlsx": "生成 Excel", "write_docx": "生成 Word 文档",
    "write_pptx": "生成 PPT", "kb_add": "导入资料库", "kb_search": "检索资料库", "run_python": "运行分析代码",
    "web_fetch": "打开网页", "web_search": "联网搜索", "todo_write": "更新计划", "write_todos": "更新计划",
    "ask_user": "向你提问", "memory_save": "记住一件事", "memory": "更新记忆", "session_search": "查找以前的任务",
    "skill_save": "保存技能", "schedule_create": "创建定时任务", "skill_load": "读取技能说明", "spawn_agent": "派助手去调查",
    "system_info": "查看电脑配置",
}
RATING_TEXT = {"up": "有用", "down": "没用"}      # docs/两种模式.md 9.5 的反馈
ARG_CHARS = 60       # 每个参数值最多几个字
ARGS_CHARS = 200     # 参数摘要总长


def tool_label(name):
    return TOOL_NAMES.get(name or "", name or "（未知工具）")


def _one_line(s, n):
    s = re.sub(r"\s+", " ", s if isinstance(s, str) else json.dumps(s, ensure_ascii=False, default=str)).strip()
    return s if len(s) <= n else s[:n - 1] + "…"


def args_summary(args):
    if not isinstance(args, dict) or not args:
        return ""
    parts = ["%s=%s" % (k, _one_line(v, ARG_CHARS)) for k, v in args.items()]
    return _one_line("，".join(parts), ARGS_CHARS)


def _date(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts or 0))


def _quote(text):
    """多行文字放进引用块，免得里面的 # 之类变成标题。"""
    return "\n".join("> " + line if line.strip() else ">" for line in (text or "").strip().splitlines()) or ">"


def session_markdown(store, sid):
    s = store.session(sid)
    if not s:
        raise KeyError("没有这个任务")
    ws = store.workspace(s["workspace_id"]) or {}
    title = s["title"] or "（无标题）"
    out = ["# " + title, "", "- 工作区：%s" % (ws.get("name") or ""), "- 开始：%s" % _date(s["created"]),
           "- 最后更新：%s" % _date(s["updated"]), ""]
    events = store.events(sid)
    results = {e["payload"].get("call_id"): e["payload"] for e in events if e["type"] == "tool_result"}
    from . import feedback
    feedback_now = feedback.current_from_events(events)
    for e in events:
        p, t = e["payload"] or {}, e["type"]
        if t == "user_message":
            out += ["## 用户（%s）" % _date(e["ts"]), "", _quote(p.get("text") or "")]
            if p.get("images"):
                out.append("（附图 %d 张）" % len(p["images"]))
            if p.get("files"):
                out += [""] + ["- 附件：%s" % f for f in p["files"]]
            if p.get("skill"):
                out += ["", "- 技能：%s" % p["skill"]]
            if p.get("depth") == "deep":
                out += ["", "- （深入研究）"]
            out.append("")
        elif t == "assistant_message":
            content = (p.get("content") or "").strip()
            calls = p.get("tool_calls") or []
            if content:
                out += ["## 助手（%s）" % _date(e["ts"]), "", content, ""]
                fb = feedback_now.get(e["seq"])
                if fb:
                    out += ["- 反馈：%s%s" % (RATING_TEXT.get(fb["rating"], fb["rating"]),
                                           ("（%s）" % _one_line(fb["note"], 300)) if fb.get("note") else ""), ""]
            for c in calls:
                r = results.get(c.get("id"))
                state = "没有结果" if r is None else ("成功" if r.get("ok") else "失败")
                summ = args_summary(c.get("arguments"))
                out.append("- 工具：%s%s · %s" % (tool_label(c.get("name")), ("（%s）" % summ) if summ else "", state))
            if calls:
                out.append("")
        elif t == "question":
            out += ["- 提问：%s" % _one_line(p.get("question") or "", 300), ""]
        elif t == "answer":
            out += ["- 回答：%s" % _one_line(p.get("text") or "", 300), ""]
        elif t == "summary":
            out += ["## 较早过程的摘要", "", _quote(p.get("text") or ""), ""]
    arts = store.q("SELECT path, version, created FROM artifacts WHERE session_id=? ORDER BY created", (sid,))
    out += ["## 成品", ""]
    if arts:
        seen = {}
        for a in arts:
            seen[a["path"]] = a["version"]
        out += ["- %s（第 %s 版）" % (path, v) for path, v in seen.items()]
    else:
        out.append("（无）")
    out.append("")
    return "\n".join(out)


def file_name(title):
    """导出文件名：对话标题去掉文件名里不能用的字符；没有标题时用"对话记录"。"""
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", title or "").strip(" .") or "对话记录"
    return name[:60] + ".md"
