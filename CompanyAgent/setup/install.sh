#!/usr/bin/env bash
# 检查这台电脑的环境，装好运行要用的 Python 和依赖（macOS / Linux）。
#
# 用法（在 CompanyAgent 目录下）：
#   bash setup/install.sh              检查并安装；已经装好、依赖清单没变时什么都不做
#   bash setup/install.sh --force      不管装没装过，重新检查、重新安装
#   bash setup/install.sh --if-needed  给 start.sh 用：装好了就安静地退出；上次失败不到一天不再重试
#   bash setup/install.sh --check      只做环境检查，不安装
#
# 只动项目文件夹里的 .venv/（虚拟环境）和 .tools/（自动装的 uv 和 Python），不改系统的 Python，
# 不改 shell 配置，不需要管理员密码。删掉这两个文件夹就恢复原样。
# 过程记在 setup/安装记录.txt，检查结果写在 setup/环境检查结果.txt。
# 环境变量 CA_SETUP_PYTHON=某个 Python 3.11+ 的路径：指定用它建 .venv，不自动找、不自动下载。
#
# 公司内网用自己的软件源、代理、或者离线安装：把 setup/mirror.env.example 复制成 setup/mirror.env 再改，
# 离线安装把各平台的安装包放进 setup/wheels/（见 README"安装和环境检查"一节）。
#
# 写法上兼容 macOS 自带的 bash 3.2：不用关联数组、${x,,}、mapfile。
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
cd "$ROOT" || exit 1

MODE="normal"
for a in ${1+"$@"}; do
  case "$a" in
    --force) MODE="force" ;;
    --if-needed) MODE="ifneeded" ;;
    --check) MODE="check" ;;
    -h|--help) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "不认识的参数：${a}（bash setup/install.sh --help 看用法）"; exit 64 ;;
  esac
done

PY_WANT="3.12"                 # 需要自动装 Python 时装这个版本：各个依赖都有现成的安装包
VENV="$ROOT/.venv"
VPY="$VENV/bin/python"
TOOLS="$ROOT/.tools"
STATE="$TOOLS/setup-state.json"
LOG="$HERE/安装记录.txt"
WHEELS="$HERE/wheels"
OS="$(uname -s)"
ARCH="$(uname -m)"

# ---------------- 小工具 ----------------

say() { echo "$@"; echo "$@" >> "$LOG"; }
step() { echo; echo "【$1】$2"; echo >> "$LOG"; echo "== $1 $2  $(date '+%H:%M:%S') ==" >> "$LOG"; }
log_only() { echo "$@" >> "$LOG"; }
# 跑一条命令，输出只进记录；返回命令自己的退出码
run() { echo "\$ $*" >> "$LOG"; "$@" >> "$LOG" 2>&1; }

hash_reqs() {
  { cat "$HERE/requirements-core.txt" "$HERE/requirements-extra.txt"; echo "python=$PY_WANT"; } | {
    if command -v shasum >/dev/null 2>&1; then shasum -a 256
    elif command -v sha256sum >/dev/null 2>&1; then sha256sum
    else cksum; fi
  } | awk '{print $1}'
}

json_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }

# 记下这次安装的结果。check_env.py 之后会在同一个文件里补上新内核自检的结果
write_state() {   # write_state ok(true|false) 原因 python版本 装不上的可选依赖
  mkdir -p "$TOOLS"
  printf '{"req": "%s", "ok": %s, "why": "%s", "python": "%s", "extra_failed": "%s", "at": %s}\n' \
    "$REQ_HASH" "$1" "$(json_escape "$2")" "$3" "$(json_escape "$4")" "$(date +%s)" > "$STATE"
}

py_ok() {   # py_ok 解释器 最低主版本 最低次版本：能运行且版本够
  "$1" -c "import sys; sys.exit(0 if sys.version_info >= ($2, $3) else 1)" >/dev/null 2>&1
}

py_ver() { "$1" -c 'import platform; print(platform.python_version())' 2>/dev/null; }

