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
import json
import math
import random
import shutil
import tempfile
import subprocess
import threading
import traceback
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from queue import Queue, Empty

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import cv2
import numpy as np
from PIL import Image, ImageOps, ImageFilter, ImageDraw, ImageFont, ImageColor

try:
    import imageio_ffmpeg
except Exception:
    imageio_ffmpeg = None

# 被写体追従カメラ（実験機能）。無くても従来カメラワークは動く。
try:
    import subject_detector
except Exception:
    subject_detector = None

# おすすめ解析（写真を選ぶときの手がかり）。無くても写真選択はそのまま使える。
try:
    import photo_recommendation
except Exception:
    photo_recommendation = None

# 画像embedding（似た場面を見つける補助）。無ければ従来の解析だけで動く。
try:
    import photo_embedding
except Exception:
    photo_embedding = None


APP_NAME = "PhotoMovieMaker GPU"

# カメラワーク。既定は従来方式で、何も設定しなければ今までと同じ結果になる。
CAMERA_LEGACY = "legacy"
CAMERA_SUBJECT = "subject"
# 被写体へ寄りつつ、見つかった被写体の枠（顔・人物・犬）＋余白が最後のフレームまで画面に残るよう、
# 写真ごとにズームを弱め、パンを抑える（SI Director v2）。指定しなければ従来のまま。
CAMERA_SUBJECT_SAFE = "subject_safe"
SUBJECT_CAMERA_MODES = (CAMERA_SUBJECT, CAMERA_SUBJECT_SAFE)
SAFE_MARGIN_RATIO = 0.03        # 被写体の枠の外側に残す余白（表示している写真の幅・高さに対する比率）
SAFE_ZOOM_STEP_PERCENT = 0.5    # 収まらないときにズームを弱める刻み（8% → 7.5% → … → 0%）
SAFE_MIN_RELATIVE_AREA = 0.2    # 同じ種類で一番大きい枠の 20% 未満の枠（遠くの人など）は守る対象にしない
SAFE_OPEN_EDGE_RATIO = 0.015    # 枠が写真の端からこの距離以内なら、その辺は元の写真ですでに切れている（守らない）
PAN_RATIO = 0.34                # パンはズームで生まれた余白のこの割合まで（render_motion と同じ値）

# 写真と写真のつなぎ方（SI Director v2）。Project に指定が無ければ従来どおりクロスフェードだけ。
TRANSITION_CROSSFADE = "crossfade"
TRANSITION_CUT = "cut"
TRANSITION_FADE_BLACK = "fade_black"
TRANSITION_SLIDE = "slide"
TRANSITION_TYPES = (TRANSITION_CROSSFADE, TRANSITION_CUT, TRANSITION_FADE_BLACK, TRANSITION_SLIDE)
SUPPORTED_IMAGES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
AUDIO_FILETYPES = [
    ("Audio", "*.mp3 *.wav *.m4a *.aac *.flac *.ogg"),
    ("All files", "*.*"),
]

# タイトルカード用フォント。日本語glyphを持つものを先に試す。
TITLE_FONT_CANDIDATES = [
    "YuGothM.ttc",    # 游ゴシック Medium
    "YuGothR.ttc",    # 游ゴシック Regular
    "meiryo.ttc",     # メイリオ
    "YuGothB.ttc",    # 游ゴシック Bold
    "msgothic.ttc",   # MS ゴシック
    "segoeui.ttf",    # Segoe UI（欧文のみ）
    "arial.ttf",
]

_font_cache: dict = {}


def _font_dirs() -> list[Path]:
    dirs = []
    win = os.environ.get("WINDIR") or r"C:\Windows"
    dirs.append(Path(win) / "Fonts")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        # ユーザー単位でインストールされたフォント
        dirs.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
    return [d for d in dirs if d.is_dir()]


