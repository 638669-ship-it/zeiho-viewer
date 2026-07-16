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

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw_tsutatsu"
OUT_DIR = ROOT / "docs" / "data"

# 国税庁のHTMLは <p> の閉じ忘れが多い。標準の html.parser は閉じ忘れを補正しないため、
# 評基通の目次では245本のリンクと30個の節見出しが1つの <p> に飲み込まれ、
# 階層も順序も壊れた（最大の <p> が6,041字）。html5lib はブラウザと同じHTML5の
# 解釈で構造を組み直すので、同じ <p> が56字になり、節見出しとリンクが正しく並ぶ。
HTML_PARSER = "html5lib"

# 法令側（parse_laws.py）と同じ目安。gzip後で1チャンク約48KB。
CHUNK_BYTES = 350_000

SOURCE_NOTE = "出典：国税庁ホームページ 法令解釈通達を加工して表示。正確な内容は原文を確認してください。"

# 図表とみなすのは、その通達自身のディレクトリ配下にある画像だけ。
# それ以外はサイト共通の字形画像・アイコンで、図表ではない（実測）:
#   /shared/images/unit/hoten.gif  alt="ほてん"  本文中の「補塡」（塡が表外字）
#   /shared/images/decimal/a.gif   alt="A"       算式中の記号
#   /category/icon_img/no_img/01.gif alt="1"     法基通の丸数字
#   /top_img/shim.gif              alt=" "       レイアウト用の透明画像
# これらは alt の文字に戻す。判定に使うのでページ解析の前に parse_one が設定する。
CURRENT_BASE = ""

# alt が読み仮名になっていて、字形そのものを当てたほうが原文に近いもの。
# hoten.gif は alt="ほてん" だが、前後の文字（を/の/付/差/り → する/さ/金）から
# 画像の実体は「補塡」2文字。「補」が直前に来る例は全37件中ゼロで裏付けた。
IMG_TEXT = {"hoten.gif": "補塡", "hoten_h2.gif": "補塡"}


# 図表が文章のどの位置にあったかを覚えておくための印（本文には現れない制御文字）
FIG_MARK = "\x00FIG\x00"


def is_figure(src: str) -> bool:
    return bool(CURRENT_BASE) and src.startswith(CURRENT_BASE)


ZEN2HAN = str.maketrans("０１２３４５６７８９", "0123456789")

# 項目番号の区切りに使われるダッシュ類（原文に4種類が混在する）。
# 文字クラス内でハイフンが範囲と解釈されないよう1文字ずつエスケープする。
DASH = "[" + "".join(re.escape(c) for c in "－-―ー−‐–—") + "]"
# 「の」による枝番は2段以上になる（49－1の3の2、67の2－2の2）。* で繰り返しを許す。
BRANCH = r"\d+(?:の\d+)*"
# 1セグメント：2 / 36の2 / 23～35 / 36・37 / 83から84の2 （末尾に「共」が付くことがある）
SEG = rf"{BRANCH}(?:(?:～|・|から){BRANCH})*共?"
# 評基通の補正率表は「付表1」「付表10」という番号を持つ。
ITEM_PREFIX = r"(?:付表)?"
# 通達によってセグメント数が違う。少ないほうに合わせると誤検出するので通達ごとに指定する。
#   所基通・相基通など … 条-枝番の2つ（36-1、23～35共-1）
#   法基通・消基通など … 章-節-連番の3つ（1-1-1、2-1-1の10）
#   評基通             … 通し番号1つ（1、4-2、付表1）
# 区切りは原文が4種のダッシュを混在させる。取れるだけ取る（最大3セグメント）。
_ITEM_RE_CACHE: dict[int, re.Pattern] = {}


def item_re(min_seg: int) -> re.Pattern:
    if min_seg not in _ITEM_RE_CACHE:
        _ITEM_RE_CACHE[min_seg] = re.compile(
            rf"^({ITEM_PREFIX}{SEG}(?:{DASH}{SEG}){{{min_seg - 1},2}})(?![0-9])"
        )
    return _ITEM_RE_CACHE[min_seg]

# 号列記の記号：(1) （1） （注） (注) （イ） / 裸のイロハ / 注の中の枝番号
IROHA = "イロハニホヘトチリヌルヲワカヨタレソツネナラムウヰノオクヤマケフコエテアサキユメミシヱヒモセス"
MARKER_RE = re.compile(
    rf"^([（(][^）)]{{1,6}}[）)]|[{IROHA}]|[①-⑳]|\d{{1,3}})[　\s]+(.*)$", re.S
)
MARKER_ONLY_RE = re.compile(rf"^([（(][^）)]{{1,6}}[）)])$")

# 「法第36条《収入金額》関係」→ 対応する所得税法の条番号
REL_JO_RE = re.compile(r"法第(\d+)条(?:の(\d+))?")

