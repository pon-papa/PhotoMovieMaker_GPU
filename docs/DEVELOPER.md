# 開発者向け: AI・自動化から使う

**通常の利用（画面で動画を作る）には、この文書の内容は一切不要です。**
ToolDock・MCP・AI をインストールしなくても、PhotoMovieMaker はこれまでどおり単体で動きます。
画面はこの文書の CLI も `tooldock.tool.json` も読み込まず、通信もしません。

この文書は、AI や自動化から操作したい場合だけのものです。
画面を使わずに、写真の一覧・おすすめ解析・上映計画づくりを呼び出せます。
作者の環境では、別の（非公開の）連携ツール ToolDock がこの CLI を呼び出しています。
MCP の処理はその連携ツール側の役目なので、PhotoMovieMaker に MCP SDK などの追加の依存はありません。
ToolDock が無くても、CLI は単体でそのまま使えます。

役割分担は次のとおりです。

- **AI（または人）**：何を作るか、どの写真を使うか、どう並べるか、何秒見せるかを決める
- **PhotoMovieMaker**：決まった内容を、解析・変換・書き出しとして確実に実行する

写真を勝手に外したり並べ替えたりする判断は、PhotoMovieMaker には入れていません。

## JSON CLI

標準出力には JSON だけを出し、進捗は標準エラー出力へ出します。

```powershell
.venv\Scripts\python.exe pmm_cli.py capabilities
.venv\Scripts\python.exe pmm_cli.py scan --folder "C:\写真\広島旅行"
.venv\Scripts\python.exe pmm_cli.py analyze --folder "C:\写真\広島旅行"
.venv\Scripts\python.exe pmm_cli.py plan --folder "C:\写真\広島旅行" --title "広島旅行" --analyze --save "C:\計画\広島.photomovie.json"
.venv\Scripts\python.exe pmm_cli.py validate --project "C:\計画\広島.photomovie.json"
.venv\Scripts\python.exe pmm_cli.py render --project "C:\計画\広島.photomovie.json" --folder "C:\写真\広島旅行" --output "C:\動画\広島.mp4"
```

| コマンド | 内容 |
| --- | --- |
| `capabilities` | できること・対応形式・モデルの有無 |
| `scan` | フォルダー直下の写真の一覧（名前・大きさ・縦横） |
| `analyze` | おすすめ解析（代替候補グループ・同じ写真の別バージョン・推奨度・人物/犬の数・撮影時刻） |
| `plan` | Project JSON（上映計画）の叩き台 |
| `validate` | Project JSON が正しい形かの確認 |
| `render` | Project JSON のとおりに MP4 を書き出す（画面と同じ処理） |

終了コードは 0=成功 / 1=処理できなかった / 2=使い方の誤り / 130=中止 です。
エラーのときも JSON（`"ok": false` と `error.code`）を返します。

- 元の写真は**読むだけ**です。削除・移動・名前の変更・上書きはしません
- フォルダーの**直下だけ**を見ます。サブフォルダーやドライブ全体は探しません
- 通信はしません。モデルを実行時にダウンロードすることもありません
- `render` は画面と同じ `VideoRenderer` で書き出します。同じ設定なら、画面から作った MP4 と同じものになります
  - 使うのは `enabled: true` の写真だけで、`order` の順に並びます。`video` と `title_card`（被写体追従カメラを含む）は計画どおりです
  - `--folder` には計画の `source_folder` と同じフォルダーを指定します。写真のフォルダーの中には書き出しません
  - 書き出しは一時フォルダーで行い、完成した MP4 と `*_settings.json` だけを置きます。中止・失敗のときは何も残しません
  - 同じ名前のファイルは `--overwrite` を付けない限り上書きしません
  - BGM 付きも書き出せます。`--music-folder` に計画の `music_folder` と同じ曲のフォルダーを指定し、曲はその直下のものだけを使います（画面と同じ曲の合成）
- 環境変数 `TOOLDOCK_CANCEL_FILE` が指すファイルが現れると、解析・書き出しを安全に止めます（自動化から使うときだけ。画面には関係しません）

## SI から写真と BGM で作品を組み立てる（SI Director）

AI（SI）が写真と BGM の候補を見比べて、選択・順序・選曲・曲の区間・タイトルを決め、
PhotoMovieMaker がそのとおりに書き出すための部品があります（`music-scan`・`music-analyze`・`compose`・`preview`・`director`）。
判断は SI 側で、PhotoMovieMaker の中に AI は入っていません。仕組みと約束ごとは [docs/SI_DIRECTOR_V1.md](SI_DIRECTOR_V1.md) にあります。実作品を見て直したところ（安全な構図・つなぎ方・音声の安全・曲のページ送り）は [docs/SI_DIRECTOR_V2.md](SI_DIRECTOR_V2.md)。

## Project JSON（上映計画）

AI と PhotoMovieMaker の間でやり取りする「何をどう上映するか」の計画です。
ファイル名は `*.photomovie.json` にします。

