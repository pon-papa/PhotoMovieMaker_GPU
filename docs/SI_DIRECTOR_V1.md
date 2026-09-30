# SI Director v1 — SI が写真と BGM で一本の作品を組み立てる

PhotoMovieMaker は AI アプリではありません。**どう見せるか（写真の選択・順序・構成・選曲・曲の区間・タイトル）は外側の SI が判断**し、
PhotoMovieMaker は、その判断を安全に・再現できる形で MP4 にするレンダリングエンジンとして働きます。
PhotoMovieMaker の中に LLM や AI の API は入れません。画面（`run.bat`）は今までどおり単体で使えます。

| 役割 | 持つもの |
|---|---|
| 人 | 目的・制約・素材（写真フォルダー・曲フォルダー）・最終判断 |
| SI | 写真の選択・順序・構成・選曲・曲の区間・タイトル・演出の判断（Director Plan に記録） |
| PhotoMovieMaker | 写真の解析・曲の技術的な測定・安全な Project の組み立てと検査・既存の renderer による映像と音声の合成 |

---

## 1. 今の BGM の仕組み（調査・2026-10-01、コードは変えずに確認）

### 画面（GUI）

- 「BGM」欄に、全体で 1 組のつなぎ方（`BGMTiming`）と、区間の一覧（`BGMSegment`）がある
- 区間 = **開始写真〜終了写真（上映順の何枚目か）** と、その区間に流す曲 1 つ。曲はファイル選択画面で選ぶ
  （絞り込みは `*.mp3 *.wav *.m4a *.aac *.flac *.ogg`。「すべてのファイル」も選べる）
- 写真の番号は `shown_images()`＝**使う写真だけを上映順に数えた番号**。並べ替えても「何枚目」の意味のまま（曲はファイルではなく位置に付く）
- つなぎ方の既定: 冒頭オフセット 3.0 秒・冒頭フェードイン 1.5・曲間フェードアウト 1.5・曲間無音 0.7・曲間フェードイン 1.5・最終フェードアウト 3.0

### renderer（`VideoRenderer`、画面と Core API で同じもの）

- 映像を先に無音で作り（`render_silent_video`、一時フォルダー `photomovie_*`）、最後に `mux_bgm` で曲を合成する
- `validate_bgm_segments`: 区間の写真番号が範囲内・曲のファイルがある・区間が重ならない
- `bgm_plan`: 区間を実時間へ割り付ける
  - 1 枚の時間 = `interval_seconds`（**全写真で同じ**）。タイトルの秒数ぶん後ろへずれる
  - 写真 1 から始まる区間だけ、タイトル中の `first_offset`（既定 3.0 秒）から鳴らし始める
  - 隣り合う区間の間は「前の曲のフェードアウト → 無音 → 次の曲のフェードイン」（2 曲を重ねない）
  - 最後の区間が動画の最後まで続くときだけ最終フェードアウト。短い区間はフェードを按分、0.30 秒未満の区間は鳴らさない
- `mux_bgm`: FFmpeg 1 回。曲ごとに `-stream_loop -1`（**曲が短ければ繰り返す**）、`atrim`・`afade`・`adelay`、
  `amix`（重ならないので並べるだけ、`normalize=0`）、全長に `apad`＋`atrim`、映像は `-c:v copy`、音声は AAC 256k
- **曲は常に先頭から**使う（曲の途中から使う指定は無い）。音量の調整も無い
- FFmpeg は PATH のもの、無ければ imageio-ffmpeg 同梱のもの。パスは引数の配列で渡すので日本語・空白・記号の名前も通る
- 中止: 映像の描画中は `stop_event` を見て止まる。**曲の合成（`mux_bgm`）の最中は見ていない**（`subprocess.run` で終わるまで待つ）
- 設定ファイル `*_settings.json` に `bgm_timing` と `bgm_segments`（`start_photo`・`end_photo`・`start_file`・`end_file`・`audio`＝曲の絶対パス）を残す

### Project JSON と Connector

