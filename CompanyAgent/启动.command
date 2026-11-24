#!/bin/bash
# macOS：在"访达"里双击这个文件就能启动。第一次会先自动检查环境、装好需要的东西（几分钟），以后直接启动。
# 关掉：在弹出的终端窗口里按 Control + C，或者直接关掉窗口。
cd "$(dirname "$0")" || exit 1
bash start.sh "$@"
status=$?
if [ $status -ne 0 ]; then
  echo
  echo "程序退出了（代码 ${status}）。上面是原因；安装记录在 setup/安装记录.txt。按回车键关闭这个窗口。"
  read -r _
fi
