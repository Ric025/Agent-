"""中间件：我们自己的一层（KernelMiddleware）加官方中间件，组装成 create_agent 的 middleware 列表。

执行顺序（官方文档 middleware/custom）：before_* 按列表顺序；wrap_model_call 第一个包住后面所有的；
after_model 倒序。所以 KernelMiddleware 放第一个：它最外层包住模型调用（负责打断和记账），
它的 after_model 最后执行（批准中间件先判批准，再轮到它处理提问）。"""
from __future__ import annotations

from langchain.agents.middleware import (AgentMiddleware, HumanInTheLoopMiddleware, SummarizationMiddleware,
                                         TodoListMiddleware, ModelFallbackMiddleware, ModelRetryMiddleware,
                                         ToolRetryMiddleware, ModelCallLimitMiddleware, ToolCallLimitMiddleware)

from .. import context
from .kernel import TODO_TOOL

TODO_PROMPT = ("# 计划清单\n三步以上的任务，先用 write_todos 列出计划；每完成一步就更新一次，每次传完整清单。"
               "同一时间只把一项标成 in_progress。简单的一两步任务不要列计划。")
TODO_DESC = ("写下或更新本任务的计划清单。三步以上的任务先列计划；每完成一步就更新一次（传完整清单）。"
             "每项的状态：pending 未开始，in_progress 正在做，completed 已完成。")
SUMMARY_PROMPT = context.COMPRESS_PROMPT + "\n\n<messages>\n{messages}\n</messages>"
APPROVAL_DESC = "这一步需要你批准"


class KernelMiddleware(AgentMiddleware):
    """把 kernel.TaskKernel 挂到 create_agent 上。只有三个钩子，逻辑都在内核里。"""

    def __init__(self, kernel):
        super().__init__()
        self.k = kernel

    def wrap_model_call(self, request, handler):
        return self.k.model_call(request, handler)

    def wrap_tool_call(self, request, handler):
        return self.k.run_tool(request, handler)

    def after_model(self, state, runtime):
        return self.k.after_model(state)


class LedgerSummarization(SummarizationMiddleware):
    """官方的压缩中间件，压缩后在账本里记一条 summary 事件（界面显示"较早的过程已整理成摘要"）。"""

    def __init__(self, kernel, **kwargs):
        super().__init__(**kwargs)
        self._k = kernel

    def before_model(self, state, runtime):
        out = super().before_model(state, runtime)
        if out and isinstance(out, dict):
            try:
                self._k.on_summary(out.get("messages") or [])
            except Exception as e:      # 记账失败不影响压缩本身
                print("[lg] 记录摘要事件失败：%s" % e, flush=True)
        return out


def _with_failure(cls, **kw):
    """on_failure 的取值：新版本叫 "error"，老版本叫 "raise"（官方文档注明前者是后者的新名字）。"""
    try:
        return cls(on_failure="error", **kw)
    except (ValueError, TypeError):
        return cls(on_failure="raise", **kw)


def build(kernel, main_model, fallbacks, summary_model):
    cfg = kernel.cfg
    sub = kernel.sub
    mw = [KernelMiddleware(kernel)]
    if not sub:
        # 批准：官方 HumanInTheLoopMiddleware + when 条件（langchain>=1.3.3）。
        # 同一个工具要不要问取决于参数和现场（新建/覆盖）、批准模式、每个工具的设置，见 kernel.gate。
        interrupt_on = {name: {"allowed_decisions": ["approve", "reject"], "when": kernel.gate_request,
                               "description": APPROVAL_DESC} for name in kernel.tools}
        if interrupt_on:
            mw.append(HumanInTheLoopMiddleware(interrupt_on=interrupt_on, description_prefix=APPROVAL_DESC))
        mw.append(TodoListMiddleware(system_prompt=TODO_PROMPT, tool_description=TODO_DESC))
    # 压缩：按 32K 窗口算。输入预算 = 窗口 - 最大输出 - 1024，再扣掉系统提示和工具定义（中间件只数消息）
    # 工具按需加载时只扣这时带上的工具；中途又加载的那部分由 kernel.summary_tokens 加进计数（docs/借鉴开源项目.md 3.7）
    fixed = kernel.fixed_tokens()
    kernel._fixed_at_build = fixed
    budget = context.input_budget(cfg) - fixed
    budget = max(budget, 800)
    mw.append(LedgerSummarization(kernel, model=summary_model, trigger=("tokens", int(budget)),
                                  keep=("tokens", int(budget * 0.4)), token_counter=kernel.summary_tokens,
                                  summary_prompt=SUMMARY_PROMPT))
    if fallbacks:
        mw.append(ModelFallbackMiddleware(*fallbacks))
    retries = cfg.get("model", "retries", default=2)
    if retries and int(retries) > 0:
        mw.append(_with_failure(ModelRetryMiddleware, max_retries=int(retries), initial_delay=1.0, backoff_factor=2.0))
    steps = kernel.budget("sub_agent_steps" if sub else "max_steps") or (10 if sub else 40)
    if not sub:
        # docs/两种模式.md 9.4：深入研究的那一轮步数上限翻倍（不超过 60）。图在每次任务线程起来时建，
        # 所以按建图时账本里最后一条用户发言算（线程还在跑时插进来的 deep 发言要等下一次起线程才生效）
        steps = context.step_limit(steps, context.turn_is_deep(kernel.store.events(kernel.sid)))
    mw.append(ModelCallLimitMiddleware(run_limit=int(steps), exit_behavior="error"))
    tool_cap = kernel.budget("max_tool_calls") or int(steps) * 8
    mw.append(ToolCallLimitMiddleware(run_limit=int(tool_cap), exit_behavior="error"))
    # 工具重试只给只读工具，而且只重试超时、连接这类临时错误：有副作用的工具重做就是"结果未知时重做"
    ro = [n for n, t in kernel.tools.items() if t.risk == "read" and n != "spawn_agent"]
    tool_retries = kernel.budget("tool_retries", 1)
    if ro and tool_retries and int(tool_retries) > 0:
        mw.append(_with_failure(ToolRetryMiddleware, max_retries=int(tool_retries), tools=ro,
                                retry_on=(TimeoutError, ConnectionError), initial_delay=0.5))
    return mw


__all__ = ["build", "KernelMiddleware", "LedgerSummarization", "TODO_TOOL"]
