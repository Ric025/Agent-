"""把 results/*.out（run.sh 的原始输出）整理成一张简表，方便核对模型的行为。用法：python3 summarize.py"""
from __future__ import annotations
import glob, json, os, re

HERE = os.path.dirname(os.path.abspath(__file__))
for f in sorted(glob.glob(os.path.join(HERE, "results", "*.out"))):
    raw = open(f, encoding="utf-8", errors="replace").read()
    m = re.search(r"__T__ (\d+) ([\d.]+) ([\d.]+)", raw)
    http, ttfb, total = (m.group(1), m.group(2), m.group(3)) if m else ("?", "?", "?")
    body = raw[:m.start()].strip() if m else raw.strip()
    name = os.path.basename(f)[:-4]
    out = {"http": http, "ttfb": ttfb, "total": total}
    try:
        if body.startswith("data:"):
            content, reasoning, calls, usage = "", "", {}, None
            for line in body.splitlines():
                if not line.startswith("data:") or "[DONE]" in line:
                    continue
                j = json.loads(line[5:])
                usage = j.get("usage") or usage
                d = (j.get("choices") or [{}])[0].get("delta") or {}
                content += d.get("content") or ""
                reasoning += d.get("reasoning_content") or d.get("reasoning") or ""
                for c in d.get("tool_calls") or []:
                    o = calls.setdefault(c.get("index", 0), {"name": "", "args": ""})
                    o["name"] += (c.get("function") or {}).get("name") or ""
                    o["args"] += (c.get("function") or {}).get("arguments") or ""
            out.update(stream=True, content=content[:300], reasoning_len=len(reasoning), tool_calls=list(calls.values()), usage=usage)
        else:
            j = json.loads(body)
            if "choices" not in j:
                out["error"] = json.dumps(j, ensure_ascii=False)[:500]
            else:
                c = j["choices"][0]
                msg = c["message"]
                r = msg.get("reasoning_content") or msg.get("reasoning") or ""
                out.update(finish=c.get("finish_reason"), usage=j.get("usage"), content=(msg.get("content") or "")[:500 if name[3:] in ("final", "inject", "kb_none", "plan", "kb_deep_write") else 240],
                           reasoning_len=len(r), reasoning_head=r[:120],
                           tool_calls=[{"name": t["function"]["name"], "args": t["function"]["arguments"][:400]} for t in msg.get("tool_calls") or []],
                           keys=sorted(msg.keys()))
    except Exception as e:
        out["parse_error"] = "%s | %s" % (e, body[:300])
    print("== %s\n%s" % (name, json.dumps(out, ensure_ascii=False)))
for extra in ("_env.txt", "_models.json"):
    p = os.path.join(HERE, "results", extra)
    if os.path.exists(p):
        print("== %s\n%s" % (extra, open(p, encoding="utf-8", errors="replace").read()[:900]))
