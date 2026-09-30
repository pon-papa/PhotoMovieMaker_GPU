# -*- coding: utf-8 -*-
"""曲（BGM の候補）の一覧と技術的な測定（SI Director v1）。

曲はその場で FFmpeg の正弦波と無音から作る試験音だけ。曲は読むだけで、変わらないことも確かめる。

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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import PhotoMovieMaker_GPU as app  # noqa: E402
import pmm_core as core  # noqa: E402

FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def ffmpeg(*args: str) -> None:
    subprocess.run([app.find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", *args],
                   check=True, creationflags=FLAGS)


def fingerprint(folder: Path) -> dict:
    return {p.name: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
            for p in sorted(folder.iterdir()) if p.is_file()}


class MusicTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="pmm music 試験 — ")
        self.addCleanup(self._tmp.cleanup)
        self.music = Path(self._tmp.name) / "曲 — 候補 & (試験) #1"
        self.music.mkdir()
        # 2 曲目: 2 秒無音 → 4 秒の音 → 3 秒無音（無音と静かな区間を確かめる）
        ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=6", str(self.music / "曲 10 — 明るい & (#A).wav"))
        ffmpeg("-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=2", "-f", "lavfi", "-i",
               "sine=frequency=330:duration=4", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=3",
               "-filter_complex", "[0:a][1:a][2:a]concat=n=3:v=0:a=1", "-c:a", "libmp3lame", "-b:a", "96k",
               str(self.music / "曲 2 静か.mp3"))
        (self.music / "メモ.txt").write_text("曲ではない", encoding="utf-8")
        (self.music / "中のフォルダー").mkdir()
        ffmpeg("-f", "lavfi", "-i", "sine=frequency=500:duration=1", str(self.music / "中のフォルダー" / "奥.wav"))
        self.before = fingerprint(self.music)

    def tearDown(self):
        self.assertEqual(fingerprint(self.music), self.before)       # 曲は読むだけ

    def test_scan_lists_only_tracks_directly_inside_in_natural_order(self):
        found = core.scan_music(self.music)
        self.assertEqual([t["file"] for t in found["tracks"]], ["曲 2 静か.mp3", "曲 10 — 明るい & (#A).wav"])
        self.assertEqual(found["skipped_files"], 1)
        self.assertEqual(found["formats"], [".aac", ".flac", ".m4a", ".mp3", ".ogg", ".wav"])

    def test_analysis_measures_only_technical_facts(self):
        result = core.analyze_music(self.music)
        tracks = {t["file"]: t for t in result["tracks"]}
        tone, quiet = tracks["曲 10 — 明るい & (#A).wav"], tracks["曲 2 静か.mp3"]
        self.assertAlmostEqual(tone["duration_seconds"], 6.0, delta=0.1)
        self.assertEqual((tone["codec"], tone["sample_rate"], tone["channels"]), ("pcm_s16le", 44100, 1))
        self.assertEqual(len(tone["per_second_rms_dbfs"]), 6)
        self.assertLess(abs(tone["rms_dbfs"] - (-21.1)), 1.0)             # 振幅 1/8 の正弦波
        self.assertIsNotNone(tone["loudness"]["integrated_lufs"])
        self.assertIsNotNone(tone["loudness"]["true_peak_dbfs"])
        self.assertEqual(tone["silences"], [])
        self.assertAlmostEqual(quiet["duration_seconds"], 9.0, delta=0.2)
        self.assertEqual(quiet["codec"], "mp3")
        self.assertEqual(quiet["leading_silence_seconds"], 2)
        self.assertIn(quiet["trailing_silence_seconds"], (2, 3))
        self.assertEqual(quiet["silences"][0], {"start_seconds": 0, "end_seconds": 2})
        self.assertEqual(len(quiet["energy_profile"]), 9)
        # 測らないものは unknown のまま（雰囲気を作らない）
        self.assertEqual(result["unknown"], ["tempo_bpm", "beats", "mood"])
        self.assertEqual(tone["tempo"]["status"], "unknown")
        self.assertIsNone(tone["tempo"]["bpm"])
        self.assertNotIn("mood", tone)

    def test_cancel_and_limits(self):
        with self.assertRaises(core.CancelledError):
            core.analyze_music(self.music, should_stop=lambda: True)
        for folder in (self.music / "無い", "\\\\server\\曲", "C:\\"):
            with self.subTest(folder=folder), self.assertRaises(core.CoreError):
                core.scan_music(folder)

    def test_cli_music_commands_are_json_with_progress(self):
        env = dict(os.environ, PYTHONUTF8="1")
        for command in ("music-scan", "music-analyze"):
            with self.subTest(command=command):
                proc = subprocess.run([sys.executable, "-B", str(ROOT / "pmm_cli.py"), command,
                                       "--folder=" + str(self.music)], capture_output=True, env=env, timeout=300)
                reply = json.loads(proc.stdout.decode("utf-8").strip().splitlines()[-1])
                self.assertTrue(reply["ok"], reply)
                self.assertEqual(reply["result"]["count"], 2)
                if command == "music-analyze":
                    self.assertIn('"phase": "music"', proc.stderr.decode("utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
