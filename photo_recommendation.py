# -*- coding: utf-8 -*-
"""おすすめ解析 — 写真を選ぶときの手がかりを出すだけの機能。

PhotoMovieMaker GPU で、似た写真がたくさんあるときに
「どれを使うか」を人が決めやすくするための選別支援です。

この機能がすること:
- 見た目がかなり似ている写真をまとめて、同じグループIDを付ける
- ぶれ・露出・コントラスト・解像度といった技術的な面を比べる
- その結果を ★★★ / ★★ / ★ で表示する

この機能がしないこと:
- 写真を消さない
- 使う / 使わないを勝手に切り替えない
- 上映順を勝手に変えない
- 写真の良し悪しそのものを決めない
  （構図・表情・思い出としての価値は判断していません）

外部通信は一切ありません。写真はPCの外へ出ません。
LLMやVLM、大きな画像embeddingモデルも使いません。
使うのは Pillow / NumPy / OpenCV だけです。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps


# ------------------------------------------------------------------
# 調整用の設定。GUIには出さず、ここだけ見れば直せるようにしておく。
# ------------------------------------------------------------------
@dataclass
class RecommendationSettings:
    """しきい値と重み。似ていないものを混ぜないよう、保守的な値にしてある。"""

    # 解析用に縮小するときの最大辺。元画像は読むだけで書き換えない。
    analysis_max_side: int = 768

    # --- 似ているかどうかの判定 ---
    # ほぼ同一と見なす（連写のほぼ完全な重複）
    duplicate_phash: int = 2
    duplicate_dhash: int = 4
    duplicate_correlation: float = 0.94

    # 通常の判定。この範囲なら撮影時刻を見なくても同じ場面と見なす。
    tight_phash: int = 8
    tight_dhash: int = 12
    tight_correlation: float = 0.82

    # 少し離れていても、色がよく一致していて撮影時刻も近いなら同じ場面と見なす。
    loose_phash: int = 12
    loose_dhash: int = 16
    loose_correlation: float = 0.92
    loose_time_window_seconds: float = 300.0

    # 色の分布がほぼ一致していれば、撮影時刻が無くても同じ場面と見なす。
    # 少しトリミングした・少し構図を変えた写真を取り逃がさないための逃げ道。
    recomposed_phash: int = 12
    recomposed_dhash: int = 16
    recomposed_correlation: float = 0.95

    # 縦横比がここまで違うと、同じ場面でもまとめない
    aspect_log_tolerance: float = 0.22

    # 連鎖で巨大なグループができるのを防ぐため、代表写真との距離も見る
    representative_phash: int = 16
    representative_correlation: float = 0.72
    # embeddingを使うときは、代表写真との見た目の近さも確認する
    representative_embedding: float = 0.80

    # --- 画像embedding（任意）---
    # 実データ614枚で類似度分布を測ってから決めた値。
    #   人物133枚 : 同じ場面 0.834〜0.951 / 別場面 0.843〜0.864
    #   犬330枚   : 同じ場面 0.885〜0.990 / 同じ犬の別の日 0.885〜0.887
    #   前撮り151 : 同じ場面 0.947〜0.984
    # 犬の「同じ犬・別の日」が0.887まで上がってくるので、
    # embedding単独で同じ場面と見なすのは0.90以上に限る。
    embedding_alone: float = 0.90
    # 0.90に届かない場合は、古典的な特徴とも一致していることを条件にする。
    embedding_with_classic: float = 0.85
    embedding_classic_phash: int = 20
    embedding_classic_correlation: float = 0.88

    # --- 第1層 Variant Family（同じ写真の別バージョン）---
    # カラー版と白黒版、コピー、軽い現像違いをひとまとめにする。
    # 色は見ない。明るさ・コントラストを正規化した「構造」だけを比べる。
    # 実データ614枚での実測:
    #   同じ写真の別現像 … 構造 0.9989〜1.0000 / 輪郭 0.9974〜1.0000
    #   構図が似た別カット … 構造 0.5836〜0.9778 / 輪郭 0.3271〜0.9530
    # 間がはっきり空いているので、その真ん中より上へ置く。
    # 別カットを「同じ写真」と誤るほうが危ないので、高めに取る。
    variant_structure: float = 0.995
    variant_edge: float = 0.99
    # 総当たりを減らすための足切り（ここを通ったものだけ構造を比べる）
    variant_phash: int = 12
    variant_dhash: int = 12

    # --- 第2層 Selection Candidate Group ---
    # グループへ入るには強い結びつき（is_similar）が1つ必要。
    # ただしグループの全員に同じ強さは求めず、
    # 「明らかに別物ではない」ことだけを確認する。
    # 犬の1秒差の連写（pHash 8・色 0.93）を助けつつ、
    # 別の撮影カット（pHash 24以上）は通さない値。
    compat_phash: int = 12
    compat_correlation: float = 0.90
    # embedding だけの結びつきを「相性」として認めるのは、色も明らかには違わないときだけ。
    # 寄った縦位置のように撮影意図が違う写真が、色相関 0.70〜0.71 のまま
    # embedding の近さだけで群へ入り込んでいた。
    # 0.72〜0.76 の間では結果が変わらないので、その中央を取る。
    compat_embedding_correlation: float = 0.74

    # --- 同じ連写の僅差を、撮影時刻で補う ---
    # 画角が少し動いただけで pHash が離れ、相性を僅かに外す組がある。
    # 撮影時刻がごく近く、画像の指標も3つのうち2つが近いときに限って補う。
    # ただし時刻だけでは絶対にまとめない（撮影時刻の無い写真では何もしない）。
    burst_seconds: float = 180.0
    burst_embedding: float = 0.85
    # 補ってよいのは、合併する2つの群をまたぐ組のうち、この割合以上に
    # すでに強い結びつきがあるときだけ。大半が同意している中の僅差だけを救い、
    # 姿勢の違う別カットどうしを時刻の近さだけでつながないため。
    burst_support: float = 0.75

    # --- 技術スコアの重み（合計が1になるよう正規化して使う） ---
    weight_sharpness: float = 0.40
    weight_exposure: float = 0.25
    weight_contrast: float = 0.13
    weight_resolution: float = 0.07
    weight_subject: float = 0.15

    # --- 星の境目 ---
    # はっきり問題があるときの頭打ち。ここだけは絶対値で見る。
    severe_sharpness: float = 15.0   # これ未満は強いぶれ → ★1
    severe_exposure: float = 40.0    # これ未満は露出が破綻 → ★1
    weak_sharpness: float = 35.0     # これ未満は★2まで
    weak_exposure: float = 60.0      # これ未満は★2まで

    # 似た写真のグループの中での順位づけ
    group_tie_gap: float = 0.15      # 1位との差がこれ以内なら同格（★★★）
    group_weak_gap: float = 8.0      # 1位とこれだけ離れたら★

    # 単独写真は比べる相手がいないので、控えめに決める。
    # 上位にいるだけでなく、全体の中央からはっきり離れているときだけ★★★にする。
    # みんな同じくらいきれいな写真ばかりのときに、わずかな差で
    # 「こちらがおすすめ」と言い切ってしまわないため。
    singleton_top_percentile: float = 55.0
    singleton_margin: float = 1.5


DEFAULT_SETTINGS = RecommendationSettings()

STAR_FULL = "★"   # ★
STAR_EMPTY = "☆"  # ☆
NOT_ANALYZED = "未解析"


@dataclass
class PhotoAnalysis:
    """1枚ぶんの解析結果。画像そのものは持たない。"""

    path: Path
    filename: str
    width: int = 0
    height: int = 0
    phash: int = 0
    dhash: int = 0
    taken_at: float | None = None

    sharpness_raw: float = 0.0
    sharpness_score: float = 0.0
    exposure_score: float = 0.0
    contrast_score: float = 0.0
    resolution_score: float = 0.0
    subject_score: float | None = None   # 検出できなかったときは None（減点しない）
    technical_score: float = 0.0

    group_id: str | None = None
    group_size: int = 1
    stars: int = 0
    note: str = ""

    # 比較用。配列なので settings JSON には出さない。
    hist: np.ndarray | None = field(default=None, repr=False)
    # 画像embedding（正規化済み）。無ければ従来どおりの判定になる。
    embedding: np.ndarray | None = field(default=None, repr=False)
    # 色に依存しない構造（縮小グレースケールと輪郭）。同じ写真かどうかの判定用。
    struct_sig: np.ndarray | None = field(default=None, repr=False)
    saturation: float = 0.0              # 代表を選ぶとき、カラー版を優先するため

    group_rule: str = ""
    variant_family: str = ""             # 同じ写真の別バージョンのまとまり
    variant_role: str = ""               # canonical / variant
    strong_edge: str = ""                # このグループへ入るきっかけになった関係
    compat_note: str = ""                # グループ内で最も弱かった相性
    group_embedding_similarity: float | None = None   # 同じ群でいちばん近い相手との値

    @property
    def aspect(self) -> float:
        return (self.width / self.height) if self.height else 1.0

    def star_text(self, use_symbol: bool = True) -> str:
        if self.stars <= 0:
            return ""
        return (STAR_FULL if use_symbol else "*") * self.stars


# ------------------------------------------------------------------
# 画像の特徴を取り出す小さな関数たち
# ------------------------------------------------------------------

def compute_phash(gray: np.ndarray) -> int:
    """知覚ハッシュ。32x32へ縮めてDCTし、低周波8x8の中央値で2値化する。

    DC成分（左上）は全体の明るさなので、明るさ違いに引きずられないよう外す。
    """
    small = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA)
    dct = cv2.dct(np.float32(small))
    block = dct[:8, :8].flatten()
    block = block[1:]                       # DC成分を除外して63個
    median = np.median(block)
    bits = block > median
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return value


def compute_dhash(gray: np.ndarray) -> int:
    """差分ハッシュ。9x8へ縮めて、横に隣り合う画素の大小で64bitにする。"""
    small = cv2.resize(gray, (9, 8), interpolation=cv2.INTER_AREA).astype(np.int16)
    bits = (small[:, 1:] > small[:, :-1]).flatten()
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return value


def compute_histogram(bgr: np.ndarray) -> np.ndarray:
    """色の分布。色相と彩度の2次元ヒストグラムを正規化して返す。

    全然違う場面をハッシュだけでまとめてしまうのを防ぐために使う。
    """
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [30, 32], [0, 180, 0, 256])
    cv2.normalize(hist, hist, 0, 1, cv2.NORM_MINMAX)
    return hist


STRUCT_SIDE = 64          # 構造を比べる大きさ。これ以上細かくしても判定は変わらない


def compute_structure(gray: np.ndarray) -> np.ndarray:
    """色に依存しない「構造」を取り出す。

    縮小グレースケールと、その輪郭の強さを、それぞれ平均0・分散1にする。
    正規化してあるので、明るさやコントラストを変えただけでは値が動かない。
    白黒版とカラー版が同じ写真かどうかを、色を見ずに判定するために使う。
    float16 で持つ（1枚あたり16KB）。フル解像度の画像は一切持たない。
    """
    small = cv2.resize(gray, (STRUCT_SIDE, STRUCT_SIDE),
                       interpolation=cv2.INTER_AREA).astype(np.float32)
    gx = cv2.Sobel(small, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(small, cv2.CV_32F, 0, 1, ksize=3)
    edge = cv2.magnitude(gx, gy)

    def unit(a: np.ndarray) -> np.ndarray:
        a = a - float(a.mean())
        scale = float(np.sqrt(float((a * a).mean())))
        return a / scale if scale > 1e-6 else a

    return np.concatenate([unit(small).ravel(),
                           unit(edge).ravel()]).astype(np.float16)


def structure_match(a: "PhotoAnalysis", b: "PhotoAnalysis") -> tuple[float, float]:
    """(構造の一致度, 輪郭の一致度)。どちらも1.0で完全一致。"""
    if a.struct_sig is None or b.struct_sig is None:
        return 0.0, 0.0
    half = STRUCT_SIDE * STRUCT_SIDE
    x = a.struct_sig.astype(np.float32)
    y = b.struct_sig.astype(np.float32)
    return (float(np.dot(x[:half], y[:half]) / half),
            float(np.dot(x[half:], y[half:]) / half))


def is_same_photo_variant(a: "PhotoAnalysis", b: "PhotoAnalysis",
                          cfg: "RecommendationSettings") -> bool:
    """同じ写真の別バージョン（カラー/白黒・コピー・軽い現像違い）か。

    ポーズや表情が少しでも違う別カットは、ここでは同じにしない。
    まとめ過ぎるほうが危ないので、はっきり一致するときだけ True にする。
    """
    if a.struct_sig is None or b.struct_sig is None:
        return False
    # 縦横比が違えば別（同じ写真を切り直したものは Candidate Group 側で拾う）
    if a.aspect > 0 and b.aspect > 0:
        if abs(np.log(a.aspect / b.aspect)) > cfg.aspect_log_tolerance:
            return False
    # 速度のための足切り。ハッシュが遠ければ構造まで見ない。
    if (hamming(a.phash, b.phash) > cfg.variant_phash
            or hamming(a.dhash, b.dhash) > cfg.variant_dhash):
        return False
    g, e = structure_match(a, b)
    return g >= cfg.variant_structure and e >= cfg.variant_edge


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


# ------------------------------------------------------------------
# 技術的な品質
# ------------------------------------------------------------------

def score_sharpness(gray: np.ndarray) -> tuple[float, float]:
    """ぶれの手がかり。ラプラシアンの分散を見る。

    画像サイズで値が変わるので、解析用に縮めた画像で測る。
    これだけで写真の良し悪しは決めない。
    """
    raw = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    # 20付近＝かなりぼけている / 600付近＝しっかりしている、として対数で0-100へ
    low, high = 20.0, 600.0
    if raw <= 0:
        return raw, 0.0
    t = (np.log(raw) - np.log(low)) / (np.log(high) - np.log(low))
    return raw, float(np.clip(t, 0.0, 1.0) * 100.0)


def score_exposure(gray: np.ndarray) -> float:
    """黒つぶれ・白飛び・平均の明るさ。

    少し暗い/明るいだけでは下げない。意図的なローキー（暗く締めた作品）や
    ハイキーの写真があるため、黒が多いことそのものは問題にしない。

    見分け方は明暗の幅（標準偏差）。
    意図的なローキーは黒が広くても明暗の幅が保たれているが、
    本当に露出を外した写真は幅そのものが失われる。
    白飛びは後から取り返せないので、こちらは幅に関係なく下げる。
    """
    total = gray.size
    dark = float((gray < 8).sum()) / total
    bright = float((gray > 247).sum()) / total
    mean = float(gray.mean())
    std = float(gray.std())

    score = 100.0

    # 白飛び。ハイライトは戻せないので、広ければしっかり下げる。
    score -= max(0.0, bright - 0.02) * 220.0

    # 黒つぶれ。明暗の幅が残っていれば、意図的なローキーとみなして大目に見る。
    dark_tolerance = 0.30 if std >= 35.0 else 0.05
    score -= max(0.0, dark - dark_tolerance) * 200.0

    # 平均輝度は極端なときだけ
    if mean < 30.0:
        score -= (30.0 - mean) * 2.0
    elif mean > 225.0:
        score -= (mean - 225.0) * 2.0

    # 明暗の幅そのものが失われている＝露出を外している
    if std < 20.0:
        score -= (20.0 - std) * 3.0

    return float(np.clip(score, 0.0, 100.0))


def score_contrast(gray: np.ndarray) -> float:
    """明暗の幅。低すぎるときだけ下げる。高ければ良いとは考えない。"""
    std = float(gray.std())
    if std >= 45.0:
        return 100.0
    return float(np.clip(std / 45.0, 0.0, 1.0) * 100.0)


def score_resolution(width: int, height: int) -> float:
    """極端に小さいときだけ軽く下げる。大きいほど高評価にはしない。"""
    pixels = width * height
    if pixels >= 2_000_000:
        return 100.0
    if pixels <= 200_000:
        return 40.0
    t = (pixels - 200_000) / (2_000_000 - 200_000)
    return float(40.0 + t * 60.0)


def score_subject(detection, width: int, height: int) -> float | None:
    """人物の顔や犬の写り方。検出できなかったときは None を返す。

    None は「評価に使わない」という意味で、減点ではない。
    横顔・後ろ姿・遠景・風景写真がいくらでもあるため、
    「顔が見つからない＝低評価」には絶対にしない。
    犬の後ろ姿も同じで、顔が見えないことを理由に下げない。
    """
    if detection is None or not getattr(detection, "has_target", False):
        return None
    tx, ty = detection.target_x, detection.target_y
    if tx is None or ty is None:
        return None

    score = 100.0
    # 目標点が画面のふちに寄りすぎている＝被写体が切れている可能性
    edge = min(tx, 1.0 - tx, ty, 1.0 - ty)
    if edge < 0.06:
        score -= 22.0
    elif edge < 0.12:
        score -= 8.0
    return float(np.clip(score, 0.0, 100.0))


def combine_technical(a: PhotoAnalysis, cfg: RecommendationSettings) -> float:
    """重み付き平均。被写体の項目は、検出できたときだけ使う。"""
    parts = [
        (cfg.weight_sharpness, a.sharpness_score),
        (cfg.weight_exposure, a.exposure_score),
        (cfg.weight_contrast, a.contrast_score),
        (cfg.weight_resolution, a.resolution_score),
    ]
    if a.subject_score is not None:
        parts.append((cfg.weight_subject, a.subject_score))
    total_w = sum(w for w, _ in parts)
    if total_w <= 0:
        return 0.0
    return float(sum(w * s for w, s in parts) / total_w)


# ------------------------------------------------------------------
# 1枚ぶんの解析
# ------------------------------------------------------------------

def read_exif_time(im: Image.Image) -> float | None:
    """撮影日時。無くても機能するので、あれば使う程度の扱い。"""
    try:
        exif = im.getexif()
    except Exception:
        return None
    for tag in (36867, 36868, 306):   # DateTimeOriginal, DateTimeDigitized, DateTime
        value = exif.get(tag)
        if not value:
            continue
        try:
            import time as _time
            return _time.mktime(_time.strptime(str(value).strip(), "%Y:%m:%d %H:%M:%S"))
        except Exception:
            continue
    return None


def analyze_photo(path: Path, cfg: RecommendationSettings,
                  detector=None) -> PhotoAnalysis:
    """写真1枚を解析する。元ファイルは読むだけで、一切書き換えない。

    detector を渡すと、同じ縮小画像で人物の顔と犬も見る（任意）。
    モデルが無ければ渡さなくてよく、その場合も残りの指標だけで動く。
    """
    result = PhotoAnalysis(path=path, filename=path.name)

    with Image.open(path) as im:
        result.taken_at = read_exif_time(im)
        # 人が見る向きへ直してから解析する
        upright = ImageOps.exif_transpose(im).convert("RGB")
        result.width, result.height = upright.size
        # 解析用に縮小する。全枚をフル解像度で持たない。
        work = upright.copy()
        work.thumbnail((cfg.analysis_max_side, cfg.analysis_max_side),
                       Image.Resampling.LANCZOS)
        rgb = np.asarray(work, dtype=np.uint8)

    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)

    result.phash = compute_phash(gray)
    result.dhash = compute_dhash(gray)
    result.hist = compute_histogram(bgr)
    result.struct_sig = compute_structure(gray)
    # 彩度の平均。カラー版と白黒版のどちらを代表にするか決めるためだけに使う。
    result.saturation = float(cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)[:, :, 1].mean())

    result.sharpness_raw, result.sharpness_score = score_sharpness(gray)
    result.exposure_score = score_exposure(gray)
    result.contrast_score = score_contrast(gray)
    result.resolution_score = score_resolution(result.width, result.height)
    detection = None
    if detector is not None and getattr(detector, "available", False):
        # 顔・犬の位置は正規化座標なので、縮小した画像で調べても同じ意味になる。
        # 検出できなくても減点しない（横顔・後ろ姿・風景がいくらでもあるため）。
        try:
            detection = detector.detect(path.name, rgb)
        except Exception:
            detection = None
    result.subject_score = score_subject(detection, result.width, result.height)
    result.technical_score = combine_technical(result, cfg)
    return result


# ------------------------------------------------------------------
# 似ているかどうか
# ------------------------------------------------------------------

def correlation(a: PhotoAnalysis, b: PhotoAnalysis) -> float:
    if a.hist is None or b.hist is None:
        return 0.0
    return float(cv2.compareHist(a.hist, b.hist, cv2.HISTCMP_CORREL))


def time_close(a: PhotoAnalysis, b: PhotoAnalysis, window: float) -> bool:
    """撮影時刻が近いか。どちらかに時刻が無ければ判断材料にしない。"""
    if a.taken_at is None or b.taken_at is None:
        return False
    return abs(a.taken_at - b.taken_at) <= window


def is_same_scene_by_embedding(a: PhotoAnalysis, b: PhotoAnalysis,
                               cfg: RecommendationSettings) -> bool:
    """embeddingを使って「少し変化した同じ場面」を拾う。

    embeddingだけで決めない。同じ被写体が別の日に写っているだけの写真も
    見た目が近くなるので、非常に近いとき以外は古典的な特徴の一致も求める。
    """
    if a.embedding is None or b.embedding is None:
        return False

    # ここでは縦横比で門前払いしない。
    # embeddingは正方形へ中央切り出ししてから見ているので、
    # 同じ場面を縦で撮ったものと横で撮ったものも近い値になる。
    # 実データでも、同じ場面の縦横違いが cos 0.90〜0.95 で並んでいた。
    cos = float(np.dot(a.embedding, b.embedding))
    if cos >= cfg.embedding_alone:
        return True
    if cos < cfg.embedding_with_classic:
        return False
    # ここから下は、古典的な特徴も揃っているときだけ認める。
    # pHashは縦横比が大きく違うと当てにならないので、そこだけ従来のゲートを使う。
    if a.aspect > 0 and b.aspect > 0:
        if abs(np.log(a.aspect / b.aspect)) > cfg.aspect_log_tolerance:
            return False
    return (hamming(a.phash, b.phash) <= cfg.embedding_classic_phash
            and correlation(a, b) >= cfg.embedding_classic_correlation)


def is_similar_classic(a: PhotoAnalysis, b: PhotoAnalysis,
                       cfg: RecommendationSettings) -> bool:
    """従来からある判定。ほぼ同じ写真を確実に見つけるためのもの。

    見逃しよりも、違う場面をまとめてしまう方を避けたいので保守的にする。
    embeddingの有無に関係なく、この判定は変わらない。
    """
    # 縦横比が大きく違うものはまとめない（軽いトリミング程度は許容する）
    if a.aspect > 0 and b.aspect > 0:
        if abs(np.log(a.aspect / b.aspect)) > cfg.aspect_log_tolerance:
            return False

    pd = hamming(a.phash, b.phash)
    dd = hamming(a.dhash, b.dhash)
    corr = correlation(a, b)

    # ほぼ完全に同じ画像は確実に同じグループへ
    if (pd <= cfg.duplicate_phash and dd <= cfg.duplicate_dhash
            and corr >= cfg.duplicate_correlation):
        return True

    if (pd <= cfg.tight_phash and dd <= cfg.tight_dhash
            and corr >= cfg.tight_correlation):
        return True

    # 少し離れていても、色がよく一致していて撮影時刻も近ければ同じ場面とみなす
    if (pd <= cfg.loose_phash and dd <= cfg.loose_dhash
            and corr >= cfg.loose_correlation
            and time_close(a, b, cfg.loose_time_window_seconds)):
        return True

    # 少しトリミングした・少し構図を変えた場合。
    # 色の分布がほぼ一致していることを条件にするので、別の場面は入ってこない。
    if (pd <= cfg.recomposed_phash and dd <= cfg.recomposed_dhash
            and corr >= cfg.recomposed_correlation):
        return True

    return False


def is_similar(a: PhotoAnalysis, b: PhotoAnalysis,
               cfg: RecommendationSettings) -> bool:
    """同じ場面で撮った写真らしいか。

    従来からある「ほぼ同じ写真」の判定に加えて、embeddingがあれば
    「少し変化した同じ場面」も拾う。
    """
    if is_similar_classic(a, b, cfg):
        return True
    return is_same_scene_by_embedding(a, b, cfg)


# ------------------------------------------------------------------
# グループ分け
# ------------------------------------------------------------------

def _find(parent: list[int], i: int) -> int:
    while parent[i] != i:
        parent[i] = parent[parent[i]]
        i = parent[i]
    return i


def group_photos_legacy(items: list[PhotoAnalysis],
                        cfg: RecommendationSettings) -> None:
    """従来のまとめ方（embedding を使わないときはこちら）。

    素直な union-find でまとめたあと、代表写真から離れすぎたものを外す。
    v0.1.1 までと同じ結果になるよう、ここは変えない。
    """
    # 同じ写真を使い回している場合に備えて、二層のまとめ方の跡を消しておく
    for a in items:
        a.variant_family = a.variant_role = a.strong_edge = a.compat_note = ""
    n = len(items)
    parent = list(range(n))
    for i in range(n):
        for j in range(i + 1, n):
            if _find(parent, i) == _find(parent, j):
                continue
            if is_similar(items[i], items[j], cfg):
                parent[_find(parent, i)] = _find(parent, j)

    buckets: dict[int, list[int]] = {}
    for i in range(n):
        buckets.setdefault(_find(parent, i), []).append(i)

    # 代表写真（グループ内で他との距離の合計が最小のもの）から離れすぎたら外す
    refined: list[list[int]] = []
    for members in buckets.values():
        if len(members) <= 2:
            refined.append(members)
            continue
        best, best_cost = members[0], None
        for cand in members:
            cost = sum(hamming(items[cand].phash, items[m].phash)
                       for m in members if m != cand)
            if best_cost is None or cost < best_cost:
                best, best_cost = cand, cost
        kept, dropped = [], []
        for m in members:
            if m == best:
                kept.append(m)
                continue
            near_classic = (
                hamming(items[best].phash, items[m].phash) <= cfg.representative_phash
                and correlation(items[best], items[m]) >= cfg.representative_correlation
            )
            near_embedding = (
                items[best].embedding is not None
                and items[m].embedding is not None
                and float(np.dot(items[best].embedding, items[m].embedding))
                >= cfg.representative_embedding
            )
            if near_classic or near_embedding:
                kept.append(m)
            else:
                dropped.append(m)
        refined.append(kept)
        for m in dropped:
            refined.append([m])

    # 一覧での並び順が早いグループから G01, G02 ... と振る
    refined = [m for m in refined if m]
    refined.sort(key=lambda members: min(members))
    counter = 0
    for members in refined:
        if len(members) < 2:
            idx = members[0]
            items[idx].group_id = None
            items[idx].group_size = 1
            continue
        counter += 1
        gid = f"G{counter:02d}"
        for idx in members:
            items[idx].group_id = gid
            items[idx].group_size = len(members)
        # このグループが、従来の判定だけで成立するか（embeddingが効いたか）を記録する
        classic_only = any(
            is_similar_classic(items[i], items[j], cfg)
            for i in members for j in members if i < j
        )
        for idx in members:
            items[idx].group_rule = "classic" if classic_only else "embedding"


# ------------------------------------------------------------------
# 写真選別のためのまとめ方（二層）
#
#   第1層 Variant Family      … 同じ写真の別バージョン
#   第2層 Candidate Group     … どれか1枚を選べばよい、代替候補の群
#
# 第1層を先に畳むのは、白黒版の色ヒストグラムが元写真と全く違うため、
# そのままだと第2層の相性チェックを通れず、正しい候補群を割ってしまうから。
# 第2層では各 Variant Family の代表1枚だけを使って判定し、
# 群が決まってから家族全員をそこへ入れる。
# ------------------------------------------------------------------

# 結びつきの強さの順。確かなものから先に固めることで、
# embedding 由来の弱い関係が先にグループの形を決めてしまうのを防ぐ。
EDGE_PRIORITY = {
    "classic:duplicate": 5,
    "classic:tight": 4,
    "classic:recomposed": 3,
    "classic:loose": 2,
    "embedding:hybrid": 1,      # 古典的な特徴の裏付けがあるので alone より先
    "embedding:alone": 0,
}


def edge_kind(a: PhotoAnalysis, b: PhotoAnalysis,
              cfg: RecommendationSettings) -> str:
    """2枚を直接結んでいる関係の名前。結ばないときは空文字。

    判定の中身は is_similar と同じ。どの規則で通ったかを残すためだけに分ける。
    """
    aspect_ok = True
    if a.aspect > 0 and b.aspect > 0:
        aspect_ok = abs(np.log(a.aspect / b.aspect)) <= cfg.aspect_log_tolerance

    pd = hamming(a.phash, b.phash)
    dd = hamming(a.dhash, b.dhash)
    corr = correlation(a, b)

    if aspect_ok:
        if (pd <= cfg.duplicate_phash and dd <= cfg.duplicate_dhash
                and corr >= cfg.duplicate_correlation):
            return "classic:duplicate"
        if (pd <= cfg.tight_phash and dd <= cfg.tight_dhash
                and corr >= cfg.tight_correlation):
            return "classic:tight"
        if (pd <= cfg.loose_phash and dd <= cfg.loose_dhash
                and corr >= cfg.loose_correlation
                and time_close(a, b, cfg.loose_time_window_seconds)):
            return "classic:loose"
        if (pd <= cfg.recomposed_phash and dd <= cfg.recomposed_dhash
                and corr >= cfg.recomposed_correlation):
            return "classic:recomposed"

    if a.embedding is None or b.embedding is None:
        return ""
    cos = float(np.dot(a.embedding, b.embedding))
    if cos >= cfg.embedding_alone:
        return "embedding:alone"
    if cos >= cfg.embedding_with_classic and aspect_ok and \
            pd <= cfg.embedding_classic_phash and corr >= cfg.embedding_classic_correlation:
        return "embedding:hybrid"
    return ""


def is_compatible(a: PhotoAnalysis, b: PhotoAnalysis,
                  cfg: RecommendationSettings, kind: str | None = None) -> bool:
    """「明らかに別物ではない」かどうか。グループの全員に求める緩い条件。

    グループへ入るには強い結びつきが1つ必要だが、
    そこにいる全員と同じ強さで結ばれている必要はない。
    全員に強い結びつきを求めると、1秒差の連写のように
    僅差で外れた1組のせいで正しい群まで割れてしまう。

    ただし embedding だけの結びつきは、それだけでは相性とみなさない。
    見た目の雰囲気が近くても、色も構図も違う写真はいくらでもあるため。
    """
    if kind is None:
        kind = edge_kind(a, b, cfg)
    corr = correlation(a, b)
    if kind and kind != "embedding:alone":
        return True               # 古典的な特徴の裏付けがある結びつき
    if kind == "embedding:alone" and corr >= cfg.compat_embedding_correlation:
        return True
    return (hamming(a.phash, b.phash) <= cfg.compat_phash
            and corr >= cfg.compat_correlation)


def is_burst_neighbor(a: PhotoAnalysis, b: PhotoAnalysis,
                      cfg: RecommendationSettings) -> bool:
    """同じ連写の中で、画角が少し動いただけの組らしいか。

    撮影時刻がごく近く、かつ embedding・pHash・色のうち2つ以上が近いこと。
    時刻だけでは決めない。撮影時刻が無い写真では常に False。
    """
    if a.taken_at is None or b.taken_at is None:
        return False
    if abs(a.taken_at - b.taken_at) > cfg.burst_seconds:
        return False
    close_embedding = (a.embedding is not None and b.embedding is not None
                       and float(np.dot(a.embedding, b.embedding)) >= cfg.burst_embedding)
    close_phash = hamming(a.phash, b.phash) <= cfg.compat_phash
    close_color = correlation(a, b) >= cfg.compat_correlation
    return (close_embedding + close_phash + close_color) >= 2


def canonical_key(a: PhotoAnalysis) -> tuple:
    """Variant Family の代表を選ぶ手がかり。カラー版を白黒版より優先する。"""
    is_color = 1 if a.saturation >= 8.0 else 0
    return (is_color, comparison_value(a))


def build_variant_families(items: list[PhotoAnalysis],
                           cfg: RecommendationSettings) -> tuple[list[list[int]], list[int]]:
    """第1層。同じ写真の別バージョンをまとめ、代表を1枚決める。

    返り値は (家族ごとの添字リスト, 家族ごとの代表の添字)。
    元ファイルには一切手を触れない。白黒版も消さないし、使用OFFにもしない。
    """
    n = len(items)
    parent = list(range(n))
    for i in range(n):
        for j in range(i + 1, n):
            if _find(parent, i) == _find(parent, j):
                continue
            if is_same_photo_variant(items[i], items[j], cfg):
                parent[_find(parent, i)] = _find(parent, j)

    buckets: dict[int, list[int]] = {}
    for i in range(n):
        buckets.setdefault(_find(parent, i), []).append(i)
    families = sorted(buckets.values(), key=lambda m: min(m))

    reps: list[int] = []
    counter = 0
    for members in families:
        rep = max(members, key=lambda i: canonical_key(items[i]))
        reps.append(rep)
        if len(members) > 1:
            counter += 1
            vid = f"V{counter:02d}"
            for i in members:
                items[i].variant_family = vid
                items[i].variant_role = "canonical" if i == rep else "variant"
        else:
            items[members[0]].variant_family = ""
            items[members[0]].variant_role = ""
    return families, reps


def group_candidates(items: list[PhotoAnalysis],
                     cfg: RecommendationSettings) -> None:
    """第2層。代替候補の群を作る。items を直接書き換える。

    連結成分（A≈B, B≈C なら A と C も同じ）は使わない。
    A と C が互いに代替候補でないなら、つなげてはいけないため。
    代わりに、グループへ入るときに「既に居る全員と矛盾しないか」を確かめる。
    """
    families, reps = build_variant_families(items, cfg)
    R = len(families)

    kinds: dict[tuple[int, int], str] = {}
    compat = [[True] * R for _ in range(R)]
    burst = [[False] * R for _ in range(R)]
    edges = []
    for x in range(R):
        for y in range(x + 1, R):
            a, b = items[reps[x]], items[reps[y]]
            kind = edge_kind(a, b, cfg)
            kinds[(x, y)] = kinds[(y, x)] = kind
            ok = is_compatible(a, b, cfg, kind)
            compat[x][y] = compat[y][x] = ok
            if not ok:
                near = is_burst_neighbor(a, b, cfg)
                burst[x][y] = burst[y][x] = near
            if kind:
                cos = 0.0
                if a.embedding is not None and b.embedding is not None:
                    cos = float(np.dot(a.embedding, b.embedding))
                edges.append((EDGE_PRIORITY.get(kind, 0), cos, x, y))
    # 確かな結びつきから順に固める
    edges.sort(key=lambda e: (-e[0], -e[1]))

    def can_merge(gx: int, gy: int, x: int, y: int) -> bool:
        cross = [(p, q) for p in cluster[gx] for q in cluster[gy]]
        # またぐ組のうち、すでに強い結びつきがある割合
        support = sum(1 for p, q in cross if kinds[(p, q)]) / len(cross)
        for p, q in cross:
            if {p, q} == {x, y}:
                continue          # 合併のきっかけになった結びつきそのもの
            if compat[p][q]:
                continue
            if burst[p][q] and support >= cfg.burst_support:
                continue          # 大半が同意している中の、同じ連写の僅差
            return False
        return True

    cluster = {x: [x] for x in range(R)}
    where = list(range(R))
    changed = True
    while changed:
        changed = False
        for _, _, x, y in edges:
            gx, gy = where[x], where[y]
            if gx == gy:
                continue
            if not can_merge(gx, gy, x, y):
                continue
            cluster[gx] = cluster[gx] + cluster[gy]
            for q in cluster[gy]:
                where[q] = gx
            del cluster[gy]
            changed = True
            break

    # 家族を展開して、一覧での並び順が早いグループから G01, G02 ... と振る
    expanded = []
    for members in cluster.values():
        photos = [i for x in members for i in families[x]]
        expanded.append((sorted(members), sorted(photos)))
    expanded.sort(key=lambda t: min(t[1]))

    counter = 0
    for fams, photos in expanded:
        if len(photos) < 2:
            a = items[photos[0]]
            a.group_id, a.group_size = None, 1
            a.group_rule = a.strong_edge = a.compat_note = ""
            continue
        counter += 1
        gid = f"G{counter:02d}"
        # このグループを成り立たせている関係のうち、いちばん確かなもの
        present = [kinds[(x, y)] for i, x in enumerate(fams) for y in fams[i + 1:]
                   if kinds[(x, y)]]
        best = max(present, key=lambda k: EDGE_PRIORITY.get(k, 0)) if present else "variant"
        # グループ内でいちばん弱かった相性（どこまで離れた相手を許したか）
        worst_ph, worst_corr = 0, 1.0
        for i, x in enumerate(fams):
            for y in fams[i + 1:]:
                a, b = items[reps[x]], items[reps[y]]
                worst_ph = max(worst_ph, hamming(a.phash, b.phash))
                worst_corr = min(worst_corr, correlation(a, b))
        note = f"pH<={worst_ph} 色>={worst_corr:.2f}"
        for i in photos:
            items[i].group_id = gid
            items[i].group_size = len(photos)
            items[i].group_rule = best
            items[i].compat_note = note
        for x in fams:
            own = [kinds[(x, y)] for y in fams if y != x and kinds[(min(x, y), max(x, y))]]
            strongest = (max(own, key=lambda k: EDGE_PRIORITY.get(k, 0))
                         if own else "variant-only")
            for i in families[x]:
                items[i].strong_edge = strongest


def group_photos(items: list[PhotoAnalysis],
                 cfg: RecommendationSettings) -> None:
    """似た写真へ同じグループIDを振る。items を直接書き換える。

    embedding があるときは、写真選別のための二層のまとめ方を使う。
    embedding が無いときは、v0.1.1 までと同じ従来のまとめ方にする。
    """
    if any(a.embedding is not None for a in items):
        group_candidates(items, cfg)
    else:
        group_photos_legacy(items, cfg)

    # あとで CSV へ出すための、同じグループでいちばん近い相手との類似度
    by_group: dict[str, list[PhotoAnalysis]] = {}
    for a in items:
        if a.group_id:
            by_group.setdefault(a.group_id, []).append(a)
    for members in by_group.values():
        for a in members:
            best = None
            for b in members:
                if b is a or a.embedding is None or b.embedding is None:
                    continue
                cos = float(np.dot(a.embedding, b.embedding))
                best = cos if best is None else max(best, cos)
            a.group_embedding_similarity = best


# ------------------------------------------------------------------
# 星
# ------------------------------------------------------------------

def comparison_value(a: PhotoAnalysis) -> float:
    """グループ内で順位を付けるための値。

    technical_score は 0〜100 で頭打ちになるので、きれいな写真ばかりだと
    横並びになってしまう。飽和しない鮮鋭度をわずかに足して、
    同じグループの中でどれが一番くっきりしているかを見分けられるようにする。
    """
    bonus = 0.0
    if a.sharpness_raw > 0:
        bonus = max(0.0, float(np.log10(a.sharpness_raw) - np.log10(600.0))) * 6.0
    return a.technical_score + bonus


def star_cap(a: PhotoAnalysis, cfg: RecommendationSettings) -> int:
    """はっきりした問題があるときの上限。ここだけは絶対値で判断する。

    少し暗い・少し明るい程度では下げない。
    潰れている・飛んでいる・強くぶれている場合だけ効かせる。
    """
    if a.sharpness_score < cfg.severe_sharpness or a.exposure_score < cfg.severe_exposure:
        return 1
    if a.sharpness_score < cfg.weak_sharpness or a.exposure_score < cfg.weak_exposure:
        return 2
    return 3


def assign_stars(items: list[PhotoAnalysis],
                 cfg: RecommendationSettings) -> None:
    """星を決める。写真の価値ではなく、選ぶときの手がかりとしての目安。

    ★★★ おすすめ候補（同じグループの中で技術的に有力）
    ★★   十分使える
    ★     同じグループの他を優先した方がよさそう、または技術的な問題がある

    似た写真のグループがあるときは、その中での相対比較を重視する。
    単独の写真は比べる相手がいないので、控えめに決める。
    """
    if not items:
        return

    values = {id(a): comparison_value(a) for a in items}
    ordered = sorted(values.values())
    median = ordered[len(ordered) // 2]

    def percentile_of(value: float) -> float:
        if len(ordered) <= 1:
            return 100.0
        below = sum(1 for v in ordered if v < value)
        return 100.0 * below / (len(ordered) - 1)

    groups: dict[str, list[PhotoAnalysis]] = {}
    for a in items:
        if a.group_id:
            groups.setdefault(a.group_id, []).append(a)
    group_best = {gid: max(values[id(x)] for x in v) for gid, v in groups.items()}

    for a in items:
        cap = star_cap(a, cfg)
        value = values[id(a)]

        if a.group_id:
            gap = group_best[a.group_id] - value
            if gap <= cfg.group_tie_gap:
                stars, note = 3, "同じグループの中で有力"
            elif gap >= cfg.group_weak_gap:
                stars, note = 1, "同じグループの他を優先した方がよさそう"
            else:
                stars, note = 2, "同じグループの中で十分使える"
        elif (percentile_of(value) >= cfg.singleton_top_percentile
                and value - median >= cfg.singleton_margin):
            stars, note = 3, "単独・技術的に問題なし"
        else:
            stars, note = 2, "単独・十分使える"

        if cap < stars:
            stars = cap
            note = ("技術的な問題あり（ぶれ・露出）" if cap == 1 else "技術的にやや不利")
        a.stars, a.note = stars, note


# ------------------------------------------------------------------
# まとめて実行
# ------------------------------------------------------------------

def analyze_photos(paths, cfg: RecommendationSettings | None = None,
                   detector=None, embeddings: dict | None = None,
                   progress=None, should_stop=None, phase=None,
                   cache: dict | None = None) -> list[PhotoAnalysis]:
    """一覧の全写真を解析して、グループIDと星を付けて返す。

    progress(done, total, filename) が呼ばれる。
    should_stop() が True を返したら、その時点で打ち切って空リストを返す。
    cache は (解決済みパス, サイズ, 更新時刻) をキーにした使い回し用。
    """
    cfg = cfg or DEFAULT_SETTINGS
    paths = list(paths)
    total = len(paths)
    items: list[PhotoAnalysis] = []

    for i, path in enumerate(paths):
        if should_stop is not None and should_stop():
            return []
        if progress is not None:
            progress(i, total, path.name)
        key = None
        try:
            st = path.stat()
            key = (str(path.resolve()), st.st_size, st.st_mtime_ns)
        except Exception:
            key = None
        if cache is not None and key is not None and key in cache:
            items.append(cache[key])
            continue
        try:
            item = analyze_photo(path, cfg, detector)
        except Exception as e:
            item = PhotoAnalysis(path=path, filename=path.name,
                                 note=f"解析できません: {type(e).__name__}")
        if cache is not None and key is not None:
            cache[key] = item
        items.append(item)

    if should_stop is not None and should_stop():
        return []
    if progress is not None:
        progress(total, total, "")

    if embeddings:
        for a in items:
            a.embedding = embeddings.get(str(a.path))

    if phase is not None:
        phase("似た写真をまとめています")
    usable = [a for a in items if a.hist is not None]
    group_photos(usable, cfg)
    assign_stars(usable, cfg)
    return items


def csv_header() -> str:
    return ("filename,variant_family,variant_role,candidate_group,stars,"
            "technical_score,sharpness_raw,sharpness_score,"
            "exposure_score,contrast_score,resolution_score,subject_score,"
            "phash,dhash,width,height,embedding_enabled,embedding_similarity,"
            "group_rule,strong_edge,compatibility_result,note")


def to_csv_row(a: PhotoAnalysis) -> str:
    def s(v, nd=2):
        if v is None:
            return ""
        return f"{v:.{nd}f}" if isinstance(v, float) else str(v)
    return ",".join([
        a.filename.replace(",", " "),
        a.variant_family,
        a.variant_role,
        a.group_id or "",
        str(a.stars),
        s(a.technical_score), s(a.sharpness_raw), s(a.sharpness_score),
        s(a.exposure_score), s(a.contrast_score), s(a.resolution_score),
        s(a.subject_score),
        format(a.phash, "016x"), format(a.dhash, "016x"),
        str(a.width), str(a.height),
        "1" if a.embedding is not None else "0",
        s(getattr(a, "group_embedding_similarity", None), 4),
        a.group_rule,
        a.strong_edge,
        a.compat_note.replace(",", ";"),
        a.note.replace(",", ";"),
    ])
