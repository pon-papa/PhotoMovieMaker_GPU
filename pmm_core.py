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
import os
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

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
        ],
        "planned": ["render_preview", "render_final", "transition_primitives",
                    "video_clips", "beat_aligned_plan"],
        "gui_only": ["render_mp4", "bgm", "title_card", "subject_aware_camera"],
        "media": {"image_extensions": sorted(app.SUPPORTED_IMAGES),
                  "video_extensions": [], "recursive": False},
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
        if (not isinstance(name, str) or not name or name in {".", ".."}
                or any(c in name for c in "\\/:") or name != name.strip()):
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
    if not isinstance(project.get("bgm_segments"), list):
        problems.append("bgm_segments は配列である必要があります。")
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
