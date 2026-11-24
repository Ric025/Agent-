#!/bin/bash
# 整个应用的截图：bash /home/claude/tools/shots.sh <标签> [主题 id，默认 auto] [端口，默认 18902] [数据目录，默认 /home/claude/home_c]
# 先写演示数据（seed_demo.py）→ 起假模型服务（serve_demo.py，不会把等你处理的对话改成暂停）→ 截图（shots.js）→ 关服务。
# 想一次截全部主题：for t in plain gongwen celadon ink-night; do bash shots.sh v7 $t; done
TAG="${1:-app}"; THEME="${2:-auto}"; PORT="${3:-18902}"; HOME_DIR="${4:-/home/claude/home_c}"
HERE="$(cd "$(dirname "$0")" && pwd)"
CODE="${CA_CODE:-/home/claude/ca}"
mkdir -p "$HOME_DIR" /home/claude/shots
SEED="$(PYTHONDONTWRITEBYTECODE=1 python3 "$HERE/seed_demo.py" "$HOME_DIR" --code "$CODE")" || { echo "$SEED"; echo "演示数据写入失败"; exit 1; }
MAIN="$(echo "$SEED" | sed -n 's/^demo_main=//p')"; QUESTION="$(echo "$SEED" | sed -n 's/^demo_question=//p')"
cd "$CODE" || exit 1
CA_CODE="$CODE" python3 "$HERE/serve_demo.py" --port "$PORT" --home "$HOME_DIR" --mock --no-open > "$HOME_DIR/server.log" 2>&1 &
PID=$!
# 端口刚被占用（上一次的连接还没释放）时服务会自动往后找一个，所以以日志里打印的地址为准
for i in $(seq 1 60); do grep -q "在浏览器打开" "$HOME_DIR/server.log" 2>/dev/null && break; sleep 0.25; done
REAL="$(sed -n 's#.*http://127.0.0.1:\([0-9]*\)/.*#\1#p' "$HOME_DIR/server.log" | head -1)"
[ -n "$REAL" ] && [ "$REAL" != "$PORT" ] && echo "（端口 $PORT 被占用，服务用了 $REAL）" && PORT="$REAL"
for i in $(seq 1 40); do curl -s -o /dev/null "http://127.0.0.1:$PORT/" && break; sleep 0.25; done
CA_PORT="$PORT" CA_HOME="$HOME_DIR" CA_DEMO_MAIN="$MAIN" CA_DEMO_QUESTION="$QUESTION" \
  NODE_PATH=/opt/npm-tools/node_modules timeout 180 node "$HERE/shots.js" "$TAG" "$THEME"
RC=$?
kill $PID 2>/dev/null; wait $PID 2>/dev/null
exit $RC
