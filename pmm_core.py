# -*- coding: utf-8 -*-
"""PhotoMovieMaker を画面以外から使うための入口（Core API）。

GUI・CLI・（ToolDock 経由の）MCP が同じ処理を呼べるようにする薄い層です。
Tk の画面は一切作りません。

守ること:
- 元の写真は読むだけ。削除・移動・名前の変更・上書きは一切しない
- 指定されたフォルダーの直下だけを見る（サブフォルダーやドライブ全体を探さない）
- 外部との通信はしない。モデルも実行時にダウンロードしない

何を作るか・どの写真を使うかは呼び出す側（人や AI）が決めます。
ここは、決まったことを確実に実行するための部品だけを提供します。
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty, Queue

import numpy as np
from PIL import Image

# 画面の部品（Tk）はここでは作らない。定数と描画用の設定クラスだけを借りる。
import PhotoMovieMaker_GPU as app
import photo_recommendation as pr

try:
    import photo_embedding
except Exception:        # onnxruntime が無くても、それ以外は動く
    photo_embedding = None

try:
    import subject_detector
except Exception:
    subject_detector = None


API_VERSION = 1
PROJECT_KIND = "photomoviemaker.project"
PROJECT_SCHEMA_VERSION = 1
PROJECT_SUFFIX = ".photomovie.json"
TOOL_VERSION = "0.1.1+external-api"

# GUI の既定値と同じにしておく（PhotoMovieMaker_GPU.App の初期値）
DEFAULT_VIDEO = {
    "width": 1920,
    "height": 1080,
    "fps": 30,
    "interval_seconds": 8.0,
    "transition_seconds": 1.0,
    "zoom_percent": 8.0,
    "blur_background": True,
    "camera_mode": app.CAMERA_LEGACY,
    "encoder_choice": "auto",
}
ENCODER_CHOICES = {"auto", "nvenc", "cpu"}
CAMERA_MODES = {app.CAMERA_LEGACY, app.CAMERA_SUBJECT}
MEDIA_TYPES = {"image", "video"}        # video は将来用（今はまだ描画できない）
# 曲の形式は、画面の曲選択（AUDIO_FILETYPES の絞り込み）と同じものだけ
AUDIO_SUFFIXES = frozenset(pattern[1:].lower() for pattern in app.AUDIO_FILETYPES[0][1].split())
# Project JSON の BGM 区間の項目。*_settings.json の bgm_segments と同じ名前（audio はファイル名だけ）
SEGMENT_KEYS = {"start_photo", "end_photo", "audio", "start_file", "end_file"}


class CoreError(Exception):
    """呼び出し側へそのまま返せるエラー。code は機械処理用。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class CancelledError(CoreError):
    def __init__(self):
        super().__init__("cancelled", "処理を中止しました。")


# ------------------------------------------------------------------
# フォルダーと写真
# ------------------------------------------------------------------

def resolve_folder(folder) -> Path:
    """写真フォルダーを確かめて、絶対パスで返す。

    ネットワーク上の場所・ドライブ全体・存在しない場所は受け付けない。
    """
    text = str(folder or "").strip().strip('"')
    if not text:
        raise CoreError("invalid_folder", "フォルダーを指定してください。")
    if text.startswith(("\\\\", "//")):
        raise CoreError("invalid_folder", "ネットワーク上のフォルダーは対象外です。")
    path = Path(os.path.abspath(os.path.expandvars(text)))
    if path == Path(path.anchor):
        raise CoreError("invalid_folder", "ドライブ全体ではなく、写真の入ったフォルダーを指定してください。")
    if not path.exists():
        raise CoreError("invalid_folder", f"フォルダーが見つかりません: {path}")
    if not path.is_dir():
        raise CoreError("invalid_folder", f"フォルダーではありません: {path}")
    return path


def list_images(folder: Path) -> list[Path]:
    """フォルダー直下の写真を、GUI と同じ順番（番号の自然順）で返す。"""
    try:
        entries = list(folder.iterdir())
    except OSError as e:
        raise CoreError("unreadable_folder", f"フォルダーを読み取れません: {e}") from None
    images = [p for p in entries
              if p.is_file() and p.suffix.lower() in app.SUPPORTED_IMAGES]
    return sorted(images, key=app.natural_key)


def _image_size(path: Path) -> tuple[int | None, int | None]:
    """見たときの向き（EXIF の回転を考慮）での幅と高さ。画素は読み込まない。"""
    try:
        with Image.open(path) as im:
            w, h = im.size
            orientation = im.getexif().get(0x0112, 1)
    except Exception:
        return None, None
    if orientation in (5, 6, 7, 8):
        w, h = h, w
    return w, h


