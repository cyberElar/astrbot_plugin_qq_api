"""用假 event 跑 qq_api 的 handler —— 不起 AstrBot，不连 QQ，也不发消息。

## 怎么跑

插件 import 的是 AstrBot 的包，所以得在 astrbot 容器里跑：

    ./scripts/dock.sh "docker exec astrbot sh -c 'mkdir -p /tmp/fakeroot'"
    ./scripts/dock.sh "docker cp plugins/qq_api astrbot:/tmp/qq_api_check"
    ./scripts/dock.sh "docker cp plugins/qq_api/test_offline.py astrbot:/tmp/test_qq_api.py"
    ./scripts/dock.sh "docker exec -e ASTRBOT_ROOT=/tmp/fakeroot \
        -e ASTRBOT_CONFIG_PATH=/tmp/fakeroot/cmd_config.json \
        astrbot python3 /tmp/test_qq_api.py"

## ⚠️ 那两个 -e 不能省

`import astrbot` 会走到 `AstrBotConfig.__init__`，而**它初始化时就会往
data/cmd_config.json 写盘**（补默认键 + 修正键序）。不重定向 ASTRBOT_ROOT 的话，
光是"导入一下看看"就会改到正在跑的配置 —— 我第一次就是这么干的。

注意 ASTRBOT_ROOT 只挡住配置，日志之类的还是可能落到别处；验证完顺手看一眼
`git status` 或者文件的 mtime。
"""

import asyncio
import base64
import sys

sys.path.insert(0, "/tmp/qq_api_check")

from aiocqhttp.exceptions import ActionFailed  # noqa: E402
from astrbot.api.message_components import Image, Reply  # noqa: E402

import main  # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    print(f"{'✅' if cond else '❌'} {name}" + (f"  {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


class FakeBot:
    def __init__(self):
        self.calls = []

    async def call_action(self, action, **params):
        self.calls.append((action, params))
        if action == "boom":
            raise ActionFailed(
                {"status": "failed", "retcode": 200, "message": "不支持的Api boom"}
            )
        return {"ok": True, "echo": action}


class FakeEvent:
    def __init__(self, text, segs=()):
        self._text = text
        self.message_obj = type("M", (), {"message": list(segs)})()
        self.bot = FakeBot()

    def get_message_str(self):
        return self._text

    def plain_result(self, text):
        return ("RESULT", text)


async def call(text, segs=()):
    """跑一遍 handler，返回 (bot调用列表, 输出文本列表)。"""
    ev = FakeEvent(text, segs)
    plugin = main.QQApi.__new__(main.QQApi)  # 绕过 __init__（它要 Context，本测试用不到）
    out = []
    async for r in plugin.qqapi(ev):
        out.append(r[1])
    return ev.bot.calls, out


async def amain():
    # 1. 只有指令名 → 帮助
    calls, out = await call("qqapi")
    check("空参数打印帮助", calls == [] and out and "get_status" in out[0])

    # 2. 无参数动作
    calls, out = await call("qqapi get_status")
    check("get_status 无参数", calls == [("get_status", {})], str(calls))

    # 3. 类型判断
    calls, _ = await call("qqapi set_group_card group_id=123 user_id=456 card=新名片")
    params = calls[0][1] if calls else {}
    check(
        "数字转 int、字符串保留",
        params.get("group_id") == 123
        and isinstance(params.get("group_id"), int)
        and params.get("card") == "新名片",
        str(params),
    )

    # 4. 布尔
    calls, _ = await call("qqapi set_group_whole_ban group_id=1 enable=true")
    params = calls[0][1] if calls else {}
    check("true 转 bool", params.get("enable") is True, str(params))

    # 5. 带引号的值
    calls, _ = await call('/qqapi set_group_card group_id=1 card="新 名片"')
    params = calls[0][1] if calls else {}
    check("引号里的空格", params.get("card") == "新 名片", str(params))

    # 6. 失败路径：ActionFailed → 提示 retcode，不抛
    calls, out = await call("qqapi boom")
    check(
        "ActionFailed 被接住并报 retcode",
        calls and "200" in out[0] and "不支持的Api" in out[0],
        str(out),
    )

    # 7. 参数格式不对
    calls, out = await call("qqapi foo bar")
    check("非 key=value 报错且不发请求", calls == [] and "key=value" in out[0], str(out))

    # 8. 成功输出带 ✅ 和 data
    calls, out = await call("qqapi get_status")
    check("成功输出", out and out[0].startswith("✅") and "ok" in out[0], str(out))

    # 9. @self：图片和指令在同一条消息里
    img = Image.fromFileSystem("/tmp/qq_api_check/logo.bin")
    calls, out = await call("qqapi set_qq_avatar file=@self", [img])
    params = calls[0][1] if calls else {}
    got = params.get("file", "")
    # 断言要能证伪：解回来和原文件逐字节比，而不是比长度
    decoded = base64.b64decode(got.removeprefix("base64://")) if got.startswith("base64://") else b""
    check(
        "@self 把图片编成 base64:// 且内容一致",
        decoded == b"hello\n",
        f"解回来 {decoded!r}",
    )

    # 10. @reply：图片在被引用的那条消息里
    reply = Reply(id=1, chain=[img])
    calls, out = await call("qqapi set_qq_avatar file=@reply", [reply])
    params = calls[0][1] if calls else {}
    check(
        "@reply 从被引用消息里取图",
        params.get("file", "").startswith("base64://"),
        str(out)[:80],
    )

    # 11. @reply 但没回复任何东西 → 明确报错，不发请求
    calls, out = await call("qqapi set_qq_avatar file=@reply")
    check("@reply 无回复时报错", calls == [] and "回复" in out[0], str(out))

    # 12. @self 但消息里没图
    calls, out = await call("qqapi set_qq_avatar file=@self")
    check("@self 无图时报错", calls == [] and "图片" in out[0], str(out))

    # 13. 带斜杠的写法也要能容忍（有的路径不剥前缀）
    calls, out = await call("/qqapi get_status")
    check("开头带 / 也能用", calls == [("get_status", {})], str(calls))

    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 项失败: {FAILED}")
        return 1
    print("✅ 全部通过")
    return 0


sys.exit(asyncio.run(amain()))
