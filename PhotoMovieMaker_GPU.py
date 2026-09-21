# -*- coding: utf-8 -*-
"""
PhotoMovieMaker GPU
Windows 11 / NVIDIA GPU 向け 写真→MP4作成アプリ

主な機能
- フォルダー内の画像をファイル名の自然順で使用
- 1枚ごとの表示間隔（初期値 8秒）
- ランダム方向へゆっくりパン + ズーム
- 写真間クロスフェード
- 縦写真は「ぼかし背景 + 写真全体表示」
- BGMを「開始画像～終了画像」で区間指定
- BGM境界はクロスフェード
- NVIDIA NVENCを自動検出し、利用可能なら優先
- 1080p / 4K 選択
"""

from __future__ import annotations

import os
import re
import sys
import math
import time
import random
import shutil
import tempfile
import subprocess
import threading
import traceback
from dataclasses import dataclass
from pathlib import Path
from queue import Queue, Empty

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import cv2
import numpy as np
from PIL import Image, ImageOps, ImageFilter

try:
    import imageio_ffmpeg
except Exception:
    imageio_ffmpeg = None


APP_NAME = "PhotoMovieMaker GPU"
SUPPORTED_IMAGES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
AUDIO_FILETYPES = [
    ("Audio", "*.mp3 *.wav *.m4a *.aac *.flac *.ogg"),
    ("All files", "*.*"),
]

# Dual Xeon環境ではOpenCV内部の並列化も使う。
try:
    cv2.setNumThreads(max(1, os.cpu_count() or 8))
except Exception:
    pass


def natural_key(p: Path):
    parts = re.split(r"(\d+)", p.name.lower())
    return [int(x) if x.isdigit() else x for x in parts]


def smoothstep(x: float) -> float:
    x = max(0.0, min(1.0, x))
    return x * x * (3.0 - 2.0 * x)


def find_ffmpeg() -> str:
    # 1) PATH上のFFmpeg（NVENC対応版を入れていれば最優先）
    found = shutil.which("ffmpeg")
    if found:
        return found

    # 2) imageio-ffmpeg同梱版
    if imageio_ffmpeg is not None:
        try:
            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            pass

    raise RuntimeError(
        "FFmpegが見つかりません。\n"
        "setup.bat を実行するか、WindowsにFFmpegをインストールしてください。"
    )


def ffmpeg_has_nvenc(ffmpeg: str) -> bool:
    try:
        r = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        txt = (r.stdout or "") + (r.stderr or "")
        return "h264_nvenc" in txt
    except Exception:
        return False


@dataclass
class BGMSegment:
    start_index: int  # 0-based
    end_index: int    # 0-based, inclusive
    audio_path: Path
    fade_seconds: float = 2.5


@dataclass
class Motion:
    dx: float
    dy: float


