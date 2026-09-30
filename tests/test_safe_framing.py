# -*- coding: utf-8 -*-
"""安全な構図（camera_mode = subject_safe, SI Director v2）。

被写体の枠（顔・人物・犬）＋余白が、ズームとパンの最後のフレームまで画面に残ることを確かめる。
枠は検出器を通さず、ここで直接与える（幾何の確認）。写真はその場で作る合成画像だけ。

    py -3 -m unittest discover -s tests
"""

import json
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
import subject_detector as sd  # noqa: E402

W, H = 640, 360


def box(kind, x0, y0, x1, y1):
    return {"kind": kind, "x0": x0, "y0": y0, "x1": x1, "y1": y1, "score": 0.9}


class SafeFramingBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="pmm safe 試験 — ")
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def photo(self, name, size, draw=None):
        im = Image.new("RGB", size, (90, 120, 150))
        if draw:
            draw(im)
        path = self.dir / name
        im.save(path, quality=95)
        return path

    def renderer(self, paths, mode=app.CAMERA_SUBJECT_SAFE, zoom=8.0, blur=True, interval=2.0, out="out.mp4"):
        return app.VideoRenderer(
            image_paths=paths, output_path=self.dir / out, width=W, height=H, fps=10,
            interval_seconds=interval, transition_seconds=0.5, zoom_percent=zoom, blur_background=blur,
            bgm_segments=[], encoder_pref="cpu", q=Queue(), stop_event=threading.Event(),
            title=None, camera_mode=mode)

    @staticmethod
    def inject(r, path, size, boxes, face_count=0, dog_count=0, target=None):
        mode = sd.MODE_LEGACY
        if target is not None:
            mode = sd.MODE_HUMAN_FACE if face_count else sd.MODE_DOG_UPPER
        r.detections[str(path)] = sd.SubjectDetection(
            filename=path.name, face_count=face_count, dog_count=dog_count, mode=mode,
            target_x=target[0] if target else None, target_y=target[1] if target else None, boxes=boxes)
        r.source_sizes[str(path)] = size

    def assert_contained(self, r, index, motion):
        region = r.safe_region(index)
        zoom = r.zoom_end if motion.zoom is None else motion.zoom
        if zoom <= 1.0:
            return
        vis = r.visible_rect(motion, zoom)
        loose = tuple(None if v is None else v + (1e-6 if k < 2 else -1e-6) for k, v in enumerate(region))
        self.assertTrue(r.region_inside(loose, vis), (region, vis))


