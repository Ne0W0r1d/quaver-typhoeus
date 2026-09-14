"""Typhoeus — Quaver 播放后端核心（音质档位 + 会员门控 + 明文流中继）.

设计原则：
- **不碰解密**：只提供明文档位（含无损/母带/全景声）的流式中继；加密档位（QMC
  mflac/mgg，带 ekey）在策略层即被拒绝，本包不含任何解密逻辑。
- **仅限会员的高档位**：高音质档位需会员身份才能取链（非会员上游会降级或回
  result=104003），本模块显式建模会员门槛并在协商时把关、按 rank 回退。
- **与具体 SDK 解耦**：核心只依赖 `QualityProvider` 协议（会员查询 + 取链），
  QQMusicApi 适配放在 `typhoeus.adapters.qqmusic`，可被任意 provider 替换。
"""

from __future__ import annotations

from typhoeus.errors import (
    MembershipRequired,
    ProviderError,
    StreamError,
    TierNotPlayable,
    TyphoeusError,
    UnknownTier,
)
from typhoeus.provider import LinkRequest, LinkResult, QualityProvider, SongRef
from typhoeus.quality import (
    STREAMABLE,
    TIERS,
    TIER_ORDER,
    Membership,
    Tier,
    TierId,
    available_for,
    fallback_chain,
    tier_by_id,
)
from typhoeus.resolver import ResolvedStream, StreamResolver
from typhoeus.stream import ByteRange, UpstreamResponse, open_range, sniff

__version__ = "0.1.0"

__all__ = [
    "TyphoeusError",
    "MembershipRequired",
    "ProviderError",
    "StreamError",
    "TierNotPlayable",
    "UnknownTier",
    "Membership",
    "Tier",
    "TierId",
    "TIERS",
    "TIER_ORDER",
    "STREAMABLE",
    "tier_by_id",
    "fallback_chain",
    "available_for",
    "QualityProvider",
    "SongRef",
    "LinkRequest",
    "LinkResult",
    "StreamResolver",
    "ResolvedStream",
    "ByteRange",
    "UpstreamResponse",
    "open_range",
    "sniff",
    "__version__",
]
