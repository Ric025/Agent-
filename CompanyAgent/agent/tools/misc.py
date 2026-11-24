"""计划、提问、记忆、定时、技能、子任务。"""
from __future__ import annotations
import os, re, shutil
from . import tool, S, A, O, ToolError
from ..store import new_id, now


@tool("todo_write", "写下或更新本任务的计划清单。三步以上的任务先列计划；每完成一步就更新一次（传完整清单）",
      {"items": A("计划项", O({"text": S("内容"), "status": S("状态", ["pending", "doing", "done"])}))}, risk="none")
def todo_write(ctx, items):
    s = ctx.store.session(ctx.sid)
    s["meta"]["todo"] = items
    ctx.store.set_session(ctx.sid, meta=s["meta"])
    ctx.emit("todo", {"items": items})
    mark = {"pending": "[ ]", "doing": "[>]", "done": "[x]"}
    return "当前计划：\n" + "\n".join("%s %s" % (mark[i["status"]], i["text"]) for i in items)


@tool("ask_user", "判断不了又不能做错、或需要用户做决定时提问（能判断的不要问）。给 2 到 4 个选项",
      {"question": S("问题，一句话"), "options": A("可选答案", {"type": "string"})}, required=["question"], risk="none")
def ask_user(ctx, question, options=None):
    return {"answer": ctx.ask(question, options or [])}


@tool("memory", "管理长期记忆：记下一条、改写一条或删掉一条。只在用户明确要求记住，或说出了以后一直适用的身份、偏好、约定时使用",
      {"action": S("add 新记一条；replace 改写已有的一条；remove 删掉一条", ["add", "replace", "remove"]),
       "target": S("user：关于用户本人和公司，所有工作区通用；workspace：只和当前工作区有关", ["user", "workspace"]),
       "content": S("add、replace 时必填：要记下的内容，一句话"),
       "old_text": S("replace、remove 时必填：要改或要删的那一条里的一小段原文，能唯一认出它")},
      required=["action", "target"], risk="memory")
def memory(ctx, action, target, content=None, old_text=None):
    from .. import memory as mem
    try:
        out, ev = mem.apply(ctx.store, ctx.cfg, ctx.ws["id"], action, target, content, old_text,
                            origin="chat", session_id=ctx.sid)
    except mem.MemoryError as e:
        raise ToolError(mem.error_text(e, action, target))
    if ev:
        ctx.emit("memory", ev)
    return out


@tool("schedule_create", "创建定时任务：到点自动新开一个任务并执行给定的指令",
      {"prompt": S("到点要执行的指令，写完整，届时没有当前对话的上下文"),
       "when": S('时间规则：“每天 09:00”“每周一 09:00”“每30分钟”“每2小时”或“2026-12-01 09:00”')}, risk="memory")
def schedule_create(ctx, prompt, when):
    from ..scheduler import next_run
    try:
        nxt = next_run(when, now())
    except ValueError as e:
        raise ToolError(str(e))
    sid = new_id("t_")
    ctx.store.x("INSERT INTO schedules(id,workspace_id,prompt,spec,next_run,enabled,created) VALUES(?,?,?,?,?,1,?)",
                (sid, ctx.ws["id"], prompt, when, nxt, now()))
    import time
    return {"created": True, "id": sid, "next_run": time.strftime("%Y-%m-%d %H:%M", time.localtime(nxt))}


@tool("skill_load", "读取一个技能的完整说明。任务和某个技能相关时，先读它再动手", {"name": S("技能名")}, risk="none", sub_ok=True)
def skill_load(ctx, name):
    sk = ctx.rt.skills.get(name) if ctx.rt else None
    if not sk:
        raise ToolError("没有这个技能。可用的有：%s" % "、".join(ctx.rt.skills.names() if ctx.rt else []))
    return sk["body"][:8000]


SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")
SKILL_DESC_CHARS = 200
SKILL_BODY_CHARS = 6000


@tool("skill_save", "把一套做法存成技能，以后同类任务直接照做。只在用户同意保存之后使用",
      {"name": S("技能名：小写英文字母、数字、短横线，2 到 40 个字符，例如 monthly-reconcile"),
       "description": S("一句话：这个技能管什么事，用户说什么话的时候该用"),
       "body": S("技能正文（Markdown）：适用场景、步骤、每步要检查什么、成品长什么样。写给以后的自己看，不要写这一次的具体数字和文件名")},
      required=["name", "description", "body"], risk="skill")
def skill_save(ctx, name, description, body):
    """存到 <数据目录>/skills/<name>/SKILL.md。规格见 docs/记忆设计.md 9.1。"""
    from .. import memory as mem
    if ctx.is_sub:
        raise ToolError("子代理不能保存技能")
    name = (name or "").strip()
    if not SKILL_NAME.match(name):
        raise ToolError("技能名「%s」不合规：只能用小写英文字母、数字和短横线，以字母或数字开头，2 到 40 个字符，"
                        "例如 monthly-reconcile。换一个名字再保存。" % name)
    desc = re.sub(r"\s+", " ", description or "").strip()
    body = (body or "").strip()
    if not desc:
        raise ToolError("description 不能为空：用一句话写清这个技能管什么事、用户说什么话时该用。")
    if len(desc) > SKILL_DESC_CHARS:
        raise ToolError("description 太长了（%d 个字，上限 %d）。压缩成一句话再保存。" % (len(desc), SKILL_DESC_CHARS))
    if not body:
        raise ToolError("body 不能为空：写上适用场景、步骤、每步要检查什么、成品长什么样。")
    if len(body) > SKILL_BODY_CHARS:
        raise ToolError("正文太长了（%d 个字，上限 %d）。只留步骤和检查要点，删掉例子和这一次的具体内容再保存。"
                        % (len(body), SKILL_BODY_CHARS))
    for label, text in (("描述", desc), ("正文", body)):
        try:
            mem.scan(text, injection=False)      # 只查隐藏字符和密码密钥；技能本来就是指令，不查指令话术
        except mem.MemoryError as e:
            raise ToolError("技能的%s不能保存：%s。去掉这部分内容再保存（技能里不要写密码、密钥）。"
                            % (label, e.data.get("reason") or e))
    skills = ctx.rt.skills
    cur = skills.get(name)
    if cur is not None and cur.get("source") == "builtin":
        raise ToolError("已经有一个内置技能叫 %s，不能覆盖。换一个名字再保存。" % name)
    root = skills.dir_of("user") or os.path.join(ctx.cfg.home, "skills")
    d = os.path.join(root, name)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "SKILL.md")
    updated = os.path.isfile(path)
    if updated:
        shutil.copyfile(path, path + ".bak")     # 覆盖前留一份旧的
    text = "---\nname: %s\ndescription: %s\n---\n\n%s\n" % (name, desc, body)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)
    skills.reload()
    ctx.emit("skill", {"name": name, "description": desc, "updated": updated})
    return {"saved": True, "name": name}


@tool("spawn_agent", "派一个只读的子代理去独立完成一项调查（读文件、查资料），它只返回结论。适合可以分头做的调查",
      {"task": S("子任务说明：要查什么、要什么形式的结论。写完整，子代理看不到当前对话")}, risk="read")
def spawn_agent(ctx, task):
    if ctx.is_sub:
        raise ToolError("子代理不能再派子代理")
    return {"report": ctx.rt.run_subagent(ctx, task)}
