# -*- coding: utf-8 -*-
"""画像embedding（同じ場面かどうかを見分けるための補助）。

おすすめ解析で、pHash や色ヒストグラムだけでは取りこぼす
「少し変化した同じ場面」を見つけるために使います。

役割分担:
- pHash / dHash / 色ヒストグラム … ほぼ同じ写真を確実に見つける
- embedding                      … 少し変化した同じ場面を見つける

embedding は**似ているかどうかを測るためだけ**に使います。
きれい・かわいい・構図が良い、といった評価には一切使いません。

外部通信はありません。モデルは models/ に置かれた ONNX を読むだけで、
実行時にダウンロードもアップロードもしません。
モデルが無ければこの機能は使えないだけで、おすすめ解析そのものは動きます。

使用モデル:
- DINOv2 ViT-S/14 (facebook/dinov2-small) Apache License 2.0 / Meta Platforms
  revision ed25f3a31f01632728cabb09d1542f84ab7b0056
  CLSトークン384次元を取り出すONNXへ書き出したもの。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

try:
    import onnxruntime as ort
except Exception:      # onnxruntime が無くてもアプリは動く
    ort = None


MODELS_DIR_NAME = "models"
EMBEDDING_MODEL_FILE = "dinov2_small_embedding.onnx"

MODEL_ID = "facebook/dinov2-small"
MODEL_REVISION = "ed25f3a31f01632728cabb09d1542f84ab7b0056"
EMBEDDING_DIMENSION = 384

# 公式 preprocessor_config.json どおり。独自の正規化はしない。
RESIZE_SHORTEST_EDGE = 256
CROP_SIZE = 224
IMAGE_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
IMAGE_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)

DEFAULT_BATCH_SIZE = 8


@dataclass
class EmbeddingInfo:
    """設定ファイルへ残す用の情報。"""
    model_id: str = MODEL_ID
    revision: str = MODEL_REVISION
    dimension: int = EMBEDDING_DIMENSION
    license: str = "Apache-2.0"


def preprocess(path: Path) -> np.ndarray:
    """1枚を (3, 224, 224) float32 にする。

    EXIFの向きを直してから、公式仕様どおり
    短辺256へ縮小 → 中央224で切り出し → 0-1へ → mean/stdで正規化。
    元ファイルは読むだけで書き換えない。
    """
    with Image.open(path) as im:
        upright = ImageOps.exif_transpose(im).convert("RGB")
        w, h = upright.size
        scale = RESIZE_SHORTEST_EDGE / min(w, h)
        resized = upright.resize(
            (max(1, round(w * scale)), max(1, round(h * scale))),
            Image.Resampling.BICUBIC,
        )
    rw, rh = resized.size
    left = (rw - CROP_SIZE) // 2
    top = (rh - CROP_SIZE) // 2
    cropped = resized.crop((left, top, left + CROP_SIZE, top + CROP_SIZE))

    arr = np.asarray(cropped, dtype=np.float32) / 255.0
    arr = (arr - IMAGE_MEAN) / IMAGE_STD
    return np.transpose(arr, (2, 0, 1))


class EmbeddingExtractor:
    """ONNXモデルを1回だけ読み込み、全写真で使い回す。"""

    def __init__(self, models_dir: Path | None = None,
                 batch_size: int = DEFAULT_BATCH_SIZE,
                 threads: int | None = None):
        self.models_dir = Path(models_dir) if models_dir else (
            Path(__file__).resolve().parent / MODELS_DIR_NAME
        )
        self.batch_size = max(1, int(batch_size))
        self.available = False
        self.unavailable_reason = ""
        self.info = EmbeddingInfo()
        self._session = None
        self._load(threads)

    def _load(self, threads: int | None):
        if ort is None:
            self.unavailable_reason = (
                "onnxruntime が入っていないため、画像embeddingは使えません。" + chr(10)
                + "setup.bat をもう一度実行すると入ります。" + chr(10)
                + "（embeddingを使わないおすすめ解析はそのまま動きます）"
            )
            return

        path = self.models_dir / EMBEDDING_MODEL_FILE
        if not path.is_file():
            self.unavailable_reason = (
                "画像embeddingのモデルが見つかりません。" + chr(10)
                + f"{EMBEDDING_MODEL_FILE} を models フォルダーへ置いてください。" + chr(10)
                + "（embeddingを使わないおすすめ解析はそのまま動きます）"
            )
            return

        try:
            options = ort.SessionOptions()
            if threads:
                options.intra_op_num_threads = int(threads)
            # OpenCVと違い onnxruntime は日本語パスも開けるが、
            # 念のためバイト列から読む（顔・犬モデルと同じやり方）。
            self._session = ort.InferenceSession(
                path.read_bytes(), options, providers=["CPUExecutionProvider"]
            )
        except Exception as e:
            self.unavailable_reason = (
                "画像embeddingのモデルを読み込めませんでした。" + chr(10)
                + f"{type(e).__name__}: {e}" + chr(10)
                + "（embeddingを使わないおすすめ解析はそのまま動きます）"
            )
            return

        self.available = True

    def embed_paths(self, paths, progress=None, should_stop=None) -> dict:
        """写真のパス → 正規化済みembedding の辞書を返す。

        progress(done, total) が呼ばれる。
        should_stop() が True を返したら、その時点で打ち切って空を返す。
        """
        if not self.available:
            return {}
        paths = list(paths)
        total = len(paths)
        result: dict[str, np.ndarray] = {}

        for start in range(0, total, self.batch_size):
            if should_stop is not None and should_stop():
                return {}
            chunk = paths[start:start + self.batch_size]
            tensors, keys = [], []
            for p in chunk:
                try:
                    tensors.append(preprocess(p))
                    keys.append(str(p))
                except Exception:
                    # 読めない写真はembedding無しにする。解析全体は止めない。
                    continue
            if not tensors:
                if progress is not None:
                    progress(min(start + len(chunk), total), total)
                continue
            batch = np.stack(tensors).astype(np.float32)
            try:
                out = self._session.run(["embedding"], {"pixel_values": batch})[0]
            except Exception:
                if progress is not None:
                    progress(min(start + len(chunk), total), total)
                continue
            for key, vec in zip(keys, out):
                norm = float(np.linalg.norm(vec))
                if norm > 0:
                    result[key] = (vec / norm).astype(np.float32)
            if progress is not None:
                progress(min(start + len(chunk), total), total)

        return result


def cosine(a: np.ndarray | None, b: np.ndarray | None) -> float:
    """正規化済みベクトル同士なので内積がそのままcosine similarity。"""
    if a is None or b is None:
        return 0.0
    return float(np.dot(a, b))
