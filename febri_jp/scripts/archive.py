"""
Febri ページ保存スクリプト

febri_jp/list/*.csv に記載された URL のページを Playwright で取得し、
febri_jp/source/ 配下に HTML ファイルとして保存する。
ページ内の febri.jp ドメインの画像も同じディレクトリにダウンロードし、
HTML 内の src をローカルパスに書き換える。

CSV の pages が 2 以上の記事は、1 ページ目のページ送り
（div.pagination a.post-page-numbers）のリンクに追従して 2 ページ目以降も保存する。

実サイトにアクセスできない場合（閉鎖後のリダイレクト・HTTP エラー・通信エラー）は
Internet Archive（Wayback Machine）から取得する。取得規則は次のとおり。
  HTML:  CDX API で WAYBACK_TO 以前の最新キャプチャ（200, text/html）を探し、
         id_（無加工）で取得する。改行は LF に揃える。
  CSS:   HTML と同じタイムスタンプで cs_ 取得し、web.archive.org の URL 接頭辞を外す。
         febri.jp 以外の @import は展開せず削除する。
  画像:  HTML と同じタイムスタンプで id_ 取得する。
  CSS・画像で WAYBACK_TO より後のキャプチャに解決された場合は、
  CDX API で WAYBACK_TO 以前の最も近いキャプチャを探して取り直す。

保存パス規則:
  https://febri.jp/topics/foo/   → source/topics/foo/index.html
  画像: source/topics/foo/<ファイル名>
  https://febri.jp/topics/foo/2/ → source/topics/foo/2/index.html
  画像: source/topics/foo/2/<ファイル名>

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
WAYBACK_BASE = "https://web.archive.org"
WAYBACK_CDX = f"{WAYBACK_BASE}/cdx/search/cdx"
# Internet Archive から取得するキャプチャの上限時刻（UTC, YYYYmmddHHMMSS）。
# 既存の source/ の 2 ページ目以降は 2026-11-25 07:02〜07:07 UTC に取得した。
# これより後のキャプチャを除外することで、再実行しても同じキャプチャを選ぶ。
WAYBACK_TO = "20261125070000"
WAYBACK_TIMEOUT = 90.0  # Internet Archive へのリクエストタイムアウト（秒）
WAYBACK_RETRY_COUNT = 5  # 429 / 5xx / 通信エラー時の最大試行回数
WAYBACK_RETRY_DELAY = (
    10.0  # 再試行前の待機秒数（attempt ごとに倍増: 10s, 20s, 40s, 80s）
)
# Internet Archive への同時リクエスト数の上限。
# 同時接続を増やすと接続を拒否され、しばらく拒否が続く（2026-11-25 に確認）。
WAYBACK_CONCURRENCY = 2
# 置換済み URL（https://web.archive.org/web/<ts><mode>/ や /web/<ts><mode>/）の接頭辞
WAYBACK_PREFIX_RE = re.compile(
    r"(?:https?://web\.archive\.org)?/web/\d+(?:[a-z]{2}_)?/"
)
# リダイレクト後の最終 URL からタイムスタンプと元 URL を取り出す
WAYBACK_REPLAY_RE = re.compile(r"^https?://web\.archive\.org/web/(\d{14})[a-z_]*/(.+)$")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

logger = logging.getLogger(__name__)


# ---- ユーティリティ ----------------------------------------------------------
def collect_urls(csv_files: list[Path]) -> list[tuple[str, str, int]]:
    """CSV ファイルから (csv_name, url, pages) のリストを返す。

    febri.jp ホスト以外の URL は SSRF 防止のためスキップする。
    pages 列が空・不正な場合は 1 として扱う。
    """
    entries: list[tuple[str, str, int]] = []
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
                try:
                    pages = max(1, int((row.get("pages") or "1").strip()))
                except ValueError:
                    logger.warning("pages 列が不正なため 1 とみなす: %s", url)
                    pages = 1
                entries.append((csv_path.name, url, pages))
    return entries


def _article_base_path(url: str) -> str:
    """記事 URL のパスを末尾スラッシュ付きで返す（例: /topics/foo/）。"""
    path = urlparse(url).path
    return path if path.endswith("/") else path + "/"


def default_subpage_url(url: str, page_no: int) -> str:
    """WordPress のページ分割規則に従った N ページ目の URL を返す（<記事URL>/N/）。"""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{_article_base_path(url)}{page_no}/"


def extract_subpage_urls(html: str, url: str) -> dict[int, str]:
    """
    HTML のページ送り（div.pagination a.post-page-numbers）から
    {ページ番号: URL} を返す。

    同じ記事の <記事パス>/<数字>/ を指す febri.jp のリンクだけを対象にする。
    """
    soup = BeautifulSoup(html, "html.parser")
    pattern = re.compile(rf"^{re.escape(_article_base_path(url))}(\d+)/?$")
    found: dict[int, str] = {}
    for a in soup.select("div.pagination a.post-page-numbers[href]"):
        abs_url = urljoin(url, a["href"])
        if not is_febri_url(abs_url):
            continue
        m = pattern.match(urlparse(abs_url).path)
        if not m:
            continue
        page_no = int(m.group(1))
        if page_no >= 2:
            found.setdefault(page_no, default_subpage_url(url, page_no))
    return found


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


def is_febri_url(url: str) -> bool:
    """febri.jp ドメイン（サブドメイン含む）の URL かどうか判定する。"""
    host = urlparse(url).netloc.lower()
    return host == FEBRI_HOST or host.endswith(f".{FEBRI_HOST}")


def is_febri_image(url: str) -> bool:
    """febri.jp ドメインの画像 URL かどうか判定する。"""
    return is_febri_url(url)


def image_filename(img_url: str) -> str:
    """画像 URL からファイル名を取得する（クエリ・フラグメントを除去）。"""
    if not img_url or not img_url.startswith("http"):
        raise ValueError(f"img_url は http(s) で始まる必要があります: {img_url}")
    parsed = urlparse(img_url)
    name = Path(parsed.path).name
    if not name or "." not in name:
        name = hashlib.sha256(img_url.encode()).hexdigest()[:12] + ".jpg"
    return name


# ---- 取得元（実サイト / Internet Archive） -----------------------------------
class LiveFetcher:
    """実サイトから画像・CSS を取得する。"""

    def __init__(self, http_client: httpx.AsyncClient) -> None:
        self.http_client = http_client

    def allows(self, url: str) -> bool:
        return True

    async def get_bytes(self, url: str) -> bytes:
        resp = await self.http_client.get(url, timeout=IMAGE_TIMEOUT)
        resp.raise_for_status()
        return resp.content

    async def get_text(self, url: str) -> str:
        resp = await self.http_client.get(url, timeout=IMAGE_TIMEOUT)
        resp.raise_for_status()
        return resp.text


_wayback_semaphore = asyncio.Semaphore(WAYBACK_CONCURRENCY)


async def wayback_request(
    http_client: httpx.AsyncClient, url: str, params=None
) -> httpx.Response:
    """Internet Archive へ GET する。429 / 5xx / 通信エラーは待機して再試行する。"""
    last_exc: Exception | None = None
    for attempt in range(1, WAYBACK_RETRY_COUNT + 1):
        try:
            async with _wayback_semaphore:
                resp = await http_client.get(
                    url, params=params, timeout=WAYBACK_TIMEOUT
                )
        except httpx.TransportError as exc:
            last_exc = exc
        else:
            if resp.status_code != 429 and resp.status_code < 500:
                resp.raise_for_status()
                return resp
            last_exc = httpx.HTTPStatusError(
                f"HTTP {resp.status_code}", request=resp.request, response=resp
            )
        if attempt < WAYBACK_RETRY_COUNT:
            wait = WAYBACK_RETRY_DELAY * 2 ** (attempt - 1)
            logger.debug(
                "  WAYBACK RETRY %d/%d (%.0fs) %s : %r",
                attempt,
                WAYBACK_RETRY_COUNT,
                wait,
                url,
                last_exc,
            )
            await asyncio.sleep(wait)
    assert last_exc is not None
    raise last_exc


async def find_wayback_capture(
    http_client: httpx.AsyncClient,
    url: str,
    *,
    closest: str | None = None,
    html_only: bool = False,
) -> tuple[str, str] | None:
    """
    CDX API で WAYBACK_TO 以前の 200 のキャプチャを探し、(timestamp, original) を返す。

    closest を指定するとその時刻に最も近いもの、省略すると最新のものを返す。
    """
    params: list[tuple[str, str]] = [
        ("url", url),
        ("output", "json"),
        ("fl", "timestamp,original"),
        ("filter", "statuscode:200"),
        ("to", WAYBACK_TO),
    ]
    if html_only:
        params.append(("filter", "mimetype:text/html"))
    if closest:
        params += [("closest", closest), ("sort", "closest"), ("limit", "1")]
    resp = await wayback_request(http_client, WAYBACK_CDX, params=params)
    rows = resp.json() if resp.text.strip() else []
    if len(rows) < 2:  # 先頭行はヘッダー
        return None
    timestamp, original = rows[1] if closest else rows[-1]
    return timestamp, original


class WaybackFetcher:
    """
    Internet Archive から、ページのキャプチャ時刻（timestamp）に近い
    画像・CSS を取得する。febri.jp 以外の URL は取得しない。
    """

    def __init__(self, http_client: httpx.AsyncClient, timestamp: str) -> None:
        self.http_client = http_client
        self.timestamp = timestamp

    def allows(self, url: str) -> bool:
        return is_febri_url(url)

    async def _get(self, url: str, mode: str) -> httpx.Response:
        if not self.allows(url):
            raise ValueError(f"febri.jp 以外は Internet Archive から取得しない: {url}")
        resp = await wayback_request(
            self.http_client, f"{WAYBACK_BASE}/web/{self.timestamp}{mode}/{url}"
        )
        m = WAYBACK_REPLAY_RE.match(str(resp.url))
        if m and m.group(1) > WAYBACK_TO:
            # 閉鎖後のキャプチャに解決された。上限時刻以前で最も近いものを取り直す
            capture = await find_wayback_capture(
                self.http_client, url, closest=self.timestamp
            )
            if capture is None:
                raise LookupError(f"{WAYBACK_TO} 以前のキャプチャがない: {url}")
            resp = await wayback_request(
                self.http_client, f"{WAYBACK_BASE}/web/{capture[0]}{mode}/{capture[1]}"
            )
        self._replayed(resp)
        return resp

    @staticmethod
    def _replayed(resp: httpx.Response) -> tuple[str, str]:
        """最終 URL が febri.jp のキャプチャであることを確かめ、(timestamp, 元URL) を返す。"""
        m = WAYBACK_REPLAY_RE.match(str(resp.url))
        if not m or not is_febri_url(m.group(2)):
            raise ValueError(f"febri.jp のキャプチャではない: {resp.url}")
        return m.group(1), m.group(2)

    async def get_bytes(self, url: str) -> bytes:
        return (await self._get(url, "id_")).content

    async def get_text(self, url: str) -> str:
        resp = await self._get(url, "cs_")
        return WAYBACK_PREFIX_RE.sub("", resp.text)


Fetcher = LiveFetcher | WaybackFetcher


# ---- 画像ダウンロード --------------------------------------------------------
async def download_images(
    html: str,
    page_url: str,
    save_dir: Path,
    fetcher: Fetcher,
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
            dest.write_bytes(await fetcher.get_bytes(img_url))
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
    fetcher: Fetcher,
) -> str:
    """
    HTML 内の <link rel="stylesheet"> を取得してインライン <style> に置換する。
    @import も再帰的に展開する。fetcher が取得しない URL の @import は削除する。
    """
    soup = BeautifulSoup(html, "html.parser")
    link_tags = soup.find_all("link", rel=lambda r: r and "stylesheet" in r)

    if not link_tags:
        return html

    async def fetch_css(css_url: str, depth: int = 0) -> str:
        """CSS を取得し、@import を再帰的に展開して返す。"""
        if depth > 5:
            return ""
        if not fetcher.allows(css_url):
            logger.debug("  CSS SKIP %s", css_url)
            return ""
        try:
            css_text = await fetcher.get_text(css_url)
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
PageResult = tuple[str, str, Path]  # (url, status, save_path)


class SiteClosedError(Exception):
    """実サイトが閉鎖済み（febri.jp 以外へのリダイレクト・HTTP エラー・HTML 以外）。"""


async def fetch_live(browser_context, url: str) -> str:
    """
    実サイトを Playwright で開き、JS 実行後の HTML を返す。

    閉鎖済みと判断できる応答なら SiteClosedError を送出する（再試行しない）。
    通信エラーなどは RETRY_COUNT 回まで再試行し、最後の例外を送出する。
    """
    last_exc: Exception | None = None
    for attempt in range(1, RETRY_COUNT + 1):
        page = await browser_context.new_page()
        try:
            resp = await page.goto(
                url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT
            )
            content_type = resp.headers.get("content-type", "") if resp else ""
            if (
                resp is None
                or not resp.ok
                or not is_febri_url(page.url)
                or "text/html" not in content_type
            ):
                status = resp.status if resp else None
                raise SiteClosedError(
                    f"status={status} url={page.url} content-type={content_type}"
                )
            # JS レンダリングの完了を待つ
            await page.wait_for_timeout(2000)
            return await page.content()
        except SiteClosedError:
            raise
        except Exception as exc:
            last_exc = exc
            logger.warning("RETRY %d/%d %s : %s", attempt, RETRY_COUNT, url, exc)
        finally:
            await page.close()

        # 次のリトライまで待機（最終 attempt 後はスリープしない）
        if attempt < RETRY_COUNT:
            await asyncio.sleep(RETRY_DELAY * attempt)  # 5s → 10s

    assert last_exc is not None
    raise last_exc


async def fetch_wayback(http_client: httpx.AsyncClient, url: str) -> tuple[str, str]:
    """
    Internet Archive から WAYBACK_TO 以前の最新キャプチャの HTML を取得する。

    戻り値: (timestamp, html)。HTML は id_（無加工）で取得し、改行を LF に揃える。
    """
    capture = await find_wayback_capture(http_client, url, html_only=True)
    if capture is None:
        raise LookupError(f"Internet Archive にキャプチャがない（{WAYBACK_TO} 以前）")
    timestamp, original = capture
    resp = await wayback_request(
        http_client, f"{WAYBACK_BASE}/web/{timestamp}id_/{original}"
    )
    return timestamp, resp.text.replace("\r\n", "\n")


async def fetch_and_save(
    semaphore: asyncio.Semaphore,
    browser_context,
    http_client: httpx.AsyncClient,
    url: str,
    save_path: Path,
) -> tuple[str, str | None]:
    """
    1 ページを取得し、画像ダウンロード・CSS インライン化をして保存する。

    実サイトから取得できなければ Internet Archive から取得する。

    戻り値: (status, html)
      status: "saved" | "saved:wayback:<timestamp>" | "error:<message>"
      html:   取得した HTML（書き換え前）。失敗時は None
    """
    async with semaphore:
        status = "saved"
        fetcher: Fetcher
        try:
            raw_html = await fetch_live(browser_context, url)
            fetcher = LiveFetcher(http_client)
        except Exception as live_exc:
            logger.info(
                "LIVE NG %s : %s → Internet Archive から取得する", url, live_exc
            )
            try:
                timestamp, raw_html = await fetch_wayback(http_client, url)
            except Exception as exc:
                logger.error("FAILED %s : live=%s / wayback=%s", url, live_exc, exc)
                return f"error:live={live_exc} / wayback={exc}", None
            fetcher = WaybackFetcher(http_client, timestamp)
            status = f"saved:wayback:{timestamp}"

        try:
            # 画像ダウンロード & src 書き換え
            html = await download_images(raw_html, url, save_path.parent, fetcher)
            # CSS インライン化
            html = await inline_css(html, url, fetcher)
            save_path.parent.mkdir(parents=True, exist_ok=True)
            save_path.write_text(html, encoding="utf-8")
        except Exception as exc:
            logger.error("FAILED %s : %s", url, exc)
            return f"error:{exc}", None

        await asyncio.sleep(REQUEST_DELAY)
        return status, raw_html


def _resolve_subpage_url(links: dict[int, str], url: str, page_no: int) -> str:
    """ページ送りから得た N ページ目の URL を返す。見つからなければ既定規則で補う。"""
    sub_url = links.get(page_no)
    if sub_url is None:
        sub_url = default_subpage_url(url, page_no)
        logger.warning(
            "ページ送りに %d ページ目のリンクがないため既定 URL を使う: %s",
            page_no,
            sub_url,
        )
    return sub_url


async def save_page(
    semaphore: asyncio.Semaphore,
    browser_context,
    http_client: httpx.AsyncClient,
    url: str,
    pages: int,
    force: bool,
) -> list[PageResult]:
    """
    1 記事を保存する。pages が 2 以上なら、ページ送りのリンクに追従して
    2 ページ目以降も保存する。

    保存済みのページは取得せずスキップする（--force 時を除く）。
    スキップしたページも、リンク抽出のためにローカルの HTML を読む。

    戻り値: ページごとの (url, status, save_path) のリスト
      status: "saved" | "saved:wayback:<timestamp>" | "skipped" | "error:<message>"
    """
    results: list[PageResult] = []

    save_path = url_to_save_path(url, SOURCE_DIR)
    html: str | None = None
    if not force and save_path.exists():
        results.append((url, "skipped", save_path))
        if pages >= 2:
            html = save_path.read_text(encoding="utf-8")
    else:
        status, html = await fetch_and_save(
            semaphore, browser_context, http_client, url, save_path
        )
        results.append((url, status, save_path))

    if pages < 2:
        return results

    if html is None:
        for page_no in range(2, pages + 1):
            sub_url = default_subpage_url(url, page_no)
            results.append(
                (
                    sub_url,
                    "error:1 ページ目を取得できなかったため未取得",
                    url_to_save_path(sub_url, SOURCE_DIR),
                )
            )
        return results

    links = extract_subpage_urls(html, url)

    for page_no in range(2, pages + 1):
        sub_url = _resolve_subpage_url(links, url, page_no)
        sub_path = url_to_save_path(sub_url, SOURCE_DIR)

        # 後続ページのリンクが未発見なら、このページのページ送りからも探す
        need_more = any(n not in links for n in range(page_no + 1, pages + 1))

        if not force and sub_path.exists():
            results.append((sub_url, "skipped", sub_path))
            sub_html = sub_path.read_text(encoding="utf-8") if need_more else None
        else:
            status, sub_html = await fetch_and_save(
                semaphore, browser_context, http_client, sub_url, sub_path
            )
            results.append((sub_url, status, sub_path))

        if sub_html is not None:
            for n, u in extract_subpage_urls(sub_html, url).items():
                links.setdefault(n, u)

    if links and max(links) > pages:
        logger.warning(
            "ページ送りに CSV の pages（%d）を超えるページ（%d）がある: %s",
            pages,
            max(links),
            url,
        )

    return results


async def _run_browser_tasks(
    entries: list[tuple[str, str, int]],
    semaphore: asyncio.Semaphore,
    force: bool,
) -> dict[str, int]:
    """ブラウザ・HTTP クライアントを起動してページ保存タスクを実行し、結果を返す。

    件数はページ単位で数える（2 ページ目以降も 1 件として数える）。
    """
    results: dict[str, int] = {"saved": 0, "wayback": 0, "skipped": 0, "error": 0}
    async with (
        async_playwright() as pw,
        await pw.chromium.launch(headless=True) as browser,
        httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT}, follow_redirects=True
        ) as http_client,
    ):
        context = await browser.new_context(user_agent=USER_AGENT, locale="ja-JP")
        tasks = [
            save_page(semaphore, context, http_client, url, pages, force)
            for _, url, pages in entries
        ]
        total = len(tasks)
        for done, coro in enumerate(asyncio.as_completed(tasks), 1):
            for url, status, save_path in await coro:
                if status.startswith("saved"):
                    results["saved"] += 1
                    via = status.removeprefix("saved")
                    if via:
                        results["wayback"] += 1
                    logger.info(
                        "SAVED (%d/%d) %s → %s%s",
                        done,
                        total,
                        url,
                        save_path.relative_to(SOURCE_DIR),
                        f" [{via.lstrip(':')}]" if via else "",
                    )
                elif status == "skipped":
                    results["skipped"] += 1
                    logger.info("SKIP  (%d/%d) %s", done, total, url)
                elif status.startswith("error"):
                    results["error"] += 1
                    logger.error("FAILED (%d/%d) %s : %s", done, total, url, status)
        await context.close()
    return results


async def _process_entries(
    entries: list[tuple[str, str, int]],
    semaphore: asyncio.Semaphore,
    force: bool,
) -> dict[str, int]:
    return await _run_browser_tasks(entries, semaphore, force)


def _dry_run_targets(url: str, pages: int) -> list[str]:
    """ドライラン用に、記事の全ページ URL を返す（ネットワークにはアクセスしない）。

    1 ページ目が保存済みならそのページ送りのリンクを使い、なければ既定規則で補う。
    """
    targets = [url]
    if pages < 2:
        return targets
    save_path = url_to_save_path(url, SOURCE_DIR)
    links = (
        extract_subpage_urls(save_path.read_text(encoding="utf-8"), url)
        if save_path.exists()
        else {}
    )
    for page_no in range(2, pages + 1):
        targets.append(links.get(page_no) or default_subpage_url(url, page_no))
    return targets


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

    total_pages = sum(pages for _, _, pages in entries)
    logger.info(
        "対象 URL: %d 件 / %d ページ（CSV: %d ファイル）",
        len(entries),
        total_pages,
        len(csv_files),
    )

    if dry_run:
        for csv_name, url, pages in entries:
            for target in _dry_run_targets(url, pages):
                save_path = url_to_save_path(target, SOURCE_DIR)
                exists = "EXISTS" if save_path.exists() else "NEW   "
                print(f"[{exists}] [{csv_name}] {target}")
                print(f"         → {save_path}")
        return

    semaphore = asyncio.Semaphore(concurrency)
    results = await _process_entries(entries, semaphore, force)

    logger.info(
        "完了 — 保存: %d件（うち Internet Archive: %d件） / スキップ: %d件 / エラー: %d件",
        results["saved"],
        results["wayback"],
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
