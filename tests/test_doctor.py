# -*- coding: utf-8 -*-
"""診断（app_doctor.py / diagnose.bat）。何も変更せず、個人の情報を出さない。

    py -3 -m unittest discover -s tests
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app_doctor as doctor  # noqa: E402
import pmm_version  # noqa: E402


def listing(folder: Path) -> list:
    return sorted((p.name, p.stat().st_size) for p in folder.iterdir() if p.is_file())


class DoctorTest(unittest.TestCase):
    def test_report_shape_and_version(self):
        report = doctor.diagnose()
        self.assertEqual(report["version"], pmm_version.__version__)
        names = [c["name"] for c in report["checks"]]
        for expected in ("Python", "仮想環境 (.venv)", "Pillow", "NumPy", "OpenCV", "FFmpeg",
                         "H.264（CPU x264）と AAC 音声", "NVENC（NVIDIA の GPU）", "書き込み（一時フォルダー）"):
            self.assertIn(expected, names)
        self.assertTrue(report["ok"], doctor.render(report))            # 開発環境では必須の項目はそろっている
        text = doctor.render(report)
        self.assertIn(f"PhotoMovieMaker GPU v{pmm_version.__version__} 診断", text)

    def test_output_has_no_personal_information(self):
        text = doctor.render(doctor.diagnose()) + json.dumps(doctor.diagnose(), ensure_ascii=False)
        home = Path.home()
        for secret in {str(home), home.name, os.environ.get("USERNAME", "") or home.name, str(ROOT), str(ROOT.parent),
                       os.environ.get("COMPUTERNAME", "") or "\0"}:
            if len(secret) >= 3:
                self.assertNotIn(secret, text)
        self.assertNotIn(":\\", text)                       # ドライブ名から始まる場所を出さない
        self.assertNotIn("Users", text)

    def test_nothing_is_changed(self):
        before = listing(ROOT), listing(ROOT / "models")
        temp_before = {p.name for p in Path(tempfile.gettempdir()).glob("pmm_doctor_*")}
        doctor.diagnose()
        self.assertEqual((listing(ROOT), listing(ROOT / "models")), before)
        self.assertEqual({p.name for p in Path(tempfile.gettempdir()).glob("pmm_doctor_*")}, temp_before)

    def test_unsupported_python_is_reported_with_advice(self):
        with mock.patch.object(doctor, "SUPPORTED_PYTHON", ((3, 99), (3, 99))):
            first = doctor.check_python()[0]
        self.assertEqual(first["status"], doctor.NG)
        self.assertIn("setup.bat", first["advice"])

    def test_missing_and_wrong_models(self):
        with tempfile.TemporaryDirectory(prefix="pmm doctor 試験 — ") as tmp:
            (Path(tmp) / "models").mkdir()
            (Path(tmp) / "models" / doctor.EXPECTED_MODELS[0]["file"]).write_bytes(b"not a model")
            with mock.patch.object(doctor, "APP_DIR", Path(tmp)):
                checks = {c["name"]: c for c in doctor.check_models()}
        wrong = checks[f"モデル {doctor.EXPECTED_MODELS[0]['label']}"]
        missing = checks[f"モデル {doctor.EXPECTED_MODELS[1]['label']}"]
        self.assertEqual(wrong["status"], doctor.NG)                    # 壊れている・別のファイル
        self.assertEqual(missing["status"], doctor.WARN)                # 無い（ほかの機能は動く）
        for c in (wrong, missing):
            self.assertIn("Release ZIP", c["advice"])

    def test_models_match_the_documented_hashes(self):
        table = (ROOT / "models" / "README.md").read_text(encoding="utf-8")
        for m in doctor.EXPECTED_MODELS:
            self.assertIn(m["file"], table)
            self.assertIn(m["sha256"], table)

    def test_cli_json_and_exit_code(self):
        proc = subprocess.run([sys.executable, "-B", str(ROOT / "app_doctor.py"), "--json"], capture_output=True,
                              env=dict(os.environ, PYTHONUTF8="1"), timeout=300)
        report = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(proc.returncode, 0 if report["ok"] else 1)
        self.assertEqual(report["app"], "PhotoMovieMaker GPU")

    def test_doctor_needs_only_the_standard_library_to_start(self):
        """パッケージが入っていない Python でも、診断そのものは動く（-S で site-packages を見せない）。"""
        env = {k: v for k, v in os.environ.items() if k.upper() != "PYTHONPATH"}
        proc = subprocess.run([sys.executable, "-S", "-B", str(ROOT / "app_doctor.py"), "--json"],
                              capture_output=True, env=dict(env, PYTHONUTF8="1"), timeout=300, cwd=str(ROOT))
        self.assertTrue(proc.stdout.strip(), proc.stderr.decode("utf-8", "replace")[-2000:])
        report = json.loads(proc.stdout.decode("utf-8"))
        by = {c["name"]: c for c in report["checks"]}
        self.assertEqual(by["Pillow"]["status"], doctor.NG)
        self.assertIn("setup.bat", by["Pillow"]["advice"])
        self.assertEqual(proc.returncode, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
