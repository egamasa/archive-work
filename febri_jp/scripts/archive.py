"""
Febri ページ保存スクリプト

febri_jp/list/*.csv に記載された URL のページを Playwright で取得し、
febri_jp/source/ 配下に HTML ファイルとして保存する。
ページ内の febri.jp ドメインの画像も同じディレクトリにダウンロードし、
HTML 内の src をローカルパスに書き換える。

保存パス規則:
  https://febri.jp/topics/foo/ → source/topics/foo/index.html
  画像: source/topics/foo/<ファイル名>

使い方:
  # すべての CSV を処理
  python3 archive.py

  # 特定の CSV のみ処理
  python3 archive.py --list topics.csv febritalk-list.csv

  # スキップ済みファイルを強制再取得
  python3 archive.py --force

  # 同時接続数を変更（デフォルト: 3）
  python3 archive.py --concurrency 5

  # ドライラン（保存せずに対象 URL 一覧を表示）
  python3 archive.py --dry-run
"""

import argparse
import asyncio
import csv
import datetime
import hashlib
import logging
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

# ---- パス定義 ----------------------------------------------------------------
SCRIPT_DIR = Path(__file__).parent
LIST_DIR = SCRIPT_DIR.parent / "list"
SOURCE_DIR = SCRIPT_DIR.parent / "source"

# ---- 定数 -------------------------------------------------------------------
FEBRI_HOST = "febri.jp"
REQUEST_DELAY = 2.0  # ページ取得後の待機秒数
PAGE_TIMEOUT = 90_000  # ページ読み込みタイムアウト（ms）
IMAGE_TIMEOUT = 30.0  # 画像ダウンロードタイムアウト（秒）
RETRY_COUNT = 3  # ページ取得の最大リトライ回数
RETRY_DELAY = 5.0  # リトライ前の待機秒数（attempt ごとに倍増: 5s, 10s）
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

logger = logging.getLogger(__name__)


