#!/usr/bin/env python3
"""往账本里直接写一套演示数据，供截图和审看界面用。

用法：python3 /home/claude/tools/seed_demo.py <数据目录> [--code /home/claude/ca]

写进去的东西（全部用 agent.store / agent.memory 的接口写，界面照常能画）：
  - 一个内容丰富的对话「三季度各区域销售汇报」（状态：等你批准）：
      带附件和技能标签的用户发言；计划清单；多步工具调用（其中一步出错）；一次压缩摘要；
      带标题、列表、表格、代码块、引用、链接的长回答；成品文件卡片（两个版本）；
      记忆、技能、建议三种提示条；一张已回答的提问卡片；一张等批准的卡片（覆盖写文件）。
  - 一个「供应商合同到期提醒」（状态：等你回答），里面有一张还没回答的提问卡片。
  - 三个已完成的旧对话（只有一问一答），让侧栏不空。
  - 几条记忆（关于我 / 这个文件夹）、两个定时任务、工作文件夹里几份真实的小文件。
重复运行会先删掉上次写的演示对话、演示记忆和演示定时，再重写。

注意：真的服务启动时会把"等你批准 / 等你回答"的对话一律改成"已暂停"（程序重启后的保护）。
截图时用 /home/claude/tools/serve_demo.py 启动服务，它跳过这一步，卡片才会保持原样。
"""
from __future__ import annotations
import argparse, json, os, sys, time

ap = argparse.ArgumentParser()
ap.add_argument("home")
ap.add_argument("--code", default="/home/claude/ca")
a = ap.parse_args()
sys.path.insert(0, a.code)
sys.dont_write_bytecode = True

from agent import config as config_mod          # noqa: E402
from agent.config import Config                 # noqa: E402
from agent.store import Store, new_id, now      # noqa: E402
from agent import memory as mem                 # noqa: E402

os.makedirs(a.home, exist_ok=True)
cfg = Config(a.home)
store = Store(cfg.home)

# ---------- 默认文件夹 ----------
ws = store.default_workspace()
if ws is None:
    path = config_mod.default_folder(cfg)
    os.makedirs(path, exist_ok=True)
    ws = store.add_workspace("默认", os.path.realpath(path), is_default=True)
root = ws["path"]
os.makedirs(root, exist_ok=True)
WID = ws["id"]

# ---------- 清掉上次的演示数据 ----------
old = [r["id"] for r in store.q("SELECT id, meta FROM sessions") if json.loads(r["meta"] or "{}").get("demo")]
for sid in old:
    store.delete_fts(sid)
    store.x("DELETE FROM events WHERE session_id=?", (sid,))
    store.x("DELETE FROM actions WHERE session_id=?", (sid,))
    store.x("DELETE FROM artifacts WHERE session_id=?", (sid,))
    store.x("DELETE FROM memory_log WHERE session_id=?", (sid,))
    store.x("DELETE FROM memories WHERE session_id=?", (sid,))
    store.x("DELETE FROM sessions WHERE id=?", (sid,))

USER_MEM = [
    "我是华东大区销售总监，向集团 COO 汇报，常看的是各区域月度和季度业绩。",
    "汇报材料一律用公司模板，金额单位用万元，保留一位小数。",
    "周报在每周一上午 10 点前发给区域经理群，抄送 HRBP。",
]
WS_MEM = [
    "「资料」里的销售明细每月 5 日更新，口径是含税出库金额。",
    "华南、华东、华北、西南四个区域；西北并入西南统计。",
]
REVIEW_MEM = "汇报的第一页放结论，不放目录。"
for t in USER_MEM + WS_MEM + [REVIEW_MEM]:
    for r in store.q("SELECT id FROM memories WHERE text=?", (t,)):
        store.x("DELETE FROM memory_log WHERE memory_id=?", (r["id"],))
        store.x("DELETE FROM memories WHERE id=?", (r["id"],))
SCHEDULES = (("每周一汇总上周各区域销售，做成一页简报发给我", "每周一 08:30"),
             ("每天检查有没有 30 天内到期的供应商合同", "每天 09:00"))
for prompt, _ in SCHEDULES:
    store.x("DELETE FROM schedules WHERE prompt=?", (prompt,))

