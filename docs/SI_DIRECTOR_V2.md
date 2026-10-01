# SI Director v2 — 実作品を見て気になったところを直す

SI Director v1 では、SI が写真と BGM で一本の作品を最後まで作れるところまで来ました（3 作品の benchmark）。
v2 は「機能を増やす」のではなく、**その 3 作品を作り、人が見て、実際に困ったことだけ**を直します。

| 実際に起きたこと | v2 で入れたもの |
|---|---|
| 人物へ寄ると、ドレスの裾・衣装の端が画面の外へ出る | 安全な構図（`camera_mode: subject_safe`） |
| クロスフェードだけでも見られるが、場面転換にもう少し表情が欲しい | つなぎ方の profile（`transitions`） |
| AAC にしたあと音声のピークが 0 dBFS を超えた（3 作品とも） | 音声の安全（`audio_safety`） |
| 曲全体の音量では、実際に使う区間の音量を予測できなかった | 区間の音量の見込み（preview の `expected_level`）・区間ごとの音量（`gain_db`） |
| 124 曲を渡したら公開 API が 30 曲で止まった | 曲の分析のページ送り（`offset` / `limit`） |

役割は v1 と同じです。**判断は SI、保証はコード**。PhotoMovieMaker の中に AI は入れません。
新しい指定はすべて任意で、**指定の無い Project（v1 の Project）は v1 とまったく同じ MP4** になります。

---

## 1. 安全な構図（`video.camera_mode: "subject_safe"`）

### 今までの仕組み（調査）

- 写真は 1920×1080 の canvas に置かれる（ぼかし背景あり＝写真全体を収めて左右/上下をぼかす。なし＝中央で切り抜く）
- 各写真は `interval_seconds` のあいだに 1.0 倍 → `1 + zoom_percent/100` 倍（既定 8%）へ smoothstep で寄る。
  パンは寄ったぶんの余白の 34% まで
- `subject` は、顔または犬が **1 つだけ** 見つかった写真で、パンの向きをその中心へ向けるだけ（ズーム量は全写真で同じ 8%）。
  顔の中心へ寄るので、全身の写真では下（裾・足元）が余計に切れる

### 直し方（ドレスを見分けるモデルは使わない）

- 「中心へ寄る」に加えて「**大事な被写体の範囲を最後のフレームまで画面に残す**」
- 守る範囲 = 顔・人物・犬の枠の和 ＋ 余白 3%（同じ種類で一番大きい枠の 20% 未満＝遠くの写り込みは除く）
  - 人物の枠は、犬と同じ YOLOX の 1 回の推論の出力（COCO の person）から取り出すだけ。追加のモデルは無い
  - 白いドレスなどで人物のスコアが低い（0.1〜0.3）ことがあったので、**顔が枠の上側にある人物だけ**低いスコアでも採る
  - 枠が写真の端まで届いている辺は、元の写真ですでに切れているので守らない（半身の写真でズームを止めないため）
  - 余白は写真の端までの隙間の半分まで（余白のせいでズームを止めないため）
- 見えている範囲は寄るほど単調に狭くなるので、**最後のフレームで収まっていれば途中も必ず収まる**
- まずパンを抑え、それでも収まらなければズームを 0.5% ずつ弱める（8% → 7.5% → … → 0%）。寄れる範囲では寄る
- 写真ごとに決めたこと（`adjustment`: none / pan_limited / zoom_reduced / zoom_off / no_subject、実際のズーム、守った範囲、
  守らなかった辺）は `*_settings.json` の `safe_framing` と書き出し結果に残る
- `legacy` / `subject` は今までどおり（同じ動き・同じ MP4）

## 2. つなぎ方（`transitions`）

- 種類は 4 つだけ: `crossfade`（従来）・`cut`・`fade_black`（暗転）・`slide`（次の写真が右から押し出す）。
  回転・星形・3D のような派手なものは入れない
- どれもクロスフェードと同じ区間（`transition_seconds`）の中で描くので、**動画の長さ・フレーム数は変わらない**。
  `cut` は 0 秒（次の写真は自分の区間の先頭から）。タイトル → 写真 1 は従来のフェードのまま
- SI が作品ごとに **profile（確率）** を決める。確率は「方針」で、書き出しのたびに乱数は引かない:

