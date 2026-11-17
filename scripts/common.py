#!/usr/bin/env python3
"""Bugaoshan SideStore/AltStore 源仓库的共享工具。

约定：
- 渠道（channel）只有两个：``preview`` 与 ``stable``。
  * ``preview`` 对应上游 tag ``vX.Y.Z-preview`` / ``vX.Y.Z-preview.N`` / ``vX.Y.Z-rcN``；
  * ``stable``  对应上游正式 release 的 tag ``vX.Y.Z``（无后缀）。
- 本仓库为每个渠道维护一份源 JSON（``preview.json`` / ``stable.json``），
  每个上游 tag 对应本仓库的一个 GitHub Release，资产是同 tag 源码构建的未签名 IPA。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "source.json"
WORK_DIR = ROOT / ".work"

CHANNEL_FILES = {"preview": "preview.json", "stable": "stable.json"}
MIRROR_FILES = {"preview": "preview-mirror.json", "stable": "stable-mirror.json"}
GITHUB_HOSTS = (
    "https://github.com/",
    "https://raw.githubusercontent.com/",
    "https://raw.github.com/",
    "https://api.github.com/",
)
CHANNEL_PRERELEASE = {"preview": True, "stable": False}
_FAMILY_RANK = {"preview": 0, "rc": 1, None: 2}

TAG_RE = re.compile(
    r"^v(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
    r"(?:-(?P<family>preview|rc)(?:[.-]?(?P<serial>\d+))?)?$"
)


@dataclass(frozen=True)
class Tag:
    """解析后的上游 tag，例如 ``v2.6.0-rc3`` 或 ``v2.5.3``。"""

    name: str
    major: int
    minor: int
    patch: int
    family: str | None
    serial: int

    @property
    def version(self) -> str:
        """去掉前导 ``v`` 的版本号，写入源 JSON 的 ``version`` 字段。"""
        return self.name[1:]

    @property
    def base(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.patch)

    @property
    def channel(self) -> str:
        return "stable" if self.family is None else "preview"

    @property
    def sort_key(self) -> tuple[int, int, int, int, int]:
        """同一 base 版本内 rc 比 preview 新，数字序号按数值比较。"""
        return (*self.base, _FAMILY_RANK[self.family], self.serial)


def parse_tag(name: str) -> Tag | None:
    match = TAG_RE.match(name)
    if match is None:
        return None
    return Tag(
        name=name,
        major=int(match.group("major")),
        minor=int(match.group("minor")),
        patch=int(match.group("patch")),
        family=match.group("family"),
        serial=int(match.group("serial") or 0),
    )


def load_config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def read_source(channel: str) -> dict[str, Any]:
    path = source_path(channel)
    if not path.exists():
        raise FileNotFoundError(f"源文件不存在：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


def write_source(channel: str, data: dict[str, Any]) -> None:
    path = source_path(channel)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    path.write_text(text, encoding="utf-8")


def latest_source_version(channel: str) -> str | None:
    """源 JSON 中 ``versions[0]`` 的版本号；源为空或尚未初始化时返回 None。"""
    data = read_source(channel)
    apps = data.get("apps") or []
    if not apps:
        return None
    versions = apps[0].get("versions") or []
    if not versions:
        return None
    version = versions[0].get("version")
    return version if isinstance(version, str) and version else None


def source_path(channel: str) -> Path:
    if channel not in CHANNEL_FILES:
        raise ValueError(f"未知渠道：{channel}")
    return ROOT / CHANNEL_FILES[channel]


def mirror_filename(channel: str) -> str:
    if channel not in MIRROR_FILES:
        raise ValueError(f"未知渠道：{channel}")
    return MIRROR_FILES[channel]


def mirror_source_path(channel: str) -> Path:
    return ROOT / mirror_filename(channel)


def mirror_prefix(config: dict[str, Any]) -> str:
    prefix = str(config.get("mirror", {}).get("prefix") or "").strip()
    if not prefix:
        raise ValueError("config 缺少 mirror.prefix")
    return prefix.rstrip("/") + "/"


def mirror_url(prefix: str, url: str) -> str:
    """给 GitHub 资源 URL 套上镜像前缀；非 GitHub URL 原样返回。"""
    if not url:
        return url
    if url.startswith(prefix):
        return url
    if url.startswith(GITHUB_HOSTS):
        return prefix + url
    return url


def public_url(config: dict[str, Any], relative_path: str) -> str:
    base = str(config["publicBaseURL"]).rstrip("/")
    return f"{base}/{relative_path.lstrip('/')}"


def asset_name(version: str) -> str:
    return f"Bugaoshan-{version}.ipa"


def release_download_url(repo: str, tag: str, version: str) -> str:
    return f"https://github.com/{repo}/releases/download/{tag}/{asset_name(version)}"


def release_asset_url(repo: str, tag: str, version: str) -> str:
    """本仓库 Release 资产的 browser_download_url（与 release_download_url 等价）。"""
    return release_download_url(repo, tag, version)


def build_injection(tags: list[Tag], tag: Tag) -> tuple[str, str]:
    """返回 ``(build_name, build_number)``，即 IPA 的 CFBundleShortVersionString /
    CFBundleVersion。

    注意：这里**有意**与上游 CI 的注入规则不同。上游构建预览版时把
    ``build_name`` 写成「上一正式版」（例如 2.6.0-rc3 → 2.5.3），但 SideStore
    默认开启 App 版本校验（``isAppVersionVerificationEnabled``），安装时要求
    IPA 的 ``CFBundleShortVersionString`` 与源 JSON 的 ``version`` 完全一致，
    否则会以 ``mismatchedVersion`` 失败；而且不同 RC 之间版本号相同会让客户端
    无法判断“有新预览版”。

    因此预览渠道统一注入 tag 的真实版本号（如 ``2.6.0-rc3``），稳定渠道仍是
    ``X.Y.Z``；``build_number`` 沿用上游公式 ``major*10000 + minor*100 + patch``。
    """
    build_name = tag.version
    major, minor, patch = tag.base
    return build_name, str(major * 10000 + minor * 100 + patch)


def fetch_upstream_tags(repo: str, timeout: int = 120) -> list[Tag]:
    """用 ``git ls-remote`` 拉取上游全部 tag（不需要 clone 整个仓库）。"""
    url = f"https://github.com/{repo}.git"
    proc = subprocess.run(
        ["git", "ls-remote", "--tags", "--refs", url],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git ls-remote 失败（{url}）：{proc.stderr.strip() or proc.returncode}"
        )
    tags: list[Tag] = []
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2 or not parts[1].startswith("refs/tags/"):
            continue
        name = parts[1][len("refs/tags/") :]
        parsed = parse_tag(name)
        if parsed is not None:
            tags.append(parsed)
    return tags


def pick_latest_tag(tags: list[Tag], channel: str) -> Tag | None:
    candidates = [t for t in tags if t.channel == channel]
    if not candidates:
        return None
    return max(candidates, key=lambda t: t.sort_key)


def github_api(path: str, token: str | None = None, timeout: int = 30) -> tuple[int, Any]:
    url = f"https://api.github.com{path}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "bugaoshan-sidestore",
        },
    )
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
            return response.status, json.loads(payload) if payload else None
    except urllib.error.HTTPError as error:
        return error.code, None
    except urllib.error.URLError:
        return 0, None


def get_release(repo: str, tag: str, token: str | None = None) -> dict[str, Any] | None:
    status, payload = github_api(f"/repos/{repo}/releases/tags/{tag}", token=token)
    if status != 200 or not isinstance(payload, dict):
        return None
    return payload


def asset_sha256(asset: dict[str, Any] | None) -> str:
    """GitHub Release 资产自带的 ``digest`` 字段（形如 ``sha256:...``）。"""
    digest = str((asset or {}).get("digest") or "")
    if digest.startswith("sha256:"):
        return digest[len("sha256:") :].strip().lower()
    return ""


def find_ipa_asset(release: dict[str, Any] | None, version: str) -> dict[str, Any] | None:
    if not release:
        return None
    for asset in release.get("assets") or []:
        name = str(asset.get("name", ""))
        if name == asset_name(version) or name.lower().endswith(".ipa"):
            return asset
    return None


def emit_outputs(values: dict[str, str]) -> None:
    """写到 ``$GITHUB_OUTPUT``；本地执行时打印到 stdout。"""
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as handle:
            for key, value in values.items():
                if "\n" in value:
                    raise ValueError(f"输出 {key} 含换行，需使用分隔符写法")
                handle.write(f"{key}={value}\n")
        return
    for key, value in values.items():
        print(f"{key}={value}")


def env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return value.strip() if value else default
