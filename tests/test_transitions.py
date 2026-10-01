# -*- coding: utf-8 -*-
"""写真と写真のつなぎ方（SI Director v2）: 確率の profile から境目ごとの種類を一度だけ確定する。

確定は乱数ではなく seed と境目の番号の SHA-256 で行い、Project に保存する。書き出しは保存された
順番を再生するだけ。profile の例（calm / dynamic）は試験用で、コードに固定した判断ではない。

    py -3 -m unittest discover -s tests
"""

import hashlib
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from queue import Queue

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import PhotoMovieMaker_GPU as app  # noqa: E402
import pmm_core as core  # noqa: E402

FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

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


class RendererTransitionTest(unittest.TestCase):
    """各つなぎ方を短い合成動画で書き出し、フレーム数・長さ・境目の画を確かめる。"""
    W, H = 160, 90
    COLORS = [(255, 0, 0), (0, 255, 0), (0, 0, 255)]

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="pmm transition 試験 — ")
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.paths = []
        for k, color in enumerate(self.COLORS, 1):
            path = self.dir / f"写真 {k}.png"
            Image.new("RGB", (self.W, self.H), color).save(path)
            self.paths.append(path)

    def render(self, transitions, name):
        r = app.VideoRenderer(
            image_paths=self.paths, output_path=self.dir / name, width=self.W, height=self.H, fps=10,
            interval_seconds=1.0, transition_seconds=0.4, zoom_percent=0.0, blur_background=False,
            bgm_segments=[], encoder_pref="cpu", q=Queue(), stop_event=threading.Event(), title=None,
            transitions=transitions)
        r.run()
        return r

    def frames(self, path):
        proc = subprocess.run([app.find_ffmpeg(), "-v", "error", "-i", str(path), "-f", "rawvideo",
                               "-pix_fmt", "rgb24", "-"], capture_output=True, creationflags=FLAGS)
        self.assertEqual(proc.stderr, b"")                       # 壊れていない（デコードのエラーが無い）
        return np.frombuffer(proc.stdout, np.uint8).reshape(-1, self.H, self.W, 3).astype(int)

    def near(self, pixels, color, tol=40):
        return bool(np.all(np.abs(pixels.reshape(-1, 3).mean(axis=0) - np.array(color)) < tol))

    def test_each_type_keeps_length_and_draws_the_boundary(self):
        red, green, black = self.COLORS[0], self.COLORS[1], (0, 0, 0)
        for kind in app.TRANSITION_TYPES:
            with self.subTest(kind=kind):
                r = self.render([kind, "crossfade"], f"{kind}.mp4")
                f = self.frames(r.output)
                self.assertEqual(len(f), 30)                          # 1 秒 × 3 枚 × 10 fps（つなぎ方で変わらない）
                self.assertEqual(r.total_frames, 30)
                self.assertTrue(self.near(f[5], red) and self.near(f[10], green))
                if kind == "cut":
                    self.assertTrue(self.near(f[9], red))            # 境目の直前まで写真 1 だけ
                elif kind == "fade_black":
                    self.assertTrue(self.near(f[7], black, 20))      # 途中で黒になる
                elif kind == "slide":
                    self.assertTrue(self.near(f[7][:, :60], red) and self.near(f[7][:, 100:], green))
                else:
                    self.assertFalse(self.near(f[8], red) or self.near(f[8], green))   # 混ざっている
                settings = r.settings_path().read_text(encoding="utf-8")
                self.assertIn(f'"{kind}"', settings)

    def test_no_sequence_equals_all_crossfade(self):
        a = self.render(None, "なし.mp4")
        b = self.render(["crossfade", "crossfade"], "クロスフェード.mp4")
        sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()   # noqa: E731
        self.assertEqual(sha(a.output), sha(b.output))

    def test_wrong_length_is_refused(self):
        for bad in (["crossfade"], ["crossfade", "zoom"]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.render(bad, "x.mp4")


class ComposePreviewTest(unittest.TestCase):
    """compose で一度だけ確定し、preview で境目ごとに確かめられ、同じ Project は同じ映像になる。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="pmm compose transition 試験 — ")
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.photos, self.out = base / "写真 & 素材 #1", base / "出力 #2"
        self.photos.mkdir()
        self.out.mkdir()
        for k in range(6):
            Image.new("RGB", (160, 90), (40 * k, 200 - 30 * k, 90)).save(self.photos / f"写真 {k + 1}.png")
        self.selected = [f"写真 {k}.png" for k in (2, 1, 3, 6, 5, 4)]

    def edits(self, **transitions):
        edits = {"selected": self.selected, "title_card": {"main": "つなぎ方", "duration": 2.0, "text_fade_seconds": 1.0},
                 "video": {"width": 160, "height": 90, "fps": 10, "interval_seconds": 1.0,
                           "transition_seconds": 0.4, "zoom_percent": 0.0, "encoder_choice": "cpu"}}
        if transitions:
            edits["transitions"] = transitions
        return edits

    def compose(self, name, **transitions):
        return core.compose_project(self.photos, self.edits(**transitions), self.out / f"{name}.photomovie.json")

    def test_compose_fixes_the_sequence_and_preview_shows_it(self):
        result = self.compose("作品", profile=DYNAMIC, seed=42, overrides=[{"after_photo": 3, "type": "fade_black"}])
        block = result["project"]["transitions"]
        self.assertEqual((block["seed"], len(block["sequence"]), block["sequence"][2]), (42, 5, "fade_black"))
        preview = core.preview_project(result["project_file"], self.photos)["timeline"]
        self.assertEqual([b["type"] for b in preview["transitions"]], block["sequence"])
        third = preview["transitions"][2]
        self.assertEqual((third["after_photo"], third["from_file"], third["to_file"], third["seconds"]),
                         (3, "写真 3.png", "写真 6.png", 0.4))
        self.assertEqual(third["starts_at"], 4.6)                    # タイトル 2 秒 + 3 枚 - 0.4 秒
        for b in preview["transitions"]:
            self.assertEqual(b["seconds"], 0.0 if b["type"] == "cut" else 0.4)
        self.assertIn("00:05 ↪ fade_black（写真 3→4）", preview["lines"])
        self.assertEqual(sum(preview["transition_counts"].values()), 5)
        self.assertEqual(preview["title_transition"], {"type": "crossfade", "seconds": 1.0})

    def test_same_inputs_same_project_and_default_seed_is_stored(self):
        a = self.compose("一", profile=CALM)["project"]["transitions"]
        b = self.compose("二", profile=CALM)["project"]["transitions"]
        self.assertEqual(a, b)
        self.assertIsInstance(a["seed"], int)
        self.assertEqual(a["method"], core.TRANSITION_METHOD)

    def test_old_style_compose_is_crossfade_only(self):
        result = self.compose("従来")
        self.assertNotIn("transitions", result["project"])
        timeline = core.preview_project(result["project_file"], self.photos)["timeline"]
        self.assertEqual({b["type"] for b in timeline["transitions"]}, {"crossfade"})
        self.assertFalse(any("↪" in line for line in timeline["lines"]))

    def test_bad_transition_edits(self):
        cases = [({"profile": {"crossfade": 0.5}}, "invalid_transitions"),
                 ({"profile": {"spin": 1.0}}, "invalid_transitions"),
                 ({"overrides": [{"after_photo": 6, "type": "cut"}]}, "invalid_transitions"),
                 ({"seed": -3}, "invalid_transitions")]
        for change, code in cases:
            with self.subTest(change=change), self.assertRaises(core.CoreError) as ctx:
                self.compose("x", **change)
            self.assertEqual(ctx.exception.code, code)
        edits = self.edits()
        edits["transitions"] = {"profile": CALM, "sequence": ["cut"] * 5}
        with self.assertRaises(core.CoreError) as ctx:
            core.compose_project(self.photos, edits, self.out / "y.photomovie.json")
        self.assertEqual(ctx.exception.code, "invalid_edits")
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), [])

    def test_same_project_renders_the_same_video(self):
        result = self.compose("同じ", profile=DYNAMIC, seed=7)
        hashes = []
        for k in (1, 2):
            r = core.render_project(result["project_file"], self.photos, self.out / f"同じ {k}.mp4")
            self.assertEqual(r["transitions"], {t: n for t, n in
                                                ((t, result["project"]["transitions"]["sequence"].count(t))
                                                 for t in core.TRANSITION_TYPES) if n})
            hashes.append(hashlib.sha256(Path(r["output"]).read_bytes()).hexdigest())
        self.assertEqual(hashes[0], hashes[1])


PINNED_CALM_12_SEED_1 = ["cut"] + ["crossfade"] * 8 + ["fade_black", "crossfade"]   # 実装時に計算して固定


if __name__ == "__main__":
    unittest.main(verbosity=2)
