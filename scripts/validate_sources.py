#!/usr/bin/env python3
"""校验 preview.json / stable.json 是否符合 AltStore / SideStore 源格式。

用法：
    python3 scripts/validate_sources.py                # 结构校验
    python3 scripts/validate_sources.py --check-urls   # 另外 HEAD 校验最新 IPA 地址
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}:\d{2}(Z|[+-]\d{2}:\d{2})?)?$")
_OS_RE = re.compile(r"^\d+(\.\d+){1,2}$")


def validate_source(
    channel: str,
    data: dict[str, Any],
    config: dict[str, Any],
    repo: str | None = None,
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    channel_config = config["channels"][channel]

    for key in ("name", "identifier", "sourceURL", "apps"):
        if key not in data:
            errors.append(f"缺少顶层字段 {key}")
    if data.get("identifier") != channel_config["sourceIdentifier"]:
        errors.append(
            f"identifier 应为 {channel_config['sourceIdentifier']}，实际 {data.get('identifier')!r}"
        )
    expected_source_url = common.public_url(config, common.CHANNEL_FILES[channel])
    if data.get("sourceURL") != expected_source_url:
        errors.append(f"sourceURL 应为 {expected_source_url}，实际 {data.get('sourceURL')!r}")

    apps = data.get("apps")
    if not isinstance(apps, list):
        return errors + ["apps 必须是数组"], warnings
    if not apps:
        warnings.append("apps 为空：源尚未同步到任何版本（首次 CI 后会填充）")
        return errors, warnings

    bundle_id = config["bundleIdentifier"]
    matched = [a for a in apps if a.get("bundleIdentifier") == bundle_id]
    if not matched:
        errors.append(f"apps 中缺少 bundleIdentifier={bundle_id} 的应用")
        return errors, warnings

    seen_versions: set[str] = set()
    for app in matched:
        for key in ("name", "developerName", "iconURL", "localizedDescription"):
            if not isinstance(app.get(key), str) or not app[key].strip():
                errors.append(f"应用 {key} 缺失或为空")
        if not str(app.get("iconURL", "")).startswith("https://"):
            errors.append("iconURL 必须是 https 地址")

        versions = app.get("versions")
        if not isinstance(versions, list) or not versions:
            errors.append("versions 必须是非空数组（SideStore 以 versions[0] 为最新版）")
            continue

        previous_key: tuple[int, ...] | None = None
        for index, entry in enumerate(versions):
            label = f"versions[{index}]"
            for key in ("version", "date", "downloadURL", "size"):
                if key not in entry:
                    errors.append(f"{label} 缺少 {key}")
            version = str(entry.get("version", ""))
            if version in seen_versions:
                errors.append(f"{label} 版本 {version} 重复")
            seen_versions.add(version)

            parsed = common.parse_tag(f"v{version}")
            if parsed is None:
                errors.append(f"{label} 版本号 {version!r} 不符合上游 tag 规则")
            elif parsed.channel != channel:
                errors.append(f"{label} 版本 {version} 不属于 {channel} 渠道")
            else:
                key = parsed.sort_key
                if previous_key is not None and key > previous_key:
                    errors.append(
                        f"{label} 版本 {version} 比前一条更新：versions 必须按新→旧排序"
                    )
                previous_key = key

            date = str(entry.get("date", ""))
            if not _DATE_RE.match(date):
                errors.append(f"{label} date 格式非法：{date!r}")

            url = str(entry.get("downloadURL", ""))
            if not url.startswith("https://"):
                errors.append(f"{label} downloadURL 必须是 https 地址")
            elif repo and f"/releases/download/" not in url:
                errors.append(f"{label} downloadURL 应指向本仓库 Release：{url}")

            size = entry.get("size")
            if not isinstance(size, int) or size <= 0:
                errors.append(f"{label} size 必须是正整数，实际 {size!r}")

            min_os = entry.get("minOSVersion")
            if min_os is not None and not _OS_RE.match(str(min_os)):
                errors.append(f"{label} minOSVersion 格式非法：{min_os!r}")

            sha256 = entry.get("sha256")
            if sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", str(sha256)):
                errors.append(f"{label} sha256 必须是 64 位小写十六进制")

        latest = versions[0]
        if app.get("version") != latest.get("version"):
            errors.append("App 顶层 version 与 versions[0].version 不一致")
        if app.get("downloadURL") != latest.get("downloadURL"):
            errors.append("App 顶层 downloadURL 与 versions[0].downloadURL 不一致")
        if app.get("size") != latest.get("size"):
            errors.append("App 顶层 size 与 versions[0].size 不一致")

    return errors, warnings


def download_sha256(url: str, timeout: int = 180) -> str:
    digest = hashlib.sha256()
    request = urllib.request.Request(
        url, headers={"User-Agent": "bugaoshan-sidestore-validate"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        for chunk in iter(lambda: response.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def head_size(url: str, timeout: int = 60) -> tuple[int, int | None]:
    """返回 (HTTP 状态码, Content-Length)；HEAD 被拒时退化为 Range GET。"""
    headers = {"User-Agent": "bugaoshan-sidestore-validate"}
    for method in ("HEAD", "GET"):
        request = urllib.request.Request(url, method=method, headers=headers)
        if method == "GET":
            request.add_header("Range", "bytes=0-0")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                length = response.headers.get("Content-Length")
                return response.status, int(length) if length and length.isdigit() else None
        except urllib.error.HTTPError as error:
            if method == "GET":
                return error.code, None
        except urllib.error.URLError:
            if method == "GET":
                return 0, None
    return 0, None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-urls", action="store_true", help="校验最新 IPA 的下载地址")
    parser.add_argument(
        "--check-hashes", action="store_true", help="下载最新 IPA 并校验 JSON 中的 sha256"
    )
    parser.add_argument("--repo", default=common.env("GITHUB_REPOSITORY"))
    args = parser.parse_args(argv)

    config = common.load_config()
    failed = False
    for channel in ("preview", "stable"):
        path = common.source_path(channel)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            print(f"✗ {path.name} 不存在")
            failed = True
            continue
        except json.JSONDecodeError as error:
            print(f"✗ {path.name} 不是合法 JSON：{error}")
            failed = True
            continue

        errors, warnings = validate_source(channel, data, config, repo=args.repo or None)
        for warning in warnings:
            print(f"⚠ {path.name}: {warning}")
        for error in errors:
            print(f"✗ {path.name}: {error}")
        if errors:
            failed = True
            continue

        latest = data["apps"][0]["versions"][0] if data.get("apps") else None
        if args.check_urls and latest:
            status, length = head_size(latest["downloadURL"])
            if status not in (200, 206):
                print(f"✗ {path.name}: {latest['downloadURL']} 返回 HTTP {status}")
                failed = True
            elif length is not None and length != latest["size"]:
                print(
                    f"✗ {path.name}: 远程大小 {length} 与 JSON size {latest['size']} 不一致"
                )
                failed = True
            elif args.check_hashes and latest.get("sha256"):
                actual = download_sha256(latest["downloadURL"])
                if actual != latest["sha256"]:
                    print(
                        f"✗ {path.name}: IPA sha256 {actual} 与 JSON {latest['sha256']} 不一致"
                    )
                    failed = True
                else:
                    print(f"✓ {path.name}: 最新版本 {latest['version']} 下载地址与 sha256 均通过")
            else:
                print(f"✓ {path.name}: 最新版本 {latest['version']} 下载地址可访问")

    if failed:
        print("源校验失败")
        return 1
    print("源校验通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
