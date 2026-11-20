#!/usr/bin/env python3
"""从被构建的上游 tag 中检测应使用的 Flutter 版本。

优先级（从高到低）：
1. ``--override`` / ``FLUTTER_VERSION_OVERRIDE``：仓库变量，便于临时钉住版本；
2. 上游 CI 的 pin：``.github/actions/setup/action.yml`` → ``.github/workflows/*.yml``
   里的 ``flutter-version:``；
3. ``.metadata`` 的 ``version.revision`` → Flutter 官方 releases manifest 的 hash 映射
   （注意：上游的 ``.metadata`` 可能滞后，实测 main 上的 revision 对应 3.38.5 而 CI pin 是 3.44.9，
   所以此路径只作为没有 CI pin 时的兜底）；
4. ``--default``（默认 3.44.9）；加 ``--fail-on-default`` 时若走到这一步直接失败
   （CI 里用它触发 Actions 失败通知，避免静默用旧版本构建）。

默认不联网。加 ``--check-drift`` 才会拉取 releases manifest 并比较 CI pin 与
``.metadata`` revision 映射出的版本（仅用于人工排查，不建议放进日常构建）。

用法：
    python3 scripts/detect_flutter_version.py --upstream-dir .work/upstream
    # 在 GitHub Actions 中会自动写 $GITHUB_OUTPUT / $GITHUB_ENV
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

MANIFEST_URL = (
    "https://storage.googleapis.com/flutter_infra_release/releases/"
    "releases_macos.json"
)
DEFAULT_VERSION = "3.44.9"
# flutter-version: "3.44.9" / '3.44.9' / 3.44.9
VERSION_RE = re.compile(r"""flutter-version:\s*['"]?(\d+(?:\.\d+)+)""")
REVISION_RE = re.compile(r"""revision:\s*['"]?([0-9a-f]{7,40})['"]?""")

ManifestFetcher = Callable[[], Optional[dict[str, Any]]]


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def find_ci_pin(upstream_dir: Path) -> tuple[str, str] | None:
    """返回 (版本, 来源文件相对路径)；找不到返回 None。"""
    setup_action = upstream_dir / ".github" / "actions" / "setup" / "action.yml"
    match = VERSION_RE.search(_read_text(setup_action))
    if match:
        return match.group(1), str(setup_action.relative_to(upstream_dir))

    workflows_dir = upstream_dir / ".github" / "workflows"
    if workflows_dir.is_dir():
        for workflow in sorted(workflows_dir.glob("*.y*ml")):
            match = VERSION_RE.search(_read_text(workflow))
            if match:
                return match.group(1), str(workflow.relative_to(upstream_dir))
    return None


def read_metadata_revision(upstream_dir: Path) -> str | None:
    match = REVISION_RE.search(_read_text(upstream_dir / ".metadata"))
    return match.group(1) if match else None


def fetch_manifest(timeout: int = 20) -> dict[str, Any] | None:
    """下载 Flutter releases manifest；失败返回 None（非致命）。"""
    request = urllib.request.Request(
        MANIFEST_URL, headers={"User-Agent": "bugaoshan-sidestore"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except Exception as error:  # noqa: BLE001 - 网络问题不应阻断版本检测
        print(f"::warning::获取 Flutter releases manifest 失败：{error}")
        return None


def version_for_revision(manifest: dict[str, Any] | None, revision: str | None) -> str | None:
    if not manifest or not revision:
        return None
    for release in manifest.get("releases") or []:
        release_hash = str(release.get("hash") or "")
        if release_hash == revision or (
            len(revision) >= 7 and release_hash.startswith(revision)
        ):
            version = str(release.get("version") or "")
            if version:
                return version
    return None


def detect(
    upstream_dir: Path,
    *,
    override: str = "",
    default: str = DEFAULT_VERSION,
    check_drift: bool = False,
    manifest_fetcher: ManifestFetcher = fetch_manifest,
) -> dict[str, Any]:
    override = override.strip()
    if override:
        return {
            "version": override,
            "source": "override",
            "drift": "skipped",
            "metadata_version": "",
        }

    pin = find_ci_pin(upstream_dir)
    revision = read_metadata_revision(upstream_dir)
    metadata_version = ""
    drift = "unknown"

    # 只有「没有 CI pin 但存在 .metadata revision」或「显式要求漂移检查」时才需要 manifest，
    # 避免没有 .metadata 的仓库/测试也去联网。
    need_manifest = (pin is None and revision is not None) or check_drift
    if need_manifest:
        manifest = manifest_fetcher()
        metadata_version = version_for_revision(manifest, revision) or ""
        if check_drift and pin and metadata_version:
            drift = "false" if metadata_version == pin[0] else "true"

    if pin:
        return {
            "version": pin[0],
            "source": pin[1],
            "drift": drift,
            "metadata_version": metadata_version,
        }
    if metadata_version:
        return {
            "version": metadata_version,
            "source": ".metadata revision",
            "drift": drift,
            "metadata_version": metadata_version,
        }
    return {
        "version": default,
        "source": "default",
        "drift": drift,
        "metadata_version": metadata_version,
    }


def emit_outputs(result: dict[str, Any]) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as handle:
            for key in ("version", "source", "drift", "metadata_version"):
                handle.write(f"flutter_{key}={result[key]}\n")
    env_path = os.environ.get("GITHUB_ENV")
    if env_path:
        with open(env_path, "a", encoding="utf-8") as handle:
            handle.write(f"FLUTTER_VERSION={result['version']}\n")
            handle.write(f"FLUTTER_SOURCE={result['source']}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream-dir", required=True, type=Path)
    parser.add_argument(
        "--override", default=os.environ.get("FLUTTER_VERSION_OVERRIDE", "")
    )
    parser.add_argument("--default", default=DEFAULT_VERSION)
    parser.add_argument(
        "--fail-on-default",
        action="store_true",
        help="检测不到上游 pin / .metadata 映射时直接失败（CI 用，触发失败通知）",
    )
    parser.add_argument(
        "--check-drift",
        action="store_true",
        help="拉取 Flutter releases manifest，比较 CI pin 与 .metadata revision 对应版本（默认关闭）",
    )
    args = parser.parse_args(argv)

    if not args.upstream_dir.is_dir():
        print(f"::error::上游目录不存在：{args.upstream_dir}")
        return 1

    override = args.override.strip()
    if override and not re.fullmatch(r"\d+(\.\d+)+", override):
        print(f"::error::FLUTTER_VERSION 覆盖值格式非法：{override!r}")
        return 1

    result = detect(
        args.upstream_dir,
        override=args.override,
        default=args.default,
        check_drift=args.check_drift,
    )

    if result["source"] == "default":
        message = (
            "未能从上游源码检测到 Flutter 版本（.github pin 与 .metadata 映射都不可用）；"
            "请检查上游 .github/actions/setup/action.yml、.github/workflows/*.yml、.metadata 结构，"
            "或设置仓库变量 FLUTTER_VERSION。"
        )
        if args.fail_on_default:
            print(f"::error::{message}")
            return 1
        print(f"::warning::{message} 回退到默认 {result['version']}。")
    if result["drift"] == "true":
        print(
            f"::warning::Flutter 版本可能漂移：CI pin={result['version']}，"
            f".metadata revision 对应 {result['metadata_version']}"
        )

    print(
        f"Flutter {result['version']}（来源：{result['source']}，"
        f"drift={result['drift']}）"
    )
    emit_outputs(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
