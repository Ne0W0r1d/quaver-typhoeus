"""档位协商解析器：会员门控 + 明文档位回退链.

流程（resolve）：
1. 查 provider 会员等级；请求档位 requires > 会员等级 → MembershipRequired（403，仅限会员）。
2. 加密档位（Tier.encrypted）→ TierNotPlayable（451，不涉解密）。
3. 从目标档位沿 fallback_chain 逐档取链；命中 result=0 且有 purl → 首块明文嗅探
   （`sniff`，判定 magic；若疑似 QMC 密文则降档继续——防止上游偶发对高档位下发
   加密内容而客户端拿不到解密器）。
4. 全链失败 → ProviderError（保留最后一个上游 result 码，UI 可提示）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from typhoeus import stream  # noqa: PLC0414  (显式子模块导入，避免循环)
from typhoeus.errors import (
    MembershipRequired,
    ProviderError,
    TierNotPlayable,
)
from typhoeus.provider import LinkRequest, LinkResult, QualityProvider, SongRef
from typhoeus.quality import Membership, Tier, fallback_chain, tier_by_id

# 明文容器 magic 白名单（前缀匹配）；不在表内 → 视为疑似加密/未知格式，拒绝直连播
PLAIN_MAGICS: tuple[bytes, ...] = (
    b"fLaC",      # FLAC
    b"OggS",      # Ogg（QMC 的 mgg 密文不会有合法 OggS 页首）
    b"ID3",       # MP3 with tag
    b"\xff\xfb",  # MP3 frame sync (MPEG1 Layer3)
    b"\xff\xf3",
    b"\xff\xf2",
    b"\x8aX\x1a\xdf",  # Matroska/EBML 头（DTS:X/杜比 mp4 容器若为明文）
)

# FLAC 尾部 ~2s 有良性 sync 警告的先例（docs/spike-1）：只嗅探头部即可判明文


def looks_plain(head: bytes) -> bool:
    return any(head.startswith(m) for m in PLAIN_MAGICS) or (len(head) >= 8 and head[4:8] == b"ftyp")


@dataclass
class ResolvedStream:
    """协商结果：一条可直连/可中继的明文播放流。"""

    mid: str
    tier: Tier            # 实际命中档位（可能低于请求档位=降级）
    requested_tier: Tier  # 客户端请求档位
    url: str
    filename: str
    size: int
    degraded: bool


class StreamResolver:
    """会员门控 + 档位回退协商器（provider 无关，可注入假 provider 测试）。"""

    def __init__(self, provider: QualityProvider, *, probe_plain: bool = True) -> None:
        self.provider = provider
        # probe_plain=False 关闭首块嗅探（省一次 RTT；单测/可信环境用）
        self.probe_plain = probe_plain

    async def resolve(self, song: SongRef, tier_id: str, *, auto_downgrade: bool = False) -> ResolvedStream:
        """协商一条明文播放流。

        auto_downgrade=False（默认，用户显式选档）：会员不足 → MembershipRequired(403)。
        auto_downgrade=True（「自动/默认音质」模式）：把链裁剪到会员可及的最高档，
        不抛门控错误——免费档永远可达，故无需兜底特判。
        """
        target = tier_by_id(tier_id)
        if target.encrypted:
            raise TierNotPlayable(f"{target.label} 为加密音源，Typhoeus 不提供解密播放")

        membership = await self.provider.membership()
        if membership < target.requires:
            if not auto_downgrade:
                raise MembershipRequired(_gate_reason(membership, target), target.label)
        chain = fallback_chain(tier_id)
        last_code = -1
        for tier in chain:
            if membership < tier.requires:
                continue  # auto 模式：越过会员不可及的高档
            results = await self.provider.resolve_links(LinkRequest(songs=(song,), tier=tier))
            hit = next((r for r in results if r.mid == song.mid), None)
            if hit is None or not hit.playable or not hit.url:
                last_code = hit.result_code if hit else last_code
                continue
            if self.probe_plain and not await asyncio.to_thread(self._plain_ok, hit):
                last_code = -2  # 疑似加密内容
                continue
            return ResolvedStream(
                mid=song.mid,
                tier=tier,
                requested_tier=target,
                url=hit.url,
                filename=hit.filename,
                size=hit.size,
                degraded=tier is not target,
            )
        raise ProviderError(f"无可播明文档位（最后上游 result={last_code}）")

    def _plain_ok(self, link: LinkResult) -> bool:
        try:
            head = stream.sniff(link.url)
        except Exception:
            return False
        return looks_plain(head)


def _gate_reason(membership: Membership, tier: Tier) -> str:
    if membership is Membership.NONE:
        return f"「{tier.label}」需登录后开通会员"
    return f"「{tier.label}」需 {tier.requires.name} 及以上会员（当前 {membership.name}）"