```json
"transitions": {
  "profile": {"crossfade": 0.8, "cut": 0.1, "fade_black": 0.1},
  "seed": 260921,
  "overrides": [{"after_photo": 17, "type": "fade_black"}]
}
```

- `compose` が境目ごとの種類を **一度だけ** 確定し、Project の `transitions.sequence` に保存する（`profile`・`seed`・
  `overrides`・`method` も一緒に）。書き出しは保存された並びを再生するだけ → **同じ Project は毎回同じ映像**
- 確定の方法: 境目 i の値 = `sha256("pmm-transition:{seed}:{i}")` の先頭 8 バイト / 2^64。
  seed を指定しなければ、使う写真の並びと profile から決めた seed を保存する
- 最低限の決まり（ヒューリスティックは増やさない）: 暗転・スライドを続けない／同じ特別なつなぎ方を 3 回続けない／
  タイトルの直後（写真 1 → 2）は暗転・スライドにしない。当てはまる種類を外して確率を配り直し、何も残らなければクロスフェード
- `overrides`: 章の切り替わりなどを SI が明示する。profile より優先し、決まりも当てはめない（SI の判断）。
  PhotoMovieMaker は物語の構造を解釈しない
- `preview` / `compose` の時間割に、境目ごとの種類・秒数・開始時刻・前後の写真と、種類ごとの数（`transition_counts`）が入る
- `transitions` の無い Project は、従来どおりすべてクロスフェード（自動で変換しない）

## 3. 音声の安全（`audio_safety`）と区間の音量

| mode | すること |
|---|---|
| `legacy`（指定なしも同じ） | 何もしない。BGM の合成コマンドは v1 と同じ |
| `headroom` | 合成した音声を `headroom_db`（既定 1.0 dB）下げる |
| `limiter` | 4 倍に上げて先読みリミッター（`alimiter`、上限 −0.5 dB の余裕）→ 元の周波数へ。AAC にしたあとトゥルーピークを測り（`ebur128`）、`ceiling_db`（既定 −1.0 dBTP）を超えていれば音量だけ下げて作り直す（最大 2 回） |

- 調べたこと: FFmpeg の標準フィルターだけで、リミッターだけでは AAC 化の上振れ（最大 1 dB ほど）を抑えきれなかった
  （benchmark の曲でリミッター −1 dB のあとも +0.2〜−0.6 dBTP）。そのため **測って直す** 2 段階にした。同じ入力なら同じ結果
- 新しい依存は無い。合成中・測定中も中止でき、作りかけのファイルは残さない
- GUI は今までどおり（legacy）。SI が Project で選んだときだけ効く
- **区間の音量の見込み**: preview / compose の時間割の各曲に `expected_level`（その区間で実際に使う秒＝曲の先頭から
  区間の長さぶん、短い曲は繰り返し、フェードの秒を除く の 1 秒ごとの RMS の中央値・平均）と、前の曲からの差
  `step_from_previous_db` が入る。3 dB を超えると warning。曲全体の値ではない（benchmark C: 曲全体 +2.1 LU → 実際 +3.8 dB）
- **区間ごとの音量**: `bgm_segments[].gain_db`（−24〜+12 dB、任意）。0・指定なしならコマンドは従来と同じ

## 4. 曲の分析のページ送り

- `analyze_music(folder, offset=, limit=, expect_fingerprint=)` / CLI `music-analyze --offset --limit --expect-fingerprint` /
  Connector の `analyze_music`（同じ引数）
- 30 曲を超えるフォルダーは 30 曲ずつ（自然順）。結果の `next_offset` が null になるまで続ける
- `scan_music` の `listing_fingerprint` を `expect_fingerprint` に渡すと、ページの間に一覧が変わったとき `folder_changed` で止まる
- 読めない曲は `failures` に入れて続ける。offset / limit を指定しないときは v1 と同じ（30 曲まで）
- **job にはしなかった**: 30 曲で約 100 秒、既存の 900 秒の時間制限に収まる。ToolDock の Job Runner（connector v2）は
  manifest だけで書けるが、結果を書くフォルダーと人が起動する Runner が要るので、いちばん単純で安全なページ送りにした

## 5. 小さな直し

- 曲のタグの文字化け: ID3 の文字コードが Latin-1 なのに中身が Shift_JIS のタグを直す（`tags_repaired`・元の値は `tags_raw`）。
  C1 制御文字があり、cp932 として読め、全角の日本語になるときだけ（欧文のアクセントは直さない）
