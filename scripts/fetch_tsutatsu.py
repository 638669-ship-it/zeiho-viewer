"""国税庁サイトから法令解釈通達のHTMLを取得し data/raw_tsutatsu/ に保存する。

  python scripts/fetch_tsutatsu.py --list-only      # 目次のリンク列挙のみ（取得はしない）
  python scripts/fetch_tsutatsu.py                  # 未取得のページだけ取得
  python scripts/fetch_tsutatsu.py --force          # 全ページを取り直す
  python scripts/fetch_tsutatsu.py --only shotoku   # 通達を指定して部分取得

e-Gov法令API（fetch_laws.py）と違い、通達はAPIが無くHTMLスクレイピングのみ。
国税庁サーバーへの負荷配慮として直列取得・1秒スリープ・リトライ3回で行儀よく取得する。
robots.txt は 2026-07-16 に確認済み。Disallow は /service_publication/ 系のみで
/law/tsutatsu/ は対象外（取得可）。

文字コードの注意：同じサイト内でもページによって異なる。
  menu.htm            → UTF-8
  目次・本文（01.htm等）→ Shift_JIS
決め打ちすると片方が壊れるため、meta charset を見て判定する（detect_encoding）。

標準ライブラリ＋beautifulsoup4 で動作する。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw_tsutatsu"

# 国税庁のHTMLは <p> の閉じ忘れが多い。標準の html.parser は閉じ忘れを補正しないため、
# 評基通の目次では245本のリンクと30個の節見出しが1つの <p> に飲み込まれ、
# 階層も順序も壊れた（最大の <p> が6,041字）。html5lib はブラウザと同じHTML5の
# 解釈で構造を組み直すので、同じ <p> が56字になり、節見出しとリンクが正しく並ぶ。
HTML_PARSER = "html5lib"

ORIGIN = "https://www.nta.go.jp"
UA = "zeiho-viewer/0.2 (personal tax-law reference tool; contact via https://github.com/638669-ship-it/zeiho-viewer)"

# 指示書 4-3：国税庁サーバーへの負荷配慮（1リクエスト1秒以上の間隔）
SLEEP_SEC = 1.2
RETRY = 3
TIMEOUT = 120

# 「（令和8年3月31日付通達まで掲載）」の掲載時点は目次ではなく menu.htm に載っている。
MENU_URL = f"{ORIGIN}/law/tsutatsu/menu.htm"

# 通達ごとのアダプタ。active を True にすれば同じ処理で取得できる。
#
# numbering … 項目番号の体系。関連条文リンクの作り方が変わる。
#   "article" 条-枝番（所基通 36-1 ＝ 法第36条）。番号から関係法条を導ける。
#   "chapter" 章-節-連番（法基通 1-1-1）。番号は条を表さないので導けない。
#   "flat"    通し番号（評基通 1、4-2）。通達内で完結した番号で、条とは無関係。
# 番号から条を導けるのは "article" だけ。他は法令側からの「関連通達」リンクを出さない。
#
# min_segments … 項目番号の最小セグメント数。少なく見積もると本文中の数字を項目と
#   取り違えるので通達ごとに指定する。所基通 36-1＝2、法基通 1-1-1＝3だが最小は2、
#   評基通は「1」という裸の番号があるので1。
SOURCES = {
    "shotoku": {
        "name": "所得税基本通達",
        "abbr": "所基通",
        "parent": "shotoku_act",  # 親法令（docs/data/index.json の key）
        "toc_url": f"{ORIGIN}/law/tsutatsu/kihon/shotoku/01.htm",
        "base_path": "/law/tsutatsu/kihon/shotoku/",
        "numbering": "article",
        "min_segments": 2,
        "active": True,
    },
    "houjin": {
        "name": "法人税基本通達",
        "abbr": "法基通",
        "parent": "houjin_act",
        "toc_url": f"{ORIGIN}/law/tsutatsu/kihon/hojin/01.htm",
        "base_path": "/law/tsutatsu/kihon/hojin/",
        "numbering": "chapter",
        "min_segments": 2,
        "active": True,
    },
    "hyoka": {
        "name": "財産評価基本通達",
        "abbr": "評基通",
        "parent": "souzoku_act",
        "toc_url": f"{ORIGIN}/law/tsutatsu/kihon/sisan/hyoka_new/01.htm",
        "base_path": "/law/tsutatsu/kihon/sisan/hyoka_new/",
        # 評基通の 1・4-2 は通達内の通し番号。相続税法の条とは対応しない。
        "numbering": "flat",
        "min_segments": 1,
        "active": True,
    },
    "souzoku": {
        "name": "相続税法基本通達",
        "abbr": "相基通",
        "parent": "souzoku_act",
        "toc_url": f"{ORIGIN}/law/tsutatsu/kihon/sisan/sozoku2/01.htm",
        "base_path": "/law/tsutatsu/kihon/sisan/sozoku2/",
        # 相基通 9-1 ＝ 法第9条。所基通と同じ条-枝番。
        "numbering": "article",
        "min_segments": 2,
        "active": True,
    },
    "shohi": {
        "name": "消費税法基本通達",
        "abbr": "消基通",
        "parent": "shohi_act",
        "toc_url": f"{ORIGIN}/law/tsutatsu/kihon/shohi/01.htm",
        "base_path": "/law/tsutatsu/kihon/shohi/",
        # 消基通 1-1-1 は章-節-連番（法基通と同じ体系）。条とは対応しない。
        "numbering": "chapter",
        "min_segments": 2,
        "active": True,
    },
    "tsusoku": {
        "name": "国税通則法基本通達",
        "abbr": "通基通",
        "parent": "tsusoku_act",
        # 00.htm は前文で、目次は mokuji.htm という別ページ。
        "toc_url": f"{ORIGIN}/law/tsutatsu/kihon/tsusoku/mokuji.htm",
        # menu.htm が張っているのは 00.htm。掲載時点はそちらの行から拾う。
        "menu_link": f"{ORIGIN}/law/tsutatsu/kihon/tsusoku/00.htm",
        "base_path": "/law/tsutatsu/kihon/tsusoku/",
        # 通基通の番号は「第○条関係」ごとに1から振り直される（「1」が46か所にある）。
        # 番号だけでは項目を特定できないので、条関係の条番号と合成して 2-1 とする。
        "numbering": "article",
        "min_segments": 1,
        # 項目の置き場所が indent1・indent2・class無しに散らばっていて、深さでは
        # 見分けられない（実測 253/42/12）。確実なのは「先頭の太字が数字だけ」の形。
        "item_by_strong": True,
        "compose_kankei": True,
        "active": True,
    },
    # 措置法通達（所得税関係）は1本ではなく、入口の sotihou.htm に14本の通達が
    # 並んだ一覧表になっている。実務で引く主要4本を別々の通達として収録する
    # （残り10本は肉用牛の売却・社会保険診療報酬の範囲などの個別論点）。
    "sochi": {
        "name": "租税特別措置法関係通達（所得税）",
        "abbr": "措通",
        "parent": "sochi_act",
        "toc_url": f"{ORIGIN}/law/tsutatsu/kobetsu/shotoku/sochiho/801226/sinkoku/01.htm",
        "menu_link": f"{ORIGIN}/law/tsutatsu/kobetsu/shotoku/sochiho/sotihou.htm",
        "base_path": "/law/tsutatsu/kobetsu/shotoku/sochiho/801226/sinkoku/",
        "numbering": "article",
        "min_segments": 2,
        "active": True,
    },
    "sochi_gensen": {
        "name": "租税特別措置法関係通達（源泉所得税）",
        "abbr": "措通源泉",
        "parent": "sochi_act",
        "toc_url": f"{ORIGIN}/law/tsutatsu/kobetsu/shotoku/sochiho/880331/gensen/58/01.htm",
        "menu_link": f"{ORIGIN}/law/tsutatsu/kobetsu/shotoku/sochiho/sotihou.htm",
        "base_path": "/law/tsutatsu/kobetsu/shotoku/sochiho/880331/gensen/58/",
        "numbering": "article",
        "min_segments": 2,
        "active": True,
    },
    "sochi_kabushiki": {
        "name": "租税特別措置法関係通達（株式等に係る譲渡所得等）",
        "abbr": "措通株式",
        "parent": "sochi_act",
        "toc_url": f"{ORIGIN}/law/tsutatsu/kobetsu/shotoku/sochiho/020624/sanrin/01.htm",
        "menu_link": f"{ORIGIN}/law/tsutatsu/kobetsu/shotoku/sochiho/sotihou.htm",
        "base_path": "/law/tsutatsu/kobetsu/shotoku/sochiho/020624/sanrin/",
        "numbering": "article",
        "min_segments": 2,
        "active": True,
    },
    "sochi_jouto": {
        "name": "租税特別措置法関係通達（山林所得・譲渡所得）",
        "abbr": "措通譲渡",
        "parent": "sochi_act",
        "toc_url": f"{ORIGIN}/law/tsutatsu/kobetsu/shotoku/sochiho/710826/sanrin/sanjyou/01.htm",
        "menu_link": f"{ORIGIN}/law/tsutatsu/kobetsu/shotoku/sochiho/sotihou.htm",
        "base_path": "/law/tsutatsu/kobetsu/shotoku/sochiho/710826/sanrin/sanjyou/",
        "numbering": "article",
        "min_segments": 2,
        "active": True,
    },
    "choshu": {
        "name": "国税徴収法基本通達",
        "abbr": "徴基通",
        "parent": "choshu_act",
        "toc_url": f"{ORIGIN}/law/tsutatsu/kihon/chosyu/index.htm",
        "base_path": "/law/tsutatsu/kihon/chosyu/",
        # 通基通と同じ作り。「第2条関係」の下に 1・2… と振り直される番号が並ぶ。
        "numbering": "article",
        "min_segments": 1,
        "item_by_strong": True,
        "compose_kankei": True,
        "active": True,
    },
    # 印紙税法基本通達は他通達と作りが違う（実地調査 2026-07-16）。
    #   本文（inshi01〜04）… 逐条。各項目が <strong>第N条</strong> で始まり、
    #       項（2・3…）を strong の裸数字で持つ＝印紙税法の条とそのまま対応（印基通↔印法）。
    #   別表第一 課税物件表（betsu01）… 号文書ごと（第1号の1文書…）に 1・2… が振り直される。
    #   別表第二 重要な事項の一覧表（betsu02）… 一覧。
    # 目次は 00.htm ではなく mokuji.htm。逐条なので numbering="jo" の専用モードで解釈する。
    "inshi": {
        "name": "印紙税法基本通達",
        "abbr": "印基通",
        "parent": "inshi_act",
        "toc_url": f"{ORIGIN}/law/tsutatsu/kihon/inshi/mokuji.htm",
        "base_path": "/law/tsutatsu/kihon/inshi/",
        "numbering": "jo",
        "min_segments": 1,
        "item_by_jo": True,
        "active": True,
    },
}

# 基本通達本体以外（指示書 Step1-4：取得しない）。
# /20230930/ のような日付のディレクトリは旧版のアーカイブ（消基通の
# 「令和5年9月30日以前の通達」）。現行の本体ではないので対象外。
# 措通株式の 1273_1/ は「平成14年11月27日付改正以前のもの」の別冊＝旧版。
EXCLUDE_RE = re.compile(r"(/kaisei/|/joho/|shiryo|shushi|/\d{8}/|/1273_1/|\.pdf$)", re.I)

# /shared/ 配下の画像は図表ではなく、表外字や記号を字形画像で埋めたもの
#   hoten.gif      alt="ほてん" … 本文中の「補塡」（塡が表外字のため画像）
#   decimal/*.gif  alt="A"/"1"  … 算式中の記号
# これらは parse 側で文字に置き換えるので取得しない。取得するのは図表・算式のみ。
SHARED_IMG_RE = re.compile(r"^/shared/")


def detect_encoding(body: bytes, ctype: str | None) -> str:
    """HTTPヘッダ→meta charset の順で文字コードを判定する。既定は cp932。"""
    if ctype:
        m = re.search(r"charset=([\w-]+)", ctype, re.I)
        if m:
            return normalize_enc(m.group(1))
    head = body[:2048].decode("ascii", "replace")
    m = re.search(r'charset=["\']?([\w-]+)', head, re.I)
    if m:
        return normalize_enc(m.group(1))
    return "cp932"


def normalize_enc(name: str) -> str:
    n = name.strip().lower()
    # Shift_JIS 宣言でも実体は cp932（機種依存文字を含む）なので cp932 で読む
    if n in ("shift_jis", "shift-jis", "sjis", "x-sjis", "ms_kanji"):
        return "cp932"
    if n in ("utf-8", "utf8"):
        return "utf-8"
    return n


def fetch(url: str) -> tuple[bytes, str, int]:
    """1ページ取得して (生バイト列, 判定した文字コード, HTTPステータス) を返す。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
    last_err: Exception | None = None
    for attempt in range(1, RETRY + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                body = r.read()
                return body, detect_encoding(body, r.headers.get("Content-Type")), r.status
        except urllib.error.HTTPError as e:
            last_err = RuntimeError(f"HTTP {e.code}")
            if e.code < 500:  # 4xx はリトライしても無駄
                break
        except Exception as e:  # noqa: BLE001 - タイムアウト等
            last_err = e
        if attempt < RETRY:
            wait = SLEEP_SEC * (2**attempt)
            print(f"    retry {attempt}/{RETRY - 1} in {wait:.0f}s ({last_err})")
            time.sleep(wait)
    raise RuntimeError(f"取得失敗 {url}: {last_err}")


def decode(body: bytes, enc: str) -> str:
    return body.decode(enc, errors="replace")


def body_area(html: str) -> BeautifulSoup:
    """本文領域（div#bodyArea）だけを対象にする。サイドバー等の共通リンクを拾わないため。"""
    soup = BeautifulSoup(html, HTML_PARSER)
    area = soup.find(id="bodyArea")
    return area if area is not None else soup


# 掲載時点の言い回しは通達によって違う。
#   所基通 「（令和8年3月31日付通達まで掲載）」
#   他6本 「（令和8年1月30日付改正分まで更新）」
PUBLISHED_RE = re.compile(r"[（(]([^（）()]*まで(?:掲載|更新))[）)]")


def fetch_published_as_of(key: str, src: dict) -> str | None:
    """掲載時点（「令和8年3月31日付通達まで掲載」等）を拾う。

    基本通達は menu.htm の行に併記されている。措置法通達は menu.htm には
    まとめて1行しかなく、通達ごとの掲載時点は一覧表 sotihou.htm の行にある。
    そこで menu_link に一覧表を指定し、そのページから自分の目次への
    リンクを探して同じ行の文言を読む。
    """
    index_url = src.get("menu_link", MENU_URL)
    toc_path = urllib.parse.urlparse(src["toc_url"]).path

    for page_url, want in ((index_url, toc_path), (MENU_URL, urllib.parse.urlparse(index_url).path)):
        body, enc, _ = fetch(page_url)
        soup = BeautifulSoup(decode(body, enc), HTML_PARSER)
        for a in soup.find_all("a", href=True):
            if urllib.parse.urlparse(urllib.parse.urljoin(ORIGIN, a["href"])).path != want:
                continue
            # リンクを含む行（tr）やセルの文言に掲載時点が併記されている
            for scope in ("tr", "td", "li", "p"):
                cell = a.find_parent(scope)
                if cell is None:
                    continue
                m = PUBLISHED_RE.search(cell.get_text(" ", strip=True))
                if m:
                    return m.group(1)
        if index_url == MENU_URL:
            break
    return None


def collect_links(key: str, src: dict) -> tuple[list[dict], str]:
    """目次ページから本文ページのリンクを列挙する（アンカー除去・重複排除・出現順維持）。

    戻り値は (ページ一覧, 目次HTML)。ページ一覧の各要素は
    {"path": 相対パス, "url": 絶対URL, "anchors": [アンカー...], "labels": [リンク文字列...]}。
    """
    body, enc, _ = fetch(src["toc_url"])
    html = decode(body, enc)
    # 目次のファイル名は通達ごとに違う（01.htm / mokuji.htm / index.htm）。
    # 決め打ちすると本文と同じ名前で二重に保存され、ページ数が合わなくなる。
    save_raw(key, src["toc_url"].rsplit("/", 1)[-1], body)

    area = body_area(html)
    base = src["base_path"]
    # ここで外したいのは「目次が自分自身を指すリンク」だけ。menu_link（通基通では
    # 前文の 00.htm）と取り違えると、前文が落ちて目次が本文として入る。
    toc_path = urllib.parse.urlparse(src["toc_url"]).path

    pages: dict[str, dict] = {}
    for a in area.find_all("a", href=True):
        url = urllib.parse.urljoin(src["toc_url"], a["href"])
        parts = urllib.parse.urlparse(url)
        if parts.netloc not in ("", "www.nta.go.jp"):
            continue
        if not parts.path.startswith(base):
            continue  # 通達本体の配下だけ
        if parts.path == toc_path:
            continue  # 目次自身へのリンク（ページ内リンク）
        if EXCLUDE_RE.search(parts.path):
            continue  # 一部改正通達・PDF等
        if not parts.path.endswith((".htm", ".html")):
            continue

        rel = parts.path[len(base) :]
        page = pages.setdefault(rel, {"path": rel, "url": ORIGIN + parts.path, "anchors": [], "labels": []})
        if parts.fragment and parts.fragment not in page["anchors"]:
            page["anchors"].append(parts.fragment)
        label = a.get_text(" ", strip=True)
        if label and label not in page["labels"]:
            page["labels"].append(label)

    return list(pages.values()), html


def save_raw(key: str, rel: str, body: bytes) -> Path:
    out = RAW_DIR / key / rel
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(body)
    return out


def collect_image_urls(key: str, src: dict) -> list[str]:
    """保存済みの本文HTMLから、図表・算式の画像URL（ページ固有のもの）を集める。"""
    urls: list[str] = []
    base = src["base_path"]
    for page in sorted((RAW_DIR / key).rglob("*.htm")):
        html = decode(page.read_bytes(), "cp932")
        area = body_area(html)
        page_url = ORIGIN + base + str(page.relative_to(RAW_DIR / key)).replace("\\", "/")
        for im in area.find_all("img", src=True):
            path = urllib.parse.urlparse(urllib.parse.urljoin(page_url, im["src"])).path
            if SHARED_IMG_RE.match(path):
                continue  # 字形画像（parse 側で文字に置換）
            if path.startswith(base) and path not in urls:
                urls.append(path)
    return urls


def fetch_images(key: str, src: dict, args) -> int:
    """図表・算式の画像を data/raw_tsutatsu/{key}/_img/ に取得する。"""
    urls = collect_image_urls(key, src)
    print(f"\n図表・算式の画像: {len(urls)}件")
    base = src["base_path"]
    log_path = RAW_DIR / key / "_img_log.json"
    log = json.loads(log_path.read_text(encoding="utf-8")) if log_path.exists() else {}

    ok = skipped = failed = 0
    for i, path in enumerate(urls, 1):
        rel = path[len(base) :]
        out = RAW_DIR / key / "_img" / rel
        if out.exists() and not args.force:
            skipped += 1
            continue
        try:
            body, _, status = fetch(ORIGIN + path)
        except Exception as e:  # noqa: BLE001
            print(f"  [{i}/{len(urls)}] {rel} … !! {e}", file=sys.stderr)
            failed += 1
            time.sleep(SLEEP_SEC)
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(body)
        log[rel] = {
            "url": ORIGIN + path,
            "status": status,
            "bytes": len(body),
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        ok += 1
        time.sleep(SLEEP_SEC)
    log_path.write_text(
        json.dumps(log, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(f"画像: 取得 {ok} / skip {skipped} / 失敗 {failed}")
    return 1 if failed else 0


def run(key: str, src: dict, args) -> int:
    print(f"=== {src['name']}（{src['abbr']}）===")
    print(f"目次: {src['toc_url']}")

    pages, _ = collect_links(key, src)
    total_links = sum(max(1, len(p["anchors"])) for p in pages)
    print(f"\n目次リンク数（アンカー込み）: {total_links}")
    print(f"取得対象ページ数（アンカー除去・重複排除後）: {len(pages)}\n")
    print("--- 先頭10件 ---")
    for i, p in enumerate(pages[:10], 1):
        anc = f"  #{','.join(p['anchors'])}" if p["anchors"] else ""
        label = p["labels"][0] if p["labels"] else ""
        print(f"{i:2d}. {p['path']:<12}{anc}")
        print(f"    {label}")

    published = fetch_published_as_of(key, src)
    print(f"\n掲載時点（menu.htm より）: {published}")

    links_path = RAW_DIR / key / "_links.json"
    links_path.parent.mkdir(parents=True, exist_ok=True)
    links_path.write_text(
        json.dumps(
            {
                "key": key,
                "name": src["name"],
                "abbr": src["abbr"],
                "parent": src["parent"],
                "toc_url": src["toc_url"],
                "numbering": src["numbering"],
                "min_segments": src.get("min_segments", 2),
                "item_by_strong": src.get("item_by_strong", False),
                "compose_kankei": src.get("compose_kankei", False),
                "item_by_jo": src.get("item_by_jo", False),
                "published_as_of": published,
                "listed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "page_count": len(pages),
                "pages": pages,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"リンク一覧を保存: {links_path.relative_to(ROOT)}")

    if args.list_only:
        print("\n--list-only のため取得はここまで。")
        return 0

    log_path = RAW_DIR / key / "_fetch_log.json"
    log = json.loads(log_path.read_text(encoding="utf-8")) if log_path.exists() else {}

    ok = skipped = failed = 0
    print()
    for i, p in enumerate(pages, 1):
        out = RAW_DIR / key / p["path"]
        head = f"[{i}/{len(pages)}] {p['path']}"
        if out.exists() and not args.force:
            print(f"{head} … skip（取得済み）")
            skipped += 1
            continue
        try:
            body, enc, status = fetch(p["url"])
        except Exception as e:  # noqa: BLE001
            print(f"{head} … !! {e}", file=sys.stderr)
            failed += 1
            time.sleep(SLEEP_SEC)
            continue

        save_raw(key, p["path"], body)
        text = decode(body, enc)
        bad = text.count("�")
        log[p["path"]] = {
            "url": p["url"],
            "status": status,
            "bytes": len(body),
            "encoding": enc,
            "replacement_chars": bad,
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        warn = f"  !! 置換文字{bad}件" if bad else ""
        print(f"{head} … OK {len(body) / 1024:.1f}KB / {enc}{warn}")
        ok += 1
        time.sleep(SLEEP_SEC)

    log_path.write_text(
        json.dumps(log, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(f"\n完了: 取得 {ok} / skip {skipped} / 失敗 {failed}　→ {log_path.relative_to(ROOT)}")
    rc = 1 if failed else 0
    return rc | fetch_images(key, src, args)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list-only", action="store_true", help="目次のリンク列挙のみ（本文は取得しない）")
    ap.add_argument("--force", action="store_true", help="取得済みページも取り直す")
    ap.add_argument("--only", nargs="*", help="SOURCES の key を指定して部分取得")
    args = ap.parse_args()

    keys = [k for k, v in SOURCES.items() if v["active"]]
    if args.only:
        keys = [k for k in args.only if k in SOURCES]
        if not keys:
            print("該当する key がありません", file=sys.stderr)
            return 1

    rc = 0
    for key in keys:
        rc |= run(key, SOURCES[key], args)
    return rc


if __name__ == "__main__":
    sys.exit(main())
