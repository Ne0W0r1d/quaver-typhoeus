"""Quaver sidecar 包入口：把仓库内 vendor 依赖注入 sys.path.

正常路径是 `uv sync` 后以 path 依赖 editable 安装（QQMusicApi + 本仓库的
typhoeus 核心）。这里的注入只是兜底，保证直接 `python run.py` 在未 sync 的
python 下也能 import 到 SDK 与 typhoeus。
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent          # vendor/Typhoeus/
_VENDOR = _ROOT.parent                                    # 主仓 vendor/

for _p in (_VENDOR / "QQMusicApi", _ROOT):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
