from typing import Any, Callable

from .base import SandboxMethodType


def get_sandbox_method_type(method: Callable) -> SandboxMethodType:
    if hasattr(method, "_method_type"):
        try:
            return SandboxMethodType(method._method_type)  # noqa: SLF001
        except ValueError as e:
            raise ValueError(f"方法 {method.__name__} 的 _method_type 属性值无效。") from e
    raise AttributeError(f"方法 {method.__name__} 没有 _method_type 属性。")

#: 标记属性：方法体自行完成了超级用户校验。
#:
#: core 无法静态判定「这个函数有没有查权限」——校验可能藏在几层调用之下，
#: 源码扫描既漏报又误报。因此改为插件显式声明：
#:
#:     @plugin.mount_sandbox_method(..., requires_superuser=True)
#:     async def foo(...):
#:         ...
#:     foo.permission_checked = True   # 或 mark_superuser_checked(foo)
#:
#: 若声明了 requires_superuser 却没标记 permission_checked，插件加载时告警。
SUPERUSER_CHECKED_ATTR = "_permission_checked"


def mark_superuser_checked(func: Callable) -> Callable:
    """标记该方法体自行完成了超级用户校验。

    用于配合 mount_sandbox_method(requires_superuser=True)：
    声明与实现一致时不告警，不一致时在插件加载日志里提示。
    """
    setattr(func, SUPERUSER_CHECKED_ATTR, True)
    return func


def audit_superuser_declarations(plugin: Any) -> list[str]:
    """校验声明了 requires_superuser 但未自行校验的方法，返回告警列表。

    空列表表示全部一致。
    """
    warnings: list[str] = []
    for method in getattr(plugin, "sandbox_methods", []):
        if not getattr(method, "requires_superuser", False):
            continue
        func = method.func
        # 接受两种写法：mark_superuser_checked() 设的 _permission_checked，
        # 以及插件手写的 permission_checked。两者语义相同。
        if getattr(func, SUPERUSER_CHECKED_ATTR, False) or getattr(func, "permission_checked", False):
            continue
        warnings.append(
            f"{plugin.name}.{method.name} 声明了 requires_superuser=True，"
            "但方法体未标记 permission_checked；core 不会自动拦截，"
            "请确认该方法内部确实校验了调用者权限。"
        )
    return warnings