class SafeGeometryTest(SafeFramingBase):
    """ズームを弱め・パンを抑えて、守る範囲が最後のフレームまで残る。"""

    def one(self, size, boxes, *, face_count=1, target=(0.5, 0.4), blur=True, zoom=8.0):
        path = self.photo("写真 1.jpg", size)
        r = self.renderer([path], blur=blur, zoom=zoom)
        self.inject(r, path, size, boxes, face_count=face_count, target=target)
        motion = r.motions()[0]
        self.assert_contained(r, 0, motion)
        return r, motion, r.framing_log[0]

    def test_centered_subject_keeps_the_original_motion(self):
        r, motion, entry = self.one((1600, 900), [box("face", 0.45, 0.35, 0.55, 0.5),
                                                  box("person", 0.4, 0.3, 0.6, 0.7)])
        self.assertEqual(entry["adjustment"], "none")
        self.assertIsNone(motion.zoom)                  # 従来と同じ動き（同じ描画）
        subject = self.renderer([r.images[0]], mode=app.CAMERA_SUBJECT)
        subject.detections, subject.source_sizes = r.detections, r.source_sizes
        self.assertEqual(subject.motions()[0], motion)

    def test_subject_near_each_edge_reduces_zoom_but_still_zooms(self):
        # 元の動きが、端の被写体から離れる向きにパンする場合（従来なら端が切れる）
        cases = {"left": (box("person", 0.035, 0.3, 0.3, 0.7), app.Motion(-1.0, 0.0)),
                 "right": (box("person", 0.7, 0.3, 0.965, 0.7), app.Motion(1.0, 0.0)),
                 "top": (box("person", 0.35, 0.04, 0.65, 0.5), app.Motion(0.0, -1.0)),
                 "bottom": (box("person", 0.35, 0.5, 0.65, 0.955), app.Motion(0.0, 1.0))}
        for side, (b, away) in cases.items():
            with self.subTest(side=side):
                path = self.photo("写真 1.jpg", (1600, 900))
                r = self.renderer([path])
                self.inject(r, path, (1600, 900), [b], face_count=2)
                vis = r.visible_rect(away, r.zoom_end)
                self.assertFalse(r.region_inside(r.safe_region(0), vis))   # 従来の動きでは切れる
                motion, entry = r.safe_motion(0, away)
                self.assert_contained(r, 0, motion)
                self.assertIn(entry["adjustment"], ("pan_limited", "zoom_reduced"))
                self.assertGreater(entry["zoom_percent"], 0.0)          # 完全にやめず、寄れる範囲で寄る
                self.assertLessEqual(entry["zoom_percent"], 8.0)

    def test_side_already_cut_by_the_photo_is_not_protected(self):
        # 枠が写真の下端まで届いている（元の写真で切れている半身の写真など）→ 下辺は守らず、ズームを止めない
        _r, motion, entry = self.one((1600, 900), [box("person", 0.3, 0.2, 0.6, 0.995)])
        self.assertEqual(entry["open_sides"], ["bottom"])
        self.assertEqual(entry["zoom_percent"], 8.0)

    def test_margin_never_forces_zoom_off_by_itself(self):
        # 裾が写真の端のすぐ近く（2%）: 余白は隙間の半分までにして、裾を残したまま少しは寄る
        _r, motion, entry = self.one((1600, 900), [box("person", 0.35, 0.3, 0.65, 0.98)], target=(0.5, 0.35))
        self.assertEqual(entry["open_sides"], [])
        self.assertGreater(entry["zoom_percent"], 0.0)

    def test_tall_person_and_wide_subject(self):
        for label, b in (("tall", box("person", 0.4, 0.05, 0.6, 0.94)), ("wide", box("person", 0.06, 0.3, 0.94, 0.6))):
            with self.subTest(label):
                _r, _m, entry = self.one((1600, 900), [b])
                self.assertEqual(entry["adjustment"], "zoom_reduced")
                self.assertGreater(entry["zoom_percent"], 0.0)
                self.assertLess(entry["zoom_percent"], 8.0)

    def test_portrait_photo_uses_the_displayed_rect(self):
        # 縦位置の写真は左右がぼかし背景。横方向は写真の外（背景）まで気にしない
        _r, _m, entry = self.one((1000, 1500), [box("person", 0.05, 0.06, 0.95, 0.94)])
        region = entry["region"]
        self.assertGreater(region[0], 0.3)                  # 写真は画面の中央 1/3 ほど
        self.assertLess(region[2], 0.7)
        self.assertEqual(entry["adjustment"], "zoom_reduced")   # 縦方向だけで決まる
        self.assertEqual(entry["zoom_percent"], 6.0)

    def test_dog_subject_uses_the_same_containment(self):
        path = self.photo("犬.jpg", (1600, 900))
        r = self.renderer([path])
        self.inject(r, path, (1600, 900), [box("dog", 0.2, 0.45, 0.62, 0.98)], dog_count=1, target=(0.41, 0.6))
        motion = r.motions()[0]
        self.assert_contained(r, 0, motion)
        self.assertEqual(r.framing_log[0]["adjustment"], "zoom_reduced")

    def test_no_subject_keeps_the_legacy_motion(self):
        path = self.photo("風景.jpg", (1600, 900))
        safe = self.renderer([path])
        self.inject(safe, path, (1600, 900), [])
        legacy = self.renderer([path], mode=app.CAMERA_LEGACY)
        self.assertEqual(safe.motions(), legacy.motions())
        self.assertEqual(safe.framing_log[0]["adjustment"], "no_subject")

    def test_small_background_people_are_not_protected(self):
        # 主役より十分小さい枠（遠くの人）は守る対象にしない
        _r, _m, entry = self.one((1600, 900), [box("person", 0.4, 0.3, 0.6, 0.8),
                                              box("person", 0.0, 0.5, 0.03, 0.56)])
        self.assertGreater(entry["region"][0], 0.3)

    def test_blur_off_crops_to_the_canvas(self):
        # ぼかし背景なし（fit）: 縦位置の写真は上下が切り落とされ、枠は画面の上下いっぱいになる
        _r, motion, entry = self.one((1000, 1500), [box("person", 0.3, 0.3, 0.7, 0.7)], blur=False)
        self.assertEqual(entry["open_sides"], ["top", "bottom"])      # 画面の外まで続く辺は守れない
        self.assertEqual(entry["adjustment"], "none")

    def test_zoom_zero_setting_is_untouched(self):
        _r, motion, entry = self.one((1600, 900), [box("person", 0.3, 0.2, 0.6, 0.9)], zoom=0.0)
        self.assertEqual(entry["adjustment"], "none")
        self.assertIsNone(motion.zoom)


