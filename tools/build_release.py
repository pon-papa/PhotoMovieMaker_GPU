# -*- coding: utf-8 -*-
"""配布 ZIP（PhotoMovieMaker_GPU_v<版>_Windows.zip）を組み立てる。

    py -3 tools/build_release.py            … dist/ に ZIP・SHA256SUMS.txt・リリースノートを作る
    py -3 tools/build_release.py --plan     … 何を入れて何を入れないかだけを表示する（何も作らない）

決まり:
- 入れるのは、Git で追跡しているファイルのうち下の SHIP に書いたものと、ハッシュを確かめたモデルだけ。
  作業フォルダーを丸ごと固めない。追跡ファイルは作業ツリーではなく commit（HEAD）の中身から取り出す
- 追跡ファイルは、SHIP（入れる）か EXCLUDE（入れない）のどちらかに必ず当てはめる。どちらでもないファイルが
  あれば止まる（知らないうちに入る・抜けるのを防ぐ）
- clean な staging フォルダーへ並べ、禁止ファイル・個人の情報・秘密の値が無いことを確かめてから ZIP にする
- ZIP の中身（ファイルの集合・順番・時刻）は commit で決まる。時刻は commit の時刻
"""

from __future__ import annotations

import argparse
import fnmatch
import getpass
import hashlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app_doctor  # noqa: E402  （モデルの期待値の表）
import pmm_version  # noqa: E402

VERSION = pmm_version.__version__
NAME = f"PhotoMovieMaker_GPU_v{VERSION}"
ZIP_NAME = f"{NAME}_Windows.zip"
DIST = ROOT / "dist"

# 配布 ZIP に入れる追跡ファイル
SHIP = [
    "PhotoMovieMaker_GPU.py", "subject_detector.py", "photo_recommendation.py", "photo_embedding.py",
    "pmm_version.py", "app_doctor.py", "pmm_core.py", "pmm_cli.py",
    "setup.bat", "run.bat", "diagnose.bat", "requirements.txt",
    "README.md", "CHANGELOG.md", f"RELEASE_NOTES_v{VERSION}.md", "LICENSE", "THIRD_PARTY_NOTICES.md",
    "licenses/DINOv2-Apache-2.0.txt", "licenses/YOLOX-Apache-2.0.txt", "licenses/YuNet-MIT.txt",
    "models/README.md",
]
# 配布 ZIP に入れない追跡ファイル（開発用）。通常の利用には要らない
EXCLUDE = [".gitignore", ".gitattributes", ".github/*", "tests/*", "docs/*", "tools/*", "tooldock.tool.json",
           "RELEASE_NOTES_v*.md"]
# staging にあってはいけないもの
FORBIDDEN_NAMES = ["*.mp4", "*.mov", "*.avi", "*.mkv", "*.zip", "*.pyc", "*.pyo", "*.part", "*.log", "*.tmp", "*.bak",
                   "*.jpg", "*.jpeg", "*.png", "*.webp", "*.bmp", "*.tif", "*.tiff", "*.heic",
                   "*.mp3", "*.wav", "*.m4a", "*.aac", "*.flac", "*.ogg",
                   "*.photomovie.json", "*.director.json", "*_settings.json", "*.sqlite", "*.db",
                   "__pycache__", ".venv", "venv", ".git", ".github", ".pytest_cache", ".idea", ".vscode",
                   "tests", "docs", "tools", "dist", "userdata", "tooldock.tool.json", "Thumbs.db", "desktop.ini"]
TEXT_SUFFIXES = {".py", ".bat", ".md", ".txt", ".json"}
# 個人の情報・作者の環境・秘密の値に当たる文字列（大文字小文字を区別しない）
FORBIDDEN_TEXT = [r"[a-z]:\\users\\", r"/users/", r"\\desktop\\", r"\bdesktop\b", r"[\\/]appdata[\\/]", r"onedrive",
                  r"\.tooldock", r"si_director_v\d_benchmark", r"\bbenchmark", r"てすとbgm",
                  r"api[_-]?key", r"\bsecret\b", r"\bpassword\b", r"\bpasswd\b", r"access[_-]?token",
                  r"bearer\s+[a-z0-9]", r"ghp_[a-z0-9]{20,}", r"sk-[a-z0-9]{20,}"]


