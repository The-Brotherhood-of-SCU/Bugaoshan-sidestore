#!/usr/bin/env python3
"""从直连源生成国内镜像源（GitHub 加速前缀版本）。

用法：
    python3 scripts/make_mirror.py --channel all     # 生成 preview-mirror.json / stable-mirror.json

镜像源除了把 ``iconURL`` / ``screenshotURLs`` / ``downloadURL`` / ``sourceURL``
全部套上 ``config.mirror.prefix``（默认 https://gh-proxy.com/）之外，与直连源完全一致，
方便中国大陆网络下 SideStore 直接拉取。
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common


def mirror_app_metadata(app: dict[str, Any], prefix: str) -> dict[str, Any]:
    mirrored = copy.deepcopy(app)

    if mirrored.get("iconURL"):
        mirrored["iconURL"] = common.mirror_url(prefix, str(mirrored["iconURL"]))

    if mirrored.get("screenshotURLs"):
        mirrored["screenshotURLs"] = [
            common.mirror_url(prefix, str(url)) for url in mirrored["screenshotURLs"]
        ]

    # AltStore 也支持对象式 screenshots（含 imageURL/url），一并处理。
    def mirror_screenshots(items: Any) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            for key in ("imageURL", "url"):
                if item.get(key):
                    item[key] = common.mirror_url(prefix, str(item[key]))

    mirror_screenshots(mirrored.get("screenshots"))

    for key in ("downloadURL",):
        if mirrored.get(key):
            mirrored[key] = common.mirror_url(prefix, str(mirrored[key]))

    for entry in mirrored.get("versions") or []:
        if isinstance(entry, dict) and entry.get("downloadURL"):
            entry["downloadURL"] = common.mirror_url(prefix, str(entry["downloadURL"]))

    return mirrored


def build_mirror_source(
    source: dict[str, Any], config: dict[str, Any], channel: str
) -> dict[str, Any]:
    prefix = common.mirror_prefix(config)
    channel_config = config["channels"][channel]
    suffix = str(config.get("mirror", {}).get("nameSuffix") or "")

    data = copy.deepcopy(source)
    data["name"] = f"{channel_config['sourceName']}{suffix}"
    data["identifier"] = f"{channel_config['sourceIdentifier']}.mirror"
    data["sourceURL"] = common.mirror_url(
        prefix, common.public_url(config, common.mirror_filename(channel))
    )

    if data.get("iconURL"):
        data["iconURL"] = common.mirror_url(prefix, str(data["iconURL"]))
    if data.get("headerImageURL"):
        data["headerImageURL"] = common.mirror_url(prefix, str(data["headerImageURL"]))

    data["apps"] = [
        mirror_app_metadata(app, prefix) for app in (data.get("apps") or [])
    ]

    # news 里的图片（当前源没有，但保持脚本通用）
    for item in data.get("news") or []:
        if not isinstance(item, dict):
            continue
        for key in ("imageURL",):
            if item.get(key):
                item[key] = common.mirror_url(prefix, str(item[key]))

    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--channel", default="all", choices=["all", "preview", "stable"]
    )
    args = parser.parse_args(argv)

    config = common.load_config()
    channels = ["preview", "stable"] if args.channel == "all" else [args.channel]

    for channel in channels:
        try:
            source = common.read_source(channel)
        except FileNotFoundError:
            print(f"跳过 {channel}：直连源不存在")
            continue
        mirror = build_mirror_source(source, config, channel)
        path = common.mirror_source_path(channel)
        path.write_text(json.dumps(mirror, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        versions = (mirror.get("apps") or [{}])[0].get("versions") or []
        latest = versions[0]["version"] if versions else "（空）"
        print(f"已生成 {path.name}：{latest}，镜像前缀 {common.mirror_prefix(config)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
