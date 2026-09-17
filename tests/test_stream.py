"""Range 解析与中继纯函数测试（无网络）+ 断流续传测试（本地假 CDN）。"""

from __future__ import annotations

import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from typhoeus import stream as stream_mod
from typhoeus.errors import StreamError
from typhoeus.stream import ByteRange, UpstreamResponse, open_range, resume_stream


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


# ——— 断流续传 ———


def _resp(chunks, status=206, content_range=None) -> UpstreamResponse:
    return UpstreamResponse(
        status=status, headers={}, content_length=0,
        content_range=content_range, chunks=iter(chunks),
    )


def _half_then_reset(first: bytes, exc: Exception):
    """先正常吐 first，再抛 exc —— 模拟 CDN 半路掐连接 / 读超时。"""
    def gen():
        yield first
        raise exc
    return gen()


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(stream_mod, "_RESUME_BACKOFF", 0)


def test_resume_reopens_from_last_byte(monkeypatch):
    """断流后按已发字节数带 Range 重开，拼接结果完整。"""
    calls: list[ByteRange] = []

    def fake_open(url, byte_range=None):
        calls.append(byte_range)
        return _resp([b"CCCC"], content_range="bytes 4-7/8")

    monkeypatch.setattr(stream_mod, "open_range", fake_open)
    body = resume_stream("u", _resp(_half_then_reset(b"AAAA", ConnectionResetError("peer reset"))),
                         start=0, end=7, retries=2)
    assert b"".join(body) == b"AAAACCCC"
    assert calls == [ByteRange(4, None)]


def test_resume_retries_more_than_once(monkeypatch):
    """连续断流也能接回来：每次都要从新的偏移续，且末段给完就收工。"""
    payload = b"ABCDEF"
    starts: list[int] = []

    def truncated_from(start: int):
        def gen():
            yield payload[start:start + 2]  # 每次只给 2 字节就断
            raise TimeoutError("read timed out")
        return gen()

    def fake_open(url, byte_range=None):
        starts.append(byte_range.start)
        return _resp(truncated_from(byte_range.start),
                     content_range=f"bytes {byte_range.start}-{len(payload) - 1}/{len(payload)}")

    monkeypatch.setattr(stream_mod, "open_range", fake_open)
    body = resume_stream("u", _resp(truncated_from(0)), start=0, end=len(payload) - 1, retries=4)
    assert b"".join(body) == payload
    assert starts == [2, 4]


def test_resume_refuses_when_upstream_ignores_range(monkeypatch):
    """上游回 200 全量 = 不认 Range：宁可报错也不重发已发过的字节。"""
    monkeypatch.setattr(stream_mod, "open_range", lambda url, r=None: _resp([b"x" * 100], status=200))
    with pytest.raises(StreamError, match="忽略 Range"):
        b"".join(resume_stream("u", _resp(_half_then_reset(b"A", OSError("reset"))),
                               start=1, end=99, retries=2))


def test_resume_gives_up_after_retries(monkeypatch):
    def always_truncated(url, r=None):
        return _resp(_half_then_reset(b"", ConnectionResetError()), content_range="bytes 1-3/4")

    monkeypatch.setattr(stream_mod, "open_range", always_truncated)
    with pytest.raises(StreamError, match="回源流中断"):
        b"".join(resume_stream("u", _resp(_half_then_reset(b"A", ConnectionResetError())),
                               start=0, end=3, retries=1))


def test_no_resume_when_response_already_complete(monkeypatch):
    """有界 Range 已发满就收工，不因为后面偶发异常再拉一次。"""
    monkeypatch.setattr(stream_mod, "open_range", lambda *a, **k: pytest.fail("不该重开回源"))

    def gen():
        yield b"AAAA"
        raise ConnectionResetError("close after last byte")

    assert b"".join(resume_stream("u", _resp(gen()), start=0, end=3, retries=3)) == b"AAAA"


def test_resume_passes_client_disconnect_through(monkeypatch):
    """客户端主动断开走 GeneratorExit，不是可续传错误，必须原样放行。"""
    monkeypatch.setattr(stream_mod, "open_range", lambda *a, **k: pytest.fail("不该重开回源"))
    body = resume_stream("u", _resp(iter([b"A" * 100])), start=0, end=999, retries=3)
    next(body)
    body.close()


# ——— 真实 socket 上的续传（本地假 CDN）———


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        state = self.server.state
        payload = state["payload"]
        state["hits"] += 1
        m = re.match(r"bytes=(\d+)-", self.headers.get("range") or "")
        start = int(m.group(1)) if m else 0
        body = payload[start:]
        self.send_response(206 if start else 200)
        self.send_header("content-range", f"bytes {start}-{len(payload) - 1}/{len(payload)}")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        if state["hits"] == 1:  # 首个连接：发一半就掐，制造「声明长度却少发字节」
            self.wfile.write(body[: len(body) // 3])
            self.wfile.flush()
            time.sleep(0.05)
            self.close_connection = True
            self.connection.close()
            return
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def test_resume_over_real_socket():
    payload = bytes(range(256)) * 1024  # 256 KiB
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.state = {"payload": payload, "hits": 0}
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/song.mp3"
    try:
        first = open_range(url)  # 这一条会被掐
        got = b"".join(resume_stream(url, first, start=0, end=len(payload) - 1, retries=2))
        assert got == payload
        assert srv.state["hits"] == 2
    finally:
        srv.shutdown()
        srv.server_close()


def test_open_range_still_reports_truncation_to_caller():
    """没启用续传时（retries=0）行为不变：照旧把断流暴露给调用方。"""
    payload = b"z" * 4096
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.state = {"payload": payload, "hits": 0}
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/song.mp3"
    try:
        first = open_range(url)
        with pytest.raises(StreamError):
            b"".join(resume_stream(url, first, start=0, end=len(payload) - 1, retries=0))
    finally:
        srv.shutdown()
        srv.server_close()
