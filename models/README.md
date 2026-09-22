# models フォルダー

「被写体追従（人物・犬）」で使う検出モデルをここへ置きます。

このフォルダーにモデルが無くても、アプリは起動しますし、
**従来カメラワークでの動画作成はそのまま行えます。**
被写体追従だけが選べなくなり、GUIにその旨が表示されます。

アプリが実行時にモデルをダウンロードすることはありません。
写真がPCの外へ出ることもありません。

## 必要なファイル

| ファイル | 用途 | サイズ | SHA-256 |
| --- | --- | --- | --- |
| `face_detection_yunet_2023mar.onnx` | 人物の顔の位置 | 232,589 bytes | `8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4` |
| `object_detection_yolox_2022nov.onnx` | 犬の位置 | 35,858,002 bytes | `c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063` |

## 入手元

どちらも [OpenCV Zoo](https://github.com/opencv/opencv_zoo) の配布物です。
Git LFS で管理されているため、`media.githubusercontent.com` 側のURLから取得します。

```bash
curl -L -o models/face_detection_yunet_2023mar.onnx "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"
```

```bash
curl -L -o models/object_detection_yolox_2022nov.onnx "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/object_detection_yolox/object_detection_yolox_2022nov.onnx"
```

取得後、上の表のサイズとSHA-256が一致することを確認してください。

## ライセンス

| モデル | ライセンス | 権利者 |
| --- | --- | --- |
| YuNet (face_detection_yunet) | MIT License | Shiqi Yu |
| YOLOX (object_detection_yolox) | Apache License 2.0 | Megvii Inc. |

詳細は、リポジトリ直下の [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) を参照してください。

## なぜモデルをGitに入れていないか

`.onnx` は `.gitignore` で除外しています。YOLOX が約34MBあり、
Gitの履歴へ入れると以後ずっとクローンに付いてくるためです。
配布時は GitHub Releases へ同梱するか、上のコマンドで取得してください。
