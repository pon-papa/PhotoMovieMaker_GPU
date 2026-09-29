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
    group_rule: str = ""

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


def group_photos(items: list[PhotoAnalysis],
                 cfg: RecommendationSettings) -> None:
    """似た写真へ同じグループIDを振る。items を直接書き換える。

    素直な union-find でまとめたあと、代表写真から離れすぎたものを外す。
    A≈B, B≈C でも A と C が全然違う、という連鎖で
    巨大なグループができてしまうのを防ぐため。
    """
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
# 星
# ------------------------------------------------------------------

def comparison_value(a: PhotoAnalysis) -> float:
    """グループ内で順位を付けるための値。

    technical_score は 0〜100 で頭打ちになるので、きれいな写真ばかりだと
    横並びになってしまう。飽和しない鮮鋭度をわずかに足して、
    同じ場面の中でどれが一番くっきりしているかを見分けられるようにする。
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

    ★★★ おすすめ候補（同じ場面の中で技術的に有力）
    ★★   十分使える
    ★     同じ場面の他を優先した方がよさそう、または技術的な問題がある

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
                stars, note = 3, "同じ場面の中で有力"
            elif gap >= cfg.group_weak_gap:
                stars, note = 1, "同じ場面の他を優先した方がよさそう"
            else:
                stars, note = 2, "同じ場面の中で十分使える"
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
                   progress=None, should_stop=None,
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

    usable = [a for a in items if a.hist is not None]
    group_photos(usable, cfg)
    assign_stars(usable, cfg)
    return items


def csv_header() -> str:
    return ("filename,group_id,stars,technical_score,sharpness_raw,sharpness_score,"
            "exposure_score,contrast_score,resolution_score,subject_score,"
            "phash,dhash,width,height,embedding_enabled,group_rule,note")


def to_csv_row(a: PhotoAnalysis) -> str:
    def s(v, nd=2):
        if v is None:
            return ""
        return f"{v:.{nd}f}" if isinstance(v, float) else str(v)
    return ",".join([
        a.filename.replace(",", " "),
        a.group_id or "",
        str(a.stars),
        s(a.technical_score), s(a.sharpness_raw), s(a.sharpness_score),
        s(a.exposure_score), s(a.contrast_score), s(a.resolution_score),
        s(a.subject_score),
        format(a.phash, "016x"), format(a.dhash, "016x"),
        str(a.width), str(a.height),
        "1" if a.embedding is not None else "0",
        a.group_rule,
        a.note.replace(",", ";"),
    ])
