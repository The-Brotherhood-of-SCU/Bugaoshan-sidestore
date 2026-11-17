#!/usr/bin/env python3
"""scripts/ 的单元测试（不联网，不对真实仓库写入）。"""

from __future__ import annotations

import json
import plistlib
import stat
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import common  # noqa: E402
import make_mirror  # noqa: E402
import package_ipa  # noqa: E402
import plan as plan_module  # noqa: E402
import update_source  # noqa: E402
import validate_sources  # noqa: E402

CONFIG = common.load_config()

TAGS = [
    "v2.5.3",
    "v2.5.4-preview",
    "v2.5.4-preview.2",
    "v2.5.4-preview.9",
    "v2.5.4-preview.10",
    "v2.5.4-preview.48",
    "v2.6.0-rc",
    "v2.6.0-rc1",
    "v2.6.0-rc2",
    "v2.6.0-rc3",
    "v2.6.0",
]


def parsed(names: list[str]) -> list[common.Tag]:
    tags = [common.parse_tag(name) for name in names]
    return [tag for tag in tags if tag is not None]


class TagTests(unittest.TestCase):
    def test_parse_valid_tags(self) -> None:
        self.assertEqual(common.parse_tag("v2.6.0").version, "2.6.0")
        self.assertIsNone(common.parse_tag("v2.6.0").family)
        rc = common.parse_tag("v2.6.0-rc3")
        self.assertEqual((rc.family, rc.serial), ("rc", 3))
        preview = common.parse_tag("v2.5.4-preview.48")
        self.assertEqual((preview.family, preview.serial), ("preview", 48))
        self.assertEqual(common.parse_tag("v2.6.0-rc").serial, 0)

    def test_parse_invalid_tags(self) -> None:
        for name in ("2.6.0", "v2.6", "v2.6.0-beta1", "nightly", "v2.6.0-rc3.1"):
            self.assertIsNone(common.parse_tag(name), name)

    def test_ordering(self) -> None:
        tags = parsed(["v2.5.4-preview.9", "v2.5.4-preview.10", "v2.6.0-rc", "v2.6.0-rc3"])
        ordered = sorted(tags, key=lambda t: t.sort_key)
        self.assertEqual(
            [t.name for t in ordered],
            ["v2.5.4-preview.9", "v2.5.4-preview.10", "v2.6.0-rc", "v2.6.0-rc3"],
        )

    def test_pick_latest(self) -> None:
        tags = parsed(TAGS)
        self.assertEqual(common.pick_latest_tag(tags, "preview").name, "v2.6.0-rc3")
        self.assertEqual(common.pick_latest_tag(tags, "stable").name, "v2.6.0")
        self.assertIsNone(common.pick_latest_tag(parsed(["v2.6.0"]), "preview"))

    def test_build_injection(self) -> None:
        tags = parsed(TAGS)
        # 预览版注入 tag 的真实版本号：SideStore 默认校验 IPA 版本与源 JSON 一致，
        # 且不同 RC 必须能排出先后（否则客户端判断不出“有新预览版”）。
        self.assertEqual(
            common.build_injection(tags, common.parse_tag("v2.6.0-rc3")), ("2.6.0-rc3", "20600")
        )
        self.assertEqual(
            common.build_injection(tags, common.parse_tag("v2.5.4-preview.48")),
            ("2.5.4-preview.48", "20504"),
        )
        self.assertEqual(
            common.build_injection(tags, common.parse_tag("v2.6.0")), ("2.6.0", "20600")
        )

    def test_asset_sha256(self) -> None:
        digest = "sha256:" + "a" * 64
        self.assertEqual(common.asset_sha256({"digest": digest}), "a" * 64)
        self.assertEqual(common.asset_sha256({"digest": None}), "")
        self.assertEqual(common.asset_sha256(None), "")