# 節見出しが「法第○条《…》関係」かどうか。通基通・徴基通は「法」を付けずに
# 「第2条関係　定義」と書く。
# 「条」が抜けている見出しが実在する（通基通「第50関係　担保の種類」＝国税庁の誤植）。
# 条を必須にすると、その条関係だけ番号の合成先が前の条のまま残って番号が衝突する。
KANKEI_RE = re.compile(r"^(法)?第\d+条?(の\d+)?.*関係")

# 「第2条関係　定義」「第7条の2関係　…」→ 条番号（7の2）
# 番号の合成に使う条番号。「関係」が条のすぐ後ろに来るものだけを条関係の見出しとみなす。
# 「法第108条との関係」は第115条関係の中の小見出しで、これを条関係と誤ると
# 以降の項目が 115-N ではなく 108-N になってしまう（実測）。
KANKEI_JO_RE = re.compile(r"^(?:法)?第(\d+)条?(?:の(\d+))?関係")

# 先頭の太字が数字だけ（通基通の項目の目印）。枝番は「8-2」とダッシュで書かれる
# （＝8の2）ので、区切りの4種のダッシュと「の」の両方を許す。
NUM_ONLY_RE = re.compile(rf"^\d+(?:(?:の|{DASH})\d+)*[　\s]*$")

# 太字のかっこ書きだけの段落＝項目の見出し（通基通）
CAP_ONLY_RE = re.compile(r"^[（(].*[）)]$")

# 附則ページの h2 は「改正通達の名前」と「見出し」の両方に使われる。
# 改正通達名は「附則（…）」か元号（昭45直審（所）55）で始まる。見出しは「（施行期日）」。
FUSOKU_HEAD_RE = re.compile(r"^(附則|昭\d|平\d|令\d|昭和|平成|令和)")

# 別表のページ（評基通 08/09.htm）。表だけが並び、項目番号を持たない。
# 「別表2」の中の表題「1 樹齢1年以下の森林の立木の標準価額表」を項目1と読むと、
# 本物の評基通1（評価の原則）と衝突して中身が丸ごと消える。番号は読まない。
BETSU_RE = re.compile(r"^(別表|〔参考〕)")

# それ自体では中身の分からないリンク文字列。この場合だけ直前の見出しを頼る。
GENERIC_LABEL_RE = re.compile(r"^(詳細はこちら|こちら|目次)$")

# plain モード（項目番号を持たないページ）で、どの見出しを項目の切れ目にするか
PLAIN_BOUNDARY = {"fusoku": FUSOKU_HEAD_RE, "betsu": BETSU_RE}

# plain モードの最初の項目につける名前
PLAIN_FIRST_LABEL = {"fusoku": "附則（制定時）", "preface": "前文・説明文", "betsu": "別表"}


def norm(s: str) -> str:
    """全角数字を半角へ。項目番号の判定を安定させる。"""
    return s.translate(ZEN2HAN)


def clean(s: str) -> str:
    """前後の空白・全角空白を落とす。"""
    return s.replace("　", " ").strip()


# ---------------------------------------------------------------- テキスト抽出


def el_text(el, figs: list, inline_figs: bool = False) -> str:
    """要素配下の地の文。<img> は字形画像なら文字に、図表なら figs に退避して本文からは外す。

    inline_figs=True のときは図表も alt の文字として本文に並べる。表のセルの中に
    図表が入っている場合に使う（法基通 9-1-6の2 の表は、見出しのセルが
    <img alt="保有期間の年数/経過年数"> という斜線見出しの画像になっている）。
    セルは文字しか持てないので、画像として出さずに alt を置く。
    """
    out = []
    # <img> そのものを渡されることがある（<div class="ml2"><img> のように <p> に
    # 包まれていない図表）。descendants は自分自身を含まないので、その場合は自分を見る。
    nodes = [el] if getattr(el, "name", None) == "img" else el.descendants
    for node in nodes:
        if isinstance(node, Tag):
            if node.name == "img":
                src = node.get("src", "")
                alt = (node.get("alt") or "").strip()
                name = src.rsplit("/", 1)[-1]
                if is_figure(src):
                    figs.append({"src": src, "alt": alt})
                    # 図表が文章のどこにあったかを印で残す（para_parts が切り分ける）
                    out.append(alt if inline_figs else FIG_MARK)
                else:
                    # 表外字・記号の字形画像 → 文字に戻す
                    out.append(IMG_TEXT.get(name, alt))
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
    return [clean(x) for x in raw.replace(FIG_MARK, "").split("\n") if clean(x)]