# macOS 上的 /usr/bin/python3 是个空壳：没装命令行工具时一调用就弹"安装开发者工具"的窗口。先确认装了再用。
usable_path() {
  p="$1"
  [ -n "$p" ] && [ -x "$p" ] || return 1
  if [ "$OS" = "Darwin" ] && [ "$p" = "/usr/bin/python3" ]; then
    xcode-select -p >/dev/null 2>&1 || return 1
  fi
  return 0
}

# 找一个够格的 Python：3.11 到 3.13 优先（3.12 最稳），更新的也接受
find_python() {
  cands=""
  for v in 3.12 3.11 3.13; do
    cands="$cands $(command -v python$v 2>/dev/null)"
    cands="$cands /Library/Frameworks/Python.framework/Versions/$v/bin/python3 /opt/homebrew/bin/python$v /usr/local/bin/python$v"
  done
  cands="$cands $(command -v python3 2>/dev/null) $(command -v python 2>/dev/null)"
  for c in $cands; do
    usable_path "$c" || continue
    if py_ok "$c" 3 11; then echo "$c"; return 0; fi
  done
  return 1
}

# 随便一个 Python 3.8+（只用来装 uv）
find_any_python() {
  for c in $(command -v python3 2>/dev/null) $(command -v python 2>/dev/null) /usr/bin/python3; do
    usable_path "$c" || continue
    if py_ok "$c" 3 8; then echo "$c"; return 0; fi
  done
  return 1
}

# 读 setup/mirror.env（不执行它，只认几个键）
load_mirror() {
  f="$HERE/mirror.env"
  [ -f "$f" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*) continue ;; esac
    k="${line%%=*}"; v="${line#*=}"
    k="$(echo "$k" | tr -d ' ')"; v="$(echo "$v" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//; s/^"//; s/"$//')"
    [ -n "$v" ] || continue
    case "$k" in
      PYPI_MIRROR) export PIP_INDEX_URL="$v"; export UV_DEFAULT_INDEX="$v"; export UV_INDEX_URL="$v" ;;
      PIP_TRUSTED_HOST) export PIP_TRUSTED_HOST="$v" ;;
      PYTHON_MIRROR) export UV_PYTHON_INSTALL_MIRROR="$v" ;;
      UV_INSTALLER_URL) UV_INSTALLER_URL="$v" ;;
      HTTPS_PROXY) export HTTPS_PROXY="$v"; export https_proxy="$v" ;;
      HTTP_PROXY) export HTTP_PROXY="$v"; export http_proxy="$v" ;;
      NO_PROXY) export NO_PROXY="$v"; export no_proxy="$v" ;;
      SYSTEM_CERTS) [ "$v" = "1" ] && { export UV_SYSTEM_CERTS=1; export UV_NATIVE_TLS=1; } ;;
      *) log_only "mirror.env 里不认识的键：${k}（忽略）" ;;
    esac
  done < "$f"
  log_only "已读 setup/mirror.env"
}

# ---------------- 先看要不要装 ----------------

mkdir -p "$TOOLS"
REQ_HASH="$(hash_reqs)"

installed_ok() {
  [ -x "$VPY" ] && py_ok "$VPY" 3 11 && [ -f "$STATE" ] \
    && grep -q "\"req\": \"$REQ_HASH\"" "$STATE" && grep -q '"ok": true' "$STATE"
}

if [ "$MODE" = "check" ]; then
  P="$VPY"; [ -x "$P" ] && py_ok "$P" 3 8 || P="$(find_python || find_any_python || true)"
  if [ -z "$P" ]; then echo "这台电脑上没有能用的 Python，先运行 bash setup/install.sh 安装。"; exit 2; fi
  exec "$P" "$HERE/check_env.py" --write
fi

if [ "$MODE" != "force" ] && installed_ok; then
  [ "$MODE" = "ifneeded" ] || echo "已经装好了（$(py_ver "$VPY")，依赖清单没有变化）。要重装：bash setup/install.sh --force"
  exit 0
fi

