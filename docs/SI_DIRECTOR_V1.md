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
