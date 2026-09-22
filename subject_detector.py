# -*- coding: utf-8 -*-
"""ローカル完結の被写体検出（人物の顔 / 犬）。

PhotoMovieMaker GPU の「被写体追従カメラ」で使う。

方針:
- 外部通信は一切しない。モデルは models/ に置かれた ONNX を読むだけで、
  実行時にダウンロードもアップロードもしない。
- 顔認識（誰であるか）はしない。顔が「どこにあるか」だけを見る。
  個人識別・顔embedding・照合データベースのたぐいは一切作らない。
- 犬も個体識別や犬種判定はしない。COCOの dog クラスの位置だけを使う。
- 判断が曖昧なとき（被写体が複数あるなど）は主役を勝手に決めず、
  呼び出し側へ「従来方式へ戻れ」と返す。

使用モデル:
- 顔: YuNet (face_detection_yunet_2023mar.onnx) MIT License / Shiqi Yu
- 犬: YOLOX (object_detection_yolox_2022nov.onnx) Apache-2.0 / Megvii Inc.
  いずれも OpenCV Zoo (https://github.com/opencv/opencv_zoo) の配布物。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np


MODELS_DIR_NAME = "models"
FACE_MODEL_FILE = "face_detection_yunet_2023mar.onnx"
DOG_MODEL_FILE = "object_detection_yolox_2022nov.onnx"

# ------------------------------------------------------------------
# 調整用の定数。GUIには出さず、ここだけ見れば直せるようにしておく。
# 誤検出を減らす方を優先した値にしてある。
# ------------------------------------------------------------------
FACE_SCORE_THRESHOLD = 0.85   # YuNetのスコア下限
FACE_NMS_THRESHOLD = 0.30
DOG_SCORE_THRESHOLD = 0.45    # YOLOXのスコア下限
DOG_NMS_THRESHOLD = 0.50

# 犬のbbox内で狙う点。横は中央、縦は上から35%＝頭・肩・背中寄り。
# 後ろ姿のときに胴体中央や腰へ寄ってしまうのを避けるための値。
DOG_TARGET_X_RATIO = 0.50
DOG_TARGET_Y_RATIO = 0.35

# 画面に対して極端に小さい検出は無視する（写り込みや誤検出対策）
MIN_FACE_AREA_RATIO = 0.0008
MIN_DOG_AREA_RATIO = 0.0030

COCO_DOG_CLASS_ID = 16
YOLOX_INPUT_SIZE = (640, 640)
YOLOX_STRIDES = (8, 16, 32)
YOLOX_PAD_VALUE = 114.0

MODE_HUMAN_FACE = "human_face"
MODE_DOG_UPPER = "dog_upper"
MODE_LEGACY = "legacy_fallback"


@dataclass
class SubjectDetection:
    """1枚の写真の解析結果。座標は元写真に対する正規化値 (0.0〜1.0)。

    画像そのものは保持しない。150枚解析しても増えるのはこの軽い値だけ。
    """
    filename: str
    face_count: int = 0
    dog_count: int = 0
    mode: str = MODE_LEGACY
    target_x: float | None = None
    target_y: float | None = None
    face_confidence: float | None = None
    dog_confidence: float | None = None
    note: str = ""

    @property
    def has_target(self) -> bool:
        return (
            self.mode != MODE_LEGACY
            and self.target_x is not None
            and self.target_y is not None
        )


class _YoloXDogDetector:
    """YOLOX(ONNX)をOpenCV DNNで動かして dog クラスだけ取り出す。

    前処理・後処理は OpenCV Zoo の公式実装に合わせてある。
    """

    def __init__(self, model_bytes: bytes):
        # OpenCVはWindowsで非ASCIIのファイルパスを開けないため、
        # Python側で読んだバイト列を渡す。日本語フォルダーでも動く。
        self.net = cv2.dnn.readNetFromONNX(bytearray(model_bytes))
        self.net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
        self.net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
        self._build_anchors()

    def _build_anchors(self):
        grids, expanded = [], []
        for stride in YOLOX_STRIDES:
            hsize = YOLOX_INPUT_SIZE[0] // stride
            wsize = YOLOX_INPUT_SIZE[1] // stride
            xv, yv = np.meshgrid(np.arange(hsize), np.arange(wsize))
            grid = np.stack((xv, yv), 2).reshape(1, -1, 2)
            grids.append(grid)
            expanded.append(np.full((*grid.shape[:2], 1), stride))
        self.grids = np.concatenate(grids, 1)
        self.expanded_strides = np.concatenate(expanded, 1)

    @staticmethod
    def _letterbox(image_rgb: np.ndarray):
        """アスペクト比を保ったまま640x640へ収め、余白は114で埋める。"""
        padded = np.ones(
            (YOLOX_INPUT_SIZE[0], YOLOX_INPUT_SIZE[1], 3), dtype=np.float32
        ) * YOLOX_PAD_VALUE
        ratio = min(
            YOLOX_INPUT_SIZE[0] / image_rgb.shape[0],
            YOLOX_INPUT_SIZE[1] / image_rgb.shape[1],
        )
        resized = cv2.resize(
            image_rgb,
            (int(image_rgb.shape[1] * ratio), int(image_rgb.shape[0] * ratio)),
            interpolation=cv2.INTER_LINEAR,
        ).astype(np.float32)
        padded[: resized.shape[0], : resized.shape[1]] = resized
        return padded, ratio

    def detect_dogs(self, image_rgb: np.ndarray):
        """戻り値: [(x, y, w, h, score), ...] 元画像のピクセル座標。"""
        padded, ratio = self._letterbox(image_rgb)
        blob = np.transpose(padded, (2, 0, 1))[np.newaxis, :, :, :]
        self.net.setInput(blob)
        outs = self.net.forward(self.net.getUnconnectedOutLayersNames())

        dets = outs[0][0]
        dets[:, :2] = (dets[:, :2] + self.grids) * self.expanded_strides
        dets[:, 2:4] = np.exp(dets[:, 2:4]) * self.expanded_strides

        # dogクラスだけを見る。ほかのクラスは主役判定に使わない。
        dog_scores = dets[:, 4] * dets[:, 5 + COCO_DOG_CLASS_ID]
        keep = dog_scores > DOG_SCORE_THRESHOLD
        if not np.any(keep):
            return []

        boxes = dets[keep][:, :4]
        scores = dog_scores[keep]
        xywh = np.empty_like(boxes)
        xywh[:, 0] = boxes[:, 0] - boxes[:, 2] / 2.0
        xywh[:, 1] = boxes[:, 1] - boxes[:, 3] / 2.0
        xywh[:, 2] = boxes[:, 2]
        xywh[:, 3] = boxes[:, 3]

        idx = cv2.dnn.NMSBoxes(
            xywh.tolist(), scores.tolist(), DOG_SCORE_THRESHOLD, DOG_NMS_THRESHOLD
        )
        if len(idx) == 0:
            return []
        idx = np.array(idx).reshape(-1)

        results = []
        h, w = image_rgb.shape[:2]
        for i in idx:
            x, y, bw, bh = xywh[i] / ratio          # letterboxを戻す
            if bw <= 0 or bh <= 0:
                continue
            if (bw * bh) / float(w * h) < MIN_DOG_AREA_RATIO:
                continue
            results.append((float(x), float(y), float(bw), float(bh), float(scores[i])))
        return results


class SubjectDetector:
    """顔detectorと犬detectorをまとめて1回だけloadし、写真ごとに1回だけ走らせる。"""

    def __init__(self, models_dir: Path | None = None):
        self.models_dir = Path(models_dir) if models_dir else (
            Path(__file__).resolve().parent / MODELS_DIR_NAME
        )
        self.available = False
        self.unavailable_reason = ""
        self._face = None
        self._dog = None
        self._face_input_size = None
        self._load()

    # ---------------- 読み込み ----------------

    def _load(self):
        face_path = self.models_dir / FACE_MODEL_FILE
        dog_path = self.models_dir / DOG_MODEL_FILE

        missing = [p.name for p in (face_path, dog_path) if not p.is_file()]
        if missing:
            self.unavailable_reason = (
                "人物・犬検出モデルが見つかりません。" + chr(10)
                + "次のファイルを models フォルダーへ置いてください:" + chr(10)
                + chr(10).join("  " + m for m in missing) + chr(10)
                + "（従来カメラワークはそのまま使用できます）"
            )
            return

        try:
            # どちらもファイルパスではなくバイト列から読む。
            # OpenCVはWindowsで非ASCIIパスのモデルを開けないが、
            # この方法なら日本語フォルダーのままで動く。
            face_bytes = face_path.read_bytes()
            dog_bytes = dog_path.read_bytes()
            self._face = cv2.FaceDetectorYN.create(
                "onnx",
                bytearray(face_bytes),
                b"",
                (320, 320),
                FACE_SCORE_THRESHOLD,
                FACE_NMS_THRESHOLD,
            )
            self._dog = _YoloXDogDetector(dog_bytes)
        except Exception as e:
            self._face = None
            self._dog = None
            self.unavailable_reason = (
                "人物・犬検出モデルを読み込めませんでした。" + chr(10)
                + f"{type(e).__name__}: {e}" + chr(10)
                + "（従来カメラワークはそのまま使用できます）"
            )
            return

        self.available = True

    # ---------------- 検出 ----------------

    def _detect_faces(self, image_bgr: np.ndarray):
        """戻り値: [(x, y, w, h, score), ...]"""
        h, w = image_bgr.shape[:2]
        if self._face_input_size != (w, h):
            self._face.setInputSize((w, h))
            self._face_input_size = (w, h)
        _, faces = self._face.detect(image_bgr)
        if faces is None:
            return []
        results = []
        for f in faces:
            x, y, bw, bh = float(f[0]), float(f[1]), float(f[2]), float(f[3])
            score = float(f[-1])
            if bw <= 0 or bh <= 0 or score < FACE_SCORE_THRESHOLD:
                continue
            if (bw * bh) / float(w * h) < MIN_FACE_AREA_RATIO:
                continue
            results.append((x, y, bw, bh, score))
        return results

    def detect(self, filename: str, image_rgb: np.ndarray) -> SubjectDetection:
        """EXIF補正後の「元写真」を受け取り、追従目標を1つだけ決める。

        ぼかし背景を合成したあとのcanvasを渡してはいけない。
        背景側に被写体が拡大コピーされていて二重に検出されるため。
        """
        det = SubjectDetection(filename=filename)

        if not self.available:
            det.note = "detector unavailable"
            return det

        if image_rgb is None or image_rgb.ndim != 3 or image_rgb.shape[2] != 3:
            det.note = "invalid image"
            return det

        h, w = image_rgb.shape[:2]
        if h < 8 or w < 8:
            det.note = "image too small"
            return det

        image_bgr = cv2.cvtColor(image_rgb, cv2.COLOR_RGB2BGR)

        # 1枚でdetectorが転んでも、その写真だけ従来方式へ戻す。
        # 動画全体を止めない。
        try:
            faces = self._detect_faces(image_bgr)
        except Exception as e:
            faces = []
            det.note = f"face detector error: {type(e).__name__}"
        try:
            dogs = self._dog.detect_dogs(image_rgb)
        except Exception as e:
            dogs = []
            det.note = (det.note + " / " if det.note else "") + \
                       f"dog detector error: {type(e).__name__}"

        det.face_count = len(faces)
        det.dog_count = len(dogs)
        if faces:
            det.face_confidence = round(max(f[4] for f in faces), 4)
        if dogs:
            det.dog_confidence = round(max(d[4] for d in dogs), 4)

        # ---- 主役を勝手に決めないためのルール ----
        # 対象が明確に1つのときだけ追従する。曖昧なら従来方式。
        if det.face_count == 1 and det.dog_count == 0:
            x, y, bw, bh, score = faces[0]
            tx = x + bw * 0.5
            ty = y + bh * 0.5
            det.mode = MODE_HUMAN_FACE
            det.note = det.note or "single face"
        elif det.face_count == 0 and det.dog_count == 1:
            x, y, bw, bh, score = dogs[0]
            tx = x + bw * DOG_TARGET_X_RATIO
            ty = y + bh * DOG_TARGET_Y_RATIO
            det.mode = MODE_DOG_UPPER
            det.note = det.note or "single dog"
        else:
            if det.face_count == 0 and det.dog_count == 0:
                det.note = det.note or "no subject"
            elif det.face_count >= 2 and det.dog_count == 0:
                det.note = det.note or "multiple faces"
            elif det.dog_count >= 2 and det.face_count == 0:
                det.note = det.note or "multiple dogs"
            else:
                det.note = det.note or "person and dog"
            return det

        # 座標が異常なら追従しない
        if not (0.0 <= tx <= w and 0.0 <= ty <= h):
            det.mode = MODE_LEGACY
            det.note = "target out of image"
            return det

        det.target_x = round(tx / w, 6)
        det.target_y = round(ty / h, 6)
        return det


def csv_header() -> str:
    return ("filename,face_count,dog_count,mode,"
            "target_x_norm,target_y_norm,face_confidence,dog_confidence,note")


def to_csv_row(det: SubjectDetection) -> str:
    def s(v):
        return "" if v is None else str(v)
    name = det.filename.replace(",", " ")
    note = det.note.replace(",", ";")
    return ",".join([
        name, str(det.face_count), str(det.dog_count), det.mode,
        s(det.target_x), s(det.target_y),
        s(det.face_confidence), s(det.dog_confidence), note,
    ])