def git(*args: str, binary: bool = False):
    out = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, check=True).stdout
    return out if binary else out.decode("utf-8").strip()


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def plan() -> dict:
    """追跡ファイルを「入れる」「入れない」に分ける。どちらでもないものがあれば報告する。"""
    tracked = git("-c", "core.quotepath=off", "ls-files").splitlines()
    ship = [f for f in tracked if f in SHIP]
    excluded = [f for f in tracked if f not in SHIP and any(fnmatch.fnmatch(f, pat) for pat in EXCLUDE)]
    unknown = [f for f in tracked if f not in ship and f not in excluded]
    missing = [f for f in SHIP if f not in tracked]
    return {"ship": ship, "excluded": excluded, "unclassified": unknown, "ship_but_not_tracked": missing,
            "models": [m["file"] for m in app_doctor.EXPECTED_MODELS]}


def personal_words() -> list[str]:
    """この PC に固有の名前（ZIP に入っていてはいけない）。表示はしない。"""
    words = {getpass.getuser(), Path.home().name, socket.gethostname(), os.environ.get("COMPUTERNAME", "")}
    try:
        email = git("config", "user.email")
        words.update({email, email.split("@")[0]})
    except subprocess.CalledProcessError:
        pass
    # 「User」のようなありふれた名前は、普通の文章にも出てくるので見ない（パスの形は FORBIDDEN_TEXT で見ている）
    generic = {"user", "users", "admin", "administrator", "owner", "public", "guest", "desktop", "home"}
    # 公開しているリポジトリの所有者名（README の URL・LICENSE に出る）は個人の情報として扱わない
    try:
        owner = re.search(r"github\.com[:/]([^/]+)/", git("remote", "get-url", "origin"))
        if owner:
            generic.add(owner.group(1).lower())
    except subprocess.CalledProcessError:
        pass
    return sorted(w.lower() for w in words if w and len(w) >= 4 and w.lower() not in generic)


def scan(folder: Path) -> list[str]:
    """staging / 展開物の検査。問題の一覧を返す（空なら問題なし）。場所は folder からの相対で書く。"""
    problems = []
    personal = personal_words()
    for path in sorted(folder.rglob("*")):
        rel = path.relative_to(folder).as_posix()
        for part in path.relative_to(folder).parts:
            if any(fnmatch.fnmatch(part.lower(), pat.lower()) for pat in FORBIDDEN_NAMES):
                problems.append(f"禁止されたファイル・フォルダー: {rel}")
                break
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            problems.append(f"UTF-8 で読めない: {rel}")
            continue
        lowered = text.lower()
        for pattern in FORBIDDEN_TEXT:
            found = re.search(pattern, lowered)
            if found:
                problems.append(f"禁止された文字列（{pattern}）: {rel}")
        for word in personal:
            if word in lowered:
                problems.append(f"この PC に固有の名前が入っている: {rel}")
        if path.suffix.lower() == ".bat":
            if b"\n" in raw.replace(b"\r\n", b"") or raw.startswith(b"\xef\xbb\xbf"):
                problems.append(f"bat は CRLF・BOM なしにする: {rel}")
        if path.suffix.lower() == ".json":
            try:
                json.loads(text)
            except ValueError:
                problems.append(f"JSON として読めない: {rel}")
        if path.suffix.lower() == ".py":
            try:
                compile(text, rel, "exec")
            except SyntaxError:
                problems.append(f"Python として読めない: {rel}")
    return problems


def check_models() -> list[dict]:
    """同梱するモデルを、名前・大きさ・SHA-256 で確かめる。合わなければ止まる。"""
    result = []
    for m in app_doctor.EXPECTED_MODELS:
        path = ROOT / "models" / m["file"]
        if not path.is_file():
            raise SystemExit(f"モデルがありません: models/{m['file']}（models/README.md の手順で取得してください）")
        size, digest = path.stat().st_size, sha256(path)
        if size != m["bytes"] or digest != m["sha256"]:
            raise SystemExit(f"モデルの中身が期待と違います: models/{m['file']}（{size} バイト・{digest}）")
        result.append({"file": f"models/{m['file']}", "bytes": size, "sha256": digest, "used_for": m["used_for"]})
    return result