def _iso(ts: float | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


# ------------------------------------------------------------------
# 公開する操作
# ------------------------------------------------------------------

def get_capabilities() -> dict:
    """このツールで何ができるかを、機械で読める形で返す。"""
    embedding_ready = False
    if photo_embedding is not None and getattr(photo_embedding, "ort", None) is not None:
        model = Path(__file__).resolve().parent / photo_embedding.MODELS_DIR_NAME \
            / photo_embedding.EMBEDDING_MODEL_FILE
        embedding_ready = model.is_file()
    detector_ready = False
    if subject_detector is not None:
        models = Path(__file__).resolve().parent / "models"
        detector_ready = models.is_dir() and any(models.glob("*.onnx"))

    return {
        "tool": "PhotoMovieMaker_GPU",
        "name": app.APP_NAME,
        "version": TOOL_VERSION,
        "api_version": API_VERSION,
        "capabilities": [
            "media_scan",
            "photo_analysis",
            "photo_recommendation",
            "variant_family",
            "candidate_group",
            "subject_detection",
            "project_plan",
            "render_final",
            "music_scan",
            "music_analysis",
            "project_compose",
            "timeline_preview",
            "director_record",
        ],
        "planned": ["render_preview", "transition_primitives",
                    "video_clips", "beat_aligned_plan"],
        # render は Project JSON の title_card・camera_mode（被写体追従）・BGM 区間を使う。
        # 曲は呼び出し側が明示した曲のフォルダー（music_folder）の直下だけ。
        "gui_only": [],
        "media": {"image_extensions": sorted(app.SUPPORTED_IMAGES),
                  "video_extensions": [], "audio_extensions": sorted(AUDIO_SUFFIXES),
                  "recursive": False},
        "models": {"embedding": embedding_ready, "subject_detection": detector_ready},
        "project": {"kind": PROJECT_KIND, "schema_version": PROJECT_SCHEMA_VERSION,
                    "file_suffix": PROJECT_SUFFIX},
        "policy": {"source_media": "read_only", "network": "none",
                   "runtime_download": False},
    }


def scan_media(folder) -> dict:
    """フォルダー直下の写真を一覧にする。中身の解析はしない。"""
    root = resolve_folder(folder)
    images = list_images(root)
    media = []
    for p in images:
        st = p.stat()
        w, h = _image_size(p)
        media.append({
            "file": p.name,
            "type": "image",
            "bytes": st.st_size,
            "modified": _iso(st.st_mtime),
            "width": w,
            "height": h,
        })
    others = 0
    try:
        others = sum(1 for p in root.iterdir() if p.is_file()) - len(images)
    except OSError:
        pass
    return {"folder": str(root), "count": len(media), "media": media,
            "skipped_files": max(0, others)}


def _engine_info(items) -> dict:
    enabled = any(a.embedding is not None for a in items)
    engine = {
        "version": 2,
        "embedding_enabled": enabled,
        "grouping": "selection_candidate_groups" if enabled else "legacy",
    }
    if enabled and photo_embedding is not None:
        engine["embedding_model"] = photo_embedding.MODEL_ID
        engine["embedding_revision"] = photo_embedding.MODEL_REVISION
        engine["embedding_dimension"] = photo_embedding.EMBEDDING_DIMENSION
    return engine


def analyze_photos(folder, *, use_embedding: bool = True, detect_subjects: bool = True,
                   progress=None, should_stop=None) -> dict:
    """おすすめ解析を行い、結果を JSON にできる形で返す。

    GUI の「おすすめ解析」と同じ処理です。写真は読むだけで、何も書き換えません。
    progress(phase, done, total) が呼ばれます。
    should_stop() が True を返したら中止し、CancelledError を送ります。
    """
    root = resolve_folder(folder)
    paths = list_images(root)
    stop = should_stop or (lambda: False)
    notes: list[str] = []

    detector = None
    if detect_subjects and subject_detector is not None:
        try:
            d = subject_detector.SubjectDetector()
            if d.available:
                detector = d
            else:
                notes.append("人物・犬の検出モデルが無いため、検出は行いませんでした。")
        except Exception as e:
            notes.append(f"人物・犬の検出を使えませんでした: {type(e).__name__}")

    embeddings = {}
    if use_embedding:
        if photo_embedding is None:
            notes.append("onnxruntime が無いため、画像embeddingは使いませんでした。")
        else:
            extractor = photo_embedding.EmbeddingExtractor()
            if extractor.available:
                embeddings = extractor.embed_paths(
                    paths,
                    progress=(lambda d, t: progress("embedding", d, t)) if progress else None,
                    should_stop=stop,
                )
            else:
                notes.append(extractor.unavailable_reason.split(chr(10))[0])
    if stop():
        raise CancelledError()

    items = pr.analyze_photos(
        paths, detector=detector, embeddings=embeddings,
        progress=(lambda d, t, n: progress("analysis", d, t)) if progress else None,
        should_stop=stop,
    )
    if stop() or (paths and not items):
        raise CancelledError()

    photos = []
    groups: dict[str, list[str]] = {}
    for a in items:
        if a.group_id:
            groups.setdefault(a.group_id, []).append(a.filename)
        photos.append({
            "file": a.filename,
            "candidate_group": a.group_id,
            "group_size": a.group_size,
            "variant_family": a.variant_family or None,
            "variant_role": a.variant_role or None,
            "stars": a.stars or None,
            "technical_score": round(a.technical_score, 1),
            "recommendation_note": a.note or None,
            "width": a.width or None,
            "height": a.height or None,
            "taken_at": _iso(a.taken_at),
            "face_count": a.face_count,
            "dog_count": a.dog_count,
        })
    stars = {s: sum(1 for p in photos if p["stars"] == s) for s in (1, 2, 3)}
    return {
        "folder": str(root),
        "count": len(photos),
        "engine": _engine_info(items),
        "summary": {
            "candidate_groups": len(groups),
            "singletons": sum(1 for p in photos if not p["candidate_group"]),
            "variant_families": len({p["variant_family"] for p in photos
                                     if p["variant_family"]}),
            "stars": {"3": stars[3], "2": stars[2], "1": stars[1]},
        },
        "groups": [{"id": g, "files": files} for g, files in sorted(groups.items())],
        "photos": photos,
        "notes": notes,
        "meaning": {
            "candidate_group": "どれか1枚を選べば、残りは外してもよいほど互いに代わりになる写真の群",
            "variant_family": "同じ1枚の写真の別バージョン（カラー版と白黒版、コピーなど）",
            "stars": "同じ群の中での推奨度。写真の価値や良し悪しではない",
        },
    }


# ------------------------------------------------------------------
# 曲（BGM の候補）: 一覧と技術的な測定。曲は読むだけ
# ------------------------------------------------------------------

MAX_TRACKS = 30
ANALYSIS_RATE = 8000        # 音量の推移を測るために mono へまとめるときのサンプルレート
SILENCE_DB = -50.0          # 1 秒の RMS がこれ未満なら「無音」
QUIET_BELOW_DB = 18.0       # 曲全体の RMS よりこれだけ小さい 1 秒が 3 秒以上続けば「静かな区間」


def _run_tool(command: list[str], *, timeout: float = 300, binary: bool = False):
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        return subprocess.run(command, capture_output=True, text=not binary, timeout=timeout,
                              creationflags=flags, **({} if binary else {"encoding": "utf-8", "errors": "replace"}))
    except subprocess.TimeoutExpired:
        raise CoreError("analysis_failed", f"{timeout:.0f} 秒以内に終わりませんでした。") from None
    except OSError as e:
        raise CoreError("analysis_failed", f"FFmpeg を起動できません: {e}") from None


def find_ffprobe() -> str | None:
    """ffprobe（PATH のもの、無ければ画面と同じ FFmpeg の隣）。無ければ None。"""
    found = shutil.which("ffprobe")
    if found:
        return found
    beside = Path(app.find_ffmpeg()).with_name("ffprobe" + (".exe" if os.name == "nt" else ""))
    return str(beside) if beside.is_file() else None


def list_tracks(folder: Path) -> list[Path]:
    """フォルダー直下の曲を、写真と同じ番号の自然順で返す（形式は画面の曲選択と同じ）。"""
    try:
        entries = list(folder.iterdir())
    except OSError as e:
        raise CoreError("unreadable_folder", f"フォルダーを読み取れません: {e}") from None
    return sorted((p for p in entries if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES),
                  key=app.natural_key)


def scan_music(folder) -> dict:
    """曲のフォルダー直下の曲を一覧にする。中身は読まない。"""
    root = resolve_folder(folder)
    tracks = list_tracks(root)
    others = 0
    try:
        others = sum(1 for p in root.iterdir() if p.is_file()) - len(tracks)
    except OSError:
        pass
    return {"folder": str(root), "count": len(tracks),
            "tracks": [{"file": p.name, "format": p.suffix.lower()[1:], "bytes": p.stat().st_size,
                        "modified": _iso(p.stat().st_mtime)} for p in tracks],
            "skipped_files": max(0, others), "formats": sorted(AUDIO_SUFFIXES)}


def _probe_track(ffprobe: str | None, path: Path) -> dict:
    if not ffprobe:
        return {}
    r = _run_tool([ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
                   "-select_streams", "a:0", str(path)], timeout=60)
    try:
        data = json.loads(r.stdout or "{}")
    except ValueError:
        return {}
    fmt, streams = data.get("format") or {}, data.get("streams") or []
    stream = streams[0] if streams else {}
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}

    def number(value, kind=float):
        try:
            return kind(value)
        except (TypeError, ValueError):
            return None

    return {
        "container": fmt.get("format_name"),
        "codec": stream.get("codec_name"),
        "sample_rate": number(stream.get("sample_rate"), int),
        "channels": number(stream.get("channels"), int),
        "channel_layout": stream.get("channel_layout"),
        "bit_rate": number(fmt.get("bit_rate"), int),
        "probe_duration_seconds": number(fmt.get("duration")),
        "tags": {k: str(tags[k])[:200] for k in ("title", "artist", "album", "genre", "date") if k in tags},
    }


