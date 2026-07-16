"""通達の変換結果（docs/data/{key}_tsutatsu/）を原文HTMLと突き合わせて検証する。

  python scripts/verify_tsutatsu.py
  python scripts/verify_tsutatsu.py --only shotoku

指示書 Step4 の検収基準を機械的に確認する:

  1. 目次リンク数（アンカー除去後）＝取得ページ数＝パース成功ページ数（欠落ゼロ）
  2. 文字化けゼロ（cp932変換もれ＝置換文字 U+FFFD が0件）
  3. 目次ツリーの葉・番号ジャンプの索引・本文チャンクの項目が過不足なく一致すること
  4. 各項目の本文が、原文HTMLのテキストに「文字列としてそのまま含まれる」こと

4 は欠落と改変の両方を捕まえる。項目の本文を連結したものが原文の部分文字列に
ならなければ、段落を落としたか、順序を崩したか、文字を書き換えたかのいずれか。
実際にこの検査で、番号判定用の全角→半角正規化が本文にまで及んで
「令７課個2-10」を「令7課個2-10」に書き換えていた不具合（213項目）を検出した。

3 は 4 では捕まらない穴を塞ぐ。落ちた項目は突合の対象にも入らないので
「全項目一致」でも見逃せてしまう。消基通 16-2-2 は目次に出るのに本文が無い状態で
検収を通っていた（重複除外が、残すはずの1つも巻き添えにしていた）。

比較の前処理は最小限にとどめる（空白の無視と、字形画像→文字の復元のみ）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw_tsutatsu"
OUT_DIR = ROOT / "docs" / "data"

# 国税庁のHTMLは <p> の閉じ忘れが多い。標準の html.parser は閉じ忘れを補正しないため、
# 評基通の目次では245本のリンクと30個の節見出しが1つの <p> に飲み込まれ、
# 階層も順序も壊れた（最大の <p> が6,041字）。html5lib はブラウザと同じHTML5の
# 解釈で構造を組み直すので、同じ <p> が56字になり、節見出しとリンクが正しく並ぶ。
HTML_PARSER = "html5lib"

WS_RE = re.compile(r"\s|　")


def squeeze(s: str) -> str:
    return WS_RE.sub("", s)


def node_text(n: dict) -> str:
    """出力JSONの1段落（項・号）を、表示順どおりの地の文にする。

    図表（fig）は原文では <img alt="…"> なので、原文側の alt と突き合わせられるよう
    alt を地の文として並べる。
    """
    s = n.get("t", "")
    for b in n.get("blocks") or []:
        if b.get("kind") == "table":
            s += b.get("title", "")   # <caption>（原文でも行より前に出る）
            for row in b.get("rows") or []:
                for c in row:
                    s += c["t"]
        elif b.get("kind") == "fig":
            s += b.get("alt", "")
    for it in n.get("items") or []:
        s += it.get("n", "") + node_text(it)
    return s


def para_text(p: dict) -> str:
    """1項（Paragraph）の突合用テキスト。項番号（2・3…）は原文にも出るので前に付ける。

    第1項の項番号は原文でも空（印刷六法と同じ）なので n="1" のときは付けない。
    印基通の逐条項目は「第3条 …する。2 前項における…」のように条内に項2以降を持ち、
    項番号を落とすと項1と項2の連結が原文とずれる（実測20項目）。
    """
    n = p.get("n", "")
    return (n if n and n != "1" else "") + node_text(p)


def item_text(a: dict) -> str:
    return squeeze("".join(para_text(p) for p in a["paras"]))


def item_chunks(a: dict) -> list[str]:
    """突合の単位。

    通常の項目は本文を1本につないで原文と照合する（順序の崩れも捕まえる）。
    前文と附則は原文の見出し（h1/h2/h3）が本文の途中に挟まるが、出力では
    label・cap に分けて持つため1本にはつながらない。段落ごとに照合する。
    """
    if a.get("fusoku"):
        return [squeeze(para_text(p)) for p in a["paras"]]
    return [item_text(a)]


def raw_text(path: Path) -> str:
    """原文HTMLの本文テキスト。<img> は alt の文字に置き換える。

    図表（その通達のディレクトリ配下の画像）の alt は出力側でも本文として並べ、
    字形画像（/shared/ /category/ /top_img/）の alt は本文に埋め込むので、
    どちらも alt に揃えれば突合できる。
    """
    soup = BeautifulSoup(path.read_bytes().decode("cp932", "replace"), HTML_PARSER)
    area = soup.find(id="bodyArea") or soup
    for im in area.find_all("img"):
        src = im.get("src", "")
        im.replace_with("補塡" if "hoten" in src else (im.get("alt") or ""))
    return squeeze(area.get_text("", strip=False))


def verify(key: str) -> int:
    links = json.loads((RAW_DIR / key / "_links.json").read_text(encoding="utf-8"))
    out_dir = OUT_DIR / f"{key}_tsutatsu"
    toc = json.loads((out_dir / "toc.json").read_text(encoding="utf-8"))
    arts: list = []
    for p in sorted(out_dir.glob("main-*.json")):
        arts += json.loads(p.read_text(encoding="utf-8"))["arts"]

    print(f"=== {toc['abbr']} {toc['name']} ===")
    bad = 0

    # --- 1. 欠落ゼロ（目次が挙げたページ＝取得したページ＝項目を取り出せたページ）
    toc_page = links["toc_url"].rsplit("/", 1)[-1]
    listed = links["page_count"]
    fetched = len([p for p in (RAW_DIR / key).rglob("*.htm") if p.name != toc_page or p.parent != RAW_DIR / key])
    parsed = len({a["page"] for a in arts})
    ok = listed == fetched == parsed
    print(f"[{'OK' if ok else '!!'}] 目次リンク {listed} / 取得 {fetched} / パース {parsed}")
    if not ok:
        missing = {p["path"] for p in links["pages"]} - {a["page"] for a in arts}
        print(f"     項目を取り出せなかったページ: {sorted(missing)}")
    bad += 0 if ok else 1

    # --- 2. 文字化けゼロ
    n_bad = sum(p.read_text(encoding="utf-8").count("�") for p in out_dir.rglob("*.json"))
    print(f"[{'OK' if not n_bad else '!!'}] 文字化け（置換文字）{n_bad}件")
    bad += 0 if not n_bad else 1

    # --- 3. 目次ツリーと本文の整合
    #
    # 目次に出るのに開けない項目が無いこと。ビューアは目次ツリーの葉を押して
    # 本文チャンクを引くので、片方にしか無いと「目次にあるのに本文が無い」ことになる。
    # 実際に消基通 16-2-2 がこれで消えていた（重複除外が残すはずの1つも巻き添えにした）。
    leaves: list[dict] = []
    def walk(nodes: list) -> None:
        for n in nodes:
            if n.get("k") == "条":
                leaves.append(n)
            elif n.get("c"):
                walk(n["c"])
    walk(toc["toc"])
    body_ids = {a["id"] for a in arts}
    leaf_ids = {n["id"] for n in leaves}
    index_ids = {a["id"] for a in toc["arts"]}

    orphan = leaf_ids - body_ids            # 目次にあるのに本文が無い
    unlisted = body_ids - leaf_ids          # 本文にあるのに目次に無い
    lost_idx = index_ids - body_ids         # 番号ジャンプの索引にあるのに本文が無い
    ok3 = not (orphan or unlisted or lost_idx)
    print(
        f"[{'OK' if ok3 else '!!'}] 目次と本文の整合：目次の葉 {len(leaf_ids)} / 本文 {len(body_ids)}"
        f" / ジャンプ索引 {len(index_ids)}"
    )
    for label, s in (("目次にあるのに本文が無い", orphan), ("本文にあるのに目次に無い", unlisted),
                     ("ジャンプ索引にあるのに本文が無い", lost_idx)):
        if s:
            print(f"     {label}: {len(s)}件 {sorted(s)[:8]}")
    bad += 0 if ok3 else 1

    # --- 3. 原文との突合
    cache: dict[str, str] = {}
    mismatch = []
    chars = 0
    for a in arts:
        page = a["page"]
        if page not in cache:
            cache[page] = raw_text(RAW_DIR / key / page)
        raw = cache[page]
        chars += len(item_text(a))
        for t in item_chunks(a):
            if t and t not in raw:
                mismatch.append((a, t))
                break
    print(
        f"[{'OK' if not mismatch else '!!'}] 原文と突合：項目 {len(arts)} 件中 "
        f"一致 {len(arts) - len(mismatch)} / 不一致 {len(mismatch)}（本文 {chars:,} 字）"
    )
    for a, t in mismatch[:10]:
        raw = cache[a["page"]]
        k = next((i for i in range(len(t)) if t[: i + 1] not in raw), len(t))
        print(f"     {a['label']}（{a['page']}）位置{k}: {t[max(0, k - 30) : k + 20]!r}")
    bad += 0 if not mismatch else 1

    # --- 参考：本文が空の項目
    empty = [a["label"] for a in arts if not item_text(a)]
    if empty:
        print(f"[??] 本文が空の項目 {len(empty)}件: {empty[:8]}")

    print(f"掲載時点 {toc['published_as_of']}／出典 {toc['source_url']}")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*")
    args = ap.parse_args()
    keys = [p.name for p in sorted(RAW_DIR.iterdir()) if (p / "_links.json").exists()]
    if args.only:
        keys = [k for k in args.only if k in keys]
    bad = 0
    for k in keys:
        bad += verify(k)
    print("\n" + ("検収項目はすべて通りました。" if not bad else f"!! {bad} 項目が基準を満たしていません。"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
