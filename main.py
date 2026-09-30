"""在聊天里调 NapCat 的 OneBot API —— 走反向 WS，不碰那个 HTTP 服务端。

## 为什么是插件，而不是 HTTP

`scripts/api.sh` 是从宿主机侧面调的（必须 `docker exec` 进 napcat，因为那个 HTTP
服务端绑的是容器的回环）。但如果要**机器人自己动手** —— 收到指令就改群名片、换头像 ——
绕一圈 HTTP 是多余的：反向 WS 本来就一直连着，适配器手上就有 OneBot 的连接。

    await event.bot.call_action("set_group_card", group_id=..., user_id=..., card=...)

`event.bot` 是 aiocqhttp 的 `CQHttp` 实例（`aiocqhttp_message_event.py:33`，父类构造完
再 `self.bot = bot`）。语义（`aiocqhttp/api_impl.py:28`）：

  - 成功 → 返回响应里的 `data` 字段
  - `status == "failed"` → 抛 `ActionFailed`，异常上带 `.retcode` 和 `.result`

## 用法

    /qqapi                                看帮助
    /qqapi get_status                     探活
    /qqapi get_group_list                 拉群列表
    /qqapi set_group_card group_id=123 user_id=456 card=新名片
    /qqapi set_group_name group_id=123 group_name=新群名
    /qqapi set_qq_avatar file=@reply        ← 回复一张图片，把它设成机器人头像
    /qqapi set_qq_avatar file=@self         ← 图片和指令在同一条消息里
    /qqapi set_qq_avatar file=https://...   也可以直接给 URL

值 `@reply` / `@self` 会把图片读出来编码成 NapCat 认的 `base64://` —— 不能直接传
容器里的路径（那是 napcat 自己的路径，而且用户发的图未必落在可读位置）。

## ⚠️ 这是个"什么都能调"的口子

能调到的包括 `bot_exit`（退出登录）、`set_group_kick`、`delete_friend`、
`set_group_leave` 这类不可逆操作。所以：

  - 整个 handler 用 `PermissionType.ADMIN` 锁死，只有管理员能用
  - **别把它做成 LLM 工具**（`@filter.llm_tool`）。让模型自由生成 action 名，
    迟早会踩到 `bot_exit`
"""

import base64
import json
import shlex

from aiocqhttp.exceptions import ActionFailed
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Reply
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.permission import PermissionType

HELP = """\
/qqapi <动作> [参数...]   —— 调 NapCat 的 OneBot API（仅管理员）

  /qqapi get_status
  /qqapi get_group_list
  /qqapi set_group_card group_id=123 user_id=456 card=新名片
  /qqapi set_qq_avatar file=@reply     （回复一张图，设为机器人头像）
  /qqapi set_qq_avatar file=https://example.com/a.png

参数写 key=value，自动判类型（数字→number，true/false→boolean）。
带空格的值用引号：card="新 名片"
接口清单：https://napcat.apifox.cn\
"""

MAX_LEN = 800


def _fmt(data) -> str:
    """把 API 返回的 data 变成能读的一行。"""
    if data is None:
        return "(无返回数据)"
    try:
        text = json.dumps(data, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(data)
    if len(text) > MAX_LEN:
        text = f"{text[:MAX_LEN]}…… （共 {len(text)} 字符）"
    return text


def _coerce(val: str):
    """key=value 里的 value 猜个类型。NapCat 大多也能吃字符串，但数字给对了更稳。"""
    low = val.lower()
    if low in ("true", "false"):
        return low == "true"
    if val.lstrip("-").isdigit():
        return int(val)
    return val


@register(
    "qq_api",
    "Elarian",
    "在聊天里调 NapCat 的 OneBot API（仅管理员），如改群名片、换头像。",
    "1.0.0",
)
class QQApi(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)

    @filter.command("qqapi")
    @filter.permission_type(PermissionType.ADMIN)
    async def qqapi(self, event: AstrMessageEvent):
        raw = event.get_message_str().strip()
        # 指令名前缀这一步之所以要自己做：CommandFilter 只在它自己的 filter() 里剥
        # （command.py:204 用的是局部变量），event.message_str 里指令名还留着。
        # 开头的 "/" 正常已经被 waking_check 按 wake_prefix 剥掉了（stage.py:130），
        # 但那条路只在命中前缀时才走，所以这里再容忍一次，免得哪种路径漏了就失灵。
        raw = raw.lstrip("/").strip()
        if raw.startswith("qqapi"):
            raw = raw[len("qqapi") :].strip()
        if not raw:
            yield event.plain_result(HELP)
            return

        try:
            tokens = shlex.split(raw)
        except ValueError as e:
            yield event.plain_result(f"参数解析失败（引号没配对？）：{e}")
            return

        action, rest = tokens[0], tokens[1:]

        bot = getattr(event, "bot", None)
        if bot is None:
            yield event.plain_result(
                "当前平台不是 aiocqhttp，没有 bot 句柄，调不了 OneBot API。"
            )
            return

        try:
            params = await self._parse(event, rest)
        except ValueError as e:
            yield event.plain_result(f"❌ {e}")
            return

        try:
            data = await bot.call_action(action, **params)
        except ActionFailed as e:
            msg = e.result.get("message") or e.result.get("wording") or ""
            yield event.plain_result(f"❌ {action} 失败（retcode={e.retcode}）{msg}")
            return
        except Exception as e:  # 超时、连接断了之类
            logger.exception(f"[qq_api] {action} 调用异常")
            yield event.plain_result(f"❌ {action} 调用异常：{type(e).__name__}: {e}")
            return

        yield event.plain_result(f"✅ {action}\n{_fmt(data)}")

    async def _parse(self, event: AstrMessageEvent, rest: list[str]) -> dict:
        params: dict = {}
        for item in rest:
            key, sep, val = item.partition("=")
            if not sep or not key:
                raise ValueError(f"参数要写成 key=value：{item}")
            if val in ("@reply", "@self"):
                val = await self._image_b64(event, val[1:])
            else:
                val = _coerce(val)
            params[key] = val
        return params

    async def _image_b64(self, event: AstrMessageEvent, where: str) -> str:
        """把用户发的图片（本条消息里，或引用的那条里）读出来编码成 base64://。"""
        segs = list(getattr(event.message_obj, "message", None) or [])

        if where == "reply":
            reply = next((s for s in segs if isinstance(s, Reply)), None)
            if reply is None:
                raise ValueError("@reply 要回复一张带图片的消息再发指令")
            segs = list(reply.chain or [])

        image = next((s for s in segs if isinstance(s, Image)), None)
        if image is None:
            raise ValueError(
                "没找到图片。回复一张图片用 @reply，或者把图片和指令放在同一条消息里用 @self"
            )

        # convert_to_file_path 是异步的：图片是 URL 时会先下载下来
        path = await image.convert_to_file_path()
        with open(path, "rb") as f:
            return "base64://" + base64.b64encode(f.read()).decode()
