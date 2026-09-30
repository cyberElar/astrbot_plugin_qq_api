"""在聊天里调 NapCat 的 OneBot API —— 走反向 WS，不碰那个 HTTP 服务端。

## 为什么是插件，而不是 HTTP

`scripts/api.sh` 是从宿主机侧面调的（必须 `docker exec` 进 napcat，因为那个 HTTP
服务端绑的是容器的回环）。但如果要**机器人自己动手** —— 收到指令就改群名片、换头像 ——
绕一圈 HTTP 是多余的：反向 WS 本来就一直连着，适配器手上就有 OneBot 的连接。

## 三个文件的分工

    main.py     ← 你在这里。插件入口、命令、帮助文本 —— 都是"聊天界面"。
    onebot.py   ← OneBot 调用层。**别的插件复用的就是它。**
    tiers.py    ← action 危险分级。决定哪些能安全暴露、哪些要二次确认。

想在自己插件里调 OneBot 的话，**不要**从这里 import —— 这个文件是界面层，随时会
因为改文案、改命令而变。稳定接口只有：

    from data.plugins.qq_api.onebot import call
    from data.plugins.qq_api.tiers import tier_of, SAFE

## 用法

    /qqapi                       看说明（= /qqapi --help）
    /qqapi --help                同上
    /qqapi get_status            探活
    /qqapi get_group_list        拉群列表
    /qqapi set_group_card group_id=123 user_id=456 card=新名片
    /qqapi set_qq_avatar file=@reply      ← 回复一张图片，设为机器人头像
    /qqapi bot_exit --yes        不可逆动作要带 --yes

完整说明在下面的 `HELP` 常量里，改文案只改那一处。

## ⚠️ 这是个"什么都能调"的口子

能调到的包括 `bot_exit`（退出登录）、`set_group_kick`、`delete_friend`、
`set_group_leave` 这类不可逆操作。三层防护：

1. 整个 handler 锁 `PermissionType.ADMIN` —— 只有管理员能用
2. `tiers.py` 把危险 action 挑出来，执行前要显式带 `--yes`
3. **不要把它做成 LLM 工具**（`@filter.llm_tool`）

第 3 条要展开说：不是"永远不能给 LLM 用"，而是**不能把整个口子给 LLM**。让模型
自由生成 action 名，迟早会踩到 `bot_exit`。真要让 LLM 用，正确做法是先过
`tiers.tier_of()`，只放行 `SAFE` 那档。
"""

import base64
import json
import shlex

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Reply
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.permission import PermissionType

from . import onebot, tiers

# 指令名。插件在 AstrBot 里的 id 是 `qq_api`，但指令用 `qqapi` —— 打字少一次下划线。
COMMAND = "qqapi"

# 回复里 JSON 的截断长度。聊天窗口放不下更长的，而且真要看全的应该去翻日志。
MAX_LEN = 800

# 危险 action 的确认开关。
FORCE_FLAG = "--yes"

# 触发帮助的写法（除了"只发指令名"）。
HELP_FLAGS = ("--help", "-h", "help")

HELP = f"""\
/{COMMAND} <动作> [参数...]      调 NapCat 的 OneBot API（仅管理员）
/{COMMAND} --help               看这份说明

── 参数怎么传 ──
  key=value，可以写多个，顺序随意
    group_id=123          数字自动转 int
    enable=true           true/false 自动转布尔
    card=新名片            字符串原样传
    card="新 名片"         值里有空格就用引号
    file=@reply           取「被引用那条消息」里的图片
    file=@self            取「本条消息」里的图片
    file=https://...      直接给 URL，NapCat 自己去下

  @reply / @self 会把图片读出来编码成 base64:// —— 不能直接传容器里的路径，
  那是 napcat 自己的文件系统，而且用户发的图未必落在可读位置。

── 写错了会怎样 ──
  参数少给或名字写错，NapCat 会直接拒绝，回你一句「不支持的Api」之类。
  ⚠️ 不可逆的动作要显式加 {FORCE_FLAG}：
     /{COMMAND} bot_exit {FORCE_FLAG}
  属于这一档的：{", ".join(sorted(tiers.DANGEROUS))}

── 常用动作（完整约 130 个见 https://napcat.apifox.cn）──
  探活    get_status · get_version_info · get_login_info
  群      get_group_list · get_group_info · get_group_member_list
  改资料  set_group_card · set_group_name · set_qq_avatar
  发消息  send_group_msg · send_private_msg
  文件    get_group_root_files · get_group_file_url
"""