- `bgm_timing`（上と同じ項目）と `bgm_segments`（配列）の欄はあるが、`bgm_segments` の中身の形は決まっていない（検査は「配列であること」だけ）
- Connector の `render` は `bgm_segments` が空でなければ `bgm_not_supported` で断り、renderer には常に空の区間を渡している。
  理由（コードの注記）: **曲のファイルの場所を安全に受け取る方法が決まっていなかった**。Project JSON に書かれた曲のパスをそのまま信じると、
  PC のどこのファイルでも読めてしまう
- 変更前の基準: 同じ条件で書き出すと MP4 は毎回同じバイト列になる（BGM なし・BGM 2 曲とも確認）

---

## 2. SI が使える部品（Core API / CLI / ToolDock Connector）

どれも**判断をしない**部品です。選ぶ・並べる・曲を決めるのは SI で、PhotoMovieMaker は決まったことを確かめて形にするだけです。

| 部品 | CLI | 何をするか |
|---|---|---|
| 写真の一覧・解析 | `scan` / `analyze` | 既存のまま（候補群・別バージョン・星・技術点・人物/犬の数・撮影日時が分かれば） |
| 曲の一覧 | `music-scan` | 曲のフォルダー直下の曲（mp3・wav・m4a・aac・flac・ogg）。中身は読まない |
| 曲を測る | `music-analyze` | 下の「曲の測定」 |
| 組み立て | `compose` | SI の決めた選択・順序・タイトル・映像の設定・BGM の区間で、新しい Project JSON を作って保存（既存は上書きしない） |
| 書き出す前の確認 | `preview` | 書き出しと同じ検査と時間割。何も書かない |
| 判断の記録 | `director` | Director Plan を Project の隣に保存 |
| 書き出し | `render` | BGM 付きも可（`--music-folder`） |

### 曲の測定（事実として返すもの・返さないもの）

- **事実（測定値）**: 長さ・入れ物と符号化の形式・サンプルレート・チャンネル・ビットレート・ファイルに書かれたメタデータ（title・artist・album・genre・date、あれば）・
  統合ラウドネスとラウドネスレンジとトゥルーピーク（FFmpeg の ebur128）・1 秒ごとの RMS・全体の RMS とピーク・無音（1 秒の RMS が -50 dBFS 未満）・
  静かな区間（曲全体の RMS より 18 dB 以上小さい 1 秒が 3 秒以上）・10 区分の音量の概形
- **unknown**: テンポ（BPM）・拍・雰囲気（mood）。この PC の環境には拍を測れるライブラリが無く、依存も増やさない
- 明るい・穏やか・懐かしいなどは**測っていない**。SI がファイル名・メタデータ・人のメモ・測定値から推定するときは、推定として Director Plan に書く

### 曲のファイルの安全（BGM の区間）

- Project JSON の `bgm_segments` の 1 件 = `{"start_photo", "end_photo", "audio"}`（任意で `start_file`・`end_file`）。
  写真の番号は画面と同じ「使う写真の上映順で何枚目か」。`audio` は**ファイル名だけ**
- 曲のフォルダーは `project.music_folder`（絶対パス）に書き、**書き出しのときに呼び出し側が同じフォルダーを `music_folder` として明示**する。
  違えば `folder_mismatch`。曲はそのフォルダーの**直下**にあり、形式が上の 6 つで、実体がフォルダーの外にあるリンクではないこと
- 区間は重ならない・範囲は使う写真の枚数以内（検査で断る）。書き出し先は写真・曲のフォルダーの中にできない
- ToolDock から使うときは、写真のフォルダーと曲のフォルダーを両方 `--media-root` に入れる（曲も読むだけの素材）

### BGM 付きの書き出し

- Core / CLI / Connector の書き出しは、確かめた区間を**画面と同じ `VideoRenderer` と `mux_bgm`** に渡すだけ。曲の合成を別に作っていない。
  同じ計画なら、画面の呼び方と Core の書き出しは**同じバイト列の MP4** になる（試験で確認）
- BGM なしの書き出し・画面の BGM 付きの書き出しは、この変更の前と同じバイト列（試験用の基準で確認）
- 曲の合成（`mux_bgm`）の最中でも中止できるようにした（画面の中止ボタンにも効く）。中止すると FFmpeg を止め、作りかけのファイルを残さない