# ---------- 工作文件夹里的真实小文件 ----------
def write(rel, data):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    mode = "wb" if isinstance(data, bytes) else "w"
    with open(p, mode, **({} if mode == "wb" else {"encoding": "utf-8"})) as f:
        f.write(data)
    return p

REGIONS = [("华南", 14260.4, 12688.1), ("华东", 11830.2, 12413.6), ("华北", 12105.9, 11496.3), ("西南", 10018.7, 9116.0)]
try:
    import openpyxl
    wb = openpyxl.Workbook()
    sh = wb.active
    sh.title = "销售明细"
    sh.append(["月份", "区域", "客户", "产品线", "销售额（万元）"])
    for m in ("7月", "8月", "9月"):
        for r, cur, _ in REGIONS:
            sh.append([m, r, "示例客户", "工业自动化", round(cur / 3, 1)])
    os.makedirs(os.path.join(root, "资料"), exist_ok=True)
    wb.save(os.path.join(root, "资料", "三季度销售明细.xlsx"))
except Exception:
    write("资料/三季度销售明细.xlsx", b"")
REPORT_MD = """# 三季度销售汇报

## 结论
全国销售额 48,215 万元，同比增长 6.1%；华东同比下降 4.7%，是唯一下滑的区域。

## 各区域
| 区域 | 三季度（万元） | 同比 |
|---|---:|---:|
| 华南 | 14,260.4 | +12.4% |
| 华北 | 12,105.9 | +5.3% |
| 华东 | 11,830.2 | -4.7% |
| 西南 | 10,018.7 | +9.9% |
"""
write("汇报/三季度销售汇报.md", REPORT_MD)
try:
    from pptx import Presentation
    prs = Presentation()
    for title in ("三季度结论", "各区域业绩", "华东为什么下滑", "四季度要做的三件事", "附：数据口径"):
        s = prs.slides.add_slide(prs.slide_layouts[1])
        s.shapes.title.text = title
    prs.save(os.path.join(root, "汇报", "三季度销售汇报.pptx"))
except Exception:
    write("汇报/三季度销售汇报.pptx", b"")

# ---------- 写事件的小工具：时间往前倒，步骤耗时看起来真实 ----------
class Clock:
    def __init__(self, start):
        self.t = start

    def tick(self, sec):
        self.t += sec
        return self.t

def ev(sid, clock, sec, etype, payload):
    e = store.add_event(sid, etype, payload)
    store.x("UPDATE events SET ts=? WHERE seq=?", (clock.tick(sec), e["seq"]))
    return e

def session(title, status, minutes_ago, meta=None):
    s = store.add_session(WID, title=title, meta=dict(meta or {}, demo=True))
    t = time.time() - minutes_ago * 60
    store.x("UPDATE sessions SET status=?, created=?, updated=? WHERE id=?", (status, t, t + 60, s["id"]))
    return s["id"], Clock(t)

def call(sid, clock, cid, tool, args, ok, content, risk="read", secs=(1.2, 2.4)):
    """一次工具调用：动作记录 + 开始 + 结果。"""
    act = store.add_action(sid, 1, cid, tool, args, risk, state="done" if ok else "failed")
    ev(sid, clock, secs[0], "tool_started", {"action_id": act["id"], "call_id": cid, "tool": tool, "args": args})
    ev(sid, clock, secs[1], "tool_result", {"call_id": cid, "action_id": act["id"], "tool": tool, "ok": ok, "content": content})
    return act

def assistant(sid, clock, sec, content, calls=()):
    p = {"content": content, "tool_calls": [{"id": c[0], "name": c[1], "arguments": json.dumps(c[2], ensure_ascii=False)}
                                            for c in calls]}
    return ev(sid, clock, sec, "assistant_message", p)

def artifact(sid, clock, rel, note):
    p = os.path.join(root, rel)
    prev = store.one("SELECT MAX(version) AS v FROM artifacts WHERE workspace_id=? AND path=?", (WID, rel))
    ver = (prev["v"] or 0) + 1
    aid = new_id("f_")
    store.x("INSERT INTO artifacts(id,session_id,workspace_id,path,version,hash,size,note,created) VALUES(?,?,?,?,?,?,?,?,?)",
            (aid, sid, WID, rel, ver, "demo%012d" % ver, os.path.getsize(p) if os.path.exists(p) else 0, note, clock.t))
    ev(sid, clock, 0.4, "artifact", {"id": aid, "path": rel, "version": ver, "note": note})

