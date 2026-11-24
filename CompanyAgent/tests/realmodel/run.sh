#!/bin/bash
# 真实模型探测。只向你配置的模型地址发送 requests/ 里的测试请求，结果写入 results/。
# 用法：bash run.sh [模型地址] [模型名]。不给就用程序设置里的第一个模型（数据目录的 settings.json，或项目里的 site.json）。
cd "$(dirname "$0")" || exit 1
PY="python3"; [ -x ../../.venv/bin/python ] && PY="../../.venv/bin/python"       # 有自动装好的环境就用它（新内核在里面）
BASE="${1:-}"; MODEL="${2:-}"
if [ -z "${BASE}" ] || [ -z "${MODEL}" ]; then
  CONF="$("$PY" -c "
import sys; sys.path.insert(0, '../..')
from agent.config import Config
e = (Config().get('endpoints') or [{}])[0]
print((e.get('base_url') or '-') + ' ' + (e.get('model') or '-'))" 2>/dev/null)"
  CB="${CONF%% *}"; CM="${CONF#* }"
  [ -z "${BASE}" ] && [ "${CB}" != "-" ] && BASE="${CB}"
  [ -z "${MODEL}" ] && [ "${CM}" != "-" ] && MODEL="${CM}"
fi
if [ -z "${BASE}" ] || [ -z "${MODEL}" ]; then
  echo "不知道模型地址或模型名。先在程序的 设置 → 模型服务 里填好，或者：bash run.sh 模型地址 模型名"; exit 1
fi
echo "模型：${MODEL}  地址：${BASE}"
mkdir -p results requests; rm -f results/*.out
# 每次都按 gen.js 重新生成请求（有 node 时），新加的探测才会带上
if command -v node >/dev/null 2>&1; then node gen.js >/dev/null || echo "gen.js 运行失败，沿用 requests/ 里已有的请求"; fi
{ echo "date: $(date)"; echo "base: $BASE"; uname -a
  for t in python3 node pip3 uv dotnet; do printf "%s: " "$t"; (command -v "$t" >/dev/null 2>&1 && "$t" --version 2>&1 | head -1) || echo MISSING; done
  python3 -c "
import importlib,sys
print('py', sys.version.split()[0], sys.executable)
for m in ['openpyxl','docx','pptx','pypdf','pdfplumber','requests','playwright','sqlite3']:
    try: importlib.import_module(m); print('  ok', m)
    except Exception as e: print('  --', m)
import sqlite3; c=sqlite3.connect(':memory:'); print('sqlite', sqlite3.sqlite_version)
try: c.execute('create virtual table t using fts5(x, tokenize=\"trigram\")'); print('  fts5 trigram ok')
except Exception as e: print('  fts5 trigram FAIL', e)
" 2>&1
} > results/_env.txt 2>&1
curl -s -m 10 "$BASE/models" > results/_models.json
MODEL_ESC="$(printf '%s' "${MODEL}" | sed 's/[&|\\]/\\&/g')"
run(){ n=$(basename "$1" .json)
  sed "s|__MODEL__|${MODEL_ESC}|g" "$1" | curl -s -N -m 240 -H 'Content-Type: application/json' -d @- -w '\n__T__ %{http_code} %{time_starttransfer} %{time_total}\n' "$BASE/chat/completions" > "results/$n.out" 2>&1
  echo "  完成 $n"; }
echo "开始探测（约 1~3 分钟）..."
i=0; for f in requests/*.json; do run "$f" & i=$((i+1)); if [ $((i%3)) -eq 0 ]; then wait; fi; done; wait
echo "探测完成。接着跑端到端场景（约 5~10 分钟）..."
if command -v python3 >/dev/null 2>&1; then
  PYTHONDONTWRITEBYTECODE=1 "$PY" e2e.py "$BASE" "$MODEL" 2>&1 | tee results/_e2e_log.txt
else
  echo "这台电脑没有 python3，跳过端到端场景。" | tee results/_e2e_log.txt
fi
echo "全部完成，结果已写入 $(pwd)/results/ 。可以回到 Claude 了。"
