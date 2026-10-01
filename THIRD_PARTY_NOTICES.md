# サードパーティのソフトウェアとモデル

PhotoMovieMaker GPU が利用している第三者の成果物と、そのライセンスです。

---

## 同梱しているモデル

配布 ZIP の `models/` に 3 つのモデルを同梱しています（被写体追従用の 2 つと、おすすめ解析用の 1 つ）。
YuNet と YOLOX は [OpenCV Zoo](https://github.com/opencv/opencv_zoo) から入手したものです。
OpenCV Zoo のリポジトリ自体は Apache License 2.0 ですが、モデルごとにライセンスが異なります。

### YuNet — 人物の顔の位置検出

| 項目 | 内容 |
| --- | --- |
| ファイル | `models/face_detection_yunet_2023mar.onnx` |
| バージョン | 2023mar |
| 入手元 | https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet |
| ライセンス | **MIT License** |
| 権利者 | Copyright (c) 2020 Shiqi Yu \<shiqi.yu@gmail.com\> |
| 再配布 | 可。著作権表示とライセンス本文の同梱が必要 |
| ライセンス本文 | [licenses/YuNet-MIT.txt](licenses/YuNet-MIT.txt) |

### YOLOX — 犬・人物の位置検出（COCO の dog / person クラス）

| 項目 | 内容 |
| --- | --- |
| ファイル | `models/object_detection_yolox_2022nov.onnx` |
| バージョン | 2022nov |
| 入手元 | https://github.com/opencv/opencv_zoo/tree/main/models/object_detection_yolox |
| ライセンス | **Apache License 2.0** |
| 権利者 | Copyright (c) 2021-2022 Megvii Inc. |
| 再配布 | 可。ライセンス本文の同梱が必要（第4条a）。改変した場合はその旨の告知も必要（第4条b） |
| ライセンス本文 | [licenses/YOLOX-Apache-2.0.txt](licenses/YOLOX-Apache-2.0.txt) |

前処理・後処理の実装は、OpenCV Zoo の公式サンプル
（`models/object_detection_yolox/yolox.py`, Apache License 2.0）に合わせています。

### DINOv2 — 似た場面の判定（おすすめ解析）

| 項目 | 内容 |
| --- | --- |
| ファイル | `models/dinov2_small_embedding.onnx` |
| 元の重み | facebook/dinov2-small (ViT-S/14) |
| revision | `ed25f3a31f01632728cabb09d1542f84ab7b0056` |
| 入手元 | https://huggingface.co/facebook/dinov2-small |
| コード | https://github.com/facebookresearch/dinov2 |
| ライセンス | **Apache License 2.0**（コード・重みとも） |
| 権利者 | Copyright (c) Meta Platforms, Inc. and affiliates |
| 再配布 | 可。ライセンス本文の同梱が必要（第4条a）。改変した場合はその旨の告知も必要（第4条b） |
| ライセンス本文 | [licenses/DINOv2-Apache-2.0.txt](licenses/DINOv2-Apache-2.0.txt) |

公式配布のONNXが無いため、本プロジェクトで
`facebook/dinov2-small` から ONNX へ書き出しています。
重みそのものは変更していませんが、**CLSトークンだけを返す形へ変換している**ため、
Apache-2.0 第4条(b)にあたる「変更した旨の告知」としてここに記載します。
変換の詳細（revision・opset・前処理・検証結果）は
[models/README.md](models/README.md) にあります。

なお、DINOv2 のうち Cell-DINO / XRay-DINO などの派生モデルは
FAIR Noncommercial Research License ですが、本プロジェクトが使うのは
標準の ViT-S/14（Apache-2.0）だけです。

---

## 公開・再配布するときの注意

- いずれのライセンスも、MIT / Apache-2.0 という扱いやすい条件です。
  アプリ全体を特定のライセンスへ揃えることを強制するものではありません。

- **モデルを同梱して配布する場合は、このファイルと `licenses/` フォルダーを
  必ず一緒に配布してください。** 条文上の根拠は次のとおりです。

  - MIT は「上記の著作権表示および本許諾表示を、ソフトウェアのすべての複製
    または重要な部分に記載するものとする」と定めています。要約ではなく
    **本文そのもの**が必要なため、`licenses/YuNet-MIT.txt` を同梱しています。
  - Apache-2.0 第4条(a)は「頒布先に本ライセンスの複製を渡さなければならない」
    と定めています。そのため `licenses/YOLOX-Apache-2.0.txt` と `licenses/DINOv2-Apache-2.0.txt` を同梱しています。

- Apache-2.0 第4条(b)は改変時の告知を求めます。YuNet と YOLOX は**一切改変していません**
  （OpenCV Zoo が配布するバイナリをそのまま同梱しています）。DINOv2 は ONNX へ書き出しているため、
  その旨を上の DINOv2 の項に記載しています。

- Apache-2.0 第4条(d)の NOTICE 同梱義務は、配布元に NOTICE ファイルが
  存在する場合にのみ適用されます。OpenCV Zoo の YOLOX 配布ディレクトリと、
  DINOv2 のリポジトリ（facebookresearch/dinov2）の直下に NOTICE ファイルが**無い**ことを
  確認済み（2026-10-01）のため、この義務は発生しません。

- AGPL のモデル・ライブラリ（Ultralytics YOLOv5 / YOLOv8 など）は、
  配布条件がアプリ全体へ波及しうるため採用していません。

---

## Python パッケージ

`requirements.txt` に記載のものを利用しています。**配布 ZIP には同梱していません。**
`setup.bat` を実行したときに、利用者の PC が PyPI から取得します（アプリのフォルダーの中の `.venv` に入ります）。
それぞれのライセンスは、取得したパッケージに付属するものに従います。

| パッケージ | ライセンス |
| --- | --- |
| Pillow | MIT-CMU |
| NumPy | BSD 3-Clause（同梱の一部に 0BSD / MIT / Zlib / CC0-1.0） |
| opencv-python | Apache License 2.0 |
| onnxruntime | MIT License |
| imageio-ffmpeg | BSD 2-Clause |

## FFmpeg

動画のエンコードと音声の合成に FFmpeg を使用します。
本プロジェクトは FFmpeg 本体を同梱せず（配布 ZIP にも入っていません）、
PATH 上の FFmpeg か、`imageio-ffmpeg` のパッケージに入っている FFmpeg を呼び出します。

FFmpeg のライセンス（LGPL-2.1 以降、ビルド構成によっては GPL）は、
利用者が導入した FFmpeg ビルドの条件に従います。
