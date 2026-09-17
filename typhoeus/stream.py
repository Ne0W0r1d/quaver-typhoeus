"""HTTP Range 流中继（标准库实现，零第三方依赖）.

只做一件事：向 CDN 带 `Range: bytes=…` 取一段，原样流式转发。不落盘、不解密。
（文件级缓存交给上层 sidecar 以后另做；缓存的也只是明文档位。）

中途断流可续：`resume_stream` 会按已发字节数带 Range 重连，把「CDN 掐连接 / 读超时」
从「播放中断」降级为「一次短暂停顿」。见彼处注释。
"""

from __future__ import annotations

import http.client
import logging
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Iterator

from typhoeus.errors import StreamError

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) Quaver/0.1 typhoeus"
_TIMEOUT = 15.0
_CHUNK = 256 * 1024

# 中途断流的续传次数与退避（每次从已发字节数接着拉）
RESUME_RETRIES = 2
_RESUME_BACKOFF = 0.25

_log = logging.getLogger("typhoeus.stream")

_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)", re.ASCII)
_CONTENT_RANGE_RE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", re.ASCII)


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


def _range_start(content_range: str | None) -> int | None:
    """从 `content-range: bytes 100-199/5000` 取起始偏移。"""
    m = _CONTENT_RANGE_RE.match(content_range or "")
    return int(m.group(1)) if m else None


def _reopen_at(url: str, pos: int, cause: Exception) -> "UpstreamResponse":
    """从 pos 字节处重开一段。上游不认 Range 时宁可放弃，也不重发已发字节。"""
    nxt = open_range(url, ByteRange(pos, None))
    if nxt.status not in (200, 206):
        raise StreamError(f"续传失败 HTTP {nxt.status}") from cause
    if nxt.status == 200 and pos > 0:
        raise StreamError("上游忽略 Range（回 200 全量），放弃续传以免字节重复") from cause
    got = _range_start(nxt.content_range)
    if got is not None and got != pos:
        raise StreamError(f"续传起点不符：期望 {pos}，上游给 {got}") from cause
    return nxt


def resume_stream(
    url: str,
    first: "UpstreamResponse",
    *,
    start: int = 0,
    end: int | None = None,
    retries: int = RESUME_RETRIES,
) -> Iterator[bytes]:
    """把一段已打开的回源响应包成「断了能接着拉」的字节流。

    为什么需要：`_TIMEOUT` 经 urllib 对**每一次 read** 生效，CDN 节点也会主动掐断空闲连接。
    任一发生，一个已经声明了 content-length 的响应就会少发字节 —— 客户端（浏览器 /
    Electron 主进程的 fetch）只会把它判成「传输被终止」，用户侧就是放着放着中断。
    这里按已发字节数带 Range 重连，把中断降级成一次短暂停顿。

    start：首个响应体的绝对起始偏移（无 Range 请求即 0）。
    end：本次响应的末字节偏移（闭区间）；到齐即收工，用于有界 Range。
    """
    resp = first
    pos = start
    left = max(0, retries)
    while True:
        exc: Exception | None = None
        try:
            for buf in resp.chunks:
                pos += len(buf)
                yield buf
        except (OSError, http.client.HTTPException) as e:
            # OSError 覆盖 ConnectionResetError / TimeoutError(socket.timeout)；
            # HTTPException 覆盖 IncompleteRead。CancelledError 是 BaseException，不在此列。
            exc = e

        if end is not None and pos > end:
            return  # 该发的都发完了，后面再出什么事都不关本段
        if exc is None:
            # 正常收尾。注意：定长响应被截断时 http.client 只静默关闭连接、不抛错，
            # 所以「少收字节」必须靠 pos 比对 end 才能发现，不能指望异常。
            if end is None:
                return  # 终点未知，无从判断是否截断
            exc = StreamError(f"回源提前 EOF（已发 {pos} 字节）")

        if left <= 0:
            raise StreamError(f"回源流中断（已发 {pos} 字节，续传 {retries - left} 次）: {exc}") from exc
        left -= 1
        _log.warning("回源中断 @%d 字节（%s），续传，剩余 %d 次", pos, exc, left)
        if _RESUME_BACKOFF:
            time.sleep(_RESUME_BACKOFF)
        resp = _reopen_at(url, pos, exc)


def sniff(url: str, nbytes: int = 16) -> bytes:
    """取文件头若干字节用于明文判定（不落盘、不缓存）。"""
    r = open_range(url, ByteRange(0, nbytes - 1))
    head = b""
    for c in r.chunks:
        head += c
        if len(head) >= nbytes:
            break
    return head[:nbytes]
