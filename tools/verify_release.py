# -*- coding: utf-8 -*-
"""配布 ZIP を別のフォルダーへ展開し、展開物だけで動くことを確かめる（Release Gate）。

    py -3 tools/verify_release.py [--zip dist/…_Windows.zip] [--work C:\\Temp\\pmm_release_verify] [--keep]

開発用のフォルダー・開発用の Python のパッケージ・PYTHONPATH には頼らない。
確かめること: setup（2 回）・診断・run.bat からの起動・CPU / NVENC / BGM 付きの書き出し・
PATH に FFmpeg が無いときの imageio-ffmpeg・モデルの読み込み・CLI・後片付け・個人の情報が無いこと。
写真と曲は、その場で作る合成物だけ（ZIP には入れない）。結果は dist/verify/verify_report.json。
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import wave
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import build_release  # noqa: E402

FLAGS = subprocess.CREATE_NO_WINDOW
PLAIN = "PhotoMovieMaker"
AWKWARD = "動画 テスト (1)#PMM & 試験"
PACKAGES = ("pillow", "numpy", "opencv-python", "imageio-ffmpeg", "onnxruntime")

SMOKE = r'''
import json, shutil, socket, sys, threading
from pathlib import Path
from queue import Queue

app_dir, photos, music, out, encoder, camera = sys.argv[1:7]
sys.path.insert(0, app_dir)


class NoNetwork(socket.socket):                      # アプリ（Python 側）が通信しないことの番人
    def connect(self, *a, **k):
        raise RuntimeError("network use detected")
    connect_ex = connect


socket.socket = NoNetwork
import PhotoMovieMaker_GPU as app

if len(sys.argv) > 7 and sys.argv[7] == "no-nvenc":
    # NVIDIA の GPU が無い PC のまね: FFmpeg の一覧には h264_nvenc があるが、試し書き出しは失敗する
    real_run = app.subprocess.run

    def run(cmd, *a, **k):
        if "h264_nvenc" in cmd and "lavfi" in cmd:
            return app.subprocess.CompletedProcess(cmd, 1, "", "Cannot load nvcuda.dll")
        return real_run(cmd, *a, **k)
    app.subprocess.run = run

images = sorted(Path(photos).glob("*.jpg"), key=app.natural_key)
segments = [app.BGMSegment(0, len(images) - 1, Path(music))] if music != "-" else []
r = app.VideoRenderer(
    image_paths=images, output_path=Path(out), width=1280, height=720, fps=30, interval_seconds=2.0,
    transition_seconds=0.5, zoom_percent=8.0, blur_background=True, bgm_segments=segments, encoder_pref=encoder,
    q=Queue(), stop_event=threading.Event(),
    title=app.TitleCard(main="試験の動画", sub="smoke test", duration=2.0, fade_seconds=0.5, text_fade_seconds=1.0),
    camera_mode=camera)
r.run()
print("RESULT " + json.dumps({
    "encoder_used": r.encoder_used, "frames": r.total_frames, "seconds": r.total_duration,
    "ffmpeg": "PATH" if shutil.which("ffmpeg") else "imageio-ffmpeg", "nvenc_usable": r.nvenc_available,
    "detections": sorted({d.mode for d in r.detections.values()}), "version": app.APP_VERSION}))
'''

GUI_FLOW = r'''
import json, sys, time
from pathlib import Path

app_dir, photos, music, out_dir = sys.argv[1:5]
sys.path.insert(0, app_dir)
import PhotoMovieMaker_GPU as app

shown = []                                          # 確認の窓は出さずに、文面だけ記録する
for name in ("showinfo", "showwarning", "showerror"):
    setattr(app.messagebox, name, lambda title, text="", _n=name, **k: shown.append((_n, str(text))))


def pump(window, until, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not until():
        window.update()
        time.sleep(0.02)
    return until()


window = app.App()
window.folder_var.set(photos)                       # 写真フォルダーを選ぶ
window.load_images()
names = [p.name for p in window.images]
window.images = [window.images[1], window.images[0]] + window.images[2:]     # 並べ替え（1 枚目と 2 枚目を入れ替え）
window.excluded.add(window.images[3])               # 4 枚目は使わない
window.update_summary()
window.bgm_segments.append(app.BGMSegment(0, 2, Path(music)))                # BGM 区間（使う写真の 1〜3 枚目）
window.refresh_bgm_tree()
window.interval_var.set(2.0)
window.transition_var.set(0.5)
window.fps_var.set(24)
window.encoder_var.set("CPU x264")
window.title_main_var.set("画面の試験")
window.title_dur_var.set(2.0)
window.title_text_fade_var.set(1.0)
first = Path(out_dir) / "画面から作成.mp4"
window.output_var.set(str(first))
window.start()                                      # 「MP4を作成」
done = pump(window, lambda: str(window.start_btn["state"]) == "normal" and window.worker is not None
            and not window.worker.is_alive(), 300)
pump(window, lambda: False, 0.5)
settings = json.loads(first.with_name(first.stem + "_settings.json").read_text(encoding="utf-8")) if done else {}

second = Path(out_dir) / "中止する動画.mp4"         # 「中止」
window.interval_var.set(8.0)
window.output_var.set(str(second))
window.start()
pump(window, lambda: False, 1.5)
window.cancel()
cancelled = pump(window, lambda: not window.worker.is_alive() and str(window.start_btn["state"]) == "normal", 60)
status = window.status.cget("text")
title = window.title()
window.destroy()
print("RESULT " + json.dumps({
    "title": title, "loaded": names, "done": bool(done), "output": first.is_file(),
    "image_order": settings.get("image_order"), "included": [i["included"] for i in settings.get("images", [])],
    "bgm": len(settings.get("bgm_segments", [])), "encoder": settings.get("video", {}).get("encoder_used"),
    "frames": settings.get("video", {}).get("total_frames"),
    "messages": [m for m in shown], "cancelled": bool(cancelled), "cancel_status": status,
    "cancel_left_file": second.exists()}, ensure_ascii=False))
'''

MODELS = r'''
import json, sys
from pathlib import Path
import numpy as np

app_dir, photos = sys.argv[1:3]
sys.path.insert(0, app_dir)
import subject_detector as sd
import photo_embedding as pe

detector = sd.SubjectDetector()
rgb = np.full((480, 640, 3), 128, dtype=np.uint8)
found = detector.detect("gray.jpg", rgb) if detector.available else None
extractor = pe.EmbeddingExtractor()
paths = sorted(Path(photos).glob("*.jpg"))
vectors = extractor.embed_paths(paths) if extractor.available else {}
print("RESULT " + json.dumps({
    "detector_available": detector.available, "detect_ran": found is not None,
    "embedding_available": extractor.available, "embedded": len(vectors),
    "embedding_dimension": len(next(iter(vectors.values()))) if vectors else 0}))
'''


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def clean_env(**extra: str) -> dict:
    """開発環境の影響を外した環境変数。"""
    env = {k: v for k, v in os.environ.items() if k.upper() not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}
    env.update(PMM_NO_PAUSE="1", **extra)
    return env


def without_ffmpeg(env: dict) -> dict:
    """このプロセスの子だけ、PATH から FFmpeg のあるフォルダーを外す（PC の PATH は変えない）。"""
    keep = [d for d in env.get("PATH", "").split(os.pathsep)
            if d and not any((Path(d) / n).is_file() for n in ("ffmpeg.exe", "ffprobe.exe", "ffmpeg.EXE"))]
    return dict(env, PATH=os.pathsep.join(keep))


def run_bat(bat: Path, cwd: Path, env: dict, timeout: float = 1800) -> tuple[int, str]:
    proc = subprocess.run(f'cmd /d /s /c ""{bat}""', cwd=cwd, env=env, capture_output=True, timeout=timeout)
    return proc.returncode, proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace")


def fingerprint(folder: Path) -> dict:
    return {p.name: (sha256(p), p.stat().st_mtime_ns) for p in sorted(folder.iterdir()) if p.is_file()}


def temp_leftovers() -> set:
    t = Path(tempfile.gettempdir())
    return {p.name for pat in ("pmm_render_*", "photomovie_*", "pmm_doctor_*") for p in t.glob(pat)}


def make_media(folder: Path) -> tuple[Path, Path]:
    """合成の写真 4 枚（横 3・縦 1）と合成の BGM（WAV）。"""
    from PIL import Image, ImageDraw
    photos, music = folder / "写真 (合成) #1", folder / "曲 (合成) & 試験"
    photos.mkdir(parents=True)
    music.mkdir(parents=True)
    for k in range(4):
        size = (1067, 1600) if k == 2 else (1600, 1067)
        im = Image.new("RGB", size, (40 + 50 * k, 110 + 20 * k, 200 - 40 * k))
        d = ImageDraw.Draw(im)
        d.ellipse([size[0] * 0.3, size[1] * 0.25, size[0] * 0.7, size[1] * 0.75], fill=(240, 225, 200))
        d.rectangle([40, 40, 240, 160], fill=(30, 30, 30))
        im.save(photos / f"写真 {k + 1}.jpg", quality=90)
    track = music / "合成の曲 — 試験音.wav"
    rate = 44100
    with wave.open(str(track), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for i in range(rate * 12):
            v = int(9000 * math.sin(2 * math.pi * 330 * i / rate) * (0.6 + 0.4 * math.sin(2 * math.pi * 0.5 * i / rate)))
            frames += struct.pack("<hh", v, v)
        w.writeframes(bytes(frames))
    return photos, track


def probe(mp4: Path) -> dict:
    """検査側の道具（PATH の ffprobe / ffmpeg）で、できた MP4 を確かめる。"""
    info = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,width,height,nb_frames:format=duration",
         "-of", "json", str(mp4)], capture_output=True, text=True, creationflags=FLAGS).stdout)
    decode = subprocess.run(["ffmpeg", "-v", "error", "-i", str(mp4), "-f", "null", "-"], capture_output=True,
                            text=True, creationflags=FLAGS)
    streams = {s["codec_type"]: s for s in info["streams"]}
    return {"video": streams.get("video", {}).get("codec_name"), "audio": streams.get("audio", {}).get("codec_name"),
            "width": streams.get("video", {}).get("width"), "height": streams.get("video", {}).get("height"),
            "frames": int(streams.get("video", {}).get("nb_frames") or 0),
            "seconds": round(float(info["format"]["duration"]), 2),
            "decodes_cleanly": decode.returncode == 0 and not decode.stderr.strip(), "bytes": mp4.stat().st_size}


def find_window(title: str) -> int:
    return ctypes.windll.user32.FindWindowW(None, title)


class Verifier:
    def __init__(self, zip_path: Path, work: Path):
        self.zip_path, self.work = zip_path, work
        self.results: list[dict] = []
        self.ok = True

    def check(self, name: str, passed: bool, detail="") -> bool:
        self.results.append({"check": name, "ok": bool(passed), "detail": detail})
        self.ok = self.ok and bool(passed)
        print(("  [OK] " if passed else "  [NG] ") + name + (f" — {detail}" if detail else ""), flush=True)
        return bool(passed)

    def python(self, app: Path, code: str, *args: str, env: dict, cwd: Path, name: str, timeout: float = 900) -> dict | None:
        """展開物の .venv の Python で、展開物の外に置いたスクリプトを動かす。"""
        script = self.work / "_scripts" / f"{name}.py"
        script.parent.mkdir(exist_ok=True)
        script.write_text(code, encoding="utf-8")
        proc = subprocess.run([str(app / ".venv" / "Scripts" / "python.exe"), "-B", str(script), str(app), *args],
                              capture_output=True, env=dict(env, PYTHONUTF8="1", OPENCV_LOG_LEVEL="ERROR"), cwd=cwd,
                              timeout=timeout, creationflags=FLAGS)
        out = proc.stdout.decode("utf-8", "replace")
        line = next((ln for ln in out.splitlines() if ln.startswith("RESULT ")), None)
        if proc.returncode != 0 or not line:
            self.check(f"{name}: スクリプトが終わる", False, (proc.stderr.decode("utf-8", "replace") or out)[-600:])
            return None
        return json.loads(line[7:])

    def install(self, label: str, folder_name: str) -> Path | None:
        print(f"\n== {label}: 展開 → setup ==", flush=True)
        target = self.work / folder_name
        target.mkdir(parents=True)
        with zipfile.ZipFile(self.zip_path) as z:
            z.extractall(target)
        tops = [p.name for p in target.iterdir()]
        self.check(f"{label}: ZIP の直下はフォルダー 1 つ", len(tops) == 1 and tops[0].startswith("PhotoMovieMaker_GPU_v"), str(tops))
        app = target / tops[0]
        problems = build_release.scan(target)
        self.check(f"{label}: 展開物に禁止ファイル・個人の情報が無い", not problems, "; ".join(problems)[:300])
        manifest = json.loads((app / "RELEASE_MANIFEST.json").read_text(encoding="utf-8"))
        wrong = [f["path"] for f in manifest["files"] if sha256(app / f["path"]) != f["sha256"]]
        self.check(f"{label}: 展開物が manifest のハッシュと一致", not wrong, str(wrong))
        code, out = run_bat(app / "run.bat", self.work, clean_env())
        self.check(f"{label}: setup の前の run.bat は案内して終わる", code == 1 and "まず setup.bat を実行してください" in out)
        started = time.monotonic()
        code, out = run_bat(app / "setup.bat", self.work, clean_env())          # 別のフォルダーから起動する
        first = round(time.monotonic() - started, 1)
        self.check(f"{label}: setup.bat（.venv なしから）", code == 0 and "セットアップが終わりました" in out,
                   f"{first} 秒" if code == 0 else out[-600:])
        if code != 0:
            return None
        self.check(f"{label}: .venv がアプリのフォルダーの中にできた", (app / ".venv" / "Scripts" / "python.exe").is_file())
        started = time.monotonic()
        code, out = run_bat(app / "setup.bat", self.work, clean_env())
        self.check(f"{label}: setup.bat をもう一度（壊れない）", code == 0 and "既にある .venv を使います" in out,
                   f"{round(time.monotonic() - started, 1)} 秒")
        return app

    def diagnose(self, label: str, app: Path, env: dict, expect_ffmpeg: str) -> dict | None:
        proc = subprocess.run([str(app / ".venv" / "Scripts" / "python.exe"), "-B", str(app / "app_doctor.py"), "--json"],
                              capture_output=True, env=dict(env, PYTHONUTF8="1"), cwd=self.work, timeout=600, creationflags=FLAGS)
        try:
            report = json.loads(proc.stdout.decode("utf-8"))
        except ValueError:
            self.check(f"{label}: 診断が動く", False, proc.stderr.decode("utf-8", "replace")[-400:])
            return None
        by = {c["name"]: c for c in report["checks"]}
        bad = [c["name"] for c in report["checks"] if c["status"] in ("NG", "注意")]
        self.check(f"{label}: 診断で NG・注意なし", report["ok"] and not bad, str(bad))
        self.check(f"{label}: 診断の FFmpeg は {expect_ffmpeg}", expect_ffmpeg in by["FFmpeg"]["detail"], by["FFmpeg"]["detail"])
        self.check(f"{label}: 診断は .venv の Python で動いている", by["仮想環境 (.venv)"]["status"] == "OK")
        text = json.dumps(report, ensure_ascii=False)
        self.check(f"{label}: 診断の結果に場所・ユーザー名が無い", ":\\" not in text and "Users" not in text
                   and str(self.work) not in text)
        return report

    def start_gui(self, label: str, app: Path, version: str) -> None:
        title = f"PhotoMovieMaker GPU v{version}"
        if find_window(title):
            self.check(f"{label}: run.bat から起動", False, "同じ題名の窓が既に開いています（閉じてからやり直してください）")
            return
        proc = subprocess.Popen(f'cmd /d /s /c ""{app / "run.bat"}""', cwd=self.work, env=clean_env(),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=FLAGS)
        hwnd, deadline = 0, time.monotonic() + 90
        while time.monotonic() < deadline and proc.poll() is None and not hwnd:
            time.sleep(0.5)
            hwnd = find_window(title)
        if hwnd:
            time.sleep(1.5)
            ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)        # WM_CLOSE（窓の × と同じ）
        try:
            code = proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
            code = -1
        out = proc.stdout.read().decode("utf-8", "replace") if proc.stdout else ""
        self.check(f"{label}: run.bat から起動し、窓「{title}」が開いて閉じる", bool(hwnd) and code == 0,
                   "" if hwnd and code == 0 else f"終了コード {code} / {out[-400:]}")

    def render(self, label: str, app: Path, media, env: dict, *, encoder: str, camera: str = "legacy", bgm: bool = True,
               expect_encoder: str | None = None, expect_ffmpeg: str | None = None, simulate: str = "-") -> None:
        photos, track = media
        out = self.work / "_output" / f"{label}.mp4".replace(":", "")
        out.parent.mkdir(exist_ok=True)
        result = self.python(app, SMOKE, str(photos), str(track) if bgm else "-", str(out), encoder, camera, simulate,
                             env=env, cwd=self.work, name="smoke_render")
        if result is None:
            return
        info = probe(out)
        expected_frames = int(round((2.0 + 4 * 2.0) * 30))
        good = (info["video"] == "h264" and info["width"] == 1280 and info["height"] == 720
                and info["frames"] == expected_frames == result["frames"] and info["decodes_cleanly"]
                and (info["audio"] == "aac") == bgm and abs(info["seconds"] - 10.0) < 0.2)
        if expect_encoder:
            good = good and result["encoder_used"] == expect_encoder
        if expect_ffmpeg:
            good = good and result["ffmpeg"] == expect_ffmpeg
        settings = out.with_name(out.stem + "_settings.json")
        good = good and settings.is_file() and json.loads(settings.read_text(encoding="utf-8"))["app_version"] == result["version"]
        self.check(f"{label}", good, f"{result['encoder_used']}・FFmpeg={result['ffmpeg']}・{info['frames']} フレーム・"
                                     f"{info['seconds']} 秒・映像 {info['video']}・音声 {info['audio']}・{info['bytes']:,} バイト")

    def gui_flow(self, label: str, app: Path, media, env: dict) -> None:
        """画面の操作の流れ（フォルダー → 並べ替え → 使わない写真 → BGM → 作成 → 中止）を、画面の部品を直接動かして確かめる。"""
        photos, track = media
        out = self.work / "_output"
        out.mkdir(exist_ok=True)
        r = self.python(app, GUI_FLOW, str(photos), str(track), str(out), env=env, cwd=self.work, name="gui_flow")
        if r is None:
            return
        files = sorted(p.name for p in photos.glob("*.jpg"))
        self.check(f"{label}: 写真フォルダーを開くと自然順で 4 枚", r["loaded"] == files, str(r["loaded"]))
        self.check(f"{label}: 並べ替えと「使わない」が動画に反映される",
                   r["done"] and r["output"] and r["image_order"] == [files[1], files[0], files[2]]
                   and r["included"] == [True, True, True, False], f"上映順 {r['image_order']}")
        self.check(f"{label}: BGM 付きで完成し、完成の表示が出る",
                   r["bgm"] == 1 and r["frames"] == (2 + 3 * 2) * 24 and any("完成しました" in m[1] for m in r["messages"])
                   and not any(m[0] == "showerror" for m in r["messages"]), f"{r['encoder']}・{r['frames']} フレーム")
        self.check(f"{label}: 「中止」で止まり、作りかけを残さない",
                   r["cancelled"] and r["cancel_status"] == "中止しました。" and not r["cancel_left_file"], r["cancel_status"])
        probe_ok = probe(out / "画面から作成.mp4") if r["output"] else {}
        self.check(f"{label}: 画面から作った MP4 が再生できる形（h264 + aac）",
                   probe_ok.get("video") == "h264" and probe_ok.get("audio") == "aac" and probe_ok.get("decodes_cleanly"),
                   f"{probe_ok.get('seconds')} 秒")

    def cli(self, label: str, app: Path, media, env: dict) -> None:
        photos, track = media
        py = str(app / ".venv" / "Scripts" / "python.exe")
        out = self.work / "_output"

        def call(*args):
            proc = subprocess.run([py, "-B", str(app / "pmm_cli.py"), *args], capture_output=True,
                                  env=dict(env, PYTHONUTF8="1"), cwd=self.work, timeout=900, creationflags=FLAGS)
            return json.loads(proc.stdout.decode("utf-8").strip().splitlines()[-1])
        caps = call("capabilities")
        self.check(f"{label}: CLI capabilities", caps["ok"] and caps["result"]["models"] == {"embedding": True, "subject_detection": True},
                   f"v{caps['result']['version']}")
        edits = {"selected": sorted(p.name for p in photos.glob("*.jpg")),
                 "title_card": {"main": "CLI の試験", "duration": 2.0, "text_fade_seconds": 1.0},
                 "video": {"width": 640, "height": 360, "fps": 15, "interval_seconds": 2.0, "transition_seconds": 0.5,
                           "camera_mode": "subject_safe", "encoder_choice": "cpu"},
                 "bgm_segments": [{"start_photo": 1, "end_photo": 4, "audio": track.name}],
                 "transitions": {"profile": {"crossfade": 0.5, "cut": 0.25, "slide": 0.25}, "seed": 1,
                                 "overrides": [{"after_photo": 2, "type": "fade_black"}]},
                 "audio_safety": {"mode": "limiter", "ceiling_db": -1.0}}
        project = out / "CLI の試験.photomovie.json"
        composed = call("compose", "--folder", str(photos), "--edits-json", json.dumps(edits, ensure_ascii=False),
                        "--save", str(project), "--music-folder", str(track.parent))
        rendered = call("render", "--project", str(project), "--folder", str(photos), "--output", str(out / "CLI の試験.mp4"),
                        "--music-folder", str(track.parent)) if composed["ok"] else {"ok": False, "error": composed.get("error")}
        good = rendered["ok"] and rendered["result"]["audio_safety"]["true_peak_dbtp"] <= -1.0 + 0.05 \
            and rendered["result"]["transitions"].get("fade_black") == 1
        self.check(f"{label}: CLI compose → render（subject_safe・つなぎ方・limiter）", good,
                   str(rendered.get("error") or {k: rendered["result"][k] for k in ("transitions", "encoder_used")}))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="配布 ZIP の確認（Release Gate）")
    parser.add_argument("--zip", default=str(build_release.DIST / build_release.ZIP_NAME))
    parser.add_argument("--work", default=r"C:\Temp\pmm_release_verify")
    parser.add_argument("--keep", action="store_true", help="確認に使ったフォルダーを残す")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    zip_path, work = Path(args.zip).resolve(), Path(args.work)
    if ROOT in work.resolve().parents or work.resolve() == ROOT:
        raise SystemExit("確認用のフォルダーは、ソースのフォルダーの外にしてください。")
    if work.exists():
        raise SystemExit(f"確認用のフォルダーが既にあります（消してからやり直してください）: {work}")
    parent_existed = work.parent.exists()
    work.mkdir(parents=True)
    v = Verifier(zip_path, work)
    sums = (zip_path.parent / "SHA256SUMS.txt").read_text(encoding="utf-8").split()
    v.check("ZIP の SHA-256 が SHA256SUMS.txt と一致", sums[0] == sha256(zip_path) and sums[1] == zip_path.name)
    leftovers_before = temp_leftovers()
    media = make_media(work / "_media")
    before = fingerprint(media[0]), fingerprint(media[1].parent)
    versions: dict = {}
    version = build_release.VERSION
    try:
        app = v.install("ふつうの場所", PLAIN)
        if app:
            env = clean_env()
            v.diagnose("ふつうの場所", app, env, "PATH 上のもの")
            pip = subprocess.run([str(app / ".venv" / "Scripts" / "python.exe"), "-m", "pip", "list", "--format=json"],
                                 capture_output=True, text=True, env=env, creationflags=FLAGS)
            versions = {p["name"].lower(): p["version"] for p in json.loads(pip.stdout) if p["name"].lower() in PACKAGES}
            pyver = subprocess.run([str(app / ".venv" / "Scripts" / "python.exe"), "-c",
                                    "import sys; print('.'.join(map(str, sys.version_info[:3])))"],
                                   capture_output=True, text=True, creationflags=FLAGS).stdout.strip()
            versions["python"] = pyver
            v.check("パッケージが .venv に入っている", set(PACKAGES) <= set(versions), json.dumps(versions))
            v.start_gui("ふつうの場所", app, version)
            print("\n== 画面の操作の流れ ==", flush=True)
            v.gui_flow("画面", app, media, env)
            print("\n== 書き出し ==", flush=True)
            v.render("書き出し: 写真だけ（自動）", app, media, env, encoder="auto", bgm=False)
            v.render("書き出し: BGM 付き・CPU x264（NVIDIA 無しでも動く）", app, media, env, encoder="cpu", expect_encoder="CPU x264")
            nvenc = v.python(app, "import sys, json; sys.path.insert(0, sys.argv[1]); import PhotoMovieMaker_GPU as a; "
                                  "print('RESULT ' + json.dumps({'nvenc': a.ffmpeg_has_nvenc(a.find_ffmpeg())}))",
                             env=env, cwd=work, name="nvenc")
            if nvenc and nvenc["nvenc"]:
                v.render("書き出し: BGM 付き・自動 → NVENC", app, media, env, encoder="auto", expect_encoder="NVIDIA NVENC")
            else:
                v.check("書き出し: 自動 → NVENC", True, "この PC では NVENC を使えないため省略（自動は CPU になる）")
            v.render("書き出し: BGM 付き・自動（NVENC が使えない PC のまね）→ CPU x264", app, media, env, encoder="auto",
                     expect_encoder="CPU x264", simulate="no-nvenc")
            v.render("書き出し: 被写体追従（subject）", app, media, env, encoder="cpu", camera="subject")
            v.render("書き出し: 安全な構図（subject_safe）", app, media, env, encoder="cpu", camera="subject_safe")
            print("\n== PATH に FFmpeg が無い状態 ==", flush=True)
            bare = without_ffmpeg(env)
            v.check("この確認では PATH に FFmpeg が無い", shutil.which("ffmpeg", path=bare["PATH"]) is None)
            v.diagnose("FFmpeg なしの PATH", app, bare, "imageio-ffmpeg に入っているもの")
            v.render("書き出し: BGM 付き・CPU（imageio-ffmpeg の FFmpeg）", app, media, bare, encoder="cpu",
                     expect_encoder="CPU x264", expect_ffmpeg="imageio-ffmpeg")
            v.render("書き出し: BGM 付き・自動（imageio-ffmpeg の FFmpeg）", app, media, bare, encoder="auto",
                     expect_ffmpeg="imageio-ffmpeg")
            print("\n== モデル・CLI ==", flush=True)
            models = v.python(app, MODELS, str(media[0]), env=env, cwd=work, name="models")
            if models:
                v.check("YuNet・YOLOX を読み込んで検出が動く（実行時のダウンロードなし）",
                        models["detector_available"] and models["detect_ran"])
                v.check("DINOv2 を読み込んで embedding が出る", models["embedding_available"] and models["embedded"] == 4,
                        f"{models['embedding_dimension']} 次元")
            v.cli("CLI", app, media, env)
        hard = v.install("日本語・空白・括弧・#・& を含む場所", AWKWARD)
        if hard:
            env = clean_env()
            v.diagnose("特殊な場所", hard, env, "PATH 上のもの")
            v.start_gui("特殊な場所", hard, version)
            v.render("書き出し: 特殊な場所・BGM 付き・CPU", hard, media, env, encoder="cpu", expect_encoder="CPU x264")
        v.check("元の写真・曲は変わっていない", (fingerprint(media[0]), fingerprint(media[1].parent)) == before)
        v.check("一時フォルダーに作りかけが残っていない", temp_leftovers() == leftovers_before,
                str(sorted(temp_leftovers() - leftovers_before)))
        parts = [p.name for p in (work / "_output").iterdir() if p.suffix in (".part", ".tmp")] if (work / "_output").exists() else []
        v.check("書き出し先に作りかけ（.part）が残っていない", not parts, str(parts))
    finally:
        report = {"zip": zip_path.name, "zip_sha256": sha256(zip_path), "version": version, "ok": v.ok,
                  "verified_python": versions.get("python"), "packages": {k: versions[k] for k in PACKAGES if k in versions},
                  "work_folders": [PLAIN, AWKWARD], "checks": v.results}
        target = build_release.DIST / "verify"
        target.mkdir(parents=True, exist_ok=True)
        (target / "verify_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)
            if not parent_existed:
                try:
                    work.parent.rmdir()
                except OSError:
                    pass
    passed = sum(1 for r in v.results if r["ok"])
    print(f"\n結果: {passed}/{len(v.results)} 件 OK — {'RELEASE GATE: PASS' if v.ok else 'RELEASE GATE: FAIL'}")
    return 0 if v.ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
