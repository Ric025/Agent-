"""计算：运行一段 Python 做数据分析。属于"执行"级别，默认要用户批准。"""
from __future__ import annotations
import os, subprocess, sys
from . import tool, S, ToolError
from .. import policy


@tool("run_python", "运行一段 Python 做计算或数据分析（当前目录是工作区根目录，可用 pandas/openpyxl）。结果用 print 输出",
      {"code": S("Python 代码")}, risk="exec")
def run_python(ctx, code):
    scratch = os.path.join(ctx.root, policy.INTERNAL_DIR, "scratch", ctx.sid)
    os.makedirs(scratch, exist_ok=True)
    script = os.path.join(scratch, "run_%d.py" % len(os.listdir(scratch)))
    with open(script, "w", encoding="utf-8") as f:
        f.write(code)
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    try:
        p = subprocess.run([sys.executable, script], cwd=ctx.root, env=env, capture_output=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise ToolError("运行超过 120 秒，已终止")
    out = p.stdout.decode("utf-8", "replace")
    err = p.stderr.decode("utf-8", "replace")
    return {"exit_code": p.returncode, "stdout": out[-4000:], "stderr": err[-1500:]}
