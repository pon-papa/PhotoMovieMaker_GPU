# v0.2.0 公開配布の準備（開発者向けの記録）

「自分の PC で動く」から「知らない人へ渡しても動く」へ。機能は増やさず、配布のしかたを固めます。
この文書は開発者向けで、配布 ZIP には入れません。

## R0. 配布前の調査（2026-10-01、コードを変える前）

対象: main `b3b8c03`、公開中の Release は v0.1.1（13 ファイル: 画面本体・`subject_detector.py`・検出モデル 2 つ・
ライセンス・`setup.bat` / `run.bat` / `requirements.txt` / README。フォルダーで包まず ZIP の直下に展開される）。

### 見つかった問題

| # | 問題 | 第三者への影響 |
|---|---|---|
| 1 | `setup.bat` が `py -m pip install` で **グローバルの Python** へ入れる。`.venv` を作らない | 利用者の Python 環境を書き換える。ほかのツールと依存がぶつかる |
| 2 | `run.bat` もグローバルの `py` で起動する | setup をしていなくても起動しようとし、import エラーになる |
| 3 | Python が無い・版が合わないときの案内が無い | `'py' is not recognized …` で終わり、何をすればよいか分からない |
| 4 | **NVENC の判定が「エンコーダー名が一覧にあるか」だけ**。imageio-ffmpeg の FFmpeg にも `h264_nvenc` が入っている | **NVIDIA の無い PC では「自動」が NVENC を選び、書き出しが失敗する** |
| 5 | バージョン表記がばらばら（Core / manifest は `0.1.1+external-api`、README は「v0.1.1（+ 開発中）」、画面には表示なし） | どの版を使っているか分からない |
| 6 | 診断の手段が無い | 不具合の報告に必要な情報を集められない |
| 7 | 配布 ZIP を手作業で作っていた。モデルは Git 管理外で、サイズ・SHA-256 の検査が無い。ZIP に版・commit の記録が無い | 欠けた・違うモデルが混ざっても気付けない。ZIP とソースの対応が分からない |
| 8 | README に一般利用者向けと開発者向け（CLI・SI Director・ToolDock manifest）が混在。セットアップの説明はグローバル導入前提 | 最初に何をすればよいか分かりにくい。AI による自動編集が付いてくると誤解されうる |
| 9 | CHANGELOG・リリースノートが無い | v0.1.1 から何が変わったか分からない |
| 10 | 確認できる Python は 3.13 だけ（この PC に 3.11 / 3.12 は無い） | 3.11 / 3.12 は「対応見込み・未確認」としか書けない |

### 問題が無かった点

- 追跡しているファイルに、個人のパス・名前・秘密の値・benchmark の素材は無い
- アプリのコードに通信は無い（`urllib` / `requests` / `socket` などを使っていない）。モデルを実行時に取得することも無い
- モデル 3 つ（YuNet・YOLOX・DINOv2）のライセンス本文は `licenses/` にあり、`THIRD_PARTY_NOTICES.md` に入手元と権利者がある
- `setup.bat` / `run.bat` は `cd /d "%~dp0"` で自分の場所を基準にしている（別のフォルダーから起動しても動く形）
- FFmpeg は PATH を優先し、無ければ imageio-ffmpeg のものを使う（`libx264`・`aac` あり）
- GitHub Actions の workflow は無い（今回は大きな CI は入れない）

## R1〜R4. 直したこと

| 段階 | 内容 |
|---|---|
| R1 | `setup.bat`: アプリのフォルダーの中に `.venv` を作り、その中だけへ入れる。Python 3.13 / 3.12 / 3.11 を `py` 優先で探し、無ければ理由と対処を表示。2 回実行しても壊れない。`run.bat`: `.venv` の Python だけで起動。どちらも `%~dp0` 基準・変数は引用符つき・丸括弧のブロックなし（日本語・空白・括弧・`#`・`&` を含む場所で動く）。NVENC は小さな試し書き出しで「実際に使えるか」を確かめる |
| R2 | `diagnose.bat` / `app_doctor.py`（何も変更しない・個人の情報を出さない）。バージョンの正本を `pmm_version.py` の 1 か所に |
| R3 | README を配布 ZIP だけを持つ人向けに書き直し、開発者向けは `docs/DEVELOPER.md` へ。`CHANGELOG.md`・`RELEASE_NOTES_v0.2.0.md`・Issue のテンプレート |
| R4 | `tools/build_release.py`: commit の中身（`git archive`）＋ハッシュを確かめたモデルだけから clean な staging を作り、検査してから ZIP と `SHA256SUMS.txt` を作る |

