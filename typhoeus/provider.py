"""QualityProvider 协议：Typhoeus 核心与具体音源 SDK（QQMusicApi 等）之间的边界。

核心只认这个协议；换后端（如 C++/Zig 原生实现或别的音源）= 换一个 provider。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from typhoeus.quality import Membership, Tier


@dataclass(frozen=True)
class SongRef:
    """一首歌在取链所需的最小信息。"""

    mid: str
    media_mid: str = ""
    song_type: int = 0


@dataclass(frozen=True)
class LinkRequest:
    """provider 取链请求：一批歌 + 目标档位。provider 自行映射到自己 SDK 的类型枚举。"""

    songs: tuple[SongRef, ...]
    tier: Tier


@dataclass
class LinkResult:
    """单曲取链结果（已判定可播性，不含 ekey——加密链在 provider 层就被丢弃）。"""

    mid: str
    playable: bool
    url: str = ""
    filename: str = ""
    result_code: int = -1
    size: int = 0
    extra: dict = field(default_factory=dict)


@runtime_checkable
class QualityProvider(Protocol):
    """音源后端协议。"""

    async def membership(self) -> Membership:
        """当前账号会员等级（未登录 = NONE）。带缓存由实现决定。"""
        ...

    async def resolve_links(self, req: LinkRequest) -> list[LinkResult]:
        """批量取明文档位播放链接；无权限/无资源必须回 playable=False。"""
        ...