新しい形式を別に作るのではなく、動画を作ったときに保存する
`*_settings.json` と**同じ区画名・同じ中身**（`video`・`title_card`・`bgm_timing`・`bgm_segments`）を使っています。

```json
{
  "kind": "photomoviemaker.project",
  "schema_version": 1,
  "project": {"title": "広島旅行", "source_folder": "C:\\写真\\広島旅行", "target_duration_seconds": 240},
  "media": [
    {"file": "001.jpg", "type": "image", "enabled": true, "order": 1,
     "candidate_group": "G01", "variant_family": null, "stars": 3}
  ],
  "video": {"width": 1920, "height": 1080, "fps": 30, "interval_seconds": 8.0,
            "transition_seconds": 1.0, "zoom_percent": 8.0, "blur_background": true,
            "camera_mode": "legacy", "encoder_choice": "auto"},
  "title_card": {"enabled": true, "main": "広島旅行", "...": "..."},
  "bgm_timing": {"first_offset": 3.0, "...": "..."},
  "bgm_segments": [],
  "recommendation_engine": {"grouping": "selection_candidate_groups", "...": "..."},
  "extensions": {}
}
```

- `media[].file` はファイル名だけです（フォルダーの外を指す指定は受け付けません）
- 叩き台では、写真はすべて `enabled: true`・フォルダーの順です。外す・並べ替えるのは計画を書く側です
- `type` は今は `image` だけを扱います。`video`（写真の間に挟む動画）は将来のために形だけ用意しています
- トランジションの種類や、曲の拍・小節に合わせた演出などは、将来 `extensions` に入れる予定です
- `project.source_folder` には写真フォルダーの絶対パスが入ります。共有するときは注意してください

## ToolDock から見つけてもらうための manifest

フォルダー直下の `tooldock.tool.json` に、この CLI でできること（操作・入力と出力の形・
読むだけか書くか・どの引数がフォルダーやファイルか）を、ToolDock Connector v1 の形式で書いてあります。

- ToolDock はこのファイルを読むだけで、PhotoMovieMaker を起動せずに能力を知ることができます
- AI から使うときも、ToolDock の汎用の仕組みがこの宣言どおりに CLI を呼び出します。
  PhotoMovieMaker 専用のコードは ToolDock 側にありません
- 宣言と CLI が食い違わないことは、`tests/test_external_api.py` で確かめています
- このファイルが無くても・壊れていても、画面と CLI の動作には影響しません。
  ToolDock・MCP が無い状態で画面・CLI・動画書き出しが動くことは `tests/test_standalone.py` で確かめています

## CLI のそのほかのコマンド（SI Director）

```powershell
.venv\Scripts\python.exe pmm_cli.py music-scan --folder "C:\曲"
.venv\Scripts\python.exe pmm_cli.py music-analyze --folder "C:\曲" --offset 0 --limit 30
.venv\Scripts\python.exe pmm_cli.py compose --folder "C:\写真\旅行" --edits-json "{...}" --save "C:\計画\旅行.photomovie.json" --music-folder "C:\曲"
.venv\Scripts\python.exe pmm_cli.py preview --project "C:\計画\旅行.photomovie.json" --folder "C:\写真\旅行" --music-folder "C:\曲"
.venv\Scripts\python.exe pmm_cli.py director --project "C:\計画\旅行.photomovie.json" --plan-json "{...}"
```

| コマンド | 内容 |
| --- | --- |
| `music-scan` | 曲のフォルダー直下の曲の一覧（一覧の指紋 `listing_fingerprint` つき） |
| `music-analyze` | 曲の技術的な測定（長さ・ラウドネス・音量の推移など）。30 曲を超えるフォルダーは `--offset` / `--limit` で 30 曲ずつ |
| `compose` | 呼び出し側が決めたとおりの Project JSON を組み立てて保存し、時間割を返す |
| `preview` | 書き出す前の確認と時間割（曲の区間の音量の見込み・写真の境目ごとのつなぎ方を含む） |
| `director` | 判断の記録（Director Plan）を Project の隣に保存する |

Project JSON で指定でき、**画面からは使えない**もの（v0.2.0）:

| 指定 | 内容 |
| --- | --- |
| `video.camera_mode: "subject_safe"` | 見つかった被写体の枠（顔・人物・犬）＋余白を、最後のフレームまでできるだけ画面に残す |
| `transitions` | 写真の境目ごとのつなぎ方（`crossfade` / `cut` / `fade_black` / `slide`）。確率の profile を compose で一度だけ確定 |
| `audio_safety` | `headroom`（音量を下げる）/ `limiter`（AAC 後のトゥルーピークを測って上限を守る） |
| `bgm_segments[].gain_db` | その区間の曲だけの音量 |

これらを指定しない Project は、v0.1 系と同じ MP4 になります。

曲の長さの確認など一部の情報には `ffprobe` を使います。PATH に FFmpeg 一式が無く、imageio-ffmpeg の FFmpeg だけの
環境では `ffprobe` が無いため、その情報（曲の形式・タグ・繰り返しの警告）は出ません。書き出しそのものは動きます。
