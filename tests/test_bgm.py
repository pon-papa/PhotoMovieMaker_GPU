# -*- coding: utf-8 -*-
"""BGM 付きの Project JSON: 区間の形・曲のフォルダーの境界・形式（SI Director v1）。

曲はその場で FFmpeg の正弦波から作る試験音だけ。品質を見るための曲ではない。

    py -3 -m unittest discover -s tests
"""

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

TRACK_A = "試験音 A — 440 & (#1).wav"
TRACK_B = "試験音 B — 660 (#2).mp3"


def make_tone(target: Path, frequency: int, seconds: float) -> None:
    args = ["-c:a", "libmp3lame", "-b:a", "96k"] if target.suffix == ".mp3" else []
    subprocess.run([app.find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"sine=frequency={frequency}:duration={seconds}", *args, str(target)],
                   check=True, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)


class BgmBase(unittest.TestCase):
    photos_count = 6

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="pmm bgm 試験 — ")
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.photos = base / "写真 & 素材 (元) #1"
        self.music = base / "曲 — 候補 & (試験) #2"
        self.out = base / "出力 (試験) #3"
        for folder in (self.photos, self.music, self.out):
            folder.mkdir()
        for k in range(self.photos_count):
            Image.new("RGB", (320, 200), (40 + 30 * k, 120, 180)).save(self.photos / f"写真 {k + 1}.jpg")
        make_tone(self.music / TRACK_A, 440, 4)
        make_tone(self.music / TRACK_B, 660, 2)

    def project(self, segments, **info):
        project = core.create_project_plan(self.photos, title="BGM 試験")
        project["project"]["music_folder"] = str(self.music)
        project["project"].update(info)
        project["bgm_segments"] = segments
        return project


class SegmentValidationTest(BgmBase):
    def test_valid_single_and_multi_segment_projects(self):
        one = self.project([{"start_photo": 1, "end_photo": 6, "audio": TRACK_A}])
        self.assertEqual(core.validate_project(one), [])
        two = self.project([{"start_photo": 4, "end_photo": 6, "audio": TRACK_B, "start_file": "写真 4.jpg"},
                            {"start_photo": 1, "end_photo": 3, "audio": TRACK_A, "end_file": "写真 3.jpg"}])
        self.assertEqual(core.validate_project(two), [])
        gap = self.project([{"start_photo": 1, "end_photo": 2, "audio": TRACK_A},
                            {"start_photo": 5, "end_photo": 6, "audio": TRACK_A}])
        self.assertEqual(core.validate_project(gap), [])

    def test_segments_count_only_the_photos_that_are_used(self):
        project = self.project([{"start_photo": 1, "end_photo": 5, "audio": TRACK_A, "end_file": "写真 6.jpg"}])
        project["media"][2]["enabled"] = False                 # 使う写真は 5 枚、5 枚目は「写真 6」
        self.assertEqual(core.validate_project(project), [])
        project["bgm_segments"][0]["end_photo"] = 6
        self.assertTrue(any("5 枚" in p for p in core.validate_project(project)))

    def test_broken_segments_are_refused(self):
        cases = {
            "範囲の外": [{"start_photo": 1, "end_photo": 7, "audio": TRACK_A}],
            "逆順": [{"start_photo": 4, "end_photo": 2, "audio": TRACK_A}],
            "0枚目": [{"start_photo": 0, "end_photo": 2, "audio": TRACK_A}],
            "真偽値": [{"start_photo": True, "end_photo": 2, "audio": TRACK_A}],
            "重なり": [{"start_photo": 1, "end_photo": 3, "audio": TRACK_A},
                    {"start_photo": 3, "end_photo": 5, "audio": TRACK_B}],
            "知らない項目": [{"start_photo": 1, "end_photo": 2, "audio": TRACK_A, "volume": 0.5}],
            "写真名の食い違い": [{"start_photo": 1, "end_photo": 2, "audio": TRACK_A, "start_file": "写真 2.jpg"}],
            "パス": [{"start_photo": 1, "end_photo": 2, "audio": "..\\外.mp3"}],
            "絶対パス": [{"start_photo": 1, "end_photo": 2, "audio": "C:\\Users\\外.mp3"}],
            "上の階層": [{"start_photo": 1, "end_photo": 2, "audio": ".."}],
            "形式": [{"start_photo": 1, "end_photo": 2, "audio": "曲.exe"}],
            "MIDI": [{"start_photo": 1, "end_photo": 2, "audio": "曲.mid"}],
            "区間が配列でない": {"start_photo": 1},
        }
        for label, segments in cases.items():
            with self.subTest(label):
                self.assertTrue(core.validate_project(self.project(segments)))
        no_folder = self.project([{"start_photo": 1, "end_photo": 2, "audio": TRACK_A}], music_folder=None)
        self.assertTrue(any("music_folder" in p for p in core.validate_project(no_folder)))
        relative = self.project([], music_folder="曲")
        self.assertTrue(any("music_folder" in p for p in core.validate_project(relative)))

    def test_supported_formats_are_those_of_the_gui_picker(self):
        self.assertEqual(core.AUDIO_SUFFIXES, {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"})


class MusicFolderTest(BgmBase):
    def test_segments_become_renderer_segments_inside_the_music_folder(self):
        project = self.project([{"start_photo": 4, "end_photo": 6, "audio": TRACK_B},
                                {"start_photo": 1, "end_photo": 3, "audio": TRACK_A}])
        root, segments = core.resolve_bgm(project, self.music)
        self.assertEqual(root, self.music)
        self.assertEqual([(s.start_index, s.end_index, s.audio_path.name) for s in segments],
                         [(0, 2, TRACK_A), (3, 5, TRACK_B)])
        self.assertTrue(all(isinstance(s, app.BGMSegment) for s in segments))
        self.assertEqual(core.resolve_bgm(self.project([]), None), (None, []))

    def test_the_music_folder_is_explicit_and_must_match(self):
        project = self.project([{"start_photo": 1, "end_photo": 2, "audio": TRACK_A}])
        other = Path(self._tmp.name) / "別の曲"
        other.mkdir()
        (other / TRACK_A).write_bytes((self.music / TRACK_A).read_bytes())
        for folder, code in ((None, "music_folder_required"), ("", "music_folder_required"),
                             (other, "folder_mismatch"), (self.music / "無い", "invalid_folder"),
                             ("\\\\server\\share", "invalid_folder")):
            with self.subTest(folder=folder):
                with self.assertRaises(core.CoreError) as ctx:
                    core.resolve_bgm(project, folder)
                self.assertEqual(ctx.exception.code, code)

    def test_missing_track_and_link_out_of_the_folder(self):
        missing = self.project([{"start_photo": 1, "end_photo": 2, "audio": "無い曲.mp3"}])
        with self.assertRaises(core.CoreError) as ctx:
            core.resolve_bgm(missing, self.music)
        self.assertEqual(ctx.exception.code, "missing_audio")
        outside = Path(self._tmp.name) / "外の曲.wav"
        make_tone(outside, 330, 1)
        try:
            os.symlink(outside, self.music / "リンク.wav")
        except OSError:
            self.skipTest("この環境ではシンボリックリンクを作れない")
        linked = self.project([{"start_photo": 1, "end_photo": 2, "audio": "リンク.wav"}])
        with self.assertRaises(core.CoreError) as ctx:
            core.resolve_bgm(linked, self.music)
        self.assertEqual(ctx.exception.code, "invalid_audio")


if __name__ == "__main__":
    unittest.main(verbosity=2)
