#!/usr/bin/env python3
"""解析上游最新 tag，规划两个渠道的构建，并写入 GitHub Actions outputs。

用法（CI）：
    python3 scripts/plan.py                # 读 CHANNEL / TARGET_TAG / FORCE 环境变量
    python3 scripts/plan.py --channel preview --tag v2.6.0-rc3 --force

输出（GITHUB_OUTPUT）：
    preview_needed=true|false
    preview_plan={"channel":"preview","mode":"build|reuse|skip",...}
    stable_needed=...
    stable_plan=...
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common


def plan_channel(
    channel: str,
    tags: list[common.Tag],
    config: dict[str, Any],
    *,
    current_version: str | None,
    release: dict[str, Any] | None,
    target_tag: str | None = None,
    force: bool = False,
) -> dict[str, Any] | None:
    """计算单个渠道的计划；上游没有对应 tag 时返回 None（视为无需同步）。

    ``mode`` 含义：
    - ``build``：需要从上游源码构建 IPA（可能还需要发布 Release）；
    - ``reuse``：Release 资产已存在（例如上次发布成功但 JSON 提交失败），只补 JSON；
    - ``skip``：源 JSON 已是最新且资产齐全，什么都不做。
    """
    if target_tag:
        tag = common.parse_tag(target_tag)
        if tag is None:
            raise ValueError(f"无法识别的 tag：{target_tag}")
        if tag.channel != channel:
            raise ValueError(
                f"tag {target_tag} 属于 {tag.channel} 渠道，不能用于 {channel} 渠道"
            )
    else:
        tag = common.pick_latest_tag(tags, channel)
    if tag is None:
        return None

    asset = common.find_ipa_asset(release, tag.version)
    if current_version == tag.version and not force:
        mode = "skip" if asset else "build"
    elif asset and not force:
        mode = "reuse"
    else:
        mode = "build"

    build_name, build_number = common.build_injection(tags, tag)
    return {
        "channel": channel,
        "mode": mode,
        "tag": tag.name,
        "version": tag.version,
        "build_name": build_name,
        "build_number": build_number,
        "upstream_repo": config["upstreamRepo"],
        "source_file": common.CHANNEL_FILES[channel],
        "asset_url": str((asset or {}).get("browser_download_url") or ""),
        "asset_size": int((asset or {}).get("size") or 0),
        "asset_date": str((asset or {}).get("created_at") or "")[:10],
        "asset_sha256": common.asset_sha256(asset),
    }


def _channel_list(channel: str) -> list[str]:
    if channel == "all":
        return ["preview", "stable"]
    if channel in common.CHANNEL_FILES:
        return [channel]
    raise ValueError(f"未知渠道：{channel}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--channel",
        default=common.env("CHANNEL", "all"),
        choices=["all", "preview", "stable"],
        help="要规划的渠道，默认读 $CHANNEL 或 all",
    )
    parser.add_argument(
        "--tag",
        default=common.env("TARGET_TAG"),
        help="强制构建指定上游 tag（仅单渠道时可用）",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        default=common.env("FORCE") in ("1", "true", "True"),
        help="强制重新构建，无视源 JSON 中已有的版本",
    )
    parser.add_argument("--repo", default=common.env("GITHUB_REPOSITORY"), help="本仓库 owner/name")
    args = parser.parse_args(argv)

    if args.tag and args.channel == "all":
        parser.error("--tag 必须与 --channel preview 或 --channel stable 一起使用")

    config = common.load_config()
    token = common.env("GH_TOKEN") or common.env("GITHUB_TOKEN")
    tags = common.fetch_upstream_tags(config["upstreamRepo"])

    outputs: dict[str, str] = {}
    report: dict[str, Any] = {}
    for channel in _channel_list(args.channel):
        current_version = None
        try:
            current_version = common.latest_source_version(channel)
        except FileNotFoundError:
            current_version = None

        release = None
        if args.repo:
            planned_tag = args.tag or getattr(common.pick_latest_tag(tags, channel), "name", None)
            if planned_tag:
                release = common.get_release(args.repo, planned_tag, token=token)

        plan = plan_channel(
            channel,
            tags,
            config,
            current_version=current_version,
            release=release,
            target_tag=args.tag or None,
            force=args.force,
        )
        needed = plan is not None and plan["mode"] != "skip"
        outputs[f"{channel}_needed"] = "true" if needed else "false"
        outputs[f"{channel}_plan"] = json.dumps(plan, ensure_ascii=False) if plan else ""
        report[channel] = {
            "current_version": current_version,
            "needed": needed,
            "plan": plan,
            "upstream_latest": getattr(common.pick_latest_tag(tags, channel), "name", None),
        }

    common.emit_outputs(outputs)
    print(json.dumps(report, ensure_ascii=False, indent=2))

    summary_path = common.env("GITHUB_STEP_SUMMARY")
    if summary_path:
        lines = ["## 上游同步计划", "", "| 渠道 | 源 JSON 当前版本 | 上游最新 | 动作 | tag |", "| --- | --- | --- | --- | --- |"]
        for channel, item in report.items():
            action = item["plan"]["mode"] if item["plan"] else "无可用 tag"
            tag = item["plan"]["tag"] if item["plan"] else "-"
            lines.append(
                f"| {channel} | {item['current_version'] or '（空）'} | "
                f"{item['upstream_latest'] or '-'} | {action} | {tag} |"
            )
        Path(summary_path).write_text("\n".join(lines) + "\n", encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
