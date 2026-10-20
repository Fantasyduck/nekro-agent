from enum import Enum
from typing import Any, Callable, Coroutine


class SandboxMethodType(str, Enum):
    """沙盒方法类型

    用于描述方法的类型，如工具方法, 行为方法等；方法类型区别主要在于对返回值的处理方式。

    * TOOL: 工具方法
        提供给 LLM 使用的工具，返回值可以是任意类型，LLM 可获取返回值作进一步处理

    * AGENT: 代理方法
        用于提供 LLM 交互反馈，其返回值必须为 str 类型，描述 LLM 行为的结果，返回后会被添加到上下文中再次调用

    * BEHAVIOR: 行为方法
        用于提供 LLM 交互反馈，其返回值必须为 str 类型，描述 LLM 行为的结果，返回后会被添加到上下文中但不触发再次调用

    * MULTIMODAL_AGENT: 多模态 Agent
        用于提供 LLM 交互反馈，其返回值为一段 多模态 message，描述 LLM 行为的结果，返回后会被添加到上下文中再次调用
    """

    TOOL = "tool"
    AGENT = "agent"
    BEHAVIOR = "behavior"
    MULTIMODAL_AGENT = "multimodal_agent"


class WebhookMethod:
    """Webhook 方法"""

    def __init__(self, name: str, description: str, func: Callable[..., Coroutine[Any, Any, Any]]):
        self.name = name
        self.description = description
        self.func = func


class SandboxMethod:
    """沙盒方法

    ``requires_superuser`` 是**声明**：插件标明该方法只应供超级用户使用。
    core 不据此自动拦截——沙盒 RPC 只能看到 ``from_chat_key`` 与方法名，
    无法区分「用户要求模型调用」与「模型自主决定调用」，强行判定会给出一个
    在群聊里形同虚设的口径。真正的判定仍由插件方法体完成（如天使记忆的
    ``_can_manage_relation_profiles``）。

    core 只做一件事：声明与实现的一致性校验。若方法声明了该权限却未在任何
    执行路径上校验，则在插件加载时告警，避免「以为拦住了其实没拦」。
    """

    def __init__(
        self,
        method_type: SandboxMethodType,
        name: str,
        description: str,
        func: Callable[..., Coroutine[Any, Any, Any]],
        requires_superuser: bool = False,
    ):
        self.method_type = method_type
        self.name = name
        self.description = description
        self.func = func
        self.requires_superuser = requires_superuser


class PromptInjectMethod:
    """提示注入方法"""

    def __init__(self, name: str, description: str, func: Callable[..., Coroutine[Any, Any, str]]):
        self.name = name
        self.description = description
        self.func = func
