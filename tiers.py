"""action 危险分级 —— 回答「这个接口能不能随便给人调」。

## 为什么要分级

NapCat 的接口不是同一种东西。`get_group_list` 只是读一下，`bot_exit` 会让机器人
直接掉线。之前它们混在同一层，导致两个后果：

1. **别的插件没法安全复用。** 想把 OneBot 暴露给 LLM 的插件，只能自己人肉记一份
   安全名单 —— 记漏一个就是事故。
2. **管理员手滑没有拦截。** 指令本来就只对管理员开放，但管理员也可能敲错一个字。

这张表就是给这两件事提供依据的。

## 三档

    safe       只读、无副作用、不泄露隐私。可以放心暴露给 LLM 或普通用户。
    admin      有副作用但可逆，或者涉及隐私。默认档，锁管理员。
    dangerous  不可逆、会让机器人掉线、或丢数据。**执行前要显式确认。**

分级是**保守**的：拿不准的一律不放 SAFE。名单里没有的 action 会落到 `admin`，
这也是有意的 —— 新接口默认不该被当成安全的。

## 给复用方

    from data.plugins.qq_api.tiers import tier_of, SAFE

    if tier_of(action) == SAFE:      # 或者 `action in SAFE`，等价
        ...                          # 可以放心交给 LLM

注意分级**只看 action 名、不看参数** —— `set_group_ban duration=0` 其实是解禁，
但分级管不到这个层面。要更细的判断得自己写。
"""

# 只读、无副作用、不泄露隐私。
#
# 刻意**没有**放进来（都能读，但读到的东西不该随便给）：
#   - `get_cookies`              ← 拿到登录凭证等于拿到这个号
#   - `get_group_msg_history` /
#     `get_friend_msg_history` /
#     `get_msg` / `get_forward_msg`  ← 聊天记录，隐私
#   - `get_group_root_files` 等   ← 群文件列表，可能含隐私
#   - `get_online_clients`        ← 暴露登录设备
SAFE = frozenset(
    {
        "get_status",
        "get_version_info",
        "get_login_info",
        "get_group_list",
        "get_group_info",
        "get_group_member_list",
        "get_group_member_info",
        "get_stranger_info",
        "get_friend_list",
        "get_group_honor_info",
        "get_group_at_all_remain",
        "can_send_image",
        "can_send_record",
    }
)

# 不可逆、会让机器人掉线、或丢数据 —— 执行前必须显式确认。
DANGEROUS = frozenset(
    {
        # 机器人自己
        "bot_exit",  # 退出登录，得重新扫码
        "set_restart",  # 重启 NapCat
        "clean_cache",  # 清缓存
        # 关系 / 成员，撤不回来
        "set_group_kick",  # 踢人
        "set_group_leave",  # 退群
        "delete_friend",  # 删好友
        "delete_group_folder",  # 删群文件夹
        "delete_essence_msg",  # 删精华
        "_del_group_notice",  # 删群公告（NapCat 的内部名，带下划线）
    }
)

DEFAULT_TIER = "admin"


def tier_of(action: str) -> str:
    """返回 `"safe"` / `"admin"` / `"dangerous"`。

    未知 action 一律 `"admin"`（保守：不认识的别当安全的）。
    """
    if action in SAFE:
        return "safe"
    if action in DANGEROUS:
        return "dangerous"
    return DEFAULT_TIER


def describe(action: str) -> str:
    """给聊天里看的短说明。"""
    return {
        "safe": "只读，无副作用",
        "admin": "有副作用（默认档）",
        "dangerous": "不可逆 / 会让机器人掉线，需要 --yes",
    }[tier_of(action)]
