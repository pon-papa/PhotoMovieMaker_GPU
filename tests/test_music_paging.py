# -*- coding: utf-8 -*-
"""曲の分析のページ送り（SI Director v2）: 30 曲を超えるフォルダーを、公開 API だけで 30 曲ずつ測れる。

曲は標準ライブラリの wave で作る短い試験音（0.6 秒）だけ。曲は読むだけで、変わらないことも確かめる。

    py -3 -m unittest discover -s tests
"""

import hashlib
import json
import math
import os
import struct
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pmm_core as core  # noqa: E402


def make_wav(path: Path, freq: float = 440.0, seconds: float = 0.6, rate: int = 8000) -> None:
    frames = b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * freq * i / rate)))
                      for i in range(int(rate * seconds)))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)


def fingerprint(folder: Path) -> dict:
    return {p.name: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
            for p in sorted(folder.iterdir()) if p.is_file()}


class PagingBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="pmm paging 試験 — ")
        self.addCleanup(self._tmp.cleanup)
        self.music = Path(self._tmp.name) / "曲 & 候補 #1"
        self.music.mkdir()

    def tracks(self, count: int, name="曲 {k}.wav"):
        for k in range(1, count + 1):
            make_wav(self.music / name.format(k=k), freq=200 + 3 * k)


class SmallFoldersTest(PagingBase):
    def test_one_and_thirty_tracks_work_without_paging(self):
        for count in (1, 30):
            with self.subTest(count=count):
                for p in self.music.iterdir():
                    p.unlink()
                self.tracks(count)
                result = core.analyze_music(self.music)
                self.assertEqual((result["count"], result["total"], result["next_offset"]), (count, count, None))

    def test_thirty_one_tracks_need_paging(self):
        self.tracks(31)
        with self.assertRaises(core.CoreError) as ctx:
            core.analyze_music(self.music)
        self.assertEqual(ctx.exception.code, "too_many_tracks")
        self.assertIn("offset", ctx.exception.message)
        first = core.analyze_music(self.music, offset=0, limit=30)
        rest = core.analyze_music(self.music, offset=first["next_offset"], limit=30)
        self.assertEqual((first["count"], rest["count"], rest["next_offset"]), (30, 1, None))
        self.assertEqual(rest["tracks"][0]["file"], "曲 31.wav")           # 自然順（曲 2 < 曲 10 < 曲 31）

    def test_invalid_pages(self):
        self.tracks(3)
        for offset, limit in ((-1, 10), (4, 10), (0, 0), (0, 31), ("0", 10), (0, 2.5), (True, 3)):
            with self.subTest(offset=offset, limit=limit), self.assertRaises(core.CoreError) as ctx:
                core.analyze_music(self.music, offset=offset, limit=limit)
            self.assertEqual(ctx.exception.code, "invalid_page")
        self.assertEqual(core.analyze_music(self.music, offset=3, limit=10)["count"], 0)   # 最後の次は空


class LargeFolderTest(PagingBase):
    def test_124_tracks_in_pages_with_stable_order(self):
        self.tracks(124)
        before = fingerprint(self.music)
        scan = core.scan_music(self.music)
        self.assertEqual(scan["count"], 124)
        names, offsets, offset = [], [], 0
        while offset is not None:
            page = core.analyze_music(self.music, offset=offset, limit=30,
                                      expect_fingerprint=scan["listing_fingerprint"])
            self.assertEqual(page["failures"], [])
            self.assertEqual(page["listing_fingerprint"], scan["listing_fingerprint"])
            names += [t["file"] for t in page["tracks"]]
            offsets.append(page["next_offset"])
            offset = page["next_offset"]
        self.assertEqual(offsets, [30, 60, 90, 120, None])
        self.assertEqual(names, [t["file"] for t in scan["tracks"]])          # 抜け・重複・順番の入れ替わりなし
        self.assertEqual(len(set(names)), 124)
        self.assertEqual(fingerprint(self.music), before)                    # 曲は読むだけ


class EdgeCasesTest(PagingBase):
    def test_folder_changed_between_pages(self):
        self.tracks(5)
        scan = core.scan_music(self.music)
        (self.music / "曲 2.wav").unlink()                                    # 途中で 1 曲なくなった
        with self.assertRaises(core.CoreError) as ctx:
            core.analyze_music(self.music, offset=0, limit=2, expect_fingerprint=scan["listing_fingerprint"])
        self.assertEqual(ctx.exception.code, "folder_changed")
        page = core.analyze_music(self.music, offset=0, limit=10)             # 指紋なしなら今の一覧で測る
        self.assertEqual([t["file"] for t in page["tracks"]], ["曲 1.wav", "曲 3.wav", "曲 4.wav", "曲 5.wav"])

    def test_unsupported_files_and_japanese_names(self):
        make_wav(self.music / "春 — ワルツ & (#1) ♪.wav")
        make_wav(self.music / "夏の曲 2.wav")
        (self.music / "メモ.txt").write_text("曲ではない", encoding="utf-8")
        (self.music / "譜面.mid").write_bytes(b"MThd")
        scan = core.scan_music(self.music)
        self.assertEqual([t["file"] for t in scan["tracks"]], ["夏の曲 2.wav", "春 — ワルツ & (#1) ♪.wav"])
        self.assertEqual(scan["skipped_files"], 2)
        page = core.analyze_music(self.music, offset=0, limit=30)
        self.assertEqual(page["count"], 2)

    def test_broken_track_is_reported_and_the_page_continues(self):
        self.tracks(3)
        (self.music / "曲 2.wav").write_bytes(b"RIFF\x00\x00\x00\x00broken")
        page = core.analyze_music(self.music, offset=0, limit=30)
        self.assertEqual([t["file"] for t in page["tracks"]], ["曲 1.wav", "曲 3.wav"])
        self.assertEqual([f["file"] for f in page["failures"]], ["曲 2.wav"])

    def test_cli_pages(self):
        self.tracks(4)
        env = dict(os.environ, PYTHONUTF8="1")
        proc = subprocess.run([sys.executable, "-B", str(ROOT / "pmm_cli.py"), "music-analyze",
                               "--folder=" + str(self.music), "--offset", "2", "--limit", "30"],
                              capture_output=True, env=env, timeout=300)
        reply = json.loads(proc.stdout.decode("utf-8").strip().splitlines()[-1])
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(([t["file"] for t in reply["result"]["tracks"]], reply["result"]["next_offset"]),
                         (["曲 3.wav", "曲 4.wav"], None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
