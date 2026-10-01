# -*- coding: utf-8 -*-
"""音声の安全（audio_safety, SI Director v2）。指定が無ければ従来どおり（legacy）。

曲はその場で FFmpeg で作る試験音だけ。0 dBFS いっぱいの矩形波は AAC にすると必ず 0 dBFS を超えるので、
headroom・limiter の効き目を数値で確かめられる。

    py -3 -m unittest discover -s tests
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from queue import Queue

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import PhotoMovieMaker_GPU as app  # noqa: E402
import pmm_core as core  # noqa: E402

FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
HOT = "熱い音 — 矩形波 (#1).wav"


def ffmpeg(*args):
    subprocess.run([app.find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", *args], check=True,
                   creationflags=FLAGS)


def true_peak(path: Path) -> float:
    err = subprocess.run([app.find_ffmpeg(), "-hide_banner", "-nostats", "-i", str(path), "-map", "0:a:0",
                          "-af", "ebur128=peak=true", "-f", "null", "-"], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", creationflags=FLAGS).stderr
    return float(re.search(r"True peak:\s*Peak:\s*(-?[\d.]+)", err[err.rfind("Summary:"):]).group(1))


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class AudioSafetyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="pmm audio 試験 — ")
        base = Path(cls._tmp.name)
        cls.photos, cls.music, cls.out = base / "写真 #1", base / "曲 & 試験 #2", base / "出力 #3"
        for folder in (cls.photos, cls.music, cls.out):
            folder.mkdir()
        for k in range(3):
            Image.new("RGB", (160, 90), (60 * k, 90, 160)).save(cls.photos / f"写真 {k + 1}.png")
        ffmpeg("-f", "lavfi", "-i", "aevalsrc=0.999*sgn(sin(2*PI*220*t)):s=44100:d=8", "-ac", "2",
               str(cls.music / HOT))

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def compose(self, name, safety="absent"):
        edits = {"selected": ["写真 1.png", "写真 2.png", "写真 3.png"],
                 "title_card": {"enabled": False},
                 "video": {"width": 160, "height": 90, "fps": 10, "interval_seconds": 2.0,
                           "transition_seconds": 0.5, "zoom_percent": 0.0, "encoder_choice": "cpu"},
                 "bgm_segments": [{"start_photo": 1, "end_photo": 3, "audio": HOT}]}
        if safety != "absent":
            edits["audio_safety"] = safety
        return core.compose_project(self.photos, edits, self.out / f"{name}.photomovie.json",
                                    music_folder=self.music, overwrite=True)["project_file"]

    def render(self, name, safety="absent"):
        result = core.render_project(self.compose(name, safety), self.photos, self.out / f"{name}.mp4",
                                     music_folder=self.music, overwrite=True)
        return result, Path(result["output"])

    def test_legacy_is_unchanged(self):
        _r, absent = self.render("指定なし")
        _r, legacy = self.render("legacy", {"mode": "legacy"})
        self.assertEqual(sha(absent), sha(legacy))
        self.assertGreater(true_peak(absent), 0.0)              # 従来は 0 dBFS を超える（試験音の性質）

    def test_headroom_lowers_the_level(self):
        _r, absent = self.render("基準")
        result, path = self.render("headroom", {"mode": "headroom", "headroom_db": 1.0})
        self.assertAlmostEqual(true_peak(absent) - true_peak(path), 1.0, delta=0.35)
        self.assertEqual(result["audio_safety"]["mode"], "headroom")

    def test_limiter_keeps_the_ceiling(self):
        result, path = self.render("limiter", {"mode": "limiter", "ceiling_db": -1.0})
        measured = true_peak(path)
        self.assertLessEqual(measured, -1.0 + 0.05)
        report = result["audio_safety"]
        self.assertEqual(report["true_peak_dbtp"], measured)
        self.assertLessEqual(report["correction_gain_db"], 0.0)
        settings = json.loads(Path(result["settings_file"]).read_text(encoding="utf-8"))
        self.assertEqual(settings["audio_safety"]["passes"], report["passes"])
        probe = subprocess.run([core.find_ffprobe(), "-v", "error", "-show_entries", "stream=codec_type,codec_name",
                                "-of", "json", str(path)], capture_output=True, text=True, creationflags=FLAGS).stdout
        self.assertIn({"codec_name": "aac", "codec_type": "audio"}, json.loads(probe)["streams"])
        # 同じ Project なら同じ結果
        _r, again = self.render("limiter 再", {"mode": "limiter", "ceiling_db": -1.0})
        self.assertEqual(sha(path), sha(again))

    def test_preview_reports_the_mode(self):
        project = self.compose("preview", {"mode": "limiter"})
        timeline = core.preview_project(project, self.photos, self.music)["timeline"]
        self.assertEqual(timeline["audio_safety"], {"mode": "limiter", "ceiling_db": -1.0})
        project = self.compose("preview 従来")
        self.assertEqual(core.preview_project(project, self.photos, self.music)["timeline"]["audio_safety"],
                         {"mode": "legacy"})

    def test_invalid_settings_are_refused(self):
        for bad in ({"mode": "loud"}, {"mode": "headroom", "headroom_db": 0}, {"mode": "headroom", "headroom_db": -2},
                    {"mode": "limiter", "ceiling_db": 1.0}, {"mode": "limiter", "gain": 1}, "limiter"):
            with self.subTest(bad=bad), self.assertRaises(core.CoreError) as ctx:
                self.compose("不正", bad)
            self.assertEqual(ctx.exception.code, "invalid_edits")

    def test_cancel_during_limited_mux_leaves_nothing(self):
        work = self.out / "中止"
        work.mkdir(exist_ok=True)
        silent = work / "video_only.mp4"
        ffmpeg("-f", "lavfi", "-i", "color=c=gray:s=32x32:r=1:d=3600", "-c:v", "libx264", "-preset", "ultrafast",
               str(silent))
        target = work / "作品.mp4"
        stop = threading.Event()
        renderer = app.VideoRenderer(
            image_paths=[self.photos / "写真 1.png"] * 360, output_path=target, width=32, height=32, fps=1,
            interval_seconds=10.0, transition_seconds=0.0, zoom_percent=0.0, blur_background=False,
            bgm_segments=[app.BGMSegment(0, 359, self.music / HOT)], encoder_pref="cpu", q=Queue(),
            stop_event=stop, title=None, audio_safety={"mode": "limiter"})
        threading.Timer(0.5, stop.set).start()
        started = time.monotonic()
        with self.assertRaises(InterruptedError):
            renderer.mux_bgm(silent)
        self.assertLess(time.monotonic() - started, 10)
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
