"""data/raw/ の取得結果を検収する。

  python scripts/verify_raw.py

Phase 0 の検収基準：
  * 24本すべての生JSONが存在し、JSONとして読めること
  * 本則の条数が官報ベースの常識と齟齬ないこと（条番号の連続性・枝番の有無で確認）
  * 指示書の検収対象条文（所法56・法法22・措法42の4・66の6 等）が実在すること
  * 指示書 5-2：地方税法・措置法の分量を計測し Phase 1 の分割戦略の材料にする
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
MASTER = ROOT / "laws_master.json"
RAW_DIR = ROOT / "data" / "raw"

# 「この条が引けなければ話にならない」ランドマーク（指示書 3・8・9 より）
LANDMARKS = {
    "shotoku_act": ["56", "57"],
    "houjin_act": ["21", "22", "22_2"],
    "sochi_act": ["42_4", "66_6"],
    "chihou_act": ["1", "72_2"],
    "shohi_act": ["30"],
    "souzoku_act": ["22"],
    "tsusoku_act": ["23"],
    "choshu_act": ["8"],
}


def find_tag(node, tag):
    if isinstance(node, dict):
        if node.get("tag") == tag:
            return node
        for v in node.values():
            hit = find_tag(v, tag)
            if hit is not None:
                return hit
    elif isinstance(node, list):
        for v in node:
            hit = find_tag(v, tag)
            if hit is not None:
                return hit
    return None


def iter_tag(node, tag):
    """指定タグのノードを再帰的に列挙する。"""
    if isinstance(node, dict):
        if node.get("tag") == tag:
            yield node
        for v in node.values():
            yield from iter_tag(v, tag)
    elif isinstance(node, list):
        for v in node:
            yield from iter_tag(v, tag)


def num_sort_key(num: str):
    """'22_2' → (22, 2) のように枝番を数値順に並べる。

    e-Gov の Article/@Num は次の3形式をとる（実データで確認）：
      * "22"    … 第二十二条
      * "22_2"  … 第二十二条の二（枝番。'_' で連結）
      * "4:7"   … 第四条から第七条まで（「削除」等でまとめられた範囲。':' で連結）
    """
    head = num.split(":")[0]
    try:
        return tuple(int(x) for x in head.split("_"))
    except ValueError:
        return (10**9,)


def base_numbers(num: str) -> list[int]:
    """条番号文字列が占める本条番号を列挙する（範囲表記を展開する）。"""
    parts = num.split(":")
    try:
        start = int(parts[0].split("_")[0])
        if len(parts) == 1:
            return [start]
        end = int(parts[-1].split("_")[0])
        return list(range(start, end + 1))
    except ValueError:
        return []


def main() -> int:
    master = json.loads(MASTER.read_text(encoding="utf-8"))
    problems: list[str] = []
    total_bytes = 0

    print(
        f"{'略称':<5}{'法令名':<24}{'MB':>7}{'本則条数':>8}{'最大条番号':>10}"
        f"  枝番  範囲  条番号の連続性"
    )
    print("-" * 104)

    for law in master["laws"]:
        path = RAW_DIR / f"{law['law_id']}.json"
        if not path.exists():
            problems.append(f"{law['name']}: ファイルなし ({path.name})")
            continue

        size = path.stat().st_size
        total_bytes += size
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            problems.append(f"{law['name']}: JSON解析失敗 {e}")
            continue

        full = d.get("law_full_text")
        main = find_tag(full, "MainProvision")
        if main is None:
            problems.append(f"{law['name']}: MainProvision が見つからない")
            continue

        nums = [a.get("attr", {}).get("Num", "") for a in iter_tag(main, "Article")]
        nums = [n for n in nums if n]
        if not nums:
            problems.append(f"{law['name']}: 本則に Article が0件")
            continue

        covered: set[int] = set()
        for n in nums:
            covered.update(base_numbers(n))
        if not covered:
            problems.append(f"{law['name']}: 条番号を解釈できない（例: {nums[:3]}）")
            continue

        eda = sum(1 for n in nums if "_" in n)
        rng = sum(1 for n in nums if ":" in n)
        # 本条番号（枝番の親）が1から連番で通っているか
        gaps = [x for x in range(1, max(covered) + 1) if x not in covered]
        cont = "連続" if not gaps else f"欠番{len(gaps)}件(例:{gaps[:3]})"

        print(
            f"{law['abbr']:<5}{law['name']:<24}{size / 1024 / 1024:>7.2f}"
            f"{len(nums):>8}{max(nums, key=num_sort_key):>10}  {eda:>4}{rng:>5}  {cont}"
        )

        # ランドマーク条文の存在確認
        for want in LANDMARKS.get(law["key"], []):
            if want not in nums:
                problems.append(f"{law['name']}: 第{want.replace('_', 'の')}条が見つからない")

    print("-" * 104)
    print(f"生データ合計: {total_bytes / 1024 / 1024:.1f} MB / {len(master['laws'])}本\n")

    # 指示書 5-2：分割戦略のための分量計測
    print("=== 分量上位（Phase 1 の分割・遅延読み込み設計の材料）===")
    sizes = []
    for law in master["laws"]:
        p = RAW_DIR / f"{law['law_id']}.json"
        if p.exists():
            sizes.append((p.stat().st_size, law["name"]))
    for s, n in sorted(sizes, reverse=True)[:6]:
        print(f"  {n:<24}{s / 1024 / 1024:>7.2f} MB")

    print()
    if problems:
        print("!! 検収NG:")
        for p in problems:
            print("   -", p)
        return 1
    print("検収OK：24本すべて取得済み・ランドマーク条文すべて実在")
    return 0


if __name__ == "__main__":
    sys.exit(main())
