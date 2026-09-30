# -*- coding: utf-8 -*-
"""ToolDock・MCP が無くても、PhotoMovieMaker が単体で動くことの試験。

AI・自動化からの利用（tooldock.tool.json / pmm_cli）は任意の追加機能で、
通常の画面・動画生成はそれに一切頼らない。ここでは別プロセスで、
  - tooldock / tooldock_mcp / mcp の import を禁止し
  - tooldock.tool.json を開こうとしたら失敗させ
  - ネットワークのソケットを作ろうとしたら失敗させ
た状態で、画面の起動・Core・CLI・短い動画の書き出しを行う。

    py -3 -m unittest discover -s tests
"""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 別プロセスの先頭で実行する「番人」。禁止したことが起きたら例外で止まる。
GUARD = textwrap.dedent(r'''
    import builtins, importlib.abc, io, socket, sys
    sys.path.insert(0, ROOT)

    class _Block(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname.split(".")[0] in ("tooldock", "tooldock_mcp", "mcp"):
                raise ModuleNotFoundError(f"blocked for the standalone test: {fullname}")
            return None
    sys.meta_path.insert(0, _Block())

    _open = builtins.open
    def _guarded_open(file, *a, **k):
        if "tooldock.tool.json" in str(file):
            raise AssertionError("the app opened tooldock.tool.json")
        return _open(file, *a, **k)
    builtins.open = io.open = _guarded_open

    class _NoSocket(socket.socket):
        def __init__(self, *a, **k):
            raise AssertionError("the app tried to open a network socket")
    socket.socket = _NoSocket
''')


def run_isolated(body: str, *argv: str) -> subprocess.CompletedProcess:
    code = f"ROOT = {str(ROOT)!r}\n" + GUARD + textwrap.dedent(body)
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH",)}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run([sys.executable, "-B", "-c", code, *argv], capture_output=True,
                          cwd=str(ROOT), env=env, timeout=600)


def make_images(folder: Path, count: int = 3) -> None:
    from PIL import Image, ImageDraw
    for k in range(count):
        im = Image.new("RGB", (480, 320), (40 + 60 * k, 110, 190 - 50 * k))
        ImageDraw.Draw(im).rectangle([60 + 30 * k, 60, 260 + 30 * k, 260], fill=(235, 225, 200))
        im.save(folder / f"写真 {k + 1}.jpg")


class StandaloneTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="pmm 単体 — ")
        self.tmp = Path(self._td.name)
        self.photos = self.tmp / "写真 & 素材 (単体)"
        self.photos.mkdir()
        make_images(self.photos)

    def tearDown(self):
        self._td.cleanup()

    def check(self, proc):
        self.assertEqual(proc.returncode, 0,
                         proc.stdout.decode("utf-8", "replace")[-1500:]
                         + proc.stderr.decode("utf-8", "replace")[-3000:])
        return proc.stdout.decode("utf-8", "replace")

    def test_a_gui_starts_without_tooldock_mcp_or_manifest(self):
        out = self.check(run_isolated('''
            import PhotoMovieMaker_GPU as app
            window = app.App()
            window.withdraw()
            window.update()
            window.destroy()
            loaded = sorted(m for m in sys.modules if m.split(".")[0] in ("tooldock", "tooldock_mcp", "mcp", "pmm_cli", "pmm_core"))
            print("LOADED", loaded)
        '''))
        # 画面は外部操作の入口（pmm_core / pmm_cli）も読み込まない
        self.assertIn("LOADED []", out)

    def test_b_core_and_cli_work_without_tooldock_or_mcp(self):
        out = self.check(run_isolated('''
            import json, runpy
            import pmm_core as core
            print("CORE", core.scan_media(sys.argv[1])["count"])
            sys.argv = ["pmm_cli.py", "scan", "--folder=" + sys.argv[1]]
            try:
                runpy.run_path(ROOT + "/pmm_cli.py", run_name="__main__")
            except SystemExit as e:
                assert e.code == 0, e.code
        ''', str(self.photos)))
        self.assertIn("CORE 3", out)
        reply = json.loads(out.strip().splitlines()[-1])
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["result"]["count"], 3)

    def test_c_no_app_source_refers_to_tooldock_or_mcp(self):
        for path in ROOT.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            for word in ("import tooldock", "from tooldock", "import mcp", "from mcp", "tooldock.tool.json"):
                self.assertNotIn(word, text, f"{path.name}: {word}")
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
        self.assertNotIn("mcp", requirements)
        self.assertNotIn("tooldock", requirements)

    def test_d_video_render_still_works_standalone(self):
        """GUI と同じ VideoRenderer で短い動画を書き出す（ToolDock / MCP なし）。"""
        out_mp4 = self.tmp / "出力 — 単体 (試験).mp4"
        out = self.check(run_isolated('''
            import threading
            from pathlib import Path
            from queue import Queue
            import imageio_ffmpeg
            import PhotoMovieMaker_GPU as app
            photos = sorted(Path(sys.argv[1]).glob("*.jpg"), key=app.natural_key)
            r = app.VideoRenderer(photos, Path(sys.argv[2]), 320, 180, 10, 1.0, 0.3, 4.0, True, [],
                                  "cpu", Queue(), threading.Event())
            r.run()
            frames, secs = imageio_ffmpeg.count_frames_and_secs(sys.argv[2])
            print("RENDER", r.total_frames, frames, round(secs, 2), r.encoder_used)
        ''', str(self.photos), str(out_mp4)))
        line = [l for l in out.splitlines() if l.startswith("RENDER")][0].split()
        self.assertEqual(int(line[1]), 30)            # 3枚 × 1秒 × 10fps
        self.assertEqual(int(line[2]), 30)
        self.assertTrue(out_mp4.is_file() and out_mp4.stat().st_size > 1000)

    def test_e_core_uses_the_same_code_as_the_gui(self):
        import pmm_core
        import PhotoMovieMaker_GPU as app
        import photo_recommendation as pr
        self.assertIs(pmm_core.app, app)
        self.assertIs(pmm_core.pr, pr)
        self.assertIs(pmm_core.app.natural_key, app.natural_key)

    def test_f_the_manifest_is_optional(self):
        """manifest は静的な自己紹介。アプリはこれを読まない（A で開くと失敗する番人を置いて確認済み）。"""
        spec = json.loads((ROOT / "tooldock.tool.json").read_text(encoding="utf-8"))
        self.assertEqual(spec["interface"]["entry"], "pmm_cli.py")
        self.assertNotIn("tooldock", (ROOT / "run.bat").read_text(encoding="utf-8").lower())
        self.assertNotIn("tooldock", (ROOT / "setup.bat").read_text(encoding="utf-8").lower())


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    unittest.main(verbosity=2)
