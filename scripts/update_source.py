#!/usr/bin/env python3
"""把新构建的 IPA 写入对应渠道的源 JSON。

用法：
    python3 scripts/update_source.py \
        --channel preview \
        --tag v2.6.0-rc3 \
        --size 12345678 \
        --download-url https://github.com/OWNER/REPO/releases/download/v2.6.0-rc3/Bugaoshan-2.6.0-rc3.ipa \
        --notes-file /tmp/notes.md
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common

PROVENANCE_PREFIX = "由 GitHub Actions 从上游"
NOTES_LIMIT = 6000
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def app_static_fields(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": config["name"],
        "bundleIdentifier": config["bundleIdentifier"],
        "developerName": config["developerName"],
        "subtitle": config["subtitle"],
        "localizedDescription": config["localizedDescription"],
        "iconURL": common.public_url(config, config["iconPath"]),
        "tintColor": config["tintColor"],
        "screenshotURLs": [
            common.public_url(config, path) for path in config["screenshotPaths"]
        ],
    }


def _strip_download_section(text: str) -> str:
    """去掉上游 Release 正文里的「⬇️ 下载」区块（那是 Android/桌面端链接）。"""
    kept: list[str] = []
    skipping = False
    for line in text.split("\n"):
        if re.match(r"^##\s", line) and ("下载" in line or "Download" in line):
            skipping = True
            continue
        if skipping and re.match(r"^#{2,3}\s", line):
            skipping = False
        if not skipping:
            kept.append(line)
    return "\n".join(kept)


def clean_notes(raw: str, provenance: str) -> str:
    """整理 release notes：去掉 HTML 标签/下载区块、统一换行、限长。"""
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    text = _HTML_TAG_RE.sub("", text)
    text = _strip_download_section(text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if PROVENANCE_PREFIX not in text:
        text = f"{provenance}\n\n{text}".strip()
    if len(text) > NOTES_LIMIT:
        text = text[: NOTES_LIMIT - 20].rstrip() + "\n\n（详见上游 Release）"
    return text


def make_version_entry(
    *,
    version: str,
    date: str,
    notes: str,
    download_url: str,
    size: int,
    min_os_version: str,
    sha256: str = "",
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "version": version,
        "date": date,
        "localizedDescription": notes,
        "downloadURL": download_url,
        "size": size,
        "minOSVersion": min_os_version,
    }
    # SideStore 在开启校验时（默认开启）会比对 IPA 的 SHA-256；缺省则跳过该检查。
    if sha256:
        entry["sha256"] = sha256.lower()
    return entry


def apply_version(
    source: dict[str, Any],
    config: dict[str, Any],
    channel: str,
    entry: dict[str, Any],
    max_history: int,
) -> dict[str, Any]:
    """返回更新后的源 JSON（不修改入参）。"""
    channel_config = config["channels"][channel]
    data = copy.deepcopy(source)

    data["name"] = channel_config["sourceName"]
    data["identifier"] = channel_config["sourceIdentifier"]
    data["sourceURL"] = common.public_url(config, common.CHANNEL_FILES[channel])

    apps = data.setdefault("apps", [])
    app = next(
        (a for a in apps if a.get("bundleIdentifier") == config["bundleIdentifier"]),
        None,
    )
    if app is None:
        app = {"versions": []}
        apps.insert(0, app)

    for key, value in app_static_fields(config).items():
        app[key] = value

    versions = [
        item for item in (app.get("versions") or []) if item.get("version") != entry["version"]
    ]
    versions.insert(0, entry)

    # SideStore 把 versions[0] 当作最新版，因此显式按 tag 语义排序（新→旧），
    # 避免手工/重跑旧版本时把顺序写乱。
    def sort_key(item: dict[str, Any]) -> tuple[int, ...]:
        parsed = common.parse_tag(f"v{item.get('version', '')}")
        return parsed.sort_key if parsed else (-1, -1, -1, -1, -1)

    versions.sort(key=sort_key, reverse=True)
    app["versions"] = versions[:max_history]

    # AltStore 1.x / 部分第三方客户端只读 App 顶层的旧字段，这里与 versions[0] 同步。
    latest = app["versions"][0]
    app["version"] = latest["version"]
    app["versionDate"] = latest["date"]
    app["versionDescription"] = latest["localizedDescription"]
    app["downloadURL"] = latest["downloadURL"]
    app["size"] = latest["size"]

    return data


def build_provenance(upstream_repo: str, tag: str) -> str:
    return (
        f"{PROVENANCE_PREFIX} [{upstream_repo}](https://github.com/{upstream_repo}) "
        f"的 tag `{tag}` 源码构建的未签名 IPA，安装时由 SideStore / AltStore 使用"
        f"你自己的证书重新签名。"
    )


def main(argv: list[str] | None = None) -> int:
    config = common.load_config()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", required=True, choices=sorted(common.CHANNEL_FILES))
    parser.add_argument("--tag", required=True, help="上游 tag，例如 v2.6.0-rc3")
    parser.add_argument("--version", default="", help="默认取 tag 去掉 v")
    parser.add_argument("--date", default="", help="默认今天（UTC），格式 YYYY-MM-DD")
    parser.add_argument("--size", type=int, required=True, help="IPA 字节数")
    parser.add_argument("--download-url", required=True, help="IPA 下载地址")
    parser.add_argument("--min-os-version", default=config["minOSVersion"])
    parser.add_argument("--sha256", default="", help="IPA 的 SHA-256（可选，SideStore 会校验）")
    parser.add_argument("--notes-file", default="", help="release notes 文件")
    parser.add_argument("--notes", default="", help="release notes 文本（与 --notes-file 二选一）")
    parser.add_argument("--max-history", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true", help="只打印结果，不写文件")
    args = parser.parse_args(argv)

    tag = common.parse_tag(args.tag)
    if tag is None:
        parser.error(f"无法识别的 tag：{args.tag}")
    if tag.channel != args.channel:
        parser.error(f"tag {args.tag} 属于 {tag.channel} 渠道")

    version = args.version or tag.version
    date = args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        parser.error(f"日期格式应为 YYYY-MM-DD：{date}")
    if args.size <= 0:
        parser.error("--size 必须为正整数")
    if not args.download_url.startswith("https://"):
        parser.error("--download-url 必须是 https 地址")
    sha256 = args.sha256.strip().lower()
    if sha256 and not re.fullmatch(r"[0-9a-f]{64}", sha256):
        parser.error("--sha256 必须是 64 位十六进制")

    if args.notes_file:
        raw_notes = Path(args.notes_file).read_text(encoding="utf-8")
    else:
        raw_notes = args.notes
    notes = clean_notes(raw_notes, build_provenance(config["upstreamRepo"], args.tag))

    entry = make_version_entry(
        version=version,
        date=date,
        notes=notes,
        download_url=args.download_url,
        size=args.size,
        min_os_version=args.min_os_version,
        sha256=sha256,
    )

    try:
        source = common.read_source(args.channel)
    except FileNotFoundError:
        source = {"apps": []}

    max_history = args.max_history or int(
        config["channels"][args.channel].get("maxHistory") or config.get("maxHistory") or 20
    )
    updated = apply_version(source, config, args.channel, entry, max_history)

    if args.dry_run:
        print(json.dumps(updated, ensure_ascii=False, indent=2))
        return 0

    common.write_source(args.channel, updated)
    print(
        f"已更新 {common.CHANNEL_FILES[args.channel]}：{version}"
        f"（{args.size} 字节，共 {len(updated['apps'][0]['versions'])} 个历史版本）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
