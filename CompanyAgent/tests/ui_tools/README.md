> **这个目录是什么。** 界面是在一台有无界面 Chrome 的 Linux 机器上渲染出来截图审看的，这里是当时用的工具，留作以后改界面时用。
> 脚本里的路径按那台机器写的：项目副本在 `/home/claude/ca`，工具在 `/home/claude/tools`（`shot.sh`、`shot.js` 在 `/home/claude/`），截图存 `/home/claude/shots`；
> 需要 Node 的 `playwright` 包和一个 Chrome（`/usr/bin/google-chrome`）。换机器用之前先改这些路径。日常使用和测试不需要这个目录。
> `e2e_slash.js` 是斜杠指令的联调脚本：`bash shot.sh e2e 18906 <数据目录> e2e_slash.js`，对着真实服务（假模型）走 18 步。

# 审看工具

都在云端工作区里用，不碰用户电脑。代码目录默认 `/home/claude/ca`，图都存到 `/home/claude/shots/`，用 Read 工具看 PNG。

## 主题画廊：`gallery.html` + `gallery_shots.js`

一个独立的 HTML 页，引用 `../ca/ui/themes.css` 和 `../ca/ui/theme.js`，把十个主题各画成一块小样：侧栏（新对话、几条对话、当前项、底部导航）、
用户气泡（带技能和附件标签）、"做了 5 步"、助手正文（小标题、链接、行内代码、表格、代码块）、等批准的卡片（同意 / 不同意 / 一个带焦点环的按钮）、
成功 / 警告 / 出错三种小标签、次要和弱文字、输入框（+、麦克风、发送）。

```bash
cd /home/claude/tools && NODE_PATH=/opt/npm-tools/node_modules node gallery_shots.js [前缀，默认 gallery]
```

出图：`gallery_light.png`、`gallery_dark.png`（总览，各五个）和 `gallery_<主题id>.png`（每个主题一张 880 宽的大图）。
浏览器里直接开也行：`gallery.html?group=light`、`?group=dark`、`?only=nord`。
页里的样式只用主题变量，可以当重做时组件长相的参考（比如"等你处理"标签用 `--accent` 实底、批准卡片左边一道 `--accent` 竖条）。

## 演示数据：`seed_demo.py`

```bash
python3 /home/claude/tools/seed_demo.py /home/claude/home_c      # 可加 --code <代码目录>
```

用 `agent.store` / `agent.memory` 的接口直接往账本里写（重复运行会先删掉上次的演示数据）：

- 「三季度各区域销售汇报」（等你批准）：带两个附件和"技能：slides"的用户发言；计划清单；9 步工具调用，其中"读取文件"出错；
  一次压缩摘要；带标题、有序列表、表格、引用、链接、代码块、分隔线的长回答；两个成品文件；记忆、技能、"存成技能"建议三种提示条；
  一张已回答的提问卡片；最后一张等批准的卡片（覆盖写 `汇报/三季度销售汇报.md`）。
- 「供应商合同到期提醒」（等你回答）：一张还没回答的提问卡片，四个选项。
- 三个已完成的旧对话；"关于我"四条、"这个文件夹"两条记忆；两个定时任务；工作文件夹里真实的 xlsx / pptx / md 小文件。
- 最后两行打印 `demo_main=<编号>`、`demo_question=<编号>`，对话页地址是 `#/task/<编号>`。

正式服务启动时会把"等你批准 / 等你回答"的对话改成"已暂停"。看演示数据要用 `serve_demo.py` 起服务（参数和 `python3 -m agent` 一样，只是跳过这一步）：

```bash
cd /home/claude/ca && python3 /home/claude/tools/serve_demo.py --port 18902 --home /home/claude/home_c --mock --no-open
```

## 整个应用的截图：`shots.sh` + `shots.js`

```bash
bash /home/claude/tools/shots.sh <标签> [主题id，默认 auto] [端口，默认 18902] [数据目录，默认 /home/claude/home_c]
# 例：for t in plain gongwen celadon ink-night; do bash /home/claude/tools/shots.sh v7 $t; done
```

顺序：`seed_demo.py` → `serve_demo.py` → `shots.js` → 关服务。端口被占用时服务会自己往后找一个，脚本以日志里的地址为准。
主题用 `addInitScript` 在页面加载前写进 `localStorage['ca.theme']`（`auto` 就删掉这个键）；`index.html` 接上 `theme.js` 和 `themes.css` 之后才会真的变样。

出图 `<标签>_<主题>_<序号>_<页面>.png`，1440×900：

| 序号 | 页面 |
|---|---|
| 01_new | 新对话页 |
| 02_demo_top / 03_demo_mid | 演示对话顶部 / 中段 |
| 04_demo_bottom_steps | 演示对话底部（步骤都点开了；底部有提问卡片、记忆条和等批准的卡片） |
| 04b_demo_top_steps | 演示对话顶部，第一组步骤展开（能看到出错的那一步） |
| 05_question | 等你回答的对话 |
| 06_memory / 07_skills / 08_schedule / 09_kb / 10_settings | 各个页面 |
| 11_narrow_new / 12_narrow_demo | 420×820 的新对话页和演示对话底部 |

页面报错（脚本异常、控制台错误、4xx / 5xx 请求）会打在那一行后面，并让脚本以非 0 退出。
重做后如果"做了 N 步"那一行或侧栏的选择器变了，改 `shots.js` 开头的 `SEL`。对话区靠"最大的能滚动的元素"来滚，一般不用改。

## 主题测试

```bash
cd /home/claude/ca && node tests/theme_test.mjs && node --check ui/theme.js
```
