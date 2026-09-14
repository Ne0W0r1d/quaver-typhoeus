"""HTTP Range 流中继（标准库实现，零第三方依赖）.

只做一件事：向 CDN 带 `Range: bytes=…` 取一段，原样流式转发。不落盘、不解密。
（文件级缓存交给上层 sidecar 以后另做；缓存的也只是明文档位。）
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Iterator

from typhoeus.errors import StreamError

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) Quaver/0.1 typhoeus"
_TIMEOUT = 15.0
_CHUNK = 256 * 1024

_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)", re.ASCII)


@dataclass(frozen=True)
class ByteRange:
    """解析后的请求区间（闭区间，start 可为 None=后缀）。"""

    start: int | None
    end: int | None

    @classmethod
    def parse(cls, header: str | None) -> "ByteRange | None":
        if not header:
            return None
        m = _RANGE_RE.match(header.strip())
        if not m:
            return None
        s, e = m.groups()
        return cls(int(s) if s else None, int(e) if e else None)

    def clamp(self, total: int) -> "ByteRange":
        """把 open-ended/后缀区间规整为具体闭区间。total<=0 时视为未知长度。"""
        if total <= 0:
            return self
        start, end = self.start, self.end
        if start is None:
            if end is None:
                return ByteRange(0, total - 1)
            start = max(0, total - end)
            end = total - 1
        else:
            end = total - 1 if end is None else min(end, total - 1)
        return ByteRange(start, end)


@dataclass
class UpstreamResponse:
    status: int
    headers: dict[str, str]
    content_length: int
    content_range: str | None
    chunks: Iterator[bytes]


def _clean_headers(d: dict[str, str]) -> dict[str, str]:
    drop = {"transfer-encoding", "connection", "content-encoding", "set-cookie"}
    return {k.lower(): v for k, v in d.items() if k.lower() not in drop}


def open_range(url: str, byte_range: ByteRange | None = None) -> UpstreamResponse:
    """同步打开上游区段流（放进 threadpool 调用）。失败抛 StreamError。"""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    if byte_range and (byte_range.start is not None or byte_range.end is not None):
        req.add_header(
            "Range",
            f"bytes={'' if byte_range.start is None else byte_range.start}-"
            f"{'' if byte_range.end is None else byte_range.end}",
        )
    try:
        resp = urllib.request.urlopen(req, timeout=_TIMEOUT)
    except urllib.error.HTTPError as exc:
        # 416 Range Not Satisfiable 也要把 Content-Range 带出去（客户端要靠它校正）
        if exc.code in (200, 206, 416):
            hdrs = _clean_headers(dict(exc.headers.items()))
            return UpstreamResponse(
                status=exc.code,
                headers=hdrs,
                content_length=int(hdrs.get("content-length", "0") or 0),
                content_range=hdrs.get("content-range"),
                chunks=iter(()),
            )
        raise StreamError(f"回源失败 HTTP {exc.code}") from exc
    except OSError as exc:
        raise StreamError(f"回源连接失败: {exc}") from exc
    hdrs = _clean_headers(dict(resp.headers.items()))
    return UpstreamResponse(
        status=resp.status,
        headers=hdrs,
        content_length=int(hdrs.get("content-length", "0") or 0),
        content_range=hdrs.get("content-range"),
        chunks=_iter_chunks(resp),
    )


def _iter_chunks(resp) -> Iterator[bytes]:
    try:
        while True:
            buf = resp.read(_CHUNK)
            if not buf:
                break
            yield buf
    finally:
        try:
            resp.close()
        except Exception:
            pass


def sniff(url: str, nbytes: int = 16) -> bytes:
    """取文件头若干字节用于明文判定（不落盘、不缓存）。"""
    r = open_range(url, ByteRange(0, nbytes - 1))
    head = b""
    for c in r.chunks:
        head += c
        if len(head) >= nbytes:
            break
    return head[:nbytes]
