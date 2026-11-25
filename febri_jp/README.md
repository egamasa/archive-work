# Febri アーカイブ

[Febri](https://febri.jp/) のページを HTML + 画像でローカルに保存するアーカイブ作業用ディレクトリ。

## ディレクトリ構成

```text
febri_jp/
├── list/      # アーカイブ対象URLのCSVリスト
├── scripts/   # ページ保存スクリプト
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
