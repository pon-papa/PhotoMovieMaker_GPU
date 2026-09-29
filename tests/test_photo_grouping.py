# -*- coding: utf-8 -*-
"""写真のまとめ方のテスト。

実際の写真は使わない。手で作った特徴量だけで、
まとめ方の決まりごとが守られているかを確かめる。

    py -3 -m unittest discover -s tests
"""

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import photo_recommendation as pr


def make_hist(hue: float, sat: float = 0.6) -> np.ndarray:
    """色ヒストグラムの代わり。山の位置を変えると色が違う写真になる。"""
    h = np.zeros((32, 32), dtype=np.float32)
    x = int(hue * 31)
    y = int(sat * 31)
    h[y, x] = 1.0
    h[max(0, y - 1), x] = 0.4
    h[y, max(0, x - 1)] = 0.4
    return (h / h.sum()).astype(np.float32)


def make_struct(seed: int, noise: float = 0.0) -> np.ndarray:
    """構造シグネチャの代わり。seed が同じなら同じ写真の構造になる。"""
    rng = np.random.default_rng(seed)
    base = rng.standard_normal(pr.STRUCT_SIDE * pr.STRUCT_SIDE * 2).astype(np.float32)
    if noise:
        base = base + rng.standard_normal(base.shape).astype(np.float32) * noise
    half = pr.STRUCT_SIDE * pr.STRUCT_SIDE
    out = np.empty_like(base)
    for lo, hi in ((0, half), (half, base.size)):
        part = base[lo:hi]
        part = part - part.mean()
        part = part / np.sqrt((part * part).mean())
        out[lo:hi] = part
    return out.astype(np.float16)


def photo(name, phash=0, dhash=0, hue=0.5, sat=0.6, struct_seed=1,
          struct_noise=0.0, embedding=None, saturation=40.0, score=90.0,
          size=(4000, 3000)):
    a = pr.PhotoAnalysis(path=Path(name), filename=name,
                         width=size[0], height=size[1])
    a.phash = phash
    a.dhash = dhash
    a.hist = make_hist(hue, sat)
    a.struct_sig = make_struct(struct_seed, struct_noise)
    a.saturation = saturation
    a.technical_score = score
    a.sharpness_raw = 500.0
    if embedding is not None:
        v = np.asarray(embedding, dtype=np.float32)
        a.embedding = v / np.linalg.norm(v)
    return a


def unit(*parts) -> np.ndarray:
    v = np.asarray(parts, dtype=np.float32)
    return v / np.linalg.norm(v)


def groups_of(items):
    out = {}
    for a in items:
        if a.group_id:
            out.setdefault(a.group_id, set()).add(a.filename)
    return {frozenset(v) for v in out.values()}


class VariantFamilyTest(unittest.TestCase):
    """第1層: 同じ写真の別バージョンをまとめる。"""

    def test_same_structure_different_color_is_variant(self):
        """カラー版と白黒版。色は全く違うが構造は同じ。"""
        cfg = pr.DEFAULT_SETTINGS
        color = photo("color.jpg", struct_seed=7, hue=0.2, saturation=60.0)
        mono = photo("mono.jpg", struct_seed=7, hue=0.9, saturation=1.0)
        self.assertLess(pr.correlation(color, mono), 0.2)   # 色は似ていない
        self.assertTrue(pr.is_same_photo_variant(color, mono, cfg))

    def test_identical_copy_is_variant(self):
        cfg = pr.DEFAULT_SETTINGS
        a = photo("a.jpg", struct_seed=3)
        b = photo("a - コピー.jpg", struct_seed=3)
        self.assertTrue(pr.is_same_photo_variant(a, b, cfg))

    def test_different_cut_is_not_variant(self):
        """構図が似ているだけの別カットは、同じ写真にしない。"""
        cfg = pr.DEFAULT_SETTINGS
        a = photo("a.jpg", struct_seed=11)
        b = photo("b.jpg", struct_seed=11, struct_noise=0.35)
        g, e = pr.structure_match(a, b)
        self.assertLess(g, cfg.variant_structure)
        self.assertFalse(pr.is_same_photo_variant(a, b, cfg))

    def test_canonical_prefers_color_version(self):
        """代表にはカラー版を選ぶ。白黒版も消さずに家族へ残す。"""
        cfg = pr.DEFAULT_SETTINGS
        color = photo("color.jpg", struct_seed=5, saturation=55.0, score=90.0)
        mono = photo("mono.jpg", struct_seed=5, hue=0.9, saturation=0.5, score=99.0)
        items = [color, mono]
        families, reps = pr.build_variant_families(items, cfg)
        self.assertEqual(len(families), 1)
        self.assertEqual(items[reps[0]].filename, "color.jpg")
        self.assertEqual(color.variant_role, "canonical")
        self.assertEqual(mono.variant_role, "variant")
        self.assertEqual(color.variant_family, mono.variant_family)
        self.assertNotEqual(color.variant_family, "")


