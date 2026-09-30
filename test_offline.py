"""用假 event 跑 astrbot_plugin_qq_api —— 不起 AstrBot，不连 QQ，也不发消息。

## 怎么跑

插件 import 的是 AstrBot 的包，所以得在 astrbot 容器里跑（命令见 README）。

**目录必须摆成包的样子**，因为 main.py 里用的是相对导入（`from . import onebot`）：

    /tmp/astrbot_plugin_qq_api_check/
      astrbot_plugin_qq_api/            ← 包名，和插件名一致
        main.py
        onebot.py
        tiers.py
        test_offline.py

这不是测试的特殊要求 —— AstrBot 自己就是这么加载的：它以 `data.plugins.astrbot_plugin_qq_api.main`
导入（`star_manager.py` 里的 `__import__(path, fromlist=[module_str])`），插件目录在
它眼里本来就是个包。按文件路径直接 load 反而会失败。

## ⚠️ 必须重定向 ASTRBOT_ROOT

`import astrbot` 会走到 `AstrBotConfig.__init__`，而**它初始化时就会往
data/cmd_config.json 写盘**（补默认键 + 修正键序）。不重定向的话，光是"导入一下
看看"就会改到正在跑的配置。
"""

import asyncio
import base64
import sys
import tempfile
from pathlib import Path

# 让插件目录作为包可导入。跑之前 cwd 无所谓，这里用 __file__ 定位。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiocqhttp.exceptions import ActionFailed  # noqa: E402
from astrbot.api.message_components import Image, Reply  # noqa: E402