def _loudness(ffmpeg: str, path: Path) -> dict:
    """EBU R128（FFmpeg の ebur128）: 統合ラウドネス・ラウドネスレンジ・トゥルーピーク。"""
    r = _run_tool([ffmpeg, "-hide_banner", "-nostats", "-i", str(path), "-vn",
                   "-af", "ebur128=peak=true", "-f", "null", "-"])
    text = (r.stderr or "")
    summary = text[text.rfind("Summary:"):] if "Summary:" in text else ""

    def pick(label):
        m = re.search(label + r":\s*(-?[\d.]+|-inf)", summary)
        if not m:
            return None
        return None if m.group(1) == "-inf" else round(float(m.group(1)), 1)

    return {"integrated_lufs": pick("I"), "loudness_range_lu": pick("LRA"), "true_peak_dbfs": pick("Peak")}


def _decode_mono(ffmpeg: str, path: Path):
    r = _run_tool([ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(path), "-vn", "-ac", "1",
                   "-ar", str(ANALYSIS_RATE), "-f", "f32le", "-"], binary=True)
    if r.returncode != 0:
        raise CoreError("analysis_failed", f"{path.name}: 曲を読めませんでした。")
    return np.frombuffer(r.stdout, dtype="<f4")


def _db(value: float) -> float | None:
    return round(20 * math.log10(value), 1) if value > 1e-10 else None


def _runs(flags, minimum: int) -> list[dict]:
    found, start = [], None
    for i, flag in enumerate(list(flags) + [False]):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            if i - start >= minimum:
                found.append({"start_seconds": start, "end_seconds": i})
            start = None
    return found