- 顔・犬の検出数は参考値（写っていても見落とすことがある）と、解析結果の `meaning` と manifest に明記

## 6. 今回入れなかったもの（設計候補として残す）

| 候補 | 状態 | 理由 |
|---|---|---|
| テンポ（BPM）・拍 | deferred（`tempo_bpm = unknown` のまま） | FFmpeg にテンポを測るフィルターが無い（`atempo` は速さを変えるだけ）。librosa などは無く、新しい依存になる |
| 曲の途中から使う（開始位置） | deferred | LOW。framing・transition・audio・music の後 |
| ファイル名からの日時の推定 | deferred | LOW。推定は事実の `taken_at` と分けて返す設計にする |
| 犬の検出の改善 | deferred | 新しい検出器は入れない。参考値であることを明記した |
| 写真ごとの秒数（最後の 1 枚だけ長く） | deferred | safe framing と transition を見てから |
| 雰囲気（mood） | 入れない | 事実として測らない。推定は SI |

## 7. 確かめたこと（2026-10-01）

### 通し試験（合成素材・公式 MCP SDK × ToolDock）

- 35 曲の曲フォルダー: ページ指定なしは `too_many_tracks`、`offset` / `limit` / `expect_fingerprint` で 2 ページ（30 + 5）、
  scan と同じ自然順。公開 API だけで全曲を測れた
- compose（subject_safe・profile＋章の override・limiter・2 曲目 gain）→ preview に境目ごとのつなぎ方と区間の音量の見込み・
  3 dB を超える段差の warning → 書き出し 2 回で同じ MP4
- v1 形式の Project（v2 の指定なし）は Core と Connector で同じ MP4
- 書き出しの中止: MP4・作りかけ・FFmpeg・一時フォルダーとも残らない。写真・曲は変化なし

### v1 との互換（実作品）

- v1 benchmark の 3 作品の Project JSON（v2 の指定なし）を v2 のコードで書き出し直すと、**3 本とも v1 の MP4 と SHA-256 が一致**
  （NVENC の書き出しも同じ入力なら同じバイト列になることを先に確かめた）

### 3 作品を v2 で書き出し直した結果（成果物はリポジトリの外）

v1 と同じ写真の選択・順序・BGM・尺で、構図（subject_safe）・つなぎ方（profile＋章の override）・音声（limiter −1 dBTP）だけを変えた。

| | 前撮り（35 枚） | 犬（42 枚） | 人物・旅行（38 枚） |
|---|---|---|---|
| 構図の調整 | 6 枚（パンだけ、ズームは 8% のまま） | 12 枚（ズームを弱めた 7・パンだけ 5、平均 7.4%） | 3 枚（ズームを弱めた 2・パンだけ 1） |
| つなぎ方 | crossfade 31 / fade_black 3 | crossfade 22 / cut 12 / slide 6 / fade_black 1 | crossfade 24 / cut 5 / slide 5 / fade_black 3 |
| トゥルーピーク v1 → v2 | +0.6 → −1.0 dBTP | +0.8 → −1.1 dBTP | +1.8 → −2.4 dBTP |
| 統合ラウドネス v1 → v2 | −13.4 → −13.6 LUFS | −9.8 → −10.5 LUFS | −9.8 → −12.9 LUFS |
| 曲の境目 v1 実測 → v2 見込み / 実測 | +0.5 → +1.0 / +0.8 dB | +0.2 → +0.3 / −0.1 dB | +3.8 → +1.9 / +2.1 dB（2 曲目 gain −2.0） |
| 長さ・フレーム数 | v1 と同じ | v1 と同じ | v1 と同じ |

- 曲の境目の見込みと実測は 0.4 dB 以内で合った。絶対値は limiter の補正ぶん実測が低い（見込みはリミッター前）
- 人物・旅行の作品では、リミッターのあとでも AAC で +0.5 dBTP に上振れし、補正 −1.7 dB で −2.4 dBTP になった
  （補正が必要量より 1.4 dB 多く効いた。AAC の上振れは音量に比例しない）。上限は守れているが、全体は v1 より 3.1 LU 静か
- 次の候補（v2.1）: 補正を上限に近づける（二分探索で 1〜2 回）。どちらが自然かは人が見て決める
