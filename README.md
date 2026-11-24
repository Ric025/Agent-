# Agent-

Make local things and works simple

装在员工自己电脑上的办公 Agent：说一个目标，它分步完成——读文件、对账、写 Word 和 PPT、查公司资料、操作网页。对话、资料和任务记录只存在这台电脑上；模型用公司内网的服务（OpenAI 兼容接口）。编排用 LangChain / LangGraph。

还在开发中。哪些验证过、哪些还没有，见《Agent架构与选型.md》第 10 节。

## 从哪看起

| 位置 | 内容 |
|---|---|
| [`CompanyAgent/`](CompanyAgent/) | 程序。安装、启动、配置看 [`CompanyAgent/README.md`](CompanyAgent/README.md) |
| [`Agent架构与选型.md`](Agent架构与选型.md) | 架构和选型的唯一依据：分层、选型理由、权限与安全、功能清单、没验证的事、计划 |
| [`CompanyAgent/docs/`](CompanyAgent/docs/) | 各部分的规格和实现记录 |
| `Windows本机Agent技术架构.md`、`Windows本机办公Agent设计方案.md` | 最早的两份设计文档，只作背景；和上面冲突的地方以《Agent架构与选型.md》为准 |

## 启动

```bash
cd CompanyAgent
bash start.sh          # macOS / Linux；macOS 也可以双击 启动.command。Windows 双击 start.bat
```

第一次启动会自动检查电脑、安装依赖。模型地址不在代码里：在界面"设置 → 模型服务"里填，或者把 `CompanyAgent/site.example.json` 复制成 `site.json` 填好（这个文件不进 git）。
