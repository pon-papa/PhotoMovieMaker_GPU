# -*- coding: utf-8 -*-
"""写真と写真のつなぎ方（SI Director v2）: 確率の profile から境目ごとの種類を一度だけ確定する。

確定は乱数ではなく seed と境目の番号の SHA-256 で行い、Project に保存する。書き出しは保存された
順番を再生するだけ。profile の例（calm / dynamic）は試験用で、コードに固定した判断ではない。

    py -3 -m unittest discover -s tests
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pmm_core as core  # noqa: E402

CALM = {"crossfade": 0.8, "cut": 0.1, "fade_black": 0.1}
DYNAMIC = {"crossfade": 0.5, "cut": 0.25, "slide": 0.2, "fade_black": 0.05}


def runs(sequence, kind):
    longest = current = 0
    for t in sequence:
        current = current + 1 if t == kind else 0
        longest = max(longest, current)
    return longest


class ProfileTest(unittest.TestCase):
    def test_valid_profiles(self):
        for profile in (CALM, DYNAMIC, {"crossfade": 1.0}, {"cut": 0.3, "crossfade": 0.7}):
            with self.subTest(profile=profile):
                self.assertEqual(len(core.plan_transitions(10, profile, 1)), 9)

    def test_invalid_profiles_are_refused(self):
        cases = [{"crossfade": 0.7, "cut": 0.2},              # 合計 0.9
                 {"crossfade": 1.2, "cut": -0.2},             # 負の値
                 {"crossfade": 0.5, "spin": 0.5},             # 知らない種類
                 {"crossfade": "0.5", "cut": 0.5},            # 数でない
                 {}]
        for profile in cases:
            with self.subTest(profile=profile), self.assertRaises(core.CoreError) as ctx:
                core.plan_transitions(10, profile, 1)
            self.assertEqual(ctx.exception.code, "invalid_transitions")
        for seed in (-1, 1.5, "7", True, 2 ** 63):
            with self.subTest(seed=seed), self.assertRaises(core.CoreError):
                core.plan_transitions(10, CALM, seed)


class DeterminismTest(unittest.TestCase):
    def test_same_inputs_same_sequence(self):
        a = core.plan_transitions(40, DYNAMIC, 260921)
        self.assertEqual(a, core.plan_transitions(40, DYNAMIC, 260921))
        self.assertNotEqual(a, core.plan_transitions(40, DYNAMIC, 260922))   # seed が違えば変わりうる

    def test_pinned_sequence(self):
        # 計算方法（SHA-256 と決まり）が知らないうちに変わっていないことの確認
        self.assertEqual(core.plan_transitions(12, CALM, 1), PINNED_CALM_12_SEED_1)

    def test_default_seed_is_stable(self):
        files = ["a.jpg", "b.jpg", "c.jpg"]
        self.assertEqual(core.default_transition_seed(files, CALM), core.default_transition_seed(list(files), dict(CALM)))
        self.assertNotEqual(core.default_transition_seed(files, CALM), core.default_transition_seed(files[::-1], CALM))

    def test_distribution_roughly_follows_the_profile(self):
        seq = core.plan_transitions(2001, {"crossfade": 0.6, "cut": 0.4}, 5)
        share = seq.count("cut") / len(seq)
        self.assertGreater(share, 0.3)
        self.assertLess(share, 0.45)


class ConstraintTest(unittest.TestCase):
    def test_no_repeated_fade_black_or_slide(self):
        for kind in ("fade_black", "slide"):
            for seed in range(20):
                seq = core.plan_transitions(30, {kind: 0.9, "crossfade": 0.1}, seed)
                with self.subTest(kind=kind, seed=seed):
                    self.assertEqual(runs(seq, kind), 1)

    def test_no_three_same_special_in_a_row(self):
        for seed in range(20):
            seq = core.plan_transitions(30, {"cut": 1.0}, seed)
            with self.subTest(seed=seed):
                self.assertLessEqual(runs(seq, "cut"), 2)

    def test_first_boundary_after_title_is_calm(self):
        for seed in range(30):
            seq = core.plan_transitions(5, {"fade_black": 0.5, "slide": 0.5}, seed)
            self.assertEqual(seq[0], "crossfade")

    def test_explicit_override_wins(self):
        overrides = [{"after_photo": 1, "type": "fade_black"}, {"after_photo": 6, "type": "slide"},
                     {"after_photo": 7, "type": "slide"}]
        seq = core.plan_transitions(10, {"crossfade": 1.0}, 3, overrides)
        self.assertEqual((seq[0], seq[5], seq[6]), ("fade_black", "slide", "slide"))   # 指定はそのまま
        self.assertEqual(seq.count("crossfade"), 6)
        # 指定の隣には同じ目立つ種類を選ばない
        seq = core.plan_transitions(10, {"fade_black": 1.0}, 3, [{"after_photo": 5, "type": "fade_black"}])
        self.assertNotIn("fade_black", (seq[3], seq[5]))

    def test_bad_overrides(self):
        for overrides in ([{"after_photo": 10, "type": "cut"}], [{"after_photo": 0, "type": "cut"}],
                          [{"after_photo": 2, "type": "wipe"}], [{"after_photo": 2}],
                          [{"after_photo": 2, "type": "cut"}, {"after_photo": 2, "type": "slide"}]):
            with self.subTest(overrides=overrides), self.assertRaises(core.CoreError):
                core.plan_transitions(10, CALM, 1, overrides)

    def test_single_photo_has_no_boundary(self):
        self.assertEqual(core.plan_transitions(1, CALM, 1), [])


class ProjectFieldTest(unittest.TestCase):
    def project(self, n=4, transitions=None):
        media = [{"file": f"写真 {k}.jpg", "type": "image", "enabled": True, "order": k} for k in range(1, n + 1)]
        p = {"kind": core.PROJECT_KIND, "schema_version": core.PROJECT_SCHEMA_VERSION,
             "project": {"source_folder": str(ROOT), "music_folder": None},
             "media": media, "video": dict(core.DEFAULT_VIDEO),
             "title_card": {"main": "x"}, "bgm_timing": {}, "bgm_segments": []}
        if transitions is not None:
            p["transitions"] = transitions
        return p

    def test_old_project_is_crossfade_only(self):
        p = self.project()
        self.assertEqual(core.validate_project(p), [])
        self.assertEqual(core.transition_sequence(p), ["crossfade"] * 3)

    def test_saved_sequence_is_validated(self):
        ok = {"profile": CALM, "seed": 1, "overrides": [], "sequence": ["crossfade", "cut", "crossfade"]}
        self.assertEqual(core.validate_project(self.project(transitions=ok)), [])
        self.assertEqual(core.transition_sequence(self.project(transitions=ok)), ["crossfade", "cut", "crossfade"])
        for bad in ({"profile": CALM, "seed": 1},                                  # 確定した並びが無い
                    dict(ok, sequence=["crossfade"]),                              # 長さが違う
                    dict(ok, sequence=["crossfade", "zoom", "cut"]),               # 知らない種類
                    dict(ok, overrides=[{"after_photo": 2, "type": "slide"}]),     # 指定と違う
                    dict(ok, extra=1)):
            with self.subTest(bad=bad):
                self.assertTrue(core.validate_project(self.project(transitions=bad)))


PINNED_CALM_12_SEED_1 = ["cut"] + ["crossfade"] * 8 + ["fade_black", "crossfade"]   # 実装時に計算して固定


if __name__ == "__main__":
    unittest.main(verbosity=2)