# ---- ユーティリティ ----------------------------------------------------------
def collect_urls(csv_files: list[Path]) -> list[tuple[str, str]]:
    """CSV ファイルから (csv_name, url) のリストを返す。

    febri.jp ホスト以外の URL は SSRF 防止のためスキップする。
    """
    entries: list[tuple[str, str]] = []
    for csv_path in csv_files:
        with csv_path.open(encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                url = (row.get("url") or "").strip()
                if not url or not url.startswith("http"):
                    continue
                host = urlparse(url).netloc.lower()
                if host != FEBRI_HOST and not host.endswith(f".{FEBRI_HOST}"):
                    logger.warning("SKIP (non-febri host) %s", url)
                    continue
                entries.append((csv_path.name, url))
    return entries


def url_to_save_path(url: str, base_dir: Path) -> Path:
    """
    URL を保存先パスに変換する。

    例:
      https://febri.jp/topics/foo/     → base_dir/topics/foo/index.html
      https://febri.jp/febritalk/list/ → base_dir/febritalk/list/index.html
    """
    if not base_dir or not base_dir.is_absolute():
        raise ValueError(f"base_dir は絶対パスで指定してください: {base_dir}")
    if not url or not url.startswith("http"):
        raise ValueError(f"url は http(s) で始まる必要があります: {url}")
    parsed = urlparse(url)
    path_parts = [p for p in parsed.path.split("/") if p and p not in (".", "..")]
    if not path_parts:
        path_parts = ["index"]
    return base_dir.joinpath(*path_parts) / "index.html"


def is_febri_image(url: str) -> bool:
    """febri.jp ドメインの画像 URL かどうか判定する。"""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    return host == FEBRI_HOST or host.endswith(f".{FEBRI_HOST}")


def image_filename(img_url: str) -> str:
    """画像 URL からファイル名を取得する（クエリ・フラグメントを除去）。"""
    if not img_url or not img_url.startswith("http"):
        raise ValueError(f"img_url は http(s) で始まる必要があります: {img_url}")
    parsed = urlparse(img_url)
    name = Path(parsed.path).name
    if not name or "." not in name:
        name = hashlib.sha256(img_url.encode()).hexdigest()[:12] + ".jpg"
    return name


# ---- 画像ダウンロード --------------------------------------------------------
async def download_images(
    html: str,
    page_url: str,
    save_dir: Path,
    http_client: httpx.AsyncClient,
) -> str:
    """
    HTML 内の febri.jp 画像をダウンロードし、src をローカルパスに書き換えた
    HTML 文字列を返す。

    soup ベースで一貫して書き換えることで、JSON-LD や data-* 属性への
    意図しない文字列置換を防ぐ。
    """
    soup = BeautifulSoup(html, "html.parser")
    img_tags = soup.find_all("img")

    # src / srcset 両方を収集
    url_to_local: dict[str, str] = {}

    def _collect_srcset_urls(srcset: str, base_url: str) -> None:
        for part in srcset.split(","):
            candidate = part.strip().split()[0]
            if candidate:
                abs_url = urljoin(base_url, candidate)
                if is_febri_image(abs_url):
                    url_to_local[abs_url] = image_filename(abs_url)

    for tag in img_tags:
        src = tag.get("src", "")
        if src:
            abs_url = urljoin(page_url, src)
            if is_febri_image(abs_url):
                url_to_local[abs_url] = image_filename(abs_url)

        srcset = tag.get("srcset", "")
        if srcset:
            _collect_srcset_urls(srcset, page_url)

    if not url_to_local:
        return html

    save_dir.mkdir(parents=True, exist_ok=True)

    # 画像を並行ダウンロード
    async def fetch_image(img_url: str, filename: str) -> None:
        dest = (save_dir / filename).resolve()
        if not str(dest).startswith(str(save_dir.resolve())):
            logger.warning("  IMG SKIP (path traversal) %s", filename)
            return
        if dest.exists():
            return
        try:
            resp = await http_client.get(img_url, timeout=IMAGE_TIMEOUT)
            resp.raise_for_status()
            dest.write_bytes(resp.content)
            logger.debug("  IMG  %s", filename)
        except Exception as exc:
            logger.warning("  IMG ERROR %s : %s", img_url, exc)

    await asyncio.gather(*(fetch_image(u, f) for u, f in url_to_local.items()))

    # soup ベースで src / srcset を書き換え（属性値のみを対象にする）
    for tag in soup.find_all("img"):
        src = tag.get("src", "")
        if src:
            abs_url = urljoin(page_url, src)
            if abs_url in url_to_local:
                tag["src"] = url_to_local[abs_url]

        srcset = tag.get("srcset", "")
        if srcset:
            new_parts = []
            for part in srcset.split(","):
                stripped = part.strip()
                tokens = stripped.split()
                if tokens:
                    abs_url = urljoin(page_url, tokens[0])
                    if abs_url in url_to_local:
                        tokens[0] = url_to_local[abs_url]
                new_parts.append(" ".join(tokens))
            tag["srcset"] = ", ".join(new_parts)

    return str(soup)


# ---- CSS 埋め込み -----------------------------------------------------------
async def inline_css(
    html: str,
    page_url: str,
    http_client: httpx.AsyncClient,
) -> str:
    """
    HTML 内の <link rel="stylesheet"> を取得してインライン <style> に置換する。
    @import も再帰的に展開する。
    """
    soup = BeautifulSoup(html, "html.parser")
    link_tags = soup.find_all("link", rel=lambda r: r and "stylesheet" in r)

    if not link_tags:
        return html

    async def fetch_css(css_url: str, depth: int = 0) -> str:
        """CSS を取得し、@import を再帰的に展開して返す。"""
        if depth > 5:
            return ""
        try:
            resp = await http_client.get(css_url, timeout=IMAGE_TIMEOUT)
            resp.raise_for_status()
            css_text = resp.text
        except Exception as exc:
            logger.warning("  CSS ERROR %s : %s", css_url, exc)
            return ""

        # @import url("...") を再帰展開
        async def replace_import(m: re.Match) -> str:
            raw = m.group(1).strip("'\"")
            abs_import = urljoin(css_url, raw)
            return await fetch_css(abs_import, depth + 1)

        # re.sub は非同期非対応なので手動で処理
        pattern = re.compile(r"@import\s+url\(([^)]+)\)[^;]*;")
        result = []
        last = 0
        for m in pattern.finditer(css_text):
            result.append(css_text[last : m.start()])
            result.append(await replace_import(m))
            last = m.end()
        result.append(css_text[last:])
        return "".join(result)

    for tag in link_tags:
        href = tag.get("href", "")
        if not href:
            continue
        abs_url = urljoin(page_url, href)
        logger.debug("  CSS  %s", abs_url)
        css_text = await fetch_css(abs_url)
        style_tag = soup.new_tag("style")
        style_tag.string = css_text
        # メディア属性を引き継ぐ
        media = tag.get("media")
        if media:
            style_tag["media"] = media
        tag.replace_with(style_tag)

    return str(soup)


# ---- ページ保存 -------------------------------------------------------------
async def save_page(
    semaphore: asyncio.Semaphore,
    browser_context,
    http_client: httpx.AsyncClient,
    url: str,
    force: bool,
) -> tuple[str, str, Path]:
    """
    1 ページを取得・画像ダウンロード込みで保存する。

    戻り値: (url, status, save_path)
      status: "saved" | "skipped" | "error:<message>"
    """
    save_path = url_to_save_path(url, SOURCE_DIR)

    if not force and save_path.exists():
        return url, "skipped", save_path

    async with semaphore:
        last_exc: Exception | None = None

        for attempt in range(1, RETRY_COUNT + 1):
            page = await browser_context.new_page()
            try:
                await page.goto(
                    url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT
                )
                # JS レンダリングの完了を待つ
                await page.wait_for_timeout(2000)
                html = await page.content()

                # 画像ダウンロード & src 書き換え
                html = await download_images(html, url, save_path.parent, http_client)

                # CSS インライン化
                html = await inline_css(html, url, http_client)

                save_path.parent.mkdir(parents=True, exist_ok=True)
                save_path.write_text(html, encoding="utf-8")

                await asyncio.sleep(REQUEST_DELAY)
                return url, "saved", save_path

            except Exception as exc:
                last_exc = exc
                logger.warning("RETRY %d/%d %s : %s", attempt, RETRY_COUNT, url, exc)

            finally:
                await page.close()

            # 次のリトライまで待機（最終 attempt 後はスリープしない）
            if attempt < RETRY_COUNT:
                await asyncio.sleep(RETRY_DELAY * attempt)  # 5s → 10s

        logger.error("FAILED %s : %s", url, last_exc)
        return url, f"error:{last_exc}", save_path


async def _run_browser_tasks(
    entries: list[tuple[str, str]],
    semaphore: asyncio.Semaphore,
    force: bool,
) -> dict[str, int]:
    """ブラウザ・HTTP クライアントを起動してページ保存タスクを実行し、結果を返す。"""
    results: dict[str, int] = {"saved": 0, "skipped": 0, "error": 0}
    async with (
        async_playwright() as pw,
        await pw.chromium.launch(headless=True) as browser,
        httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT}, follow_redirects=True
        ) as http_client,
    ):
        context = await browser.new_context(user_agent=USER_AGENT, locale="ja-JP")
        tasks = [
            save_page(semaphore, context, http_client, url, force) for _, url in entries
        ]
        total = len(tasks)
        for done, coro in enumerate(asyncio.as_completed(tasks), 1):
            url, status, save_path = await coro
            if status == "saved":
                results["saved"] += 1
                logger.info(
                    "SAVED (%d/%d) %s → %s",
                    done,
                    total,
                    url,
                    save_path.relative_to(SOURCE_DIR),
                )
            elif status == "skipped":
                results["skipped"] += 1
                logger.info("SKIP  (%d/%d) %s", done, total, url)
            elif status.startswith("error"):
                results["error"] += 1
                logger.error("FAILED (%d/%d) %s", done, total, url)
        await context.close()
    return results


