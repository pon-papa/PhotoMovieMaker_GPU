# models フォルダー

「被写体追従（人物・犬）」で使う検出モデルをここへ置きます。

このフォルダーにモデルが無くても、アプリは起動しますし、
**従来カメラワークでの動画作成はそのまま行えます。**
被写体追従だけが選べなくなり、GUIにその旨が表示されます。

アプリが実行時にモデルをダウンロードすることはありません。
写真がPCの外へ出ることもありません。

## このフォルダーにモデルはありますか？

- **配布ZIP（`PhotoMovieMaker_GPU_v0.2.0_Windows.zip` など）を展開した場合**
  → すでに同梱済みです。何もしなくて構いません。
- **GitHubからクローン／ZIPダウンロードした場合**
  → `.onnx` はGit管理外なので入っていません。下の手順で取得してください。

## 必要なファイル

| ファイル | 用途 | サイズ | SHA-256 |
| --- | --- | --- | --- |
| `face_detection_yunet_2023mar.onnx` | 人物の顔の位置 | 232,589 bytes | `8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4` |
| `object_detection_yolox_2022nov.onnx` | 犬・人物の位置 | 35,858,002 bytes | `c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063` |
| `dinov2_small_embedding.onnx` | 似た場面の判定（おすすめ解析） | 88,437,819 bytes | `a89a990a98e8022021fd294c94078c8395fc7ae3d59dbc30e80f214ff1b842c5` |

## 入手元

YuNet と YOLOX は [OpenCV Zoo](https://github.com/opencv/opencv_zoo) の配布物です。
Git LFS で管理されているため、`media.githubusercontent.com` 側のURLから取得します。

```bash
curl -L -o models/face_detection_yunet_2023mar.onnx "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"
```

```bash
curl -L -o models/object_detection_yolox_2022nov.onnx "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/object_detection_yolox/object_detection_yolox_2022nov.onnx"
```

取得後、上の表のサイズとSHA-256が一致することを確認してください。

## dinov2_small_embedding.onnx について

これは `facebook/dinov2-small`（Apache-2.0）から書き出したONNXです。
公式配布のONNXが無いため、こちらで変換しています。

- 元の重み: https://huggingface.co/facebook/dinov2-small
- revision : `ed25f3a31f01632728cabb09d1542f84ab7b0056`
- 出力     : CLSトークン 384次元
- opset    : 17 / 入力 `pixel_values` (batch, 3, 224, 224)
- 変換後に元のPyTorchモデルと突き合わせ、
  cosine similarity 0.99999988 / 要素ごとの差の最大 2.2e-05 で一致を確認済み

このモデルが無くても、おすすめ解析は従来の指標だけで動きます。

## ライセンス

| モデル | ライセンス | 権利者 |
| --- | --- | --- |
| YuNet (face_detection_yunet) | MIT License | Shiqi Yu |
| YOLOX (object_detection_yolox) | Apache License 2.0 | Megvii Inc. |
| DINOv2 (dinov2_small_embedding) | Apache License 2.0 | Meta Platforms, Inc. |

詳細は、リポジトリ直下の [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) を参照してください。

## なぜモデルをGitに入れていないか

`.onnx` は `.gitignore` で除外しています。YOLOX が約34MBあり、
Gitの履歴へ入れると以後ずっとクローンに付いてくるためです。

そのかわり、**GitHub Releases の配布ZIPにはモデルを同梱**しています。
ZIPを展開すればモデルを別途取得することなく被写体追従とおすすめ解析（画像embedding）を使えます。
モデルがそろっているか・中身が正しいかは、`diagnose.bat` で確かめられます。