class PlanTests(unittest.TestCase):
    def _plan(self, channel: str, current: str | None, release, **kwargs):
        return plan_module.plan_channel(
            channel,
            parsed(TAGS),
            CONFIG,
            current_version=current,
            release=release,
            **kwargs,
        )

    def _release(self, version: str, size: int = 123):
        return {
            "assets": [
                {
                    "name": common.asset_name(version),
                    "size": size,
                    "browser_download_url": f"https://example.com/{common.asset_name(version)}",
                    "created_at": "2026-11-17T00:00:00Z",
                    "digest": "sha256:" + "b" * 64,
                }
            ]
        }

    def test_skip_when_source_is_current(self) -> None:
        plan = self._plan("preview", "2.6.0-rc3", self._release("2.6.0-rc3"))
        self.assertEqual(plan["mode"], "skip")
        self.assertEqual(plan["tag"], "v2.6.0-rc3")

    def test_build_when_no_release(self) -> None:
        plan = self._plan("stable", "2.5.3", None)
        self.assertEqual(plan["mode"], "build")
        self.assertEqual(plan["version"], "2.6.0")
        self.assertEqual(plan["build_number"], "20600")

    def test_reuse_existing_asset(self) -> None:
        plan = self._plan("stable", "2.5.3", self._release("2.6.0"))
        self.assertEqual(plan["mode"], "reuse")
        self.assertEqual(plan["asset_size"], 123)
        self.assertEqual(plan["asset_date"], "2026-11-17")
        self.assertEqual(plan["asset_sha256"], "b" * 64)

    def test_force_rebuilds(self) -> None:
        plan = self._plan("stable", "2.6.0", self._release("2.6.0"), force=True)
        self.assertEqual(plan["mode"], "build")

    def test_target_tag(self) -> None:
        plan = self._plan("preview", None, None, target_tag="v2.5.4-preview.48")
        self.assertEqual(plan["tag"], "v2.5.4-preview.48")
        with self.assertRaises(ValueError):
            self._plan("preview", None, None, target_tag="v2.6.0")

    def test_no_tags(self) -> None:
        self.assertIsNone(
            plan_module.plan_channel(
                "preview", [], CONFIG, current_version=None, release=None
            )
        )


