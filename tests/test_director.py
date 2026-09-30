# -*- coding: utf-8 -*-
"""SI Director v1 の部品: 決めたとおりに組み立てる（compose）・時間割（preview）・判断の記録（director）。

判断（どれを使うか・順序・選曲）はしない部品であることを確かめる。写真と曲はその場で作る合成物だけ。

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

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import PhotoMovieMaker_GPU as app  # noqa: E402
import pmm_core as core  # noqa: E402

FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
SHORT = "短い曲 — 3秒 & (#1).mp3"
LONG = "長い曲 # 20秒 (#2).wav"


def fingerprint(folder: Path) -> dict:
    return {p.name: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
            for p in sorted(folder.iterdir()) if p.is_file()}


def director(**extra) -> dict:
    return {"director_version": 1, "concept": "前半は落ち着いて、後半は少し楽しく、最後は余韻（試験）",
            "benchmark": False, "known_facts": ["写真 6 枚は合成画像"], "inferences": [], **extra}


class DirectorTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="pmm director 試験 — ")
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.photos = base / "写真 & 素材 (元) #1"
        self.music = base / "曲 — 候補 & (試験) #2"
        self.out = base / "出力 (試験) #3"
        for folder in (self.photos, self.music, self.out):
            folder.mkdir()
        for k in range(6):
            Image.new("RGB", (320, 200), (40 + 30 * k, 120, 180)).save(self.photos / f"写真 {k + 1}.jpg")
        ffmpeg = app.find_ffmpeg()
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=3", "-c:a", "libmp3lame", str(self.music / SHORT)],
                       check=True, creationflags=FLAGS)
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        "sine=frequency=330:duration=20", str(self.music / LONG)], check=True, creationflags=FLAGS)
        self.before = {"photos": fingerprint(self.photos), "music": fingerprint(self.music)}

    def tearDown(self):
        self.assertEqual({"photos": fingerprint(self.photos), "music": fingerprint(self.music)}, self.before)

    def edits(self, **extra) -> dict:
        edits = {"selected": ["写真 5.jpg", "写真 2.jpg", "写真 3.jpg", "写真 6.jpg"],
                 "title_card": {"main": "試験 — 作品", "sub": "合成", "date": "2026", "duration": 3.0},
                 "video": {"interval_seconds": 4.0, "transition_seconds": 1.0, "camera_mode": "subject"},
                 "bgm_segments": [{"start_photo": 1, "end_photo": 2, "audio": SHORT},
                                  {"start_photo": 3, "end_photo": 4, "audio": LONG}],
                 "target_duration_seconds": 19}
        edits.update(extra)
        return edits

    def test_compose_follows_the_decisions_exactly(self):
        result = core.compose_project(self.photos, self.edits(director=director()), self.out / "作品.photomovie.json",
                                      music_folder=self.music)
        project = result["project"]
        self.assertEqual(core.shown_files(project), ["写真 5.jpg", "写真 2.jpg", "写真 3.jpg", "写真 6.jpg"])
        self.assertEqual([m["file"] for m in project["media"] if not m["enabled"]], ["写真 1.jpg", "写真 4.jpg"])
        self.assertEqual((project["title_card"]["main"], project["title_card"]["sub"]), ("試験 — 作品", "合成"))
        self.assertEqual(project["video"]["camera_mode"], "subject")
        self.assertEqual(project["project"]["music_folder"], str(self.music))
        timeline = result["timeline"]
        self.assertEqual((timeline["total_seconds"], timeline["title_seconds"], timeline["seconds_per_photo"]),
                         (19.0, 3.0, 4.0))
        self.assertEqual([p["start_seconds"] for p in timeline["photos"]], [3.0, 7.0, 11.0, 15.0])
        short, long_ = timeline["music"]
        self.assertEqual((short["starts_at"], short["repeats"]), (3.0, True))       # 冒頭オフセット 3.0 秒・3 秒の曲
        self.assertEqual(long_["repeats"], False)
        self.assertAlmostEqual(long_["starts_at"], 11.7, places=3)                   # 曲間の無音 0.7 秒
        self.assertEqual(timeline["silent_ranges"][0], {"start_seconds": 0.0, "end_seconds": 3.0})
        self.assertTrue(any("繰り返されます" in w for w in timeline["warnings"]))
        self.assertIn("00:00 タイトル「試験 — 作品」 / 合成 / 2026", timeline["lines"])
        self.assertIn("00:19 終わり", timeline["lines"])
        record = json.loads(Path(result["director_file"]).read_text(encoding="utf-8"))
        self.assertEqual(record["kind"], "photomoviemaker.director_plan")
        self.assertEqual(record["project_file"], "作品.photomovie.json")
        self.assertEqual((record["computed"]["photos_selected"], record["computed"]["photos_excluded"]), (4, 2))
        self.assertEqual(record["computed"]["bgm_tracks"], [SHORT, LONG])
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["作品.director.json", "作品.photomovie.json"])
        # 保存した Project は preview でも同じ時間割になる（何も書かない）
        preview = core.preview_project(result["project_file"], self.photos, self.music)
        self.assertEqual(preview["timeline"]["lines"], timeline["lines"])
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["作品.director.json", "作品.photomovie.json"])

    def test_nothing_is_chosen_or_overwritten_by_the_tool(self):
        target = self.out / "作品.photomovie.json"
        core.compose_project(self.photos, self.edits(bgm_segments=[]), target)
        with self.assertRaises(core.CoreError) as ctx:
            core.compose_project(self.photos, self.edits(bgm_segments=[]), target)
        self.assertEqual(ctx.exception.code, "output_exists")
        cases = [({"selected": []}, "invalid_edits"),
                 ({"selected": ["写真 1.jpg", "写真 1.jpg"]}, "invalid_edits"),
                 ({"selected": ["無い.jpg"]}, "missing_media"),
                 ({"selected": ["写真 1.jpg"], "effects": {}}, "invalid_edits"),
                 ({"selected": ["写真 1.jpg"], "video": {"shader": "x"}}, "invalid_edits"),
                 ({"selected": ["写真 1.jpg"], "title_card": {"font": "x"}}, "invalid_edits"),
                 ({"selected": ["写真 1.jpg"], "bgm_segments": [{"start_photo": 1, "end_photo": 1, "audio": SHORT}]},
                  "music_folder_required"),
                 ({"selected": ["写真 1.jpg"], "bgm_segments": [{"start_photo": 1, "end_photo": 2, "audio": SHORT}]},
                  "invalid_project")]
        for edits, code in cases:
            with self.subTest(edits=edits):
                with self.assertRaises(core.CoreError) as ctx:
                    core.compose_project(self.photos, edits, self.out / "別.photomovie.json",
                                         music_folder=self.music if "bgm_segments" in edits and code != "music_folder_required" else None)
                self.assertEqual(ctx.exception.code, code)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["作品.photomovie.json"])

    def test_director_plan_is_a_checked_sidecar_only(self):
        result = core.compose_project(self.photos, self.edits(), self.out / "作品.photomovie.json", music_folder=self.music)
        bad = [director(director_version=2), director(concept=""), director(computed={}), director(kind="x"),
               director(note="元は C:\\Users\\someone\\写真 にあった"), director(note="\\\\server\\share"),
               director(padding="x" * 70000), ["not", "an", "object"]]
        for plan in bad:
            with self.subTest(plan=str(plan)[:60]):
                with self.assertRaises(core.CoreError) as ctx:
                    core.save_director_plan(result["project_file"], plan)
                self.assertEqual(ctx.exception.code, "invalid_director_plan")
        saved = core.save_director_plan(result["project_file"], director(metrics={"render_seconds": 1.5}))
        with self.assertRaises(core.CoreError):
            core.save_director_plan(result["project_file"], director())             # 上書きしない
        core.save_director_plan(result["project_file"], director(revision=1), overwrite=True)
        self.assertEqual(json.loads(Path(saved["director_file"]).read_text(encoding="utf-8"))["revision"], 1)

    def test_cli_compose_preview_director(self):
        env = dict(os.environ, PYTHONUTF8="1")

        def cli(*args):
            proc = subprocess.run([sys.executable, "-B", str(ROOT / "pmm_cli.py"), *args], capture_output=True,
                                  env=env, timeout=300)
            return json.loads(proc.stdout.decode("utf-8").strip().splitlines()[-1])

        target = self.out / "作品 (CLI).photomovie.json"
        reply = cli("compose", "--folder=" + str(self.photos), "--edits-json=" + json.dumps(self.edits(), ensure_ascii=False),
                    "--save=" + str(target), "--music-folder=" + str(self.music))
        self.assertTrue(reply["ok"], reply)
        reply = cli("preview", "--project=" + str(target), "--folder=" + str(self.photos), "--music-folder=" + str(self.music))
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["result"]["timeline"]["total_seconds"], 19.0)
        reply = cli("director", "--project=" + str(target), "--plan-json=" + json.dumps(director(), ensure_ascii=False))
        self.assertTrue(reply["ok"], reply)
        reply = cli("compose", "--folder=" + str(self.photos), "--edits-json={broken", "--save=" + str(self.out / "x.photomovie.json"))
        self.assertEqual(reply["error"]["code"], "invalid_edits")


if __name__ == "__main__":
    unittest.main(verbosity=2)