def para_parts(el) -> list[tuple[str, object]]:
    """1つの <p>/<li>/<dd> を、図表の位置を保ったまま ("text", 文字列) と
    ("fig", 画像) の並びにする。

    図表は段落の末尾にあるとは限らず、文章の途中に挟まっていることがある。
      <p><img alt="…の算式">（小数点以下3位未満の端数切上げ）とする。</p>
      <p>…「昭和」を「平成」に、<img alt="「相続税評価額」を「財産評価額」">に、…改め、</p>
    まとめて末尾に追い出すと原文と順序が変わってしまうので、出てきた位置で切る。
    <br> も段落の切れ目として扱う。
    """
    figs: list = []
    raw = el_text(el, figs)
    parts: list[tuple[str, object]] = []
    i = 0
    for tok in re.split(f"(\n|{re.escape(FIG_MARK)})", raw):
        if tok == FIG_MARK:
            parts.append(("fig", figs[i]))
            i += 1
        elif tok and tok != "\n":
            t = clean(tok)
            if t:
                parts.append(("text", t))
    return parts


# ---------------------------------------------------------------- 番号の正規化


def item_num(text: str, min_seg: int = 2) -> tuple[str, str] | None:
    """段落の先頭から項目番号を読む。→ (正規化した番号, 残りの本文) / 該当なしなら None

    番号の判定には全角数字を半角に直した複製を使うが、本文はもとの文字のまま返す。
    正規化した文字列から本文を切り出すと、改正履歴の「令７課個2-10」が
    「令7課個2-10」に書き換わってしまう（原文の改変になる）。
    norm() は1文字1文字の置換なので、位置はもとの文字列と一致する。
    """
    src = text.lstrip()
    m = item_re(min_seg).match(norm(src))
    if not m:
        return None
    # 区切りのダッシュ4種を半角に揃える（36－1 も 36ー1 も 36-1 として扱う）
    num = re.sub(DASH, "-", m.group(1))
    return num, clean(src[m.end() :])


def item_id(num: str) -> str:
    """DOM id・URLハッシュに使える形。の→_ 共→c ～/から→r ・→n 付表→f"""
    s = num.replace("付表", "f").replace("の", "_").replace("から", "r").replace("～", "r")
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
    soup = BeautifulSoup(html, HTML_PARSER)
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
        # div は入れ物のこともあれば（page-header / table-responsive / ml2）、
        # 段落そのもののこともある（消基通の <div class="indent2">(注)</div>）。
        # indent クラスが付いていれば段落として扱う。降りてしまうと中の地の文が落ちる。
        if c.name == "div" and indent_level(cls) is None:
            yield from flow(c)
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
            cell = {"t": clean(el_text(td, figs, inline_figs=True).replace("\n", " "))}
            if td.get("rowspan"):
                cell["rs"] = int(td["rowspan"])
            if td.get("colspan"):
                cell["cs"] = int(td["colspan"])
            cells.append(cell)
        if cells:
            rows.append(cells)
    out = {"kind": "table", "rows": rows}
    # 表の題名が <caption> で書かれていることがある（評基通 別表2 の
    # 「6　標準伐期にある森林の立木の標準価額表」）。拾わないと題名だけ消える。
    cap = tbl.find("caption")
    if cap is not None and cap.find_parent("table") is tbl:
        title = clean(el_text(cap, figs, inline_figs=True).replace("\n", " "))
        if title:
            out["title"] = title
    return out


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


