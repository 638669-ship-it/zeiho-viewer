"""e-Gov の全文JSON（data/raw/）を、ビューアが読む階層JSONへ変換し data/json/ に出力する。

  python scripts/parse_laws.py                  # 全24法令
  python scripts/parse_laws.py --only houjin_act shotoku_act

出力（法令1本につき1ディレクトリ）:

  data/json/
    index.json              … 法令一覧・色・略称辞書・ビルド情報（ビューアが最初に読む）
    {key}/toc.json          … 目次ツリー＋条→チャンク索引（法令を開いた時に読む）
    {key}/main-NN.json      … 本則の条文本体（クリックされた条を含む1本だけ読む）
    {key}/suppl.json        … 附則の索引（改正法令ごと）
    {key}/suppl-NN.json     … 附則の本体
    {key}/appdx.json        … 別表・様式

分割の根拠（実測）：附則が全体の8割を占める（所得税法は16.4MBのうち本則は0.79MB）。
本則・附則・別表を分けるだけで、通常の閲覧で読むデータが1桁減る。

e-Gov の元データは無加工で data/raw/ に保持し、ここでは表示用の正規化のみを行う
（指示書 9-4：括弧の色分けは表示時のクライアント側処理）。

標準ライブラリのみで動作する。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MASTER = ROOT / "laws_master.json"
RAW_DIR = ROOT / "data" / "raw"

# 指示書4の構成案は data/json/ に出力して docs/data/ へコピーする形だが、
# それだと同じ66MBをリポジトリに二重に持つことになる。ビューア（GitHub Pages）も
# ローカルzip版も docs/ 配下だけで完結するので、最初から docs/data/ に出力する。
OUT_DIR = ROOT / "docs" / "data"

# 本則チャンクの目安サイズ。1条が極端に大きい場合(措法42の4等)は1条で超過してよい。
CHUNK_BYTES = 350_000

# 見出し階層タグ → 表示上の区分名
DIVISION_TAGS = {
    "Part": ("PartTitle", "編"),
    "Chapter": ("ChapterTitle", "章"),
    "Section": ("SectionTitle", "節"),
    "Subsection": ("SubsectionTitle", "款"),
    "Division": ("DivisionTitle", "目"),
}
SUBITEM_TAGS = ["Subitem1", "Subitem2", "Subitem3", "Subitem4"]

# 条見出し（ArticleCaption）が無い条で、見出しの代わりに使ってよい階層。
# 節・款・目までとし、編・章は使わない（「第一章　総則」を見出しにすると誤読を招く）。
CAPTION_FALLBACK_KINDS = {"節", "款", "目"}

# 「第二款　各事業年度の所得の金額の計算の通則」→「各事業年度の所得の金額の計算の通則」
DIVISION_PREFIX_RE = re.compile(r"^第[一二三四五六七八九十百千]+(?:の[一二三四五六七八九十]+)?[編章節款目]\s*[　\s]*")


# ---------------------------------------------------------------- テキスト抽出


def text_of(node) -> str:
    """ノード配下の地の文を連結する。

    Ruby（ルビ）は親字だけを採り、Rt（読み仮名）は落とす。そうしないと
    「租税（そぜい）」が「租税そぜい」になって本文が壊れる。
    """
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ""
    if node.get("tag") == "Rt":
        return ""
    return "".join(text_of(c) for c in (node.get("children") or []))


def children(node, tag: str) -> list:
    return [c for c in (node.get("children") or []) if isinstance(c, dict) and c.get("tag") == tag]


def child(node, tag: str):
    hits = children(node, tag)
    return hits[0] if hits else None


def child_text(node, tag: str) -> str:
    c = child(node, tag)
    return text_of(c).strip() if c is not None else ""


def sentences_text(node) -> str:
    """ParagraphSentence / ItemSentence 等の直下の Sentence を連結する。

    Sentence/@Function は main（本文）と proviso（ただし書）の2値だが、
    印刷六法でも両者は地続きに組まれるため連結して1つの文とする。
    """
    return "".join(text_of(s) for s in children(node, "Sentence")).strip()


# ---------------------------------------------------------------- 条番号の正規化


def article_label(num: str) -> str:
    """Article/@Num → 人が読む短い条番号。

    "22"    → "22"        第二十二条
    "22_2"  → "22の2"     第二十二条の二（枝番は _ 連結）
    "4:7"   → "4〜7"      第四条から第七条まで（削除等でまとめられた範囲）
    """
    if ":" in num:
        lo, hi = num.split(":", 1)
        return f"{article_label(lo)}〜{article_label(hi)}"
    return num.replace("_", "の")


def article_id(num: str) -> str:
    """URLハッシュ・DOM id に使える形。":" は使えないので "-" に寄せる。"""
    return "a" + num.replace(":", "-")


def article_sort_key(num: str):
    """条番号の自然順ソート用キー。"22_2" は "22" と "23" の間に来る。"""
    head = num.split(":", 1)[0]
    return tuple(int(x) if x.isdigit() else 0 for x in head.split("_"))


# ---------------------------------------------------------------- 構造の変換


def parse_table(struct: dict) -> dict:
    """TableStruct → {title, rows:[[{t,rs,cs}]], remarks:[]}"""
    out: dict = {}
    title = child_text(struct, "TableStructTitle")
    if title:
        out["title"] = title
    rows = []
    for table in children(struct, "Table"):
        for tr in children(table, "TableRow"):
            cells = []
            for td in children(tr, "TableColumn"):
                cell: dict = {"t": text_of(td).strip()}
                rs, cs = (td.get("attr") or {}).get("rowspan"), (td.get("attr") or {}).get("colspan")
                if rs:
                    cell["rs"] = int(rs)
                if cs:
                    cell["cs"] = int(cs)
                cells.append(cell)
            rows.append(cells)
    out["rows"] = rows
    remarks = []
    for rm in children(struct, "Remarks"):
        label = child_text(rm, "RemarksLabel")
        body = "".join(text_of(s) for s in children(rm, "Sentence")).strip()
        body += "".join(parse_item(it, "Item")["t"] for it in children(rm, "Item"))
        remarks.append((label + " " + body).strip())
    if remarks:
        out["remarks"] = remarks
    return out


def parse_list(node: dict) -> str:
    """List → ListSentence の連結（実データでは85件のみ・単純な列記）。"""
    return "".join(sentences_text(ls) for ls in children(node, "ListSentence")).strip()


def parse_blocks(node: dict) -> list:
    """Paragraph / Item 配下の表・列記を拾う。"""
    blocks = []
    for ts in children(node, "TableStruct"):
        blocks.append({"kind": "table", **parse_table(ts)})
    for ls in children(node, "List"):
        t = parse_list(ls)
        if t:
            blocks.append({"kind": "list", "t": t})
    return blocks


def parse_item(node: dict, tag: str) -> dict:
    """Item / Subitem1〜4 → {n, t, cols?, items?, blocks?}"""
    title_tag, sent_tag = tag + "Title", tag + "Sentence"
    out: dict = {"n": child_text(node, title_tag)}

    sent = child(node, sent_tag)
    if sent is not None:
        cols = children(sent, "Column")
        if cols:
            # 定義規定（法法2等）の二段組み。Column/@Num 順に並べる。
            out["cols"] = [text_of(c).strip() for c in cols]
            out["t"] = "　".join(out["cols"])
        else:
            out["t"] = sentences_text(sent)
    else:
        out["t"] = ""

    kids = []
    for sub in SUBITEM_TAGS:
        for s in children(node, sub):
            kids.append(parse_item(s, sub))
    if kids:
        out["items"] = kids
    blocks = parse_blocks(node)
    if blocks:
        out["blocks"] = blocks
    return out


def parse_paragraph(node: dict) -> dict:
    """Paragraph → {n, cap?, t, items?, blocks?}

    ParagraphNum のテキストは第1項では空（"" ）で、第2項以降だけ "２" が入る。
    モックHTMLが第1項の項番号バッジを出さないのはこの仕様と一致する。
    """
    out: dict = {"n": (node.get("attr") or {}).get("Num", "1")}
    cap = child_text(node, "ParagraphCaption")
    if cap:
        out["cap"] = cap
    ps = child(node, "ParagraphSentence")
    out["t"] = sentences_text(ps) if ps is not None else ""
    items = [parse_item(i, "Item") for i in children(node, "Item")]
    if items:
        out["items"] = items
    blocks = parse_blocks(node)
    if blocks:
        out["blocks"] = blocks
    return out


def derive_caption(path: list[tuple[str, str]]) -> str:
    """条見出しが無い条について、直近の節・款・目の名称を見出しとして借りる。

    実データでは条見出しの欠落は「その条だけ見出しが無い」のではなく、
    「款・目の名称がそのまま見出しの役割を負っている」ケースがほとんど。
      法人税法22条  → 第二款「各事業年度の所得の金額の計算の通則」
      措置法66条の6 → 第一款「内国法人の外国関係会社に係る所得の課税の特例」
    いずれも印刷六法で当該条の見出しとして掲げられているものと一致する。
    借りたものだと分かるよう、呼び出し側で capd フラグを立てて区別表示する。
    """
    for kind, title in reversed(path):
        if kind in CAPTION_FALLBACK_KINDS:
            name = DIVISION_PREFIX_RE.sub("", title).strip()
            if name:
                return f"（{name}）"
    return ""


def parse_article(node: dict, path: list[tuple[str, str]]) -> dict:
    """Article → {num, id, label, title, cap?, capd?, path, paras}"""
    num = (node.get("attr") or {}).get("Num", "")
    out = {
        "num": num,
        "id": article_id(num),
        "label": article_label(num),
        "title": child_text(node, "ArticleTitle"),
        "path": [t for _, t in path],
        "paras": [parse_paragraph(p) for p in children(node, "Paragraph")],
    }
    cap = child_text(node, "ArticleCaption")
    if cap:
        out["cap"] = cap
    else:
        derived = derive_caption(path)
        if derived:
            out["cap"] = derived
            out["capd"] = 1  # 款・目名から補完した見出し（原文の条見出しではない）
    return out


def walk_divisions(node: dict, path: list[tuple[str, str]], articles: list, toc: list) -> None:
    """MainProvision 配下を再帰して、目次ツリー(toc)と条の一覧(articles)を同時に作る。

    実データの入れ子は Part > Chapter > Section > Subsection > Division > Article。
    ただし小さい法令（国税徴収法施行規則等）は MainProvision 直下に Article が来る。
    """
    for c in node.get("children") or []:
        if not isinstance(c, dict):
            continue
        tag = c.get("tag")
        if tag in DIVISION_TAGS:
            title_tag, kind = DIVISION_TAGS[tag]
            title = child_text(c, title_tag)
            sub: list = []
            walk_divisions(c, path + [(kind, title)], articles, sub)
            toc.append({"k": kind, "t": title, "c": sub})
        elif tag == "Article":
            art = parse_article(c, path)
            articles.append(art)
            leaf = {
                "k": "条",
                "id": art["id"],
                "l": art["label"],
                "t": art["title"],
            }
            if art.get("cap"):
                leaf["cap"] = art["cap"]
            if art.get("capd"):
                leaf["capd"] = 1
            toc.append(leaf)


def parse_suppl(node: dict) -> dict:
    """SupplProvision → 1本の附則。

    条番号は法令内で一意ではない（附則にも第1条がある）。改正法令番号
    （@AmendLawNum）ごとに分けて持ち、本則の条番号ジャンプとは混ぜない。
    """
    attr = node.get("attr") or {}
    out: dict = {
        "label": child_text(node, "SupplProvisionLabel"),
        "amend": attr.get("AmendLawNum"),
        "extract": attr.get("Extract") == "true",
        "arts": [parse_article(a, []) for a in children(node, "Article")],
        # 附則は条を立てず項だけのものが多い（実測3,866項）
        "paras": [parse_paragraph(p) for p in children(node, "Paragraph")],
    }
    tables = []
    for t in children(node, "SupplProvisionAppdxTable"):
        tt = child_text(t, "SupplProvisionAppdxTableTitle")
        for ts in children(t, "TableStruct"):
            tables.append({"title": tt, **parse_table(ts)})
    if tables:
        out["tables"] = tables
    return out


def parse_appdx(body: dict) -> list:
    """別表（AppdxTable）・様式（AppdxStyle / AppdxFormat）。

    様式は中身が Fig（画像）で、e-Gov の添付ファイルAPIを叩かないと実体が無い
    （今回の取得では attached_files_info は null）。表題と関係条文だけ載せ、
    原文リンクへ送る。
    """
    out = []
    for t in children(body, "AppdxTable"):
        entry = {
            "kind": "table",
            "title": child_text(t, "AppdxTableTitle"),
            "rel": child_text(t, "RelatedArticleNum"),
            "tables": [parse_table(ts) for ts in children(t, "TableStruct")],
        }
        items = [parse_item(i, "Item") for i in children(t, "Item")]
        if items:
            entry["items"] = items
        out.append(entry)
    for tag, title_tag, kind in (
        ("AppdxStyle", "AppdxStyleTitle", "style"),
        ("AppdxFormat", "AppdxFormatTitle", "format"),
    ):
        for s in children(body, tag):
            out.append(
                {
                    "kind": kind,
                    "title": child_text(s, title_tag),
                    "rel": child_text(s, "RelatedArticleNum"),
                    "note": "様式は画像で提供されているため、e-Gov原文を参照してください。",
                }
            )
    return out


# ---------------------------------------------------------------- 分割・書き出し


def jsize(obj) -> int:
    return len(json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def write_json(path: Path, obj) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    path.write_bytes(data)
    return len(data)


def chunk_articles(articles: list) -> list[list]:
    """条を出現順に、CHUNK_BYTES を目安にまとめる。

    どの条がどのチャンクかは toc.json が持つので、境界は章に揃えなくてよい。
    条番号ジャンプは「その条を含む1チャンク」だけを取りに行けばよい。
    """
    chunks: list[list] = []
    cur: list = []
    cur_size = 0
    for a in articles:
        s = jsize(a)
        if cur and cur_size + s > CHUNK_BYTES:
            chunks.append(cur)
            cur, cur_size = [], 0
        cur.append(a)
        cur_size += s
    if cur:
        chunks.append(cur)
    return chunks or [[]]


def index_toc_chunks(toc: list, where: dict) -> None:
    """目次ツリーの条ノードに、その条が入っているチャンク番号(f)を書き込む。"""
    for n in toc:
        if n.get("k") == "条":
            n["f"] = where[n["id"]]
        elif n.get("c"):
            index_toc_chunks(n["c"], where)


def parse_law(law: dict) -> dict:
    """1法令を変換して data/json/{key}/ に書き出し、index.json 用の要約を返す。"""
    raw = json.loads((RAW_DIR / f"{law['law_id']}.json").read_text(encoding="utf-8"))
    rev = raw.get("revision_info") or {}
    body = None
    for c in raw["law_full_text"].get("children") or []:
        if isinstance(c, dict) and c.get("tag") == "LawBody":
            body = c
            break
    if body is None:
        raise RuntimeError(f"LawBody が見つかりません: {law['key']}")

    out_dir = OUT_DIR / law["key"]
    if out_dir.exists():
        shutil.rmtree(out_dir)

    # --- 本則
    main = child(body, "MainProvision")
    articles: list = []
    toc: list = []
    walk_divisions(main, [], articles, toc)

    chunks = chunk_articles(articles)
    where = {a["id"]: i for i, ch in enumerate(chunks) for a in ch}
    index_toc_chunks(toc, where)
    bytes_main = 0
    for i, ch in enumerate(chunks):
        bytes_main += write_json(out_dir / f"main-{i:02d}.json", {"arts": ch})

    # --- 附則（改正法令ごと。分量が大きいので索引と本体を分ける）
    suppls = [parse_suppl(s) for s in children(body, "SupplProvision")]
    sup_chunks = []
    cur: list = []
    cur_size = 0
    for s in suppls:
        sz = jsize(s)
        if cur and cur_size + sz > CHUNK_BYTES:
            sup_chunks.append(cur)
            cur, cur_size = [], 0
        cur.append(s)
        cur_size += sz
    if cur:
        sup_chunks.append(cur)

    sup_index = []
    bytes_sup = 0
    for i, ch in enumerate(sup_chunks):
        bytes_sup += write_json(out_dir / f"suppl-{i:02d}.json", {"sups": ch})
        for j, s in enumerate(ch):
            sup_index.append(
                {
                    "label": s["label"],
                    "amend": s["amend"],
                    "f": i,
                    "i": j,
                    "n": len(s["arts"]) + len(s["paras"]),
                }
            )
    if sup_index:
        bytes_sup += write_json(out_dir / "suppl.json", {"list": sup_index})

    # --- 別表・様式
    appdx = parse_appdx(body)
    bytes_appdx = write_json(out_dir / "appdx.json", {"appdx": appdx}) if appdx else 0

    # --- 目次
    bytes_toc = write_json(
        out_dir / "toc.json",
        {
            "key": law["key"],
            "name": law["name"],
            "abbr": law["abbr"],
            "law_id": law["law_id"],
            "law_num": law["law_num"],
            "enforced": rev.get("amendment_enforcement_date"),
            "updated": rev.get("updated"),
            "chunks": len(chunks),
            "toc": toc,
            "arts": [{"id": a["id"], "l": a["label"], "f": where[a["id"]]} for a in articles],
            "has_suppl": bool(sup_index),
            "suppl_chunks": len(sup_chunks),
            "has_appdx": bool(appdx),
        },
    )

    return {
        "key": law["key"],
        "group": law["group"],
        "kind": law["kind"],
        "name": law["name"],
        "abbr": law["abbr"],
        "law_id": law["law_id"],
        "law_num": law["law_num"],
        "enforced": rev.get("amendment_enforcement_date"),
        "articles": len(articles),
        "suppls": len(suppls),
        "appdx": len(appdx),
        "chunks": len(chunks),
        "_bytes": {
            "toc": bytes_toc,
            "main": bytes_main,
            "suppl": bytes_sup,
            "appdx": bytes_appdx,
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", help="laws_master.json の key を指定して部分変換")
    args = ap.parse_args()

    master = json.loads(MASTER.read_text(encoding="utf-8"))
    laws = master["laws"]
    if args.only:
        laws = [x for x in laws if x["key"] in args.only]
        if not laws:
            print("該当する key がありません", file=sys.stderr)
            return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    summaries = []
    print(f"{'法令':<22}{'本則条':>7}{'附則':>6}{'別表':>5}{'chunk':>7}{'本則KB':>9}{'附則KB':>9}")
    for law in laws:
        s = parse_law(law)
        summaries.append(s)
        b = s["_bytes"]
        print(
            f"{s['abbr'] + ' ' + s['name']:<22}{s['articles']:>7}{s['suppls']:>6}"
            f"{s['appdx']:>5}{s['chunks']:>7}{b['main'] / 1024:>9.0f}{b['suppl'] / 1024:>9.0f}"
        )

    total = sum(sum(s["_bytes"].values()) for s in summaries)

    # --- index.json（ビューアが最初に読む1本）
    if not args.only:
        for s in summaries:
            s.pop("_bytes", None)
        index = {
            "built_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "source": master["source"],
            "kinds": master["kinds"],
            "groups": master["groups"],
            "laws": summaries,
            # 「法法22」形式のジャンプ用。略称 → 法令key。
            "abbr": {s["abbr"]: s["key"] for s in summaries},
        }
        n = write_json(OUT_DIR / "index.json", index)
        print(f"\nindex.json {n / 1024:.1f}KB")

    print(f"合計 {total / 1e6:.1f}MB → {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