async def _process_entries(
    entries: list[tuple[str, str]],
    semaphore: asyncio.Semaphore,
    force: bool,
) -> dict[str, int]:
    return await _run_browser_tasks(entries, semaphore, force)


async def run(
    csv_files: list[Path],
    force: bool,
    concurrency: int,
    dry_run: bool,
) -> None:
    entries = collect_urls(csv_files)
    if not entries:
        logger.warning("対象 URL が見つかりませんでした。")
        return

    logger.info("対象 URL: %d 件（CSV: %d ファイル）", len(entries), len(csv_files))

    if dry_run:
        for csv_name, url in entries:
            save_path = url_to_save_path(url, SOURCE_DIR)
            exists = "EXISTS" if save_path.exists() else "NEW   "
            print(f"[{exists}] [{csv_name}] {url}")
            print(f"         → {save_path}")
        return

    semaphore = asyncio.Semaphore(concurrency)
    results = await _process_entries(entries, semaphore, force)

    logger.info(
        "完了 — 保存: %d件 / スキップ: %d件 / エラー: %d件",
        results["saved"],
        results["skipped"],
        results["error"],
    )


# ---- エントリポイント --------------------------------------------------------
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="febri_jp/list/*.csv の URL を Playwright で保存する",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--list",
        nargs="+",
        metavar="FILE",
        help="処理する CSV ファイル名（省略時: list/ 配下の全 CSV）",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="保存済みファイルを強制再取得する",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=3,
        metavar="N",
        help="同時ページ数（デフォルト: 3）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="保存せずに対象 URL と保存先を表示する",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="DEBUG レベルのログを出力する",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    JST = datetime.timezone(datetime.timedelta(hours=9))
    log_file = SCRIPT_DIR / f"archive_{datetime.datetime.now(tz=JST).date()}.log"

    logging.basicConfig(
        level=log_level,
        format=LOG_FORMAT,
        handlers=[
            logging.StreamHandler(sys.stderr),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )

    if args.list:
        csv_files = [LIST_DIR / name for name in args.list]
        missing = [p for p in csv_files if not p.exists()]
        if missing:
            for p in missing:
                logger.error("CSV が見つかりません: %s", p)
            sys.exit(1)
    else:
        csv_files = sorted(LIST_DIR.glob("*.csv"))
        if not csv_files:
            logger.error("list/ ディレクトリに CSV ファイルがありません: %s", LIST_DIR)
            sys.exit(1)

    SOURCE_DIR.mkdir(parents=True, exist_ok=True)

    asyncio.run(
        run(
            csv_files=csv_files,
            force=args.force,
            concurrency=args.concurrency,
            dry_run=args.dry_run,
        )
    )


if __name__ == "__main__":
    main()