class CandidateGroupTest(unittest.TestCase):
    """第2層: 代替候補の群を作る。"""

    def test_chain_alone_does_not_join_two_groups(self):
        """A≈B, B≈C でも、A と C が代替候補でなければ同じ群にしない。"""
        cfg = pr.DEFAULT_SETTINGS
        a = photo("a.jpg", phash=0b0, dhash=0b0, hue=0.50, struct_seed=1,
                  embedding=unit(1.0, 0.0, 0.0))
        b = photo("b.jpg", phash=0b0, dhash=0b0, hue=0.50, struct_seed=2,
                  embedding=unit(0.95, 0.31, 0.0))
        # c は b とだけ近く、a とは色もハッシュも遠い
        c = photo("c.jpg", phash=(1 << 40) - 1, dhash=(1 << 40) - 1, hue=0.05,
                  struct_seed=3, embedding=unit(0.80, 0.60, 0.0))
        items = [a, b, c]
        pr.group_photos(items, cfg)
        self.assertNotEqual(a.group_id, c.group_id,
                            "連鎖だけで別の撮影カットがつながってはいけない")

    def test_color_variant_does_not_split_group(self):
        """白黒版が混じっても、本来の候補群を割らない。"""
        cfg = pr.DEFAULT_SETTINGS
        base = photo("01.jpg", phash=0, dhash=0, hue=0.50, struct_seed=21,
                     saturation=50.0, embedding=unit(1.0, 0.0, 0.0))
        mono = photo("02.jpg", phash=0, dhash=0, hue=0.95, struct_seed=21,
                     saturation=0.5, embedding=unit(0.99, 0.14, 0.0))
        near = photo("03.jpg", phash=0b111, dhash=0b11, hue=0.50, struct_seed=22,
                     saturation=50.0, embedding=unit(0.995, 0.10, 0.0))
        items = [base, mono, near]
        pr.group_photos(items, cfg)
        self.assertEqual(len(groups_of(items)), 1)
        self.assertEqual(groups_of(items), {frozenset({"01.jpg", "02.jpg", "03.jpg"})})

    def test_compatible_but_no_strong_edge_stays_out(self):
        """『明らかに別物ではない』だけでは群へ入れない。強い結びつきが要る。"""
        cfg = pr.DEFAULT_SETTINGS
        a = photo("a.jpg", phash=0, dhash=0, hue=0.5, struct_seed=31,
                  embedding=unit(1.0, 0.0, 0.0))
        b = photo("b.jpg", phash=0b11, dhash=0b1, hue=0.5, struct_seed=32,
                  embedding=unit(0.999, 0.045, 0.0))
        # c は色もハッシュも近い（＝矛盾しない）が、縦位置で撮った別カット。
        # 縦横比が違うので従来判定は通らず、embedding も遠い。
        c = photo("c.jpg", phash=0, dhash=0, hue=0.5, size=(3000, 4000),
                  struct_seed=33, embedding=unit(0.60, 0.80, 0.0))
        self.assertTrue(pr.is_compatible(a, c, cfg))
        self.assertEqual(pr.edge_kind(a, c, cfg), "")
        items = [a, b, c]
        pr.group_photos(items, cfg)
        self.assertEqual(a.group_id, b.group_id)
        self.assertNotEqual(a.group_id, c.group_id)

    def test_variant_members_are_expanded_into_the_group(self):
        """群が決まったあと、家族の全員がその群へ入る。"""
        cfg = pr.DEFAULT_SETTINGS
        a = photo("a.jpg", phash=0, dhash=0, hue=0.5, struct_seed=41,
                  saturation=50.0, embedding=unit(1.0, 0.0, 0.0))
        a_mono = photo("a_bw.jpg", phash=0, dhash=0, hue=0.95, struct_seed=41,
                       saturation=0.4, embedding=unit(0.98, 0.20, 0.0))
        b = photo("b.jpg", phash=0b11, dhash=0b1, hue=0.5, struct_seed=42,
                  saturation=50.0, embedding=unit(0.999, 0.045, 0.0))
        items = [a, a_mono, b]
        pr.group_photos(items, cfg)
        self.assertEqual(a.group_id, a_mono.group_id)
        self.assertEqual(a.group_id, b.group_id)
        self.assertEqual(a_mono.group_size, 3)

    def test_without_embedding_uses_legacy_grouping(self):
        """embedding が無いときは従来のまとめ方に戻る。"""
        cfg = pr.DEFAULT_SETTINGS
        a = photo("a.jpg", phash=0, dhash=0, hue=0.5, struct_seed=51)
        b = photo("b.jpg", phash=0b1, dhash=0b1, hue=0.5, struct_seed=52)
        items = [a, b]
        self.assertTrue(all(x.embedding is None for x in items))
        pr.group_photos(items, cfg)
        self.assertEqual(a.group_id, b.group_id)
        self.assertEqual(a.variant_family, "")   # 従来方式では第1層を使わない


class EdgePriorityTest(unittest.TestCase):
    def test_hybrid_ranks_above_alone(self):
        """古典的な特徴の裏付けがある hybrid を先に評価する。"""
        self.assertGreater(pr.EDGE_PRIORITY["embedding:hybrid"],
                           pr.EDGE_PRIORITY["embedding:alone"])
        self.assertGreater(pr.EDGE_PRIORITY["classic:duplicate"],
                           pr.EDGE_PRIORITY["classic:tight"])
        self.assertGreater(pr.EDGE_PRIORITY["classic:loose"],
                           pr.EDGE_PRIORITY["embedding:hybrid"])

    def test_edge_kind_matches_is_similar(self):
        """どの規則で通ったかの判定と、似ているかの判定が食い違わない。"""
        cfg = pr.DEFAULT_SETTINGS
        rng = np.random.default_rng(0)
        for k in range(200):
            a = photo("a.jpg", phash=int(rng.integers(0, 1 << 40)),
                      dhash=int(rng.integers(0, 1 << 40)),
                      hue=float(rng.random()), struct_seed=k,
                      embedding=unit(*rng.standard_normal(4)))
            b = photo("b.jpg", phash=int(rng.integers(0, 1 << 40)),
                      dhash=int(rng.integers(0, 1 << 40)),
                      hue=float(rng.random()), struct_seed=k + 1000,
                      embedding=unit(*rng.standard_normal(4)))
            self.assertEqual(bool(pr.edge_kind(a, b, cfg)),
                             pr.is_similar(a, b, cfg))


if __name__ == "__main__":
    unittest.main()