def remember(sid, clock, target, text, origin):
    out, payload = mem.apply(store, cfg, WID, "add", target, text, origin=origin, session_id=sid)
    if sid and payload:
        ev(sid, clock, 0.6, "memory", payload)

# ---------- 旧对话（侧栏用） ----------
OLD = [
    ("董事会会议纪要整理", 60 * 50, "把 11 月 18 日董事会的录音转写整理成纪要，按议题分段，决议单独列出来。",
     "纪要整理好了，共 4 个议题、3 项决议，存在 `纪要/2026-11-18 董事会纪要.docx`。决议里第 2 项（华东渠道调整）需要你在周五前给出方案。"),
    ("招聘进度周报", 60 * 75, "汇总一下本周各区域销售岗的招聘进度。",
     "本周共发出 6 个录用通知，到岗 2 人。华东缺口最大，还差 4 个客户经理。"),
    ("核对 10 月差旅报销", 60 * 5, "核对 10 月差旅报销单和行程记录，把对不上的列出来。",
     "核对了 132 张报销单，有 3 张和行程对不上，已列在 `报销/10月差异.xlsx` 里。"),
]
for title, ago, q, ans in OLD:
    sid, c = session(title, "done", ago)
    ev(sid, c, 0, "user_message", {"text": q})
    assistant(sid, c, 18, ans)
    ev(sid, c, 0.2, "status", {"status": "done"})

# ---------- 等你回答的对话 ----------
sid2, c2 = session("供应商合同到期提醒", "waiting_input", 40)
ev(sid2, c2, 0, "user_message", {"text": "查一下明年一季度到期的供应商合同，按金额从大到小列出来。"})
assistant(sid2, c2, 4, "", [("q1", "search_files", {"query": "合同", "glob": "*.pdf"})])
call(sid2, c2, "q1", "search_files", {"query": "合同", "glob": "*.pdf"}, True, "找到 27 份：合同/2025-采购-*.pdf 等")
assistant(sid2, c2, 6, "找到 27 份合同，其中 9 份在明年一季度到期。", [("q2", "ask_user", {"question": "到期提醒要发给谁？"})])
ev(sid2, c2, 0.3, "question", {"call_id": "q2", "question": "到期提醒要发给谁？发之前我会先给你看草稿。",
                               "options": ["只发给我", "发给采购部负责人", "发给各合同的经办人", "先不发，只列清单"]})

# ---------- 主对话 ----------
sid, c = session("三季度各区域销售汇报", "waiting_approval", 25)
TODO = [{"text": "读销售明细，按区域汇总", "status": "done"},
        {"text": "找出华东下滑的原因", "status": "done"},
        {"text": "写 5 页汇报", "status": "done"},
        {"text": "补一页华东客户明细", "status": "doing"}]
store.set_session(sid, meta={"demo": True, "todo": TODO})
store.x("UPDATE sessions SET status='waiting_approval' WHERE id=?", (sid,))

ev(sid, c, 0, "user_message", {"text": "把三季度各区域销售数据做成 5 页汇报，重点讲华东为什么下滑。下周一要给 COO 看。",
                               "files": ["资料/三季度销售明细.xlsx", "资料/华东大客户名单.xlsx"], "skill": "slides"})
assistant(sid, c, 3, "", [("c0", "skill_load", {"name": "slides"})])
call(sid, c, "c0", "skill_load", {"name": "slides"}, True, "已读取技能「slides」的说明。", risk="none", secs=(0.2, 0.3))
assistant(sid, c, 4, "", [("c1", "todo_write", {"items": TODO[:3]})])
call(sid, c, "c1", "todo_write", {"items": TODO[:3]}, True, "当前计划：…", risk="none", secs=(0.1, 0.1))
ev(sid, c, 0.1, "todo", {"items": TODO[:3]})
assistant(sid, c, 5, "先看一下销售明细的结构。", [("c2", "excel_inspect", {"path": "资料/三季度销售明细.xlsx"})])
call(sid, c, "c2", "excel_inspect", {"path": "资料/三季度销售明细.xlsx"}, True,
     "工作表「销售明细」：2,410 行，列：月份、区域、客户、产品线、销售额（万元）", secs=(0.6, 1.8))
