#!/usr/bin/env python3
"""起一个看演示数据用的服务：和 python3 -m agent 一样（参数也一样），只是跳过启动时的 recover()，
免得 seed_demo.py 写的"等你批准 / 等你回答"被改成"已暂停"。只给截图用，不要拿它当正式启动方式。
用法：python3 /home/claude/tools/serve_demo.py --port 18902 --home /home/claude/home_c --mock --no-open
环境变量 CA_CODE 指定代码目录（默认 /home/claude/ca）。"""
import os, sys
sys.dont_write_bytecode = True
sys.path.insert(0, os.environ.get("CA_CODE", "/home/claude/ca"))
from agent import runtime  # noqa: E402
runtime.Runtime.recover = lambda self: []
from agent.__main__ import main  # noqa: E402
sys.exit(main(sys.argv[1:]))
