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
    current_tag = common.parse_tag(f"v{current_version}") if current_version else None
    # repository_dispatch 可能排队执行：轮到旧 tag 时源 JSON 已经更新到更新版本，
    # 此时不要回头构建旧版本（除非显式 force），避免浪费一次 macOS runner。
    stale_target = bool(
        current_tag and not force and tag.sort_key < current_tag.sort_key
    )

    if stale_target:
        mode = "skip"
    elif current_version == tag.version and not force:
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
        "skip_reason": "target_tag 旧于源当前版本" if stale_target else "",
    }


def resolve_channel(channel: str, target_tag: str | None) -> str:
    """把 ``--channel all`` + ``--tag`` 归一成具体渠道（tag 自动推断）。

    ``repository_dispatch`` 只会传 tag，不传渠道；这里根据 tag 后缀判断
    preview / stable，非法 tag 或渠道冲突时抛 ``ValueError``。
    """
    if not target_tag:
        return channel
    parsed = common.parse_tag(target_tag)
    if parsed is None:
        raise ValueError(f"无法识别的 tag：{target_tag}")
    if channel == "all":
        return parsed.channel
    if channel != parsed.channel:
        raise ValueError(
            f"tag {target_tag} 属于 {parsed.channel} 渠道，不能用于 {channel} 渠道"
        )
    return channel


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
        help="强制构建指定上游 tag；渠道会从 tag 自动推断，与 --channel 冲突时报错",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        default=common.env("FORCE") in ("1", "true", "True"),
        help="强制重新构建，无视源 JSON 中已有的版本",
    )
    parser.add_argument("--repo", default=common.env("GITHUB_REPOSITORY"), help="本仓库 owner/name")
    args = parser.parse_args(argv)

    try:
        args.channel = resolve_channel(args.channel, args.tag or None)
    except ValueError as error:
        # 上游 tag 命名变化时不要让整个 job 直接失败，否则会静默停止更新。
        print(f"::warning::{error}；忽略该 tag，按各渠道最新处理")
        args.tag = ""

    config = common.load_config()
    token = common.env("GH_TOKEN") or common.env("GITHUB_TOKEN")
    tags = common.fetch_upstream_tags(config["upstreamRepo"])

    # payload 里的 tag 可能还没同步到上游（极少见）；找不到就回退为「各渠道最新」。
    if args.tag and args.tag not in {tag.name for tag in tags}:
        print(f"::warning::上游没有 tag {args.tag}，回退为各渠道最新 tag")
        args.tag = ""

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
