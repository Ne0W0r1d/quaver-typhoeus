"""Typhoeus 异常族（供上层映射为 HTTP 状态码）."""

from __future__ import annotations


class TyphoeusError(Exception):
    """Typhoeus 基类错误。"""

    status = 400


class UnknownTier(TyphoeusError):
    """请求了未定义的音质档位。"""

    status = 422

    def __init__(self, tier: str) -> None:
        super().__init__(f"未知音质档位: {tier}")


class MembershipRequired(TyphoeusError):
    """该档位仅限会员（未登录 / 非会员）。→ 403。"""

    status = 403

    def __init__(self, msg: str, tier_label: str = "") -> None:
        super().__init__(msg)
        self.tier_label = tier_label


class TierNotPlayable(TyphoeusError):
    """档位被策略拒绝（如加密 QMC 档位）或上游无此资源。→ 451/404 由上层细分。"""

    status = 451

    def __init__(self, msg: str) -> None:
        super().__init__(msg)


class ProviderError(TyphoeusError):
    """provider 取链失败（上游 result 非 0 / 无链接）。→ 502。"""

    status = 502


class StreamError(TyphoeusError):
    """回源流获取失败（CDN 非 2xx / 连接错误）。→ 502。"""

    status = 502
