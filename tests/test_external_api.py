# -*- coding: utf-8 -*-
"""外部から使う入口（pmm_core / pmm_cli）のテスト。

実際の写真は使わず、その場で作った画像だけで確かめる。
フォルダー名には日本語・空白・記号をわざと入れている。

    py -3 -m unittest discover -s tests
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pmm_core as core


def make_images(folder: Path, count: int = 5) -> list[Path]:
    paths = []
    for k in range(count):
        im = Image.new("RGB", (360, 240), (30 + 40 * k, 120, 200 - 30 * k))
        ImageDraw.Draw(im).ellipse([40 + 20 * k, 40, 200 + 20 * k, 200], fill=(240, 230, 210))
        # 番号の自然順を確かめるため、2 と 10 を混ぜる
        name = ["写真 2.jpg", "写真 10.jpg", "写真 1.jpg", "b.png", "a.jpeg"][k]
        p = folder / name
        im.save(p)
        paths.append(p)
    (folder / "メモ.txt").write_text("写真ではない", encoding="utf-8")
    return paths


def fingerprint(folder: Path) -> dict:
    """フォルダー内の全ファイルの中身・更新時刻・一覧。"""
    out = {}
    for p in sorted(folder.iterdir()):
        st = p.stat()
        out[p.name] = (hashlib.sha256(p.read_bytes()).hexdigest(), st.st_size, st.st_mtime_ns)
    return out


class ExternalApiTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.folder = base / "旅行 写真 (広島) & 秋・2026"
        self.folder.mkdir()
        self.out = base / "出力 フォルダー"
        self.out.mkdir()
        make_images(self.folder)
        self.before = fingerprint(self.folder)

    def tearDown(self):
        self._tmp.cleanup()

    def assertSourceUnchanged(self):
        self.assertEqual(fingerprint(self.folder), self.before,
                         "元の写真フォルダーの中身が変わってはいけない")

    # ---------------------------------------------------------------- scan
    def test_scan_lists_only_images_in_natural_order(self):
        r = core.scan_media(str(self.folder))
        self.assertEqual(r["count"], 5)
        self.assertEqual([m["file"] for m in r["media"]],
                         ["a.jpeg", "b.png", "写真 1.jpg", "写真 2.jpg", "写真 10.jpg"])
        self.assertEqual(r["skipped_files"], 1)
        self.assertEqual(r["media"][0]["width"], 360)
        json.dumps(r)
        self.assertSourceUnchanged()

    # ---------------------------------------------------------------- analyze
    def test_analyze_without_embedding_is_json_and_legacy(self):
        r = core.analyze_photos(str(self.folder), use_embedding=False, detect_subjects=False)
        self.assertEqual(r["count"], 5)
        self.assertEqual(r["engine"]["grouping"], "legacy")
        self.assertFalse(r["engine"]["embedding_enabled"])
        for p in r["photos"]:
            self.assertIn(p["stars"], (1, 2, 3))
            self.assertIsNone(p["face_count"])      # 検出器を使わなければ None
        json.dumps(r, ensure_ascii=False)
        self.assertSourceUnchanged()

    def test_analyze_with_embedding_uses_selection_groups(self):
        caps = core.get_capabilities()
        if not caps["models"]["embedding"]:
            self.skipTest("embedding モデルが無い環境")
        r = core.analyze_photos(str(self.folder), use_embedding=True, detect_subjects=True)
        self.assertTrue(r["engine"]["embedding_enabled"])
        self.assertEqual(r["engine"]["grouping"], "selection_candidate_groups")
        self.assertEqual(r["engine"]["embedding_model"], "facebook/dinov2-small")
        self.assertTrue(all(isinstance(p["face_count"], int) for p in r["photos"]))
        self.assertSourceUnchanged()

    def test_cancel_raises_and_changes_nothing(self):
        with self.assertRaises(core.CancelledError):
            core.analyze_photos(str(self.folder), use_embedding=False,
                                detect_subjects=False, should_stop=lambda: True)
        self.assertSourceUnchanged()

    # ---------------------------------------------------------------- folders
    def test_invalid_folders_are_rejected(self):
        cases = {
            "": "invalid_folder",
            str(self.folder / "無い"): "invalid_folder",
            str(self.folder / "写真 1.jpg"): "invalid_folder",
            "C:\\": "invalid_folder",
            "\\\\server\\share\\写真": "invalid_folder",
        }
        for folder, code in cases.items():
            with self.subTest(folder=folder):
                with self.assertRaises(core.CoreError) as ctx:
                    core.scan_media(folder)
                self.assertEqual(ctx.exception.code, code)

    def test_subfolders_are_not_searched(self):
        sub = self.folder / "サブ"
        sub.mkdir()
        Image.new("RGB", (50, 50)).save(sub / "奥の写真.jpg")
        r = core.scan_media(str(self.folder))
        self.assertNotIn("奥の写真.jpg", [m["file"] for m in r["media"]])

    # ---------------------------------------------------------------- project
    def test_plan_keeps_everything_enabled_in_folder_order(self):
        analysis = core.analyze_photos(str(self.folder), use_embedding=False,
                                       detect_subjects=False)
        plan = core.create_project_plan(str(self.folder), analysis=analysis)
        self.assertEqual(core.validate_project(plan), [])
        self.assertEqual(plan["kind"], core.PROJECT_KIND)
        self.assertEqual(plan["project"]["title"], self.folder.name)
        self.assertEqual([m["order"] for m in plan["media"]], [1, 2, 3, 4, 5])
        self.assertTrue(all(m["enabled"] for m in plan["media"]),
                        "計画の叩き台では、写真を勝手に外さない")
        self.assertTrue(all(m["type"] == "image" for m in plan["media"]))
        self.assertEqual(plan["video"], core.DEFAULT_VIDEO)
        self.assertEqual(plan["recommendation_engine"]["grouping"], "legacy")
        self.assertSourceUnchanged()

    def test_plan_rejects_analysis_of_another_folder(self):
        other = Path(self._tmp.name) / "別の フォルダー"
        other.mkdir()
        make_images(other, 3)
        analysis = core.analyze_photos(str(other), use_embedding=False, detect_subjects=False)
        with self.assertRaises(core.CoreError) as ctx:
            core.create_project_plan(str(self.folder), analysis=analysis)
        self.assertEqual(ctx.exception.code, "analysis_mismatch")

    def test_validate_project_catches_unsafe_or_broken_entries(self):
        plan = core.create_project_plan(str(self.folder))
        bad = json.loads(json.dumps(plan))
        bad["media"][0]["file"] = "..\\..\\Windows\\win.ini"
        bad["media"][1]["order"] = bad["media"][2]["order"]
        bad["media"][3]["type"] = "audio"
        bad["video"]["encoder_choice"] = "rm -rf"
        bad["title_card"]["unknown"] = 1
        problems = core.validate_project(bad)
        self.assertTrue(any("ファイル名だけ" in p for p in problems))
        self.assertTrue(any("order が重複" in p for p in problems))
        self.assertTrue(any("type" in p for p in problems))
        self.assertTrue(any("encoder_choice" in p for p in problems))
        self.assertTrue(any("title_card" in p for p in problems))

    def test_video_type_is_accepted_for_future_use(self):
        plan = core.create_project_plan(str(self.folder))
        plan["media"].append({"file": "clip.mp4", "type": "video", "enabled": True,
                              "order": 99, "candidate_group": None,
                              "variant_family": None, "stars": None})
        self.assertEqual(core.validate_project(plan), [])

    def test_save_project_is_safe(self):
        plan = core.create_project_plan(str(self.folder))
        target = self.out / "広島 旅行.photomovie.json"
        saved = core.save_project(plan, target)
        self.assertEqual(Path(saved), target)
        self.assertEqual(core.load_project(target)["media"], plan["media"])
        with self.assertRaises(core.CoreError) as ctx:          # 上書きしない
            core.save_project(plan, target)
        self.assertEqual(ctx.exception.code, "output_exists")
        with self.assertRaises(core.CoreError) as ctx:          # 写真の名前では保存しない
            core.save_project(plan, self.folder / "写真 1.jpg", overwrite=True)
        self.assertEqual(ctx.exception.code, "invalid_output")
        self.assertEqual([p.name for p in self.out.iterdir()], [target.name],
                         "一時ファイルを残さない")
        self.assertSourceUnchanged()

    # ---------------------------------------------------------------- CLI
    def run_cli(self, *args):
        env = dict(os.environ, PYTHONUTF8="1")
        r = subprocess.run([sys.executable, str(ROOT / "pmm_cli.py"), *args],
                           capture_output=True, env=env, timeout=300)
        return r.returncode, json.loads(r.stdout.decode("ascii"))

    def test_cli_scan_with_unicode_path(self):
        code, out = self.run_cli("scan", "--folder", str(self.folder))
        self.assertEqual(code, 0)
        self.assertTrue(out["ok"])
        self.assertEqual(out["result"]["folder"], str(self.folder))
        self.assertEqual(out["result"]["count"], 5)
        self.assertSourceUnchanged()

    def test_cli_plan_save_and_validate(self):
        target = self.out / "計画.photomovie.json"
        code, out = self.run_cli("plan", "--folder", str(self.folder), "--title", "秋の旅",
                                 "--save", str(target))
        self.assertEqual(code, 0)
        self.assertEqual(out["result"]["saved_to"], str(target))
        self.assertEqual(out["result"]["project"]["project"]["title"], "秋の旅")
        code, out = self.run_cli("validate", "--project", str(target))
        self.assertEqual(code, 0)
        self.assertTrue(out["result"]["valid"])
        self.assertSourceUnchanged()

    def test_cli_errors_are_json_with_exit_codes(self):
        code, out = self.run_cli("scan", "--folder", str(self.folder / "無い"))
        self.assertEqual((code, out["ok"], out["error"]["code"]), (1, False, "invalid_folder"))
        code, out = self.run_cli("scan")
        self.assertEqual((code, out["error"]["code"]), (2, "usage_error"))
        code, out = self.run_cli("frobnicate")
        self.assertEqual((code, out["error"]["code"]), (2, "usage_error"))


class ConnectorManifestTest(unittest.TestCase):
    """tooldock.tool.json の宣言が、実際の CLI と結果に合っていること。

    ToolDock のコードは使わない（リポジトリは別のまま）。
    """

    @classmethod
    def setUpClass(cls):
        import pmm_cli
        cls.manifest = json.loads((ROOT / "tooldock.tool.json").read_text(encoding="utf-8"))
        parser = pmm_cli.build_parser()
        sub = next(a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction")
        cls.subparsers = sub.choices

    def test_connector_v1_basics(self):
        m = self.manifest
        self.assertEqual(m["connector_version"], 1)
        self.assertEqual(m["interface"]["entry"], "pmm_cli.py")
        self.assertEqual(m["interface"]["arguments"], "flags")
        self.assertTrue((ROOT / m["interface"]["entry"]).is_file())
        self.assertEqual(m["version"], core.TOOL_VERSION)
        self.assertEqual(m["api_version"], core.API_VERSION)
        self.assertEqual(sorted(m["capabilities"]), sorted(core.get_capabilities()["capabilities"]))

    def test_every_action_maps_to_a_real_subcommand_and_flag(self):
        for action in self.manifest["actions"]:
            with self.subTest(action=action["name"]):
                self.assertIn(action["command"], self.subparsers)
                parser = self.subparsers[action["command"]]
                known = {s for a in parser._actions for s in a.option_strings}
                props = action["input_schema"]["properties"]
                for name, prop in props.items():
                    flags = [prop[k] for k in ("x-flag", "x-flag-true", "x-flag-false") if k in prop]
                    self.assertEqual(len(flags), 1, name)
                    self.assertIn(flags[0], known, f"{name} -> {flags[0]}")
                self.assertFalse(action["input_schema"]["additionalProperties"])

    def test_declared_output_keys_are_really_returned(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        folder = Path(tmp.name) / "写真 確認"
        folder.mkdir()
        make_images(folder, 3)
        results = {
            "get_capabilities": core.get_capabilities(),
            "scan_media": core.scan_media(str(folder)),
            "analyze_photos": core.analyze_photos(str(folder), use_embedding=False,
                                                  detect_subjects=False),
            "create_project_plan": {"project": core.create_project_plan(str(folder)),
                                    "saved_to": None},
        }
        for action in self.manifest["actions"]:
            if action["name"] not in results:
                continue
            for key in action["output_schema"]["required"]:
                self.assertIn(key, results[action["name"]], f"{action['name']}.{key}")


if __name__ == "__main__":
    unittest.main()