class UpdateSourceTests(unittest.TestCase):
    def _entry(self, version: str, size: int = 100) -> dict:
        return update_source.make_version_entry(
            version=version,
            date="2026-11-17",
            notes="notes",
            download_url=f"https://github.com/o/r/releases/download/v{version}/{common.asset_name(version)}",
            size=size,
            min_os_version="15.0",
        )

    def test_apply_version_inserts_newest_first_and_syncs_legacy_fields(self) -> None:
        source = {"apps": []}
        updated = update_source.apply_version(
            source, CONFIG, "preview", self._entry("2.6.0-rc3"), max_history=20
        )
        app = updated["apps"][0]
        self.assertEqual(app["bundleIdentifier"], CONFIG["bundleIdentifier"])
        self.assertEqual(app["version"], "2.6.0-rc3")
        self.assertEqual(app["versions"][0]["version"], "2.6.0-rc3")
        self.assertTrue(app["iconURL"].startswith("https://"))
        self.assertEqual(updated["identifier"], CONFIG["channels"]["preview"]["sourceIdentifier"])

    def test_apply_version_dedupes_and_trims(self) -> None:
        data = {"apps": []}
        for version in ("2.6.0-rc3", "2.6.0-rc2", "2.6.0-rc1"):
            data = update_source.apply_version(
                data, CONFIG, "preview", self._entry(version), max_history=20
            )
        data = update_source.apply_version(
            data, CONFIG, "preview", self._entry("2.6.0-rc3", size=999), max_history=20
        )
        versions = data["apps"][0]["versions"]
        self.assertEqual([v["version"] for v in versions], ["2.6.0-rc3", "2.6.0-rc2", "2.6.0-rc1"])
        self.assertEqual(versions[0]["size"], 999)

        trimmed = update_source.apply_version(
            data, CONFIG, "preview", self._entry("2.6.0-rc4"), max_history=2
        )
        self.assertEqual(
            [v["version"] for v in trimmed["apps"][0]["versions"]], ["2.6.0-rc4", "2.6.0-rc3"]
        )

    def test_clean_notes_adds_provenance_and_truncates(self) -> None:
        notes = update_source.clean_notes("<b>新增功能</b>\n\n\n\n修复问题", "PROV")
        self.assertTrue(notes.startswith("PROV"))
        self.assertNotIn("<b>", notes)
        self.assertNotIn("\n\n\n", notes)

        long_notes = update_source.clean_notes("x" * 10000, "PROV")
        self.assertLess(len(long_notes), update_source.NOTES_LIMIT + 100)

    def test_sha256_in_entry(self) -> None:
        entry = update_source.make_version_entry(
            version="2.6.0-rc3",
            date="2026-11-17",
            notes="n",
            download_url="https://example.com/x.ipa",
            size=1,
            min_os_version="15.0",
            sha256="C" * 64,
        )
        self.assertEqual(entry["sha256"], "c" * 64)
        without = update_source.make_version_entry(
            version="2.6.0-rc3",
            date="2026-11-17",
            notes="n",
            download_url="https://example.com/x.ipa",
            size=1,
            min_os_version="15.0",
        )
        self.assertNotIn("sha256", without)

    def test_clean_notes_strips_upstream_download_section(self) -> None:
        raw = (
            "## ⬇️ 下载 (Downloads)\n"
            "- Android: [apk](https://example.com/a.apk)\n\n"
            "### Added\n- 新功能\n"
        )
        notes = update_source.clean_notes(raw, "PROV")
        self.assertNotIn("Android", notes)
        self.assertNotIn("Downloads", notes)
        self.assertIn("### Added", notes)
        self.assertIn("新功能", notes)


class ValidateTests(unittest.TestCase):
    def _valid_source(self, channel: str = "preview") -> dict:
        data = {"apps": []}
        for version in ("2.6.0-rc3", "2.6.0-rc2"):
            data = update_source.apply_version(
                data,
                CONFIG,
                channel,
                update_source.make_version_entry(
                    version=version,
                    date="2026-11-17",
                    notes="notes",
                    download_url=(
                        "https://github.com/o/r/releases/download/"
                        f"v{version}/{common.asset_name(version)}"
                    ),
                    size=100,
                    min_os_version="15.0",
                ),
                max_history=20,
            )
        return data

    def test_valid_source_passes(self) -> None:
        errors, _ = validate_sources.validate_source(
            "preview", self._valid_source(), CONFIG, repo="o/r"
        )
        self.assertEqual(errors, [])

    def test_empty_apps_is_warning_only(self) -> None:
        errors, warnings = validate_sources.validate_source(
            "preview",
            {
                "name": "x",
                "identifier": CONFIG["channels"]["preview"]["sourceIdentifier"],
                "sourceURL": common.public_url(CONFIG, "preview.json"),
                "apps": [],
            },
            CONFIG,
        )
        self.assertEqual(errors, [])
        self.assertTrue(warnings)

    def test_bad_sha256_is_rejected(self) -> None:
        data = self._valid_source()
        data["apps"][0]["versions"][0]["sha256"] = "xyz"
        errors, _ = validate_sources.validate_source("preview", data, CONFIG, repo="o/r")
        self.assertTrue(any("sha256" in error for error in errors))

    def test_broken_source_is_rejected(self) -> None:
        data = self._valid_source()
        data["apps"][0]["versions"][0]["size"] = 0
        data["apps"][0]["versions"][1]["version"] = "2.6.0-rc9"
        errors, _ = validate_sources.validate_source("preview", data, CONFIG, repo="o/r")
        self.assertTrue(any("size" in error for error in errors))
        self.assertTrue(any("排序" in error for error in errors))

    def test_channel_mismatch_rejected(self) -> None:
        data = update_source.apply_version(
            {"apps": []},
            CONFIG,
            "stable",
            update_source.make_version_entry(
                version="2.6.0",
                date="2026-11-17",
                notes="notes",
                download_url=(
                    "https://github.com/o/r/releases/download/v2.6.0/"
                    + common.asset_name("2.6.0")
                ),
                size=100,
                min_os_version="15.0",
            ),
            max_history=20,
        )
        data["identifier"] = CONFIG["channels"]["preview"]["sourceIdentifier"]
        errors, _ = validate_sources.validate_source("preview", data, CONFIG, repo="o/r")
        self.assertTrue(any("不属于 preview" in error for error in errors))


