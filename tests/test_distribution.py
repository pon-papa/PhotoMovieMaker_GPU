# -*- coding: utf-8 -*-
"""第三者へ配布するための土台（setup.bat / run.bat・NVENC の判定）。

.venv を実際に作ってパッケージを入れる確認は時間がかかり通信もするので、ここではしない
（配布 ZIP を展開して行う確認は tools/verify_release.py）。

    py -3 -m unittest discover -s tests
"""

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from queue import Queue
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import PhotoMovieMaker_GPU as app  # noqa: E402

BATS = ("setup.bat", "run.bat", "diagnose.bat")
# 配布物がしてはいけないこと（レジストリ・タスク・サービス・実行ポリシー・Defender・SmartScreen）
FORBIDDEN = ("reg add", "reg delete", "schtasks", "sc create", "sc config", "set-executionpolicy",
             "add-mppreference", "smartscreen", "powershell", "netsh", "bcdedit", "runas")
# 日本語・空白・括弧・#・& を含むフォルダー名（アプリを置いたフォルダーそのもの）
AWKWARD = "動画 テスト (1)#PMM & 試験"


def run_bat(path: Path, cwd: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, PMM_NO_PAUSE="1")
    return subprocess.run(f'cmd /d /s /c ""{path}""', cwd=cwd, env=env, capture_output=True, timeout=120)


