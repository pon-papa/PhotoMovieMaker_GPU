# -*- coding: utf-8 -*-
"""Project JSON から MP4 を書き出す（pmm_core.render_project / pmm_cli render）の試験。

写真はその場で作った画像だけを使う。書き出しは一時フォルダーの中だけ。

    py -3 -m unittest discover -s tests
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pmm_core as core


def make_photos(folder: Path, count: int, size=(480, 320)) -> None:
    for k in range(count):
        im = Image.new("RGB", size, (30 + 20 * k % 200, 110, 200 - 15 * k % 150))
        ImageDraw.Draw(im).ellipse([40 + 10 * k, 40, 220 + 10 * k, 220], fill=(240, 230, 210))
        im.save(folder / f"写真 {k + 1}.jpg")


def fingerprint(folder: Path) -> dict:
    return {p.name: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
            for p in sorted(folder.iterdir())}


def ffmpeg_pids() -> set[str]:
    if os.name != "nt":
        return set()
    out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                         errors="replace").stdout
    return {line.split('","')[1] for line in out.splitlines()
            if "ffmpeg" in line.lower() and '","' in line}


def pmm_temp_dirs() -> set[str]:
    return {p.name for p in Path(tempfile.gettempdir()).glob("pmm_render_*")}


class RenderBase(unittest.TestCase):
    photos_count = 4

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="pmm render 試験 — ")
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.photos = base / "写真 & 素材 (元)"
        self.out = base / "出力 — MP4 (試験)"
        self.photos.mkdir()
        self.out.mkdir()
        make_photos(self.photos, self.photos_count)
        self.source_before = fingerprint(self.photos)

    def plan(self, **video) -> Path:
        project = core.create_project_plan(self.photos, title="試験 — 上映")
        project["video"].update(width=160, height=96, fps=10, interval_seconds=1.0,
                                transition_seconds=0.3, encoder_choice="cpu")
        project["video"].update(video)
        project["title_card"]["duration"] = 1.0
        project["title_card"]["fade_seconds"] = 0.3
        project["title_card"]["text_fade_seconds"] = 0.3
        path = self.out / "計画 (試験).photomovie.json"
        core.save_project(project, path, overwrite=True)
        return path

    def edit_plan(self, path: Path, change) -> Path:
        data = json.loads(path.read_text(encoding="utf-8"))
        change(data)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def tearDown(self):
        # どの試験でも、元の写真は変わらない
        self.assertEqual(fingerprint(self.photos), self.source_before)


class RenderProjectTest(RenderBase):
    def test_render_writes_mp4_and_settings(self):
        plan = self.plan()
        plan_before = hashlib.sha256(plan.read_bytes()).hexdigest()
        target = self.out / "完成 — 上映 & 試験 (1).mp4"
        got = []
        res = core.render_project(plan, self.photos, target, progress=lambda *a: got.append(a))
        self.assertTrue(target.is_file() and target.stat().st_size > 1000)
        self.assertEqual(res["output"], str(target))
        # 4枚 × 1秒 + タイトル 1秒、10fps
        self.assertEqual(res["frames"], 50)
        import imageio_ffmpeg
        frames, _ = imageio_ffmpeg.count_frames_and_secs(str(target))
        self.assertEqual(frames, 50)
        settings = json.loads(Path(res["settings_file"]).read_text(encoding="utf-8"))
        self.assertEqual(settings["output"], str(target))
        self.assertEqual(settings["project"]["file"], plan.name)
        self.assertEqual(settings["image_order"], [f"写真 {k}.jpg" for k in (1, 2, 3, 4)])
        self.assertEqual(hashlib.sha256(plan.read_bytes()).hexdigest(), plan_before)
        # 進捗は増えるだけで、最後は 1000 / 1000
        dones = [d for _, d, _ in got]
        self.assertEqual(dones, sorted(set(dones)))
        self.assertEqual(got[-1][1:], (1000, 1000))
        self.assertEqual([p.name for p in self.out.iterdir() if p.name.endswith(".part")], [])

    def test_enabled_and_order_are_followed(self):
        plan = self.edit_plan(self.plan(), lambda d: (
            d["media"][0].update(enabled=False),
            d["media"][1].update(order=9),
            d["title_card"].update(enabled=False)))
        res = core.render_project(plan, self.photos, self.out / "順番.mp4")
        settings = json.loads(Path(res["settings_file"]).read_text(encoding="utf-8"))
        self.assertEqual(settings["image_order"], ["写真 3.jpg", "写真 4.jpg", "写真 2.jpg"])
        self.assertEqual(res["frames"], 30)
        self.assertFalse(res["title_card"])

    def test_overwrite_is_refused_by_default(self):
        plan = self.plan()
        target = self.out / "上書き.mp4"
        core.render_project(plan, self.photos, target)
        before = fingerprint(self.out)
        with self.assertRaises(core.CoreError) as ctx:
            core.render_project(plan, self.photos, target)
        self.assertEqual(ctx.exception.code, "output_exists")
        self.assertEqual(fingerprint(self.out), before)
        # 設定ファイルだけが残っている場合も断る
        target.unlink()
        with self.assertRaises(core.CoreError) as ctx:
            core.render_project(plan, self.photos, target)
        self.assertEqual(ctx.exception.code, "output_exists")
        core.render_project(plan, self.photos, target, overwrite=True)
        self.assertTrue(target.is_file())

    def test_refusals_write_nothing(self):
        plan = self.plan()
        cases = [
            (dict(output_file=self.photos / "x.mp4"), "invalid_output"),
            (dict(output_file=self.out / "x.mov"), "invalid_output"),
            (dict(output_file=self.out / "無い" / "x.mp4"), "invalid_output"),
            (dict(photo_folder=self.out), "folder_mismatch"),
        ]
        for extra, code in cases:
            args = dict(project_file=plan, photo_folder=self.photos, output_file=self.out / "x.mp4")
            args.update(extra)
            with self.subTest(code=code, extra=str(extra)):
                with self.assertRaises(core.CoreError) as ctx:
                    core.render_project(args["project_file"], args["photo_folder"], args["output_file"])
                self.assertEqual(ctx.exception.code, code)
        edits = [
            (lambda d: [m.update(enabled=False) for m in d["media"]], "no_media"),
            (lambda d: d["media"][0].update(file="無い写真.jpg"), "missing_media"),
            (lambda d: d["media"][0].update(type="video"), "unsupported_media"),
            (lambda d: d["bgm_segments"].append({"audio": "C:/x.mp3"}), "bgm_not_supported"),
            (lambda d: d["video"].update(transition_seconds=2.0), "invalid_project"),
            (lambda d: d["title_card"].update(bg_color="あか"), "invalid_project"),
            (lambda d: d["title_card"].update(duration="5"), "invalid_project"),
            (lambda d: d["media"][0].update(file="../外.jpg"), "invalid_project"),
            (lambda d: d.update(kind="other"), "invalid_project"),
        ]
        for change, code in edits:
            path = self.edit_plan(self.plan(), change)
            with self.subTest(code=code):
                with self.assertRaises(core.CoreError) as ctx:
                    core.render_project(path, self.photos, self.out / "x.mp4")
                self.assertEqual(ctx.exception.code, code)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["計画 (試験).photomovie.json"])

    def test_cancel_leaves_nothing(self):
        plan = self.plan(width=320, height=192, interval_seconds=3.0)
        target = self.out / "中止.mp4"
        temps_before = pmm_temp_dirs()
        pids_before = ffmpeg_pids()
        seen = []

        def stop():
            return len(seen) >= 3          # 進捗が3回届いたら止める

        with self.assertRaises(core.CancelledError):
            core.render_project(plan, self.photos, target, progress=lambda *a: seen.append(a),
                                should_stop=stop)
        self.assertFalse(target.exists())
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["計画 (試験).photomovie.json"])
        self.assertEqual(pmm_temp_dirs() - temps_before, set())
        time.sleep(0.5)
        self.assertEqual(ffmpeg_pids() - pids_before, set())


class RenderCliTest(RenderBase):
    photos_count = 8

    def cli(self, *args, env_extra=None):
        env = dict(os.environ, PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1", **(env_extra or {}))
        env.pop("TOOLDOCK_CANCEL_FILE", None) if not env_extra else None
        return subprocess.Popen([sys.executable, "-B", str(ROOT / "pmm_cli.py"), *args],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)

    def test_cli_render_with_progress_lines(self):
        plan = self.plan()
        target = self.out / "CLI — 完成 (1).mp4"
        p = self.cli("render", f"--project={plan}", f"--folder={self.photos}", f"--output={target}")
        out, err = p.communicate(timeout=600)
        reply = json.loads(out.decode("ascii"))
        self.assertEqual((p.returncode, reply["ok"], reply["command"]), (0, True, "render"))
        self.assertEqual(reply["result"]["output"], str(target))
        events = [json.loads(line) for line in err.decode("utf-8").splitlines()
                  if line.startswith('{"event"')]
        self.assertTrue(events)
        self.assertEqual((events[-1]["done"], events[-1]["total"]), (1000, 1000))
        self.assertIn("映像作成", events[-1]["phase"])

    def test_cli_cancel_file_stops_render_and_ffmpeg(self):
        plan = self.plan(width=640, height=384, fps=30, interval_seconds=4.0)
        target = self.out / "CLI 中止.mp4"
        marker = self.out.parent / "cancel.flag"
        temps_before = pmm_temp_dirs()
        pids_before = ffmpeg_pids()
        p = self.cli("render", f"--project={plan}", f"--folder={self.photos}", f"--output={target}",
                     env_extra={"TOOLDOCK_CANCEL_FILE": str(marker)})
        # 進捗が出始めてから中止ファイルを置く
        started = time.time()
        while time.time() - started < 120:
            line = p.stderr.readline().decode("utf-8", "replace")
            if line.startswith('{"event"') and json.loads(line)["done"] >= 50:
                break
        marker.write_text("stop", encoding="utf-8")
        out, _ = p.communicate(timeout=120)
        reply = json.loads(out.decode("ascii"))
        self.assertEqual((p.returncode, reply["error"]["code"]), (130, "cancelled"))
        self.assertFalse(target.exists())
        self.assertEqual(sorted(x.name for x in self.out.iterdir()), ["計画 (試験).photomovie.json"])
        self.assertEqual(pmm_temp_dirs() - temps_before, set())
        time.sleep(0.5)
        self.assertEqual(ffmpeg_pids() - pids_before, set())

    def test_without_cancel_file_variable_nothing_is_watched(self):
        import pmm_cli
        saved = os.environ.pop("TOOLDOCK_CANCEL_FILE", None)
        try:
            self.assertFalse(pmm_cli.should_stop())
        finally:
            if saved is not None:
                os.environ["TOOLDOCK_CANCEL_FILE"] = saved


class RenderManifestTest(unittest.TestCase):
    def test_render_action_matches_cli_and_result(self):
        manifest = json.loads((ROOT / "tooldock.tool.json").read_text(encoding="utf-8"))
        action = next(a for a in manifest["actions"] if a["name"] == "render")
        self.assertEqual((action["access"], action["progress"], action["cancellable"]), ("write", True, True))
        props = action["input_schema"]["properties"]
        self.assertEqual(props["output_file"]["x-path"], "output_file")
        self.assertEqual(props["output_file"]["x-suffixes"], [".mp4"])
        self.assertEqual(props["project_file"]["x-path"], "project_file")
        self.assertEqual(props["photo_folder"]["x-path"], "media_folder")
        self.assertIs(props["overwrite"]["default"], False)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        photos = Path(tmp.name) / "写真"
        photos.mkdir()
        make_photos(photos, 2)
        project = core.create_project_plan(photos)
        project["video"].update(width=64, height=48, fps=5, interval_seconds=1.0,
                                transition_seconds=0.2, encoder_choice="cpu")
        project["title_card"]["enabled"] = False
        plan = Path(tmp.name) / "p.photomovie.json"
        core.save_project(project, plan)
        res = core.render_project(plan, photos, Path(tmp.name) / "o.mp4")
        for key in action["output_schema"]["required"]:
            self.assertIn(key, res)

    def test_gui_does_not_look_at_the_cancel_file(self):
        text = (ROOT / "PhotoMovieMaker_GPU.py").read_text(encoding="utf-8")
        self.assertNotIn("TOOLDOCK", text)
        self.assertNotIn("render_project", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