def _envelope(samples) -> dict:
    seconds = int(len(samples) // ANALYSIS_RATE)
    if seconds == 0:
        return {"per_second_rms_dbfs": [], "peak_dbfs": _db(float(np.max(np.abs(samples)))) if len(samples) else None,
                "rms_dbfs": None, "silences": [], "quiet_sections": [], "leading_silence_seconds": 0,
                "trailing_silence_seconds": 0, "energy_profile": []}
    frames = samples[:seconds * ANALYSIS_RATE].reshape(seconds, ANALYSIS_RATE).astype(np.float64)
    rms = np.sqrt(np.mean(frames * frames, axis=1))
    db = [(_db(float(v)) if v > 1e-10 else -120.0) for v in rms]
    overall = _db(float(np.sqrt(np.mean(samples.astype(np.float64) ** 2))))
    silent = [d < SILENCE_DB for d in db]
    lead = next((i for i, s in enumerate(silent) if not s), seconds)
    trail = next((i for i, s in enumerate(reversed(silent)) if not s), seconds)
    quiet = [d < (overall - QUIET_BELOW_DB) for d in db] if overall is not None else [False] * seconds
    bins = min(10, seconds)
    profile = []
    for k in range(bins):
        a, b = k * seconds // bins, (k + 1) * seconds // bins
        chunk = rms[a:b]
        profile.append({"start_seconds": a, "end_seconds": b,
                        "rms_dbfs": _db(float(np.sqrt(np.mean(chunk * chunk)))) if len(chunk) else None})
    return {"per_second_rms_dbfs": db, "rms_dbfs": overall,
            "peak_dbfs": _db(float(np.max(np.abs(samples)))),
            "silences": _runs(silent, 1), "quiet_sections": _runs(quiet, 3),
            "leading_silence_seconds": lead, "trailing_silence_seconds": trail,
            "energy_profile": profile}


def analyze_music(folder, *, progress=None, should_stop=None) -> dict:
    """曲のフォルダー直下の曲を、技術的に測れることだけ測る。曲は読むだけ。

    測る: 長さ・形式・サンプルレート・チャンネル・ビットレート・ファイルのメタデータ（書かれていれば）・
    統合ラウドネス・トゥルーピーク・1 秒ごとの音量の推移・無音・静かな区間・10 区分の音量の概形。
    測らない（unknown）: テンポ（BPM）・拍・曲の雰囲気。雰囲気の推定は呼び出し側（SI）の判断で、事実ではない。
    """
    root = resolve_folder(folder)
    tracks = list_tracks(root)
    if len(tracks) > MAX_TRACKS:
        raise CoreError("too_many_tracks", f"曲は {MAX_TRACKS} 曲までにしてください（{len(tracks)} 曲あります）。")
    stop = should_stop or (lambda: False)
    ffmpeg, ffprobe = app.find_ffmpeg(), find_ffprobe()
    results = []
    for index, path in enumerate(tracks, 1):
        if stop():
            raise CancelledError()
        if progress:
            progress("music", index - 1, len(tracks))
        samples = _decode_mono(ffmpeg, path)
        entry = {"file": path.name, "format": path.suffix.lower()[1:],
                 "duration_seconds": round(len(samples) / ANALYSIS_RATE, 2)}
        entry.update(_probe_track(ffprobe, path))
        entry["loudness"] = _loudness(ffmpeg, path)
        entry.update(_envelope(samples))
        entry["tempo"] = {"bpm": None, "beats": None, "status": "unknown",
                          "reason": "テンポと拍を測る仕組みはまだ無い（依存を増やさないため）"}
        results.append(entry)
    if progress:
        progress("music", len(tracks), len(tracks))
    return {
        "folder": str(root), "count": len(results), "tracks": results,
        "method": {"decode": f"FFmpeg で mono {ANALYSIS_RATE} Hz にまとめて 1 秒ごとの RMS を計算",
                   "loudness": "FFmpeg ebur128（EBU R128）", "silence_threshold_dbfs": SILENCE_DB,
                   "quiet_below_track_rms_db": QUIET_BELOW_DB, "ffprobe": bool(ffprobe)},
        "unknown": ["tempo_bpm", "beats", "mood"],
        "notes": ["値は技術的な測定。明るい・穏やか・懐かしいなどの雰囲気は測っていない（推定するなら推定として扱う）",
                  "曲は必ず先頭から使われ、短い曲は区間の長さまで繰り返される（画面の BGM と同じ）"],
    }


def create_project_plan(folder, *, title: str | None = None, analysis: dict | None = None,
                        analyze: bool = False, use_embedding: bool = True,
                        detect_subjects: bool = True,
                        target_duration_seconds: float | None = None,
                        progress=None, should_stop=None) -> dict:
    """Project JSON（上映計画）の叩き台を作る。

    写真はすべて「使う」のまま、フォルダーの並び順で並べる。
    どれを外すか・どの順に並べるかは呼び出す側が決めて、この JSON を書き換えて渡す。
    ここでは勝手に写真を外したり並べ替えたりしない。
    """
    root = resolve_folder(folder)
    if analysis is None and analyze:
        analysis = analyze_photos(root, use_embedding=use_embedding,
                                  detect_subjects=detect_subjects,
                                  progress=progress, should_stop=should_stop)
    by_file = {}
    if analysis:
        if Path(str(analysis.get("folder", ""))) != root:
            raise CoreError("analysis_mismatch", "解析結果が別のフォルダーのものです。")
        by_file = {p["file"]: p for p in analysis.get("photos", [])}

    media = []
    for order, p in enumerate(list_images(root), 1):
        info = by_file.get(p.name, {})
        media.append({
            "file": p.name,
            "type": "image",
            "enabled": True,
            "order": order,
            "candidate_group": info.get("candidate_group"),
            "variant_family": info.get("variant_family"),
            "stars": info.get("stars"),
        })

    title_card = asdict(app.TitleCard())
    title_card["main"] = title if title is not None else root.name

    project = {
        "kind": PROJECT_KIND,
        "schema_version": PROJECT_SCHEMA_VERSION,
        "created_by": {"app": app.APP_NAME, "version": TOOL_VERSION,
                       "api_version": API_VERSION},
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "project": {
            "title": title_card["main"],
            "source_folder": str(root),
            "target_duration_seconds": target_duration_seconds,
            # BGM を使うときだけ、曲のフォルダー（書き出しのときに同じものを渡す）
            "music_folder": None,
        },
        "media": media,
        # 以下の区画名と中身は、動画を作ったときに書き出す *_settings.json と同じ。
        "video": dict(DEFAULT_VIDEO),
        "title_card": title_card,
        "bgm_timing": asdict(app.BGMTiming()),
        "bgm_segments": [],
        "recommendation_engine": (analysis or {}).get("engine"),
        # 将来の演出（トランジション・拍に合わせた計画・動画の差し込みなど）用の置き場
        "extensions": {},
    }
    problems = validate_project(project)
    if problems:
        raise CoreError("invalid_project", "内部エラー: " + " / ".join(problems))
    return project


# ------------------------------------------------------------------
# Project JSON の確認と保存
# ------------------------------------------------------------------

def _number(value, lo, hi) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and lo <= value <= hi


def _plain_name(name) -> bool:
    """フォルダー直下のファイル名だけか（区切り文字・ドライブ・.. を含まない）。"""
    return (isinstance(name, str) and bool(name) and name not in {".", ".."}
            and not any(c in name for c in "\\/:") and name == name.strip())


def _position(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def shown_files(project: dict) -> list[str]:
    """使う写真（enabled）を上映順に並べたファイル名。BGM 区間の「何枚目」はこの並び。"""
    media = [m for m in project.get("media") or [] if isinstance(m, dict) and m.get("enabled") is True
             and _position(m.get("order"))]
    return [m["file"] for m in sorted(media, key=lambda m: m["order"])]


def _validate_segments(project: dict, problems: list[str]) -> None:
    segments = project.get("bgm_segments")
    if not isinstance(segments, list):
        problems.append("bgm_segments は配列である必要があります。")
        return
    info = project.get("project") if isinstance(project.get("project"), dict) else {}
    music = info.get("music_folder")
    if music is not None and (not isinstance(music, str) or not os.path.isabs(music)):
        problems.append("project.music_folder は曲のフォルダーの絶対パスです。")
    if not segments:
        return
    if not isinstance(music, str) or not music:
        problems.append("bgm_segments があるときは project.music_folder（曲のフォルダーの絶対パス）が必要です。")
    shown = shown_files(project)
    spans = []
    for i, s in enumerate(segments, 1):
        where = f"bgm_segments[{i}]"
        if not isinstance(s, dict) or set(s) - SEGMENT_KEYS:
            problems.append(f"{where} の項目が不正です（使える項目: {sorted(SEGMENT_KEYS)}）。")
            continue
        audio = s.get("audio")
        if not _plain_name(audio):
            problems.append(f"{where}.audio は曲のフォルダーの中のファイル名だけを指定してください。")
        elif Path(audio).suffix.lower() not in AUDIO_SUFFIXES:
            problems.append(f"{where}.audio の形式には対応していません（{' '.join(sorted(AUDIO_SUFFIXES))}）。")
        first, last = s.get("start_photo"), s.get("end_photo")
        if not _position(first) or not _position(last):
            problems.append(f"{where}.start_photo / end_photo は 1 以上の整数（使う写真の上映順で何枚目か）です。")
            continue
        if first > last:
            problems.append(f"{where}: start_photo は end_photo 以下にしてください。")
            continue
        if last > len(shown):
            problems.append(f"{where}: 使う写真は {len(shown)} 枚です（end_photo {last}）。")
            continue
        for key, position in (("start_file", first), ("end_file", last)):
            if key in s and s[key] != shown[position - 1]:
                problems.append(f"{where}.{key} が {position} 枚目の写真（{shown[position - 1]}）と違います。")
        spans.append((first, last, i))
    spans.sort()
    for (a_first, a_last, a), (b_first, _b_last, b) in zip(spans, spans[1:]):
        if b_first <= a_last:
            problems.append(f"bgm_segments[{a}] と bgm_segments[{b}] の写真の範囲が重なっています。")


def validate_project(project) -> list[str]:
    """Project JSON を確かめて、問題点の一覧を返す（空なら問題なし）。"""
    problems: list[str] = []
    if not isinstance(project, dict):
        return ["Project JSON がオブジェクトではありません。"]
    if project.get("kind") != PROJECT_KIND:
        problems.append(f"kind は {PROJECT_KIND} である必要があります。")
    if project.get("schema_version") != PROJECT_SCHEMA_VERSION:
        problems.append(f"schema_version は {PROJECT_SCHEMA_VERSION} だけに対応しています。")

    info = project.get("project")
    if not isinstance(info, dict) or not isinstance(info.get("source_folder"), str) \
            or not info.get("source_folder"):
        problems.append("project.source_folder が必要です。")
    elif not os.path.isabs(info["source_folder"]):
        problems.append("project.source_folder は絶対パスである必要があります。")
    if isinstance(info, dict):
        dur = info.get("target_duration_seconds")
        if dur is not None and not _number(dur, 1, 24 * 3600):
            problems.append("project.target_duration_seconds が範囲外です。")

    media = project.get("media")
    if not isinstance(media, list):
        problems.append("media は配列である必要があります。")
        media = []
    orders, files = set(), set()
    for i, m in enumerate(media, 1):
        if not isinstance(m, dict):
            problems.append(f"media[{i}] がオブジェクトではありません。")
            continue
        name = m.get("file")
        # ファイル名だけを許す。フォルダーの外を指すものは受け付けない。
        if not _plain_name(name):
            problems.append(f"media[{i}].file はファイル名だけを指定してください。")
        elif name in files:
            problems.append(f"media[{i}].file が重複しています: {name}")
        else:
            files.add(name)
        if m.get("type") not in MEDIA_TYPES:
            problems.append(f"media[{i}].type は image または video です。")
        if not isinstance(m.get("enabled"), bool):
            problems.append(f"media[{i}].enabled は true/false です。")
        order = m.get("order")
        if not isinstance(order, int) or isinstance(order, bool) or order < 1:
            problems.append(f"media[{i}].order は1以上の整数です。")
        elif order in orders:
            problems.append(f"media[{i}].order が重複しています: {order}")
        else:
            orders.add(order)

    video = project.get("video")
    if not isinstance(video, dict):
        problems.append("video が必要です。")
    else:
        checks = [("width", 16, 7680), ("height", 16, 4320), ("fps", 1, 120),
                  ("interval_seconds", 0.5, 600), ("transition_seconds", 0, 60),
                  ("zoom_percent", 0, 100)]
        for key, lo, hi in checks:
            if not _number(video.get(key), lo, hi):
                problems.append(f"video.{key} が範囲外です（{lo}〜{hi}）。")
        if not isinstance(video.get("blur_background"), bool):
            problems.append("video.blur_background は true/false です。")
        if video.get("camera_mode") not in CAMERA_MODES:
            problems.append(f"video.camera_mode は {sorted(CAMERA_MODES)} のどれかです。")
        if video.get("encoder_choice") not in ENCODER_CHOICES:
            problems.append(f"video.encoder_choice は {sorted(ENCODER_CHOICES)} のどれかです。")

    for key, cls in (("title_card", app.TitleCard), ("bgm_timing", app.BGMTiming)):
        block = project.get(key)
        known = set(cls.__dataclass_fields__)
        if not isinstance(block, dict) or set(block) - known:
            problems.append(f"{key} の項目が不正です（使える項目: {sorted(known)}）。")
    _validate_segments(project, problems)
    return problems


def save_project(project: dict, path, *, overwrite: bool = False) -> str:
    """Project JSON を保存する。元の写真を上書きすることは絶対に無い。"""
    problems = validate_project(project)
    if problems:
        raise CoreError("invalid_project", " / ".join(problems))
    target = Path(os.path.abspath(str(path)))
    if not target.name.endswith(PROJECT_SUFFIX):
        raise CoreError("invalid_output", f"Project ファイル名は *{PROJECT_SUFFIX} にしてください。")
    if target.exists() and not overwrite:
        raise CoreError("output_exists", f"同じ名前のファイルがあります: {target}")
    if not target.parent.is_dir():
        raise CoreError("invalid_output", f"保存先のフォルダーがありません: {target.parent}")
    text = json.dumps(project, ensure_ascii=False, indent=2) + chr(10)
    # 途中で止まっても壊れたファイルを残さないよう、一時ファイルから置き換える
    fd, tmp = tempfile.mkstemp(prefix=".pmm_", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline=chr(10)) as f:
            f.write(text)
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return str(target)


def load_project(path) -> dict:
    target = Path(os.path.abspath(str(path)))
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise CoreError("invalid_project", f"Project ファイルを読めません: {e}") from None
    problems = validate_project(data)
    if problems:
        raise CoreError("invalid_project", " / ".join(problems))
    return data


# ------------------------------------------------------------------
# Project JSON から MP4 を書き出す
# ------------------------------------------------------------------

VIDEO_SUFFIX = ".mp4"


def _is_inside(path: Path, folder: Path) -> bool:
    try:
        path.relative_to(folder)
        return True
    except ValueError:
        return False


def resolve_bgm(project: dict, music_folder) -> tuple[Path | None, list]:
    """Project の BGM 区間を、確かめたうえで renderer の区間（BGMSegment）にする。ここでは何も書かない。

    曲は、呼び出し側が明示した曲のフォルダー（music_folder）の直下にあるものだけ。
    Project JSON に書かれた場所をそのまま信じない:
      - music_folder は Project の project.music_folder と同じフォルダーであること
      - 曲はそのフォルダー直下のファイル名だけ（形式は画面の曲選択と同じ）
      - 実体がフォルダーの外にあるもの（リンク）は使わない
    """
    segments = sorted(project.get("bgm_segments") or [], key=lambda s: s["start_photo"])
    if not segments:
        return None, []
    if not str(music_folder or "").strip():
        raise CoreError("music_folder_required",
                        "BGM 区間があるので、曲のフォルダー（music_folder）を指定してください。")
    root = resolve_folder(music_folder)
    declared = Path(os.path.abspath(project["project"]["music_folder"]))
    if os.path.normcase(str(declared)) != os.path.normcase(str(root)):
        raise CoreError("folder_mismatch",
                        f"music_folder が Project JSON の music_folder と違います: {root} / {declared}")
    real_root = os.path.normcase(os.path.realpath(root))
    result = []
    for s in segments:
        path = root / s["audio"]
        if path.suffix.lower() not in AUDIO_SUFFIXES:
            raise CoreError("unsupported_audio", f"{s['audio']}: 対応していない曲の形式です。")
        if not path.is_file():
            raise CoreError("missing_audio", f"曲が見つかりません: {s['audio']}")
        if os.path.normcase(os.path.dirname(os.path.realpath(path))) != real_root:
            raise CoreError("invalid_audio", f"{s['audio']}: 曲のフォルダーの外を指すリンクは使えません。")
        result.append(app.BGMSegment(s["start_photo"] - 1, s["end_photo"] - 1, path))
    return root, result


def _render_plan(project: dict, photo_folder, output_file, music_folder=None) -> dict:
    """render の前に、計画と書き出し先を確かめる。ここでは何も書かない。"""
    plan = _check_inputs(project, photo_folder, music_folder)
    root, music_root = plan["root"], plan["music_root"]
    target = Path(os.path.abspath(str(output_file or "")))
    if not str(output_file or "").strip() or target.suffix.lower() != VIDEO_SUFFIX:
        raise CoreError("invalid_output", "書き出し先は *.mp4 のファイルにしてください。")
    if not target.parent.is_dir():
        raise CoreError("invalid_output", f"書き出し先のフォルダーがありません: {target.parent}")
    if _is_inside(target, root):
        raise CoreError("invalid_output", "写真のフォルダーの中には書き出しません（元の写真を守るため）。")
    if music_root is not None and _is_inside(target, music_root):
        raise CoreError("invalid_output", "曲のフォルダーの中には書き出しません（元の曲を守るため）。")
    plan["target"] = target
    plan["settings"] = target.with_name(target.stem + "_settings.json")
    return plan


def _check_inputs(project: dict, photo_folder, music_folder=None) -> dict:
    """計画・写真・曲を確かめる（書き出し先は見ない）。ここでは何も書かない。"""
    root = resolve_folder(photo_folder)
    declared = Path(os.path.abspath(project["project"]["source_folder"]))
    if os.path.normcase(str(declared)) != os.path.normcase(str(root)):
        raise CoreError("folder_mismatch",
                        "photo_folder が Project JSON の source_folder と違います: "
                        f"{root} / {declared}")

    entries = sorted((m for m in project["media"] if m["enabled"]), key=lambda m: m["order"])
    if not entries:
        raise CoreError("no_media", "使う写真（enabled: true）が1枚もありません。")
    images = []
    for m in entries:
        if m["type"] != "image":
            raise CoreError("unsupported_media", f"{m['file']}: 動画の差し込みはまだ書き出せません。")
        path = root / m["file"]
        if path.suffix.lower() not in app.SUPPORTED_IMAGES:
            raise CoreError("unsupported_media", f"{m['file']}: 対応していない形式です。")
        if not path.is_file():
            raise CoreError("missing_media", f"写真が見つかりません: {m['file']}")
        images.append(path)

    # BGM: 曲は呼び出し側が明示した曲のフォルダーの中だけ（resolve_bgm）。区間は画面と同じ BGMSegment
    music_root, segments = resolve_bgm(project, music_folder)

    video = project["video"]
    if video["transition_seconds"] >= video["interval_seconds"]:
        raise CoreError("invalid_project", "写真クロスフェードは写真の間隔より短くしてください。")
    title_block = project["title_card"]
    title = app.TitleCard(**title_block)
    if title.enabled:
        if title.duration <= 0:
            raise CoreError("invalid_project", "タイトルカードの表示時間は0より大きくしてください。")
        if title.fade_seconds > title.duration or title.text_fade_seconds > title.duration:
            raise CoreError("invalid_project", "タイトルのフェードは、タイトルの表示時間以下にしてください。")
        for label, value in (("背景色", title.bg_color), ("文字色", title.fg_color)):
            if app.parse_color(str(value), "") != str(value).strip():
                raise CoreError("invalid_project", f"タイトルカードの{label}が読めません（#FFFFFF の形式）。")
    timing = app.BGMTiming(**project["bgm_timing"])
    return {"root": root, "images": images, "video": video, "title": title,
            "timing": timing, "segments": segments, "music_root": music_root}


def _place(source: Path, target: Path) -> None:
    """一時名で書いてから置き換える。途中で止まっても壊れた完成ファイルを残さない。"""
    fd, tmp = tempfile.mkstemp(prefix=".pmm_", suffix=".part", dir=str(target.parent))
    os.close(fd)
    try:
        shutil.copyfile(source, tmp)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def render_project(project_file, photo_folder, output_file, *, overwrite: bool = False,
                   music_folder=None, progress=None, should_stop=None) -> dict:
    """Project JSON（上映計画）から、画面と同じ VideoRenderer で MP4 を書き出す。

    - Project JSON・写真・曲は読むだけ
    - BGM 区間があるときは music_folder（曲のフォルダー）が要る。曲はその直下のものだけ
      （resolve_bgm）。曲の合成は画面と同じ VideoRenderer.mux_bgm が行う
    - 書き出しは OS の一時フォルダーで行い、完成した MP4 と *_settings.json だけを
      書き出し先へ置く。中止・失敗のときは書き出し先に何も残さない
    - 同じ名前があれば overwrite を指定しない限り書かない
    progress(phase, done, total) は進捗（done / total は 0〜1000）。
    should_stop() が True を返したら中止して CancelledError を送る。
    """
    project_path = Path(os.path.abspath(str(project_file)))
    project = load_project(project_path)
    try:
        plan = _render_plan(project, photo_folder, output_file, music_folder)
    except (TypeError, ValueError) as e:     # 型の合わない値（例: 秒数が文字列）
        raise CoreError("invalid_project", f"Project JSON の値が読めません: {e}") from None
    target, settings_file = plan["target"], plan["settings"]
    if not overwrite:
        for path in (target, settings_file):
            if path.exists():
                raise CoreError("output_exists", f"同じ名前のファイルがあります: {path}")
    video = plan["video"]
    stop = should_stop or (lambda: False)

    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="pmm_render_") as work:
        q: Queue = Queue()
        stop_event = threading.Event()
        renderer = app.VideoRenderer(
            image_paths=plan["images"],
            output_path=Path(work) / target.name,
            width=int(video["width"]), height=int(video["height"]), fps=int(video["fps"]),
            interval_seconds=float(video["interval_seconds"]),
            transition_seconds=float(video["transition_seconds"]),
            zoom_percent=float(video["zoom_percent"]),
            blur_background=bool(video["blur_background"]),
            bgm_segments=plan["segments"],
            encoder_pref=video["encoder_choice"],
            q=q, stop_event=stop_event,
            title=plan["title"], bgm_timing=plan["timing"],
            camera_mode=video["camera_mode"],
            settings_extra={
                # 設定ファイルには、一時フォルダーではなく実際の書き出し先を残す
                "output": str(target),
                "images": [{"filename": m["file"], "included": m["enabled"]}
                           for m in sorted(project["media"], key=lambda m: m["order"])],
                "project": {"file": project_path.name, "title": project["project"].get("title")},
                "rendered_by": {"api": "pmm_core.render_project", "version": TOOL_VERSION},
            },
        )
        failure: list[BaseException] = []

        def work_thread():
            try:
                renderer.run()
            except BaseException as e:        # 中止・失敗は呼び出し側で扱う
                failure.append(e)

        worker = threading.Thread(target=work_thread, daemon=True)
        worker.start()
        status, last_done, warnings = "準備中", -1, []
        try:
            while True:
                alive = worker.is_alive()
                if stop() and not stop_event.is_set():
                    stop_event.set()          # VideoRenderer が FFmpeg を止めて抜ける
                try:
                    while True:
                        item = q.get_nowait()
                        if item[0] == "status":
                            status = item[1]
                        elif item[0] == "warning":
                            warnings.append(str(item[1]).splitlines()[0])
                        elif item[0] == "progress" and progress:
                            done = min(1000, int(round(float(item[1]) * 10)))
                            if done > last_done:
                                last_done = done
                                progress(status, done, 1000)
                except Empty:
                    pass
                if not alive:
                    break
                worker.join(timeout=0.2)
        except BaseException:
            # Ctrl+C などでここを抜けるときも、FFmpeg を止めてから一時フォルダーを片付ける
            stop_event.set()
            worker.join(timeout=60)
            raise

        if stop_event.is_set() or any(isinstance(e, InterruptedError) for e in failure):
            raise CancelledError()
        if failure:
            e = failure[0]
            raise CoreError("render_failed", f"書き出しに失敗しました: {type(e).__name__}: {e}")
        built = Path(work) / target.name
        built_settings = renderer.settings_path()
        if not built.is_file() or built.stat().st_size == 0:
            raise CoreError("render_failed", "MP4 が作られませんでした。")
        if stop():
            raise CancelledError()
        _place(built, target)
        placed = [str(target)]
        if built_settings.is_file():
            _place(built_settings, settings_file)
            placed.append(str(settings_file))

    detections = {}
    for det in renderer.detections.values():
        detections[det.mode] = detections.get(det.mode, 0) + 1
    return {
        "output": str(target),
        "settings_file": str(settings_file) if len(placed) > 1 else None,
        "bytes": target.stat().st_size,
        "photos": len(plan["images"]),
        "frames": renderer.total_frames,
        "duration_seconds": round(renderer.total_duration, 3),
        "width": renderer.w, "height": renderer.h, "fps": renderer.fps,
        "camera_mode": renderer.camera_mode,
        "subject_camera": detections or None,
        "title_card": bool(renderer.title),
        "bgm_tracks": [s.audio_path.name for s in plan["segments"]],
        "encoder_used": renderer.encoder_used,
        "elapsed_seconds": round(time.perf_counter() - started, 1),
        "warnings": warnings,
    }


# ------------------------------------------------------------------
# SI Director v1: 計画の組み立て・書き出し前の確認・判断の記録
# （どう見せるかは呼び出し側の SI が決める。ここは決まったことを安全に形にするだけ）
# ------------------------------------------------------------------

DIRECTOR_SUFFIX = ".director.json"
DIRECTOR_KIND = "photomoviemaker.director_plan"
DIRECTOR_VERSION = 1
DIRECTOR_RESERVED = {"kind", "project_file", "saved_at", "computed"}
MAX_DIRECTOR_BYTES = 64 * 1024
EDIT_KEYS = {"selected", "title_card", "video", "bgm_timing", "bgm_segments", "target_duration_seconds", "director"}
VIDEO_KEYS = set(DEFAULT_VIDEO)
_ABSOLUTE_PATH = re.compile(r"(?i)(?:^|[\s\"'(])(?:[a-z]:[\\/]|\\\\[^\\\s])")


def _clock(seconds: float) -> str:
    whole = int(round(seconds))
    return f"{whole // 60:02d}:{whole % 60:02d}"


def build_timeline(project: dict, plan: dict) -> dict:
    """書き出したときの時間割（タイトル・写真・曲）を、同じ VideoRenderer の計算で求める。

    何も描画しない。曲の長さは ffprobe で読むだけ（無ければ unknown）。"""
    video, title = plan["video"], plan["title"]
    renderer = app.VideoRenderer(
        image_paths=plan["images"], output_path=Path(tempfile.gettempdir()) / "pmm_preview_unused.mp4",
        width=int(video["width"]), height=int(video["height"]), fps=int(video["fps"]),
        interval_seconds=float(video["interval_seconds"]),
        transition_seconds=float(video["transition_seconds"]),
        zoom_percent=float(video["zoom_percent"]), blur_background=bool(video["blur_background"]),
        bgm_segments=plan["segments"], encoder_pref=video["encoder_choice"], q=Queue(),
        stop_event=threading.Event(), title=title, bgm_timing=plan["timing"], camera_mode=video["camera_mode"])
    title_seconds, span, total = renderer.title_duration, renderer.photo_span, renderer.total_duration
    photos = [{"position": i, "file": path.name, "start_seconds": round(title_seconds + (i - 1) * span, 3),
               "end_seconds": round(title_seconds + i * span, 3)}
              for i, path in enumerate(plan["images"], 1)]
    ffprobe = find_ffprobe()
    lengths: dict = {}
    music, warnings = [], []
    planned = renderer.bgm_plan(renderer.validate_bgm_segments()) if plan["segments"] else []
    for seg in plan["segments"]:
        name = seg.audio_path.name
        if name not in lengths:
            lengths[name] = _probe_track(ffprobe, seg.audio_path).get("probe_duration_seconds")
        entry = next((e for e in planned if e[0] is seg), None)
        item = {"audio": name, "start_photo": seg.start_index + 1, "end_photo": seg.end_index + 1,
                "track_seconds": lengths[name]}
        if entry is None:
            item.update(sounds=False, note="区間が短すぎて鳴らさない（0.30 秒未満）")
            warnings.append(f"{name}: 区間が短すぎて鳴りません。")
        else:
            _, start, duration, fade_in, fade_out = entry
            item.update(sounds=True, starts_at=round(start, 3), duration_seconds=round(duration, 3),
                        fade_in_seconds=round(fade_in, 3), fade_out_seconds=round(fade_out, 3))
            if lengths[name] is not None and lengths[name] + 0.05 < duration:
                item["repeats"] = True
                warnings.append(f"{name}: 曲（{lengths[name]:.1f} 秒）が区間（{duration:.1f} 秒）より短く、"
                                "先頭から繰り返されます。")
            elif lengths[name] is not None:
                item["repeats"] = False
                item["uses_first_seconds_of_track"] = round(duration, 3)
        music.append(item)
    silent, cursor = [], 0.0
    for item in sorted((m for m in music if m["sounds"]), key=lambda m: m["starts_at"]):
        if item["starts_at"] - cursor > 0.05:
            silent.append({"start_seconds": round(cursor, 3), "end_seconds": item["starts_at"]})
        cursor = max(cursor, item["starts_at"] + item["duration_seconds"])
    if plan["segments"] and total - cursor > 0.05:
        silent.append({"start_seconds": round(cursor, 3), "end_seconds": round(total, 3)})
    target = (project.get("project") or {}).get("target_duration_seconds")
    if target and abs(total - target) > 0.2 * target:
        warnings.append(f"長さ {total:.1f} 秒が目標 {target:.0f} 秒から 20% 以上ずれています。")
    lines = []
    if renderer.title:
        lines.append(f"00:00 タイトル「{title.main}」" + (f" / {title.sub}" if title.sub else "")
                     + (f" / {title.date}" if title.date else ""))
    events = [(p["start_seconds"], f"写真 {p['position']} {p['file']}") for p in photos]
    events += [(m["starts_at"], f"♪ {m['audio']}（写真 {m['start_photo']}〜{m['end_photo']}"
                + ("・繰り返し" if m.get("repeats") else "") + "）") for m in music if m["sounds"]]
    lines += [f"{_clock(t)} {text}" for t, text in sorted(events, key=lambda e: e[0])]
    lines.append(f"{_clock(total)} 終わり")
    return {"total_seconds": round(total, 3), "target_duration_seconds": target,
            "title_seconds": round(title_seconds, 3), "seconds_per_photo": round(span, 3),
            "crossfade_seconds": round(renderer.transition, 3), "photo_count": len(photos),
            "title": ({"main": title.main, "sub": title.sub, "date": title.date} if renderer.title else None),
            "photos": photos, "music": music, "silent_ranges": silent if plan["segments"] else [],
            "warnings": warnings, "lines": lines}


def preview_project(project_file, photo_folder, music_folder=None) -> dict:
    """書き出す前の確認と時間割。写真・曲・計画は読むだけで、何も書かない。"""
    project = load_project(project_file)
    try:
        plan = _check_inputs(project, photo_folder, music_folder)
    except (TypeError, ValueError) as e:
        raise CoreError("invalid_project", f"Project JSON の値が読めません: {e}") from None
    return {"project_file": Path(os.path.abspath(str(project_file))).name, "ready": True,
            "timeline": build_timeline(project, plan)}


def _director_text(plan) -> str:
    if not isinstance(plan, dict):
        raise CoreError("invalid_director_plan", "Director Plan は JSON オブジェクトです。")
    if plan.get("director_version") != DIRECTOR_VERSION:
        raise CoreError("invalid_director_plan", f"director_version は {DIRECTOR_VERSION} です。")
    if not isinstance(plan.get("concept"), str) or not plan["concept"].strip():
        raise CoreError("invalid_director_plan", "concept（どう見せるかの考え）が必要です。")
    reserved = DIRECTOR_RESERVED & set(plan)
    if reserved:
        raise CoreError("invalid_director_plan", f"{sorted(reserved)} は PhotoMovieMaker が書く項目です。")
    text = json.dumps(plan, ensure_ascii=False)
    if len(text.encode("utf-8")) > MAX_DIRECTOR_BYTES:
        raise CoreError("invalid_director_plan", f"Director Plan は {MAX_DIRECTOR_BYTES // 1024} KB までです。")
    if _ABSOLUTE_PATH.search(text.replace("\\\\", "\\")):
        raise CoreError("invalid_director_plan",
                        "Director Plan に絶対パスは書かないでください（ファイル名や論理的な名前で書く）。")
    return text


def _write_json(target: Path, data: dict) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".pmm_", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline=chr(10)) as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=2) + chr(10))
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def director_path(project_file) -> Path:
    path = Path(os.path.abspath(str(project_file)))
    stem = path.name[:-len(PROJECT_SUFFIX)] if path.name.endswith(PROJECT_SUFFIX) else path.stem
    return path.with_name(stem + DIRECTOR_SUFFIX)