def build(allow_dirty: bool = False) -> dict:
    files = plan()
    if files["unclassified"] or files["ship_but_not_tracked"]:
        raise SystemExit("追跡ファイルの扱いが決まっていません（tools/build_release.py の SHIP / EXCLUDE を直してください）: "
                         f"{files['unclassified'] + files['ship_but_not_tracked']}")
    dirty = git("status", "--porcelain", "--untracked-files=no")
    if dirty and not allow_dirty:
        raise SystemExit("commit していない変更があります。commit してから作ってください（ZIP は commit の中身から作ります）。")
    commit = git("rev-parse", "HEAD")
    commit_time = int(git("show", "-s", "--format=%ct", "HEAD"))
    stamp = datetime.fromtimestamp(commit_time, timezone.utc)
    models = check_models()

    staging = DIST / "staging"
    if staging.exists():
        shutil.rmtree(staging)
    app_dir = staging / NAME
    app_dir.mkdir(parents=True)
    # 追跡ファイルは commit の中身から（.gitattributes の改行の決まりも反映される）
    archive = git("archive", "--format=tar", "HEAD", "--", *files["ship"], binary=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            target = app_dir / member.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(tar.extractfile(member).read())
    for m in models:
        shutil.copyfile(ROOT / m["file"], app_dir / m["file"])

    listing = [{"path": p.relative_to(app_dir).as_posix(), "bytes": p.stat().st_size, "sha256": sha256(p)}
               for p in sorted(app_dir.rglob("*")) if p.is_file()]
    lo, hi = pmm_version.SUPPORTED_PYTHON
    manifest = {
        "app": "PhotoMovieMaker GPU", "version": VERSION, "git_commit": commit,
        "source_date_utc": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "platform": "Windows 11",
        "supported_python": f"{lo[0]}.{lo[1]} - {hi[0]}.{hi[1]}",
        "verified_python": "3.13",
        "requirements": (app_dir / "requirements.txt").read_text(encoding="utf-8").split(),
        "models": models,
        "not_bundled": ["Python", "Python packages (installed into .venv by setup.bat)", "FFmpeg"],
        "files": listing,
    }
    (app_dir / "RELEASE_MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n",
                                                   encoding="utf-8", newline="\n")

    expected = sorted(files["ship"] + [m["file"] for m in models] + ["RELEASE_MANIFEST.json"])
    actual = sorted(p.relative_to(app_dir).as_posix() for p in app_dir.rglob("*") if p.is_file())
    if actual != expected:
        raise SystemExit(f"staging の中身が計画と違います: {sorted(set(actual) ^ set(expected))}")
    problems = scan(staging)
    if problems:
        raise SystemExit("staging の検査で問題が見つかりました:\n  " + "\n  ".join(problems))

    DIST.mkdir(exist_ok=True)
    zip_path = DIST / ZIP_NAME
    date_time = (stamp.year, stamp.month, stamp.day, stamp.hour, stamp.minute, stamp.second)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel in actual:                               # 名前の順（決まった並び）
            info = zipfile.ZipInfo(f"{NAME}/{rel}", date_time=date_time)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            info.create_system = 0
            z.writestr(info, (app_dir / rel).read_bytes(), compresslevel=9)
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        if names != [f"{NAME}/{rel}" for rel in actual] or z.testzip() is not None:
            raise SystemExit("ZIP の中身が staging と違います。")
    digest = sha256(zip_path)
    (DIST / "SHA256SUMS.txt").write_text(f"{digest}  {ZIP_NAME}\n", encoding="utf-8", newline="\n")
    shutil.copyfile(app_dir / f"RELEASE_NOTES_v{VERSION}.md", DIST / f"RELEASE_NOTES_v{VERSION}.md")
    return {"zip": ZIP_NAME, "bytes": zip_path.stat().st_size, "sha256": digest, "git_commit": commit,
            "entries": len(names), "top_level": sorted({rel.split("/")[0] for rel in actual}),
            "unpacked_bytes": sum(f["bytes"] for f in listing), "dirty": bool(dirty)}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="配布 ZIP を組み立てる")
    parser.add_argument("--plan", action="store_true", help="入れるもの・入れないものを表示するだけ")
    parser.add_argument("--allow-dirty", action="store_true", help="commit していない変更があっても作る（確認用。公開には使わない）")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    if args.plan:
        print(json.dumps(plan(), ensure_ascii=False, indent=1))
        return 0
    print(json.dumps(build(allow_dirty=args.allow_dirty), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
