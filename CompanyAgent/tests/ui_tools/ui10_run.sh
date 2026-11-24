#!/bin/bash
# 用法：bash /home/claude/shot.sh <标签> [端口] [数据目录] [截图脚本]
# 起一个假模型的服务 → 用无界面 Chrome 截图到 /home/claude/shots/<标签>_*.png → 关掉服务。
# 每个人用自己的端口和数据目录，互不干扰。数据目录会保留，下次接着用（想要干净的就换一个目录）。
TAG="${1:-shot}"; PORT="${2:-18900}"; HOME_DIR="${3:-/home/claude/ca_home}"; JS="${4:-/home/claude/shot.js}"
cd "${CA_CODE:-/home/claude/ca}" || exit 1
mkdir -p "$HOME_DIR" /home/claude/shots
# 等上一次的服务把端口放开，免得服务自己换端口
for i in $(seq 1 40); do (exec 3<>/dev/tcp/127.0.0.1/$PORT) 2>/dev/null || break; sleep 0.25; done
rm -f "$HOME_DIR/token"
PYTHONDONTWRITEBYTECODE=1 python3 -m agent --port "$PORT" --home "$HOME_DIR" --mock --no-open > "$HOME_DIR/server.log" 2>&1 &
PID=$!
# 端口被占时服务会自己往后找一个：以日志里的地址为准
for i in $(seq 1 60); do
  REAL=$(grep -o "127.0.0.1:[0-9]*" "$HOME_DIR/server.log" 2>/dev/null | head -1 | cut -d: -f2)
  [ -n "$REAL" ] && [ -s "$HOME_DIR/token" ] && curl -s -o /dev/null "http://127.0.0.1:$REAL/" && break
  sleep 0.25
done
PORT="${REAL:-$PORT}"
CA_PORT="$PORT" CA_HOME="$HOME_DIR" NODE_PATH=/opt/npm-tools/node_modules timeout 900 node "$JS" "$TAG"
RC=$?
kill $PID 2>/dev/null; wait $PID 2>/dev/null
exit $RC
