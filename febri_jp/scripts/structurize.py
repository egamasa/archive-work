"""
Febri 記事 構造化スクリプト（HTML → Markdown）

febri_jp/list/*.csv に記載された記事を、archive.py が保存した
febri_jp/source/ 配下の HTML から読み取り、febri_jp/output/ 配下に
Markdown（index.md）として書き出す。source/ は読み取りのみで、変更しない。

出力規則:
  source/topics/foo/index.html（+ 2/index.html, 3/index.html …）
    → output/topics/foo/index.md（全ページを 1 ファイルに統合）
  本文で使う画像は source/ から output/ の同じ相対位置にコピーする。
    1 ページ目の画像: output/topics/foo/<ファイル名>
    N ページ目の画像: output/topics/foo/N/<ファイル名>
  記事末尾のエンドマーク画像（endmark.png）はコピーせず、水平線（---）にする。
  エンドマークの直後に画像が続く場合は、その画像（キャプションを含む）の後ろに水平線を置く。
  改行（<br>）は段落の区切りにする。作品情報・書籍情報の中だけは行末の「\\」で改行する。
  変換対象の記事を指す febri.jp のリンクは、変換後の index.md への相対リンクにする。
  それ以外の febri.jp のリンク（タグ一覧、アップロードファイルなど）は元の URL のまま残す。

抽出する範囲（記事ページ）:
  タイトル、アイキャッチ、リード文、クレジット、本文ブロック（全ページ）、
  作品情報・書籍情報、カタリベプロフィール。
  カテゴリ・公開日時・タグ・元 URL は front matter に入れる。
  ナビ、パンくず、SNS ボタン、ページ送り、関連記事、ランキング、広告は除外する。
一覧ページ（ガルパン エンサイクロペディア、カタリベリスト）も各項目を変換する。

取りこぼしの検査:
  変換対象の領域（除外要素を除く）にあるすべてのテキストノードが
  出力 Markdown に含まれるか、すべての画像がコピーされたかを記事ごとに確認し、
  見つからないものを WARNING としてログに出す。

使い方:
  # すべての CSV を処理
  python3 structurize.py

  # 特定の CSV のみ処理
  python3 structurize.py --list topics.csv febri_talk.csv

  # 特定の記事のみ処理
  python3 structurize.py --url https://febri.jp/topics/foo/

  # 出力先・並列数を変更
  python3 structurize.py --output /path/to/out --workers 4
"""

import argparse
import csv
import datetime
import filecmp
import logging
import os
import posixpath
import re
import shutil
import string
import sys
import unicodedata
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

# ---- パス定義 ----------------------------------------------------------------
SCRIPT_DIR = Path(__file__).parent
LIST_DIR = SCRIPT_DIR.parent / "list"
SOURCE_DIR = SCRIPT_DIR.parent / "source"
OUTPUT_DIR = SCRIPT_DIR.parent / "output"

# ---- 定数 -------------------------------------------------------------------
FEBRI_HOST = "febri.jp"
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
JST = datetime.timezone(datetime.timedelta(hours=9))

# HTML の空白として畳み込む文字。全角スペース（U+3000）は本文の一部なので含めない。
HTML_WS_RE = re.compile(r"[ \t\n\r\f\v\xa0]+")
# <title> 末尾のサイト名
TITLE_SUFFIX_RE = re.compile(r"\s*[|｜]\s*Febri\s*$")
# 本文中の URL（ASCII のみ。直後の日本語を巻き込まない）
BARE_URL_RE = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")
# Markdown の構文として解釈されうる文字（行頭以外）
MD_ESCAPE_RE = re.compile(r"([\\`*_\[\]~|])")
# 行頭で構文になる並び
LINE_START_RES = [
    (re.compile(r"^([#>])"), r"\\\1"),
    (re.compile(r"^([-+])(?= |$)"), r"\\\1"),
    (re.compile(r"^(\d{1,9})([.)])(?= |$)"), r"\1\\\2"),
    (re.compile(r"^(=+)(\s*)$"), r"\\\1\2"),
    (re.compile(r"^(-{2,})(\s*)$"), r"\\\1\2"),
]
# 変換中の改行（<br>）の印。出力前に、作品情報では行末の「\」、それ以外では段落の区切りにする
BR = "<br>\n"
HARD_BREAK = "\\\n"
# 記事末尾のエンドマーク画像。画像としては出力せず、水平線に置き換える
ENDMARK_NAMES = {"endmark.png"}
HR_MARK = "\x00HR\x00"  # 変換中に水平線の位置を示す印（finish() で --- にする）
# 画像ブロックのキャプション（引用）に付ける印。エンドマークの直後の画像をキャプションごと
# 水平線の前に移すために使い、finish() で取り除く
CAPTION_MARK = "\x00CAP\x00"
IMAGE_ONLY_RE = re.compile(r"(?:!\[\]\((?:<[^>]*>|[^)\s]*)\)\s*(?:<br>)?\s*)+")

# 本文として扱わない要素（変換・検査の両方で無視する）
SKIP_TAGS = {
    "script",
    "style",
    "noscript",
    "template",
    "button",
    "form",
    "input",
    "select",
    "textarea",
    "svg",
    "source",
    "track",
    "link",
    "meta",
}
INLINE_TAGS = {
    "a",
    "abbr",
    "b",
    "bdi",
    "bdo",
    "br",
    "cite",
    "code",
    "data",
    "del",
    "dfn",
    "em",
    "font",
    "i",
    "iframe",
    "img",
    "ins",
    "kbd",
    "label",
    "mark",
    "q",
    "rp",
    "rt",
    "ruby",
    "s",
    "samp",
    "small",
    "span",
    "strike",
    "strong",
    "sub",
    "sup",
    "time",
    "tt",
    "u",
    "var",
    "wbr",
}
# 本文領域の中でも出力しない要素（ページ送り・SNS・動画プレーヤーの操作部品など）
EXCLUDE_IN_CONTENT = [
    ".pagination",
    ".prev_next_posts",
    ".sns",
    ".addtoany_shortcode",
    ".mejs-controls",
    ".mejs-layers",
    ".mejs-offscreen",
    ".mejs-overlay",
    "span.date",
    # シリーズトップの種別バッジ（「text」）。作品情報の見出し（.article_cat_light）は本文として残す
    ".series_list > .article_cat:not(.article_cat_light)",
]
# div.content / div.detail の直下で、本文として扱わないと判断済みの要素
KNOWN_EXCLUDED_SECTIONS = {
    "sns",
    "related-profile",
    "related_post",
    "related-list",
    "list-outlink",
    "ads",
    "filter-cat",
    "line-back",
}
KNOWN_INCLUDED_SECTIONS = {
    "box_banner",
    "detail",
    "box_list_b_content",
    "box-profile",
    "series_list",
    "banner_series",
    "head-content",
    "list_filter",
}

logger = logging.getLogger(__name__)


# ---- データ ------------------------------------------------------------------
@dataclass
class Entry:
    """CSV の 1 行（1 記事）。"""

    csv_name: str
    url: str
    title: str
    datetime: str
    pages: int
    video_url: str = ""


@dataclass
class Result:
    """1 記事の変換結果（ワーカープロセスからの戻り値）。"""

    url: str
    md_path: str = ""
    pages: int = 0
    images_copied: int = 0
    images_skipped: int = 0
    empty_images: int = 0
    missing_texts: int = 0
    missing_images: int = 0
    endmarks: int = 0
    internal_links: int = 0  # 相対リンクに書き換えた febri.jp のリンク
    unresolved_links: list[str] = field(default_factory=list)
    logs: list[tuple[int, str]] = field(default_factory=list)


