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



class SectionLevelTest(unittest.TestCase):
    """preview の「使う区間の音量の見込み」が、曲全体の値ではなく実際に使う秒から出ていて、
    書き出した MP4 の区間の音量と合うこと（2 曲の境目を含む）。"""

    QUIET_THEN_LOUD = "静か→大きい (#2).wav"
    STEADY = "一定 -20 (#3).wav"

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="pmm section 試験 — ")
        base = Path(cls._tmp.name)
        cls.photos, cls.music, cls.out = base / "写真 #1", base / "曲 #2", base / "出力 #3"
        for folder in (cls.photos, cls.music, cls.out):
            folder.mkdir()
        for k in range(6):
            Image.new("RGB", (160, 90), (40 * k, 90, 160)).save(cls.photos / f"写真 {k + 1}.png")
        # lavfi の sine は振幅 1/8（RMS -21.1 dBFS）。最初の 30 秒は -32.1 dBFS、そのあと 40 秒は -10.1 dBFS。
        # 曲全体の値は大きい側に引っ張られる。2 曲目（一定）は -20.0 dBFS
        ffmpeg("-f", "lavfi", "-i", "sine=f=440:r=44100:d=30", "-f", "lavfi", "-i", "sine=f=440:r=44100:d=40",
               "-filter_complex", "[0:a]volume=0.2828[q];[1:a]volume=3.563[l];[q][l]concat=n=2:v=0:a=1",
               str(cls.music / cls.QUIET_THEN_LOUD))
        ffmpeg("-f", "lavfi", "-i", "sine=f=330:r=44100:d=40", "-af", "volume=1.131", str(cls.music / cls.STEADY))

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def compose(self, name, gain=None):
        second = {"start_photo": 4, "end_photo": 6, "audio": self.QUIET_THEN_LOUD}
        if gain is not None:
            second["gain_db"] = gain
        edits = {"selected": [f"写真 {k}.png" for k in range(1, 7)],
                 "title_card": {"duration": 2.0, "text_fade_seconds": 1.0},
                 "video": {"width": 160, "height": 90, "fps": 10, "interval_seconds": 4.0,
                           "transition_seconds": 0.5, "zoom_percent": 0.0, "encoder_choice": "cpu"},
                 "bgm_segments": [{"start_photo": 1, "end_photo": 3, "audio": self.STEADY}, second]}
        return core.compose_project(self.photos, edits, self.out / f"{name}.photomovie.json",
                                    music_folder=self.music, overwrite=True)

    def measured(self, mp4, entry):
        per_second = core._envelope(core._decode_mono(app.find_ffmpeg(), mp4))["per_second_rms_dbfs"]
        a = int(entry["starts_at"] + entry["fade_in_seconds"]) + 1
        b = int(entry["starts_at"] + entry["duration_seconds"] - entry["fade_out_seconds"]) - 1
        part = sorted(per_second[a:b])
        return part[len(part) // 2]

    def test_prediction_uses_the_seconds_that_play(self):
        result = self.compose("見込み")
        first, second = result["timeline"]["music"]
        self.assertAlmostEqual(first["expected_level"]["median_rms_dbfs"], -20.0, delta=0.5)
        self.assertAlmostEqual(second["expected_level"]["median_rms_dbfs"], -32.1, delta=0.5)
        self.assertAlmostEqual(second["step_from_previous_db"], -12.1, delta=0.7)
        self.assertTrue(any("使う区間の音量が" in w for w in result["timeline"]["warnings"]))
        whole = {t["file"]: t for t in core.analyze_music(self.music)["tracks"]}[self.QUIET_THEN_LOUD]
        self.assertGreater(whole["rms_dbfs"], -15.0)       # 曲全体の値だけを見ると大きい曲に見える
        mp4 = Path(core.render_project(result["project_file"], self.photos, self.out / "見込み.mp4",
                                       music_folder=self.music, overwrite=True)["output"])
        for entry in (first, second):
            self.assertAlmostEqual(self.measured(mp4, entry), entry["expected_level"]["median_rms_dbfs"], delta=1.0)

    def test_segment_gain_is_applied_and_predicted(self):
        result = self.compose("音量", gain=6.0)
        second = result["timeline"]["music"][1]
        self.assertEqual(second["gain_db"], 6.0)
        self.assertAlmostEqual(second["expected_level"]["median_rms_dbfs"], -26.1, delta=0.5)
        render = core.render_project(result["project_file"], self.photos, self.out / "音量.mp4",
                                     music_folder=self.music, overwrite=True)
        self.assertAlmostEqual(self.measured(Path(render["output"]), second), -26.1, delta=1.0)
        settings = json.loads(Path(render["settings_file"]).read_text(encoding="utf-8"))
        self.assertEqual(settings["bgm_segments"][1]["gain_db"], 6.0)
        self.assertNotIn("gain_db", settings["bgm_segments"][0])

    def test_gain_range_is_checked(self):
        for gain in (-30, 20, "3"):
            with self.subTest(gain=gain), self.assertRaises(core.CoreError):
                self.compose("範囲外", gain=gain)

if __name__ == "__main__":
    unittest.main(verbosity=2)
