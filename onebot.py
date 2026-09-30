"""OneBot 调用层 —— **这个文件是给别的插件复用的**。

`main.py` 里剩下的都是"聊天界面"（命令、参数、帮助），只有这里是稳定接口。
你的插件想调 NapCat 的接口，import 这个模块就够了：

    from data.plugins.astrbot_plugin_qq_api.onebot import NotAiocqhttp, CallFailed, call

    try:
        groups = await call(event, "get_group_list")
    except NotAiocqhttp:
        ...                      # 当前平台不是 aiocqhttp，没有 OneBot 连接
    except CallFailed as e:
        ...                      # e.action / e.retcode / e.message / e.cause

成功时返回响应里的 `data`，**不返回原始响应** —— 调用方要的通常就是 data。

## 为什么要有这一层

直接写 `await event.bot.call_action(...)` 有三个坑，每个用它的插件都得自己踩一遍：

1. **`event.bot` 只在 aiocqhttp 平台上有。** 基类 `AstrMessageEvent` 没有这个属性，
   是子类构造完再 `self.bot = bot` 挂上去的（`aiocqhttp_message_event.py:33`）。
   不兜一下，换个平台就是 `AttributeError`。
2. **失败时抛 `ActionFailed`，原因藏在 `e.result` 里** —— `["message"]` 或
   `["wording"]`，两个都可能没有（`aiocqhttp/api_impl.py:28`）。
3. **超时、连接断开走的是另一条异常路径**，跟 `ActionFailed` 不共享父类。

这一层把三件事收在一起。所以复用时**不要**再自己 try `ActionFailed` —— 它不会
漏出来，已经被归一成 `CallFailed` 了。

## 边界

- 只看 action 的返回，**不做分级判断**。要不要让某个 action 被 LLM 调到，是
  `tiers.py` 的事。
- 不重试。OneBot 的写操作大多不幂等，自动重试可能把「发一次消息」变成两次。
"""

import asyncio
from typing import Any

from aiocqhttp.exceptions import ActionFailed
from astrbot.api import logger

__all__ = [
    "DEFAULT_TIMEOUT",
    "CallFailed",
    "NotAiocqhttp",
    "bot_of",
    "call",
]

# 单次调用的默认上限（秒）；None = 不限。
#
# aiocqhttp 自己也有超时，但那是 WS 层的，且拿不到 action 名。这里再兜一层是为了
# 别让某个卡住的 action 把整条消息处理流程拖死 —— 超时后抛的是 CallFailed，
# 调用方照常能报错给用户。
DEFAULT_TIMEOUT: float | None = 30.0


class NotAiocqhttp(RuntimeError):
    """当前平台拿不到 OneBot 连接句柄（不是 aiocqhttp）。"""


class CallFailed(RuntimeError):
    """action 被 NapCat 拒绝，或者调用过程中出错。

    属性：
        action   出错的 action 名
        retcode  OneBot 的返回码；不是被拒绝、而是异常/超时的话是 None
        message  尽量从 result 里挖出来的原因，挖不到就是空串
        cause    原始异常，给需要往上查的人
    """

    def __init__(
        self,
        action: str,
        message: str = "",
        retcode: int | None = None,
        cause: BaseException | None = None,
    ) -> None:
        self.action = action
        self.message = message
        self.retcode = retcode
        self.cause = cause

        detail = f"（retcode={retcode}）" if retcode is not None else ""
        reason = f"：{message}" if message else ""
        super().__init__(f"{action} 失败{detail}{reason}")


def bot_of(event) -> Any | None:
    """取 OneBot 连接句柄；不是 aiocqhttp 平台时返回 None。

    只需要判断"能不能调"、不打算真调的时候用这个。
    """
    return getattr(event, "bot", None)


async def call(
    event,
    action: str,
    params: dict | None = None,
    *,
    timeout: float | None = DEFAULT_TIMEOUT,
) -> Any:
    """调一个 OneBot action，成功返回响应里的 `data`。

    参数用**显式字典**而不是 `**kwargs`：OneBot 不少 action 自带 `timeout`、
    `duration` 这类参数名，用 kwargs 会和本函数的 `timeout` 撞车且很难查。

    失败一律抛 `NotAiocqhttp` / `CallFailed`，调用方不需要认识 aiocqhttp 的异常。
    """
    bot = bot_of(event)
    if bot is None:
        raise NotAiocqhttp("当前平台不是 aiocqhttp，没有 bot 句柄")

    kwargs = params or {}
    try:
        calling = bot.call_action(action, **kwargs)
        if timeout is None:
            return await calling
        return await asyncio.wait_for(calling, timeout)
    except ActionFailed as e:
        # 被 NapCat 拒绝：原因在 result 的两个可能字段里，都没有就留空
        result = getattr(e, "result", None) or {}
        message = result.get("message") or result.get("wording") or ""
        raise CallFailed(action, str(message), getattr(e, "retcode", None), e) from e
    except asyncio.TimeoutError as e:
        raise CallFailed(action, f"超时（{timeout}s 没返回）", None, e) from e
    except Exception as e:
        # 连接断了、序列化失败之类 —— 这种是"不该发生"的，留完整栈
        logger.exception(f"[astrbot_plugin_qq_api.onebot] {action} 调用异常")
        raise CallFailed(action, f"{type(e).__name__}: {e}", None, e) from e