# ---- ユーティリティ ----------------------------------------------------------
def is_febri_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host == FEBRI_HOST or host.endswith(f".{FEBRI_HOST}")


def is_endmark(img: Tag) -> bool:
    src = (img.get("src") or "").strip()
    return bool(src) and Path(urlparse(src).path).name in ENDMARK_NAMES


def url_to_rel_dir(url: str) -> Path:
    """URL のパスを相対ディレクトリにする（archive.py の保存規則と同じ）。"""
    parts = [p for p in urlparse(url).path.split("/") if p and p not in (".", "..")]
    return Path(*parts) if parts else Path("index")


def classes(tag: Tag) -> list[str]:
    return tag.get("class") or []


def has_class(tag: Tag, *names: str) -> bool:
    cls = classes(tag)
    return any(n in cls for n in names)


def collapse_ws(text: str) -> str:
    return HTML_WS_RE.sub(" ", text)


def is_peelable(ch: str) -> bool:
    """強調記号の外側に出す文字（空白と非 ASCII の約物・記号）。

    「**「強調」**」のように約物で始まる・終わる強調は CommonMark では
    強調にならないため、約物を記号の外に出す。ASCII の約物は
    Markdown の構文（リンクの括弧やエスケープ）と区別できないので対象外。
    """
    if ch.isspace():
        return True
    return ord(ch) > 0x7F and unicodedata.category(ch)[0] in "PSZ"


LINK_ONLY_RE = re.compile(r"^\[((?:\\.|[^\]\\])*)\]\((<[^>]*>|[^()\s]*)\)$")


def wrap_emphasis(marker: str, text: str) -> str:
    """text を marker（** か *）で囲む。改行（<br>）をまたぐ場合は行ごとに囲む。"""
    if BR in text:
        return BR.join(wrap_emphasis(marker, part) for part in text.split(BR))
    start, end = 0, len(text)
    while start < end and is_peelable(text[start]):
        start += 1
    while end > start and is_peelable(text[end - 1]):
        end -= 1
    if start == end:
        return text
    core = text[start:end]
    if core[0] in string.punctuation or core[-1] in string.punctuation:
        # ASCII の記号で始まる・終わる強調は、前後の文字によって強調として解釈されない。
        # リンクだけを囲む場合はリンク文字列の内側に移し、それ以外は強調を付けない。
        m = LINK_ONLY_RE.match(core)
        if not m:
            return text
        core = f"[{wrap_emphasis(marker, m.group(1))}]({m.group(2)})"
        return f"{text[:start]}{core}{text[end:]}"
    return f"{text[:start]}{marker}{core}{marker}{text[end:]}"


def escape_text(text: str) -> str:
    text = MD_ESCAPE_RE.sub(r"\\\1", text)
    text = re.sub(r"<(?=[A-Za-z/!?])", r"\\<", text)
    return re.sub(r"&(?=#?\w+;)", r"\\&", text)


def strip_url_tail(url: str) -> tuple[str, str]:
    """URL 末尾の句読点や対応しない閉じ括弧を URL から外す。"""
    tail = ""
    while url:
        ch = url[-1]
        if ch in ".,;:!?'\"" or (ch == ")" and url.count("(") < url.count(")")):
            tail = ch + tail
            url = url[:-1]
        else:
            break
    return url, tail


def escape_with_autolinks(text: str, link=None) -> str:
    """テキスト中の URL を <URL> にし、それ以外をエスケープする。

    link: URL を受け取り、書き換え後のリンク先（相対パス）か None を返す関数
    """
    out: list[str] = []
    pos = 0
    for m in BARE_URL_RE.finditer(text):
        url, tail = strip_url_tail(m.group(0))
        if not urlparse(url).netloc:
            continue
        out.append(escape_text(text[pos : m.start()]))
        rel = link(url) if link else None
        out.append(f"[{escape_text(url)}]({link_dest(rel)})" if rel else f"<{url}>")
        out.append(escape_text(tail))
        pos = m.end()
    out.append(escape_text(text[pos:]))
    return "".join(out)


def link_dest(url: str) -> str:
    if any(c in url for c in " ()<>"):
        return "<" + url.replace("<", "%3C").replace(">", "%3E") + ">"
    return url


# ---- febri.jp 内リンクの書き換え ---------------------------------------------
# 変換対象の記事の URL パス（デコード済み、末尾スラッシュ付き）→ 出力ディレクトリ（相対、POSIX 形式）。
# 2 ページ目以降の URL（<記事>/N/）も同じ記事に対応させる。ワーカープロセスでは init_worker で設定する。
LINK_MAP: dict[str, str] = {}