class _BadCommand(ValueError):
    """指令本身写错了（引号没配对之类）。消息直接回给用户，不进调用层。"""


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


def _strip_command_name(raw: str) -> str:
    """剥掉开头的指令名和斜杠，返回剩下的部分。

    这一步之所以要自己做：CommandFilter 只在它自己的 filter() 里剥
    （`command.py:204` 用的是局部变量），`event.message_str` 里指令名还留着。
    开头的 "/" 正常已经被 waking_check 按 wake_prefix 剥掉了（`stage.py:130`），
    但那条路只在命中前缀时才走，所以这里再容忍一次，免得哪种路径漏了就失灵。
    """
    raw = raw.lstrip("/").strip()
    if raw.startswith(COMMAND):
        raw = raw[len(COMMAND) :].strip()
    return raw


def _split_command(event: AstrMessageEvent) -> tuple[str, list[str], bool]:
    """把消息拆成 `(动作, 参数列表, 是否带了 --yes)`。

    动作是空串表示用户只发了指令名（该看帮助了）。帮助标志原样当动作返回，
    由调用方决定怎么显示 —— 这个函数不关心 UI。
    """
    raw = _strip_command_name(event.get_message_str().strip())
    if not raw:
        return "", [], False

    try:
        tokens = shlex.split(raw)
    except ValueError as e:
        raise _BadCommand(f"参数解析失败（引号没配对？）：{e}") from e

    force = FORCE_FLAG in tokens
    tokens = [t for t in tokens if t != FORCE_FLAG]
    return (tokens[0], tokens[1:], force) if tokens else ("", [], force)


@register(
    "qq_api",
    "Elarian",
    "在聊天里调 NapCat 的 OneBot API（仅管理员），如改群名片、换头像。",
    "1.1.0",
)
class QQApi(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)

    @filter.command(COMMAND)
    @filter.permission_type(PermissionType.ADMIN)
    async def qqapi(self, event: AstrMessageEvent):
        try:
            action, rest, force = _split_command(event)
        except _BadCommand as e:
            yield event.plain_result(f"❌ {e}")
            return

        # 只发指令名、或者要 --help —— 都打印说明
        if not action or action.lower() in HELP_FLAGS:
            yield event.plain_result(HELP)
            return

        # 危险动作的二次确认。管理员锁挡的是别人，挡不住管理员自己手滑 ——
        # 而 bot_exit 这类敲错的代价是机器人掉线、要重新扫码。
        if tiers.tier_of(action) == "dangerous" and not force:
            yield event.plain_result(
                f"⚠️ {action} 是危险动作（{tiers.describe(action)}）。\n"
                f"确认要执行的话，加上 {FORCE_FLAG} 再来一次："
                f"/{COMMAND} {action} {FORCE_FLAG}"
            )
            return

        try:
            params = await self._parse(event, rest)
        except ValueError as e:
            yield event.plain_result(f"❌ {e}")
            return

        try:
            data = await onebot.call(event, action, params)
        except onebot.NotAiocqhttp as e:
            yield event.plain_result(f"❌ {e}")
            return
        except onebot.CallFailed as e:
            # CallFailed.__str__ 已经拼成「action 失败（retcode=…）：原因」
            yield event.plain_result(f"❌ {e}")
            return

        yield event.plain_result(f"✅ {action}\n{_fmt(data)}")

    async def _parse(self, event: AstrMessageEvent, rest: list[str]) -> dict:
        """把 `key=value` 列表变成参数字典。写错的键值直接抛 ValueError 给用户看。"""
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