from astrbot_plugin_qq_api import main, onebot, tiers  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond, detail: str = "") -> None:
    print(f"{'✅' if cond else '❌'} {name}" + (f"  {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


class FakeBot:
    """够 onebot.call 用：记下调用、可选地抛错。"""

    def __init__(self, fail: bool = False):
        self.calls: list[tuple[str, dict]] = []
        self.fail = fail

    async def call_action(self, action, **params):
        self.calls.append((action, params))
        if self.fail or action == "boom":
            raise ActionFailed(
                {"status": "failed", "retcode": 200, "message": "不支持的Api boom"}
            )
        return {"ok": True, "echo": action}


class FakeEvent:
    def __init__(self, text, segs=(), bot=FakeBot()):
        self._text = text
        self.message_obj = type("M", (), {"message": list(segs)})()
        self.bot = bot

    def get_message_str(self):
        return self._text

    def plain_result(self, text):
        return ("RESULT", text)


async def run(text, segs=(), bot=None):
    """跑一遍 handler，返回 (bot 调用列表, 输出文本列表)。"""
    ev = FakeEvent(text, segs, bot or FakeBot())
    # 绕过 __init__（它要 Context，本测试用不到）
    plugin = main.QQApi.__new__(main.QQApi)
    out = []
    async for r in plugin.qqapi(ev):  # 直接调真实入口，连注解器一起测到
        out.append(r[1])
    return ev.bot.calls, out


async def amain():
    logo = Path(tempfile.mkdtemp()) / "logo.bin"
    logo.write_bytes(b"hello\n")

    # ── 帮助 ───────────────────────────────────────────────
    for form in ("qqapi", "/qqapi", "qqapi --help", "qqapi -h", "qqapi help"):
        calls, out = await run(form)
        check(f"`{form}` 打印帮助", calls == [] and out and "参数怎么传" in out[0], str(out)[:60])

    # ── 前缀容忍 ───────────────────────────────────────────
    # CommandFilter 只在它自己的 filter() 里剥指令名，别处进来的消息带不带 "/"
    # 都有可能，所以两种写法都要能用。
    calls, _ = await run("/qqapi get_status")
    check("开头带 / 也能用", calls == [("get_status", {})], str(calls))

    # ── 基本调用 ───────────────────────────────────────────
    calls, _ = await run("qqapi get_status")
    check("无参数动作", calls == [("get_status", {})], str(calls))

    calls, _ = await run("qqapi set_group_card group_id=123 user_id=456 card=新名片")
    params = calls[0][1] if calls else {}
    check(
        "数字转 int、字符串保留",
        params.get("group_id") == 123 and params.get("card") == "新名片",
        str(params),
    )

    calls, _ = await run("qqapi set_group_whole_ban group_id=1 enable=true")
    check("true 转 bool", (calls[0][1] if calls else {}).get("enable") is True)

    calls, _ = await run('/qqapi set_group_card group_id=1 card="新 名片"')
    check("引号里的空格", (calls[0][1] if calls else {}).get("card") == "新 名片")

    # ── 危险分级 + --yes 闸门 ──────────────────────────────
    check("get_status 是 safe", tiers.tier_of("get_status") == "safe")
    check("bot_exit 是 dangerous", tiers.tier_of("bot_exit") == "dangerous")
    check("set_group_card 是 admin", tiers.tier_of("set_group_card") == "admin")
    check("未知动作落到 admin（保守）", tiers.tier_of("some_new_action") == "admin")

    calls, out = await run("qqapi bot_exit")
    check("危险动作不带 --yes 被拦下", calls == [] and "危险动作" in out[0], str(out)[:70])

    calls, _ = await run("qqapi bot_exit --yes")
    check("带 --yes 才真的执行", calls == [("bot_exit", {})], str(calls))

    calls, _ = await run("qqapi get_group_list --yes")
    check("--yes 对非危险动作无副作用", calls == [("get_group_list", {})], str(calls))

    # ── 错误路径 ───────────────────────────────────────────
    calls, out = await run("qqapi boom")
    check(
        "ActionFailed → 报 retcode 和原因，不抛",
        calls and "200" in out[0] and "不支持的Api" in out[0],
        str(out)[:80],
    )

    calls, out = await run("qqapi foo bar")
    check("非 key=value 报错且不发请求", calls == [] and "key=value" in out[0])

    no_bot = FakeEvent("qqapi get_status")
    del no_bot.bot
    plugin = main.QQApi.__new__(main.QQApi)
    out = [r[1] async for r in plugin.qqapi(no_bot)]
    check("没有 bot 句柄时明确报错", out and "aiocqhttp" in out[0], str(out)[:70])

    # ── 调用层本身（别的插件复用的就是这个）────────────────
    def bare_event():
        """没有 bot 句柄的 event —— 模拟跑在非 aiocqhttp 平台上。"""
        ev = FakeEvent("")
        del ev.bot
        return ev

    try:
        await onebot.call(bare_event(), "get_status")
        check("onebot.call 缺 bot 时抛 NotAiocqhttp", False, "居然没抛")
    except onebot.NotAiocqhttp:
        check("onebot.call 缺 bot 时抛 NotAiocqhttp", True)
    except Exception as e:  # noqa: BLE001
        check("onebot.call 缺 bot 时抛 NotAiocqhttp", False, f"实际抛了 {type(e).__name__}")

    try:
        await onebot.call(FakeEvent("", bot=FakeBot(fail=True)), "boom")
        check("onebot.call 失败时抛 CallFailed", False, "居然没抛")
    except onebot.CallFailed as e:
        check(
            "CallFailed 带上 action / retcode / message",
            e.action == "boom" and e.retcode == 200 and "不支持的Api" in e.message,
            f"{e}",
        )

    data = await onebot.call(FakeEvent(""), "get_status")
    check("成功时直接返回 data（不是整个响应）", data == {"ok": True, "echo": "get_status"}, str(data))

    # ── 图片参数 ───────────────────────────────────────────
    img = Image.fromFileSystem(str(logo))
    calls, _ = await run("qqapi set_qq_avatar file=@self", [img])
    got = (calls[0][1] if calls else {}).get("file", "")
    decoded = (
        base64.b64decode(got.removeprefix("base64://")) if got.startswith("base64://") else b""
    )
    check("@self 编成 base64:// 且逐字节一致", decoded == b"hello\n", f"解回来 {decoded!r}")

    calls, _ = await run("qqapi set_qq_avatar file=@reply", [Reply(id=1, chain=[img])])
    check(
        "@reply 从被引用消息里取图",
        (calls[0][1] if calls else {}).get("file", "").startswith("base64://"),
    )

    calls, out = await run("qqapi set_qq_avatar file=@reply")
    check("@reply 无回复时报错", calls == [] and "回复" in out[0])

    calls, out = await run("qqapi set_qq_avatar file=@self")
    check("@self 无图时报错", calls == [] and "图片" in out[0])

    # ── 成功输出 ───────────────────────────────────────────
    calls, out = await run("qqapi get_status")
    check("成功输出带 ✅ 和 data", out and out[0].startswith("✅") and "ok" in out[0])

    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 项失败：{FAILED}")
        return 1
    print("✅ 全部通过")
    return 0


sys.exit(asyncio.run(amain()))