if [ "$MODE" = "ifneeded" ] && [ -f "$STATE" ] && grep -q "\"req\": \"$REQ_HASH\"" "$STATE" && grep -q '"ok": false' "$STATE"; then
  at="$(sed -n 's/.*"at": \([0-9][0-9]*\).*/\1/p' "$STATE")"
  if [ -n "$at" ] && [ $(( $(date +%s) - at )) -lt 86400 ]; then
    echo "上次自动安装没有成功：$(sed -n 's/.*"why": "\([^"]*\)".*/\1/p' "$STATE")"
    echo "今天先不再重试（详细记录在 setup/安装记录.txt）。修好以后重试：bash start.sh --setup"
    exit 3
  fi
fi

: > "$LOG"
log_only "安装记录  $(date '+%Y-%m-%d %H:%M:%S')  $OS $ARCH  项目：$ROOT"
echo "============================================================"
if [ "$MODE" = "ifneeded" ]; then
  echo " 第一次运行（或者依赖有变化）：先检查这台电脑的环境，装好需要的东西。"
else
  echo " 检查这台电脑的环境，装好需要的东西。"
fi
echo " 只装在项目文件夹的 .venv 和 .tools 里，不改系统，不要管理员密码。"
echo " 第一次大约 3 到 10 分钟，看网速。以后启动不会再装。"
echo "============================================================"

load_mirror
UV=""
UV_INSTALLER_URL="${UV_INSTALLER_URL:-https://astral.sh/uv/install.sh}"

fail() {   # fail 给人看的原因 [怎么办]
  write_state false "$1" "" ""
  echo
  say "没有装好：$1"
  [ -n "${2:-}" ] && say "$2"
  say "详细记录：setup/安装记录.txt"
  exit 1
}

# ---------------- 1. 电脑 ----------------

step "1/5" "检查电脑"
if [ "$OS" = "Darwin" ]; then
  say "  系统：macOS $(sw_vers -productVersion 2>/dev/null)（${ARCH}）"
  mem_gb=$(sysctl -n hw.memsize 2>/dev/null | awk '{printf "%.0f", $1/1073741824}')
elif [ "$OS" = "Linux" ]; then
  say "  系统：Linux $(uname -r)（${ARCH}）"
  mem_gb=$(awk '/MemTotal/ {printf "%.0f", $2/1048576}' /proc/meminfo 2>/dev/null)
else
  fail "不支持这个系统：$OS" "Windows 请用 start.bat（它会调用 setup/install.ps1）。"
fi
[ -n "${mem_gb:-}" ] && [ "${mem_gb:-0}" != "0" ] && say "  内存：${mem_gb} GB"
free_kb=$(df -Pk "$ROOT" 2>/dev/null | awk 'NR==2 {print $4}')
free_gb=$(( ${free_kb:-0} / 1048576 ))
say "  项目所在的磁盘还剩：${free_gb} GB"
if [ "${free_kb:-0}" -lt 1572864 ]; then
  fail "磁盘空间不够：还剩 ${free_gb} GB，安装需要大约 1.5 GB。" "腾出一些空间后重新运行。"
fi

# ---------------- 2. Python ----------------

step "2/5" "找 Python 3.11 或更新的版本"
MPY=""
if [ -n "${CA_SETUP_PYTHON:-}" ]; then      # 指定用哪个 Python（IT 统一部署时、自动测试时用）
  if py_ok "$CA_SETUP_PYTHON" 3 11; then MPY="$CA_SETUP_PYTHON"; else say "  CA_SETUP_PYTHON=$CA_SETUP_PYTHON 不能用（要 3.11 以上），改为自动找。"; fi
fi
[ -n "$MPY" ] || MPY="$(find_python || true)"
if [ -n "$MPY" ]; then
  say "  用电脑上已有的：${MPY}（$(py_ver "$MPY")）"
else
  old="$(find_any_python || true)"
  if [ -n "$old" ]; then say "  电脑上只有 Python $(py_ver "$old")，不够新。自动装一个 ${PY_WANT}（只装在项目的 .tools 里）。"
  else say "  电脑上没有能用的 Python。自动装一个 ${PY_WANT}（只装在项目的 .tools 里）。"; fi

  # uv：一个单文件的安装工具，用来装 Python 和依赖
  for c in "$TOOLS/uv/uv" "$TOOLS/uv/bin/uv" "$HERE/bin/uv" "$(command -v uv 2>/dev/null)" "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
    if [ -n "$c" ] && [ -x "$c" ] && "$c" --version >/dev/null 2>&1; then UV="$c"; break; fi
  done
  if [ -z "$UV" ]; then
    say "  下载安装工具 uv……"
    if command -v curl >/dev/null 2>&1 && run curl -LsSf --retry 2 -m 120 "$UV_INSTALLER_URL" -o "$TOOLS/uv-install.sh" \
       && run env UV_UNMANAGED_INSTALL="$TOOLS/uv" sh "$TOOLS/uv-install.sh"; then
      for c in "$TOOLS/uv/uv" "$TOOLS/uv/bin/uv"; do [ -x "$c" ] && UV="$c" && break; done
    fi
  fi
  if [ -z "$UV" ] && [ -n "$old" ]; then
    say "  换个办法：用电脑上的 Python $(py_ver "$old") 从软件源装 uv……"
    if run "$old" -m venv "$TOOLS/uvenv" && run "$TOOLS/uvenv/bin/python" -m pip install --disable-pip-version-check uv; then
      [ -x "$TOOLS/uvenv/bin/uv" ] && UV="$TOOLS/uvenv/bin/uv"
    fi
  fi
  [ -n "$UV" ] || fail "装不上安装工具 uv（连不上 astral.sh / GitHub，也连不上 Python 软件源）。" \
    "办法：从 https://www.python.org/downloads/ 装一个 Python 3.12 后重新运行；或者在 setup/mirror.env 里配公司的代理、软件源。"
  say "  uv：${UV}（$("$UV" --version 2>/dev/null)）"

  export UV_PYTHON_INSTALL_DIR="$TOOLS/python"
  say "  下载 Python ${PY_WANT}……"
  run "$UV" python install "$PY_WANT" || fail "下载 Python $PY_WANT 没成功（要能访问 GitHub，或者在 setup/mirror.env 里配 PYTHON_MIRROR）。" \
    "办法：从 https://www.python.org/downloads/ 装一个 Python 3.12 后重新运行。"
  MPY="$("$UV" python find "$PY_WANT" 2>>"$LOG" | tail -1)"
  usable_path "$MPY" && py_ok "$MPY" 3 11 || fail "装好的 Python 找不到或不能运行：$MPY"
  say "  装好了：${MPY}（$(py_ver "$MPY")）"
fi

# ---------------- 3. 虚拟环境 ----------------

step "3/5" "准备项目自己的 Python 环境（.venv）"
if [ -e "$VENV" ] && ! { [ -x "$VPY" ] && py_ok "$VPY" 3 11; }; then
  aside="$ROOT/.venv.old-$(date +%Y%m%d-%H%M%S)"
  mv "$VENV" "$aside" && say "  原来的 .venv 不能用了（Python 版本太旧或者坏了），改名留在 $(basename "$aside")，可以删掉。"
fi
if [ ! -x "$VPY" ]; then
  run "$MPY" -m venv "$VENV" || fail "建虚拟环境没成功。" "Linux 上可能要先装 python3-venv（例如 sudo apt install python3.12-venv）。"
  say "  建好了：.venv（$(py_ver "$VPY")）"
else
  say "  已有：.venv（$(py_ver "$VPY")）"
fi
PYV="$(py_ver "$VPY")"

# ---------------- 4. 依赖 ----------------

step "4/5" "安装依赖"
OFFLINE=""
if [ -d "$WHEELS" ] && ls "$WHEELS" 2>/dev/null | grep -q '\.whl$'; then
  OFFLINE="--no-index --find-links $WHEELS"
  say "  用 setup/wheels 里的安装包，不联网。"
else
  idx="${PIP_INDEX_URL:-https://pypi.org/simple}"
  code="$(curl -s -o /dev/null -m 15 -w '%{http_code}' "${idx%/}/pip/" 2>/dev/null || true)"
  [ "$code" = "000" ] && code=""
  case "$code" in
    2*|3*) say "  软件源：${idx}（连得上）" ;;
    *) fail "连不上 Python 软件源 ${idx}（返回：${code:-没有响应}）。" \
         "办法：检查网络；公司要走代理或用内部软件源的，把 setup/mirror.env.example 复制成 setup/mirror.env 填好后重新运行。" ;;
  esac