## R5. Release Gate（配布 ZIP を展開して確かめる）

`tools/verify_release.py` は、ZIP をソースとは別のフォルダーへ展開し、**展開物だけ**で次を確かめます
（開発用のフォルダー・PYTHONPATH・開発用の Python のパッケージには頼らない。写真と曲はその場で作る合成物）。

- ふつうの場所と、日本語・空白・括弧・`#`・`&` を含む場所の 2 か所で: 展開 → 禁止ファイルと個人の情報の検査 →
  manifest のハッシュ照合 → setup 前の `run.bat` の案内 → `setup.bat`（.venv なしから）→ もう一度 `setup.bat` →
  診断 → `run.bat` で窓が開いて閉じる → BGM 付きの書き出し
- 書き出し: 写真だけ／BGM 付き CPU x264／自動 → NVENC（使える PC のとき）／NVENC が使えない PC のまね → CPU／
  被写体追従／安全な構図
- PATH から FFmpeg を外した状態（その確認のプロセスだけ）で、診断と書き出しが imageio-ffmpeg の FFmpeg で動く
- YuNet・YOLOX・DINOv2 を読み込んで動かす（実行時のダウンロードなし）。アプリの Python 側が通信しようとしたら失敗にする
- CLI: capabilities・compose → render（subject_safe・つなぎ方・limiter）
- 元の写真と曲が変わっていないこと、一時フォルダー・書き出し先に作りかけが残っていないこと

### 確かめられていないこと（正直に）

- Python 3.11 / 3.12（この PC には 3.13 しか無い）。README では「未確認」と書いている
- NVIDIA の GPU が本当に無い PC での書き出し（試し書き出しが失敗する状態をまねて、自動が CPU になることは確かめた）
- ブラウザーでダウンロードした ZIP に付く印（Mark of the Web）で出る Windows の警告の実際の文面

## リリースの手順（人の最終確認のあと）

```powershell
py -3 tools/build_release.py          # dist/ に ZIP・SHA256SUMS.txt・リリースノート
py -3 tools/verify_release.py         # Release Gate（全部 OK で終了コード 0）
git tag v0.2.0 <ZIP を作った commit>   # RELEASE_MANIFEST.json の git_commit と同じ commit
git push origin v0.2.0
gh release create v0.2.0 dist/PhotoMovieMaker_GPU_v0.2.0_Windows.zip dist/SHA256SUMS.txt `
  --title "PhotoMovieMaker GPU v0.2.0" --notes-file dist/RELEASE_NOTES_v0.2.0.md
```

ZIP は commit の中身から作るので、同じ commit からは同じファイルの集合・同じ順番・同じ時刻の ZIP になります。
`dist/` は Git に入れません（配布物は GitHub Releases に置く）。

## 公開の MCP Bridge（次の段階の候補・今回は実装しない）

結論: **実装できる見込み（YES）**。v0.2.0 の Release Gate には含めない。

- 形: 本体とは別の、任意で入れる小さなサーバー（公式の MCP Python SDK を使う）。PhotoMovieMaker 本体は MCP SDK に依存しないまま
- 中身: 既にある安全な操作（`pmm_cli.py` の scan / analyze / music-scan / music-analyze / compose / preview / render /
  validate / director）だけを、決まった引数で呼ぶ。任意のコマンドや任意のパスは受け付けない
- 守ること（本体が既に保証しているもの）: 写真・曲は読むだけ、フォルダーの直下だけ、曲は明示した曲のフォルダーの中だけ、
  写真・曲のフォルダーの中へは書き出さない、実行時のダウンロードなし
- Bridge 側で足すもの: 起動時に渡す「写真・曲・書き出し先」の許可フォルダーと、その中に収まっているかの確認、
  進捗（CLI は stderr に JSON 行を出す）と中止（中止ファイルの環境変数）の橋渡し、公式 SDK のクライアントでの通し試験
- 依存: `mcp` は任意（別の requirements）。ToolDock は要らない