def leading_strong(el: Tag) -> str:
    """段落の先頭に連続する <strong> の文字列。

    通基通は項目の置き場所（indent の深さ）が揃っておらず、確実な目印は
    「先頭の太字が数字だけ」であること。所基通の <strong>2</strong><strong>－1</strong>
    のように太字が連続することがあるのでまとめて返す。
    """
    out = []
    for c in el.children:
        if isinstance(c, NavigableString):
            if str(c).strip():
                break          # 太字の前に地の文がある＝先頭ではない
            continue
        if isinstance(c, Tag) and c.name == "strong":
            out.append(c.get_text("", strip=False))
            continue
        break
    return "".join(out)


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

    def __init__(self, rel: str, mode: str | None = None, min_seg: int = 2, label: str = "",
                 by_strong: bool = False, compose: bool = False, toc_name: str = ""):
        self.rel = rel
        self.toc_name = toc_name
        self.label = label
        # 通基通は「先頭の太字が数字だけ」が項目の目印で、番号は条関係ごとに
        # 1から振り直される。条番号と合成して 2-1 という一意な番号にする。
        self.by_strong = by_strong
        self.compose = compose
        self.cur_jo: str | None = None
        self.min_seg = min_seg  # 項目番号の最小セグメント数（通達ごとに違う）
        self.mode = mode  # None＝本文 / "fusoku"＝附則 / "preface"＝前文・説明文
        self.fusoku = mode == "fusoku"
        # 附則・前文・別表は項目番号を持たない。番号らしき文字列は他項目の引用や
        # 表の題名なので拾わない。
        self.plain = mode in ("fusoku", "preface", "betsu")
        self.groups: list[dict] = []
        self.cur: dict | None = None
        self.item: dict | None = None
        self.cap: str | None = None
        self.pending_anchor: str | None = None
        self.figs: list = []

    def new_group(self, anchor: str | None, title: str) -> None:
        if self.compose:
            m = KANKEI_JO_RE.match(norm(clean(title)))
            if m:
                self.cur_jo = m.group(1) + (f"の{m.group(2)}" if m.group(2) else "")
        self.cur = {"anchor": anchor, "title": clean(title), "items": []}
        self.groups.append(self.cur)
        self.item = None

    def ensure_group(self) -> dict:
        if self.cur is None:
            self.new_group(None, "")
        return self.cur

    def new_item(self, num: str | None, body: str, label: str | None = None) -> None:
        # 受け皿を先に確保する。ensure_group が新しい節を作ると self.item が None に
        # 戻されるので、順番を逆にすると None を並べてしまう（見出しの無いページで出る）。
        group = self.ensure_group()
        self.item = {
            "num": num,
            "label": label,
            "cap": self.cap,
            "paras": [{"n": "1", "t": body}] if body else [],
            "_stack": [],
        }
        self.cap = None
        group["items"].append(self.item)

    def add_sub(self, level: int, text: str) -> None:
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

    def emit(self, el: Tag, on_text) -> None:
        """要素の中身を、図表の位置を保ったまま流し込む。"""
        for kind, val in para_parts(el):
            if kind == "fig":
                self.figs.append({**val, "item": self.item["num"] if self.item else None, "page": self.rel})
                self.add_block({"kind": "fig", "src": val["src"], "alt": val["alt"]})
            else:
                on_text(val)

    def add_list(self, lst: Tag, level: int) -> None:
        """<ol>/<ul> の号列記。原文は <p class="indent2"> と <ol><li> の2通りで書かれている。"""
        for ch in [x for x in lst.children if isinstance(x, Tag)]:
            if ch.name == "li":
                self.add_li(ch, level)
            elif ch.name in ("ol", "ul"):
                # <ol> の直下に <ol> が入っている（措通 41の4の3-1）。不正なHTMLだが
                # 実在する。li だけを見ると「イ・ロ」が丸ごと落ちる。
                self.add_list(ch, level + 1)

    def add_li(self, li: Tag, level: int) -> None:
        """<li> の中身を文書順に流し込む。

        li は地の文だけでなく <p> <div><img> <ol> を抱えている（措通株式 37の10-8）。
        入れ子のリストを後回しにすると、そのリストの後ろにある段落より後に出てしまい、
        原文と順序が変わる（「※1…」が「また、…」の後ろに回る）。
        """
        run: list = []

        def flush() -> None:
            if not any(str(x).strip() for x in run):
                run.clear()
                return
            holder = BeautifulSoup("<p></p>", HTML_PARSER).find("p")
            for n in list(run):
                holder.append(n.extract() if isinstance(n, Tag) else NavigableString(str(n)))
            run.clear()
            self.emit(holder, lambda t: self.add_sub(level, t))

        for ch in list(li.children):
            if isinstance(ch, Comment):
                continue   # <!-- 取得価額 --> のような注記を本文にしない
            if not (isinstance(ch, Tag) and ch.name in ("p", "div", "ol", "ul", "dl", "table", "img")):
                run.append(ch)
                continue
            flush()
            if ch.name in ("ol", "ul"):
                self.add_list(ch, level + 1)
            elif ch.name == "dl":
                self.add_dl(ch, level + 1)
            elif ch.name == "table":
                self.add_block(parse_table(ch, self.figs))
            else:
                self.emit(ch, lambda t: self.add_sub(level, t))
        flush()

    def add_dl(self, dl: Tag, level: int) -> None:
        """<dl><dt>(注)</dt><dd>1 …</dd><dd>2 …</dd></dl> 形式の（注）。

        法基通が使う書き方（実測62か所。所基通には無い）。dt が「(注)」、
        dd が枝番号つきの中身なので、dd を dt の1段下に置く。

        ただし <dl> の直下に <p> や <img> が dd と混ざって並んでいることがある
        （不正なHTMLだが実在。2-2-11 は dd の間に算式の <img> と「イ」「ロ」の <p> が入る）。
        dt/dd だけ拾うと中身が落ちるうえ順序も崩れるので、直下の子を文書順に全部見る。
        """
        for ch in [c for c in dl.children if isinstance(c, Tag)]:
            if ch.name == "img":
                self.emit(ch, lambda t: None)
                continue
            if ch.name == "table":
                self.add_block(parse_table(ch, self.figs))
                continue
            if ch.name in ("ul", "ol"):
                self.add_list(ch, level + 2)
                continue
            if ch.name == "dt":
                lv = level
            elif ch.name == "dd":
                lv = level + 1
            elif ch.name == "p":
                lv = indent_level(set(ch.get("class") or [])) or (level + 1)
            else:
                continue
            self.emit(ch, lambda t, lv=lv: self.add_sub(lv, t))

    def add_block(self, block: dict) -> None:
        if self.item is None:
            return
        if not self.item["paras"]:
            # 本文を持たない項目（評基通の「付表7」は番号だけの段落で、
            # 中身は直後の表）。受け皿が無いと表・図表を落としてしまう。
            self.item["paras"].append({"n": "1", "t": ""})
        stack = self.item["_stack"]
        target = stack[-1][1] if stack else self.item["paras"][-1]
        target.setdefault("blocks", []).append(block)

    def parse(self, html: str) -> list[dict]:
        if self.plain:
            # 見出しより前に日付・発信者が置かれているので、先に受け皿を作っておく
            self.new_group(None, "")
            self.new_item(None, "", label=PLAIN_FIRST_LABEL.get(self.mode, self.label or "本文"))
        for el in flow(body_area(html)):
            # 通基通・徴基通は本文の後ろにページ内ナビ（「目次」「●…の制定について」）が
            # 続く。目次へのリンクが出たらそこから先は本文ではない。
            if self.toc_name and el.find("a", href=lambda h: h and h.endswith("/" + self.toc_name)):
                break
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
                if self.plain:
                    boundary = PLAIN_BOUNDARY.get(self.mode)
                    if el.name == "h2" and boundary and boundary.match(text):
                        # 附則は改正通達1本、別表は表1本が1リーフ。h2 が切れ目。
                        self.new_item(None, "", label=text)
                    elif self.item is not None:
                        # 附則の h2/h3 は改正通達名だけでなく、その中の見出しにも使われる
                        # （法基通「第1　法人税基本通達の改正　（略）」「（施行期日）」）。
                        # 見出しとして捨てず、本文の流れの中に残す。
                        self.item["paras"].append({"n": "1", "t": text})
                        self.item["_stack"].clear()
                    else:
                        self.cap = text
                elif self.by_strong and el.name == "h2":
                    # 通基通・徴基通の h2 は項目の見出しではなく小群の見出し
                    # （「納税者」「国税」「相続人が2人以上ある場合の承継税額」）。
                    self.new_group(find_anchor(el), text)
                elif el.name == "h2":
                    if self.item is not None and not self.item["paras"] and not self.item["cap"]:
                        # 評基通の付表は「<p><strong>付表7</strong></p><h2>奥行長大補正率表</h2>」の順で、
                        # 見出しが番号の後に来る。次の項目の見出しにしてしまわないよう、
                        # 中身がまだ空の項目にはその場で付ける。
                        self.item["cap"] = text
                    else:
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

            if el.name == "dl":
                self.add_dl(el, indent_level(cls) or 2)
                continue

            # <p> に包まれていない裸の図表（<div class="ml2"><img> 等）
            if el.name == "img":
                self.emit(el, lambda t: None)
                continue

            # indent クラスの付いた div は段落（flow がここまで通してくる）
            if el.name not in ("p", "div"):
                continue

            lv = indent_level(cls)

            if self.by_strong and not self.plain:
                # 通基通・徴基通は太字の使い方で意味が決まる。
                #   段落まるごと太字の（…） → その次の項目の見出し
                #   段落まるごと太字のそれ以外 → 小群の見出し（「納税者」「法定納期限」）
                #   先頭の太字が数字 → 項目
                # アンカーが太字の前後どちらにも入るので（<p><a id><strong>（最近）</strong></p>、
                # <p><a name><strong>法定納期限</strong></a></p>）、位置ではなく
                # 「段落の地の文が全部太字か」で見る。
                whole = clean(el.get_text("", strip=False))
                strongs = el.find_all("strong")
                bold = clean("".join(x.get_text("", strip=False) for x in strongs))
                if whole and bold == whole:
                    if CAP_ONLY_RE.match(whole):
                        self.cap = whole
                    else:
                        self.new_group(find_anchor(el), whole)
                    continue
                first = clean(strongs[0].get_text("", strip=False)) if strongs else ""
                if first and NUM_ONLY_RE.match(norm(first)) and norm(whole).startswith(norm(first)):
                    body = clean(whole[len(first):])
                    raw_num = re.sub(DASH, "-", norm(first).strip())
                    num = f"{self.cur_jo}-{raw_num}" if (self.compose and self.cur_jo) else raw_num
                    self.new_item(num, body)
                    continue

            # 項目が始まりうるのは indent1 か、class も style も持たない段落だけ。
            #
            # 前の項目の続きは他項目への参照で始まることがあり、そこを項目の始まりと
            # 取り違えると番号だけの幽霊項目ができる。字下げの表し方が2通りあるので両方外す:
            #   class="indent"                所基通「67の２－２に定める『おおむね全部』の判定…」
            #   style="margin-left:2.5em;…"   消基通「16－2－2(2)ニに掲げる方法と同様の方法により…」
            # class も style も無い段落には本物の項目がある（所基通「90－1 削除」など34件）
            # ので、その分岐自体は残す。
            # 附則・前文も既存項目を引用する（「50-5を次のように改め、」）ので対象外。
            bare = lv is None and not cls and not el.get("style")
            item_slot = (lv == 1 or bare) and not self.plain and not self.by_strong

            # 項目番号は段落の先頭にしか来ない。<br> で折り返した2行目以降を項目の
            # 始まりと見ると、評基通の「（2）…生産緑地<br>　100分の5」の「100分の5」を
            # 項目100として作ってしまう（本物の100と衝突する）。
            first = [True]

            def on_text(t: str, lv=lv, item_slot=item_slot) -> None:
                hit = item_num(t, self.min_seg) if (item_slot and first[0]) else None
                first[0] = False
                if hit:
                    self.new_item(hit[0], hit[1])
                    return
                if self.cap and self.item is not None:
                    # 見出しが出たのに番号付きの項目が続かなかった場合。
                    # 相基通 53-2 の後ろの「<h2>（附則）</h2>」、措通譲渡 37の3-2 の
                    # 項目の途中に入る「<h2>〔設例1〕</h2>」。捨てると順序が狂う。
                    self.item["paras"].append({"n": "1", "t": self.cap})
                    self.cap = None
                    self.item["_stack"].clear()
                if lv is not None and lv >= 2:
                    self.add_sub(lv, t)
                elif self.item is None and self.compose and self.cur_jo:
                    # 条関係に項目が1つしかないと、原文は番号を省く
                    # （第36条関係の「（納付場所の指定）」）。条関係そのものを番号にする。
                    self.new_item(self.cur_jo, t)
                elif self.item is not None:
                    self.item["paras"].append({"n": "1", "t": t})
                    self.item["_stack"].clear()

            self.emit(el, on_text)

        for g in self.groups:
            for it in g["items"]:
                it.pop("_stack", None)
            # plain モードは見出しの前に受け皿を作るので、中身が来なければ空で残る
            g["items"] = [it for it in g["items"] if it["num"] or it["paras"]]
        return self.groups


