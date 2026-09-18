"""Quaver 本地 API sidecar — 基于 L-1124/QQMusicApi 的薄适配层.

会话/凭证持久化：
- 配置根目录与 Electron 主进程共用**同一套平台规则**（真相在 ui/electron/config.mjs，
  改一边必须改两边）：
      Linux    $XDG_CONFIG_HOME/quaver-music   默认 ~/.config/quaver-music
      Windows  %AppData%/Quaver Music
      macOS    ~/Library/Application Support/Quaver Music
  Electron 拉起 sidecar 时会显式下传 QUAVER_CONFIG_DIR；手工单独跑则走内置规则。
- credential.json 存登录凭证（0600），QR 登录 DONE 时由服务端写入，token 不落浏览器。
- device.json 存 SDK 设备指纹（跨重启保号），与凭证同目录。
- 同一目录里还有客户端设置 quaver.conf（INI），那是 Electron 侧的文件，本模块不碰。
- 旧路径（~/.config/quaver、~/.local/state/quaver）的文件在首次访问时搬过来，避免丢登录态。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import sys
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

from qqmusic_api import Client, Credential
from qqmusic_api.core.exceptions import CredentialInvalidError

logger = logging.getLogger("quaver.session")


def _xdg(base_env: str, default: str) -> Path:
    return Path(os.environ.get(base_env, str(Path.home() / default))).expanduser()


def _config_dir() -> Path:
    """配置根目录（与 ui/electron/config.mjs:configDir 保持一致）."""
    override = os.environ.get("QUAVER_CONFIG_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("APPDATA", "").strip() or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "Quaver Music"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Quaver Music"
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    return Path(xdg) / "quaver-music" if xdg else Path.home() / ".config" / "quaver-music"


CONFIG_DIR = _config_dir()
CREDENTIAL_PATH = CONFIG_DIR / "credential.json"
DEVICE_PATH = CONFIG_DIR / "device.json"

# 迁移前的老位置（凭证在 XDG_CONFIG_HOME/quaver，设备指纹在 XDG_STATE_HOME/quaver）
_LEGACY_PATHS = (
    (_xdg("XDG_CONFIG_HOME", ".config") / "quaver" / "credential.json", CREDENTIAL_PATH),
    (_xdg("XDG_STATE_HOME", ".local/state") / "quaver" / "device.json", DEVICE_PATH),
)


def migrate_legacy_files() -> None:
    """把老目录里的凭证/设备指纹搬进新配置目录。目标已存在就不动（只搬一次）。"""
    for old, new in _LEGACY_PATHS:
        try:
            if new.exists() or not old.exists() or old.resolve() == new.resolve():
                continue
            new.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.move(str(old), str(new))  # 可能跨设备（Windows 上 .config 与 %AppData% 不同盘）
            os.chmod(new, stat.S_IRUSR | stat.S_IWUSR)
            logger.info("已迁移凭证文件 %s -> %s", old, new)
        except OSError:
            logger.warning("迁移 %s 失败（忽略，按未登录处理）", old, exc_info=True)


migrate_legacy_files()


def credential_has_login(credential: Credential) -> bool:
    """Credential 是否含可用登录信息（同上游 web 层判定）."""
    return credential.musicid > 0 and bool(credential.musickey)


def _load_credential_from_disk() -> Credential:
    try:
        raw = CREDENTIAL_PATH.read_text(encoding="utf-8").strip()
        if not raw:
            return Credential()
        cred = Credential.model_validate_json(raw)
        if credential_has_login(cred):
            logger.info("已加载登录凭证 musicid=%s", cred.musicid)
            return cred
    except FileNotFoundError:
        pass
    except Exception:
        logger.exception("凭证文件解析失败，按未登录处理: %s", CREDENTIAL_PATH)
        try:
            CREDENTIAL_PATH.unlink()
        except OSError:
            pass
    return Credential()


def save_credential(credential: Credential) -> None:
    """持久化凭证（原子写 + 0600）."""
    CREDENTIAL_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=CREDENTIAL_PATH.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(credential.model_dump_json())
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp, CREDENTIAL_PATH)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def clear_credential() -> None:
    try:
        CREDENTIAL_PATH.unlink()
    except FileNotFoundError:
        pass


class Session:
    """进程级 SDK Client 封装（凭证变更集中处理）."""

    def __init__(self) -> None:
        DEVICE_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = threading.RLock()
        self.client = Client(credential=_load_credential_from_disk(), device_path=str(DEVICE_PATH))
        self._listeners: list[Callable[[], None]] = []

    def add_change_listener(self, cb) -> None:
        """登录态变更回调（adopt/logout 后触发）。供 Typhoeus 等下游清缓存。"""
        self._listeners.append(cb)

    def _notify(self) -> None:
        for cb in self._listeners:
            try:
                cb()
            except Exception:
                logger.warning("session 变更监听回调失败", exc_info=True)

    @property
    def credential(self) -> Credential:
        return self.client.credential

    @property
    def logged_in(self) -> bool:
        return credential_has_login(self.client.credential)

    def require(self) -> Credential:
        cred = self.client.credential
        if not credential_has_login(cred):
            raise CredentialInvalidError("需要登录：请先在应用内扫码登录")
        return cred

    def adopt(self, credential: Credential) -> None:
        """登录成功后写入新凭证（内存 + 磁盘）."""
        with self._lock:
            self.client.credential = credential
            save_credential(credential)
            logger.info("登录凭证已更新 musicid=%s", credential.musicid)
            self._notify()

    async def logout(self) -> None:
        with self._lock:
            if self.logged_in:
                try:
                    await self.client.login.logout()
                except Exception:
                    logger.warning("上游登出失败，仅清除本地凭证", exc_info=True)
            clear_credential()
            self.client.credential = Credential()
            self._notify()


session = Session()


def self_euin() -> str:
    """当前账号的加密 UIN / 字符串 UIN（收藏歌单等接口的主键）."""
    cred = session.require()
    return cred.encrypt_uin or cred.str_musicid or str(cred.musicid)
