#!/usr/bin/env bash
# 新内核（LangChain / LangGraph）第一次在真机上运行：建虚拟环境、装依赖、逐项自检、跑全部单元测试。
# 用法（macOS / Linux）：bash tests/run_lg_tests.sh
# 结果写在 tests/results/lg_run.txt，把这个文件交给开发者即可。
# 只在项目目录下建 .venv（安装由 setup/install.sh 完成），不改系统 Python；需要能访问 PyPI（或公司的软件源，见 README“安装和环境检查”）。
set -u
cd "$(dirname "$0")/.." || exit 1
ROOT="$(pwd)"
OUT_DIR="$ROOT/tests/results"
OUT="$OUT_DIR/lg_run.txt"
mkdir -p "$OUT_DIR"
: > "$OUT"

log() { echo "$@" | tee -a "$OUT"; }

log "== 新内核测试  $(date '+%Y-%m-%d %H:%M:%S')  $(uname -sm) =="

# 1-3. 找 Python、建 .venv、装依赖：和启动时的自动安装是同一套（setup/install.sh）
log "== 安装：bash setup/install.sh --if-needed（过程见 setup/安装记录.txt） =="
bash setup/install.sh --if-needed 2>&1 | tee -a "$OUT"
RC=${PIPESTATUS[0]}
if [ "$RC" = 3 ]; then
  log "上次安装没成功、今天不自动重试；这里强制重装一遍。"
  bash setup/install.sh --force 2>&1 | tee -a "$OUT"
  RC=${PIPESTATUS[0]}
fi
VPY="$ROOT/.venv/bin/python"
if [ "$RC" != 0 ] || ! "$VPY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
  log "安装没成功，原因见上面和 setup/安装记录.txt。"
  exit 4
fi
log "使用的 Python：${VPY}（$("$VPY" -c 'import platform; print(platform.python_version())')）"
log "== 相关包的版本 =="
"$VPY" -m pip list 2>/dev/null | grep -i -E '^(langchain|langgraph|openai|httpx|mcp|pydantic|openpyxl|python-docx|python-pptx|pypdf|chromadb|numpy) ' | tee -a "$OUT"

export PYTHONUTF8=1
export PYTHONDONTWRITEBYTECODE=1

# 4. 逐项自检
log ""
log "== 逐项自检：tests/lg_smoke.py =="
"$VPY" tests/lg_smoke.py 2>&1 | tee -a "$OUT"
SMOKE=${PIPESTATUS[0]}
log ""
log "== 资料库的 LangChain 向量库自检：tests/kb_lc_smoke.py =="
"$VPY" tests/kb_lc_smoke.py 2>&1 | tee -a "$OUT"
KBLC=${PIPESTATUS[0]}
[ "$KBLC" = 0 ] || SMOKE=1

# 5. 全部单元测试（旧内核 + 新内核）
log ""
log "== 全部单元测试：python -m unittest discover -s tests -v =="
"$VPY" -m unittest discover -s tests -v 2>&1 | tee -a "$OUT"
UNIT=${PIPESTATUS[0]}

# 6. 演示模式能启动（新内核 + 假模型），5 秒后关掉
log ""
log "== 演示模式启动检查：python -m agent --mock --no-open =="
TMPH="$(mktemp -d 2>/dev/null || mktemp -d -t ca)"
"$VPY" -m agent --mock --no-open --home "$TMPH" --port 18765 >"$TMPH/out.txt" 2>&1 &
APID=$!
sleep 5
kill "$APID" 2>/dev/null
wait "$APID" 2>/dev/null
cat "$TMPH/out.txt" | tee -a "$OUT"
if grep -q "执行内核：LangChain / LangGraph" "$TMPH/out.txt"; then MOCK=0; else MOCK=1; fi

log ""
log "== 汇总 =="
log "逐项自检：$([ "$SMOKE" = 0 ] && echo 全部通过 || echo 有失败，见上方 [失败] 行)"
log "单元测试：$([ "$UNIT" = 0 ] && echo 全部通过 || echo 有失败，见上方 FAIL / ERROR)"
log "演示模式：$([ "$MOCK" = 0 ] && echo 用新内核启动成功 || echo 没有用新内核启动，见上方输出)"
log "结果文件：$OUT"
if [ "$SMOKE" = 0 ] && [ "$UNIT" = 0 ] && [ "$MOCK" = 0 ]; then exit 0; fi
exit 1