# ---------------------------------------------------------------- 目次ページ

# 階層は見出しのタグではなく文言から決める。通達ごとにタグの使い方が違うため。
#   所基通 h2=編 h3=章 h4=節 h5=款/目（款と目が同じタグ）
#   法基通 h2=章、節・款は入れ子の <ul><li>
DIV_ORDER = "編章節款目"
DIV_RE = re.compile(r"^第[0-9]+(?:の[0-9]+)?\s*([編章節款目])")
# 前文の言い方は通達ごとに違う。所基通「前文・説明文」／通基通「● …の制定について」／
# 徴基通「…の全文改正について」「引用の法令番号一覧表」「主要省略用語一覧表」。
# いずれも項目番号を持たない読み物なので、番号の解釈をせずそのまま載せる。
PREFACE_RE = re.compile(r"前文|制定について|全文改正について|一覧表")
FUSOKU_RE = re.compile(r"^附則")


def div_of(title: str) -> tuple[str, int] | None:
    """「第2款　所得金額の計算の通則」→ ("款", 3)。階層でなければ None。"""
    m = DIV_RE.match(norm(title).replace("　", " ").lstrip())
    return (m.group(1), DIV_ORDER.index(m.group(1))) if m else None


def parse_toc(html: str, base_path: str, allowed: set[str] | None = None) -> list[dict]:
    """目次ページ → ページ単位の配置情報。

    各ページを「どの 編/章/節/款/目 の下に、どういう名目で置くか」に落とす。
    1ページが目次に何度も現れる場合は最初の出現位置を採る（所基通では26ページが複数の
    『法第○条関係』にまたがるが、その場合ページ側の h1 が条関係の見出しを持っている）。

    通達ごとに目次の作りが違うので、次の2通りをどちらも受ける:
      所基通 見出しタグで階層 → <p>法第2条《定義》関係</p> → <ul><li><a>〔…〕</a>
      法基通 <h2>第1章 総則</h2> → <ul><li>第1節 …<ul><li><a>第1款 …</a>
    """
    area = body_area(html)
    order: list[dict] = []
    seen: set[str] = set()
    path: list[tuple[str, str]] = []
    state = {"rel": None}

    def add(href: str, label: str, extra_path: list) -> None:
        if base_path not in href:
            return
        page = href.split(base_path, 1)[1].split("#")[0]
        if not page.endswith((".htm", ".html")) or page in seen:
            return
        # 取得対象から外したページ（旧版アーカイブ・一部改正通達など）は目次からも外す。
        # 取得側（fetch_tsutatsu.py の EXCLUDE_RE）と食い違わないよう、
        # 実際に取得した一覧（_links.json）を正とする。
        if allowed is not None and page not in allowed:
            return
        seen.add(page)
        # 前文・附則は、リンクの文字列が「詳細はこちら」で、直前の見出しが
        # 「前文・説明文」「附則」ということがある（評基通・法基通・措通）。
        # ただし直前の見出し（rel）は次の見出しまで残るので、無条件に見ると
        # 以降のページを全部その種別にしてしまう（措通譲渡で52ページが前文になった）。
        # リンクの文字列に手がかりが無いときだけ rel を頼る。
        rel = state["rel"] or ""
        generic = bool(GENERIC_LABEL_RE.match(label.strip()))
        kind = None
        if PREFACE_RE.search(label) or (generic and PREFACE_RE.search(rel)):
            kind = "preface"
        elif FUSOKU_RE.match(label) or (generic and FUSOKU_RE.match(rel)):
            kind = "fusoku"
        elif BETSU_RE.match(label):
            kind = "betsu"
        order.append(
            {
                "page": page,
                "path": path + extra_path,
                "rel": state["rel"],
                "kind": kind,
                "label": label,
            }
        )

    def set_div(title: str) -> bool:
        d = div_of(title)
        if not d:
            return False
        kind, level = d
        del path[level:]
        path.append((kind, title))
        state["rel"] = None
        return True

    def walk_list(lst: Tag, extra: list) -> None:
        """<ul>/<ol> を降りる。リンクを持たない <li> は階層の見出し。"""
        for li in [x for x in lst.find_all("li", recursive=False)]:
            subs = li.find_all(["ul", "ol"], recursive=False)
            own = "".join(t for t in li.find_all(string=True, recursive=False)).strip()
            a = li.find("a", href=True)
            if a is not None and not subs:
                label = a.get_text(" ", strip=True)
                d = div_of(label)
                # 法基通はリーフのリンク自身が階層（第1款 …）。ツリーに階層として出す。
                add(a["href"], label, extra + [(d[0], label)] if d else extra)
                continue
            d = div_of(own) if own else None
            here = extra + [(d[0], own)] if d else extra
            if a is not None:
                add(a["href"], a.get_text(" ", strip=True), here)
            for s in subs:
                walk_list(s, here)

    for el in area.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "ul", "ol", "a"]):
        if el.find_parent(["li", "ul", "ol"]) is not None and el.name in ("ul", "ol"):
            continue  # 入れ子のリストは walk_list が降りる
        if el.name == "a":
            # <p> にも <li> にも入っていない裸のリンク。評基通の目次に実在する
            # （「61　<a href=".../02/12.htm">牧場及び牧場の上に存する権利の評価</a>」）。
            # 拾わないとそのページが丸ごと落ちる。
            if el.get("href") and el.find_parent(["p", "li", "h1", "h2", "h3", "h4", "h5", "h6"]) is None:
                add(el["href"], el.get_text(" ", strip=True), [])
            continue
        if el.name in ("h1", "h2", "h3", "h4", "h5", "h6"):
            title = el.get_text(" ", strip=True)
            # 見出しの中にリンクが入っていることがある（措通の「<h1><a>附則</a></h1>」）。
            # 見出しとして流すだけだとそのページが丸ごと落ちる。
            a = el.find("a", href=True)
            if a is not None and (FUSOKU_RE.match(title) or PREFACE_RE.search(title)):
                state["rel"] = title
                add(a["href"], title, [])
                continue
            if set_div(title):
                continue
            if FUSOKU_RE.match(title) or PREFACE_RE.search(title):
                # 「<h2>附則</h2><p class="more-link"><a>詳細はこちら</a></p>」の形。
                # リンク側に手がかりが無いので、直前の見出しを覚えておく。
                state["rel"] = title
                del path[0:]
            continue
        if el.name == "p":
            if "breadcrumb" in " ".join(el.get("class") or []):
                continue
            title = el.get_text(" ", strip=True)
            links = el.find_all("a", href=True)
            # 評基通は章を h2、節を <p align="center"><strong>第6節　…</strong></p> で書く。
            # 見出しタグだけ見ていると節が丸ごと落ちるので、リンクの無い <p> も階層として見る。
            if not links and set_div(title):
                continue
            if KANKEI_RE.match(title) or FUSOKU_RE.match(title):
                state["rel"] = title
                for a in links:
                    add(a["href"], title, [])
                continue
            # 評基通の目次は番号をリンクの外に置く（<p>別表1　<a>耕作権割合等一覧表</a></p>）。
            # リンクが1本ならその <p> の全文を名前にする。番号・別表の別が名前に要る。
            if len(links) == 1:
                add(links[0]["href"], title, [])
            else:
                for a in links:
                    add(a["href"], a.get_text(" ", strip=True) or title, [])
            continue
        if el.name in ("ul", "ol"):
            if "breadcrumb" in " ".join(el.get("class") or []):
                continue
            walk_list(el, [])

    # 附則・前文は本文の階層に属さない。最上位に出す。
    for o in order:
        if o["kind"]:
            o["path"] = []
    return order


