# -*- coding: utf-8 -*-
"""PhotoMovieMaker GPU の診断（diagnose.bat から呼ばれます）。

    diagnose.bat                 … 画面に結果を出す
    python app_doctor.py --json  … 機械で読める形

この PC でアプリが動く条件がそろっているかを確かめます。何も変更しません
（書き込みの確認のために一時フォルダーへ小さなファイルを作り、すぐ消すだけ）。通信もしません。

結果は不具合の報告にそのまま貼れるよう、ユーザー名・フォルダーの場所・写真や曲の名前は出しません。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from pmm_version import SUPPORTED_PYTHON, __version__

APP_DIR = Path(__file__).resolve().parent
# OpenCV の情報・警告の行を結果に混ぜない（診断の結果だけを出す）
os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")
OK, WARN, NG = "OK", "注意", "NG"

# 配布 ZIP に入っているはずのモデル。ZIP を作るときにも同じ表で確かめます。
EXPECTED_MODELS = [
    {"file": "face_detection_yunet_2023mar.onnx", "label": "YuNet（顔の位置）", "bytes": 232589,
     "sha256": "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4", "used_for": "被写体追従"},
    {"file": "object_detection_yolox_2022nov.onnx", "label": "YOLOX（犬・人物の位置）", "bytes": 35858002,
     "sha256": "c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063", "used_for": "被写体追従"},
    {"file": "dinov2_small_embedding.onnx", "label": "DINOv2（似た場面の判定）", "bytes": 88437819,
     "sha256": "a89a990a98e8022021fd294c94078c8395fc7ae3d59dbc30e80f214ff1b842c5", "used_for": "おすすめ解析"},
]
# (import 名, 配布名, 表示名, 必須か, 無いときの説明)
PACKAGES = [
    ("PIL", "pillow", "Pillow", True, ""),
    ("numpy", "numpy", "NumPy", True, ""),
    ("cv2", "opencv-python", "OpenCV", True, ""),
    ("imageio_ffmpeg", "imageio-ffmpeg", "imageio-ffmpeg", True, ""),
    ("onnxruntime", "onnxruntime", "onnxruntime", False, "無くても動きます（おすすめ解析が従来の指標だけになります）"),
]
FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
RUN_SETUP = "setup.bat を実行してください。"
REDOWNLOAD = "配布 ZIP が不完全です。公式の Release ZIP を取得し直して、すべて展開してください。"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def item(name: str, status: str, detail: str, advice: str = "") -> dict:
    return {"name": name, "status": status, "detail": detail, "advice": advice}


def check_python() -> list[dict]:
    lo, hi = SUPPORTED_PYTHON
    v = sys.version_info
    text = f"{v.major}.{v.minor}.{v.micro}（{platform.architecture()[0]}）"
    if lo <= (v.major, v.minor) <= hi:
        out = [item("Python", OK, text)]
    else:
        out = [item("Python", NG, text, f"Python {lo[0]}.{lo[1]}〜{hi[0]}.{hi[1]} をインストールし、.venv フォルダーを削除して"
                                        "から setup.bat を実行し直してください。")]
    in_venv = sys.prefix != sys.base_prefix
    here = in_venv and Path(sys.prefix).resolve() == (APP_DIR / ".venv").resolve()
    if here:
        out.append(item("仮想環境 (.venv)", OK, "このフォルダーの .venv で動いています"))
    elif (APP_DIR / ".venv" / "Scripts" / "python.exe").is_file() or (APP_DIR / ".venv" / "bin" / "python").is_file():
        out.append(item("仮想環境 (.venv)", WARN, ".venv はありますが、別の Python で診断しています",
                        "diagnose.bat から実行してください。"))
    else:
        out.append(item("仮想環境 (.venv)", WARN, "まだありません", RUN_SETUP))
    return out


def check_packages() -> list[dict]:
    out = []
    try:
        import tkinter
        out.append(item("Tkinter（画面）", OK, str(tkinter.TkVersion)))
    except Exception:
        out.append(item("Tkinter（画面）", NG, "読み込めません",
                        "Python を「tcl/tk and IDLE」を含めてインストールし直してください（python.org の標準の構成に含まれます）。"))
    for module, dist, label, required, note in PACKAGES:
        try:
            importlib.import_module(module)
            try:
                version = importlib.metadata.version(dist)
            except importlib.metadata.PackageNotFoundError:
                version = "（版は不明）"
            out.append(item(label, OK, version))
        except Exception as e:
            if required:
                out.append(item(label, NG, f"読み込めません（{type(e).__name__}）", RUN_SETUP))
            else:
                out.append(item(label, WARN, "入っていません", note))
    return out


def find_ffmpeg() -> tuple[str | None, str]:
    """アプリと同じ順（PATH → imageio-ffmpeg）。場所そのものは返さず、どちらのものかだけを返す。"""
    found = shutil.which("ffmpeg")
    if found:
        return found, "PATH 上のもの"
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe(), "imageio-ffmpeg に入っているもの"
    except Exception:
        return None, ""


def run_ffmpeg(ffmpeg: str, *args: str, timeout: float = 30) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run([ffmpeg, "-hide_banner", *args], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, creationflags=FLAGS)
    except Exception:
        return None


def check_ffmpeg() -> list[dict]:
    ffmpeg, source = find_ffmpeg()
    if not ffmpeg:
        return [item("FFmpeg", NG, "見つかりません", RUN_SETUP + "（imageio-ffmpeg に FFmpeg が入っています）")]
    version = run_ffmpeg(ffmpeg, "-version")
    first = (version.stdout.splitlines() or [""])[0] if version else ""
    number = first.split()[2] if first.startswith("ffmpeg version") and len(first.split()) > 2 else "（版は不明）"
    out = [item("FFmpeg", OK, f"{number}（{source}）")]
    color = ["-f", "lavfi", "-i", "color=c=black:s=256x256:r=30:d=0.2"]
    quiet = ["-loglevel", "error"]
    cpu = run_ffmpeg(ffmpeg, *quiet, *color, "-f", "lavfi", "-i", "sine=frequency=440:duration=0.2",
                     "-c:v", "libx264", "-c:a", "aac", "-shortest", "-f", "null", "-")
    if cpu is not None and cpu.returncode == 0:
        out.append(item("H.264（CPU x264）と AAC 音声", OK, "書き出せます"))
    else:
        out.append(item("H.264（CPU x264）と AAC 音声", NG, "書き出しの試しに失敗しました",
                        "PATH 上の FFmpeg が古い・機能が足りない可能性があります。新しい FFmpeg へ入れ替えてください。"))
    listed = run_ffmpeg(ffmpeg, "-encoders")
    nvenc = None
    if listed is not None and "h264_nvenc" in (listed.stdout or ""):
        nvenc = run_ffmpeg(ffmpeg, *quiet, *color, "-c:v", "h264_nvenc", "-f", "null", "-")
    if nvenc is not None and nvenc.returncode == 0:
        out.append(item("NVENC（NVIDIA の GPU）", OK, "使えます（「自動」は NVENC で書き出します）"))
    else:
        out.append(item("NVENC（NVIDIA の GPU）", "なし", "使えません（「自動」は CPU x264 で書き出します。問題ではありません）"))
    return out


def check_models() -> list[dict]:
    out = []
    folder = APP_DIR / "models"
    for m in EXPECTED_MODELS:
        path = folder / m["file"]
        name = f"モデル {m['label']}"
        if not path.is_file():
            out.append(item(name, WARN, f"ありません（{m['used_for']}が使えません。ほかの機能は動きます）", REDOWNLOAD))
        elif path.stat().st_size != m["bytes"] or sha256(path) != m["sha256"]:
            out.append(item(name, NG, "中身が配布物と違います（壊れているか、別のファイルです）", REDOWNLOAD))
        else:
            out.append(item(name, OK, f"{m['bytes']:,} バイト・SHA-256 一致"))
    try:
        import subject_detector
        detector = subject_detector.SubjectDetector()
        if detector.available:
            out.append(item("被写体検出の読み込み", OK, "YuNet と YOLOX を読み込めました"))
        else:
            out.append(item("被写体検出の読み込み", WARN, "読み込めません（被写体追従は選べません）", REDOWNLOAD))
    except Exception as e:
        out.append(item("被写体検出の読み込み", WARN, f"確かめられません（{type(e).__name__}）", RUN_SETUP))
    return out


def check_write() -> list[dict]:
    try:
        with tempfile.TemporaryDirectory(prefix="pmm_doctor_") as tmp:
            probe = Path(tmp) / "write_test.txt"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        return [item("書き込み（一時フォルダー）", OK, "書けます")]
    except Exception as e:
        return [item("書き込み（一時フォルダー）", NG, f"書けません（{type(e).__name__}）",
                     "一時フォルダー（TEMP）に空きと書き込みの権限があるか確かめてください。")]


def diagnose() -> dict:
    checks = check_python() + check_packages() + check_ffmpeg() + check_models() + check_write()
    problems = [c for c in checks if c["status"] == NG]
    warnings = [c for c in checks if c["status"] == WARN]
    return {"app": "PhotoMovieMaker GPU", "version": __version__,
            "os": f"{platform.system()} {platform.release()}（{platform.version()}）",
            "checks": checks, "problems": len(problems), "warnings": len(warnings),
            "ok": not problems}


def render(report: dict) -> str:
    lines = [f"PhotoMovieMaker GPU v{report['version']} 診断", f"OS: {report['os']}", ""]
    for c in report["checks"]:
        lines.append(f"[{c['status']}] {c['name']}: {c['detail']}")
        if c["advice"] and c["status"] in (NG, WARN):
            lines.append(f"      → {c['advice']}")
    lines.append("")
    if report["problems"]:
        lines.append(f"結果: 問題が {report['problems']} 件あります（[NG] の項目を見てください）。")
    elif report["warnings"]:
        lines.append(f"結果: 必須の項目に問題はありません。注意が {report['warnings']} 件あります（[注意] の項目を見てください）。")
    else:
        lines.append("結果: 問題は見つかりませんでした。")
    lines.append("この結果には、ユーザー名・フォルダーの場所・写真や曲の名前は含まれていません。")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="PhotoMovieMaker GPU の診断")
    parser.add_argument("--json", action="store_true", help="機械で読める形（JSON）で出す")
    parser.add_argument("--brief", action="store_true", help="setup.bat 用（結果は同じ。終了コードは NG があれば 1）")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    report = diagnose()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        print(render(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
