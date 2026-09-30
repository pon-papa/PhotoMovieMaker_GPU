# -*- coding: utf-8 -*-
"""PhotoMovieMaker を AI や自動化から使うための CLI。

標準出力には JSON だけを出します（進捗は標準エラー出力へ）。
ToolDock の MCP Sidecar は、このコマンドを通して PhotoMovieMaker を使います。

    py -3 pmm_cli.py capabilities
    py -3 pmm_cli.py scan --folder "C:\\写真\\旅行"
    py -3 pmm_cli.py analyze --folder "C:\\写真\\旅行" [--no-embedding] [--no-subjects]
    py -3 pmm_cli.py plan --folder "C:\\写真\\旅行" [--title 題名] [--analyze] [--save 計画.photomovie.json]
    py -3 pmm_cli.py validate --project 計画.photomovie.json
    py -3 pmm_cli.py render --project 計画.photomovie.json --folder "C:\\写真\\旅行" --output 旅行.mp4 [--overwrite]

    py -3 pmm_cli.py music-scan --folder "C:\\曲"
    py -3 pmm_cli.py music-analyze --folder "C:\\曲"
    py -3 pmm_cli.py compose --folder "C:\\写真\\旅行" --edits-json "{...}" --save 作品.photomovie.json [--music-folder "C:\\曲"]
    py -3 pmm_cli.py preview --project 作品.photomovie.json --folder "C:\\写真\\旅行" [--music-folder "C:\\曲"]
    py -3 pmm_cli.py director --project 作品.photomovie.json --plan-json "{...}" [--overwrite]

終了コード: 0=成功 / 1=処理できなかった / 2=使い方の誤り / 130=中止
元の写真は読むだけで、書き換えません。

中止: 環境変数 TOOLDOCK_CANCEL_FILE が指すファイルが現れたら、処理を安全に止める
（呼び出し側が作る。ToolDock などの自動化から使うときだけ。画面には関係しない）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

OUTPUT_FORMAT = 1


class UsageError(ValueError):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(message)


def build_parser() -> argparse.ArgumentParser:
    p = Parser(prog="pmm_cli", description="PhotoMovieMaker を画面なしで使うための JSON CLI")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("capabilities", help="できることの一覧")
    s = sub.add_parser("scan", help="フォルダー直下の写真を一覧にする")
    s.add_argument("--folder", required=True)
    a = sub.add_parser("analyze", help="おすすめ解析（代替候補グループ・星など）")
    a.add_argument("--folder", required=True)
    a.add_argument("--no-embedding", action="store_true")
    a.add_argument("--no-subjects", action="store_true")
    pl = sub.add_parser("plan", help="Project JSON（上映計画）の叩き台を作る")
    pl.add_argument("--folder", required=True)
    pl.add_argument("--title")
    pl.add_argument("--analyze", action="store_true")
    pl.add_argument("--no-embedding", action="store_true")
    pl.add_argument("--no-subjects", action="store_true")
    pl.add_argument("--target-duration", type=float)
    pl.add_argument("--save")
    pl.add_argument("--overwrite", action="store_true")
    v = sub.add_parser("validate", help="Project JSON を確かめる")
    v.add_argument("--project", required=True)
    r = sub.add_parser("render", help="Project JSON から MP4 を書き出す")
    r.add_argument("--project", required=True)
    r.add_argument("--folder", required=True)
    r.add_argument("--output", required=True)
    r.add_argument("--overwrite", action="store_true")
    r.add_argument("--music-folder", help="BGM 区間があるときの曲のフォルダー（計画の music_folder と同じ）")
    ms = sub.add_parser("music-scan", help="曲のフォルダー直下の曲を一覧にする")
    ms.add_argument("--folder", required=True)
    ma = sub.add_parser("music-analyze", help="曲を技術的に測る（長さ・音量の推移・無音など）")
    ma.add_argument("--folder", required=True)
    c = sub.add_parser("compose", help="SI の決めた選択・順序・タイトル・BGM 区間で Project JSON を組み立てて保存する")
    c.add_argument("--folder", required=True)
    c.add_argument("--edits-json", required=True)
    c.add_argument("--save", required=True)
    c.add_argument("--music-folder")
    c.add_argument("--overwrite", action="store_true")
    pv = sub.add_parser("preview", help="書き出す前の確認と時間割（何も書かない）")
    pv.add_argument("--project", required=True)
    pv.add_argument("--folder", required=True)
    pv.add_argument("--music-folder")
    d = sub.add_parser("director", help="Director Plan（SI の判断の記録）を Project の隣に保存する")
    d.add_argument("--project", required=True)
    d.add_argument("--plan-json", required=True)
    d.add_argument("--overwrite", action="store_true")
    return p


def _json_argument(text: str, name: str):
    import pmm_core as core
    try:
        return json.loads(text)
    except ValueError:
        raise core.CoreError("invalid_edits", f"{name} が JSON として読めません。") from None


def should_stop() -> bool:
    """呼び出し側が中止ファイルを置いたら True（TOOLDOCK_CANCEL_FILE が無ければ常に False）。"""
    marker = os.environ.get("TOOLDOCK_CANCEL_FILE")
    return bool(marker) and os.path.exists(marker)


def emit(payload: dict) -> None:
    # ASCII エスケープにして、Windows の古いコードページでも壊れないようにする
    sys.stdout.write(json.dumps(payload, ensure_ascii=True) + "\n")
    sys.stdout.flush()


def progress(phase: str, done: int, total: int) -> None:
    """進捗は標準エラー出力へ、1行1件の JSON で出す（呼び出し側が読んでもよい）。"""
    sys.stderr.write(json.dumps({"event": "progress", "phase": phase,
                                 "done": done, "total": total}) + "\n")
    sys.stderr.flush()


def run(argv: list[str]) -> int:
    try:
        args = build_parser().parse_args(argv)
    except UsageError as e:
        emit({"ok": False, "format": OUTPUT_FORMAT,
              "error": {"code": "usage_error", "message": str(e)}})
        return 2

    import pmm_core as core   # 重いライブラリの読み込みは、引数を確かめてから

    try:
        if args.command == "capabilities":
            result = core.get_capabilities()
        elif args.command == "scan":
            result = core.scan_media(args.folder)
        elif args.command == "analyze":
            result = core.analyze_photos(args.folder, use_embedding=not args.no_embedding,
                                         detect_subjects=not args.no_subjects,
                                         progress=progress, should_stop=should_stop)
        elif args.command == "plan":
            result = core.create_project_plan(
                args.folder, title=args.title, analyze=args.analyze,
                use_embedding=not args.no_embedding, detect_subjects=not args.no_subjects,
                target_duration_seconds=args.target_duration, progress=progress,
                should_stop=should_stop)
            if args.save:
                saved = core.save_project(result, args.save, overwrite=args.overwrite)
                result = {"project": result, "saved_to": saved}
            else:
                result = {"project": result, "saved_to": None}
        elif args.command == "music-scan":
            result = core.scan_music(args.folder)
        elif args.command == "music-analyze":
            result = core.analyze_music(args.folder, progress=progress, should_stop=should_stop)
        elif args.command == "compose":
            result = core.compose_project(args.folder, _json_argument(args.edits_json, "edits_json"), args.save,
                                          music_folder=args.music_folder, overwrite=args.overwrite)
        elif args.command == "preview":
            result = core.preview_project(args.project, args.folder, args.music_folder)
        elif args.command == "director":
            result = core.save_director_plan(args.project, _json_argument(args.plan_json, "plan_json"),
                                             overwrite=args.overwrite)
        elif args.command == "render":
            result = core.render_project(args.project, args.folder, args.output,
                                         overwrite=args.overwrite, music_folder=args.music_folder,
                                         progress=progress, should_stop=should_stop)
        else:  # validate
            result = {"project": core.load_project(args.project), "valid": True}
    except core.CancelledError as e:
        emit({"ok": False, "format": OUTPUT_FORMAT,
              "error": {"code": e.code, "message": e.message}})
        return 130
    except core.CoreError as e:
        emit({"ok": False, "format": OUTPUT_FORMAT,
              "error": {"code": e.code, "message": e.message}})
        return 1
    except KeyboardInterrupt:
        emit({"ok": False, "format": OUTPUT_FORMAT,
              "error": {"code": "cancelled", "message": "処理を中止しました。"}})
        return 130
    except Exception as e:     # 想定外でも JSON で返す（詳細は標準エラーへ）
        import traceback
        traceback.print_exc(file=sys.stderr)
        emit({"ok": False, "format": OUTPUT_FORMAT,
              "error": {"code": "internal_error", "message": f"{type(e).__name__}: {e}"}})
        return 1

    emit({"ok": True, "format": OUTPUT_FORMAT, "api_version": core.API_VERSION,
          "command": args.command, "result": result})
    return 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    return run(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