# ---------------------------------------------------------------- 関係法条


def jo_list(num: str) -> list[str]:
    """項目番号 → 関係する法令の条番号。**番号が条を表す通達だけ**に使う。

      2-47        → ["2"]
      36の2-1     → ["36の2"]
      23～35共-1  → ["23", "24", … "35"]（範囲を展開）
      36・37共-1  → ["36", "37"]
      83から84の2-1 → ["83", "84の2"]（枝番を含む範囲は両端のみ）

    法基通の 1-1-1 は「章-節-連番」で条ではない。そういう通達では呼ばない
    （SOURCES の numbering が "article" のものだけ）。
    """
    head = num.split("-", 1)[0].rstrip("共")
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


def build(order: list, pages: dict, numbering: str) -> tuple[list, list]:
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
                if num and numbering == "article":
                    art["ja"] = jo_list(num)
                if not num:
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
    drop_ids: set[int] = set()   # 落とす項目そのもの（オブジェクトの同一性で持つ）
    for aid, group in by_id.items():
        if len(group) < 2:
            continue
        def score(a: dict) -> int:
            want = set(rel_articles(a["_rel"] or ""))
            return 1 if want & set(a.get("ja") or []) else 0
        group.sort(key=score, reverse=True)
        for a in group[1:]:
            a["_holder"].remove(a["_leaf"])
            drop_ids.add(id(a))
            dropped.append({"id": aid, "page": a["page"], "cap": a.get("cap")})
    # 残す項目は「落とす」と決めたオブジェクトそのものを除いて選ぶ。
    # (ページ, 番号) で突き合わせると、同じページに同じ番号が2つある場合に
    # 残すはずの1つも一緒に消え、目次には出るのに本文が無い項目ができる
    # （消基通 16-2-2 がこれで丸ごと消えていた）。
    keep = [a for a in items if id(a) not in drop_ids]
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
    # カードの所属は親法令に合わせる。通達のキーと法令グループは一致しない
    # （評基通のキーは hyoka だが所属は相続税、措通源泉のキーは sochi_gensen だが
    # 所属は租税特別措置法）。
    parent = next((x for x in idx["laws"] if x["key"] == entry["parent"]), None)
    if parent is None:
        raise RuntimeError(f"親法令が index.json にありません: {entry['parent']}")
    entry["group"] = parent["group"]
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
    global CURRENT_BASE
    links = json.loads((RAW_DIR / key / "_links.json").read_text(encoding="utf-8"))
    src = {"base_path": "/" + links["toc_url"].split("//", 1)[1].split("/", 1)[1].rsplit("/", 1)[0] + "/"}
    CURRENT_BASE = src["base_path"]   # この配下の画像だけを図表とみなす
    out_key = f"{key}_tsutatsu"
    out_dir = OUT_DIR / out_key
    if out_dir.exists():
        shutil.rmtree(out_dir)

    # --- 目次
    toc_name = links["toc_url"].rsplit("/", 1)[-1]   # 01.htm / mokuji.htm / index.htm
    toc_html = (RAW_DIR / key / toc_name).read_bytes().decode("cp932", "replace")
    order = parse_toc(toc_html, src["base_path"], {p["path"] for p in links["pages"]})

    numbering = links.get("numbering", "article")
    min_seg = links.get("min_segments", 2)
    by_strong = links.get("item_by_strong", False)
    compose = links.get("compose_kankei", False)

    # --- 各ページ
    pages: dict[str, list] = {}
    figs_all: list = []
    for o in order:
        rel = o["page"]
        html = (RAW_DIR / key / rel).read_bytes().decode("cp932", "replace")
        # 前文・附則は目次のリンク文字列から判定する（parse_toc が kind を付けている）。
        # 「rel が無ければ前文」とはできない。法基通は全ページが rel を持たない。
        pp = PageParser(rel, mode=o["kind"], min_seg=min_seg, label=o.get("label", ""),
                        by_strong=by_strong, compose=compose,
                        toc_name=toc_name if by_strong else "")
        pages[rel] = pp.parse(html)
        figs_all.extend(pp.figs)

    tree, items = build(order, pages, numbering)
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
            "numbering": numbering,
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
        "group": None,   # merge_index が親法令から決める
        "kind": "tsutatsu",
        "name": links["name"],
        "abbr": links["abbr"],
        "parent": links["parent"],
        # 番号から関係法条を導ける通達（所基通 36-1 ＝ 法第36条）だけ、法令側から
        # 「関連通達」リンクを出す。法基通の 1-1-1 は章-節-連番なので出さない。
        "numbering": numbering,
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
