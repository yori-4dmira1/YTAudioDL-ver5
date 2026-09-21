# YouTube Audio Downloader

YouTube から音声のみを取り出し、**m4a形式・サムネイル画像埋め込み付き**で
保存するデスクトップアプリ。利用者は完成した exe を1回ダウンロードするだけで、
Python のインストールなど一切の準備なしにそのまま使える。

> 権利上ダウンロードが許諾されている動画（自分の投稿、CC ライセンス、
> パブリックドメイン等）にのみ使用してください。

## 利用者側の流れ

1. リポジトリの右サイドバー（または `Releases` タブ）にある **「Releases」** を開く
2. 一番上にある最新リリースの **`YTAudioDL.exe`** をクリックしてダウンロードする
   （他に `Source code (zip)` のようなファイルも自動的に並ぶが、それらは不要。
   ダウンロードすべきなのは `YTAudioDL.exe` の1つだけ）
3. 起動する（初回のみ数秒のセットアップ画面）
4. URL を貼って「ダウンロード」を押す

`https://github.com/<ユーザー名>/<リポジトリ名>/releases/latest` を直接ブックマークして
おくと、毎回同じリンクから常に最新版をダウンロードできる（タグ名を `latest` に
固定しているため）。身内に配布する際はこのリンクを渡すとよい。
プライベートリポジトリの場合、相手をリポジトリの共同作業者として招待しておく必要がある。

Python、ffmpeg、PATH設定、管理者権限、ネットワーク接続（初回以降）は
いずれも不要。m4aはWindows標準のメディアプレイヤーやエクスプローラーの
サムネイル表示にそのまま対応しているため、追加コーデックの導入も不要。

## exe を作る側（開発者）の流れ ― ローカルにPythonは不要

このリポジトリには GitHub Actions の設定 (`.github/workflows/build.yml`) が
含まれており、**自分のPCにPythonもWindowsも用意せずに** exe を作れる。

1. このフォルダ一式を GitHub リポジトリに push する
   （GitHubアカウントが無ければ無料で作成）
2. リポジトリの「Actions」タブを開き、
   「Build Windows EXE」ワークフローを実行する
   （`workflow_dispatch` なので手動実行ボタンが出る）
3. 数分待つと実行結果に `YTAudioDL-windows` という成果物ができるので、
   それをダウンロードして展開すると `YTAudioDL.exe` が入っている

ビルド作業自体はGitHubの提供するWindows仮想マシン上で行われるため、
Pythonのインストールも、Windows環境の用意も、こちら側では一切発生しない。

手元にWindows機とPythonがあり、その場でビルドしたい場合は従来通り
下記の手順も使える。


## 構成

| ファイル | 役割 |
|---|---|
| `app.py` | GUI 本体。yt-dlp を子プロセスとして起動し進捗を解析する |
| `runtime.py` | 依存バイナリの展開・更新・復旧 |
| `build.py` | vendor 取得と PyInstaller ビルドの一括実行 |
| `vendor/` | ビルド時に生成（同梱される yt-dlp / ffmpeg / ffprobe） |

`runtime.py` は「同梱物があればコピー、無ければネットから取得」の二段構え。
配布版は前者で完結し、開発中やバイナリ破損時は後者が働く。

## 開発時の実行

```bash
python app.py
```

`vendor/` が無ければ初回起動時にネットから取得する。
追加の pip パッケージは不要（標準ライブラリのみ）。

## 配布用ビルド

```bash
pip install pyinstaller
python build.py
```

これだけで `dist/YTAudioDL.exe` ができる。内部で以下を行っている。

1. `vendor/` に yt-dlp と ffmpeg/ffprobe を取得
2. PyInstaller に `--add-data vendor` で同梱させ単一ファイル化

サブコマンド:

```bash
python build.py --fetch    # vendor の取得のみ
python build.py --onedir   # 起動が速い onedir 形式
python build.py --clean    # 生成物を削除
```

### サイズについて

汎用 ffmpeg ビルドは全コーデックを含むため、成果物は **100〜200MB** 程度になる。
気になる場合の選択肢は 2 つ。

- **onedir にする** — 起動のたびの自己展開が無くなり体感が大幅に改善する。
  インストーラで包むならこちらが有利。
- **ffmpeg を自前ビルドする** — 音声用途に必要なもの
  （`libmp3lame` / `aac` / `libopus` / `flac`）だけを有効にして
  `--disable-everything` 系で削ると 15MB 前後まで落ちる。手間はかかる。

### インストーラ化（任意）

`--onedir` でビルドした `dist/YTAudioDL/` を Inno Setup（Windows）や
`create-dmg`（macOS）で包むと、利用者から見た操作は
「ダウンロード → インストーラ実行 → 起動」のままで、
起動速度だけが改善する。ダウンロード回数は 1 回で変わらない。

### 署名

未署名の exe は Windows SmartScreen に、未公証の .app は macOS Gatekeeper に
ブロックされる。技術的に環境設定が不要でも、起動時に警告が出れば
体験は損なわれる。実配布するなら Windows は OV/EV 証明書、
macOS は Developer ID + notarization を通すこと。

## ライセンス上の注意

ffmpeg を同梱して再配布する構成に変わったため、**再配布者としての義務が発生する**。

- BtbN の GPL ビルドを同梱する場合、成果物全体が GPL の条件下に置かれ、
  ソースコードの提供義務等が生じうる。
- 回避したい場合は **LGPL ビルド**（GPL 専用コーデックを含まないもの）を選び、
  ffmpeg を動的リンクではなく別プロセスとして呼ぶ本構成の性質を踏まえて
  条件を確認すること。
- いずれの場合も、同梱したバイナリのライセンス文書を成果物に含めること。

社内配布や個人利用のみであれば「再配布」に当たらないため、この問題は生じない。
公開配布する前に必ず確認すること。

## 設計上のポイント

**なぜ yt-dlp を CLI として呼ぶか**
ライブラリとして import すると PyInstaller の hidden import 解決が煩雑になる。
単体バイナリなら `yt-dlp -U` による自己更新が使え、YouTube 側の仕様変更に
再配布なしで追従できる。同梱版も起動時に裏で更新確認を行う（失敗しても無害）。

**進捗の取得**
`--progress-template` で `PROG|...` 形式の行を吐かせ、`--newline --no-colors`
と併用して 1 行ずつパースする。ANSI エスケープは念のため除去。

**スレッド境界**
tkinter はメインスレッド以外からウィジェットを触ると不安定になるため、
ワーカーからは `queue` に積み、`after()` でポーリングして反映する。

## 復旧

バイナリが壊れた場合は GUI の「コンポーネント再取得」を押す。
`bin/` を削除し、同梱物から再展開する（同梱が無ければ再ダウンロード）。
