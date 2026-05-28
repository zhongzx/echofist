# EchoFist (エコーフィスト)

アマチュア無線（HAM）愛好家のためのクロスプラットフォームAI支援連続波（CW）通信ソフトウェア。

## 🎯 プロジェクト理念

**ギーク精神、テキストが王様** - 派手なグラフィカルインターフェースを捨て、ギーク文化の本質に戻り、機能性とパフォーマンスに焦点を当てる。

## ✨ コア機能

- **高ロバスト性ブラインド復調**: 高ノイズおよび信号フェージング環境での正確な「ドット・ダッシュ」検出率の実現
- **共有無線機アクセス**: KiwiSDRネットワークを通じて世界中の700以上のリモート受信機にアクセス
- **フィスト特徴抽出**: 相手の送信タイミング特性の捕捉と記録（フィスト署名）
- **自動化QSOプロセス**: 手動コピーの負担を軽減し、標準化されたルールによる通信完了
- **擬人化再生**: 生成されたCW信号に特定の個性/手の揺らぎを付与

## 🚀 クイックスタート

### 依存関係のインストール
```bash
# 仮想環境の作成
python -m venv venv

# 仮想環境の有効化
# Linux/macOS
source venv/bin/activate
# Windows
venv\Scripts\activate

# 依存関係のインストール
pip install -r requirements.txt
```

### アプリケーションの起動
```bash
# 基本リスニングモード
python -m echofist listen --server kiwi.remotehams.com:8073

# 自動通信モード
python -m echofist auto --freq 7.025 --wpm 20

# ヘルプの表示
python -m echofist --help
```

## 📁 プロジェクト構造

```
echofist/
├── echofist/              # メインパッケージディレクトリ
│   ├── core/             # コアモジュール
│   ├── ai/               # AI/MLモジュール
│   ├── ui/               # テキストインターフェース
│   ├── data/             # データ管理
│   └── utils/            # ユーティリティ関数
├── tests/                # テストディレクトリ
├── scripts/              # ツールスクリプト
├── data/                 # データファイル
├── docs/                 # ドキュメント
└── examples/             # サンプルコード
```

## 🔧 技術スタック

- **音声処理**: numpy, scipy, librosa, sounddevice
- **テキストインターフェース**: rich, click, prompt-toolkit
- **ネットワーク通信**: websockets, aiohttp, requests
- **データストレージ**: sqlalchemy, sqlite3, pandas
- **機械学習**: torch, scikit-learn, onnxruntime

## 📊 操作モード

| モード | 説明 | 使用ケース |
|--------|------|------------|
| **リスニングモード** | リアルタイムデコード表示、自動応答なし | 日常的な周波数スキャン、学習 |
| **セミオートモード** | 自動デコード、送信手動確認 | 通常の通信 |
| **フルオートモード** | 完全自動化QSOプロセス | コンテスト、無人運用 |

## 🤝 貢献ガイドライン

1. プロジェクトをフォーク
2. 機能ブランチを作成 (`git checkout -b feature/AmazingFeature`)
3. 変更をコミット (`git commit -m 'Add some AmazingFeature'`)
4. ブランチにプッシュ (`git push origin feature/AmazingFeature`)
5. プルリクエストを開く

## 📄 ライセンス

MITライセンス - 詳細は[LICENSE](LICENSE)ファイルを参照

## 🙏 謝辞

- グローバル受信機ネットワークを提供するKiwiSDRコミュニティ
- すべてのオープンソース音声処理ライブラリの貢献者
- アマチュア無線コミュニティの継続的な革新精神