def save_director_plan(project_file, plan: dict, *, overwrite: bool = False) -> dict:
    """SI の判断の記録（Director Plan）を、Project の隣に <名前>.director.json として置く。

    PhotoMovieMaker は中身を解釈しない（書き出しには使わない）。形と大きさと、絶対パスが無いことだけを
    確かめ、計画から計算した時間割（computed）を添える。"""
    _director_text(plan)
    project_path = Path(os.path.abspath(str(project_file)))
    project = load_project(project_path)
    target = director_path(project_path)
    if target.exists() and not overwrite:
        raise CoreError("output_exists", f"同じ名前のファイルがあります: {target}")
    info = project["project"]
    try:
        checked = _check_inputs(project, info["source_folder"], info.get("music_folder"))
        timeline = build_timeline(project, checked)
    except CoreError as e:
        timeline = {"unavailable": f"{e.code}: {e.message}"}
    shown = shown_files(project)
    record = {"kind": DIRECTOR_KIND, **plan, "project_file": project_path.name,
              "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "computed": {"photos_in_folder": len(project["media"]), "photos_selected": len(shown),
                           "photos_excluded": len(project["media"]) - len(shown),
                           "bgm_tracks": [s["audio"] for s in project["bgm_segments"]],
                           "timeline": {k: v for k, v in timeline.items() if k != "photos"}}}
    _write_json(target, record)
    return {"director_file": str(target), "computed": record["computed"]}


