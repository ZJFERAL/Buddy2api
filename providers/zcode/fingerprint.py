"""每账号客户端指纹（设备档案）—— 默认真机形态，随机池作换发/兜底。

用途：消灭「多账号同一设备」关联信号。每账号分配独立 device_mid
（billing/messages 身份头共用），平台/语言/时区按真实主流组合取值。

合规 = 官方客户端真实会出现的组合（协议事实）：
  - X-Platform = {platform}-{arch}：darwin×arm64/x64、win32×x64、linux×x64
  - X-Os-Version = os.release() 语义，按平台从版本池取
  - 语言/时区取真实地区对（zh-CN↔上海、en-US↔纽约/洛杉矶…）
  - 分辨率取桌面端常见值；device_mid = UUIDv4
"""

from __future__ import annotations

import os
import re
import secrets
import uuid
from dataclasses import dataclass, field

# 平台×架构（官方 process.platform-process.arch 真实组合）
_PLATFORM_ARCHS = (
    ("darwin", "arm64"),
    ("darwin", "x64"),
    ("win32", "x64"),
    ("linux", "x64"),
)
# os.release() 语义版本池
_OS_VERSIONS = {
    "darwin": ("22.6.0", "23.6.0", "24.5.0", "24.6.0", "25.5.0"),
    "win32": ("10.0.19045", "10.0.22000", "10.0.22621", "10.0.22631", "10.0.26100", "10.0.26200"),
    "linux": ("5.15.0-91-generic", "6.1.0-18-amd64", "6.8.0-45-generic"),
}
# 语言-时区真实地区对
_LOCALES = (
    ("zh-CN", "Asia/Shanghai"),
    ("en-US", "America/New_York"),
    ("en-US", "America/Los_Angeles"),
    ("en-GB", "Europe/London"),
    ("de-DE", "Europe/Berlin"),
    ("ja-JP", "Asia/Tokyo"),
    ("ko-KR", "Asia/Seoul"),
    ("en-SG", "Asia/Singapore"),
)
# 桌面端常见分辨率
_SCREENS = (
    "1920x1080", "2560x1440", "3840x2160", "5120x2880",
    "2560x1600", "1728x1117", "1512x982", "1440x900", "1366x768",
)

_SCREEN_RE = re.compile(r"^\d{3,4}x\d{3,4}$")
_RELEASE_SHAPE = re.compile(r"^\d+\.\d+(\.\d+)?[\w.\-]*$")


@dataclass(frozen=True)
class DeviceProfile:
    platform: str
    arch: str
    os_version: str
    language: str
    timezone: str
    screen: str
    device_mid: str = field(default_factory=lambda: str(uuid.uuid4()))

    @property
    def platform_full(self) -> str:
        return f"{self.platform}-{self.arch}"

    @property
    def os_category(self) -> str:
        if self.platform in ("darwin", "macos"):
            return "macos"
        if self.platform in ("win32", "windows"):
            return "windows"
        return "linux"

    def to_dict(self) -> dict:
        return {
            "platform": self.platform, "arch": self.arch,
            "os_version": self.os_version, "language": self.language,
            "timezone": self.timezone, "screen": self.screen,
            "device_mid": self.device_mid,
        }


def _validate(profile: DeviceProfile) -> None:
    if (profile.platform, profile.arch) not in _PLATFORM_ARCHS:
        raise ValueError(f"非法平台组合: {profile.platform_full}")
    if profile.os_version not in _OS_VERSIONS.get(profile.platform, ()):
        raise ValueError(f"os_version 与平台不符: {profile.platform}/{profile.os_version}")
    if (profile.language, profile.timezone) not in _LOCALES:
        raise ValueError(f"语言/时区组合不真实: {profile.language}/{profile.timezone}")
    if not _SCREEN_RE.match(profile.screen):
        raise ValueError(f"分辨率形态非法: {profile.screen}")
    uuid.UUID(profile.device_mid)