def _draws_glyphs(font, text: str) -> bool:
    """文字が「豆腐」(.notdef)にならずに描けるか確認する。"""
    try:
        size = max(8, getattr(font, "size", 16))
        def probe(ch: str) -> bytes:
            im = Image.new("L", (size * 3, size * 3), 0)
            ImageDraw.Draw(im).text((size // 2, size // 2), ch, font=font, fill=255)
            return im.tobytes()
        notdef = probe(chr(0xFFFF))  # 非文字。どのフォントにも無いので必ず .notdef になる
        for ch in set(text):
            if ch.isspace():
                continue
            if probe(ch) == notdef:
                return False
        return True
    except Exception:
        return True


def load_title_font(size: int, text: str):
    """Windows 11上で text を描けるフォントを安全に探す。
    見つからなければ Pillow 既定フォントへフォールバックし、例外は投げない。"""
    key = (size, "".join(sorted(set(text))))
    if key in _font_cache:
        return _font_cache[key]

    dirs = _font_dirs()
    for name in TITLE_FONT_CANDIDATES:
        for d in dirs:
            path = d / name
            if not path.is_file():
                continue
            try:
                font = ImageFont.truetype(str(path), size)
            except Exception:
                continue
            if _draws_glyphs(font, text):
                _font_cache[key] = font
                return font

    # どれも見つからない/描けない場合でもクラッシュさせない
    try:
        font = ImageFont.load_default(size)
    except Exception:
        font = ImageFont.load_default()
    _font_cache[key] = font
    return font


def parse_color(value: str, fallback: str) -> str:
    """"#FFFFFF" 等のHEX文字列を検証する。不正なら fallback を返す。"""
    try:
        ImageColor.getrgb(value.strip())
        return value.strip()
    except Exception:
        return fallback

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


@dataclass
class TitleCard:
    """動画冒頭のタイトル区間。写真リストには含めない。"""
    enabled: bool = True
    main: str = ""
    sub: str = ""
    date: str = ""
    duration: float = 5.0        # タイトルを見せる秒数
    fade_seconds: float = 1.0    # タイトル→写真1 のクロスフェード
    text_fade_seconds: float = 1.5  # 白背景の上で文字だけが現れるまでの秒数
    bg_color: str = "#FFFFFF"
    fg_color: str = "#333333"


@dataclass
class BGMTiming:
    """BGM全体の入り方・つなぎ方・終わり方。区間ごとではなく全体で1組持つ。"""
    first_offset: float = 3.0     # 動画開始から最初のBGMが鳴り出すまで
                                  # 冒頭を無音にして「溜め」を作るため既定は3.0秒
    first_fade_in: float = 1.5    # 冒頭のフェードイン
    fade_out: float = 1.5         # 曲の終わりのフェードアウト
    silence_gap: float = 0.7      # 曲と曲の間の無音
    fade_in: float = 1.5          # 次の曲のフェードイン
    final_fade_out: float = 3.0   # 最後の曲を動画末尾で消すまで


@dataclass
class Motion:
    dx: float
    dy: float
    # この写真だけのズーム（1.0 = ズームなし）。None なら全体のズーム（zoom_percent）のまま。
    zoom: float | None = None


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
        title: "TitleCard | None" = None,
        bgm_timing: "BGMTiming | None" = None,
        camera_mode: str = CAMERA_LEGACY,
        settings_extra: dict | None = None,
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

        self.title = title if (title and title.enabled) else None
        self.bgm_timing = bgm_timing or BGMTiming()
        self.camera_mode = camera_mode
        self.settings_extra = settings_extra or {}
        # 写真ごとの解析結果。画像は持たず、bbox由来の軽い値だけ。
        # 並べ替えても別の写真へ結果が付かないよう、キーは写真のパスにする。
        self.detections: dict[str, object] = {}
        self.source_sizes: dict[str, tuple[int, int]] = {}
        # 安全な構図（subject_safe）で写真ごとに決めたこと。設定ファイルと書き出し結果に残す。
        self.framing_log: list[dict] = []

        self.interval_frames = max(1, round(self.interval * self.fps))
        self.transition_frames = max(0, round(self.transition * self.fps))

        # タイトルカードは「写真0」ではなく独立した区間。
        # self.images には一切入れないので、写真番号はタイトル有無で変わらない。
        if self.title:
            self.title_frames = max(1, round(self.title.duration * self.fps))
            self.title_fade_frames = min(
                max(0, round(self.title.fade_seconds * self.fps)),
                self.title_frames,
            )
            # 文字だけのフェードイン。タイトル区間の内側の演出なので
            # 総フレーム数（＝動画の長さ）には影響しない。
            self.title_text_fade_frames = min(
                max(0, round(self.title.text_fade_seconds * self.fps)),
                self.title_frames,
            )
        else:
            self.title_frames = 0
            self.title_fade_frames = 0
            self.title_text_fade_frames = 0

        # 最終写真も interval 秒見せる
        self.total_frames = self.title_frames + len(self.images) * self.interval_frames
        self.total_duration = self.total_frames / self.fps
        # 音声側はフレーム数から逆算した値を使い、映像と必ず一致させる
        self.title_duration = self.title_frames / self.fps
        self.photo_span = self.interval_frames / self.fps

        self.encoder_used = ""
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

    def render_title_background(self) -> np.ndarray:
        """タイトルカードの背景だけ。文字フェードイン中の下地に使う。
        文字入りcanvasと背景canvasを混ぜると、背景画素は同じ値どうしの
        混合になるので一切変化せず、文字のopacityだけが変わる。"""
        rgb = ImageColor.getrgb(self.title.bg_color)[:3]
        return np.ascontiguousarray(
            np.full((self.h, self.w, 3), rgb, dtype=np.uint8)
        )

    def render_title_canvas(self) -> np.ndarray:
        """タイトルカードを1枚の静止画として作る。
        パン・ズーム・warpAffine は一切かけない。"""
        t = self.title
        img = Image.new("RGB", (self.w, self.h), t.bg_color)
        draw = ImageDraw.Draw(img)

        # 画面高さに対する比率で決めるので 1080p でも 4K でも同じ見え方になる
        rows = [
            (t.main, 0.070),
            (t.sub, 0.035),
            (t.date, 0.030),
        ]
        rows = [(text.strip(), ratio) for text, ratio in rows if text and text.strip()]
        if not rows:
            return np.asarray(img, dtype=np.uint8)

        laid_out = []
        for text, ratio in rows:
            font = load_title_font(max(12, int(self.h * ratio)), text)
            box = draw.textbbox((0, 0), text, font=font)
            laid_out.append((text, font, box))

        line_gap = int(self.h * 0.035)
        block_h = sum(b[3] - b[1] for _, _, b in laid_out) + line_gap * (len(laid_out) - 1)
        y = (self.h - block_h) // 2

        for text, font, box in laid_out:
            text_w = box[2] - box[0]
            draw.text(
                ((self.w - text_w) // 2 - box[0], y - box[1]),
                text, font=font, fill=t.fg_color,
            )
            y += (box[3] - box[1]) + line_gap

        return np.ascontiguousarray(np.asarray(img, dtype=np.uint8))

    # ------------------------------------------------------------------
    # 被写体追従カメラ（実験機能）
    # 従来のパン＆ズームには手を入れず、パン方向だけを差し替える。
    # 被写体が1つだけ見つかった写真以外は、すべて従来方式のまま。
    # ------------------------------------------------------------------

    def analyze_subjects(self):
        """写真ごとに1回だけ検出を走らせる。モデルのloadも1回だけ。

        失敗しても例外を投げず、その写真（または全体）を従来方式へ戻す。
        """
        if subject_detector is None:
            self.q.put(("warning",
                        "被写体検出モジュール(subject_detector.py)が読み込めません。" + chr(10)
                        + "従来カメラワークで作成します。"))
            return

        try:
            detector = subject_detector.SubjectDetector()
        except Exception as e:
            self.q.put(("warning",
                        "被写体検出の準備に失敗しました。従来カメラワークで作成します。" + chr(10)
                        + f"{type(e).__name__}: {e}"))
            return

        if not detector.available:
            # モデルが無い等。動画作成そのものは従来方式で続行する。
            self.q.put(("warning", detector.unavailable_reason))
            return

        total = len(self.images)
        for i, path in enumerate(self.images):
            if self.stop_event.is_set():
                raise InterruptedError("処理を中止しました。")
            try:
                with Image.open(path) as im:
                    # 検出は「元写真」に対して行う。
                    # ぼかし背景を合成したcanvasでは被写体が二重に写るため。
                    rgb = np.asarray(
                        ImageOps.exif_transpose(im).convert("RGB"), dtype=np.uint8
                    )
                self.source_sizes[str(path)] = (rgb.shape[1], rgb.shape[0])
                det = detector.detect(path.name, rgb)
                del rgb
            except Exception as e:
                det = subject_detector.SubjectDetection(
                    filename=path.name, note=f"analysis error: {type(e).__name__}"
                )
            self.detections[str(path)] = det
            self.q.put((
                "status",
                f"被写体解析中: {i+1}/{total}  顔{det.face_count} 犬{det.dog_count}"
                f" → {det.mode}  {path.name}",
            ))

    def subject_target_on_canvas(self, index: int):
        """元写真の正規化座標を、実際に表示される前景写真上の位置へ写す。

        ぼかし背景ONなら contain + 中央paste、OFFなら fit(中央クロップ)と
        同じ幾何で計算する。戻り値は最終canvasのピクセル座標。
        """
        if not (0 <= index < len(self.images)):
            return None
        key = str(self.images[index])
        det = self.detections.get(key)
        size = self.source_sizes.get(key)
        if det is None or size is None or not det.has_target:
            return None

        iw, ih = size
        if iw <= 0 or ih <= 0:
            return None
        nx, ny = det.target_x, det.target_y
        im_ratio = iw / ih
        dest_ratio = self.w / self.h

        if self.blur_background:
            # ImageOps.contain と同じ大きさに縮小し、中央へ貼る
            if im_ratio > dest_ratio:
                fw, fh = self.w, max(1, round(ih / iw * self.w))
            elif im_ratio < dest_ratio:
                fw, fh = max(1, round(iw / ih * self.h)), self.h
            else:
                fw, fh = self.w, self.h
            px = (self.w - fw) // 2
            py = (self.h - fh) // 2
            cx = px + nx * fw
            cy = py + ny * fh
        else:
            # ImageOps.fit と同じ中央クロップ
            if im_ratio > dest_ratio:
                crop_w, crop_h = ih * dest_ratio, float(ih)
            else:
                crop_w, crop_h = float(iw), iw / dest_ratio
            ox = (iw - crop_w) / 2.0
            oy = (ih - crop_h) / 2.0
            cx = (nx * iw - ox) / crop_w * self.w
            cy = (ny * ih - oy) / crop_h * self.h
            # クロップで切り落とされた側にある場合は画面内へ寄せる
            cx = min(max(cx, 0.0), float(self.w))
            cy = min(max(cy, 0.0), float(self.h))

        return cx, cy

    def subject_motion(self, index: int):
        """被写体の方向へ寄るためのパン方向を求める。

        完全に中央へ持ってくることは要求しない。ズームで生まれた余白の
        範囲（従来と同じ ±1.0）へclampするので、画面外は絶対に出ない。
        """
        target = self.subject_target_on_canvas(index)
        if target is None:
            return None
        span = self.zoom_end - 1.0
        if span <= 0:
            return None  # ズーム0%では寄る余地がない

        cx, cy = target
        limit_x = span * self.w * 0.34
        limit_y = span * self.h * 0.34
        if limit_x <= 0 or limit_y <= 0:
            return None

        # render_motion の式を逆に解く。被写体が中央へ近づく向きになる。
        mdx = self.zoom_end * (self.w / 2.0 - cx) / limit_x
        mdy = self.zoom_end * (self.h / 2.0 - cy) / limit_y
        mdx = max(-1.0, min(1.0, mdx))
        mdy = max(-1.0, min(1.0, mdy))
        if not (np.isfinite(mdx) and np.isfinite(mdy)):
            return None
        return Motion(mdx, mdy)

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

        # 被写体追従ONで、その写真に被写体が1つだけ見つかっていれば、
        # パン方向だけを目標方向へ差し替える。
        # 見つからない・複数ある・解析に失敗した写真は上のランダム方向のまま。
        if self.camera_mode in SUBJECT_CAMERA_MODES and self.detections:
            for i in range(len(result)):
                try:
                    m = self.subject_motion(i)
                except Exception:
                    m = None
                if m is not None:
                    result[i] = m

        # 安全な構図: 上で決めた動き（寄る方向）を基本に、被写体の枠＋余白が
        # 最後のフレームまで画面に残るよう、ズームを弱め・パンを抑える。
        if self.camera_mode == CAMERA_SUBJECT_SAFE:
            self.framing_log = []
            for i in range(len(result)):
                try:
                    result[i], entry = self.safe_motion(i, result[i])
                except Exception as e:      # 1枚の計算に失敗しても、その写真は元の動きのまま
                    entry = {"adjustment": "error", "note": f"{type(e).__name__}"}
                entry.update(photo=i + 1, file=self.images[i].name)
                self.framing_log.append(entry)
        return result

    # ------------------------------------------------------------------
    # 安全な構図（subject_safe）
    # 「被写体の中心へ寄る」だけでなく「大事な被写体の範囲を画面に残す」ことを保証する。
    # 服・ドレスを見分けるモデルは使わない。既存の検出の枠（顔・人物・犬）と幾何だけで決める。
    # ------------------------------------------------------------------

    def photo_rect_on_canvas(self, index: int):
        """写真が実際に見えている範囲（canvas のピクセル座標）と、元写真→canvas の写し方。"""
        iw, ih = self.source_sizes[str(self.images[index])]
        im_ratio, dest_ratio = iw / ih, self.w / self.h
        if self.blur_background:
            if im_ratio > dest_ratio:
                fw, fh = self.w, max(1, round(ih / iw * self.w))
            elif im_ratio < dest_ratio:
                fw, fh = max(1, round(iw / ih * self.h)), self.h
            else:
                fw, fh = self.w, self.h
            px, py = (self.w - fw) // 2, (self.h - fh) // 2

            def to_canvas(nx, ny):
                return px + nx * fw, py + ny * fh
            return (float(px), float(py), float(px + fw), float(py + fh)), to_canvas, (fw, fh)
        if im_ratio > dest_ratio:
            crop_w, crop_h = ih * dest_ratio, float(ih)
        else:
            crop_w, crop_h = float(iw), iw / dest_ratio
        ox, oy = (iw - crop_w) / 2.0, (ih - crop_h) / 2.0

        def to_canvas(nx, ny):
            return (nx * iw - ox) / crop_w * self.w, (ny * ih - oy) / crop_h * self.h
        return (0.0, 0.0, float(self.w), float(self.h)), to_canvas, (self.w * iw / crop_w, self.h * ih / crop_h)

    def safe_region(self, index: int):
        """守る範囲（canvas のピクセル座標）。辺ごとに x0, y0, x1, y1、守らない辺は None。

        - 守る枠: 顔・人物・犬の枠（同じ種類で一番大きい枠の 20% 未満は遠くの写り込みとして外す）
        - 枠が写真の端（見えている範囲の端）に届いている辺は、元の写真ですでに切れているので守らない
        - 余白は 3%。ただし写真の端までの隙間の半分まで（余白のせいでズームを止めないため）
        守る枠が無ければ None。"""
        key = str(self.images[index])
        det = self.detections.get(key)
        if det is None or key not in self.source_sizes:
            return None
        boxes = [b for b in (getattr(det, "boxes", None) or []) if b.get("kind") in ("face", "person", "dog")]
        largest: dict[str, float] = {}
        for b in boxes:
            area = (b["x1"] - b["x0"]) * (b["y1"] - b["y0"])
            largest[b["kind"]] = max(largest.get(b["kind"], 0.0), area)
        kept = [b for b in boxes
                if (b["x1"] - b["x0"]) * (b["y1"] - b["y0"]) >= SAFE_MIN_RELATIVE_AREA * largest[b["kind"]]]
        if not kept:
            return None
        rect, to_canvas, (fw, fh) = self.photo_rect_on_canvas(index)
        x0 = y0 = float("inf")
        x1 = y1 = float("-inf")
        for b in kept:
            ax, ay = to_canvas(b["x0"], b["y0"])
            bx, by = to_canvas(b["x1"], b["y1"])
            x0, y0, x1, y1 = min(x0, ax), min(y0, ay), max(x1, bx), max(y1, by)
        tol_x, tol_y = SAFE_OPEN_EDGE_RATIO * fw, SAFE_OPEN_EDGE_RATIO * fh
        mx, my = SAFE_MARGIN_RATIO * fw, SAFE_MARGIN_RATIO * fh
        region = [
            None if x0 <= rect[0] + tol_x else x0 - min(mx, (x0 - rect[0]) / 2.0),
            None if y0 <= rect[1] + tol_y else y0 - min(my, (y0 - rect[1]) / 2.0),
            None if x1 >= rect[2] - tol_x else x1 + min(mx, (rect[2] - x1) / 2.0),
            None if y1 >= rect[3] - tol_y else y1 + min(my, (rect[3] - y1) / 2.0),
        ]
        return tuple(region)

    def visible_rect(self, motion: Motion, zoom: float):
        """動きの最後のフレーム（一番寄ったとき）に見えている canvas の範囲。

        render_motion の式から、見えている範囲は拡大が進むほど単調に狭くなる。
        最後のフレームで収まっていれば、途中のフレームでも必ず収まっている。"""
        s1 = 1.0 - 1.0 / zoom
        cx, cy = self.w / 2.0, self.h / 2.0
        kx, ky = motion.dx * self.w * PAN_RATIO, motion.dy * self.h * PAN_RATIO
        return ((cx - kx) * s1, (cy - ky) * s1, self.w - (cx + kx) * s1, self.h - (cy + ky) * s1)

    @staticmethod
    def region_inside(region, vis) -> bool:
        return ((region[0] is None or vis[0] <= region[0]) and (region[1] is None or vis[1] <= region[1])
                and (region[2] is None or vis[2] >= region[2]) and (region[3] is None or vis[3] >= region[3]))

    def framing_summary(self) -> dict:
        """安全な構図で写真ごとに決めたことのまとめ（設定ファイル・書き出し結果に残す）。"""
        log = self.framing_log
        zooms = [e["zoom_percent"] for e in log if "zoom_percent" in e]
        counts: dict[str, int] = {}
        for e in log:
            counts[e["adjustment"]] = counts.get(e["adjustment"], 0) + 1
        return {
            "margin_ratio": SAFE_MARGIN_RATIO,
            "zoom_step_percent": SAFE_ZOOM_STEP_PERCENT,
            "configured_zoom_percent": round((self.zoom_end - 1.0) * 100.0, 4),
            "adjusted": sum(1 for e in log if e["adjustment"] in ("pan_limited", "zoom_reduced", "zoom_off")),
            "by_adjustment": counts,
            "average_zoom_percent": round(sum(zooms) / len(zooms), 3) if zooms else None,
            "photos": log,
        }

    def safe_motion(self, index: int, motion: Motion):
        """被写体の範囲が最後まで画面に残る、元の動きに一番近い動きを返す。(Motion, 記録)。

        ズームは設定値から 0.5% ずつ弱め、収まる一番強いズームを使う（寄れる範囲では寄る）。
        パンは元の方向を基本に、収まる範囲へ抑える。守る枠が無い写真は元の動きのまま。"""
        base_zoom = self.zoom_end if motion.zoom is None else motion.zoom
        region = self.safe_region(index)
        entry = {"zoom_percent": round((base_zoom - 1.0) * 100.0, 3), "adjustment": "none"}
        if region is None:
            entry["adjustment"] = "no_subject"
            return motion, entry
        entry["region"] = [None if v is None else round(v / d, 4)
                           for v, d in zip(region, (self.w, self.h, self.w, self.h))]
        entry["open_sides"] = [name for v, name in zip(region, ("left", "top", "right", "bottom")) if v is None]
        if base_zoom <= 1.0:
            return motion, entry
        if self.region_inside(region, self.visible_rect(motion, base_zoom)):
            return motion, entry            # そのままで収まっている（従来と同じ動き）

        cx, cy = self.w / 2.0, self.h / 2.0
        step = SAFE_ZOOM_STEP_PERCENT / 100.0
        n_steps = int(math.floor((base_zoom - 1.0) / step + 1e-9))
        candidates = [base_zoom] + [1.0 + (n_steps - j) * step for j in range(1, n_steps + 1)]
        for zoom in candidates:
            if zoom <= 1.0 + 1e-9:
                break
            s1 = 1.0 - 1.0 / zoom
            lim_x, lim_y = self.w * PAN_RATIO, self.h * PAN_RATIO
            # (cx - kx) * s1 <= x0  かつ  w - (cx + kx) * s1 >= x1  を満たす kx の範囲（y も同じ）
            lo_x = -lim_x if region[0] is None else max(-lim_x, cx - region[0] / s1)
            hi_x = lim_x if region[2] is None else min(lim_x, (self.w - region[2]) / s1 - cx)
            lo_y = -lim_y if region[1] is None else max(-lim_y, cy - region[1] / s1)
            hi_y = lim_y if region[3] is None else min(lim_y, (self.h - region[3]) / s1 - cy)
            if lo_x > hi_x or lo_y > hi_y:
                continue
            kx = min(max(motion.dx * lim_x, lo_x), hi_x)
            ky = min(max(motion.dy * lim_y, lo_y), hi_y)
            safe = Motion(kx / lim_x, ky / lim_y, zoom=zoom)
            entry["zoom_percent"] = round((zoom - 1.0) * 100.0, 3)
            entry["adjustment"] = "pan_limited" if zoom == base_zoom else "zoom_reduced"
            return safe, entry
        entry["zoom_percent"] = 0.0
        entry["adjustment"] = "zoom_off"
        return Motion(motion.dx, motion.dy, zoom=1.0), entry

    def render_motion(self, base: np.ndarray, motion: Motion, local_frame: int) -> np.ndarray:
        # intervalの最後でzoom_endに達する
        if self.interval_frames <= 1:
            u = 1.0
        else:
            u = local_frame / (self.interval_frames - 1)

        e = smoothstep(u)
        zoom_end = self.zoom_end if motion.zoom is None else motion.zoom
        scale = 1.0 + (zoom_end - 1.0) * e

        # ズームによって生じる余裕の範囲だけパン。
        dx = motion.dx * (scale - 1.0) * self.w * PAN_RATIO
        dy = motion.dy * (scale - 1.0) * self.h * PAN_RATIO

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
        self.encoder_used = encoder_name
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

        def emit(frame: np.ndarray):
            nonlocal frame_no
            proc.stdin.write(frame.tobytes())
            frame_no += 1
            # UI更新は毎フレーム行わない
            if frame_no % max(1, self.fps // 2) == 0:
                self.q.put(("progress", 100.0 * frame_no / self.total_frames))

        try:
            if self.title_frames:
                self.q.put(("status", "タイトルカードを作成中…"))
                title_canvas = self.render_title_canvas()
                title_bg = (self.render_title_background()
                            if self.title_text_fade_frames > 0 else None)

                for local in range(self.title_frames):
                    if self.stop_event.is_set():
                        raise InterruptedError("処理を中止しました。")

                    # 白背景は固定のまま、文字だけを0%から100%へ浮き出させる。
                    # frame 0 で0%、text_fade_frames で100%。
                    if title_bg is not None and local < self.title_text_fade_frames:
                        a = smoothstep(local / self.title_text_fade_frames)
                        frame = cv2.addWeighted(title_bg, 1.0 - a, title_canvas, a, 0.0)
                    else:
                        frame = title_canvas
                    # タイトル区間の最後で写真1へクロスフェードする。
                    # 写真1の時間軸は「写真1の区間開始」が0なので、ここでは負の値。
                    # これにより動画全体が余計に伸びず、写真1の動きも途切れない。
                    if self.title_fade_frames > 0 and (
                        local >= self.title_frames - self.title_fade_frames
                    ):
                        k = local - (self.title_frames - self.title_fade_frames)
                        nxt = self.render_motion(
                            current, motions[0], k - self.title_fade_frames
                        )
                        a = smoothstep((k + 1) / self.title_fade_frames)
                        frame = cv2.addWeighted(frame, 1.0 - a, nxt, a, 0.0)

                    emit(frame)

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
                        # 次写真の時間軸は「自分の区間の先頭」が0。
                        # クロスフェード中はその手前なので負の値になる。
                        # ここを k のままにすると、次写真が先に動き出してから
                        # 区間開始時に local=0 へ巻き戻り、境界で画が飛ぶ。
                        next_local = k - self.transition_frames
                        nxt = self.render_motion(next_canvas, motions[i + 1], next_local)
                        a = smoothstep((k + 1) / self.transition_frames)
                        frame = cv2.addWeighted(frame, 1.0 - a, nxt, a, 0.0)

                    emit(frame)

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
                # Windowsのkillは非同期なので、終了を待たずに抜けると
                # FFmpegが一時ファイルを掴んだままになり、
                # TemporaryDirectoryの後始末がWinError 32で失敗する。
                proc.wait(timeout=15)
            except Exception:
                pass
            try:
                if proc.stderr:
                    proc.stderr.close()
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

    def bgm_plan(self, segs: list[BGMSegment]):
        """各BGM区間を実時間へ割り付ける。

        方式は「前曲フェードアウト → 無音 → 次曲フェードイン」。
        2曲を重ねないので、境界での音量加算もクリッピングも起きない。

        戻り値: [(開始秒, 長さ秒, フェードイン秒, フェードアウト秒), ...]
        """
        t = self.bgm_timing
        MIN_AUDIO = 0.05   # 計算上これ以下には縮めない
        # 実際に鳴らす下限。これより短くなった区間は鳴らさず無音にする。
        # 0.1秒だけ音が出ても聞こえないうえ、そこまで短い音声を
        # FFmpegのフィルターへ渡すと合成が失敗することがあるため。
        MIN_AUDIBLE = 0.30
        plan = []

        for i, s_ in enumerate(segs):
            # 写真番号はタイトルの有無で変わらない。タイトルぶんは時間軸だけずらす。
            nominal_start = self.title_duration + s_.start_index * self.photo_span
            nominal_end = min(
                self.title_duration + (s_.end_index + 1) * self.photo_span,
                self.total_duration,
            )
            span = max(0.0, nominal_end - nominal_start)

            is_opening = (s_.start_index == 0)
            prev_adjacent = i > 0 and s_.start_index == segs[i - 1].end_index + 1
            is_last = (i == len(segs) - 1)
            reaches_end = nominal_end >= self.total_duration - 1e-6

            if is_opening:
                # 写真1から始まる曲だけ、タイトルカード中から先行して鳴らす。
                actual_start = min(
                    max(0.0, t.first_offset),
                    max(0.0, nominal_end - MIN_AUDIO),
                )
                fade_in = max(0.0, t.first_fade_in)
            else:
                # 直前の区間と連続しているときだけ無音を挟む。
                # 写真が飛んでいる場合はもともと無音なので gap は不要。
                gap = max(0.0, t.silence_gap) if prev_adjacent else 0.0
                gap = min(gap, max(0.0, span - MIN_AUDIO))
                actual_start = nominal_start + gap
                fade_in = max(0.0, t.fade_in)

            # 最後の曲が動画の最後まで担当しているときだけ最終フェードアウト。
            # 途中で終わる場合はその区間の末尾で消して、以後は無音のまま。
            fade_out = max(0.0, t.final_fade_out if (is_last and reaches_end)
                           else t.fade_out)

            duration = max(MIN_AUDIO, nominal_end - actual_start)

            # 区間が短いとフェードが入りきらないので按分して縮める。
            # （短い区間でも負の開始時刻や次境界越えを起こさないため）
            if fade_in + fade_out > duration:
                scale = duration / (fade_in + fade_out)
                fade_in *= scale
                fade_out *= scale

            if duration < MIN_AUDIBLE:
                # 短すぎるので鳴らさない。この区間は無音になる。
                continue

            plan.append((s_, actual_start, duration, fade_in, fade_out))

        return plan

    def mux_bgm(self, silent_video: Path):
        segs = self.validate_bgm_segments()

        if not segs:
            # BGMなしなら映像ファイルをそのまま完成品へ
            if self.output.exists():
                self.output.unlink()
            shutil.move(str(silent_video), str(self.output))
            return

        plan = self.bgm_plan(segs)

        if not plan:
            # 鳴らせる長さの区間が残らなかった。無音のまま完成させる。
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
        for entry in plan:
            cmd += ["-stream_loop", "-1", "-i", str(entry[0].audio_path)]

        filters = []
        labels = []

        for idx, (_seg, start, duration, fade_in, fade_out) in enumerate(plan, start=1):
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

            delay_ms = int(round(start * 1000))
            chain += f"adelay={delay_ms}|{delay_ms}[a{idx}]"

            filters.append(chain)
            labels.append(f"[a{idx}]")

        if len(labels) == 1:
            # 1曲だけでも全体長へ揃える
            filters.append(f"{labels[0]}apad,atrim=duration={self.total_duration:.6f}[mix]")
        else:
            # 時間上は重ならない設計なので amix は「並べる」だけの役割。
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
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            creationflags=creationflags,
        )
        errors: list[bytes] = []
        reader = threading.Thread(target=lambda: errors.append(proc.stderr.read()), daemon=True)
        reader.start()
        # 合成の最中も中止できるようにする（映像の描画と同じ stop_event を見る）。
        # 止めたら FFmpeg を終わらせ、作りかけのファイルは残さない。
        try:
            while proc.poll() is None:
                if self.stop_event.wait(0.1):
                    proc.kill()
                    proc.wait(timeout=15)
                    reader.join(timeout=5)
                    try:
                        self.output.unlink()
                    except OSError:
                        pass
                    raise InterruptedError("処理を中止しました。")
            reader.join(timeout=5)
        finally:
            try:
                proc.stderr.close()
            except OSError:
                pass
        if proc.returncode != 0:
            stderr = b"".join(errors).decode("utf-8", errors="replace")
            raise RuntimeError("BGM合成失敗:\n" + stderr[-5000:])

    def settings_path(self) -> Path:
        """作った動画の隣に置く設定ファイルのパス。"""
        return self.output.with_name(self.output.stem + "_settings.json")

    def write_settings_file(self, encoder_name: str):
        """スライドショーを作ったときの条件を、動画と同じ場所へ保存する。

        あとから「この動画はどの設定で作ったか」を見返せるようにするため。
        書き出しに失敗しても動画作成は成功扱いのままにする。
        """
        names = [p.name for p in self.images]
        data = {
            "app": APP_NAME,
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "output": str(self.output),
            "photos": {
                "count": len(self.images),
                "folder": str(self.images[0].parent) if self.images else "",
                "first": names[0] if names else "",
                "last": names[-1] if names else "",
            },
            # 実際に上映した順番。フォルダーは上に記録してあるのでファイル名だけ。
            "image_order": names,
            "video": {
                "width": self.w,
                "height": self.h,
                "fps": self.fps,
                "interval_seconds": self.interval,
                "transition_seconds": self.transition,
                "zoom_percent": round((self.zoom_end - 1.0) * 100.0, 4),
                "blur_background": self.blur_background,
                "camera_mode": self.camera_mode,
                "encoder_choice": self.encoder_pref,
                "encoder_used": encoder_name,
                "nvenc_detected": self.nvenc_available,
                "total_frames": self.total_frames,
                "total_duration_seconds": self.total_duration,
            },
            "title_card": (asdict(self.title) if self.title else {"enabled": False}),
            "bgm_timing": asdict(self.bgm_timing),
            "bgm_segments": [
                {
                    "start_photo": s_.start_index + 1,
                    "end_photo": s_.end_index + 1,
                    "start_file": names[s_.start_index] if s_.start_index < len(names) else "",
                    "end_file": names[s_.end_index] if s_.end_index < len(names) else "",
                    "audio": str(s_.audio_path),
                }
                for s_ in sorted(self.bgm_segments, key=lambda x: x.start_index)
            ],
        }

        if self.camera_mode in SUBJECT_CAMERA_MODES:
            counts: dict[str, int] = {}
            for det in self.detections.values():
                counts[det.mode] = counts.get(det.mode, 0) + 1
            shown = [
                (i, self.detections[str(pth)])
                for i, pth in enumerate(self.images)
                if str(pth) in self.detections
            ]
            data["subject_camera"] = {
                "analyzed": len(self.detections),
                "by_mode": counts,
                "photos": [
                    {
                        "photo": i + 1,
                        "file": det.filename,
                        "face_count": det.face_count,
                        "dog_count": det.dog_count,
                        "mode": det.mode,
                        "target_x": det.target_x,
                        "target_y": det.target_y,
                    }
                    for i, det in shown
                ],
            }
        if self.camera_mode == CAMERA_SUBJECT_SAFE:
            data["safe_framing"] = self.framing_summary()

        data.update(self.settings_extra)

        try:
            self.settings_path().write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + chr(10),
                encoding="utf-8",
            )
        except Exception as e:
            self.q.put(("warning",
                        "動画はできましたが、設定ファイルを保存できませんでした。" + chr(10)
                        + f"{type(e).__name__}: {e}"))

    def run(self):
        self.output.parent.mkdir(parents=True, exist_ok=True)

        if self.camera_mode in SUBJECT_CAMERA_MODES:
            self.analyze_subjects()
            if self.stop_event.is_set():
                raise InterruptedError("処理を中止しました。")

        with tempfile.TemporaryDirectory(prefix="photomovie_") as td:
            temp_video = Path(td) / "video_only.mp4"
            self.render_silent_video(temp_video)

            if self.stop_event.is_set():
                raise InterruptedError("処理を中止しました。")

            self.mux_bgm(temp_video)

        self.write_settings_file(self.encoder_used)
        self.q.put(("progress", 100.0))
        self.q.put(("done", str(self.output)))


class PhotoOrderDialog(tk.Toplevel):
    """写真の上映順を並べ替えるダイアログ。

    元の写真ファイルには一切触れない。名前の変更も移動もコピーもしない。
    並べ替えるのはアプリが持っている一覧の順番だけ。
    一覧はパスと名前しか持たないので、写真が何百枚あっても軽いまま。
    """

    # 環境によって見え方が変わらないよう、記号はASCIIだけで表す。
    MARK_ON = "[X]"
    MARK_OFF = "[  ]"

    def __init__(self, master, entries: list[tuple[Path, bool]],
                 analysis: dict | None = None, cache: dict | None = None):
        super().__init__(master)
        self.title("写真の順番と使用する写真")
        self.result = None
        self.analysis_result: dict = dict(analysis or {})
        self.iid_path: dict[str, Path] = {}
        self.iid_used: dict[str, bool] = {}
        self._drag_iid = None

        # おすすめ解析用。解析はここで完結し、動画の作り方には影響しない。
        self._rec_cache = cache if cache is not None else {}
        self._rec_queue: Queue = Queue()
        self._rec_stop = threading.Event()
        self._rec_thread = None
        self._star_symbol = True
        self._emb_cache = {}
        self._rec_note = ""
        # モデルがあれば既定でON。無ければ自動的に従来の解析だけになる。
        self.use_embedding_var = tk.BooleanVar(value=self.embedding_available())

        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both", expand=True)

        ttk.Label(
            frm,
            text="上から順に動画へ出てきます。"
                 "行をドラッグするか、選んで「上へ」「下へ」で入れ替えます。" + chr(10)
                 + "「上映」欄をクリック（またはスペースキー）で、"
                   "その写真を使うかどうかを切り替えます。" + chr(10)
                 + "「おすすめ解析」を押すと、どれか1枚を選べばよい写真をまとめて、"
                   "技術的に使いやすそうなものへ星を付けます（選ぶのはご自身です）。" + chr(10)
                 + "「類似」欄の * は、同じ写真の別バージョン"
                   "（カラー版と白黒版など）があるという印です。",
            justify="left",
        ).pack(anchor="w", pady=(0, 8))

        body = ttk.Frame(frm)
        body.pack(fill="both", expand=True)

        # 画面の高さに合わせて行数を決める。小さい画面でもはみ出さない。
        self.update_idletasks()
        rows = max(8, min(20, (self.winfo_screenheight() - 360) // 20))
        self.tree = ttk.Treeview(
            body, columns=("use", "rec", "group", "no", "name"), show="headings",
            height=rows, selectmode="extended"
        )
        self.tree.heading("use", text="上映")
        self.tree.heading("rec", text="推奨")
        self.tree.heading("group", text="類似")
        self.tree.heading("no", text="番号")
        self.tree.heading("name", text="ファイル名")
        self.tree.column("use", width=64, anchor="center", stretch=False)
        self.tree.column("rec", width=76, anchor="center", stretch=False)
        self.tree.column("group", width=56, anchor="center", stretch=False)
        self.tree.column("no", width=56, anchor="e", stretch=False)
        self.tree.column("name", width=380)
        # 使わない写真は灰色にして見分けやすくする
        # 使わない行は薄くする。薄くしすぎると読めないので中間の灰色にする。
        self.tree.tag_configure("off", foreground="#777777")
        self.tree.pack(side="left", fill="both", expand=True)

        bar = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        bar.pack(side="left", fill="y")
        self.tree.configure(yscrollcommand=bar.set)

        side = ttk.Frame(body)
        side.pack(side="left", fill="y", padx=(12, 0))
        self._edit_buttons = []
        up = ttk.Button(side, text="上へ", width=14, command=self.move_up)
        up.pack(pady=(0, 4))
        down = ttk.Button(side, text="下へ", width=14, command=self.move_down)
        down.pack(pady=(0, 4))
        natural = ttk.Button(side, text="自然順に戻す", width=14,
                             command=self.reset_natural)
        natural.pack(pady=(0, 14))
        ttk.Separator(side, orient="horizontal").pack(fill="x", pady=(0, 12))
        all_on = ttk.Button(side, text="すべて選択", width=14,
                            command=lambda: self.set_all(True))
        all_on.pack(pady=(0, 4))
        all_off = ttk.Button(side, text="すべて解除", width=14,
                             command=lambda: self.set_all(False))
        all_off.pack(pady=(0, 4))
        self._edit_buttons += [up, down, natural, all_on, all_off]
        invert = ttk.Button(side, text="選択を反転", width=14, command=self.invert_all)
        invert.pack()
        self._edit_buttons.append(invert)
        ttk.Separator(side, orient="horizontal").pack(fill="x", pady=(12, 10))
        self.rec_btn = ttk.Button(side, text="おすすめ解析", width=14,
                                  command=self.toggle_recommendation)
        self.rec_btn.pack()
        self.emb_check = ttk.Checkbutton(
            side, text="画像embeddingを使用", variable=self.use_embedding_var)
        self.emb_check.pack(pady=(6, 0))
        if not self.embedding_available():
            self.emb_check.config(state="disabled")
        self._edit_buttons.append(self.emb_check)

        self.count_label = ttk.Label(frm, text="")
        self.count_label.pack(anchor="w", pady=(8, 0))

        buttons = ttk.Frame(frm)
        buttons.pack(fill="x", pady=(10, 0))
        ttk.Button(buttons, text="キャンセル", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="この順番で決定", command=self.ok).pack(side="right", padx=(0, 8))

        self.fill(entries)

        # ドラッグ&ドロップ。ttk標準のイベントだけで完結させる。
        self.tree.bind("<ButtonPress-1>", self.on_drag_start)
        self.tree.bind("<B1-Motion>", self.on_drag_motion)
        self.tree.bind("<ButtonRelease-1>", self.on_drag_end)
        self.tree.bind("<space>", self.toggle_selected)

        self.transient(master)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.update_idletasks()
        # 念のため、画面より大きくならないように抑える
        want_w = min(self.winfo_reqwidth(), self.winfo_screenwidth() - 60)
        want_h = min(self.winfo_reqheight(), self.winfo_screenheight() - 80)
        self.geometry(f"{want_w}x{want_h}")
        self.minsize(560, 360)
        self.wait_visibility()
        self.focus_force()

    # ---------------- 一覧 ----------------

    def fill(self, entries: list[tuple[Path, bool]]):
        self.tree.delete(*self.tree.get_children())
        self.iid_path.clear()
        self.iid_used.clear()
        for i, (path, used) in enumerate(entries):
            iid = "p" + str(i)
            self.iid_path[iid] = path
            self.iid_used[iid] = bool(used)
            self.tree.insert("", "end", iid=iid, values=("", "", "", "", path.name))
        self.renumber()

    def current_entries(self) -> list[tuple[Path, bool]]:
        return [(self.iid_path[iid], self.iid_used[iid])
                for iid in self.tree.get_children()]

    def current_order(self) -> list[Path]:
        return [self.iid_path[iid] for iid in self.tree.get_children()]

    def renumber(self):
        """上映番号は「使う写真」だけに振る。使わない写真は - と表示する。"""
        items = self.tree.get_children()
        shown = 0
        for iid in items:
            if self.iid_used[iid]:
                shown += 1
                self.tree.set(iid, "use", self.MARK_ON)
                self.tree.set(iid, "no", shown)
                self.tree.item(iid, tags=())
            else:
                self.tree.set(iid, "use", self.MARK_OFF)
                self.tree.set(iid, "no", "-")
                self.tree.item(iid, tags=("off",))
        self.count_label.config(
            text=f"一覧 {len(items)}枚 / 動画に使う {shown}枚"
                 + ("" if shown else "   ← このままでは動画を作れません")
        )
        self.refresh_recommendation()

    # ---------------- おすすめ解析 ----------------

    def refresh_recommendation(self):
        """推奨・類似の列を、いまの解析結果で描き直す。

        結果は写真のパスで引くので、並べ替えや使用切替の影響を受けない。
        """
        if photo_recommendation is None:
            return
        for iid in self.tree.get_children():
            item = self.analysis_result.get(str(self.iid_path[iid]))
            if item is None:
                self.tree.set(iid, "rec", "")
                self.tree.set(iid, "group", "")
                continue
            self.tree.set(iid, "rec", item.star_text(self._star_symbol))
            # 同じ写真の別バージョン（カラー版と白黒版など）には小さく印を付ける。
            # 列は増やさない。
            mark = "*" if getattr(item, "variant_family", "") else ""
            self.tree.set(iid, "group", (item.group_id or "") + mark)

    def embedding_available(self) -> bool:
        """画像embeddingが使える状態か。モデルが無ければ静かにOFFにする。"""
        if photo_embedding is None:
            return False
        try:
            path = (Path(__file__).resolve().parent
                    / photo_embedding.MODELS_DIR_NAME
                    / photo_embedding.EMBEDDING_MODEL_FILE)
            return path.is_file() and photo_embedding.ort is not None
        except Exception:
            return False

    def toggle_recommendation(self):
        if self._rec_thread is not None and self._rec_thread.is_alive():
            self._rec_stop.set()
            self.rec_btn.config(text="中止しています…", state="disabled")
            return
        self.start_recommendation()

    def set_edit_enabled(self, enabled: bool):
        """解析中は並べ替えや使用切替を触れないようにする。"""
        state = "normal" if enabled else "disabled"
        for widget in self._edit_buttons:
            widget.config(state=state)

    def start_recommendation(self):
        if photo_recommendation is None:
            messagebox.showwarning(
                "おすすめ解析",
                "おすすめ解析のモジュール(photo_recommendation.py)が読み込めません。"
                + chr(10) + "写真の並べ替えと使用の切り替えはそのまま使えます。",
                parent=self,
            )
            return

        paths = self.current_order()
        if not paths:
            return

        detector = None
        if subject_detector is not None:
            try:
                candidate = subject_detector.SubjectDetector()
                if candidate.available:
                    detector = candidate
            except Exception:
                detector = None

        self._rec_stop.clear()
        self._rec_queue = Queue()
        self.set_edit_enabled(False)
        self.rec_btn.config(text="中止", state="normal")

        use_embedding = bool(self.use_embedding_var.get()) and self.embedding_available()

        def work():
            try:
                embeddings = {}
                if use_embedding:
                    extractor = photo_embedding.EmbeddingExtractor()
                    if extractor.available:
                        embeddings = extractor.embed_paths(
                            [p for p in paths if str(p) not in self._emb_cache],
                            progress=lambda d, t: self._rec_queue.put(
                                ("phase", "embedding生成中", d, t)),
                            should_stop=self._rec_stop.is_set,
                        )
                        self._emb_cache.update(embeddings)
                    else:
                        self._rec_queue.put(("note", extractor.unavailable_reason))
                    embeddings = {str(p): self._emb_cache[str(p)]
                                  for p in paths if str(p) in self._emb_cache}
                if self._rec_stop.is_set():
                    self._rec_queue.put(("done", None))
                    return
                items = photo_recommendation.analyze_photos(
                    paths,
                    detector=detector,
                    embeddings=embeddings,
                    progress=lambda d, t, n: self._rec_queue.put(("progress", d, t, n)),
                    phase=lambda label: self._rec_queue.put(("phase", label, 0, 0)),
                    should_stop=self._rec_stop.is_set,
                    cache=self._rec_cache,
                )
                self._rec_queue.put(("done", items))
            except Exception as e:
                self._rec_queue.put(("error", f"{type(e).__name__}: {e}"))

        self._rec_thread = threading.Thread(target=work, daemon=True)
        self._rec_thread.start()
        self.after(80, self.poll_recommendation)

    def poll_recommendation(self):
        try:
            while True:
                item = self._rec_queue.get_nowait()
                if item[0] == "progress":
                    done, total, name = item[1], item[2], item[3]
                    self.count_label.config(
                        text=f"基本特徴量の解析中 {done} / {total}    {name}")
                elif item[0] == "phase":
                    label, done, total = item[1], item[2], item[3]
                    if total:
                        self.count_label.config(text=f"{label} {done} / {total}")
                    else:
                        self.count_label.config(text=label)
                elif item[0] == "note":
                    self._rec_note = item[1]
                elif item[0] == "done":
                    self.finish_recommendation(item[1])
                    return
                elif item[0] == "error":
                    self.finish_recommendation(None, error=item[1])
                    return
        except Empty:
            pass
        if self._rec_thread is not None and self._rec_thread.is_alive():
            self.after(80, self.poll_recommendation)
        else:
            self.finish_recommendation(None)

    def finish_recommendation(self, items, error: str | None = None):
        self._rec_thread = None
        self.set_edit_enabled(True)
        self.rec_btn.config(text="おすすめ解析", state="normal")

        if items:
            # 星が「豆腐」になる環境では記号を諦めて * にする
            self._star_symbol = self._stars_render_ok()
            for a in items:
                self.analysis_result[str(a.path)] = a
        self.renumber()

        if error:
            messagebox.showwarning(
                "おすすめ解析",
                "おすすめ解析を完了できませんでした。" + chr(10) + error
                + chr(10) + "写真の並べ替えと使用の切り替えはそのまま使えます。",
                parent=self,
            )

    def _stars_render_ok(self) -> bool:
        try:
            probe = ttk.Label(self, text=photo_recommendation.STAR_FULL)
            probe.update_idletasks()
            width = probe.winfo_reqwidth()
            probe.destroy()
            return width > 2
        except Exception:
            return False

    # ---------------- 使う / 使わない ----------------

    def toggle(self, iid: str):
        self.iid_used[iid] = not self.iid_used[iid]
        self.renumber()

    def toggle_selected(self, event=None):
        for iid in self.tree.selection():
            self.iid_used[iid] = not self.iid_used[iid]
        self.renumber()
        return "break"

    def set_all(self, used: bool):
        for iid in self.tree.get_children():
            self.iid_used[iid] = used
        self.renumber()

    def invert_all(self):
        for iid in self.tree.get_children():
            self.iid_used[iid] = not self.iid_used[iid]
        self.renumber()

    # ---------------- 並べ替え ----------------

    def shift(self, delta: int):
        items = list(self.tree.get_children())
        selected = set(self.tree.selection())
        if not selected or not items:
            return
        idx = sorted(i for i, iid in enumerate(items) if iid in selected)
        if delta < 0 and idx[0] == 0:
            return
        if delta > 0 and idx[-1] == len(items) - 1:
            return
        for i in (idx if delta < 0 else list(reversed(idx))):
            items[i], items[i + delta] = items[i + delta], items[i]
        for pos, iid in enumerate(items):
            self.tree.move(iid, "", pos)
        self.renumber()
        self.tree.see(items[idx[0] + delta])

    def move_up(self):
        self.shift(-1)

    def move_down(self):
        self.shift(1)

    def reset_natural(self):
        entries = self.current_entries()
        natural = sorted(entries, key=lambda e: natural_key(e[0]))
        if [e[0] for e in entries] == [e[0] for e in natural]:
            return
        if not messagebox.askyesno(
            "写真の順番",
            "今の並べ替えを破棄して、ファイル名順に戻します。よろしいですか？"
            + chr(10) + "（使う・使わないの指定はそのまま残ります）",
            parent=self,
        ):
            return
        self.fill(natural)

    # ---------------- ドラッグ&ドロップ ----------------

    def on_drag_start(self, event):
        self._drag_iid = None
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        # 「上映」欄のクリックは、並べ替えではなく使う/使わないの切り替え
        if self.tree.identify_column(event.x) == "#1":
            self.toggle(iid)
            return
        self._drag_iid = iid

    def on_drag_motion(self, event):
        if not self._drag_iid:
            return
        target = self.tree.identify_row(event.y)
        if target and target != self._drag_iid:
            self.tree.move(self._drag_iid, "", self.tree.index(target))

    def on_drag_end(self, event):
        if self._drag_iid:
            self._drag_iid = None
            self.renumber()

    # ---------------- 決定 ----------------

    def ok(self):
        self.result = self.current_entries()
        self.destroy()


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

        if initial:
            self.start_var.set(image_names[initial.start_index])
            self.end_var.set(image_names[initial.end_index])
            self.audio_var.set(str(initial.audio_path))
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

        note = (
            "曲の入り方・つなぎ方・終わり方は「BGM全体設定」でまとめて指定します。\n"
            "区間の境目では、前の曲がフェードアウトして完全に消えてから、\n"
            "短い無音をはさんで次の曲がフェードインします。"
        )
        ttk.Label(frm, text=note, justify="left").grid(
            row=3, column=0, columnspan=3, sticky="w", pady=(10, 10)
        )

        buttons = ttk.Frame(frm)
        buttons.grid(row=4, column=0, columnspan=3, sticky="e")
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

        self.result = BGMSegment(si, ei, Path(self.audio_var.get()))
        self.destroy()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        # 大きい画面では今までどおりの見やすさ、
        # 小さい画面や表示倍率が高い環境では画面に収まる大きさで開く。
        # 足りないぶんは縦スクロールで届く。
        self.update_idletasks()
        width = min(1020, max(720, self.winfo_screenwidth() - 80))
        height = min(980, max(520, self.winfo_screenheight() - 120))
        self.geometry(f"{width}x{height}")
        self.minsize(720, 460)

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
        # カメラワーク。既定は従来方式。
        self.camera_var = tk.StringVar(value=CAMERA_LEGACY)

        # タイトルカード
        self.title_on_var = tk.BooleanVar(value=True)
        self.title_main_var = tk.StringVar()
        self.title_sub_var = tk.StringVar()
        self.title_date_var = tk.StringVar()
        self.title_dur_var = tk.DoubleVar(value=5.0)
        self.title_fade_var = tk.DoubleVar(value=1.0)
        self.title_text_fade_var = tk.DoubleVar(value=1.5)
        self.title_bg_var = tk.StringVar(value="#FFFFFF")
        self.title_fg_var = tk.StringVar(value="#333333")

        # BGM全体設定（曲ごとではなく全体で1組）
        self.bgm_offset_var = tk.DoubleVar(value=3.0)
        self.bgm_first_fade_var = tk.DoubleVar(value=1.5)
        self.bgm_fadeout_var = tk.DoubleVar(value=1.5)
        self.bgm_gap_var = tk.DoubleVar(value=0.7)
        self.bgm_fadein_var = tk.DoubleVar(value=1.5)
        self.bgm_final_var = tk.DoubleVar(value=3.0)

        # self.images は一覧の全部（並べ替え後の順番）。
        # そのうち「使わない」と指定されたものを excluded に持つ。
        # 動画・BGM番号・被写体解析はすべて shown_images() を基準にする。
        self.images: list[Path] = []
        self.excluded: set[Path] = set()
        # おすすめ解析の結果（写真のパスがキー）。動画の作り方には影響しない。
        self.recommendations: dict = {}
        self.recommendation_cache: dict = {}
        self.bgm_segments: list[BGMSegment] = []

        self.build_ui()
        self.after(120, self.poll_queue)

    def build_ui(self):
        # 画面が小さくても下端の「MP4を作成」まで届くよう、全体を縦スクロールにする。
        # 中身の組み立て方は今までと同じで、置き場所が root になるだけ。
        outer = ttk.Frame(self)
        outer.pack(fill="both", expand=True)

        self.page = tk.Canvas(outer, highlightthickness=0, borderwidth=0)
        vbar = ttk.Scrollbar(outer, orient="vertical", command=self.page.yview)
        self.page.configure(yscrollcommand=vbar.set)
        vbar.pack(side="right", fill="y")
        self.page.pack(side="left", fill="both", expand=True)

        root = ttk.Frame(self.page, padding=14)
        page_window = self.page.create_window((0, 0), window=root, anchor="nw")

        def on_inner(event):
            self.page.configure(scrollregion=self.page.bbox("all"))

        def on_outer(event):
            # 横は常にウィンドウ幅いっぱい（横スクロールはしない）
            self.page.itemconfigure(page_window, width=event.width)

        root.bind("<Configure>", on_inner)
        self.page.bind("<Configure>", on_outer)
        # このウィンドウの中だけでホイールを拾う（別ダイアログには影響しない）
        self.bind("<MouseWheel>", self.on_mousewheel)

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
        ttk.Label(r, text="写真の順番", width=14).pack(side="left")
        self.order_label = ttk.Label(r, text="写真フォルダーを選択してください。")
        self.order_label.pack(side="left", padx=6)
        self.order_btn = ttk.Button(r, text="並べ替え", command=self.open_photo_order)
        self.order_btn.pack(side="right")

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

        cam = ttk.Frame(settings)
        cam.pack(fill="x", pady=(8, 0))
        ttk.Label(cam, text="カメラワーク").pack(side="left", padx=(0, 10))
        ttk.Radiobutton(
            cam, text="従来方式", value=CAMERA_LEGACY,
            variable=self.camera_var, command=self.on_camera_mode_change,
        ).pack(side="left")
        ttk.Radiobutton(
            cam, text="被写体追従（人物・犬）［実験機能］", value=CAMERA_SUBJECT,
            variable=self.camera_var, command=self.on_camera_mode_change,
        ).pack(side="left", padx=(14, 0))
        self.camera_note = ttk.Label(cam, text="")
        self.camera_note.pack(side="left", padx=(14, 0))

        title = ttk.LabelFrame(root, text="タイトルカード", padding=10)
        title.pack(fill="x", pady=(10, 0))

        ttk.Checkbutton(
            title, text="タイトルカードを入れる（動画の先頭に表示します。写真の枚数・番号は変わりません）",
            variable=self.title_on_var,
        ).grid(row=0, column=0, columnspan=6, sticky="w", pady=(0, 6))

        for row, (label, var) in enumerate((
            ("タイトル", self.title_main_var),
            ("サブタイトル", self.title_sub_var),
            ("日付・補助文字", self.title_date_var),
        ), start=1):
            ttk.Label(title, text=label, width=14).grid(row=row, column=0, sticky="w", pady=3)
            ttk.Entry(title, textvariable=var).grid(
                row=row, column=1, columnspan=5, sticky="ew", pady=3
            )

        ttk.Label(title, text="表示時間").grid(row=4, column=0, sticky="w", pady=(6, 0))
        ttk.Spinbox(title, from_=1, to=30, increment=0.5, textvariable=self.title_dur_var,
                    width=7).grid(row=4, column=1, sticky="w", pady=(6, 0))
        ttk.Label(title, text="秒　　タイトル→写真フェード").grid(
            row=4, column=2, sticky="w", pady=(6, 0))
        ttk.Spinbox(title, from_=0, to=5, increment=0.1, textvariable=self.title_fade_var,
                    width=7).grid(row=4, column=3, sticky="w", pady=(6, 0))
        ttk.Label(title, text="秒　　背景色").grid(row=4, column=4, sticky="e", pady=(6, 0))
        ttk.Entry(title, textvariable=self.title_bg_var, width=10).grid(
            row=4, column=5, sticky="w", pady=(6, 0))
        ttk.Label(title, text="文字フェードイン").grid(row=5, column=0, sticky="w", pady=(3, 0))
        ttk.Spinbox(title, from_=0, to=30, increment=0.1, textvariable=self.title_text_fade_var,
                    width=7).grid(row=5, column=1, sticky="w", pady=(3, 0))
        ttk.Label(title, text="秒　　（白背景の上に文字だけが現れます。0で最初から表示）").grid(
            row=5, column=2, columnspan=2, sticky="w", pady=(3, 0))
        ttk.Label(title, text="文字色").grid(row=5, column=4, sticky="e", pady=(3, 0))
        ttk.Entry(title, textvariable=self.title_fg_var, width=10).grid(
            row=5, column=5, sticky="w", pady=(3, 0))
        title.columnconfigure(1, weight=1)

        bgm = ttk.LabelFrame(root, text="BGM", padding=10)
        bgm.pack(fill="both", expand=True, pady=(10, 0))

        timing = ttk.Frame(bgm)
        timing.pack(fill="x", pady=(0, 8))
        ttk.Label(timing, text="全体設定（曲の入り方・つなぎ方・終わり方）").grid(
            row=0, column=0, columnspan=6, sticky="w", pady=(0, 4))
        for col, (label, var) in enumerate((
            ("冒頭オフセット", self.bgm_offset_var),
            ("冒頭フェードイン", self.bgm_first_fade_var),
            ("曲間フェードアウト", self.bgm_fadeout_var),
            ("曲間無音", self.bgm_gap_var),
            ("曲間フェードイン", self.bgm_fadein_var),
            ("最終フェードアウト", self.bgm_final_var),
        )):
            r_, c_ = divmod(col, 3)
            cell = ttk.Frame(timing)
            cell.grid(row=1 + r_, column=c_, sticky="w", padx=(0, 24), pady=2)
            ttk.Label(cell, text=label, width=17).pack(side="left")
            ttk.Spinbox(cell, from_=0, to=10, increment=0.1, textvariable=var,
                        width=7).pack(side="left")
            ttk.Label(cell, text="秒").pack(side="left", padx=(3, 0))

        toolbar = ttk.Frame(bgm)
        toolbar.pack(fill="x", pady=(0, 6))
        ttk.Button(toolbar, text="BGM区間を追加", command=self.add_bgm).pack(side="left")
        ttk.Button(toolbar, text="選択区間を編集", command=self.edit_bgm).pack(side="left", padx=6)
        ttk.Button(toolbar, text="選択区間を削除", command=self.delete_bgm).pack(side="left")
        ttk.Label(
            toolbar,
            text="例：001.jpg～032.jpg = 曲A / 033.jpg～068.jpg = 曲B（区間の指定が無い写真は無音）",
        ).pack(side="right")

        columns = ("start", "end", "music")
        self.tree = ttk.Treeview(bgm, columns=columns, show="headings", height=6)
        self.tree.heading("start", text="開始画像")
        self.tree.heading("end", text="終了画像")
        self.tree.heading("music", text="BGM")
        self.tree.column("start", width=200)
        self.tree.column("end", width=200)
        self.tree.column("music", width=420)
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

    def on_mousewheel(self, event):
        """メイン画面の縦スクロール。

        一覧（Treeview）の上では、そちらが自前でスクロールするので何もしない。
        画面全体が勝手に動いて操作しづらくならないようにするため。
        """
        widget = event.widget
        while widget is not None:
            if isinstance(widget, ttk.Treeview):
                return
            widget = getattr(widget, "master", None)
        if self.page.bbox("all") is None:
            return
        self.page.yview_scroll(-1 if event.delta > 0 else 1, "units")

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

        # 別フォルダーを選び直したら、前のフォルダーの並び順も
        # 使う/使わないの指定も引き継がない。最初は全部を使う。
        self.excluded.clear()
        self.recommendations.clear()
        self.bgm_segments.clear()
        self.refresh_bgm_tree()
        self.update_summary()
        self.update_order_label()

    def update_summary(self):
        shown = self.shown_images()
        n = len(shown)
        if not self.images:
            self.summary.config(text="画像ファイルが見つかりません。")
            return
        if not n:
            self.summary.config(
                text=f"使用する写真がありません（一覧 {len(self.images)}枚はすべて除外中）。")
            return
        title_seconds = (
            float(self.title_dur_var.get()) if self.title_on_var.get() else 0.0
        )
        duration = title_seconds + n * float(self.interval_var.get())
        mins = int(duration // 60)
        secs = int(round(duration % 60))
        head = f"タイトル{title_seconds:g}秒 + " if title_seconds else ""
        skipped = f"（{len(self.excluded)}枚は使いません）" if self.excluded else ""
        self.summary.config(
            text=f"{n}枚{skipped} / 予想動画時間 約 {head}{mins}分{secs:02d}秒 / "
                 f"先頭: {shown[0].name} / 最後: {shown[-1].name}"
        )

    def add_bgm(self):
        if not self.shown_images():
            messagebox.showinfo("BGM", "先に写真フォルダーを選択してください。")
            return
        dlg = BGMDialog(self, [p.name for p in self.shown_images()])
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
        dlg = BGMDialog(self, [p.name for p in self.shown_images()], initial=seg)
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
        shown = self.shown_images()
        for i, s in enumerate(self.bgm_segments):
            # 写真番号は「使う写真だけの上映順」を指す
            start = shown[s.start_index].name if s.start_index < len(shown) else "-"
            end = shown[s.end_index].name if s.end_index < len(shown) else "-"
            self.tree.insert(
                "", "end", iid=str(i),
                values=(start, end, s.audio_path.name)
            )

    def shown_images(self) -> list[Path]:
        """実際に動画へ使う写真だけを、上映順で返す。

        BGM区間の「写真1」「写真2」もこの並びを指す。
        """
        return [p for p in self.images if p not in self.excluded]

    def photo_entries(self) -> list[tuple[Path, bool]]:
        return [(p, p not in self.excluded) for p in self.images]

    def update_order_label(self):
        if not self.images:
            self.order_label.config(text="画像がありません。")
            return
        shown = self.shown_images()
        natural = sorted(self.images, key=natural_key)
        head = "ファイル名順" if self.images == natural else "並べ替え済み"
        if self.excluded:
            self.order_label.config(
                text=f"{head} / 動画に使う {len(shown)}枚"
                     f"（{len(self.excluded)}枚は使いません）")
        else:
            self.order_label.config(text=f"{head}（{len(self.images)}枚）")

    def open_photo_order(self):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo(
                APP_NAME, "動画の作成中は写真の順番を変更できません。")
            return
        if not self.images:
            messagebox.showinfo(APP_NAME, "先に写真フォルダーを選択してください。")
            return
        dlg = PhotoOrderDialog(self, self.photo_entries(),
                               analysis=self.recommendations,
                               cache=self.recommendation_cache)
        self.wait_window(dlg)
        # 解析結果はキャンセルしても残す（写真の選び直しに使えるため）
        self.recommendations = dlg.analysis_result
        if dlg.result is None:
            return
        self.images = [p for p, _ in dlg.result]
        self.excluded = {p for p, used in dlg.result if not used}
        # BGM区間は「上映順の何枚目か」を指す。並べ替え後もその意味のまま。
        # 一覧の表示ファイル名だけ新しい順番に合わせて更新する。
        self.refresh_bgm_tree()
        self.update_summary()
        self.update_order_label()

    def subject_models_missing(self) -> list[str]:
        """モデルファイルの有無だけを見る。重い読み込みはここではしない。"""
        if subject_detector is None:
            return ["subject_detector.py"]
        d = Path(__file__).resolve().parent / subject_detector.MODELS_DIR_NAME
        return [
            name for name in (subject_detector.FACE_MODEL_FILE,
                              subject_detector.DOG_MODEL_FILE)
            if not (d / name).is_file()
        ]

    def on_camera_mode_change(self):
        if self.camera_var.get() != CAMERA_SUBJECT:
            self.camera_note.config(text="")
            return
        missing = self.subject_models_missing()
        if missing:
            self.camera_note.config(text="モデルが見つかりません")
            messagebox.showwarning(
                APP_NAME,
                "人物・犬検出モデルが見つかりません。" + chr(10)
                + "次のファイルを models フォルダーへ置いてください:" + chr(10)
                + chr(10).join("  " + m for m in missing) + chr(10) + chr(10)
                + "従来カメラワークはそのまま使用できます。",
            )
            self.camera_var.set(CAMERA_LEGACY)
            return
        self.camera_note.config(
            text="写真ごとに被写体を1回だけ解析します（PC内で完結）")

    def recommendation_settings_entry(self) -> dict:
        """おすすめ解析を実行していれば、その結果を設定ファイルへ残す。

        実行していなければ何も足さないので、これまでの設定ファイルと互換。
        絶対パスは残さず、ファイル名だけにする。
        """
        if not self.recommendations:
            return {}
        listed = {str(p) for p in self.images}
        data = {}
        for key, item in self.recommendations.items():
            if key not in listed:
                continue
            data[item.filename] = {
                "group": item.group_id,
                "variant_family": getattr(item, "variant_family", "") or None,
                "stars": item.stars,
                "technical_score": round(item.technical_score, 1),
                "group_rule": item.group_rule or None,
            }
        if not data:
            return {}
        engine = {
            "version": 2,
            "embedding_enabled": any(
                getattr(i, "embedding", None) is not None
                for i in self.recommendations.values()
            ),
        }
        engine["grouping"] = ("selection_candidate_groups"
                              if engine["embedding_enabled"] else "legacy")
        if engine["embedding_enabled"] and photo_embedding is not None:
            engine["embedding_model"] = photo_embedding.MODEL_ID
            engine["embedding_revision"] = photo_embedding.MODEL_REVISION
            engine["embedding_dimension"] = photo_embedding.EMBEDDING_DIMENSION
        return {"photo_recommendations": data, "recommendation_engine": engine}

    def title_card(self) -> TitleCard:
        return TitleCard(
            enabled=bool(self.title_on_var.get()),
            main=self.title_main_var.get(),
            sub=self.title_sub_var.get(),
            date=self.title_date_var.get(),
            duration=float(self.title_dur_var.get()),
            fade_seconds=float(self.title_fade_var.get()),
            text_fade_seconds=float(self.title_text_fade_var.get()),
            bg_color=parse_color(self.title_bg_var.get(), "#FFFFFF"),
            fg_color=parse_color(self.title_fg_var.get(), "#333333"),
        )

    def bgm_timing(self) -> BGMTiming:
        return BGMTiming(
            first_offset=float(self.bgm_offset_var.get()),
            first_fade_in=float(self.bgm_first_fade_var.get()),
            fade_out=float(self.bgm_fadeout_var.get()),
            silence_gap=float(self.bgm_gap_var.get()),
            fade_in=float(self.bgm_fadein_var.get()),
            final_fade_out=float(self.bgm_final_var.get()),
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
        if not self.shown_images():
            raise ValueError(
                "使用する写真がありません。" + chr(10)
                + "「写真の順番」の並べ替え画面で、動画に使う写真を選んでください。"
            )
        if not self.output_var.get():
            raise ValueError("出力MP4を指定してください。")
        if float(self.interval_var.get()) <= 0:
            raise ValueError("写真の間隔は0より大きくしてください。")
        if float(self.transition_var.get()) >= float(self.interval_var.get()):
            raise ValueError("写真クロスフェードは写真の間隔より短くしてください。")
        if self.title_on_var.get():
            if float(self.title_dur_var.get()) <= 0:
                raise ValueError("タイトルカードの表示時間は0より大きくしてください。")
            if float(self.title_fade_var.get()) > float(self.title_dur_var.get()):
                raise ValueError(
                    "タイトル→写真フェードは、タイトルの表示時間以下にしてください。"
                )
            if float(self.title_text_fade_var.get()) > float(self.title_dur_var.get()):
                raise ValueError(
                    "文字フェードインは、タイトルの表示時間以下にしてください。"
                )
            for label, value in (("背景色", self.title_bg_var.get()),
                                 ("文字色", self.title_fg_var.get())):
                try:
                    ImageColor.getrgb(value.strip())
                except Exception:
                    raise ValueError(
                        "タイトルカードの" + label + "が読めません。"
                        " #FFFFFF のような形式で指定してください。"
                    )

    def start(self):
        try:
            self.validate()
        except Exception as e:
            messagebox.showerror(APP_NAME, str(e))
            return

        self.stop_event.clear()
        self.start_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.order_btn.config(state="disabled")
        self.progress["value"] = 0

        w, h = [int(x) for x in self.resolution_var.get().split("x")]

        renderer = VideoRenderer(
            image_paths=self.shown_images(),
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
            title=self.title_card(),
            bgm_timing=self.bgm_timing(),
            camera_mode=self.camera_var.get(),
            settings_extra={
                # 一覧の全部と、それぞれを使ったかどうか。パスは残さない。
                "images": [
                    {"filename": p.name, "included": p not in self.excluded}
                    for p in self.images
                ],
                **self.recommendation_settings_entry(),
                "gui": {
                    "photo_folder": self.folder_var.get(),
                    "encoder_label": self.encoder_var.get(),
                    "resolution_label": self.resolution_var.get(),
                    "camera_label": ("被写体追従（人物・犬）"
                                     if self.camera_var.get() == CAMERA_SUBJECT
                                     else "従来方式"),
                }
            },
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
                    self.order_btn.config(state="normal")
                    self.status.config(text="完成しました。")
                    settings = Path(item[1]).with_name(
                        Path(item[1]).stem + "_settings.json")
                    extra = ("" if not settings.is_file() else
                             chr(10) + chr(10) + "このときの設定も保存しました:"
                             + chr(10) + settings.name)
                    messagebox.showinfo(
                        APP_NAME,
                        "完成しました。" + chr(10) + chr(10) + item[1] + extra)

                elif kind == "warning":
                    # 被写体追従が使えない等。動画作成は従来方式で続行する。
                    self.status.config(text=item[1].splitlines()[0])
                    messagebox.showwarning(APP_NAME, item[1])

                elif kind == "cancelled":
                    self.start_btn.config(state="normal")
                    self.cancel_btn.config(state="disabled")
                    self.order_btn.config(state="normal")
                    self.status.config(text="中止しました。")

                elif kind == "error":
                    self.start_btn.config(state="normal")
                    self.cancel_btn.config(state="disabled")
                    self.order_btn.config(state="normal")
                    self.status.config(text="エラー")
                    messagebox.showerror(APP_NAME, item[1][-7000:])

        except Empty:
            pass

        self.after(120, self.poll_queue)


if __name__ == "__main__":
    App().mainloop()