fi

# 有 uv 就用 uv 装（快得多）；否则用 pip
pip_install() {
  if [ -n "$UV" ]; then
    # shellcheck disable=SC2086
    run "$UV" pip install --python "$VPY" $OFFLINE "$@"
  else
    # shellcheck disable=SC2086
    run "$VPY" -m pip install --disable-pip-version-check $OFFLINE "$@"
  fi
}
# 先把 pip 升到新版（离线安装时不升）
if [ -z "$UV" ] && [ -z "$OFFLINE" ]; then run "$VPY" -m pip install --disable-pip-version-check --upgrade pip || true; fi

say "  必需的（LangChain / LangGraph、Office 文档、PDF）……"
pip_install -r "$HERE/requirements-core.txt" \
  || fail "必需的依赖没装上。" "常见原因：网络断了、软件源里缺包。看 setup/安装记录.txt 最后几十行。"

say "  可选的（逐个装，装不上只少对应的功能）："
EXTRA_FAILED=""
while IFS= read -r line || [ -n "$line" ]; do
  spec="${line%%#*}"; spec="${spec%%;*}"          # 去掉注释和 ; 后面的条件（.venv 一定是 3.11 以上，用不上）
  spec="$(echo "$spec" | sed 's/[[:space:]]*$//')"
  case "$spec" in ''|'#'*) continue ;; esac
  name="$(echo "$spec" | sed 's/[<>=!;~ ].*//')"
  if pip_install "$spec"; then say "    ${name}：装好了"
  else say "    ${name}：没装上"; EXTRA_FAILED="$EXTRA_FAILED $name"; fi