class SafePixelsTest(SafeFramingBase):
    """描画した最後のフレームで、裾に当たる色の帯が subject より多く残る（ドレスの裾の代わり）。"""

    def test_hem_stays_in_frame(self):
        def draw(im):
            px = im.load()
            for x in range(700, 900):          # 画面の下端近くの赤い帯（裾の代わり）
                for y in range(848, 872):
                    px[x, y] = (255, 0, 0)
        path = self.photo("裾.png", (1600, 900), draw)
        boxes = [box("face", 0.47, 0.1, 0.53, 0.2), box("person", 0.42, 0.08, 0.58, 0.975)]
        counts = {}
        for mode in (app.CAMERA_SUBJECT, app.CAMERA_SUBJECT_SAFE):
            r = self.renderer([path], mode=mode)
            self.inject(r, path, (1600, 900), boxes, face_count=1, target=(0.5, 0.15))
            motion = r.motions()[0]
            last = r.render_motion(r.load_canvas(path), motion, r.interval_frames - 1)
            red = (last[:, :, 0] > 200) & (last[:, :, 1] < 60) & (last[:, :, 2] < 60)
            counts[mode] = int(red.sum())
        full = self.renderer([path], mode=app.CAMERA_LEGACY, zoom=0.0)
        whole = full.render_motion(full.load_canvas(path), app.Motion(0, 0), 0)
        expected = int(((whole[:, :, 0] > 200) & (whole[:, :, 1] < 60) & (whole[:, :, 2] < 60)).sum())
        self.assertLess(counts[app.CAMERA_SUBJECT], expected * 0.5)        # 従来は裾が画面外へ
        self.assertGreater(counts[app.CAMERA_SUBJECT_SAFE], expected * 0.9)  # 安全な構図では残る


class SafeRenderTest(SafeFramingBase):
    def test_render_records_the_adjustments(self):
        paths = [self.photo(f"写真 {k}.jpg", (1600, 900)) for k in range(1, 4)]
        r = self.renderer(paths, interval=1.5)
        boxes = [[box("person", 0.4, 0.3, 0.6, 0.7)], [box("person", 0.3, 0.06, 0.6, 0.94)], []]

        def fake_analyze():
            for p, b in zip(paths, boxes):
                self.inject(r, p, (1600, 900), b, face_count=1 if b else 0, target=(0.5, 0.4) if b else None)
        r.analyze_subjects = fake_analyze
        r.run()
        self.assertTrue(r.output.is_file())
        settings = json.loads(r.settings_path().read_text(encoding="utf-8"))
        framing = settings["safe_framing"]
        self.assertEqual([e["adjustment"] for e in framing["photos"]], ["none", "zoom_reduced", "no_subject"])
        self.assertEqual(framing["adjusted"], 1)
        self.assertEqual(settings["video"]["camera_mode"], "subject_safe")
        self.assertEqual(settings["video"]["total_frames"], r.total_frames)


class DetectorBoxesTest(unittest.TestCase):
    def test_boxes_do_not_change_the_existing_decision(self):
        det = sd.SubjectDetector()
        if not det.available:
            self.skipTest("検出モデルが無い")
        rgb = np.full((360, 640, 3), 128, dtype=np.uint8)
        found = det.detect("灰色.jpg", rgb)
        self.assertEqual((found.face_count, found.dog_count, found.mode), (0, 0, sd.MODE_LEGACY))
        self.assertIsInstance(found.boxes, list)


if __name__ == "__main__":
    unittest.main(verbosity=2)
