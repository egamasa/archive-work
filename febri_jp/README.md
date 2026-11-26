# Febri アーカイブ

[Febri](https://febri.jp/) のページを HTML + 画像でローカルに保存するアーカイブ作業用ディレクトリ。

## ディレクトリ構成

```text
febri_jp/
├── list/      # アーカイブ対象URLのCSVリスト
├── scripts/   # ページ保存・Markdown 変換スクリプト
├── output/    # Markdown 変換結果（index.md + 本文の画像） ※Git管理外
└── source/    # 保存先（HTML + 画像） ※Git管理外
    ├── topics/
    │   └── <slug>/
    │       ├── index.html
    │       ├── <画像ファイル>
    │       └── 2/       # 2ページ目以降
    │           └── index.html
    ├── febri_talk/
    ├── febritalk/list/
    ├── series/
    │   ├── butai/
    │   ├── gppedia/
    │   └── mangaka/
    └── tv/
```

`source/` は URL のパスをそのままディレクトリ階層にしている。
`.markdownlint.jsonc` は Markdown 化した記事に適用する markdownlint の設定。

---

## list/ — アーカイブ対象URL一覧

| ファイル | 対象コンテンツ |
| --- | --- |
| `febri_talk.csv` | Febri TALK 記事（`/febri_talk/`） |
| `febritalk-list.csv` | Febri TALK カタリベリスト（`/febritalk/list/`） |
| `series-butai.csv` | 舞台探訪シリーズ（`/series/butai/`） |
| `series-gppedia.csv` | ガールズ＆パンツァー 最終章 エンサイクロペディア（`/series/gppedia/`） |
| `series-mangaka.csv` | マンガ家インタビューシリーズ（`/series/mangaka/`） |
| `topics.csv` | トピックス記事（`/topics/`） |
| `tv.csv` | TV コンテンツ（`/tv/`） |

### CSVフォーマット

```text
source_page,published_date,datetime,title,url,pages
```

- `pages` は記事のページ数。2 以上の記事は `archive.py` が 2 ページ目以降も保存する
- `tv.csv` のみ末尾に `video_url` カラムが追加されている
- `series-gppedia.csv` と `febritalk-list.csv` は `published_date` と `datetime` が空になっている
- 行は `datetime` の降順に並んでいる

---

## scripts/ — スクリプト群

### archive.py

`list/*.csv` に記載された URL のページを Playwright で取得し、`source/` 配下に HTML として保存する。

```bash
python3 scripts/archive.py                                    # list/ 配下の全 CSV
python3 scripts/archive.py --list topics.csv series-butai.csv # 特定の CSV のみ
python3 scripts/archive.py --dry-run                          # 保存せずに対象URLと保存先を表示
python3 scripts/archive.py --force                            # 保存済みページも再取得
python3 scripts/archive.py --concurrency 5                    # 同時取得ページ数（デフォルト: 3）
python3 scripts/archive.py --verbose                          # DEBUG ログを出力
```

- `--list` には `list/` からの相対ファイル名を指定する
- 保存パスは `https://febri.jp/topics/foo/` → `source/topics/foo/index.html`、2 ページ目は `source/topics/foo/2/index.html`
- febri.jp ドメインの画像を HTML と同じディレクトリに保存し、`img` の `src` / `srcset` をローカルのファイル名に書き換える
- `<link rel="stylesheet">` の CSS は取得して `<style>` に埋め込む（`@import` も展開する）
- `pages` が 2 以上の記事は、1 ページ目のページ送り（`div.pagination a.post-page-numbers`）のリンクをたどって 2 ページ目以降を保存する
- `index.html` がすでにあるページはスキップする
- febri.jp 以外のホストの URL は CSV にあっても取得しない
- 実行ログは `scripts/archive_YYYY-MM-DD.log`（JST の日付）に追記される

#### Internet Archive フォールバック

実サイトから取得できない場合（febri.jp 以外へのリダイレクト、HTTP エラー、通信エラー）は Internet Archive（Wayback Machine）から取得する。

- HTML は CDX API で `WAYBACK_TO`（`20261125070000` UTC）以前の最新キャプチャを探し、無加工（`id_`）で取得する
- CSS と画像は HTML と同じタイムスタンプで取得する。febri.jp 以外の CSS（`@import` を含む）は取得しない
- CSS や画像が `WAYBACK_TO` より後のキャプチャに解決された場合は、それ以前で最も近いキャプチャを取り直す
- Internet Archive への同時リクエストは 2 件までに制限している

### structurize.py

`source/` に保存した HTML から記事の本文を抽出し、`output/` 配下に Markdown として書き出す。
`source/` は読み取るだけで変更しない。

```bash
python3 scripts/structurize.py                                         # list/ 配下の全 CSV
python3 scripts/structurize.py --list topics.csv febri_talk.csv        # 特定の CSV のみ
python3 scripts/structurize.py --url https://febri.jp/topics/foo/      # 特定の記事のみ
python3 scripts/structurize.py --output /path/to/out --workers 4       # 出力先・並列数を変更
```

- 出力パスは `source/topics/foo/index.html` → `output/topics/foo/index.md`
- 2 ページ以上ある記事は、全ページを 1 つの `index.md` にまとめる。ページの境目には `<!-- page N -->` を入れる
- 本文で使う画像は `source/` から同じ相対位置にコピーする（N ページ目の画像は `output/<記事>/N/`）。`srcset` に大きい画像があればそちらを使う
- 出力する範囲は、タイトル、アイキャッチ、リード文、クレジット、本文の全ブロック、作品情報・書籍情報、カタリベプロフィール
- カテゴリ、公開日時（CSV の `datetime`）、タグ、元 URL、ページ数、動画 URL（`tv.csv`）は front matter に入れる
- ナビ、パンくず、SNS ボタン、ページ送り、関連記事、ランキング、広告は出力しない
- エンサイクロペディア（`/series/gppedia/`）とカタリベリスト（`/febritalk/list/`）は各項目を見出しと本文にする
- 変換の規則は次のとおり
  - インタビューの話者名（色付き文字）は太字にする
  - 改行（`<br>`）は `<br>` を使わずに段落を分ける。作品情報・書籍情報の中だけは、段落を分けずに行末の `\` で改行する。表のセルとリンク文字列の中の改行は空白にする
  - 著作権表示（`ul.list-ttl`）はリストにせず、行末の `\` で改行した 1 段落にする
  - 人物紹介など見出し語のある項目（`dl` の `dt` と `dd`）は、太字の見出し語と説明の段落にする
  - 説明だけの注釈（※1 など）、画像のキャプション、プルクオートは引用ブロックにする
  - YouTube、Spotify、Google マップ、ツイートの埋め込みと動画は、元の URL へのリンクにする
  - 記事末尾のエンドマーク画像（`endmark.png`）はコピーせず、水平線（`---`）にする。エンドマークの直後に画像が続く場合は、その画像（キャプションを含む）の後ろに水平線を置く
  - 変換対象の記事（全 CSV の記事。2 ページ目以降の URL を含む）を指す febri.jp のリンクは、変換後の `index.md` への相対リンクにする。それ以外の febri.jp のリンク（変換対象外のページ、動画ファイルなど）は元の URL のまま残し、件数をログに出す（`--verbose` で URL の一覧）
- 変換のたびに、出力すべき領域のテキストと画像がすべて Markdown に入っているかを記事ごとに検査する。欠落があれば WARNING を出し、終了コード 1 で終わる
- 画像はサイズと更新時刻が同じならコピーを省き、`index.md` は内容が変わったときだけ書き換える
- 実行ログは `scripts/structurize_YYYY-MM-DD.log`（JST の日付）に追記される

---

## 使いかた

### 事前準備

依存パッケージはリポジトリルートの `pyproject.toml` に定義している（`beautifulsoup4`、`httpx`、`playwright`）。
Playwright のブラウザも別途インストールする。

```bash
python3 -m playwright install chromium
```

### 新規記事を追加して保存する

```bash
# CSV に行を追記してから実行（保存済みページはスキップされる）
python3 scripts/archive.py --list topics.csv
```