done < "$HERE/requirements-extra.txt"
EXTRA_FAILED="$(echo "$EXTRA_FAILED" | sed 's/^ //')"

write_state true "" "$PYV" "$EXTRA_FAILED"
log_only "已装的包："; "$VPY" -m pip list --disable-pip-version-check >> "$LOG" 2>&1 || ( [ -n "$UV" ] && "$UV" pip list --python "$VPY" >> "$LOG" 2>&1 )

# 操作网页要一个浏览器：电脑上有 Chrome（Windows 上还有 Edge）就直接用；都没有时下载 Playwright 自带的 Chromium
case " $EXTRA_FAILED " in *" playwright "*) ;; *)
  has_browser=""
  if [ "$OS" = "Darwin" ]; then
    for b in "/Applications/Google Chrome.app" "$HOME/Applications/Google Chrome.app"; do [ -d "$b" ] && has_browser=1; done
  else
    for b in google-chrome google-chrome-stable chromium chromium-browser; do command -v "$b" >/dev/null 2>&1 && has_browser=1; done
    [ -x /opt/google/chrome/chrome ] && has_browser=1
  fi
  if [ -z "$has_browser" ]; then
    say "  没找到 Chrome，下载操作网页用的 Chromium（约 150 MB）……"
    run "$VPY" -m playwright install chromium && say "    Chromium：装好了" || say "    Chromium：没装上（操作网页用不了，其余照常）"
  fi ;;
esac

# ---------------- 5. 检查 ----------------

step "5/5" "检查装好的环境"
"$VPY" "$HERE/check_env.py" --write --smoke
rc=$?
echo
if [ $rc -eq 0 ]; then
  say "装好了。以后直接运行 bash start.sh（或者双击「启动.command」）。"
else
  say "装好了，但检查发现有问题，见上面和 setup/环境检查结果.txt。程序照样能启动，有问题的功能会说明原因。"
fi
exit 0
