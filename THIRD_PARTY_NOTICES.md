# サードパーティのソフトウェアとモデル

PhotoMovieMaker GPU が利用している第三者の成果物と、そのライセンスです。

---

## 検出モデル（被写体追従で使用）

どちらも [OpenCV Zoo](https://github.com/opencv/opencv_zoo) から入手したものです。
リポジトリ自体は Apache License 2.0 ですが、モデルごとにライセンスが異なります。

### YuNet — 人物の顔の位置検出

| 項目 | 内容 |
| --- | --- |
| ファイル | `models/face_detection_yunet_2023mar.onnx` |
| バージョン | 2023mar |
| 入手元 | https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet |
| ライセンス | **MIT License** |
| 権利者 | Copyright (c) 2020 Shiqi Yu \<shiqi.yu@gmail.com\> |
| 再配布 | 可。著作権表示とライセンス文を同梱すること。 |

### YOLOX — 犬の位置検出（COCOの dog クラス）

| 項目 | 内容 |
| --- | --- |
| ファイル | `models/object_detection_yolox_2022nov.onnx` |
| バージョン | 2022nov |
| 入手元 | https://github.com/opencv/opencv_zoo/tree/main/models/object_detection_yolox |
| ライセンス | **Apache License 2.0** |
| 権利者 | Copyright (c) 2021-2022 Megvii Inc. |
| 再配布 | 可。ライセンス文の同梱と、変更した場合はその旨の告知が必要。 |

前処理・後処理の実装は、OpenCV Zoo の公式サンプル
（`models/object_detection_yolox/yolox.py`, Apache License 2.0）に合わせています。

---

## 公開・再配布するときの注意

- どちらのライセンスも、MIT / Apache-2.0 という扱いやすい条件です。
  アプリ全体を特定のライセンスへ揃えることを強制するものではありません。
- モデルを同梱して配布する場合は、**このファイルを一緒に配布してください。**
  それが MIT と Apache-2.0 の両方が求める条件（著作権表示とライセンス文の同梱）を満たします。
- Apache-2.0 はモデルを改変して配布する場合、変更した旨の告知を求めます。
  本プロジェクトはモデルファイルを**一切改変していません**。
- AGPL のモデル・ライブラリ（Ultralytics YOLOv5 / YOLOv8 など）は、
  配布条件がアプリ全体へ波及しうるため採用していません。

---

## Python パッケージ

`requirements.txt` に記載のものを利用しています。

| パッケージ | ライセンス |
| --- | --- |
| Pillow | MIT-CMU |
| NumPy | BSD 3-Clause |
| opencv-python | Apache License 2.0 |
| imageio-ffmpeg | BSD 2-Clause |

## FFmpeg

動画のエンコードと音声の合成に FFmpeg を使用します。
本プロジェクトは FFmpeg 本体を同梱せず、
PATH 上の FFmpeg か、`imageio-ffmpeg` が持つ FFmpeg を呼び出します。

FFmpeg のライセンス（LGPL-2.1 以降、ビルド構成によっては GPL）は、
利用者が導入した FFmpeg ビルドの条件に従います。
