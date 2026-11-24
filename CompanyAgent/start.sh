#!/usr/bin/env bash
# 启动本机办公 Agent（macOS / Linux）。
# 用法：bash start.sh [--port 8765] [--home 数据目录] [--mock] [--no-open]
#   第一次运行（或者依赖清单有变化）时，先自动检查这台电脑的环境、装好 Python 和依赖（setup/install.sh），再启动。
#   --setup     不管装没装过，重新检查、重新安装，然后启动
#   --no-setup  跳过检查和安装，直接用现有的环境启动
#   --check     只检查环境，告诉你哪些功能能用、哪里有问题，不启动
# macOS 上也可以直接双击「启动.command」。
set -u
cd "$(dirname "$0")" || exit 1

SETUP="auto"
ARGS=()
for a in ${1+"$@"}; do
  case "$a" in
    --setup) SETUP="force" ;;
    --no-setup) SETUP="skip" ;;
    --check) exec bash setup/install.sh --check ;;
    *) ARGS+=("$a") ;;
  esac
done

# 1. 检查环境、需要时安装。装不上也不拦着：下面退回到电脑上已有的 Python，能用的功能照常用
if [ "$SETUP" = "force" ]; then
  bash setup/install.sh --force
elif [ "$SETUP" = "auto" ]; then
  bash setup/install.sh --if-needed
fi

py_ok() { "$1" -c "import sys; sys.exit(0 if sys.version_info >= ($2, $3) else 1)" >/dev/null 2>&1; }

# 2. 选 Python：.venv 里必需的包都在就用它（这次没装成、但以前装好的也算）；否则用电脑上已有的（3.9 以上）
PY=""
if [ -x ".venv/bin/python" ] && ".venv/bin/python" -c "import importlib.util as u, sys; sys.exit(0 if all(u.find_spec(m) for m in ('openpyxl', 'langchain', 'langgraph')) else 1)" >/dev/null 2>&1; then
  PY=".venv/bin/python"
fi
if [ -z "$PY" ]; then
  for c in python3.12 python3.11 python3.13 python3 python; do
    p="$(command -v "$c" 2>/dev/null)" || continue
    # macOS 的 /usr/bin/python3 在没装命令行工具时一调用就弹窗：先确认
    if [ "$(uname -s)" = "Darwin" ] && [ "$p" = "/usr/bin/python3" ] && ! xcode-select -p >/dev/null 2>&1; then continue; fi
    if py_ok "$p" 3 9; then PY="$p"; break; fi
  done
fi
if [ -z "$PY" ]; then
  echo
  echo "没有能用的 Python，程序起不来。"
  echo "  · 先检查网络，再运行 bash start.sh --setup 重新自动安装；"
  echo "  · 或者从 https://www.python.org/downloads/ 装一个 Python 3.12 后再运行 bash start.sh。"
  echo "  安装记录在 setup/安装记录.txt。"
  exit 1
fi
if [ "$PY" != ".venv/bin/python" ]; then
  echo
  echo "提示：没有用上自动装好的环境，现在用的是电脑上的 Python $("$PY" -c 'import platform; print(platform.python_version())')。"
  echo "      新内核、操作网页这些功能可能用不了（打开后在 设置 和 /状态 里能看到原因）。修好网络后：bash start.sh --setup"
  echo
fi

export PYTHONUTF8=1
exec "$PY" -m agent ${ARGS[@]+"${ARGS[@]}"}
