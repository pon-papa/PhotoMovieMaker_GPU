# -*- coding: utf-8 -*-
"""BGM 付きの書き出し（Core API / CLI）が、画面と同じ VideoRenderer・同じ曲の合成を通ること（SI Director v1）。

写真と曲はその場で作る合成物だけ。曲は正弦波の試験音で、品質を見るためのものではない。

    py -3 -m unittest discover -s tests
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from queue import Queue

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import PhotoMovieMaker_GPU as app  # noqa: E402
import pmm_core as core  # noqa: E402

FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
TRACK_A = "試験音 A — 440 & (#1).wav"
TRACK_B = "試験音 B — 660 (#2).mp3"


def ffmpeg(*args: str) -> None:
    subprocess.run([app.find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", *args],
                   check=True, creationflags=FLAGS)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fingerprint(folder: Path) -> dict:
    return {p.name: (sha(p), p.stat().st_mtime_ns) for p in sorted(folder.iterdir()) if p.is_file()}


def streams(path: Path) -> dict:
    probe = core.find_ffprobe()
    out = subprocess.run([probe, "-v", "error", "-show_entries", "stream=codec_type,duration", "-of", "json",
                          str(path)], capture_output=True, text=True, creationflags=FLAGS).stdout
    return {s["codec_type"]: float(s["duration"]) for s in json.loads(out)["streams"]}


def ffmpeg_pids() -> set[str]:
    if os.name != "nt":
        return set()
    out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, errors="replace").stdout
    return {line.split('","')[1] for line in out.splitlines() if "ffmpeg" in line.lower() and '","' in line}


class BgmRenderBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="pmm bgm render 試験 — ")
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.photos = base / "写真 & 素材 (元) #1"
        self.music = base / "曲 — 候補 & (試験) #2"
        self.out = base / "出力 (試験) #3"
        for folder in (self.photos, self.music, self.out):
            folder.mkdir()
        for k in range(5):
            im = Image.new("RGB", (480, 320), (40 + 30 * k, 110, 200 - 25 * k))
            ImageDraw.Draw(im).ellipse([40 + 20 * k, 40, 220 + 20 * k, 220], fill=(240, 230, 210))
            im.save(self.photos / f"写真 {k + 1}.jpg", quality=90)
        ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=6", str(self.music / TRACK_A))
        ffmpeg("-f", "lavfi", "-i", "sine=frequency=660:duration=3", "-c:a", "libmp3lame", "-b:a", "128k",
               str(self.music / TRACK_B))
        self.before = {"photos": fingerprint(self.photos), "music": fingerprint(self.music)}

    def tearDown(self):
        self.assertEqual({"photos": fingerprint(self.photos), "music": fingerprint(self.music)}, self.before)

    def project(self, segments, name="計画 (BGM).photomovie.json") -> Path:
        project = core.create_project_plan(self.photos, title="基準 — BGM")
        project["video"].update(width=320, height=180, fps=15, interval_seconds=1.5, transition_seconds=0.4,
                                encoder_choice="cpu", camera_mode="legacy")
        project["title_card"].update(duration=1.2, fade_seconds=0.4, text_fade_seconds=0.4)
        project["project"]["music_folder"] = str(self.music)
        project["bgm_segments"] = segments
        return Path(core.save_project(project, self.out / name, overwrite=True))

    def gui_render(self, target: Path, segments) -> Path:
        """画面（App.start）と同じ呼び方で VideoRenderer を動かす。"""
        renderer = app.VideoRenderer(
            image_paths=sorted(self.photos.glob("*.jpg"), key=app.natural_key), output_path=target,
            width=320, height=180, fps=15, interval_seconds=1.5, transition_seconds=0.4, zoom_percent=8.0,
            blur_background=True, bgm_segments=segments, encoder_pref="cpu", q=Queue(),
            stop_event=threading.Event(),
            title=app.TitleCard(main="基準 — BGM", duration=1.2, fade_seconds=0.4, text_fade_seconds=0.4),
            bgm_timing=app.BGMTiming(), camera_mode=app.CAMERA_LEGACY)
        renderer.run()
        return target


class BgmRenderTest(BgmRenderBase):
    TWO = [{"start_photo": 1, "end_photo": 3, "audio": TRACK_A},
           {"start_photo": 4, "end_photo": 5, "audio": TRACK_B, "start_file": "写真 4.jpg"}]

    def test_connector_render_uses_the_same_renderer_as_the_gui(self):
        result = core.render_project(self.project(self.TWO), self.photos, self.out / "作品.mp4",
                                     music_folder=self.music)
        self.assertEqual(result["bgm_tracks"], [TRACK_A, TRACK_B])
        gui = self.gui_render(self.out / "画面" / "作品.mp4",
                              [app.BGMSegment(0, 2, self.music / TRACK_A), app.BGMSegment(3, 4, self.music / TRACK_B)])
        # 同じ計画なら、画面の書き出しと Core の書き出しは同じバイト列の MP4 になる
        self.assertEqual(sha(Path(result["output"])), sha(gui))
        found = streams(Path(result["output"]))
        self.assertAlmostEqual(found["video"], result["duration_seconds"], delta=0.1)
        self.assertAlmostEqual(found["audio"], result["duration_seconds"], delta=0.1)
        settings = json.loads(Path(result["settings_file"]).read_text(encoding="utf-8"))
        self.assertEqual([(s["start_photo"], s["end_photo"], Path(s["audio"]).name) for s in settings["bgm_segments"]],
                         [(1, 3, TRACK_A), (4, 5, TRACK_B)])

    def test_bgm_less_render_is_unchanged(self):
        result = core.render_project(self.project([]), self.photos, self.out / "無音.mp4")
        self.assertEqual(result["bgm_tracks"], [])
        gui = self.gui_render(self.out / "画面" / "無音.mp4", [])
        self.assertEqual(sha(Path(result["output"])), sha(gui))
        self.assertNotIn("audio", streams(Path(result["output"])))

    def test_refusals_write_nothing(self):
        plan = self.project(self.TWO)
        other = Path(self._tmp.name) / "別の曲"
        other.mkdir()
        cases = [(dict(music_folder=None), "music_folder_required"),
                 (dict(music_folder=other), "folder_mismatch"),
                 (dict(output_file=self.music / "x.mp4"), "invalid_output")]
        for extra, code in cases:
            args = dict(music_folder=self.music, output_file=self.out / "x.mp4")
            args.update(extra)
            with self.subTest(code=code):
                with self.assertRaises(core.CoreError) as ctx:
                    core.render_project(plan, self.photos, args["output_file"], music_folder=args["music_folder"])
                self.assertEqual(ctx.exception.code, code)
        missing = self.project([{"start_photo": 1, "end_photo": 2, "audio": "無い曲.mp3"}], name="無い曲.photomovie.json")
        with self.assertRaises(core.CoreError) as ctx:
            core.render_project(missing, self.photos, self.out / "x.mp4", music_folder=self.music)
        self.assertEqual(ctx.exception.code, "missing_audio")
        self.assertEqual(sorted(p.name for p in self.out.iterdir()),
                         ["無い曲.photomovie.json", "計画 (BGM).photomovie.json"])

    def test_cli_render_with_music_folder(self):
        plan = self.project(self.TWO)
        proc = subprocess.run([sys.executable, "-B", str(ROOT / "pmm_cli.py"), "render", "--project=" + str(plan),
                               "--folder=" + str(self.photos), "--output=" + str(self.out / "作品 (CLI).mp4"),
                               "--music-folder=" + str(self.music)],
                              capture_output=True, env=dict(os.environ, PYTHONUTF8="1"), timeout=600)
        reply = json.loads(proc.stdout.decode("utf-8").strip().splitlines()[-1])
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["result"]["bgm_tracks"], [TRACK_A, TRACK_B])


class MuxCancelTest(BgmRenderBase):
    def test_cancel_during_the_bgm_mux_leaves_nothing(self):
        """曲の合成（mux_bgm）の最中でも止まり、FFmpeg と作りかけのファイルが残らない。"""
        work = Path(self._tmp.name) / "作業"
        work.mkdir()
        silent = work / "video_only.mp4"
        ffmpeg("-f", "lavfi", "-i", "color=c=gray:s=32x32:r=1:d=3600", "-c:v", "libx264", "-preset", "ultrafast",
               str(silent))
        target = work / "作品.mp4"
        stop = threading.Event()
        renderer = app.VideoRenderer(
            image_paths=[self.photos / "写真 1.jpg"] * 360, output_path=target, width=32, height=32, fps=1,
            interval_seconds=10.0, transition_seconds=0.0, zoom_percent=0.0, blur_background=False,
            bgm_segments=[app.BGMSegment(0, 359, self.music / TRACK_A)], encoder_pref="cpu", q=Queue(),
            stop_event=stop, title=None)
        before = ffmpeg_pids()
        threading.Timer(0.5, stop.set).start()
        started = time.monotonic()
        with self.assertRaises(InterruptedError):
            renderer.mux_bgm(silent)
        self.assertLess(time.monotonic() - started, 10)
        self.assertFalse(target.exists())
        time.sleep(0.3)
        self.assertEqual(ffmpeg_pids() - before, set())


if __name__ == "__main__":
    unittest.main(verbosity=2)