assistant(sid, c, 6, "", [("c3", "read_file", {"path": "资料/华东大客户名单.xlsx"})])
call(sid, c, "c3", "read_file", {"path": "资料/华东大客户名单.xlsx"}, False,
     "找不到文件：资料/华东大客户名单.xlsx。文件夹里有：三季度销售明细.xlsx", secs=(0.4, 0.6))
assistant(sid, c, 5, "大客户名单不在文件夹里，我改用明细里的客户列来算。",
          [("c4", "run_python", {"code": "import pandas as pd\ndf = pd.read_excel('资料/三季度销售明细.xlsx')\nprint(df.groupby('区域')['销售额（万元）'].sum())"})])
call(sid, c, "c4", "run_python", {"code": "df.groupby('区域')['销售额（万元）'].sum()"}, True,
     "区域\n华南    14260.4\n华北    12105.9\n华东    11830.2\n西南    10018.7", risk="exec", secs=(0.8, 6.2))
assistant(sid, c, 4, "", [("c5", "kb_search", {"query": "华东 大客户 延期 下单"})])
call(sid, c, "c5", "kb_search", {"query": "华东 大客户 延期 下单"}, True,
     "1. 《华东 9 月经营分析会纪要》第 3 页：两家汽车零部件客户因产线改造推迟到 11 月下单……", secs=(0.5, 1.4))
first_seq = store.last_seq()
ev(sid, c, 2, "summary", {"text": "较早的过程：读了销售明细（2,410 行），按区域汇总了三季度销售额；大客户名单文件不存在，改用明细里的客户列；"
                                  "从资料库里找到华东 9 月经营分析会纪要，确认两家大客户推迟下单。", "upto_seq": first_seq})

ANSWER = """## 三季度概况

全国销售额 **48,215 万元**，同比增长 6.1%。四个区域里只有华东下滑。

| 区域 | 三季度（万元） | 去年同期（万元） | 同比 |
|---|---:|---:|---:|
| 华南 | 14,260.4 | 12,688.1 | +12.4% |
| 华北 | 12,105.9 | 11,496.3 | +5.3% |
| 华东 | 11,830.2 | 12,413.6 | -4.7% |
| 西南 | 10,018.7 | 9,116.0 | +9.9% |

### 华东为什么下滑

1. **两家大客户推迟下单。** 两家汽车零部件客户因为产线改造，把原定 9 月的订单推到了 11 月，影响约 820 万元。
2. **渠道调整的过渡期。** 7 月起华东撤掉了两家二级经销商，新渠道 8 月才开始出货。
3. 除去这两项，华东同比是 +2.1%，基本盘没有问题。

> 9 月经营分析会纪要里写明："两家客户的订单已确认顺延至 11 月，金额不变。"

数据口径见 [销售明细说明](https://intranet.example.com/sales/glossary)，汇总用的是 `销售额（万元）` 这一列：

```python
df.groupby("区域")["销售额（万元）"].sum().sort_values(ascending=False)
```

---

汇报做成了 5 页：结论、各区域业绩、华东为什么下滑、四季度要做的三件事、数据口径。"""
assistant(sid, c, 9, "", [("c6", "write_pptx", {"path": "汇报/三季度销售汇报.pptx", "slides": "（5 页大纲）"})])
call(sid, c, "c6", "write_pptx", {"path": "汇报/三季度销售汇报.pptx", "slides": "（5 页大纲）"}, True,
     "已生成 5 页：汇报/三季度销售汇报.pptx", risk="create", secs=(0.5, 3.1))
artifact(sid, c, "汇报/三季度销售汇报.pptx", "5 页，第一页是结论")
assistant(sid, c, 7, "", [("c7", "write_file", {"path": "汇报/三季度销售汇报.md", "content": REPORT_MD})])
call(sid, c, "c7", "write_file", {"path": "汇报/三季度销售汇报.md", "content": REPORT_MD}, True,
     "已写入 汇报/三季度销售汇报.md", risk="create", secs=(0.3, 0.4))