def compose_project(folder, edits: dict, save_to, *, music_folder=None, overwrite: bool = False) -> dict:
    """写真フォルダーから、SI の決めたとおりの Project JSON を新しく組み立てて保存する。

    edits（すべて SI が決める。ここでは選ばない・並べ替えない・足さない）:
      selected                使う写真のファイル名を上映順に（それ以外は enabled: false）
      title_card / video / bgm_timing   それぞれの項目の一部（既定値に上書き）
      bgm_segments            使う写真の何枚目から何枚目までにどの曲（music_folder 直下のファイル名）
      target_duration_seconds 目標の長さ
      director                Director Plan（あれば <名前>.director.json として隣に置く）
    既存の Project は上書きしない（overwrite を指定したときだけ）。写真・曲は読むだけ。"""
    if not isinstance(edits, dict) or set(edits) - EDIT_KEYS:
        raise CoreError("invalid_edits", f"edits に使える項目は {sorted(EDIT_KEYS)} です。")
    if "director" in edits:
        _director_text(edits["director"])
    root = resolve_folder(folder)
    project = create_project_plan(root, title=None)
    known = {m["file"]: m for m in project["media"]}
    selected = edits.get("selected")
    if not isinstance(selected, list) or not selected or not all(isinstance(f, str) for f in selected):
        raise CoreError("invalid_edits", "selected は使う写真のファイル名の配列（上映順）です。")
    if len(set(selected)) != len(selected):
        raise CoreError("invalid_edits", "selected に同じ写真が重複しています。")
    missing = [f for f in selected if f not in known]
    if missing:
        raise CoreError("missing_media", f"フォルダーに無い写真: {missing[:5]}")
    chosen = set(selected)
    rest = [m["file"] for m in project["media"] if m["file"] not in chosen]
    for order, name in enumerate(selected + rest, 1):
        known[name].update(enabled=name in chosen, order=order)
    project["media"].sort(key=lambda m: m["order"])
    for key, cls in (("title_card", app.TitleCard), ("bgm_timing", app.BGMTiming)):
        change = edits.get(key) or {}
        if not isinstance(change, dict) or set(change) - set(cls.__dataclass_fields__):
            raise CoreError("invalid_edits", f"{key} に使える項目は {sorted(cls.__dataclass_fields__)} です。")
        project[key].update(change)
    change = edits.get("video") or {}
    if not isinstance(change, dict) or set(change) - VIDEO_KEYS:
        raise CoreError("invalid_edits", f"video に使える項目は {sorted(VIDEO_KEYS)} です。")
    project["video"].update(change)
    project["project"]["title"] = project["title_card"]["main"]
    if "target_duration_seconds" in edits:
        project["project"]["target_duration_seconds"] = edits["target_duration_seconds"]
    segments = edits.get("bgm_segments") or []
    project["bgm_segments"] = segments
    if segments:
        if not str(music_folder or "").strip():
            raise CoreError("music_folder_required", "BGM 区間があるので、曲のフォルダー（music_folder）を指定してください。")
        project["project"]["music_folder"] = str(resolve_folder(music_folder))
    problems = validate_project(project)
    if problems:
        raise CoreError("invalid_project", " / ".join(problems))
    try:
        checked = _check_inputs(project, root, music_folder if segments else None)
    except (TypeError, ValueError) as e:
        raise CoreError("invalid_project", f"値が読めません: {e}") from None
    timeline = build_timeline(project, checked)
    target = Path(os.path.abspath(str(save_to)))
    if "director" in edits and director_path(target).exists() and not overwrite:
        raise CoreError("output_exists", f"同じ名前のファイルがあります: {director_path(target)}")
    saved = save_project(project, target, overwrite=overwrite)
    result = {"project_file": saved, "director_file": None, "project": project, "timeline": timeline}
    if "director" in edits:
        result["director_file"] = save_director_plan(saved, edits["director"], overwrite=True)["director_file"]
    return result
