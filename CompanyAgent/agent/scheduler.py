"""定时任务：到点新开一个任务。时间规则用大白话。"""
from __future__ import annotations
import re, threading, time

WEEK = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6,
        "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def next_run(spec, after):
    """返回 after 之后的下一次触发时间戳。认不出规则时抛 ValueError。"""
    s = spec.strip().lower().replace("：", ":")
    m = re.match(r"^(?:每|every\s*)(\d+)\s*(分钟|小时|m|h|min|hour)s?$", s)
    if m:
        step = int(m.group(1)) * (60 if m.group(2) in ("分钟", "m", "min") else 3600)
        if step < 60:
            raise ValueError("间隔不能小于 1 分钟")
        return after + step
    m = re.match(r"^(?:每天|daily)\s*(\d{1,2}):(\d{2})$", s)
    if m:
        t = time.localtime(after)
        cand = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, int(m.group(1)), int(m.group(2)), 0, 0, 0, -1))
        return cand if cand > after else cand + 86400
    m = re.match(r"^(?:每周|weekly\s*)(\S+?)\s*(\d{1,2}):(\d{2})$", s)
    if m and m.group(1) in WEEK:
        t = time.localtime(after)
        cand = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, int(m.group(2)), int(m.group(3)), 0, 0, 0, -1))
        days = (WEEK[m.group(1)] - t.tm_wday) % 7
        cand += days * 86400
        return cand if cand > after else cand + 7 * 86400
    m = re.match(r"^(?:once\s*)?(\d{4})-(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})$", s)
    if m:
        y, mo, d, h, mi = map(int, m.groups())
        cand = time.mktime((y, mo, d, h, mi, 0, 0, 0, -1))
        if cand <= after:
            raise ValueError("这个时间已经过去了")
        return cand
    raise ValueError("看不懂时间规则「%s」。可以写：每天 09:00、每周一 09:00、每30分钟、每2小时、2026-12-01 09:00" % spec)


def is_once(spec):
    return bool(re.match(r"^(?:once\s*)?\d{4}-", spec.strip().lower()))


class Scheduler(threading.Thread):
    def __init__(self, rt, tick=20):
        super().__init__(daemon=True)
        self.rt, self.tick, self.stop = rt, tick, threading.Event()

    def run(self):
        while not self.stop.wait(self.tick):
            try:
                self.fire_due()
            except Exception:
                pass
            try:
                # 顺带看一眼有没有空闲下来、该做后台回顾的任务（docs/记忆设计.md 5.1）
                reviewer = getattr(self.rt, "reviewer", None)
                if reviewer is not None:
                    reviewer.scan_idle()
            except Exception:
                pass

    def fire_due(self, at=None):
        at = at or time.time()
        fired = []
        for sc in self.rt.store.q("SELECT * FROM schedules WHERE enabled=1 AND next_run<=?", (at,)):
            s = self.rt.store.add_session(sc["workspace_id"], "定时：" + sc["prompt"][:24], meta={"schedule_id": sc["id"]})
            if is_once(sc["spec"]):
                self.rt.store.x("UPDATE schedules SET enabled=0, last_run=? WHERE id=?", (at, sc["id"]))
            else:
                self.rt.store.x("UPDATE schedules SET next_run=?, last_run=? WHERE id=?", (next_run(sc["spec"], at), at, sc["id"]))
            self.rt.send_message(s["id"], sc["prompt"])
            fired.append(s["id"])
        return fired
