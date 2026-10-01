# -*- coding: utf-8 -*-
"""曲のタグの文字化けの直し（Shift_JIS を Latin-1 として読んだもの）。直せないものは触らない。

試験の曲は FFmpeg で作り、ID3v2.3 のタグ（文字コード欄は Latin-1、中身は Shift_JIS）を手で付ける。

    py -3 -m unittest discover -s tests
"""

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


def id3v23(frames: dict[str, bytes]) -> bytes:
    body = b""
    for fid, text in frames.items():
        data = b"\x00" + text                      # 文字コード欄 0 = ISO-8859-1（Latin-1）
        body += fid.encode("ascii") + len(data).to_bytes(4, "big") + b"\x00\x00" + data
    size = len(body)
    syncsafe = bytes([(size >> 21) & 0x7F, (size >> 14) & 0x7F, (size >> 7) & 0x7F, size & 0x7F])
    return b"ID3\x03\x00\x00" + syncsafe + body


class TagRepairTest(unittest.TestCase):
    def test_pure_function(self):
        broken = "しゃろう".encode("cp932").decode("latin-1")
        self.assertEqual(core.repair_tag_text(broken), "しゃろう")
        for keep in ("Beyoncé", "Café Müller", "Mötley Crüe", "Dancer", "夏の曲", "", "ÀÁÂ"):
            with self.subTest(keep=keep):
                self.assertIsNone(core.repair_tag_text(keep))

    def test_probe_repairs_misread_tags(self):
        with tempfile.TemporaryDirectory(prefix="pmm tags 試験 — ") as tmp:
            music = Path(tmp) / "曲"
            music.mkdir()
            raw = Path(tmp) / "raw.mp3"
            subprocess.run([app.find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                            "sine=frequency=440:duration=2", "-c:a", "libmp3lame", "-write_id3v2", "0", str(raw)],
                           check=True, creationflags=FLAGS)
            track = music / "夏 #1.mp3"
            track.write_bytes(id3v23({"TPE1": "しゃろう".encode("cp932"), "TIT2": "Summer".encode("ascii")})
                              + raw.read_bytes())
            entry = core.analyze_music(music)["tracks"][0]
            self.assertEqual(entry["tags"], {"artist": "しゃろう", "title": "Summer"})
            self.assertEqual(entry["tags_repaired"], ["artist"])
            self.assertEqual(entry["tags_raw"]["artist"], "しゃろう".encode("cp932").decode("latin-1"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