artifact(sid, c, "汇报/三季度销售汇报.md", "文字版，方便转发")
assistant(sid, c, 12, ANSWER)
remember(sid, c, "user", USER_MEM[1], "chat")
ev(sid, c, 0.5, "skill", {"name": "slides", "description": "把数据和要点做成 16:9 的汇报，第一页放结论", "updated": True})
ev(sid, c, 0.5, "suggestion", {"kind": "save_skill", "text": "这次的做法可以存成技能「季度销售汇报」，下次同类的事直接照做。"})
ev(sid, c, 1, "status", {"status": "done"})

ev(sid, c, 90, "user_message", {"text": "华东那页再加一张客户明细表，前 10 大客户，标出推迟下单的两家。"})
ev(sid, c, 0.2, "status", {"status": "running"})
assistant(sid, c, 4, "", [("c8", "ask_user", {"question": "客户明细按什么排？"})])
ev(sid, c, 0.3, "question", {"call_id": "c8", "question": "前 10 大客户按什么排？",
                             "options": ["三季度销售额", "全年累计销售额", "同比降幅"]})
ev(sid, c, 20, "answer", {"call_id": "c8", "text": "三季度销售额"})
call(sid, c, "c8", "ask_user", {"question": "客户明细按什么排？"}, True, "用户回答：三季度销售额", risk="none", secs=(0.1, 0.1))
ev(sid, c, 0.1, "todo", {"items": TODO})
assistant(sid, c, 3, "", [("c8b", "excel_read", {"path": "资料/三季度销售明细.xlsx", "sheet": "销售明细", "query": "区域 == '华东'"})])
call(sid, c, "c8b", "excel_read", {"path": "资料/三季度销售明细.xlsx", "sheet": "销售明细", "query": "区域 == '华东'"}, True,
     "华东共 612 行，按客户汇总后取前 10。", secs=(0.4, 2.2))
assistant(sid, c, 2, "", [("c8c", "run_python", {"code": "top10 = east.groupby('客户')['销售额（万元）'].sum().nlargest(10)"})])
call(sid, c, "c8c", "run_python", {"code": "top10 = east.groupby('客户')['销售额（万元）'].sum().nlargest(10)"}, True,
     "苏州某汽车零部件 1204.5 …", risk="exec", secs=(0.6, 4.8))
remember(sid, c, "user", REVIEW_MEM, "review")
NEW_MD = REPORT_MD + """
## 华东前 10 大客户（三季度）
| 排名 | 客户 | 三季度（万元） | 备注 |
|---:|---|---:|---|
| 1 | 苏州某汽车零部件 | 1,204.5 | 推迟到 11 月下单 |
| 2 | 宁波某精密机械 | 986.0 | |
| 3 | 无锡某汽车电子 | 912.3 | 推迟到 11 月下单 |
"""
assistant(sid, c, 8, "排好了。要把客户明细加进文字版汇报，会覆盖原来的文件（旧版本会自动留一份）。",
          [("c9", "write_file", {"path": "汇报/三季度销售汇报.md", "content": NEW_MD})])
act = store.add_action(sid, 1, "c9", "write_file", {"path": "汇报/三季度销售汇报.md", "content": NEW_MD}, "overwrite", state="awaiting")
ev(sid, c, 0.3, "approval_requested", {"action_id": act["id"], "call_id": "c9", "tool": "write_file",
                                       "args": {"path": "汇报/三季度销售汇报.md", "content": NEW_MD}, "risk": "overwrite"})
ev(sid, c, 0.1, "status", {"status": "waiting_approval"})
store.x("UPDATE sessions SET status='waiting_approval', updated=? WHERE id=?", (c.t, sid))
store.x("UPDATE sessions SET updated=? WHERE id=?", (c2.t, sid2))

# ---------- 不属于对话的记忆和定时 ----------
for t in USER_MEM[:1] + USER_MEM[2:]:
    remember(None, None, "user", t, "user")
for t in WS_MEM:
    remember(None, None, "workspace", t, "user")

from agent.scheduler import next_run   # noqa: E402
for prompt, spec in SCHEDULES:
    store.x("INSERT INTO schedules(id,workspace_id,prompt,spec,next_run,enabled,created) VALUES(?,?,?,?,?,1,?)",
            (new_id("t_"), WID, prompt, spec, next_run(spec, time.time()), now()))

store.close()
print("演示数据已写入：%s（文件夹 %s）" % (cfg.home, root))
print("demo_main=%s" % sid)          # shots.sh 读这两行
print("demo_question=%s" % sid2)
