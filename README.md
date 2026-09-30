# qq_api

在聊天里调 NapCat 的 OneBot API —— 改群名片、换头像、拉群列表、发消息。走反向 WebSocket，不碰那个 HTTP 服务端。

> **仅管理员**：整个指令锁了 `PermissionType.ADMIN`，其他人发 `/qqapi` 不响应。

## 用法

```
/qqapi                                   看帮助
/qqapi get_status                        探活
/qqapi get_group_list                    拉群列表
/qqapi set_group_card group_id=123 user_id=456 card=新名片
/qqapi set_group_name group_id=123 group_name=新群名
/qqapi set_qq_avatar file=@reply         回复一张图 → 设为机器人头像
/qqapi set_qq_avatar file=@self          图和指令在同一条消息里
/qqapi set_qq_avatar file=https://example.com/a.png
```

参数写 `key=value`，可以写多个。可调的动作就是 NapCat 的完整接口清单（约 130 条）：<https://napcat.apifox.cn>

## 参数怎么写

| 你写的 | 传过去的 |
|---|---|
| `group_id=123` | 数字 `123` |
| `enable=true` | 布尔 `true`（`false` 同理） |
| `card=新名片` | 字符串 |
| `card="新 名片"` | 带空格的值加引号 |
| `file=@reply` | 把**被引用那条消息**里的图编码成 `base64://` |
| `file=@self` | 把**本条消息**里的图编码成 `base64://` |
| `file=https://…` | 原样传，NapCat 自己会去下 |

`@reply` / `@self` 的用处：图片不能直接传容器里的路径 —— 那是 napcat 的文件系统，而且用户发的图未必落在可读位置，所以这里读出来编码成 NapCat 认的 `base64://`。

## 输出

成功回 `✅ <动作>` 加返回的 `data`（JSON 单行，超 800 字符截断）；失败回 `❌ <动作> 失败（retcode=…）` 加原因。不可逆操作也一样只是这两行 —— **它没有二次确认**。

## ⚠️ 这是个「什么都能调」的口子

能调到的包括 `bot_exit`（退出登录）、`set_group_kick`、`delete_friend`、`set_group_leave` 这类不可逆操作。所以：

- 锁死 `PermissionType.ADMIN`，只给管理员用
- **别把它做成 LLM 工具**（`@filter.llm_tool`）—— 让模型自由生成 action 名，迟早踩到 `bot_exit`

## 为什么走 WS 而不是 HTTP

`scripts/api.sh` 是从宿主机侧面调的，必须 `docker exec` 进 napcat —— 因为那个 HTTP 服务端绑的是**容器的**回环，外面够不着。但机器人自己动手时绕这一圈是多余的：反向 WS 本来就一直连着，适配器手上就有 OneBot 连接。

```python
await event.bot.call_action("set_group_card", group_id=..., user_id=..., card=...)
```

`event.bot` 是 aiocqhttp 的 `CQHttp` 实例，**只在 aiocqhttp 平台上有**（基类 `AstrMessageEvent` 没有这个属性）。所以在可能换平台的插件里要 `getattr(event, "bot", None)` 兜一下 —— 本插件就是这么做的，拿不到句柄时会明确回一句「当前平台不是 aiocqhttp」。

`call_action` 的语义：成功返回响应里的 `data`；`status == "failed"` 时抛 `ActionFailed`，异常上带 `.retcode`。本插件把它接住并报出来，不会让异常冒到日志里。

## 装法

靠 compose 里这一行挂进容器：

```yaml
      - ./plugins/qq_api:/AstrBot/data/plugins/qq_api
```

`plugins/` 下的插件要**逐个挂**，不能整个目录挂 —— 那会盖掉 WebUI 装进 `runtime/data/plugins/` 的那些（`token_controller`、`debounce`）。挂载也不能加 `:ro`，Python import 时要往插件目录写 `__pycache__`。

## 自测

`test_offline.py` 用假 event 跑一遍 handler —— 不起 AstrBot、不连 QQ、不发消息：

```bash
./scripts/dock.sh "docker exec astrbot mkdir -p /tmp/fakeroot /tmp/qq_api_check"
./scripts/dock.sh "docker cp plugins/qq_api/main.py astrbot:/tmp/qq_api_check/main.py"
./scripts/dock.sh "docker cp plugins/qq_api/test_offline.py astrbot:/tmp/test_qq_api.py"
printf 'hello\n' > /tmp/logo.bin
./scripts/dock.sh "docker cp /tmp/logo.bin astrbot:/tmp/qq_api_check/logo.bin"
./scripts/dock.sh "docker exec -e ASTRBOT_ROOT=/tmp/fakeroot -e ASTRBOT_CONFIG_PATH=/tmp/fakeroot/cmd_config.json astrbot python3 /tmp/test_qq_api.py"
```

> ⚠️ **那两个 `-e` 不能省，而且别在插件目录里跑它。**
>
> `import astrbot` 会走到 `AstrBotConfig.__init__`，**它一初始化就往 `data/cmd_config.json` 写盘**（补默认键、修键序）。不重定向 `ASTRBOT_ROOT`，光是「导入一下看看」就会改到正在跑的配置。
>
> 反过来，如果 cwd 恰好是插件目录（它 bind-mount 到宿主的 `plugins/qq_api/`），那次写盘会**落到宿主源码里**，生成一个 `plugins/qq_api/data/cmd_config.json` —— 里面有 `dashboard.password` 的哈希。这个文件不该进版本库。
