#!/usr/bin/env python3
"""把未签名的 .app 打包成 SideStore / AltStore 可用的 .ipa，并输出元数据。

用法：
    python3 scripts/package_ipa.py \
        --app build/ios/iphoneos/Runner.app \
        --out dist/Bugaoshan-2.6.0-rc3.ipa \
        --expect-bundle-id io.github.thebrotherhoodofscu.bugaoshan.ios \
        --expect-short-version 2.5.3 \
        --expect-build-number 20503

输出一行 JSON：bundle_id / short_version / build_number / min_os_version / size / sha256。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import shutil
import stat
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)


def load_app_info(app_dir: Path) -> dict[str, Any]:
    plist_path = app_dir / "Info.plist"
    if not plist_path.exists():
        raise SystemExit(f"{app_dir} 不是有效的 .app：缺少 Info.plist")
    return plistlib.loads(plist_path.read_bytes())


def check_app(
    app_dir: Path,
    info: dict[str, Any],
    *,
    expect_bundle_id: str | None,
    expect_short_version: str | None,
    expect_build_number: str | None,
) -> None:
    bundle_id = str(info.get("CFBundleIdentifier", ""))
    executable = str(info.get("CFBundleExecutable", ""))
    problems: list[str] = []
    if expect_bundle_id and bundle_id != expect_bundle_id:
        problems.append(f"CFBundleIdentifier 期望 {expect_bundle_id}，实际 {bundle_id}")
    if expect_short_version and str(info.get("CFBundleShortVersionString")) != expect_short_version:
        problems.append(
            f"CFBundleShortVersionString 期望 {expect_short_version}，"
            f"实际 {info.get('CFBundleShortVersionString')}"
        )
    if expect_build_number and str(info.get("CFBundleVersion")) != expect_build_number:
        problems.append(
            f"CFBundleVersion 期望 {expect_build_number}，实际 {info.get('CFBundleVersion')}"
        )
    executable_path = app_dir / executable if executable else None
    if not executable_path or not executable_path.is_file():
        problems.append(f"缺少主可执行文件 {executable!r}")
    if problems:
        raise SystemExit("IPA 校验失败：\n- " + "\n- ".join(problems))


def _add_entry(zf: zipfile.ZipFile, name: str, mode: int, data: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=_ZIP_EPOCH)
    info.create_system = 3
    info.external_attr = (mode & 0xFFFF) << 16
    if name.endswith("/"):
        info.external_attr |= 0x10
        info.compress_type = zipfile.ZIP_STORED
    else:
        info.compress_type = zipfile.ZIP_DEFLATED
    zf.writestr(info, data, compresslevel=6)


def write_ipa(payload_root: Path, out_path: Path) -> None:
    """按 IPA 规范写入 zip：Payload/ 为根，保留权限位与符号链接。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        out_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True
    ) as zf:
        _add_entry(zf, "Payload/", stat.S_IFDIR | 0o755, b"")
        for dirpath, dirnames, filenames in os.walk(payload_root):
            dirnames.sort()
            filenames.sort()
            base = Path(dirpath)
            for name in dirnames + filenames:
                path = base / name
                arcname = path.relative_to(payload_root.parent).as_posix()
                st = os.lstat(path)
                mode = st.st_mode & 0o7777
                if stat.S_ISLNK(st.st_mode):
                    _add_entry(
                        zf, arcname, stat.S_IFLNK | 0o777, os.readlink(path).encode("utf-8")
                    )
                elif stat.S_ISDIR(st.st_mode):
                    _add_entry(zf, arcname + "/", stat.S_IFDIR | mode, b"")
                else:
                    info = zipfile.ZipInfo(arcname, date_time=_ZIP_EPOCH)
                    info.create_system = 3
                    info.external_attr = (stat.S_IFREG | mode) << 16
                    info.compress_type = zipfile.ZIP_DEFLATED
                    with open(path, "rb") as source, zf.open(info, "w") as target:
                        shutil.copyfileobj(source, target, 1024 * 1024)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_ipa(
    app_dir: Path,
    out_path: Path,
    *,
    expect_bundle_id: str | None = None,
    expect_short_version: str | None = None,
    expect_build_number: str | None = None,
) -> dict[str, Any]:
    app_dir = app_dir.resolve()
    if app_dir.suffix != ".app" or not app_dir.is_dir():
        raise SystemExit(f"不是 .app 目录：{app_dir}")
    info = load_app_info(app_dir)
    check_app(
        app_dir,
        info,
        expect_bundle_id=expect_bundle_id,
        expect_short_version=expect_short_version,
        expect_build_number=expect_build_number,
    )

    with tempfile.TemporaryDirectory(prefix="ipa-") as temp_dir:
        payload = Path(temp_dir) / "Payload"
        payload.mkdir()
        shutil.copytree(app_dir, payload / app_dir.name, symlinks=True)
        write_ipa(payload, out_path)

    return {
        "bundle_id": str(info.get("CFBundleIdentifier", "")),
        "short_version": str(info.get("CFBundleShortVersionString", "")),
        "build_number": str(info.get("CFBundleVersion", "")),
        "min_os_version": str(info.get("MinimumOSVersion", "")),
        "size": out_path.stat().st_size,
        "sha256": sha256_of(out_path),
        "path": str(out_path),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--expect-bundle-id", default=None)
    parser.add_argument("--expect-short-version", default=None)
    parser.add_argument("--expect-build-number", default=None)
    args = parser.parse_args(argv)

    metadata = build_ipa(
        args.app,
        args.out,
        expect_bundle_id=args.expect_bundle_id,
        expect_short_version=args.expect_short_version,
        expect_build_number=args.expect_build_number,
    )
    print(json.dumps(metadata, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
