"""Typhoeus provider 适配器 —— 基于 L-1124/QQMusicApi（Python SDK）。

**加密边界的落点**：本适配器只会把档位映射到 `SongFileType`（明文），
`EncryptedSongFileType` 不出现在任何映射表里——加密档位（QMC mflac/mgg，带 ekey）
在类型层面就无法被请求。Typhoeus 全链路（档位定义 → 协商 → 适配器 → 中继）
不存在解密代码；缓存的也只是明文档位（vkey 链接缓存 + 未来的明文音频块缓存），
缓存永不涉及解密路径。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from qqmusic_api import Client
from qqmusic_api.modules.song import SongFileInfo, SongFileType
from qqmusic_api.core.exceptions import BaseApiException

from typhoeus.errors import ProviderError
from typhoeus.provider import LinkRequest, LinkResult, SongRef
from typhoeus.quality import Membership, Tier

# Typhoeus 档位 id → SDK 明文 SongFileType。加密档位（EncryptedSongFileType）刻意缺席。
TIER_TO_SDK: dict[str, SongFileType] = {
    "128": SongFileType.MP3_128,
    "320": SongFileType.MP3_320,
    "320ogg": SongFileType.OGG_320,
    "640ogg": SongFileType.OGG_640,
    "flac": SongFileType.FLAC,
    "atmos2": SongFileType.ATMOS_2,
    "atmos51": SongFileType.ATMOS_51,
    "master": SongFileType.MASTER,
}

CDN_DOMAIN_FALLBACK = "https://isure.stream.qqmusic.qq.com/"


class QQMusicProvider:
    """把 Typhoeus 的 QualityProvider 协议接到 qqmusic_api.Client 上。

    会员等级带 TTL 缓存（默认 10 分钟）；取链走 CDN dispatch 结果拼绝对 URL。
    ``credential_state`` 是 api-server 侧的 session 对象（有 ``logged_in`` 属性），
    用于把「未登录」与「登录但非会员」区分开——两者在腾讯侧行为不同。
    """

    def __init__(self, client: Client, *, membership_ttl: float = 600.0,
                 credential_state: Any = None) -> None:
        self.client = client
        self.credential_state = credential_state
        self._membership: tuple[float, Membership] | None = None
        self._membership_ttl = membership_ttl
        self._membership_lock = asyncio.Lock()
        self._cdn_domains: list[str] | None = None

    # ---- 会员 ----
    async def membership(self) -> Membership:
        now = time.monotonic()
        if self._membership and self._membership[0] > now:
            return self._membership[1]
        async with self._membership_lock:
            now = time.monotonic()
            if self._membership and self._membership[0] > now:
                return self._membership[1]
            level = await self._fetch_membership()
            self._membership = (now + self._membership_ttl, level)
            return level

    def invalidate_membership(self) -> None:
        """登录/登出/刷新后调用，强制重查。"""
        self._membership = None

    async def _fetch_membership(self) -> Membership:
        if self.credential_state is not None and not getattr(self.credential_state, "logged_in", True):
            return Membership.NONE
        try:
            vip = await self.client.user.get_vip_info()
        except BaseApiException:
            # 拿不到会员信息按最低门槛处理，档位请求仍可能成功（免费曲可播低高档）
            return Membership.NONE
        identity = getattr(vip, "identity", None)
        if getattr(vip, "svip", 0):
            return Membership.SUPER
        if getattr(vip, "huge_vip", 0) or getattr(identity, "huge_vip", 0) or getattr(identity, "vip", 0):
            return Membership.GREEN
        return Membership.NONE

    # ---- 取链 ----
    async def resolve_links(self, req: LinkRequest) -> list[LinkResult]:
        sdk_type = TIER_TO_SDK.get(req.tier.id)
        if sdk_type is None:
            raise ProviderError(f"QQMusic provider 不支持档位 {req.tier.id}")
        assert isinstance(sdk_type, SongFileType), "适配器禁止出现加密档位类型"
        files = [SongFileInfo(mid=s.mid, media_mid=s.media_mid or None, song_type=s.song_type or None)
                 for s in req.songs]
        try:
            resp = await self.client.song.get_song_urls(files, file_type=sdk_type)
        except BaseApiException as exc:
            raise ProviderError(f"取链失败: {exc}") from exc
        domain = await self._cdn()
        out: list[LinkResult] = []
        for item in resp.data:
            playable = item.result == 0 and bool(item.purl)
            # ekey 被显式丢弃：即使上游回了解密钥匙，Typhoeus 也不透传、不落地
            out.append(LinkResult(
                mid=item.mid,
                playable=playable,
                url=(domain + item.purl) if playable else "",
                filename=item.filename,
                result_code=item.result,
            ))
        return out

    async def _cdn(self) -> str:
        import random

        if self._cdn_domains is None:
            try:
                dispatch = await self.client.song.get_cdn_dispatch()
                self._cdn_domains = dispatch.sip or []
            except Exception:
                self._cdn_domains = []
        return random.choice(self._cdn_domains) if self._cdn_domains else CDN_DOMAIN_FALLBACK
