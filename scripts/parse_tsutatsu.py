"""国税庁の通達HTML（data/raw_tsutatsu/）を、ビューアが読む階層JSONへ変換する。

  python scripts/parse_tsutatsu.py                 # 有効な通達すべて
  python scripts/parse_tsutatsu.py --only shotoku

出力（法令側 parse_laws.py と同じスキーマ・同じ置き場）:

  docs/data/
    shotoku_tsutatsu/toc.json      … 目次ツリー＋項目→チャンク索引＋法条→項目の対応表
    shotoku_tsutatsu/main-NN.json  … 通達項目の本体
    shotoku_tsutatsu/img/          … 図表・算式の画像
    index.json                     … 通達エントリを追記（法令側の生成物にマージする）

法令側の処理には手を入れない（指示書 0）。index.json は parse_laws.py が生成するため、
本スクリプトは既存の index.json を読んで通達エントリだけをマージして書き戻す。
そのため実行順は parse_laws.py → parse_tsutatsu.py。

原文（HTML）の実態と対処（実地調査 2026-07-16）:

 1. 項目番号の区切りは4種類が混在する。
      －(U+FF0D 1055件) / -(U+002D 266件) / ー(U+30FC 20件) / ―(U+2015 5件)
    さらに全角数字（38－７の２）も混じる。正規化してから解釈する。
 2. 番号の形は指示書の想定（36-1 / 36の2-1 / 2-47）より広い。実在する形:
      2-47, 36の2-1, 2-4の2, 23～35共-1（条範囲の共通項目）,
      36・37共-1（条並列）, 83から84の2-1（から形式の範囲）
 3. 章節のアンカーは <a name="a-01"> と <p id="a-03"> の2形式があり、
    <a name> は見出しの前にも後にも置かれる。どちらも節の境目として扱う。
 4. /shared/ 配下の画像は図表ではなく字形画像。文字に置き換える（IMG_TEXT）。
    hoten.gif は alt="ほてん" だが、前後の文字（を/の/付/差/り → する/さ/金）から
    画像の実体は「補塡」2文字。「補」が直前に来る例は全37件中ゼロで裏付けた。
 5. 附則（40/01.htm）は88本の改正通達附則の集合。項目番号を持たないため、
    改正通達1本を1リーフとして扱い、番号ジャンプの索引からは外す。
 6. 目次のアンカーは当てにならない。実測で10ページがページ内の実体とずれる
    （02/02.htm は目次が a-03 を2件に重複指定、07/01.htm は存在しない a-03〜a-07 を指す）。
    そこで階層の突合にアンカーは使わず、
      編・章・節・款・目 … 目次から（ページ単位で最初の出現位置に置く）
      節見出し           … 本文ページ自身の h1
      関係法条           … 項目番号そのもの（36-1 → 法第36条）
    とする。番号は項目に付いているので、目次側のリンク誤りの影響を受けない。

標準ライブラリ＋beautifulsoup4 で動作する。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup, Comment, Tag

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw_tsutatsu"
OUT_DIR = ROOT / "docs" / "data"

# 法令側（parse_laws.py）と同じ目安。gzip後で1チャンク約48KB。
CHUNK_BYTES = 350_000

SOURCE_NOTE = "出典：国税庁ホームページ 法令解釈通達を加工して表示。正確な内容は原文を確認してください。"

# 字形画像 → 置き換える文字。/shared/ 配下は図表ではなく表外字・記号の画像。
IMG_TEXT = {"hoten.gif": "補塡", "hoten_h2.gif": "補塡"}

ZEN2HAN = str.maketrans("０１２３４５６７８９", "0123456789")

# 項目番号の区切りに使われるダッシュ類（原文に4種類が混在する）。
# 文字クラス内でハイフンが範囲と解釈されないよう1文字ずつエスケープする。
DASH = "[" + "".join(re.escape(c) for c in "－-―ー−‐–—") + "]"
# 「の」による枝番は2段以上になる（49－1の3の2、67の2－2の2）。* で繰り返しを許す。
BRANCH = r"\d+(?:の\d+)*"
# 条部分：2 / 36の2 / 23～35 / 36・37 / 83から84の2 （末尾に「共」が付くことがある）
JO_PART = rf"{BRANCH}(?:(?:～|・|から){BRANCH})*共?"
ITEM_NUM_RE = re.compile(rf"^({JO_PART}){DASH}({BRANCH})(?![0-9])")

# 号列記の記号：(1) （1） （注） (注) （イ） / 裸のイロハ / 注の中の枝番号
IROHA = "イロハニホヘトチリヌルヲワカヨタレソツネナラムウヰノオクヤマケフコエテアサキユメミシヱヒモセス"
MARKER_RE = re.compile(
    rf"^([（(][^）)]{{1,6}}[）)]|[{IROHA}]|[①-⑳]|\d{{1,3}})[　\s]+(.*)$", re.S
)
MARKER_ONLY_RE = re.compile(rf"^([（(][^）)]{{1,6}}[）)])$")

# 「法第36条《収入金額》関係」→ 対応する所得税法の条番号
REL_JO_RE = re.compile(r"法第(\d+)条(?:の(\d+))?")

# 節見出しが「法第○条《…》関係」かどうか
KANKEI_RE = re.compile(r"^法第.*関係")

# 附則ページの h2 は「改正通達の名前」と「見出し」の両方に使われる。
# 改正通達名は「附則（…）」か元号（昭45直審（所）55）で始まる。見出しは「（施行期日）」。
FUSOKU_HEAD_RE = re.compile(r"^(附則|昭\d|平\d|令\d|昭和|平成|令和)")


def norm(s: str) -> str:
    """全角数字を半角へ。項目番号の判定を安定させる。"""
    return s.translate(ZEN2HAN)


def clean(s: str) -> str:
    """前後の空白・全角空白を落とす。"""
    return s.replace("　", " ").strip()


# ---------------------------------------------------------------- テキスト抽出


def el_text(el, figs: list) -> str:
    """要素配下の地の文。<img> は字形画像なら文字に、図表なら figs に退避して本文からは外す。"""
    out = []
    for node in el.descendants:
        if isinstance(node, Tag):
            if node.name == "img":
                src = node.get("src", "")
                alt = (node.get("alt") or "").strip()
                name = src.rsplit("/", 1)[-1]
                if "/shared/" in src:
                    # 表外字・記号の字形画像 → 文字に戻す
                    out.append(IMG_TEXT.get(name, alt))
                else:
                    figs.append({"src": src, "alt": alt})
            elif node.name == "br":
                out.append("\n")
        elif isinstance(node, Comment):
            continue  # <!-- InstanceEndEditable --> を本文に混ぜない
        elif node.parent is None or node.parent.name not in ("script", "style"):
            out.append(str(node))
    return "".join(out)


def para_texts(el, figs: list) -> list[str]:
    """1つの <p> を、<br> で区切って段落のリストにする。"""
    raw = el_text(el, figs)
    return [clean(x) for x in raw.split("\n") if clean(x)]


# ---------------------------------------------------------------- 番号の正規化


def item_num(text: str) -> tuple[str, str] | None:
    """段落の先頭から項目番号を読む。→ (正規化した番号, 残りの本文) / 該当なしなら None

    番号の判定には全角数字を半角に直した複製を使うが、本文はもとの文字のまま返す。
    正規化した文字列から本文を切り出すと、改正履歴の「令７課個2-10」が
    「令7課個2-10」に書き換わってしまう（原文の改変になる）。
    norm() は1文字1文字の置換なので、位置はもとの文字列と一致する。
    """
    src = text.lstrip()
    m = ITEM_NUM_RE.match(norm(src))
    if not m:
        return None
    num = f"{m.group(1)}-{m.group(2)}"
    return num, clean(src[m.end() :])


def item_id(num: str) -> str:
    """DOM id・URLハッシュに使える形。の→_ 共→c ～/から→r ・→n"""
    s = num.replace("の", "_").replace("から", "r").replace("～", "r")
    s = s.replace("・", "n").replace("共", "c")
    s = re.sub(r"[^0-9A-Za-z_\-]", "x", s)
    return "t" + s


def rel_articles(title: str) -> list[str]:
    """「法第36条《収入金額》関係」→ ["36"]。「法第83条から第84条の2まで」→ ["83","84の2"]"""
    out = []
    for m in REL_JO_RE.finditer(title):
        out.append(m.group(1) + (f"の{m.group(2)}" if m.group(2) else ""))
    return out


# ---------------------------------------------------------------- 本文ページ


def body_area(html: str):
    soup = BeautifulSoup(html, "html.parser")
    return soup.find(id="bodyArea") or soup


def flow(node):
    """本文の流し込み要素を文書順に返す。div の入れ物は中へ降りる。"""
    for c in node.children:
        if not isinstance(c, Tag):
            continue
        cls = set(c.get("class") or [])
        if c.name == "ol" and "breadcrumb" in cls:
            continue
        if c.name == "p" and "skip" in cls:
            continue
        if c.name == "div":
            yield from flow(c)  # div.page-header / div.table-responsive 等
        else:
            yield c


def parse_table(tbl: Tag, figs: list) -> dict:
    """<table> → {kind:"table", rows:[[{t,rs,cs}]]}

    原文の表はセルの中にレイアウト目的の表を入れ子にしていることがある（50-3 の〔表3〕）。
    find_all は再帰するので、素直に拾うと内側の行が外側の行として二重に並ぶ。
    自分の表に属する行・セルだけを見る。
    """
    rows = []
    for tr in [x for x in tbl.find_all("tr") if x.find_parent("table") is tbl]:
        cells = []
        for td in [x for x in tr.find_all(["td", "th"]) if x.find_parent("table") is tbl]:
            cell = {"t": clean(el_text(td, figs).replace("\n", " "))}
            if td.get("rowspan"):
                cell["rs"] = int(td["rowspan"])
            if td.get("colspan"):
                cell["cs"] = int(td["colspan"])
            cells.append(cell)
        if cells:
            rows.append(cells)
    return {"kind": "table", "rows": rows}


def split_marker(text: str) -> tuple[str, str]:
    """「(1)　飼育…」→ ("(1)", "飼育…")。記号が無ければ ("", 全文)。"""
    t = text.strip()
    m = MARKER_ONLY_RE.match(t)
    if m:
        return m.group(1), ""
    m = MARKER_RE.match(t)
    if m:
        return m.group(1), m.group(2).strip()
    return "", t


def find_anchor(el: Tag) -> str | None:
    """見出し要素に結び付いたアンカー名を拾う。

    原文には3形式ある：
      <a name="a-01"></a><div class="page-header"><h1>…</h1></div>   前置き
      <div class="page-header"><h1><a id="a-01"></a>…</h1></div>     見出しの内側
      <p id="a-03">〔…〕</p>                                          見出し自身
    """
    own = el.get("id") or ""
    if own.startswith("a-"):
        return own
    for a in el.find_all("a"):
        nm = a.get("name") or a.get("id") or ""
        if nm.startswith("a-"):
            return nm
    return None


def indent_level(cls: set) -> int | None:
    for c in cls:
        m = re.fullmatch(r"indent([1-9])", c)
        if m:
            return int(m.group(1))
    return None


class PageParser:
    """1ページを ｢節（見出し）→ 通達項目｣ の並びに変換する。

    fusoku=True（附則ページ）では項目番号が無く、改正通達を示す h2 が項目の切れ目になる。
    """

    def __init__(self, rel: str, mode: str | None = None):
        self.rel = rel
        self.mode = mode  # None＝本文 / "fusoku"＝附則 / "preface"＝前文・説明文
        self.fusoku = mode == "fusoku"
        # 附則と前文は項目番号を持たない。番号らしき文字列は他項目の引用なので拾わない。
        self.plain = mode in ("fusoku", "preface")
        self.groups: list[dict] = []
        self.cur: dict | None = None
        self.item: dict | None = None
        self.cap: str | None = None
        self.pending_anchor: str | None = None
        self.figs: list = []

    def new_group(self, anchor: str | None, title: str) -> None:
        self.cur = {"anchor": anchor, "title": clean(title), "items": []}
        self.groups.append(self.cur)
        self.item = None

    def ensure_group(self) -> dict:
        if self.cur is None:
            self.new_group(None, "")
        return self.cur

    def new_item(self, num: str | None, body: str, label: str | None = None) -> None:
        self.item = {
            "num": num,
            "label": label,
            "cap": self.cap,
            "paras": [{"n": "1", "t": body}] if body else [],
            "_stack": [],
        }
        self.cap = None
        self.ensure_group()["items"].append(self.item)

    def add_sub(self, level: int, text: str, figs: list) -> None:
        """indent2 以下を号列記として、深さに応じて入れ子にする。"""
        if self.item is None:
            return
        n, t = split_marker(text)
        node = {"n": n, "t": t}
        stack = self.item["_stack"]
        while stack and stack[-1][0] >= level:
            stack.pop()
        if stack:
            parent = stack[-1][1]
            parent.setdefault("items", []).append(node)
        else:
            if not self.item["paras"]:
                self.item["paras"].append({"n": "1", "t": ""})
            self.item["paras"][-1].setdefault("items", []).append(node)
        stack.append((level, node))

    def add_list(self, lst: Tag, level: int) -> None:
        """<ol>/<ul> の号列記。原文は <p class="indent2"> と <ol><li> の2通りで書かれている。"""
        for li in lst.find_all("li", recursive=False):
            subs = li.find_all(["ol", "ul"], recursive=False)
            for s in subs:
                s.extract()  # 入れ子は先に切り離し、li 自身の地の文だけを取る
            figs: list = []
            for t in para_texts(li, figs):
                self.add_sub(level, t, figs)
            for f in figs:
                self.figs.append({**f, "item": self.item["num"] if self.item else None, "page": self.rel})
                self.add_block({"kind": "fig", "src": f["src"], "alt": f["alt"]})
            for s in subs:
                self.add_list(s, level + 1)

    def add_block(self, block: dict) -> None:
        if self.item is None or not self.item["paras"]:
            return
        stack = self.item["_stack"]
        target = stack[-1][1] if stack else self.item["paras"][-1]
        target.setdefault("blocks", []).append(block)

    def parse(self, html: str) -> list[dict]:
        if self.plain:
            # 見出しより前に日付・発信者が置かれているので、先に受け皿を作っておく
            self.new_group(None, "")
            self.new_item(None, "", label="附則（制定時）" if self.fusoku else "前文・説明文")
        for el in flow(body_area(html)):
            cls = set(el.get("class") or [])
            eid = el.get("id") or ""

            # --- アンカー（節の境目）
            if el.name == "a" and el.get("name"):
                name = el["name"]
                if self.cur is not None and self.cur["anchor"] is None and not self.cur["items"]:
                    self.cur["anchor"] = name  # 見出しの直後に置かれた形
                else:
                    self.pending_anchor = name  # 見出しの直前に置かれた形
                continue

            # --- 節の見出し
            if el.name == "h1":
                title = el.get_text(" ", strip=True)
                if self.plain:
                    self.ensure_group()["title"] = title  # 受け皿は作成済み
                else:
                    self.new_group(find_anchor(el) or self.pending_anchor, title)
                    self.pending_anchor = None
                continue
            if el.name == "p" and eid.startswith("a-"):
                self.new_group(eid, el.get_text(" ", strip=True))
                self.pending_anchor = None
                continue

            # --- 項目の見出し（かっこ書き）
            if el.name in ("h2", "h3"):
                text = el.get_text(" ", strip=True)
                if self.fusoku:
                    if el.name == "h2" and FUSOKU_HEAD_RE.match(text):
                        # 附則は改正通達1本＝1リーフ。h2（例「昭48直法5-29、直所2-70」）が切れ目。
                        self.new_item(None, "", label=text)
                    else:
                        self.cap = text
                elif el.name == "h2":
                    self.cap = text
                elif self.item is not None:
                    # 本文ページの h3 は項目の見出しではなく、項目の中の小見出し
                    # （「（計算式）」「〔表5〕…」）。実測6件。次の項目に持ち越さない。
                    self.item["paras"].append({"n": "1", "t": text})
                    self.item["_stack"].clear()
                continue

            if el.name == "table":
                self.add_block(parse_table(el, self.figs))
                continue

            if el.name in ("ol", "ul"):
                self.add_list(el, indent_level(cls) or 2)
                continue

            # <p> に包まれていない裸の図表（<div class="ml2"><img> 等）
            if el.name == "img":
                src = el.get("src", "")
                if "/shared/" not in src:
                    alt = (el.get("alt") or "").strip()
                    self.add_block({"kind": "fig", "src": src, "alt": alt})
                    self.figs.append(
                        {"src": src, "alt": alt, "item": self.item["num"] if self.item else None, "page": self.rel}
                    )
                continue

            if el.name != "p":
                continue

            lv = indent_level(cls)
            figs: list = []
            texts = para_texts(el, figs)
            if figs:
                self.figs.extend({**f, "item": self.item["num"] if self.item else None, "page": self.rel} for f in figs)

            if not texts:
                # 算式だけの段落（<img> のみ）
                for f in figs:
                    self.add_block({"kind": "fig", "src": f["src"], "alt": f["alt"]})
                continue

            # 項目が始まりうるのは indent1 か、class を持たない段落だけ。
            # class="indent" の段落は前の項目の続きで、他項目への参照で始まることがある
            # （「67の２－２に定める『おおむね全部』の判定…」）。ここを項目の始まりと
            # 取り違えると、番号だけの幽霊項目ができる。
            # 附則も同様に既存項目を引用する（「50-5を次のように改め、」）ので対象外。
            item_slot = (lv == 1 or (lv is None and not cls)) and not self.plain

            for i, t in enumerate(texts):
                hit = item_num(t) if item_slot else None
                if hit:
                    self.new_item(hit[0], hit[1])
                elif lv is not None and lv >= 2:
                    self.add_sub(lv, t, figs)
                elif self.item is not None:
                    self.item["paras"].append({"n": "1", "t": t})
                    self.item["_stack"].clear()
            for f in figs:
                self.add_block({"kind": "fig", "src": f["src"], "alt": f["alt"]})

        for g in self.groups:
            for it in g["items"]:
                it.pop("_stack", None)
        return self.groups


# ---------------------------------------------------------------- 目次ページ

# 目次の見出しタグ → 階層。款と目はどちらも h5 なので文言で見分ける。
TOC_LEVEL = {"h2": ("編", 0), "h3": ("章", 1), "h4": ("節", 2), "h5": ("款", 3)}
MOKU_RE = re.compile(r"^第[0-9０-９]+目")


def parse_toc(html: str, base_path: str) -> list[dict]:
    """目次ページ → ページ単位の配置情報。

    各ページを「どの 編/章/節/款/目 の下に、どの『法第○条関係』として置くか」に落とす。
    1ページが目次に何度も現れる場合は最初の出現位置を採る（実測26ページが複数の
    『法第○条関係』にまたがるが、その場合ページ側の h1 が条関係の見出しを持っている）。
    """
    area = body_area(html)
    order: list[dict] = []
    seen: set[str] = set()
    path: list[tuple[str, str]] = []
    cur_rel: str | None = None

    def add(href: str) -> None:
        nonlocal cur_rel
        if base_path not in href:
            return
        page = href.split(base_path, 1)[1].split("#")[0]
        if not page.endswith((".htm", ".html")) or page in seen:
            return
        seen.add(page)
        order.append({"page": page, "path": list(path), "rel": cur_rel})

    for el in area.find_all(["h2", "h3", "h4", "h5", "p", "ul"]):
        if el.name in TOC_LEVEL:
            title = el.get_text(" ", strip=True)
            kind, level = TOC_LEVEL[el.name]
            if el.name == "h5" and MOKU_RE.match(norm(title)):
                kind, level = "目", 4
            del path[level:]
            path.append((kind, title))
            cur_rel = None
            continue
        if el.name == "p":
            title = el.get_text(" ", strip=True)
            a = el.find("a", href=True)
            if KANKEI_RE.match(title) or title == "附則":
                cur_rel = title
                if a:
                    add(a["href"])
            elif a:
                add(a["href"])  # 前文・説明文など
            continue
        if el.name == "ul":
            for a in el.find_all("a", href=True):
                add(a["href"])

    # 附則は目次上「第5編 雑則」の直後に置かれているが、雑則の一部ではない。最上位に出す。
    for o in order:
        if o["rel"] == "附則":
            o["path"] = []
    return order


# ---------------------------------------------------------------- 関係法条


def jo_list(num: str) -> list[str]:
    """項目番号 → 関係する所得税法の条番号。

      2-47        → ["2"]
      36の2-1     → ["36の2"]
      23～35共-1  → ["23", "24", … "35"]（範囲を展開）
      36・37共-1  → ["36", "37"]
      83から84の2-1 → ["83", "84の2"]（枝番を含む範囲は両端のみ）
    """
    head = num.rsplit("-", 1)[0].rstrip("共")
    parts = [p for p in re.split(r"～|から|・", head) if p]
    if re.search(r"～|から", head) and len(parts) == 2 and all(p.isdigit() for p in parts):
        lo, hi = int(parts[0]), int(parts[1])
        if 0 < hi - lo < 200:
            return [str(i) for i in range(lo, hi + 1)]
    return parts


def law_article_id(jo: str) -> str:
    """所得税法側の条 id（parse_laws.py の article_id と同じ形）。36の2 → a36_2"""
    return "a" + jo.replace("の", "_")


# ---------------------------------------------------------------- ツリー構築


def ensure_node(children: list, k: str, t: str) -> dict:
    """同じ見出しが直前にあれば再利用し、無ければ足す（離れた同名は別ノードのまま）。"""
    if children and children[-1].get("k") == k and children[-1].get("t") == t:
        return children[-1]
    node = {"k": k, "t": t, "c": []}
    children.append(node)
    return node


def build(order: list, pages: dict) -> tuple[list, list]:
    """目次の配置情報＋各ページの解析結果 → 目次ツリーと項目の一覧。"""
    toc: list = []
    items: list = []
    for o in order:
        node_children = toc
        crumbs: list[str] = []
        for kind, title in o["path"]:
            node_children = ensure_node(node_children, kind, title)["c"]
            crumbs.append(title)

        for g in pages[o["page"]]:
            here, path = node_children, list(crumbs)
            title = g["title"]
            if KANKEI_RE.match(title) or title == "附則":
                here = ensure_node(here, "関係", title)["c"]
                path.append(title)
            else:
                if o["rel"]:
                    here = ensure_node(here, "関係", o["rel"])["c"]
                    path.append(o["rel"])
                if title:
                    here = ensure_node(here, "群", title)["c"]
                    path.append(title)

            for it in g["items"]:
                num, label = it["num"], it["label"]
                if num:
                    aid, disp = item_id(num), num
                else:
                    aid, disp = f"f{len(items):02d}", label or "附則"
                art = {
                    "num": num or "",
                    "id": aid,
                    "label": disp,
                    "title": disp,
                    "path": path,
                    "paras": it["paras"],
                    "page": o["page"],
                }
                if it["cap"]:
                    art["cap"] = it["cap"]
                if num:
                    art["ja"] = jo_list(num)
                else:
                    art["fusoku"] = 1

                leaf = {"k": "条", "id": aid, "l": disp, "t": disp}
                if it["cap"]:
                    leaf["cap"] = it["cap"]
                if not num:
                    leaf["fs"] = 1  # 附則（番号ジャンプの対象外）
                here.append(leaf)
                art["_leaf"] = leaf
                art["_rel"] = o["rel"]
                art["_holder"] = here
                items.append(art)
    return toc, items


def dedupe(items: list) -> tuple[list, list]:
    """同じ番号の項目が2か所に出るとき、目次が指している方を残す。

    国税庁のページには重複掲載がある。実測では 12/03.htm の末尾に「法第62条関係」が
    まるごと残っており、12/04.htm の同じ内容と重なる。目次が 12/03.htm を指しているのは
    「法第60条関係」としてなので、項目の関係法条（62-1 → 法第62条）と、そのページが
    目次上どの条関係に置かれているかを突き合わせて、一致する側を採る。
    """
    by_id: dict[str, list] = {}
    for a in items:
        by_id.setdefault(a["id"], []).append(a)

    dropped = []
    for aid, group in by_id.items():
        if len(group) < 2:
            continue
        def score(a: dict) -> int:
            want = set(rel_articles(a["_rel"] or ""))
            return 1 if want & set(a.get("ja") or []) else 0
        group.sort(key=score, reverse=True)
        for a in group[1:]:
            a["_holder"].remove(a["_leaf"])
            dropped.append({"id": aid, "page": a["page"], "cap": a.get("cap")})
    keep = [a for a in items if not any(d["page"] == a["page"] and d["id"] == a["id"] for d in dropped)]
    return keep, dropped


# ---------------------------------------------------------------- 書き出し


def jsize(obj) -> int:
    return len(json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def write_json(path: Path, obj) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    path.write_bytes(data)
    return len(data)


def chunk(items: list) -> list[list]:
    chunks: list[list] = []
    cur: list = []
    size = 0
    for a in items:
        s = jsize(a)
        if cur and size + s > CHUNK_BYTES:
            chunks.append(cur)
            cur, size = [], 0
        cur.append(a)
        size += s
    if cur:
        chunks.append(cur)
    return chunks or [[]]


def copy_images(key: str, src: dict, items: list) -> tuple[int, list]:
    """図表・算式の画像を docs/data/{key}_tsutatsu/img/ へ複製し、参照先を差し替える。"""
    out_dir = OUT_DIR / f"{key}_tsutatsu" / "img"
    raw_img = RAW_DIR / key / "_img"
    base = src["base_path"]
    used, with_fig = 0, []

    def fix(blocks: list, art: dict) -> None:
        nonlocal used
        for b in blocks:
            if b.get("kind") != "fig":
                continue
            rel = b["src"].split(base, 1)[-1].lstrip("/") if base in b["src"] else b["src"].rsplit("/", 1)[-1]
            srcf = raw_img / rel
            name = rel.replace("/", "_")
            if srcf.exists():
                out_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(srcf, out_dir / name)
                b["src"] = f"img/{name}"
                used += 1
            else:
                b["src"] = ""
            with_fig.append({"item": art["label"], "page": art["page"], "img": name, "alt": b.get("alt", "")})

    def walk(node: dict, art: dict) -> None:
        if node.get("blocks"):
            fix(node["blocks"], art)
        for sub in node.get("items") or []:
            walk(sub, art)

    for art in items:
        for p in art["paras"]:
            walk(p, art)
    return used, with_fig


def merge_index(entry: dict) -> None:
    """法令側が生成した index.json に通達エントリをマージする（法令側の処理には触らない）。"""
    path = OUT_DIR / "index.json"
    idx = json.loads(path.read_text(encoding="utf-8"))
    idx.setdefault("kinds", {})["tsutatsu"] = {"label": "通達", "law_type": "Tsutatsu"}
    idx["laws"] = [x for x in idx["laws"] if x["key"] != entry["key"]] + [entry]
    idx["abbr"][entry["abbr"]] = entry["key"]
    idx.setdefault("tsutatsu_source", {
        "site": "国税庁ホームページ 法令解釈通達",
        "attribution": SOURCE_NOTE,
    })
    write_json(path, idx)


# ---------------------------------------------------------------- 1通達の変換


def parse_one(key: str) -> dict:
    links = json.loads((RAW_DIR / key / "_links.json").read_text(encoding="utf-8"))
    src = {"base_path": "/" + links["toc_url"].split("//", 1)[1].split("/", 1)[1].rsplit("/", 1)[0] + "/"}
    out_key = f"{key}_tsutatsu"
    out_dir = OUT_DIR / out_key
    if out_dir.exists():
        shutil.rmtree(out_dir)

    # --- 目次
    toc_html = (RAW_DIR / key / "01.htm").read_bytes().decode("cp932", "replace")
    order = parse_toc(toc_html, src["base_path"])

    # --- 各ページ
    pages: dict[str, list] = {}
    figs_all: list = []
    for o in order:
        rel = o["page"]
        html = (RAW_DIR / key / rel).read_bytes().decode("cp932", "replace")
        # 前文（00/01.htm）は目次上どの「法第○条関係」にも属さない＝rel が無い
        mode = "fusoku" if o["rel"] == "附則" else ("preface" if o["rel"] is None else None)
        pp = PageParser(rel, mode=mode)
        pages[rel] = pp.parse(html)
        figs_all.extend(pp.figs)

    tree, items = build(order, pages)
    items, dropped = dedupe(items)
    for a in items:
        a.pop("_rel", None)
        a.pop("_holder", None)

    # --- id の重複チェック（番号の正規化ミスを取りこぼさないための歯止め）
    ids = [a["id"] for a in items]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        raise RuntimeError(f"項目 id が重複しています: {sorted(dup)[:10]}")

    n_fig, with_fig = copy_images(key, src, items)

    # --- チャンク分割（_leaf は目次ツリーへの参照なので出力からは外す）
    chunks = chunk([{k: v for k, v in a.items() if k != "_leaf"} for a in items])
    where: dict[str, int] = {}
    for i, ch in enumerate(chunks):
        for a in ch:
            where[a["id"]] = i
    bytes_main = 0
    for i, ch in enumerate(chunks):
        bytes_main += write_json(out_dir / f"main-{i:02d}.json", {"arts": ch})
    for a in items:
        a["_leaf"]["f"] = where[a["id"]]

    # --- 法条 → 通達項目の対応表（法令側からのリンク・通達側からのリンクに使う）
    rel_map: dict[str, list] = {}
    for a in items:
        for jo in a.get("ja") or []:
            rel_map.setdefault(law_article_id(jo), []).append(a["id"])

    n_items = sum(1 for a in items if not a.get("fusoku"))
    n_fusoku = sum(1 for a in items if a.get("fusoku"))
    bytes_toc = write_json(
        out_dir / "toc.json",
        {
            "key": out_key,
            "name": links["name"],
            "abbr": links["abbr"],
            "kind": "tsutatsu",
            "parent": links["parent"],
            "source_url": links["toc_url"],
            "published_as_of": links["published_as_of"],
            "chunks": len(chunks),
            "toc": tree,
            "arts": [
                {"id": a["id"], "l": a["label"], "f": where[a["id"]]}
                for a in items
                if not a.get("fusoku")
            ],
            "rel": rel_map,
            "has_suppl": False,
            "has_appdx": False,
        },
    )

    entry = {
        "key": out_key,
        "group": key,
        "kind": "tsutatsu",
        "name": links["name"],
        "abbr": links["abbr"],
        "parent": links["parent"],
        "law_id": "",
        "law_num": links["published_as_of"],
        "enforced": None,
        "articles": n_items,
        "suppls": n_fusoku,
        "appdx": 0,
        "chunks": len(chunks),
        "source_url": links["toc_url"],
    }
    merge_index(entry)

    print(f"{links['abbr']} {links['name']}")
    print(f"  ページ {len(order)} / 項目 {n_items} / 附則 {n_fusoku} / チャンク {len(chunks)}")
    if dropped:
        print(f"  原文の重複掲載を除外: {len(dropped)}件 " + ", ".join(f"{d['id'][1:]}({d['page']})" for d in dropped))
    print(f"  toc.json {bytes_toc / 1024:.0f}KB  本体 {bytes_main / 1024:.0f}KB  画像 {n_fig}件")
    print(f"  掲載時点 {links['published_as_of']}")

    # --- 図表を含む項目の一覧（指示書 Step2-4：評基通対応の予行演習として記録）
    log = ROOT / "data" / "raw_tsutatsu" / key / "_figures.json"
    write_json(log, {"count": len(with_fig), "items": with_fig})
    print(f"  図表を含む項目の一覧 → {log.relative_to(ROOT)}")
    return {"items": n_items, "figs": with_fig}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", help="通達の key（例 shotoku）")
    args = ap.parse_args()

    keys = [p.name for p in sorted(RAW_DIR.iterdir()) if (p / "_links.json").exists()]
    if args.only:
        keys = [k for k in args.only if k in keys]
    if not keys:
        print("変換対象がありません（先に fetch_tsutatsu.py を実行してください）", file=sys.stderr)
        return 1
    for k in keys:
        parse_one(k)
    return 0


if __name__ == "__main__":
    sys.exit(main())