def random_profile() -> DeviceProfile:
    """随机设备档案（生成时自校验）。"""
    platform, arch = secrets.choice(_PLATFORM_ARCHS)
    language, timezone = secrets.choice(_LOCALES)
    profile = DeviceProfile(
        platform=platform,
        arch=arch,
        os_version=secrets.choice(_OS_VERSIONS[platform]),
        language=language,
        timezone=timezone,
        screen=secrets.choice(_SCREENS),
        device_mid=str(uuid.uuid4()),
    )
    _validate(profile)
    return profile


def host_profile() -> DeviceProfile:
    """宿主机真实档案（平台/时区/语言/内核版本）。采集失败退随机池。"""
    try:
        import sys
        import time

        plat = {"win32": "win32", "darwin": "darwin"}.get(sys.platform, "linux")
        arch = {"x86_64": "x64", "AMD64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(
            os.environ.get("PROCESSOR_ARCHITECTURE", "") or _machine_arch(), "arm64"
        )
        release = os.uname().release if hasattr(os, "uname") else ""
        if not release or not _RELEASE_SHAPE.match(release):
            release = secrets.choice(_OS_VERSIONS.get(plat, _OS_VERSIONS["darwin"]))
        # 语言/时区：取本机环境（缺省 zh-CN/Asia/Shanghai）
        lang = os.environ.get("LANG", "").split(".")[0] or "zh_CN"
        lang = lang.replace("_", "-")
        tz = time.tzname[1] if time.daylight else (time.tzname[0] or "Asia/Shanghai")
        language = lang if any(loc == lang for loc, _ in _LOCALES) else "zh-CN"
        timezone = tz if any(tz == x for _, x in _LOCALES) else "Asia/Shanghai"
        profile = DeviceProfile(
            platform=plat,
            arch=arch,
            os_version=release,
            language=language,
            timezone=timezone,
            screen=secrets.choice(_SCREENS),
            device_mid=str(uuid.uuid4()),
        )
        # host_real 放宽：语言/时区/内核版本以真机事实为准
        if (profile.platform, profile.arch) not in _PLATFORM_ARCHS:
            raise ValueError(f"非法平台组合: {profile.platform_full}")
        uuid.UUID(profile.device_mid)
        return profile
    except (ValueError, OSError):
        return random_profile()


def _machine_arch() -> str:
    try:
        import platform as _platform

        return _platform.machine().lower()
    except Exception:  # noqa: BLE001
        return ""


def parse_profile(data: object) -> DeviceProfile | None:
    """从 dict 还原档案；不合规返回 None（回退到重新分配）。"""
    if not isinstance(data, dict):
        return None
    try:
        profile = DeviceProfile(
            platform=str(data.get("platform") or ""),
            arch=str(data.get("arch") or ""),
            os_version=str(data.get("os_version") or ""),
            language=str(data.get("language") or ""),
            timezone=str(data.get("timezone") or ""),
            screen=str(data.get("screen") or ""),
            device_mid=str(data.get("device_mid") or ""),
        )
        _validate(profile)
        return profile
    except (ValueError, TypeError):
        return None


def profile_for(account: dict) -> DeviceProfile:
    """取账号档案：extra.fingerprint 已有则还原，否则分配真机/随机并回写。"""
    extra = account.get("extra")
    if isinstance(extra, dict):
        parsed = parse_profile(extra.get("fingerprint"))
        if parsed is not None:
            return parsed
    profile = host_profile()
    patch = dict(extra or {})
    patch["fingerprint"] = profile.to_dict()
    import database as db  # 延迟 import 避免循环依赖

    db.update_account(int(account.get("id") or 0), {"extra": patch})
    return profile


def rotate(account: dict) -> DeviceProfile:
    """换发全新档案（device_mid 必变；风控后换设备语义）。"""
    profile = random_profile()
    extra = dict(account.get("extra") or {})
    extra["fingerprint"] = profile.to_dict()
    import database as db

    db.update_account(int(account.get("id") or 0), {"extra": extra})
    return profile