class MirrorTests(unittest.TestCase):
    def _direct(self, channel: str = "preview") -> dict:
        data: dict = {"apps": []}
        versions = ("2.6.0-rc3", "2.6.0-rc2") if channel == "preview" else ("2.6.0",)
        for version in versions:
            data = update_source.apply_version(
                data,
                CONFIG,
                channel,
                update_source.make_version_entry(
                    version=version,
                    date="2026-11-17",
                    notes="notes",
                    download_url=(
                        "https://github.com/o/r/releases/download/"
                        f"v{version}/{common.asset_name(version)}"
                    ),
                    size=100,
                    min_os_version="15.0",
                    sha256="a" * 64,
                ),
                max_history=20,
            )
        return data

    def test_build_and_validate_mirror(self) -> None:
        direct = self._direct()
        mirror = make_mirror.build_mirror_source(direct, CONFIG, "preview")
        prefix = common.mirror_prefix(CONFIG)
        channel_config = CONFIG["channels"]["preview"]

        self.assertEqual(mirror["identifier"], channel_config["sourceIdentifier"] + ".mirror")
        self.assertTrue(mirror["sourceURL"].startswith(prefix))
        self.assertIn("mirror", mirror["sourceURL"])
        self.assertEqual(mirror["name"], channel_config["sourceName"] + CONFIG["mirror"]["nameSuffix"])

        app = mirror["apps"][0]
        self.assertTrue(app["iconURL"].startswith(prefix))
        for url in app["screenshotURLs"]:
            self.assertTrue(url.startswith(prefix))
        for entry in app["versions"]:
            self.assertTrue(entry["downloadURL"].startswith(prefix))
            self.assertNotIn("github.com/wjj-8283", entry["downloadURL"].replace(prefix, ""))

        errors, warnings = validate_sources.validate_mirror_source(
            "preview", mirror, direct, CONFIG
        )
        self.assertEqual(errors, [])

    def test_unmirrored_url_is_rejected(self) -> None:
        direct = self._direct()
        mirror = make_mirror.build_mirror_source(direct, CONFIG, "preview")
        mirror["apps"][0]["versions"][0]["downloadURL"] = direct["apps"][0]["versions"][0][
            "downloadURL"
        ]
        errors, _ = validate_sources.validate_mirror_source("preview", mirror, direct, CONFIG)
        self.assertTrue(any("downloadURL" in error for error in errors))

    def test_mirror_version_data_must_match_direct(self) -> None:
        direct = self._direct()
        mirror = make_mirror.build_mirror_source(direct, CONFIG, "preview")
        mirror["apps"][0]["versions"][0]["size"] = 999
        errors, _ = validate_sources.validate_mirror_source("preview", mirror, direct, CONFIG)
        self.assertTrue(any("size" in error for error in errors))

        broken = make_mirror.build_mirror_source(direct, CONFIG, "preview")
        broken["apps"][0]["versions"].pop()
        errors, _ = validate_sources.validate_mirror_source("preview", broken, direct, CONFIG)
        self.assertTrue(any("版本集合" in error for error in errors))

    def test_mirror_url_idempotent_and_passthrough(self) -> None:
        prefix = common.mirror_prefix(CONFIG)
        url = "https://github.com/o/r/releases/download/v1/x.ipa"
        once = common.mirror_url(prefix, url)
        self.assertEqual(common.mirror_url(prefix, once), once)
        self.assertEqual(
            common.mirror_url(prefix, "https://example.com/a.png"), "https://example.com/a.png"
        )


