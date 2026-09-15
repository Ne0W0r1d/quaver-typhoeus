"""Typhoeus 核心单测：假 provider，无网络。pytest 运行：
    cd vendor/Typhoeus && python -m pytest
"""

from __future__ import annotations

import pytest

from typhoeus.errors import (
    MembershipRequired,
    ProviderError,
    TierNotPlayable,
    UnknownTier,
)
from typhoeus.provider import LinkRequest, LinkResult, SongRef
from typhoeus.quality import (
    Membership,
    TIERS,
    available_for,
    fallback_chain,
    tier_by_id,
)
from typhoeus.resolver import StreamResolver


class FakeProvider:
    """可编程假 provider：membership + 每档位 (result 决定 playable) 的映射。

    playable_tiers 里的档位返回可播（url 带前缀便于断言）；其余档位返回
    result=104003 无权限。
    """

    def __init__(self, membership: Membership, playable_tiers: set[str],
                 encrypted_tiers: set[str] | None = None) -> None:
        self._membership = membership
        self.playable = set(playable_tiers)
        self.encrypted_tiers = encrypted_tiers or set()
        self.calls: list[str] = []

    async def membership(self) -> Membership:
        return self._membership

    async def resolve_links(self, req: LinkRequest) -> list[LinkResult]:
        tid = req.tier.id
        self.calls.append(tid)
        out = []
        for s in req.songs:
            if tid in self.playable:
                out.append(LinkResult(mid=s.mid, playable=True, url=f"fake://{tid}/{s.mid}",
                                       filename=f"{req.tier.prefix}{s.mid}{s.mid}{req.tier.ext}",
                                       result_code=0))
            elif tid in self.encrypted_tiers:
                # 模拟上游回加密链（带 ekey）——provider 适配器不应产出这种请求；
                # 若出现，解析器的明文嗅探（此处 fake 关闭）与档位映射双重拦截。
                out.append(LinkResult(mid=s.mid, playable=False, url="",
                                      filename="", result_code=0))
            else:
                out.append(LinkResult(mid=s.mid, playable=False, url="",
                                      filename="", result_code=104003))
        return out


SONG = SongRef(mid="00TEST", media_mid="00MEDIA")


@pytest.fixture()
def no_sniff(monkeypatch):
    """关闭明文嗅探（单测里 fake:// 没有真文件头）。"""
    monkeypatch.setattr(StreamResolver, "_plain_ok", lambda self, link: True)


# —— 档位模型 ——

def test_unknown_tier_rejected():
    with pytest.raises(UnknownTier):
        tier_by_id("nope")


def test_encrypted_tier_not_streamable():
    assert TIERS["vinyl"].encrypted
    with pytest.raises(TierNotPlayable):
        fallback_chain("vinyl")


def test_fallback_chain_order():
    chain = [t.id for t in fallback_chain("flac")]
    assert chain == ["flac", "640ogg", "320ogg", "320", "128"]  # rank 降序回退
    chain = [t.id for t in fallback_chain("master")]
    assert chain[0] == "master" and chain[-1] == "128"


def test_fallback_chain_deprioritize():
    # 全景声降权：master 链里 atmos51/atmos2 压到链尾（仍在链上，只是不优先）
    chain = [t.id for t in fallback_chain("master", deprioritize=("atmos51", "atmos2"))]
    assert chain[0] == "master"
    assert chain[:4] == ["master", "flac", "640ogg", "320ogg"]
    assert chain[-2:] == ["atmos51", "atmos2"]
    # 目标本身就是被降权档：仍最先尝试（显式选档语义不变）
    chain = [t.id for t in fallback_chain("atmos51", deprioritize=("atmos51", "atmos2"))]
    assert chain[0] == "atmos51" and "atmos2" in chain


def test_available_for_membership():
    assert "128" in [t.id for t in available_for(Membership.NONE)]
    assert "flac" not in [t.id for t in available_for(Membership.NONE)]
    ids = [t.id for t in available_for(Membership.SUPER)]
    assert {"flac", "master", "atmos2"} <= set(ids)


# —— 会员门控 ——

@pytest.mark.asyncio
async def test_non_member_blocked_from_sq(no_sniff):
    p = FakeProvider(Membership.NONE, {"128"})
    r = StreamResolver(p)
    with pytest.raises(MembershipRequired):
        await r.resolve(SONG, "flac")


@pytest.mark.asyncio
async def test_green_blocked_from_master(no_sniff):
    p = FakeProvider(Membership.GREEN, {"flac"})
    r = StreamResolver(p)
    with pytest.raises(MembershipRequired):
        await r.resolve(SONG, "master")


@pytest.mark.asyncio
async def test_vinyl_always_refused(no_sniff):
    p = FakeProvider(Membership.SUPER, {"flac"}, encrypted_tiers={"vinyl"})
    r = StreamResolver(p)
    with pytest.raises(TierNotPlayable):
        await r.resolve(SONG, "vinyl")


@pytest.mark.asyncio
async def test_login_required_for_paid_tier(no_sniff):
    p = FakeProvider(Membership.NONE, set())
    r = StreamResolver(p)
    with pytest.raises(MembershipRequired):
        await r.resolve(SONG, "320")


# —— 协商与回退 ——

@pytest.mark.asyncio
async def test_member_gets_requested_tier(no_sniff):
    p = FakeProvider(Membership.SUPER, {"flac", "320"})
    r = StreamResolver(p)
    out = await r.resolve(SONG, "flac")
    assert out.tier.id == "flac" and not out.degraded
    assert out.url.startswith("fake://flac/")
    assert p.calls == ["flac"]


@pytest.mark.asyncio
async def test_fallback_when_tier_unavailable(no_sniff):
    # 会员等级够 master，但该曲无母带/全景声资源（104003）→ 沿链回退到 flac 命中
    p = FakeProvider(Membership.SUPER, {"flac", "320"})
    r = StreamResolver(p)
    out = await r.resolve(SONG, "master")
    assert out.tier.id == "flac" and out.degraded
    assert p.calls == ["master", "atmos51", "atmos2", "flac"]


@pytest.mark.asyncio
async def test_no_streamable_fallback_raises(no_sniff):
    p = FakeProvider(Membership.SUPER, set())  # 所有档位 104003
    r = StreamResolver(p)
    with pytest.raises(ProviderError):
        await r.resolve(SONG, "flac")


@pytest.mark.asyncio
async def test_chain_skips_tiers_above_membership(no_sniff):
    # 「自动」模式：绿钻请求 master → 链裁剪到会员可及档（不越权取 SUPER 链），
    # flac(不可得)→…→320 命中
    p = FakeProvider(Membership.GREEN, {"320"})
    r = StreamResolver(p)
    out = await r.resolve(SONG, "master", auto_downgrade=True)
    assert out.tier.id == "320"
    assert p.calls == ["flac", "640ogg", "320ogg", "320"]  # SUPER 档未取链


# —— 明文嗅探 ——

def test_plain_magic_detection():
    from typhoeus.resolver import looks_plain

    assert looks_plain(b"fLaC\x00\x00\x00")
    assert looks_plain(b"OggS\x00\x02")
    assert looks_plain(b"\xff\xfb\x90\x00")
    assert looks_plain(b"ID3\x04")
    assert looks_plain(b"\x00\x00\x00 ftypM4A")
    assert not looks_plain(b"\x22\x19\x34\x74\x7a\x79\x42\x46")  # 实测 QMC 密文首块
    assert not looks_plain(b"NAC_\x3f\x80\x00\x00")  # NAC 自研容器：非白名单 → 拒绝
