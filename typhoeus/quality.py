"""音质档位模型（provider 无关）.

档位按 rank 升序排列；协商失败（无权限/无资源）时按 rank 向下回退。
`requires` 是会员门槛等级，`EncryptedSongFileType` 系列档位（QMC 密文）被标记
`encrypted=True`，Typhoeus 的默认策略**直接拒绝**（本项目不做解密）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class Membership(IntEnum):
    """会员等级（按特权从低到高）。"""

    NONE = 0        # 未登录 / 普通用户
    GREEN = 1       # 绿钻 / 豪华绿钻
    SUPER = 2       # 超级会员（音乐包/超级会员）


@dataclass(frozen=True)
class Tier:
    """一个可协商的音质档位。

    Attributes:
        id: 档位标识（对 UI/API 暴露的稳定字符串）。
        label: 中文展示名。
        prefix: 腾讯文件名前缀（如 F000/AI00），仅诊断用。
        ext: 容器扩展名。
        mime: Content-Type。
        rank: 音质档位序（越大越优先；回退=rank-1 方向）。
        requires: 会员门槛。
        encrypted: QMC 密文档（Typhoeus 拒绝播放，只保留元信息供展示）。
    """

    id: str
    label: str
    prefix: str
    ext: str
    mime: str
    rank: int
    requires: Membership = Membership.NONE
    encrypted: bool = False


class TierId:
    """档位 id 常量（避免上层拼写漂移）。"""

    STD = "128"
    HQ = "320"
    HQ_OGG = "320ogg"
    SQ_OGG = "640ogg"
    SQ = "flac"
    MASTER = "master"
    ATMOS_2 = "atmos2"
    ATMOS_51 = "atmos51"
    VINYL = "vinyl"


_TIERS: tuple[Tier, ...] = (
    Tier(TierId.STD, "标准音质", "M500", ".mp3", "audio/mpeg", 10),
    Tier(TierId.HQ, "高品质 HQ", "M800", ".mp3", "audio/mpeg", 20, Membership.GREEN),
    Tier(TierId.HQ_OGG, "高品质 HQ (OGG)", "O800", ".ogg", "audio/ogg", 25, Membership.GREEN),
    Tier(TierId.SQ_OGG, "无损 SQ (OGG)", "O801", ".ogg", "audio/ogg", 30, Membership.GREEN),
    Tier(TierId.SQ, "无损 SQ", "F000", ".flac", "audio/flac", 40, Membership.GREEN),
    Tier(TierId.ATMOS_2, "臻品音质", "Q000", ".flac", "audio/flac", 50, Membership.SUPER),
    Tier(TierId.ATMOS_51, "臻品全景声 5.1", "Q001", ".flac", "audio/flac", 55, Membership.SUPER),
    Tier(TierId.MASTER, "臻品母带", "AI00", ".flac", "audio/flac", 60, Membership.SUPER),
    # —— 以下为 QMC 加密档位：仅作元信息，Typhoeus 策略层拒绝（本项目不提供解密） ——
    Tier(TierId.VINYL, "黑胶（加密）", "V0M0", ".mflac", "audio/flac", 90, Membership.SUPER, True),
)

TIERS: dict[str, Tier] = {t.id: t for t in _TIERS}

# 明文可流式档位，按 rank 升序（回退链从右往左找）
STREAMABLE: tuple[Tier, ...] = tuple(sorted((t for t in _TIERS if not t.encrypted), key=lambda t: t.rank))
TIER_ORDER: tuple[str, ...] = tuple(t.id for t in STREAMABLE)


def tier_by_id(tid: str) -> Tier:
    from typhoeus.errors import UnknownTier

    t = TIERS.get(tid)
    if t is None:
        raise UnknownTier(tid)
    return t


def fallback_chain(tid: str, *, deprioritize: tuple[str, ...] = ()) -> list[Tier]:
    """目标档位 → 依次降质的候选链（自目标 rank 向下降序，兜底标准音质）。

    deprioritize：把这些档位整体压到链尾（仍按 rank 降序）——自动模式下
    「臻品全景声」不优先降档到此档；但目标本身就是它时仍最先尝试（显式选档语义不变）。
    """
    target = tier_by_id(tid)
    if target.encrypted:
        from typhoeus.errors import TierNotPlayable

        raise TierNotPlayable(f"加密档位 {target.label} 不支持播放（本项目不涉及解密）")
    chain = [t for t in reversed(STREAMABLE) if t.rank <= target.rank]
    if deprioritize:
        head, rest = chain[:1], chain[1:]  # chain[0] = 目标档，永不动
        demoted = [t for t in rest if t.id in deprioritize]
        if demoted:
            chain = head + [t for t in rest if t.id not in deprioritize] + demoted
    return chain


def available_for(membership: Membership) -> list[Tier]:
    """按会员等级列出明文可播档位（升序）。"""
    return [t for t in STREAMABLE if membership >= t.requires]