class VideoRenderer:
    def __init__(
        self,
        image_paths: list[Path],
        output_path: Path,
        width: int,
        height: int,
        fps: int,
        interval_seconds: float,
        transition_seconds: float,
        zoom_percent: float,
        blur_background: bool,
        bgm_segments: list[BGMSegment],
        encoder_pref: str,
        q: Queue,
        stop_event: threading.Event,
    ):
        self.images = image_paths
        self.output = output_path
        self.w = width
        self.h = height
        self.fps = fps
        self.interval = interval_seconds
        self.transition = min(max(0.0, transition_seconds), max(0.0, interval_seconds - 0.05))
        self.zoom_end = 1.0 + max(0.0, zoom_percent) / 100.0
        self.blur_background = blur_background
        self.bgm_segments = bgm_segments
        self.encoder_pref = encoder_pref
        self.q = q
        self.stop_event = stop_event

        self.interval_frames = max(1, round(self.interval * self.fps))
        self.transition_frames = max(0, round(self.transition * self.fps))
        # 最終写真も interval 秒見せる
        self.total_frames = len(self.images) * self.interval_frames
        self.total_duration = self.total_frames / self.fps

        self.ffmpeg = find_ffmpeg()
        self.nvenc_available = ffmpeg_has_nvenc(self.ffmpeg)

    def load_canvas(self, path: Path) -> np.ndarray:
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")

            if self.blur_background:
                bg = ImageOps.fit(
                    im, (self.w, self.h),
                    method=Image.Resampling.LANCZOS,
                    centering=(0.5, 0.5),
                )
                blur_radius = max(14, int(min(self.w, self.h) * 0.027))
                bg = bg.filter(ImageFilter.GaussianBlur(blur_radius))

                fg = ImageOps.contain(
                    im, (self.w, self.h),
                    method=Image.Resampling.LANCZOS,
                )
                x = (self.w - fg.width) // 2
                y = (self.h - fg.height) // 2
                bg.paste(fg, (x, y))
                canvas = bg
            else:
                canvas = ImageOps.fit(
                    im, (self.w, self.h),
                    method=Image.Resampling.LANCZOS,
                    centering=(0.5, 0.5),
                )

        return np.asarray(canvas, dtype=np.uint8)

    def motions(self) -> list[Motion]:
        # 「完全ランダム」ではなく、上品に見えやすい方向セット。
        dirs = [
            (-1.00,  0.00), (1.00,  0.00),
            ( 0.00, -1.00), (0.00,  1.00),
            (-0.75, -0.75), (0.75, -0.75),
            (-0.75,  0.75), (0.75,  0.75),
            (-0.35,  0.20), (0.35, -0.20),
        ]
        rnd = random.Random(260921)  # 同じ素材なら再現可能
        result = []
        prev = None
        for _ in self.images:
            choices = [d for d in dirs if d != prev]
            d = rnd.choice(choices)
            prev = d
            result.append(Motion(*d))
        return result

    def render_motion(self, base: np.ndarray, motion: Motion, local_frame: int) -> np.ndarray:
        # intervalの最後でzoom_endに達する
        if self.interval_frames <= 1:
            u = 1.0
        else:
            u = local_frame / (self.interval_frames - 1)

        e = smoothstep(u)
        scale = 1.0 + (self.zoom_end - 1.0) * e

        # ズームによって生じる余裕の範囲だけパン。
        dx = motion.dx * (scale - 1.0) * self.w * 0.34
        dy = motion.dy * (scale - 1.0) * self.h * 0.34

        cx, cy = self.w / 2.0, self.h / 2.0
        M = np.array([
            [scale, 0.0, (1.0 - scale) * cx + dx],
            [0.0, scale, (1.0 - scale) * cy + dy],
        ], dtype=np.float32)

        return cv2.warpAffine(
            base, M, (self.w, self.h),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_REFLECT_101,
        )

    def choose_video_encoder_args(self) -> tuple[str, list[str]]:
        pref = self.encoder_pref.lower()

        if pref == "nvenc" and not self.nvenc_available:
            raise RuntimeError(
                "NVENCを指定しましたが、このFFmpegでは h264_nvenc が利用できません。\n"
                "NVIDIA対応FFmpegをPATHに入れるか、「自動」を選んでください。"
            )

        use_nvenc = (pref == "nvenc") or (pref == "auto" and self.nvenc_available)

        if use_nvenc:
            # A2000 (Ampere NVENC)を想定。品質優先寄り。
            return "NVIDIA NVENC", [
                "-c:v", "h264_nvenc",
                "-preset", "p6",
                "-tune", "hq",
                "-rc", "vbr",
                "-cq", "19",
                "-b:v", "0",
                "-profile:v", "high",
                "-pix_fmt", "yuv420p",
            ]

        # Dual Xeonを活かすCPU fallback
        threads = str(max(1, os.cpu_count() or 8))
        return "CPU x264", [
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "18",
            "-threads", threads,
            "-pix_fmt", "yuv420p",
        ]

    def render_silent_video(self, temp_video: Path):
        encoder_name, enc_args = self.choose_video_encoder_args()
        self.q.put(("encoder", encoder_name, self.nvenc_available, self.ffmpeg))

        cmd = [
            self.ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel", "error",
            "-f", "rawvideo",
            "-vcodec", "rawvideo",
            "-pix_fmt", "rgb24",
            "-s", f"{self.w}x{self.h}",
            "-r", str(self.fps),
            "-i", "-",
            "-an",
            *enc_args,
            "-movflags", "+faststart",
            str(temp_video),
        ]

        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=creationflags,
        )

        if proc.stdin is None:
            raise RuntimeError("FFmpegへの映像入力を開けません。")

        motions = self.motions()
        current = self.load_canvas(self.images[0])
        next_canvas = self.load_canvas(self.images[1]) if len(self.images) > 1 else None

        frame_no = 0

        try:
            for i, path in enumerate(self.images):
                if self.stop_event.is_set():
                    raise InterruptedError("処理を中止しました。")

                if i > 0:
                    current = next_canvas
                    next_canvas = (
                        self.load_canvas(self.images[i + 1])
                        if i + 1 < len(self.images) else None
                    )

                self.q.put(("status", f"映像作成: {i+1}/{len(self.images)}  {path.name}"))

                for local in range(self.interval_frames):
                    if self.stop_event.is_set():
                        raise InterruptedError("処理を中止しました。")

                    frame = self.render_motion(current, motions[i], local)

                    # interval末尾 transition 秒で次の写真へクロスフェード。
                    # 次写真の「本来の開始」は i+1 の8秒境界。
                    # その手前から先取りしてフェードさせる。
                    if (
                        next_canvas is not None
                        and self.transition_frames > 0
                        and local >= self.interval_frames - self.transition_frames
                    ):
                        k = local - (self.interval_frames - self.transition_frames)
                        next_local = k
                        nxt = self.render_motion(next_canvas, motions[i + 1], next_local)
                        a = smoothstep((k + 1) / self.transition_frames)
                        frame = cv2.addWeighted(frame, 1.0 - a, nxt, a, 0.0)

                    proc.stdin.write(frame.tobytes())
                    frame_no += 1

                    # UI更新は毎フレーム行わない
                    if frame_no % max(1, self.fps // 2) == 0:
                        pct = 100.0 * frame_no / self.total_frames
                        self.q.put(("progress", pct))

            proc.stdin.close()
            proc.stdin = None
            stderr = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
            rc = proc.wait()
            if rc != 0:
                raise RuntimeError("映像エンコード失敗:\n" + stderr[-4000:])

        except Exception:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except Exception:
                pass
            try:
                proc.kill()
            except Exception:
                pass
            raise

    def validate_bgm_segments(self):
        segs = sorted(self.bgm_segments, key=lambda s: (s.start_index, s.end_index))
        for s in segs:
            if s.start_index < 0 or s.end_index >= len(self.images) or s.start_index > s.end_index:
                raise ValueError("BGM区間の開始/終了画像が不正です。")
            if not s.audio_path.is_file():
                raise FileNotFoundError(f"BGMが見つかりません:\n{s.audio_path}")

        # 同じ写真範囲の重複指定は、事故のもとなので禁止
        for a, b in zip(segs, segs[1:]):
            if b.start_index <= a.end_index:
                raise ValueError(
                    "BGM区間が重複しています。\n"
                    f"{self.images[a.start_index].name}～{self.images[a.end_index].name}\n"
                    f"{self.images[b.start_index].name}～{self.images[b.end_index].name}"
                )
        return segs

    def mux_bgm(self, silent_video: Path):
        segs = self.validate_bgm_segments()

        if not segs:
            # BGMなしなら映像ファイルをそのまま完成品へ
            if self.output.exists():
                self.output.unlink()
            shutil.move(str(silent_video), str(self.output))
            return

        cmd = [
            self.ffmpeg, "-y",
            "-hide_banner", "-loglevel", "error",
            "-i", str(silent_video),
        ]

        # 各BGMは短ければ自動ループ
        for s in segs:
            cmd += ["-stream_loop", "-1", "-i", str(s.audio_path)]

        filters = []
        labels = []

        for idx, s in enumerate(segs, start=1):
            nominal_start = s.start_index * self.interval

            # 「終了画像の次の8秒境界」まで担当
            nominal_end = min((s.end_index + 1) * self.interval, self.total_duration)

            fade = max(0.0, min(s.fade_seconds, (nominal_end - nominal_start) / 2.0))

            # 次曲は境界の fade 秒前からフェードインして受け渡す。
            # 先頭曲は0秒から。途中曲は少し先行開始。
            actual_start = nominal_start if s.start_index == 0 else max(0.0, nominal_start - fade)
            actual_end = nominal_end

            duration = max(0.05, actual_end - actual_start)
            fade_in = 0.0 if actual_start == 0.0 else min(fade, duration / 2.0)
            fade_out = min(fade, duration / 2.0)

            chain = (
                f"[{idx}:a]"
                f"atrim=duration={duration:.6f},"
                f"asetpts=PTS-STARTPTS,"
            )

            if fade_in > 0:
                chain += f"afade=t=in:st=0:d={fade_in:.6f},"

            if fade_out > 0:
                chain += (
                    f"afade=t=out:"
                    f"st={max(0.0, duration - fade_out):.6f}:"
                    f"d={fade_out:.6f},"
                )

            delay_ms = int(round(actual_start * 1000))
            chain += f"adelay={delay_ms}|{delay_ms}[a{idx}]"

            filters.append(chain)
            labels.append(f"[a{idx}]")

        if len(labels) == 1:
            # 1曲だけでも全体長へ揃える
            filters.append(f"{labels[0]}apad,atrim=duration={self.total_duration:.6f}[mix]")
        else:
            filters.append(
                "".join(labels)
                + f"amix=inputs={len(labels)}:duration=longest:normalize=0,"
                  f"apad,atrim=duration={self.total_duration:.6f}[mix]"
            )

        filter_complex = ";".join(filters)

        cmd += [
            "-filter_complex", filter_complex,
            "-map", "0:v:0",
            "-map", "[mix]",
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "256k",
            "-movflags", "+faststart",
            "-shortest",
            str(self.output),
        ]

        self.q.put(("status", "BGMを合成しています…"))

        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        r = subprocess.run(
            cmd,
            capture_output=True, text=True,
            creationflags=creationflags,
        )
        if r.returncode != 0:
            raise RuntimeError("BGM合成失敗:\n" + (r.stderr or "")[-5000:])

    def run(self):
        self.output.parent.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(prefix="photomovie_") as td:
            temp_video = Path(td) / "video_only.mp4"
            self.render_silent_video(temp_video)

            if self.stop_event.is_set():
                raise InterruptedError("処理を中止しました。")

            self.mux_bgm(temp_video)

        self.q.put(("progress", 100.0))
        self.q.put(("done", str(self.output)))


class BGMDialog(tk.Toplevel):
    def __init__(self, master, image_names: list[str], initial=None):
        super().__init__(master)
        self.title("BGM区間")
        self.resizable(False, False)
        self.result = None
        self.image_names = image_names

        self.start_var = tk.StringVar()
        self.end_var = tk.StringVar()
        self.audio_var = tk.StringVar()
        self.fade_var = tk.DoubleVar(value=2.5)

        if initial:
            self.start_var.set(image_names[initial.start_index])
            self.end_var.set(image_names[initial.end_index])
            self.audio_var.set(str(initial.audio_path))
            self.fade_var.set(initial.fade_seconds)
        elif image_names:
            self.start_var.set(image_names[0])
            self.end_var.set(image_names[-1])

        frm = ttk.Frame(self, padding=14)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="開始画像").grid(row=0, column=0, sticky="w", pady=5)
        ttk.Combobox(
            frm, textvariable=self.start_var, values=image_names,
            width=52, state="readonly"
        ).grid(row=0, column=1, columnspan=2, sticky="ew", pady=5)

        ttk.Label(frm, text="終了画像").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Combobox(
            frm, textvariable=self.end_var, values=image_names,
            width=52, state="readonly"
        ).grid(row=1, column=1, columnspan=2, sticky="ew", pady=5)

        ttk.Label(frm, text="BGM").grid(row=2, column=0, sticky="w", pady=5)
        ttk.Entry(frm, textvariable=self.audio_var, width=45).grid(row=2, column=1, sticky="ew", pady=5)
        ttk.Button(frm, text="選択", command=self.pick_audio).grid(row=2, column=2, padx=(6, 0), pady=5)

        ttk.Label(frm, text="曲間フェード").grid(row=3, column=0, sticky="w", pady=5)
        ttk.Spinbox(frm, from_=0, to=10, increment=0.5, textvariable=self.fade_var, width=8).grid(
            row=3, column=1, sticky="w", pady=5
        )
        ttk.Label(frm, text="秒").grid(row=3, column=1, sticky="w", padx=(76, 0))

        note = (
            "次の曲は、指定区間の開始時刻より少し前からフェードインし、\n"
            "前の曲がフェードアウトしながら自然に受け渡します。"
        )
        ttk.Label(frm, text=note).grid(row=4, column=0, columnspan=3, sticky="w", pady=(8, 10))

        buttons = ttk.Frame(frm)
        buttons.grid(row=5, column=0, columnspan=3, sticky="e")
        ttk.Button(buttons, text="キャンセル", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="OK", command=self.ok).pack(side="right", padx=(0, 8))

        self.transient(master)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.wait_visibility()
        self.focus_force()

    def pick_audio(self):
        p = filedialog.askopenfilename(title="BGMを選択", filetypes=AUDIO_FILETYPES)
        if p:
            self.audio_var.set(p)

    def ok(self):
        if not self.start_var.get() or not self.end_var.get():
            messagebox.showerror("BGM区間", "開始画像と終了画像を選んでください。", parent=self)
            return
        if not self.audio_var.get():
            messagebox.showerror("BGM区間", "BGMファイルを選んでください。", parent=self)
            return

        si = self.image_names.index(self.start_var.get())
        ei = self.image_names.index(self.end_var.get())
        if si > ei:
            messagebox.showerror("BGM区間", "終了画像は開始画像以降にしてください。", parent=self)
            return

        self.result = BGMSegment(
            si, ei, Path(self.audio_var.get()), float(self.fade_var.get())
        )
        self.destroy()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("980x760")
        self.minsize(900, 700)

        self.q = Queue()
        self.stop_event = threading.Event()
        self.worker = None

        self.folder_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.interval_var = tk.DoubleVar(value=8.0)
        self.transition_var = tk.DoubleVar(value=1.0)
        self.zoom_var = tk.DoubleVar(value=8.0)
        self.blur_var = tk.BooleanVar(value=True)
        self.resolution_var = tk.StringVar(value="1920x1080")
        self.fps_var = tk.IntVar(value=30)
        self.encoder_var = tk.StringVar(value="自動")

        self.images: list[Path] = []
        self.bgm_segments: list[BGMSegment] = []

        self.build_ui()
        self.after(120, self.poll_queue)

    def build_ui(self):
        root = ttk.Frame(self, padding=14)
        root.pack(fill="both", expand=True)

        ttk.Label(root, text="PhotoMovieMaker GPU", font=("", 20, "bold")).pack(anchor="w")
        ttk.Label(
            root,
            text="写真を名前順に並べ、8秒ごとにパン＋ズームしながら切り替える記念映像を作成します。",
        ).pack(anchor="w", pady=(2, 12))

        inp = ttk.LabelFrame(root, text="写真と保存先", padding=10)
        inp.pack(fill="x")

        r = ttk.Frame(inp); r.pack(fill="x", pady=4)
        ttk.Label(r, text="写真フォルダー", width=14).pack(side="left")
        ttk.Entry(r, textvariable=self.folder_var).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(r, text="選択", command=self.choose_folder).pack(side="left")

        r = ttk.Frame(inp); r.pack(fill="x", pady=4)
        ttk.Label(r, text="出力MP4", width=14).pack(side="left")
        ttk.Entry(r, textvariable=self.output_var).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(r, text="選択", command=self.choose_output).pack(side="left")

        settings = ttk.LabelFrame(root, text="映像設定", padding=10)
        settings.pack(fill="x", pady=(10, 0))

        g = ttk.Frame(settings); g.pack(fill="x")

        ttk.Label(g, text="写真の間隔").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Spinbox(g, from_=2, to=30, increment=0.5, textvariable=self.interval_var, width=8).grid(row=0, column=1, padx=(5, 2))
        ttk.Label(g, text="秒").grid(row=0, column=2, sticky="w", padx=(0, 18))

        ttk.Label(g, text="写真クロスフェード").grid(row=0, column=3, sticky="w", pady=4)
        ttk.Spinbox(g, from_=0, to=4, increment=0.1, textvariable=self.transition_var, width=8).grid(row=0, column=4, padx=(5, 2))
        ttk.Label(g, text="秒").grid(row=0, column=5, sticky="w", padx=(0, 18))

        ttk.Label(g, text="ズーム量").grid(row=0, column=6, sticky="w")
        ttk.Spinbox(g, from_=0, to=20, increment=1, textvariable=self.zoom_var, width=8).grid(row=0, column=7, padx=(5, 2))
        ttk.Label(g, text="%").grid(row=0, column=8, sticky="w")

        ttk.Label(g, text="解像度").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Combobox(
            g, textvariable=self.resolution_var,
            values=["1920x1080", "3840x2160"],
            width=12, state="readonly"
        ).grid(row=1, column=1, columnspan=2, sticky="w", padx=(5, 18))

        ttk.Label(g, text="FPS").grid(row=1, column=3, sticky="w")
        ttk.Combobox(
            g, textvariable=self.fps_var,
            values=[24, 30, 60],
            width=7, state="readonly"
        ).grid(row=1, column=4, sticky="w", padx=(5, 18))

        ttk.Label(g, text="エンコーダ").grid(row=1, column=6, sticky="w")
        ttk.Combobox(
            g, textvariable=self.encoder_var,
            values=["自動", "NVENC", "CPU x264"],
            width=12, state="readonly"
        ).grid(row=1, column=7, columnspan=2, sticky="w", padx=(5, 0))

        ttk.Checkbutton(
            settings,
            text="縦写真も切らずに見せる（ぼかし背景 + 写真全体表示）",
            variable=self.blur_var,
        ).pack(anchor="w", pady=(7, 0))

        bgm = ttk.LabelFrame(root, text="BGM区間指定", padding=10)
        bgm.pack(fill="both", expand=True, pady=(10, 0))

        toolbar = ttk.Frame(bgm)
        toolbar.pack(fill="x", pady=(0, 6))
        ttk.Button(toolbar, text="BGM区間を追加", command=self.add_bgm).pack(side="left")
        ttk.Button(toolbar, text="選択区間を編集", command=self.edit_bgm).pack(side="left", padx=6)
        ttk.Button(toolbar, text="選択区間を削除", command=self.delete_bgm).pack(side="left")
        ttk.Label(
            toolbar,
            text="例：001.jpg～032.jpg = 曲A / 033.jpg～068.jpg = 曲B",
        ).pack(side="right")

        columns = ("start", "end", "music", "fade")
        self.tree = ttk.Treeview(bgm, columns=columns, show="headings", height=8)
        self.tree.heading("start", text="開始画像")
        self.tree.heading("end", text="終了画像")
        self.tree.heading("music", text="BGM")
        self.tree.heading("fade", text="曲間フェード")
        self.tree.column("start", width=160)
        self.tree.column("end", width=160)
        self.tree.column("music", width=380)
        self.tree.column("fade", width=100, anchor="center")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda e: self.edit_bgm())

        bottom = ttk.Frame(root)
        bottom.pack(fill="x", pady=(10, 0))

        self.summary = ttk.Label(bottom, text="写真フォルダーを選択してください。")
        self.summary.pack(anchor="w")

        actions = ttk.Frame(bottom)
        actions.pack(fill="x", pady=(8, 4))

        self.start_btn = ttk.Button(actions, text="MP4を作成", command=self.start)
        self.start_btn.pack(side="left")

        self.cancel_btn = ttk.Button(actions, text="中止", command=self.cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=8)

        self.encoder_info = ttk.Label(actions, text="")
        self.encoder_info.pack(side="right")

        self.progress = ttk.Progressbar(bottom, maximum=100)
        self.progress.pack(fill="x", pady=(4, 4))

        self.status = ttk.Label(bottom, text="待機中")
        self.status.pack(anchor="w")

    def choose_folder(self):
        p = filedialog.askdirectory(title="写真フォルダーを選択")
        if not p:
            return
        self.folder_var.set(p)
        self.load_images()
        if not self.output_var.get():
            self.output_var.set(str(Path(p) / "slideshow.mp4"))

    def choose_output(self):
        p = filedialog.asksaveasfilename(
            title="MP4の保存先",
            defaultextension=".mp4",
            filetypes=[("MP4", "*.mp4")],
        )
        if p:
            self.output_var.set(p)

    def load_images(self):
        folder = Path(self.folder_var.get())
        if not folder.is_dir():
            self.images = []
        else:
            self.images = sorted(
                [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED_IMAGES],
                key=natural_key,
            )

        self.bgm_segments.clear()
        self.refresh_bgm_tree()
        self.update_summary()

    def update_summary(self):
        n = len(self.images)
        if not n:
            self.summary.config(text="画像ファイルが見つかりません。")
            return
        duration = n * float(self.interval_var.get())
        mins = int(duration // 60)
        secs = int(round(duration % 60))
        self.summary.config(
            text=f"{n}枚 / 予想動画時間 約 {mins}分{secs:02d}秒 / "
                 f"先頭: {self.images[0].name} / 最後: {self.images[-1].name}"
        )

    def add_bgm(self):
        if not self.images:
            messagebox.showinfo("BGM", "先に写真フォルダーを選択してください。")
            return
        dlg = BGMDialog(self, [p.name for p in self.images])
        self.wait_window(dlg)
        if dlg.result:
            self.bgm_segments.append(dlg.result)
            self.bgm_segments.sort(key=lambda s: s.start_index)
            self.refresh_bgm_tree()

    def selected_bgm_index(self):
        sel = self.tree.selection()
        if not sel:
            return None
        return int(sel[0])

    def edit_bgm(self):
        idx = self.selected_bgm_index()
        if idx is None:
            return
        seg = self.bgm_segments[idx]
        dlg = BGMDialog(self, [p.name for p in self.images], initial=seg)
        self.wait_window(dlg)
        if dlg.result:
            self.bgm_segments[idx] = dlg.result
            self.bgm_segments.sort(key=lambda s: s.start_index)
            self.refresh_bgm_tree()

    def delete_bgm(self):
        idx = self.selected_bgm_index()
        if idx is None:
            return
        del self.bgm_segments[idx]
        self.refresh_bgm_tree()

    def refresh_bgm_tree(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        for i, s in enumerate(self.bgm_segments):
            start = self.images[s.start_index].name if self.images else ""
            end = self.images[s.end_index].name if self.images else ""
            self.tree.insert(
                "", "end", iid=str(i),
                values=(start, end, s.audio_path.name, f"{s.fade_seconds:g} 秒")
            )

    def encoder_pref_internal(self):
        v = self.encoder_var.get()
        if v == "NVENC":
            return "nvenc"
        if v == "CPU x264":
            return "cpu"
        return "auto"

    def validate(self):
        if not self.images:
            raise ValueError("写真フォルダーに画像がありません。")
        if not self.output_var.get():
            raise ValueError("出力MP4を指定してください。")
        if float(self.interval_var.get()) <= 0:
            raise ValueError("写真の間隔は0より大きくしてください。")
        if float(self.transition_var.get()) >= float(self.interval_var.get()):
            raise ValueError("写真クロスフェードは写真の間隔より短くしてください。")

    def start(self):
        try:
            self.validate()
        except Exception as e:
            messagebox.showerror(APP_NAME, str(e))
            return

        self.stop_event.clear()
        self.start_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.progress["value"] = 0

        w, h = [int(x) for x in self.resolution_var.get().split("x")]

        renderer = VideoRenderer(
            image_paths=self.images.copy(),
            output_path=Path(self.output_var.get()),
            width=w,
            height=h,
            fps=int(self.fps_var.get()),
            interval_seconds=float(self.interval_var.get()),
            transition_seconds=float(self.transition_var.get()),
            zoom_percent=float(self.zoom_var.get()),
            blur_background=bool(self.blur_var.get()),
            bgm_segments=list(self.bgm_segments),
            encoder_pref=self.encoder_pref_internal(),
            q=self.q,
            stop_event=self.stop_event,
        )

        def work():
            try:
                renderer.run()
            except InterruptedError as e:
                self.q.put(("cancelled", str(e)))
            except Exception:
                self.q.put(("error", traceback.format_exc()))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def cancel(self):
        self.stop_event.set()
        self.status.config(text="中止しています…")

    def poll_queue(self):
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]

                if kind == "status":
                    self.status.config(text=item[1])

                elif kind == "progress":
                    self.progress["value"] = float(item[1])

                elif kind == "encoder":
                    name, nv, ff = item[1], item[2], item[3]
                    self.encoder_info.config(
                        text=f"使用: {name} / NVENC検出: {'あり' if nv else 'なし'}"
                    )

                elif kind == "done":
                    self.start_btn.config(state="normal")
                    self.cancel_btn.config(state="disabled")
                    self.status.config(text="完成しました。")
                    messagebox.showinfo(APP_NAME, f"完成しました。\n\n{item[1]}")

                elif kind == "cancelled":
                    self.start_btn.config(state="normal")
                    self.cancel_btn.config(state="disabled")
                    self.status.config(text="中止しました。")

                elif kind == "error":
                    self.start_btn.config(state="normal")
                    self.cancel_btn.config(state="disabled")
                    self.status.config(text="エラー")
                    messagebox.showerror(APP_NAME, item[1][-7000:])

        except Empty:
            pass

        self.after(120, self.poll_queue)


if __name__ == "__main__":
    App().mainloop()