@unittest.skipUnless(os.name == "nt", "Windows の bat の確認")
class BatchFileTest(unittest.TestCase):
    def test_text_form(self):
        for name in BATS:
            with self.subTest(name=name):
                raw = (ROOT / name).read_bytes()
                text = raw.decode("utf-8")                       # UTF-8（BOM なし）
                self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
                self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))    # 改行は CRLF だけ
                self.assertIn("chcp 65001", text)                # 日本語を出す前にコードページを決める
                self.assertIn('set "APP_DIR=%~dp0"', text)       # 自分の置き場所を基準にする
                self.assertIn("DisableDelayedExpansion", text)
                lowered = text.lower()
                for word in FORBIDDEN:
                    self.assertNotIn(word, lowered)

    def test_packages_go_only_into_the_local_venv(self):
        setup = (ROOT / "setup.bat").read_text(encoding="utf-8")
        self.assertIn('-m venv "%APP_DIR%.venv"', setup)
        for line in setup.splitlines():
            if "pip install" in line and not line.lstrip().lower().startswith(("rem", "echo")):
                self.assertTrue(line.startswith('"%VENV_PY%"'), line)      # グローバルの Python へは入れない
        run = (ROOT / "run.bat").read_text(encoding="utf-8")
        self.assertIn('"%VENV_PY%" "%APP_DIR%PhotoMovieMaker_GPU.py"', run)
        self.assertNotIn("\npy ", run.replace("\r", ""))

    def test_run_without_setup_explains_what_to_do(self):
        with tempfile.TemporaryDirectory(prefix="pmm bat 試験 — ") as tmp:
            folder = Path(tmp) / AWKWARD
            folder.mkdir()
            shutil.copy(ROOT / "run.bat", folder / "run.bat")
            (folder / "PhotoMovieMaker_GPU.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
            proc = run_bat(folder / "run.bat", cwd=Path(tmp))        # 別のフォルダーから起動する
            out = proc.stdout.decode("utf-8", "replace")
            self.assertEqual(proc.returncode, 1, out)
            self.assertIn("まず setup.bat を実行してください", out)
            self.assertEqual(sorted(p.name for p in folder.iterdir()), ["PhotoMovieMaker_GPU.py", "run.bat"])

    def test_setup_in_an_incomplete_folder_stops_with_a_reason(self):
        with tempfile.TemporaryDirectory(prefix="pmm bat 試験 — ") as tmp:
            folder = Path(tmp) / AWKWARD
            folder.mkdir()
            shutil.copy(ROOT / "setup.bat", folder / "setup.bat")
            proc = run_bat(folder / "setup.bat", cwd=Path(tmp))
            out = proc.stdout.decode("utf-8", "replace")
            self.assertEqual(proc.returncode, 1, out)
            self.assertIn("アプリのファイルが足りません", out)
            self.assertEqual([p.name for p in folder.iterdir()], ["setup.bat"])     # .venv も作らない

    def test_run_uses_the_venv_python_in_an_awkward_folder(self):
        """.venv の Python の代わりに今の Python を置いて、引用符の付け方を確かめる。"""
        with tempfile.TemporaryDirectory(prefix="pmm bat 試験 — ") as tmp:
            folder = Path(tmp) / AWKWARD
            scripts = folder / ".venv" / "Scripts"
            scripts.mkdir(parents=True)
            (scripts / "python.exe").write_bytes(b"")                 # あることだけ見せる
            shutil.copy(ROOT / "run.bat", folder / "run.bat")
            (folder / "PhotoMovieMaker_GPU.py").write_text("x", encoding="utf-8")
            text = (folder / "run.bat").read_text(encoding="utf-8")
            launcher = f'"{sys.executable}" -c "import sys, pathlib; ' \
                       f"pathlib.Path(sys.argv[1]).with_name('started.txt').write_text(sys.argv[1], encoding='utf-8')\""
            text = text.replace('"%VENV_PY%" "%APP_DIR%PhotoMovieMaker_GPU.py"',
                                launcher + ' "%APP_DIR%PhotoMovieMaker_GPU.py"')
            (folder / "run.bat").write_bytes(text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8"))
            proc = run_bat(folder / "run.bat", cwd=Path(tmp))
            self.assertEqual(proc.returncode, 0, proc.stdout.decode("utf-8", "replace"))
            started = (folder / "started.txt").read_text(encoding="utf-8")
            self.assertEqual(Path(started), folder / "PhotoMovieMaker_GPU.py")     # パスが壊れずに渡る


class NvencDetectionTest(unittest.TestCase):
    """エンコーダーの一覧に h264_nvenc があっても、実際に使えなければ CPU（libx264）へ。"""

    def fake_run(self, probe_returncode):
        calls = []

        def run(cmd, **kwargs):
            calls.append(cmd)
            if "-encoders" in cmd:
                return SimpleNamespace(returncode=0, stdout=" V....D h264_nvenc  NVIDIA NVENC\n V....D libx264\n", stderr="")
            return SimpleNamespace(returncode=probe_returncode, stdout="", stderr="Cannot load nvcuda.dll")
        return run, calls

    def test_listed_but_unusable_means_cpu(self):
        run, calls = self.fake_run(probe_returncode=1)
        with mock.patch.object(app.subprocess, "run", run), mock.patch.dict(app._NVENC_USABLE, {}, clear=True):
            self.assertFalse(app.ffmpeg_has_nvenc("ffmpeg (NVIDIA なし)"))
            self.assertFalse(app.ffmpeg_has_nvenc("ffmpeg (NVIDIA なし)"))      # 2 回目は覚えている
            self.assertEqual(len(calls), 2)                                     # 一覧 + 試し書き出し 1 回だけ
            self.assertIn("h264_nvenc", calls[1])

    def test_listed_and_usable(self):
        run, _calls = self.fake_run(probe_returncode=0)
        with mock.patch.object(app.subprocess, "run", run), mock.patch.dict(app._NVENC_USABLE, {}, clear=True):
            self.assertTrue(app.ffmpeg_has_nvenc("ffmpeg (NVIDIA あり)"))

    def test_auto_falls_back_to_x264(self):
        with tempfile.TemporaryDirectory(prefix="pmm nvenc 試験 — ") as tmp:
            with mock.patch.object(app, "ffmpeg_has_nvenc", lambda _f: False):
                r = app.VideoRenderer(
                    image_paths=[Path(tmp) / "a.jpg"], output_path=Path(tmp) / "x.mp4", width=320, height=180, fps=10,
                    interval_seconds=1.0, transition_seconds=0.3, zoom_percent=0.0, blur_background=False,
                    bgm_segments=[], encoder_pref="auto", q=Queue(), stop_event=threading.Event())
            name, args = r.choose_video_encoder_args()
            self.assertEqual(name, "CPU x264")
            self.assertIn("libx264", args)
            r.encoder_pref = "nvenc"
            with self.assertRaises(RuntimeError):
                r.choose_video_encoder_args()


if __name__ == "__main__":
    unittest.main(verbosity=2)