## 3. Director Plan（`<名前>.director.json`）

Project JSON は書き出しの約束、Director Plan は **SI の判断の記録**です。書き出しには使いません（無くても書き出せる）。

- 必須: `director_version: 1`・`concept`（どう見せるか）
- よく使う項目（中身は SI が決める。PhotoMovieMaker は解釈しない）: `benchmark`・`source`（論理的な名前）・`structure`（intro / development / peak / ending など）・
  `selection`（選んだ写真・外した写真と理由）・`ordering`・`music`（選んだ曲・選ばなかった曲と理由・区間・つなぎ方）・`title`・`target_duration_seconds`・
  `known_facts`（事実）・`inferences`（推定）・`uncertainty`・`limitations`・`metrics`（本数・長さ・時間）・`review`
- PhotoMovieMaker が書く項目: `kind`・`project_file`・`saved_at`・`computed`（選んだ枚数・外した枚数・使った曲・時間割）
- 64 KB まで。**絶対パスは書けない**（個人のフォルダー名を残さないため。ファイル名や論理的な名前で書く）

## 4. SI が操作できる演出・まだできない演出

| できる（既存の機能をそのまま） | まだできない |
|---|---|
| 使う写真と順序 | 写真ごとの表示時間（全写真で同じ `interval_seconds`） |
| 1 枚の秒数・クロスフェードの秒数（全体で 1 つ） | 写真ごとのパン方向・ズームの指定（方向はランダム、被写体追従は人物・犬の検出から） |
| ズームの強さ・被写体追従（`camera_mode: subject`）・ぼかし背景 | 新しいトランジション |
| タイトル（main・sub・date・秒数・フェード・色） | 曲の途中から使う・曲の音量を変える |
| 曲ごとの写真の範囲、曲のつなぎ方（全体で 1 組: 冒頭の遅れ・フェード・曲間の無音） | 拍に合わせる（テンポ・拍は unknown） |
| 書き出し前の時間割（曲が短くて繰り返されるかも分かる） | 動画クリップの差し込み |

## 5. 通し試験（合成素材・2026-10-01）

合成写真 12 枚（似た写真の群・ぼけた写真・縦写真を含む）と試験音 3 曲（75 秒・150 秒・25 秒の正弦波。音楽ではない）で、
解析 → 曲の測定 → SI の判断 → 組み立て（Director Plan つき）→ 時間割 → BGM 付き書き出し → 技術的な確認 まで通した。
6 枚を選び、前半に試験音 A・後半に B、C は後半の区間より短く繰り返しになるので採用せず。53.0 秒・1280x720・30 fps・映像 1590 フレーム・AAC 音声あり。
見本フレームの一覧（4 秒ごと）で、タイトルと選んだ順の写真を確かめた。

**SI が完成品をどこまで確かめられるか**: 見本フレームの画像は見られる。音は聞けない（音声の有無・長さ・音量の測定値だけ）。完成品を通して見たとは言わない。

## 6. 公式 MCP SDK からの通し試験（ToolDock 経由・2026-10-01）

ToolDock（コードは変えていない）に、写真のフォルダーと曲のフォルダーを `--media-root`、作品の置き場を `--output-root` として渡した。
公式 MCP SDK から `slideshow_scan_media` → `analyze_photos` → `scan_music` → `analyze_music` → （SI の判断）→ `compose_project`（Director Plan つき）→
`preview_project` → `render`（`music_folder` つき）まで通した。53.0 秒・映像 1590 フレーム・AAC 音声あり、写真と曲は不変。

- 拒否（9 件）: 許可外の曲のフォルダー（一覧・書き出し）・曲のフォルダー無しで BGM 付き書き出し・計画と違う曲のフォルダー・曲のフォルダーへの書き出し・
  パスで外を指す曲・JSON でない指示・絶対パス入りの Director Plan・既存の Project の上書き
- BGM 付き書き出しを途中で中止（MCP の `notifications/cancelled`）→ MP4・`.part`・FFmpeg・一時フォルダーのどれも残らない