def normalize_febri_path(url: str) -> str | None:
    """febri.jp の URL をリンク表の照合用のパスにする。febri.jp 以外は None。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not is_febri_url(url):
        return None
    path = unquote(parsed.path)
    return path if path.endswith("/") else path + "/"


def build_link_map(entries: list["Entry"], source_dir: Path) -> dict[str, str]:
    link_map: dict[str, str] = {}
    for entry in entries:
        rel = url_to_rel_dir(entry.url)
        base_dir = source_dir / rel
        if not (base_dir / "index.html").is_file():
            continue
        path = normalize_febri_path(entry.url)
        if path is None:
            continue
        link_map[path] = rel.as_posix()
        for page in find_pages(base_dir)[1:]:
            link_map[f"{path}{page.parent.name}/"] = rel.as_posix()
    return link_map


def init_worker(link_map: dict[str, str]) -> None:
    LINK_MAP.clear()
    LINK_MAP.update(link_map)


def relative_md_link(url: str, md_dir: str) -> str | None:
    """変換済みの記事を指す febri.jp の URL を、md_dir からの相対リンクにする。"""
    path = normalize_febri_path(url)
    if path is None or path not in LINK_MAP:
        return None
    rel = posixpath.relpath(f"{LINK_MAP[path]}/index.md", start=md_dir)
    # ディレクトリ名に含まれる % がパーセントエンコードとして解釈されないようにする
    return rel.replace("%", "%25")


def escape_line_start(line: str) -> str:
    for pattern, repl in LINE_START_RES:
        line = pattern.sub(repl, line, count=1)
    return line


def clean_inline(text: str) -> str:
    """インライン要素を連結した結果の空白と改行を整える。"""
    text = re.sub(r" {2,}", " ", text)
    text = re.sub(r" *<br>\n *", BR, text)
    # 行頭の全角スペースは字下げとして残し、行末の空白（全角を含む）は表示に影響しないので落とす
    lines = [
        escape_line_start(line.strip(" ").rstrip(" \u3000"))
        for line in text.split("\n")
    ]
    text = "\n".join(lines)
    # 段落の先頭・末尾の <br> は表示に影響しないので落とす
    while text.startswith(BR):
        text = text[len(BR) :]
    while text.endswith("<br>"):
        text = text[: -len("<br>")].rstrip(" \n")
    return text.strip(" \n")


def quote(blocks: list[str]) -> str:
    """ブロックのリストを 1 つの引用ブロックにする。"""
    lines: list[str] = []
    for i, block in enumerate(blocks):
        if i:
            lines.append(">")
        lines.extend(f"> {line}" if line else ">" for line in block.split("\n"))
    return "\n".join(lines)


def indent(text: str, prefix: str) -> str:
    return "\n".join(prefix + line if line else line for line in text.split("\n"))


def normalize_for_check(text: str) -> str:
    """取りこぼし検査用に、空白と Markdown の記号を取り除く。"""
    text = text.replace("<br>", "")
    return re.sub(r"[\s\\*_`<>|#\[\]()!~\-+.=]", "", text)


# ---- HTML → Markdown 変換 -----------------------------------------------------
class Renderer:
    """1 ページ分の HTML 要素を Markdown に変換する。

    page_dir:   HTML があるディレクトリ（画像の参照元）
    img_prefix: 出力 Markdown から見た画像の置き場所（1 ページ目は ""、N ページ目は "N/"）
    md_dir:     出力 Markdown のディレクトリ（出力先からの相対、POSIX 形式。相対リンクの起点）
    """

    def __init__(
        self, page_dir: Path, page_url: str, img_prefix: str, md_dir: str
    ) -> None:
        self.page_dir = page_dir.resolve()
        self.page_url = page_url
        self.img_prefix = img_prefix
        self.md_dir = md_dir
        self.images: dict[str, Path] = {}  # Markdown 上の相対パス → コピー元
        self.empty_images = 0
        self.endmarks = 0
        self.unresolved_links: list[
            str
        ] = []  # 変換済みの記事に対応しない febri.jp の URL
        self.logs: list[tuple[int, str]] = []
        self._emph_depth = {"**": 0, "*": 0}

    def warn(self, msg: str) -> None:
        self.logs.append((logging.WARNING, msg))

    # ---- 画像 ----------------------------------------------------------------
    def resolve_image(self, img: Tag) -> tuple[str, Path | None] | None:
        """img の参照先を (Markdown 上のパス, ローカルファイル) で返す。

        srcset にローカル保存された大きい画像があればそれを優先する。
        src が空の img（元サイトの空欄）は None を返す。
        """
        candidates: list[tuple[int, str]] = []
        for part in (img.get("srcset") or "").split(","):
            bits = part.strip().split()
            if not bits:
                continue
            width = 0
            if len(bits) > 1 and bits[1].endswith("w") and bits[1][:-1].isdigit():
                width = int(bits[1][:-1])
            candidates.append((width, bits[0]))
        candidates.sort(key=lambda c: -c[0])
        src = (img.get("src") or "").strip()
        names = [name for _, name in candidates] + ([src] if src else [])
        for name in names:
            path = self._local_path(name)
            if path is not None and path.is_file():
                return self.img_prefix + name, path
        if not src:
            return None
        if src.startswith(("http://", "https://", "//")):
            return urljoin(self.page_url, src), None
        if src.startswith("data:"):
            return None
        self.warn(f"画像ファイルがありません: {self.page_dir / src}")
        return None

    def _local_path(self, name: str) -> Path | None:
        if not name or "://" in name or name.startswith(("//", "data:")):
            return None
        path = (self.page_dir / name).resolve()
        if not path.is_relative_to(self.page_dir):
            return None
        return path

    def image(self, img: Tag) -> str:
        if is_endmark(img):
            # 記事末尾のエンドマークは画像にせず、finish() で水平線にする
            self.endmarks += 1
            return HR_MARK
        ref = self.resolve_image(img)
        if ref is None:
            if not (img.get("src") or "").strip():
                self.empty_images += 1
            return ""
        md_path, local = ref
        if local is not None:
            self.images[md_path] = local
        else:
            self.warn(f"ローカルに保存されていない画像を URL で参照します: {md_path}")
        return f"![]({link_dest(md_path)})"

    # ---- 埋め込み --------------------------------------------------------------
    def embed_link(self, iframe: Tag) -> str:
        src = urljoin(self.page_url, (iframe.get("src") or "").strip())
        title = collapse_ws(iframe.get("title") or "").strip()
        parsed = urlparse(src)
        host = parsed.netloc.lower()
        if iframe.get("data-tweet-id"):
            url = f"https://twitter.com/i/status/{iframe['data-tweet-id']}"
            label = "X（Twitter）の投稿"
        elif "youtube.com" in host or "youtube-nocookie.com" in host:
            m = re.match(r"/embed/([\w-]+)", parsed.path)
            url = f"https://www.youtube.com/watch?v={m.group(1)}" if m else src
            label = f"YouTube: {title}" if title else "YouTube"
        elif "spotify.com" in host:
            url = re.sub(r"/embed(-podcast)?/", "/", src.split("?")[0])
            label = f"Spotify: {title}" if title else "Spotify"
        elif "google." in host and "/maps" in parsed.path:
            url = src
            label = "Google マップ"
        elif src:
            url = src
            label = f"埋め込み: {title}" if title else "埋め込み"
        else:
            return ""
        return f"[{escape_text(label)}]({link_dest(url)})"

    def video_link(self, video: Tag) -> str:
        # <source> の src は日本語のファイル名がそのまま入っていて読みやすいので優先する
        source = video.find("source", src=True)
        src = source["src"].strip() if source else ""
        if not src:
            src = (video.get("src") or "").strip()
        if not src:
            return ""
        url = urljoin(self.page_url, src)
        url = re.sub(r"\?_=\d+$", "", url)
        name = unquote(Path(urlparse(url).path).name)
        return f"[{escape_text('動画: ' + name)}]({link_dest(url)})"

    # ---- インライン ------------------------------------------------------------
    def inline(self, node, in_link: bool = False, plain: bool = False) -> str:
        """node の子要素をインライン Markdown にする（clean_inline 前の文字列）。"""
        out: list[str] = []
        for child in node.children:
            out.append(self.inline_node(child, in_link, plain))
        return "".join(out)

    def inline_node(self, node, in_link: bool = False, plain: bool = False) -> str:
        if isinstance(node, Comment):
            return ""
        if isinstance(node, NavigableString):
            if type(node) is not NavigableString:  # CData, Doctype など
                return ""
            text = collapse_ws(str(node))
            if in_link:
                return escape_text(text)
            return escape_with_autolinks(text, link=self.rewrite_link)
        if not isinstance(node, Tag) or node.name in SKIP_TAGS:
            return ""
        if self.is_excluded(node):
            return ""
        name = node.name
        if name == "br":
            return " " if plain else BR
        if name == "img":
            return "" if plain else self.image(node)
        if name == "iframe":
            return "" if plain or in_link else self.embed_link(node)
        if name == "video":
            return "" if plain or in_link else self.video_link(node)
        if name == "a":
            return self.link(node, in_link, plain)
        if name in ("strong", "b"):
            return self.emphasis("**", node, in_link, plain)
        if name in ("em", "i", "cite"):
            return self.emphasis("*", node, in_link, plain)
        if name == "span" and "color" in (node.get("style") or ""):
            # インタビューの話者名などの色付き文字は太字にする
            return self.emphasis("**", node, in_link, plain)
        if name == "rt":
            inner = self.inline(node, in_link, plain).strip()
            return f"（{inner}）" if inner else ""
        if name in ("p", "div", "li", "dd", "dt", "h1", "h2", "h3", "h4", "h5", "h6"):
            # インライン文脈に紛れ込んだブロック要素は改行で区切る
            inner = self.inline(node, in_link, plain)
            return f"{inner}{' ' if plain else BR}"
        return self.inline(node, in_link, plain)

    def emphasis(self, marker: str, node: Tag, in_link: bool, plain: bool) -> str:
        if plain or self._emph_depth[marker]:
            return self.inline(node, in_link, plain)
        self._emph_depth[marker] += 1
        try:
            inner = self.inline(node, in_link, plain)
        finally:
            self._emph_depth[marker] -= 1
        inner = re.sub(r" *<br>\n *", BR, re.sub(r" {2,}", " ", inner))
        return wrap_emphasis(marker, inner)

    def rewrite_link(self, url: str) -> str | None:
        """febri.jp の URL なら変換後の Markdown への相対リンクを返す。

        変換対象でない febri.jp の URL（タグ一覧、アップロードファイルなど）は記録して None を返す。
        """
        if normalize_febri_path(url) is None:
            return None
        rel = relative_md_link(url, self.md_dir)
        if rel is None:
            self.unresolved_links.append(url)
        return rel

    def link_target(self, href: str) -> str:
        return self.rewrite_link(href) or href

    def link(self, a: Tag, in_link: bool, plain: bool) -> str:
        text = self.inline(a, in_link=True, plain=plain)
        if in_link:
            return text
        href = (a.get("href") or "").strip()
        if href.lower().startswith("javascript:"):
            return text
        href = urljoin(self.page_url, href) if href and href != "#" else "#"
        # リンク文字列の中では段落を分けられないので、改行は空白にする
        label = re.sub(r" *<br>\n *", " ", text.strip(" "))
        if not label:
            return text
        dest = self.link_target(href)
        plain_label = re.sub(r"\\(.)", r"\1", label)
        is_url_label = plain_label == href and re.match(r"https?://", href)
        if is_url_label and " " not in href and dest == href:
            return f"<{href}>"
        lead = text[: len(text) - len(text.lstrip(" "))]
        tail = text[len(text.rstrip(" ")) :]
        return f"{lead}[{label}]({link_dest(dest)}){tail}"

    # ---- ブロック --------------------------------------------------------------
    def is_excluded(self, tag: Tag) -> bool:
        return any(tag.css.match(sel) for sel in EXCLUDE_IN_CONTENT)

    def paragraph(self, node) -> str:
        return clean_inline(self.inline(node))

    def heading(self, level: int, node: Tag) -> list[str]:
        text = clean_inline(self.inline(node, plain=True))
        text = re.sub(r"\s*\n\s*", " ", text)
        return [f"{'#' * level} {text}"] if text else []

    def blocks(self, node) -> list[str]:
        """node の子要素をブロックのリストにする。連続するインライン要素は 1 段落にまとめる。"""
        out: list[str] = []
        run: list = []

        def flush() -> None:
            if run:
                text = clean_inline("".join(self.inline_node(n) for n in run))
                if text:
                    out.append(text)
                run.clear()

        for child in node.children:
            if isinstance(child, NavigableString) or (
                isinstance(child, Tag)
                and child.name in INLINE_TAGS
                and not self.contains_block(child)
            ):
                run.append(child)
            elif isinstance(child, Tag):
                flush()
                out.extend(b for b in self.block(child) if b)
        flush()
        return out

    def contains_block(self, tag: Tag) -> bool:
        return any(
            t.name not in INLINE_TAGS and t.name not in SKIP_TAGS
            for t in tag.find_all(True)
        )

    def block(self, tag: Tag) -> list[str]:
        name = tag.name
        if name in SKIP_TAGS or self.is_excluded(tag):
            return []
        # ---- Febri の本文ブロック ----
        if has_class(tag, "title_h3"):
            return self.heading(2, tag)
        if has_class(tag, "title_h4"):
            return self.heading(3, tag)
        if has_class(tag, "image_info"):
            return self.image_info(tag)
        if has_class(tag, "list-info") and not has_class(tag, "copyright-small"):
            return self.list_info(tag)
        if has_class(tag, "box-bg-detailB"):
            lines = [self.paragraph(p) for p in tag.find_all("p")] or [
                self.paragraph(tag)
            ]
            lines = [line for line in lines if line]
            return [quote([BR.join(lines)])] if lines else []
        if has_class(tag, "box_bg_dark"):
            inner = self.blocks(tag)
            return [quote(inner)] if inner else []
        if has_class(tag, "twitter-tweet-rendered"):
            iframe = tag.find("iframe")
            return [self.embed_link(iframe)] if iframe else []
        if has_class(tag, "wp-video", "mejs-container"):
            return [self.video_link(v) for v in tag.find_all("video")]
        # ---- HTML 要素 ----
        if name in ("h1", "h2", "h3"):
            return self.heading(2, tag)
        if name == "h4":
            return self.heading(3, tag)
        if name in ("h5", "h6"):
            return self.heading(4, tag)
        if name == "p":
            if self.contains_block(tag):
                return self.blocks(tag)
            text = self.paragraph(tag)
            return [text] if text else []
        if name == "blockquote":
            inner = self.blocks(tag)
            return [quote(inner)] if inner else []
        if name in ("ul", "ol"):
            if has_class(tag, "list-ttl"):
                return self.copyright_lines(tag)
            return self.list_block(tag)
        if name == "dl":
            return self.dl_block(tag)
        if name == "table":
            return self.table(tag)
        if name == "hr":
            return ["---"]
        if name == "img":
            md = self.image(tag)
            return [md] if md else []
        if name == "iframe":
            md = self.embed_link(tag)
            return [md] if md else []
        if name in ("video", "audio"):
            md = self.video_link(tag)
            return [md] if md else []
        if name == "pre":
            code = tag.get_text()
            return [f"```text\n{code.strip(chr(10))}\n```"]
        if name in INLINE_TAGS:
            # ブロック要素を含むインライン要素（<a><div>…</div></a> など）
            if name == "a" and tag.get("href"):
                inner = self.blocks(tag)
                href = urljoin(self.page_url, tag["href"].strip())
                return inner + [
                    f"[{escape_text(href)}]({link_dest(self.link_target(href))})"
                ]
            return self.blocks(tag)
        return self.blocks(tag)

    def image_info(self, tag: Tag) -> list[str]:
        """画像ブロック。画像を並べ、空でないキャプションを引用にする。"""
        imgs = tag.find_all("img")
        caps = tag.find_all("blockquote")
        out: list[str] = []
        if has_class(tag, "style_3") and len(caps) == len(imgs):
            # 画像ごとにキャプションが付く形式は、画像とキャプションを交互に並べる
            for img, cap in zip(imgs, caps, strict=True):
                out.append(self.image(img))
                inner = self.blocks(cap)
                if inner:
                    out.append(CAPTION_MARK + quote(inner))
            return [b for b in out if b]
        for img in imgs:
            out.append(self.image(img))
        for cap in caps:
            inner = self.blocks(cap)
            if inner:
                out.append(CAPTION_MARK + quote(inner))
        return [b for b in out if b]

    def list_info(self, tag: Tag) -> list[str]:
        """注釈・人物紹介（dl）。dt と後続の dd を 1 つの引用ブロックにする。"""
        out: list[str] = []
        for child in tag.children:
            if isinstance(child, Tag) and child.name == "dl":
                out.extend(self.dl_block(child, as_quote=True))
            elif isinstance(child, Tag):
                out.extend(self.block(child))
            elif isinstance(child, NavigableString) and child.strip():
                text = clean_inline(self.inline_node(child))
                if text:
                    out.append(text)
        return out

    def dl_block(self, dl: Tag, as_quote: bool = False) -> list[str]:
        groups: list[tuple[str, list[str]]] = []
        for child in dl.find_all(["dt", "dd"], recursive=False):
            if child.name == "dt":
                term = clean_inline(self.emphasis("**", child, False, False))
                groups.append((term, []))
            else:
                if not groups or (groups[-1][1] and not groups[-1][0]):
                    groups.append(("", []))
                groups[-1][1].extend(self.blocks(child))
        out: list[str] = []
        for term, body in groups:
            if term:
                # 見出し語のある項目（人物紹介、注意事項、囲み記事など）は、
                # 太字の見出し語と説明の段落にする
                out += [term, *body]
            elif body:
                # 説明だけの項目（※ の注釈）は、本文と区別するため引用ブロックにする
                out.append(quote(body) if as_quote else "\n\n".join(body))
        return out

    def copyright_lines(self, tag: Tag) -> list[str]:
        """著作権表示（ul.list-ttl）。リストにせず、行末の「\\」で改行した 1 段落にする。"""
        lines = []
        for li in tag.find_all("li", recursive=False):
            text = clean_inline(self.inline(li))
            if text:
                lines.append(text.replace(BR, HARD_BREAK))
        return [HARD_BREAK.join(lines)] if lines else []

    def list_block(self, tag: Tag) -> list[str]:
        ordered = tag.name == "ol"
        items: list[str] = []
        for n, li in enumerate(tag.find_all("li", recursive=False), start=1):
            inner = self.blocks(li)
            if not inner:
                continue
            marker = f"{n}. " if ordered else "- "
            body = "\n\n".join(inner)
            items.append(marker + indent(body, " " * len(marker)).lstrip(" "))
        if not items:
            return []
        loose = any("\n\n" in item for item in items)
        return [("\n\n" if loose else "\n").join(items)]

    def table(self, tag: Tag) -> list[str]:
        rows: list[list[str]] = []
        for tr in tag.find_all("tr"):
            if tr.find_parent("table") is not tag:
                continue
            cells = []
            for cell in tr.find_all(["th", "td"], recursive=False):
                text = clean_inline(self.inline(cell))
                # 表のセルの中では段落を分けられないので、改行は空白にする
                text = text.replace(BR, " ").replace("\n", " ")
                cells.append(text.replace("|", "\\|") if "\\|" not in text else text)
            if cells:
                rows.append(cells)
        if not rows:
            return []
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        lines = ["| " + " | ".join(rows[0]) + " |", "|" + " --- |" * width]
        lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
        lines = [re.sub(r"\|  \|", "| |", line) for line in lines]
        lines = [re.sub(r"\|  \|", "| |", line) for line in lines]  # 連続する空セル
        return ["\n".join(lines)]


# ---- ページ種別ごとの変換 ------------------------------------------------------
def load_html(path: Path) -> BeautifulSoup:
    return BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")


def main_area(soup: BeautifulSoup) -> Tag:
    """本文を探す範囲。アイキャッチ（div.banner_image）は div.main の外にあるため body 全体とし、
    サイドバーは呼び出し側で除外する。"""
    return soup.body or soup


def post_title(soup: BeautifulSoup, fallback: str) -> str:
    tag = soup.find("title")
    title = collapse_ws(tag.get_text()).strip() if tag else ""
    title = TITLE_SUFFIX_RE.sub("", title)
    return title or fallback


def check_sections(soup: BeautifulSoup, label: str, logs: list) -> None:
    """div.content / div.detail の直下に未知の要素がないか確認する。"""
    area = main_area(soup)
    for parent in area.select("div.content, div.detail"):
        if parent.find_parent(class_="sidebar"):
            continue
        for child in parent.find_all(True, recursive=False):
            cls = set(classes(child))
            if cls & (KNOWN_INCLUDED_SECTIONS | KNOWN_EXCLUDED_SECTIONS):
                continue
            empty = child.name in SKIP_TAGS or not child.get_text(strip=True)
            if empty and not child.find("img"):
                continue
            logs.append(
                (
                    logging.WARNING,
                    f"未知の要素を出力していません: {label} <{child.name} class={sorted(cls)}>",
                )
            )


def text_in(tag: Tag) -> str:
    return collapse_ws(tag.get_text(" ")).strip()


def render_header(soup: BeautifulSoup, r: Renderer, title: str) -> tuple[dict, list]:
    """記事ページのヘッダー（タイトル周り・リード・クレジット・タグ）を変換する。"""
    area = main_area(soup)
    boxes = [
        b for b in area.select("div.box_banner") if not b.find_parent(class_="sidebar")
    ]
    meta: dict = {"category": "", "tags": []}
    out: list[str] = []
    box = boxes[0] if boxes else None

    if box is not None:
        cat = box.select_one("span.cat")
        meta["category"] = text_in(cat) if cat else ""
        meta["tags"] = [text_in(a) for a in box.select("ul.list-tag a") if text_in(a)]

        # 画面上の見出しのうち、タイトルに含まれない部分を残す
        norm_title = normalize_for_check(title)
        for head in box.select(".title h2, h4.ttl"):
            text = clean_inline(r.inline(head))
            if text and normalize_for_check(text) not in norm_title:
                out.append(wrap_emphasis("**", text))

    # アイキャッチ（topics などは banner_image、Febri TALK はカタリベの写真）
    eyecatch: list[Tag] = []
    banner = area.select_one("div.banner_image")
    if banner is not None:
        eyecatch += banner.find_all("img", recursive=False)
    if box is not None:
        eyecatch += box.select(".box_banner_head_image img")
    seen: set[str] = set()
    for img in eyecatch:
        md = r.image(img)
        if md and md not in seen:
            seen.add(md)
            out.append(md)

    if box is not None:
        des = box.select_one(".box_banner_content .des")
        if des is not None:
            out += r.blocks(des)
        for foot in box.select(".box_banner_content p.foot"):
            text = clean_inline(r.inline(foot))
            if text:
                out.append(wrap_emphasis("*", text))
    return meta, out


def render_profile(profile: Tag, r: Renderer) -> list[str]:
    """Febri TALK のカタリベプロフィール。"""
    out: list[str] = []
    ttl = profile.select_one(".ttl-box")
    out.append(f"## {text_in(ttl)}" if ttl and text_in(ttl) else "## Profile")
    for img in profile.select(".info-image img"):
        md = r.image(img)
        if md:
            out.append(md)
    name = profile.select_one(".info-content .title h4")
    role = profile.select_one(".info-content .title p")
    lines = []
    if name is not None and text_in(name):
        lines.append(wrap_emphasis("**", clean_inline(r.inline(name))))
    if role is not None and text_in(role):
        lines.append(clean_inline(r.inline(role)))
    if lines:
        out.append(BR.join(lines))
    for sel in (".info-content .description", ".info-footer"):
        for el in profile.select(sel):
            out += r.blocks(el)
    return out


def render_series_description(desc: Tag, r: Renderer) -> list[str]:
    """作品情報・書籍情報。"""
    out: list[str] = []
    cat = desc.select_one(".article_cat")
    if cat is not None and text_in(cat):
        out.append(f"## {text_in(cat)}")
    for child in desc.find_all(True, recursive=False):
        if child is cat:
            continue
        out += r.block(child)
    # 作品情報の中の改行は、段落を分けずに行末の「\」で表す
    return [drop_trailing_breaks(b).replace(BR, HARD_BREAK) for b in out]


def render_article(entry: Entry, pages: list[Path], res: Result) -> tuple[str, dict]:
    soups = [load_html(p) for p in pages]
    first = soups[0]
    title = post_title(first, entry.title)
    renderers = [
        Renderer(
            p.parent,
            page_url(entry.url, n),
            "" if n == 1 else f"{n}/",
            url_to_rel_dir(entry.url).as_posix(),
        )
        for n, p in enumerate(pages, start=1)
    ]
    meta, blocks = render_header(first, renderers[0], title)
    blocks.insert(0, f"# {title}")

    for n, (soup, r) in enumerate(zip(soups, renderers, strict=True), start=1):
        body = main_area(soup).select_one("#blocks_general_field")
        if n > 1:
            blocks.append(f"<!-- page {n} -->")
        if body is None:
            r.warn(f"本文（#blocks_general_field）がありません: {pages[n - 1]}")
            continue
        blocks += r.blocks(body)

    # 作品情報・書籍情報は各ページに同じものが入っているので 1 回だけ出す
    seen_desc: set[str] = set()
    for soup, r in zip(soups, renderers, strict=True):
        for desc in main_area(soup).select("div.series_list__description"):
            key = str(desc)
            if key in seen_desc:
                continue
            seen_desc.add(key)
            blocks += render_series_description(desc, r)

    for profile in main_area(first).select("div.box-profile"):
        blocks += render_profile(profile, renderers[0])

    meta["title"] = title
    return finish(blocks), {"meta": meta, "soups": soups, "renderers": renderers}


def render_list_page(entry: Entry, pages: list[Path], res: Result) -> tuple[str, dict]:
    """一覧ページ（エンサイクロペディアの各行、カタリベリスト）。"""
    soups = [load_html(p) for p in pages]
    renderers = [
        Renderer(
            p.parent,
            page_url(entry.url, n),
            "" if n == 1 else f"{n}/",
            url_to_rel_dir(entry.url).as_posix(),
        )
        for n, p in enumerate(pages, start=1)
    ]
    title = entry.title or post_title(soups[0], "")
    blocks = [f"# {title}"]
    for n, (soup, r) in enumerate(zip(soups, renderers, strict=True), start=1):
        if n > 1:
            blocks.append(f"<!-- page {n} -->")
        area = main_area(soup)
        head = area.select_one("div.head-content h1")
        if head is not None and normalize_for_check(
            text_in(head)
        ) not in normalize_for_check(title):
            blocks.append(wrap_emphasis("**", clean_inline(r.inline(head))))
        for lst in area.select("div.list_filter"):
            for item in lst.find_all("div", class_="filter_item", recursive=False):
                blocks += render_filter_item(item, r)
    return finish(blocks), {
        "meta": {"title": title, "category": "", "tags": []},
        "soups": soups,
        "renderers": renderers,
    }


def render_filter_item(item: Tag, r: Renderer) -> list[str]:
    out: list[str] = []
    talk_head = item.select_one(".filter_item_head")
    if talk_head is not None:
        # カタリベリスト: 名前・肩書き・写真・紹介文・記事リンク
        name = talk_head.select_one(".title h4")
        sub = talk_head.select_one(".title .sub_title")
        if name is not None:
            out += r.heading(2, name)
        if sub is not None and text_in(sub):
            out.append(clean_inline(r.inline(sub)))
        seen: set[str] = set()
        for img in item.select(".flex_item img"):
            md = r.image(img)
            if md and md not in seen:
                seen.add(md)
                out.append(md)
        for desc in item.select(".filter_item_description"):
            out += r.blocks(desc)
        for foot in item.select(".filter_item_footer"):
            out += r.blocks(foot)
        return out
    # エンサイクロペディア: 項目名【分類】と解説
    title = item.find("div", class_="title", recursive=False)
    if title is not None:
        h4 = title.find("h4")
        out += r.heading(2, h4 if h4 is not None else title)
    for child in item.find_all(True, recursive=False):
        if child is not title:
            out += r.block(child)
    return out


def render_series_index(
    entry: Entry, pages: list[Path], res: Result
) -> tuple[str, dict]:
    """シリーズのトップページ（エンサイクロペディアの目次）。"""
    soup = load_html(pages[0])
    r = Renderer(pages[0].parent, entry.url, "", url_to_rel_dir(entry.url).as_posix())
    area = main_area(soup)
    title = entry.title or post_title(soup, "")
    blocks = [f"# {title}"]
    for banner in area.select("div.banner_series"):
        blocks += r.blocks(banner)
    for sl in area.select("div.series_list"):
        if has_class(sl, "series_list__description"):
            blocks += render_series_description(sl, r)
            continue
        content = sl.select_one(".series_list_content")
        if content is not None:
            for child in content.find_all(True, recursive=False):
                if has_class(child, "title") and normalize_for_check(
                    text_in(child)
                ) in normalize_for_check(title):
                    continue
                blocks += r.block(child)
        for box in sl.select(".list .box_list"):
            blocks += render_box_list(box, r)
    return finish(blocks), {
        "meta": {"title": title, "category": "", "tags": []},
        "soups": [soup],
        "renderers": [r],
    }


def render_box_list(box: Tag, r: Renderer) -> list[str]:
    out: list[str] = []
    a = box.find("a", href=True)
    href = urljoin(r.page_url, a["href"].strip()) if a else ""
    h4 = box.select_one(".box_list_title h4")
    if h4 is not None:
        label = clean_inline(r.inline(h4, in_link=True))
        dest = link_dest(r.link_target(href)) if href else ""
        out.append(f"## [{label}]({dest})" if href else f"## {label}")
    for img in box.select(".box_list_img img"):
        md = r.image(img)
        if md:
            out.append(md)
    for sel in (".box_list_description", ".box_list_footer"):
        for el in box.select(sel):
            if sel == ".box_list_footer":
                parts = [
                    clean_inline(r.inline(s))
                    for s in el.find_all(True, recursive=False)
                ]
                text = "　".join(p for p in parts if p)
                if text:
                    out.append(text)
            else:
                out += r.blocks(el)
    return out


def strip_br_edges(text: str) -> str:
    text = text.strip(" \n")
    while text.startswith("<br>"):
        text = text[len("<br>") :].lstrip(" \n")
    while text.endswith("<br>"):
        text = text[: -len("<br>")].rstrip(" \n")
    return text


def expand_hr_marks(block: str) -> list[str]:
    """エンドマークの位置で段落を分け、水平線のブロックを挟む。"""
    if HR_MARK not in block:
        return [block]
    if block.startswith((">", "- ", "|", "#")) or re.match(r"\d+\. ", block):
        # 引用・リスト・表・見出しの中は分けられないので、ブロックの後ろに置く
        rest = strip_br_edges(block.replace(HR_MARK, ""))
        return ([rest] if rest else []) + ["---"]
    out: list[str] = []
    for i, part in enumerate(block.split(HR_MARK)):
        if i:
            out.append("---")
        part = strip_br_edges(part)
        if part:
            lines = part.split("\n")
            out.append("\n".join([escape_line_start(lines[0]), *lines[1:]]))
    return out


def is_image_block(block: str) -> bool:
    return bool(IMAGE_ONLY_RE.fullmatch(block))


def move_hr_after_images(blocks: list[str]) -> list[str]:
    """エンドマークの水平線の直後に画像が続く場合、水平線をその画像（とキャプション）の後ろに移す。"""
    out = list(blocks)
    i = 0
    while i < len(out):
        if out[i] == "---" and i + 1 < len(out) and is_image_block(out[i + 1]):
            j = i + 1
            while j < len(out) and (
                is_image_block(out[j]) or out[j].startswith(CAPTION_MARK)
            ):
                j += 1
            out[i:j] = [*out[i + 1 : j], "---"]
            i = j
        i += 1
    return out


def drop_trailing_breaks(block: str) -> str:
    """段落の末尾（次の行が空行・引用の空行・ブロックの終わり）にある <br> を取り除く。"""
    lines = block.split("\n")
    for i, line in enumerate(lines):
        if not line.endswith("<br>"):
            continue
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if not nxt.replace(">", "").strip():
            lines[i] = line[: -len("<br>")].rstrip(" ")
    return "\n".join(lines)


def split_paragraphs(block: str) -> str:
    """改行（<br>）の位置で段落を分ける。引用やリストの中では、その行頭記号・字下げを引き継ぐ。"""

    def repl(m: re.Match) -> str:
        prefix = m.group(1)
        return f"\n{prefix.rstrip()}\n{prefix}"

    text = re.sub(r"<br>\n((?:> )*(?: {2,})?)", repl, drop_trailing_breaks(block))
    # 段落を分けたことで生じる行末の空白・空白だけの段落・連続する空行を取り除く。
    # 元の <br><br> による空行は、段落の区切りで表される
    out: list[str] = []
    for line in text.split("\n"):
        m = re.match(r"((?:> ?)*)(.*)$", line)
        quote_prefix, rest = m.group(1), m.group(2)
        rest = rest.rstrip(" \u3000")
        if not rest.strip(" \u3000"):
            line = quote_prefix.replace(" ", "")
        elif quote_prefix:
            line = quote_prefix + rest
        else:
            line = line.rstrip(" \u3000")
        if not line.replace(">", "") and out and out[-1] == line:
            continue
        out.append(line)
    while out and not out[0].replace(">", ""):
        out.pop(0)
    while out and not out[-1].replace(">", ""):
        out.pop()
    return "\n".join(out)


def finish(blocks: list[str]) -> str:
    expanded: list[str] = []
    for b in blocks:
        if b and b.strip():
            expanded += expand_hr_marks(b.strip("\n"))
    expanded = move_hr_after_images(expanded)
    expanded = [b.replace(CAPTION_MARK, "") for b in expanded]
    out: list[str] = []
    list_re = re.compile(r"^(- |\d+\. )")
    for block in expanded:
        # 隣り合うリストは Markdown では 1 つにつながるため、コメントで区切る
        if out and list_re.match(out[-1]) and list_re.match(block):
            out.append("<!-- -->")
        # 水平線が続く場合は 1 本にする
        if out and block == "---" and out[-1] == "---":
            continue
        out.append(block)
    text = "\n\n".join(split_paragraphs(b) for b in out)
    return re.sub(r"\n{3,}", "\n\n", text) + "\n"


def page_url(url: str, n: int) -> str:
    base = url if url.endswith("/") else url + "/"
    return base if n == 1 else f"{base}{n}/"


def yaml_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def front_matter(entry: Entry, meta: dict, pages: int) -> str:
    lines = ["---", f"title: {yaml_str(meta['title'])}", f"url: {yaml_str(entry.url)}"]
    if meta.get("category"):
        lines.append(f"category: {yaml_str(meta['category'])}")
    if entry.datetime:
        lines.append(f"published: {yaml_str(entry.datetime)}")
    if meta.get("tags"):
        lines.append("tags:")
        lines += [f"  - {yaml_str(t)}" for t in meta["tags"]]
    if pages > 1:
        lines.append(f"pages: {pages}")
    if entry.video_url:
        lines.append(f"video_url: {yaml_str(entry.video_url)}")
    lines.append("---")
    return "\n".join(lines) + "\n\n"


# ---- 取りこぼし検査 ------------------------------------------------------------
def check_roots(soup: BeautifulSoup, kind: str) -> list[Tag]:
    """検査対象（本文として出力すべき）領域を返す。"""
    area = main_area(soup)
    if kind == "article":
        sels = [
            "div.box_banner",
            "div.banner_image",
            "#blocks_general_field",
            "div.series_list__description",
            "div.box-profile",
        ]
    elif kind == "list":
        sels = ["div.head-content", "div.list_filter"]
    else:
        sels = ["div.banner_series", "div.series_list"]
    roots: list[Tag] = []
    for sel in sels:
        for el in area.select(sel):
            if el.find_parent(class_="sidebar"):
                continue
            if any(el in root.descendants for root in roots):
                continue
            roots.append(el)
    return roots


def verify(kind: str, md: str, ctx: dict, res: Result, label: str) -> None:
    norm_md = normalize_for_check(md)
    copied = {p for r in ctx["renderers"] for p in r.images.values()}
    copied_by_name: dict[str, list[Path]] = {}
    for p in copied:
        copied_by_name.setdefault(p.name, []).append(p)

    def image_output(local: Path) -> bool:
        # 2 ページ目以降のヘッダー・作品情報は 1 ページ目と同じなので 1 回だけ出力している。
        # 同名・同内容の画像が出力済みなら出力されたものとみなす。
        if local in copied:
            return True
        return any(
            filecmp.cmp(local, p, shallow=False)
            for p in copied_by_name.get(local.name, [])
        )

    missing_texts: list[str] = []
    for n, (soup, r) in enumerate(
        zip(ctx["soups"], ctx["renderers"], strict=True), start=1
    ):
        for root in check_roots(soup, kind):
            excluded = {id(e) for sel in EXCLUDE_IN_CONTENT for e in root.select(sel)}
            excluded |= {id(e) for e in root.find_all(SKIP_TAGS)}

            def skipped(node, root=root, excluded=excluded) -> bool:
                for parent in node.parents:
                    if id(parent) in excluded:
                        return True
                    if parent is root:
                        return False
                return False

            for s in root.find_all(string=True):
                if isinstance(s, Comment) or type(s) is not NavigableString:
                    continue
                norm = normalize_for_check(str(s))
                if not norm or norm in norm_md or skipped(s):
                    continue
                missing_texts.append(f"p{n}: {collapse_ws(str(s)).strip()[:60]}")
            for img in root.find_all("img"):
                if skipped(img) or not (img.get("src") or "").strip():
                    continue
                if is_endmark(img):
                    if "\n---\n" not in md:
                        res.missing_images += 1
                        res.logs.append(
                            (
                                logging.WARNING,
                                f"エンドマークの水平線が出力されていません: {label} p{n}",
                            )
                        )
                    continue
                ref = r.resolve_image(img)
                if ref is None or (ref[1] is not None and not image_output(ref[1])):
                    res.missing_images += 1
                    res.logs.append(
                        (
                            logging.WARNING,
                            f"画像が出力されていません: {label} p{n} {img.get('src')}",
                        )
                    )
    res.missing_texts += len(missing_texts)
    for text in missing_texts[:20]:
        res.logs.append(
            (logging.WARNING, f"本文テキストが出力されていません: {label} {text}")
        )
    if len(missing_texts) > 20:
        res.logs.append(
            (
                logging.WARNING,
                f"本文テキストの欠落 ほか {len(missing_texts) - 20} 件: {label}",
            )
        )


# ---- 記事単位の処理 -----------------------------------------------------------
def find_pages(base_dir: Path) -> list[Path]:
    """1 ページ目と、数字のサブディレクトリにある 2 ページ目以降の HTML を返す。"""
    pages = [base_dir / "index.html"]
    subs = sorted(
        (int(d.name), d / "index.html")
        for d in base_dir.iterdir()
        if d.is_dir()
        and d.name.isdigit()
        and int(d.name) >= 2
        and (d / "index.html").is_file()
    )
    return pages + [p for _, p in subs]


def detect_kind(soup: BeautifulSoup) -> str:
    area = main_area(soup)
    if area.select_one("#blocks_general_field"):
        return "article"
    if area.select_one("div.list_filter"):
        return "list"
    if area.select_one("div.banner_series"):
        return "series_index"
    return "unknown"


def copy_image(src: Path, dest: Path) -> bool:
    """画像をコピーする。同じサイズ・更新時刻のファイルがあればスキップして False を返す。"""
    if dest.is_file():
        s, d = src.stat(), dest.stat()
        if s.st_size == d.st_size and int(s.st_mtime) == int(d.st_mtime):
            return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return True


def convert_entry(entry: Entry, source_dir: Path, output_dir: Path) -> Result:
    res = Result(url=entry.url)
    rel = url_to_rel_dir(entry.url)
    base_dir = source_dir / rel
    label = str(rel)
    try:
        if not (base_dir / "index.html").is_file():
            res.logs.append(
                (logging.ERROR, f"HTML がありません: {base_dir / 'index.html'}")
            )
            return res
        pages = find_pages(base_dir)
        res.pages = len(pages)
        if len(pages) != entry.pages:
            res.logs.append(
                (
                    logging.WARNING,
                    f"ページ数が CSV（{entry.pages}）と保存済み HTML（{len(pages)}）で異なります: {label}",
                )
            )
        first = load_html(pages[0])
        kind = detect_kind(first)
        renderers = {
            "article": render_article,
            "list": render_list_page,
            "series_index": render_series_index,
        }
        if kind not in renderers:
            res.logs.append((logging.ERROR, f"ページの形式を判別できません: {label}"))
            return res
        del first
        body, ctx = renderers[kind](entry, pages, res)
        for soup, page in zip(ctx["soups"], pages, strict=True):
            check_sections(soup, str(page.relative_to(source_dir)), res.logs)
        md = front_matter(entry, ctx["meta"], len(pages)) + body
        res.internal_links = len(
            re.findall(r"\]\(<?(?:\.\./)*[^)>:]*index\.md>?\)", body)
        )
        verify(kind, md, ctx, res, label)

        out_dir = output_dir / rel
        out_dir.mkdir(parents=True, exist_ok=True)
        for r in ctx["renderers"]:
            res.empty_images += r.empty_images
            res.endmarks += r.endmarks
            res.unresolved_links += r.unresolved_links
            res.logs += r.logs
            for md_path, src in r.images.items():
                if copy_image(src, out_dir / md_path):
                    res.images_copied += 1
                else:
                    res.images_skipped += 1
        md_file = out_dir / "index.md"
        if not md_file.is_file() or md_file.read_text(encoding="utf-8") != md:
            md_file.write_text(md, encoding="utf-8")
        res.md_path = str(md_file)
    except Exception as e:
        res.logs.append(
            (logging.ERROR, f"変換に失敗しました: {label} {type(e).__name__}: {e}")
        )
    return res


# ---- エントリポイント ----------------------------------------------------------
def collect_entries(
    csv_files: list[Path], urls: set[str], quiet: bool = False
) -> list[Entry]:
    entries: list[Entry] = []
    seen: set[str] = set()
    for csv_path in csv_files:
        with csv_path.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                url = (row.get("url") or "").strip()
                if not url.startswith("http") or not is_febri_url(url):
                    if url and not quiet:
                        logger.warning("SKIP (non-febri host) %s", url)
                    continue
                if urls and url not in urls:
                    continue
                if url in seen:
                    if not quiet:
                        logger.warning("SKIP (CSV 内で重複) %s %s", csv_path.name, url)
                    continue
                seen.add(url)
                try:
                    pages = max(1, int(row.get("pages") or 1))
                except ValueError:
                    pages = 1
                entries.append(
                    Entry(
                        csv_name=csv_path.name,
                        url=url,
                        title=collapse_ws(row.get("title") or "").strip(),
                        datetime=(row.get("datetime") or "").strip(),
                        pages=pages,
                        video_url=(row.get("video_url") or "").strip(),
                    )
                )
    return entries


def run(
    entries: list[Entry], output_dir: Path, workers: int, link_map: dict[str, str]
) -> int:
    totals = Result(url="")
    converted = failed = 0
    unresolved: dict[str, int] = {}
    with ProcessPoolExecutor(
        max_workers=workers, initializer=init_worker, initargs=(link_map,)
    ) as ex:
        futures = ex.map(
            convert_entry,
            entries,
            [SOURCE_DIR] * len(entries),
            [output_dir] * len(entries),
            chunksize=4,
        )
        for i, res in enumerate(futures, start=1):
            for level, msg in res.logs:
                logger.log(level, msg)
            if res.md_path:
                converted += 1
                logger.debug("OK %s → %s", res.url, res.md_path)
            else:
                failed += 1
            totals.pages += res.pages
            totals.images_copied += res.images_copied
            totals.images_skipped += res.images_skipped
            totals.empty_images += res.empty_images
            totals.missing_texts += res.missing_texts
            totals.missing_images += res.missing_images
            totals.endmarks += res.endmarks
            totals.internal_links += res.internal_links
            for url in res.unresolved_links:
                unresolved[url] = unresolved.get(url, 0) + 1
            if i % 100 == 0:
                logger.info("進捗 %d / %d", i, len(entries))
    logger.info(
        "完了: 記事 %d 件（%d ページ）、失敗 %d 件 / 画像 コピー %d 件・変更なし %d 件・src 空 %d 件",
        converted,
        totals.pages,
        failed,
        totals.images_copied,
        totals.images_skipped,
        totals.empty_images,
    )
    for url, count in sorted(unresolved.items()):
        logger.debug(
            "変換対象外の febri.jp リンク（元の URL のまま）: %s ×%d", url, count
        )
    logger.info(
        "リンク: febri.jp 内リンクを相対リンクに %d 件、変換対象外で元の URL のまま %d 件（%d 種類、--verbose で一覧）/ エンドマーク→水平線 %d 件",
        totals.internal_links,
        sum(unresolved.values()),
        len(unresolved),
        totals.endmarks,
    )
    logger.info(
        "検査: 欠落テキスト %d 件、欠落画像 %d 件",
        totals.missing_texts,
        totals.missing_images,
    )
    return 1 if failed or totals.missing_texts or totals.missing_images else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--list",
        nargs="+",
        metavar="CSV",
        help="処理する CSV ファイル名（list/ からの相対パス）。省略時は全 CSV",
    )
    parser.add_argument(
        "--url",
        nargs="+",
        metavar="URL",
        help="指定した URL の記事のみ処理する（CSV に記載されている必要がある）",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=OUTPUT_DIR,
        help=f"出力先ディレクトリ（デフォルト: {OUTPUT_DIR}）",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=os.cpu_count() or 1,
        help="並列プロセス数（デフォルト: CPU 数）",
    )
    parser.add_argument("--verbose", action="store_true", help="DEBUG ログを出力する")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    log_file = SCRIPT_DIR / f"structurize_{datetime.datetime.now(tz=JST).date()}.log"
    logging.basicConfig(
        level=log_level,
        format=LOG_FORMAT,
        handlers=[
            logging.StreamHandler(sys.stderr),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )

    output_dir = args.output.resolve()
    source_dir = SOURCE_DIR.resolve()
    if output_dir == source_dir or output_dir.is_relative_to(source_dir):
        logger.error("出力先を source/ の中にはできません: %s", output_dir)
        sys.exit(1)

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

    entries = collect_entries(csv_files, set(args.url or []))
    # リンクの書き換え先は、今回処理しない記事も含めた全 CSV の記事から作る
    all_entries = collect_entries(sorted(LIST_DIR.glob("*.csv")), set(), quiet=True)
    link_map = build_link_map(all_entries, SOURCE_DIR)
    if args.url:
        found = {e.url for e in entries}
        for url in sorted(set(args.url) - found):
            logger.error("CSV に記載されていない URL です: %s", url)
    if not entries:
        logger.error("処理対象の記事がありません")
        sys.exit(1)

    logger.info("対象: 記事 %d 件 → %s", len(entries), output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    sys.exit(run(entries, output_dir, max(1, args.workers), link_map))


if __name__ == "__main__":
    main()