class PackageIpaTests(unittest.TestCase):
    def _make_app(self, root: Path, *, short_version="2.5.3", build_number="20503") -> Path:
        app = root / "Runner.app"
        app.mkdir()
        info = {
            "CFBundleIdentifier": CONFIG["bundleIdentifier"],
            "CFBundleExecutable": "Runner",
            "CFBundleShortVersionString": short_version,
            "CFBundleVersion": build_number,
            "MinimumOSVersion": "15.0",
        }
        (app / "Info.plist").write_bytes(plistlib.dumps(info))
        executable = app / "Runner"
        executable.write_bytes(b"\x00binary")
        executable.chmod(0o755)
        framework = app / "Frameworks" / "App.framework"
        framework.mkdir(parents=True)
        (framework / "App").write_bytes(b"\x00framework")
        (app / "Frameworks" / "App.framework" / "Current").symlink_to("App")
        return app

    def test_build_ipa_preserves_structure_modes_and_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = self._make_app(root)
            out = root / "Bugaoshan-2.5.3.ipa"
            metadata = package_ipa.build_ipa(
                app,
                out,
                expect_bundle_id=CONFIG["bundleIdentifier"],
                expect_short_version="2.5.3",
                expect_build_number="20503",
            )
            self.assertEqual(metadata["short_version"], "2.5.3")
            self.assertEqual(metadata["size"], out.stat().st_size)
            self.assertEqual(len(metadata["sha256"]), 64)

            with zipfile.ZipFile(out) as archive:
                names = archive.namelist()
                self.assertIn("Payload/Runner.app/Info.plist", names)
                self.assertIn("Payload/Runner.app/Runner", names)
                self.assertIn("Payload/", names)
                executable = archive.getinfo("Payload/Runner.app/Runner")
                self.assertEqual((executable.external_attr >> 16) & 0o777, 0o755)
                link = archive.getinfo("Payload/Runner.app/Frameworks/App.framework/Current")
                self.assertEqual(
                    (link.external_attr >> 16) & 0o170000, stat.S_IFLNK
                )
                self.assertEqual(archive.read(link), b"App")

    def test_set_short_version_patches_app_and_extensions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = self._make_app(root, short_version="2.6.03", build_number="20600")
            appex = app / "PlugIns" / "CourseWidgetExtension.appex"
            appex.mkdir(parents=True)
            (appex / "Info.plist").write_bytes(
                plistlib.dumps(
                    {
                        "CFBundleShortVersionString": "2.6.03",
                        "CFBundleExecutable": "CourseWidgetExtension",
                    }
                )
            )
            out = root / "preview.ipa"
            metadata = package_ipa.build_ipa(
                app,
                out,
                set_short_version="2.6.0-rc3",
                expect_short_version="2.6.0-rc3",
                expect_build_number="20600",
            )
            self.assertEqual(metadata["short_version"], "2.6.0-rc3")
            with zipfile.ZipFile(out) as archive:
                main = plistlib.loads(archive.read("Payload/Runner.app/Info.plist"))
                self.assertEqual(main["CFBundleShortVersionString"], "2.6.0-rc3")
                extension = plistlib.loads(
                    archive.read(
                        "Payload/Runner.app/PlugIns/CourseWidgetExtension.appex/Info.plist"
                    )
                )
                self.assertEqual(extension["CFBundleShortVersionString"], "2.6.0-rc3")

    def test_version_mismatch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = self._make_app(root)
            with self.assertRaises(SystemExit):
                package_ipa.build_ipa(app, root / "x.ipa", expect_short_version="9.9.9")


if __name__ == "__main__":
    unittest.main()
