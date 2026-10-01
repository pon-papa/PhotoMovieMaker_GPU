# -*- coding: utf-8 -*-
"""PhotoMovieMaker GPU のバージョン（正本はここだけ）。

画面のタイトル・設定ファイル・診断・CLI / Core API・配布 ZIP の名前は、すべてこの値を使います。
標準ライブラリだけで読めるようにしてあります（診断は、パッケージが入っていなくても動く必要があるため）。
"""

__version__ = "0.2.0"

# 動作を確かめる対象の Python（setup.bat が探す版と同じ）
SUPPORTED_PYTHON = ((3, 11), (3, 13))
