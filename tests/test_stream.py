"""Range 解析与中继纯函数测试（无网络）。"""

from __future__ import annotations

from typhoeus.stream import ByteRange


def test_parse_none():
    assert ByteRange.parse(None) is None
    assert ByteRange.parse("items=0-1") is None


def test_parse_forms():
    assert ByteRange.parse("bytes=0-1023") == ByteRange(0, 1023)
    assert ByteRange.parse("bytes=500-") == ByteRange(500, None)
    assert ByteRange.parse("bytes=-200") == ByteRange(None, 200)


def test_clamp_full_open():
    r = ByteRange(None, None).clamp(1000)
    assert r == ByteRange(0, 999)


def test_clamp_suffix():
    assert ByteRange(None, 200).clamp(1000) == ByteRange(800, 999)


def test_clamp_beyond_eof():
    assert ByteRange(900, 5000).clamp(1000) == ByteRange(900, 999)


def test_clamp_unknown_total():
    assert ByteRange(10, None).clamp(0) == ByteRange(10, None)
