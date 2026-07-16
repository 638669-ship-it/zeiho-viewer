"""e-Gov と国税庁を見て、前回取得時から変わったものだけを挙げる（Phase 3・月次更新の入口）。

  python scripts/check_updates.py             # 変わったものを表示
  python scripts/check_updates.py --json      # 機械可読（GitHub Actions 用）
  python scripts/check_updates.py --github    # $GITHUB_OUTPUT にも書く

何と何を比べるか:

  法令   e-Gov の law_revision_id（例 340AC0000000033_20260401_507AC0000000013）を
         data/raw/_fetch_log.json の値と比べる。改正のたびに変わる ID なので、
         施行日だけを見るより確実。全文（97MB）を落とさずに /law_revisions で引ける。

  通達   国税庁の掲載時点の文言（「令和8年3月31日付通達まで掲載」）を
         data/raw_tsutatsu/{key}/_links.json の published_as_of と比べる。
         通達にはIDも更新日時も無いので、これが唯一の目印。

どちらの比較元もリポジトリにコミットされているので、生データが無いCI上でも動く。
変わったものだけを再取得すればよく、国税庁への負荷も最小限で済む。

標準ライブラリ＋beautifulsoup4 で動作する（fetch_tsutatsu の関数を借りる）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_tsutatsu import SOURCES, fetch_published_as_of  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MASTER = ROOT / "laws_master.json"
RAW_DIR = ROOT / "data" / "raw"
RAW_TSU = ROOT / "data" / "raw_tsutatsu"

EGOV = "https://laws.e-gov.go.jp/api/2"
UA = "zeiho-viewer/0.3 (personal tax-law reference tool; contact via https://github.com/638669-ship-it/zeiho-viewer)"
SLEEP_SEC = 2.0
RETRY = 3
TIMEOUT = 90


def egov_revision(law_id: str, today: str) -> dict | None:
    """全文を落とさずに、いま施行されている版の revision_info を引く。

    /laws?law_id= は使えない。最新版を返さないため（実測：地方税法で施行 2026-05-21 の版を
    返したが、実際に施行中の最新版は 2026-06-05 のもの）。そのまま比べると毎月「改正あり」と
    誤判定してしまう。

    /law_revisions/{law_id} は全版を施行日の新しい順に返す（将来施行分＝2117年施行まで含む）。
    そこから「施行日が今日以前の最初の版」を採ると、fetch_laws.py が /law_data で取る版と
    一致する（24法令すべてで一致することを実地確認した）。
    """
    url = f"{EGOV}/law_revisions/{urllib.parse.quote(law_id)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    last: Exception | None = None
    for attempt in range(1, RETRY + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                d = json.loads(r.read().decode("utf-8"))
            revs = d.get("revisions") or []
            return next(
                (r for r in revs if (r.get("amendment_enforcement_date") or "9999") <= today),
                None,
            )
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            last = RuntimeError(f"HTTP {e.code}")
        except Exception as e:  # noqa: BLE001
            last = e
        if attempt < RETRY:
            time.sleep(SLEEP_SEC * (2**attempt))
    raise RuntimeError(f"e-Gov 照会に失敗 {law_id}: {last}")


def check_laws() -> tuple[list[dict], list[str]]:
    """法令：law_revision_id が変わったものを挙げる。"""
    master = json.loads(MASTER.read_text(encoding="utf-8"))
    log_path = RAW_DIR / "_fetch_log.json"
    if not log_path.exists():
        return [], ["data/raw/_fetch_log.json が無いため法令の比較をとばした"]
    log = json.loads(log_path.read_text(encoding="utf-8"))

    today = datetime.now(timezone.utc).astimezone().date().isoformat()
    changed, notes = [], []
    for law in master["laws"]:
        key, law_id = law["key"], law["law_id"]
        known = log.get(key) or {}
        rev = egov_revision(law_id, today)
        time.sleep(SLEEP_SEC)
        if rev is None:
            notes.append(f"{key}: e-Gov に見当たらない（法令IDの変更か廃止の可能性）")
            continue
        now, before = rev.get("law_revision_id"), known.get("law_revision_id")
        if before and now != before:
            changed.append({
                "key": key,
                "name": law["name"],
                "before": before,
                "after": now,
                "enforced": rev.get("amendment_enforcement_date"),
                "amend": rev.get("amendment_law_title"),
            })
        elif not before:
            notes.append(f"{key}: 前回の law_revision_id が記録に無い")
    return changed, notes


def check_tsutatsu() -> tuple[list[dict], list[str]]:
    """通達：掲載時点の文言が変わったものを挙げる。"""
    changed, notes = [], []
    for key, src in SOURCES.items():
        if not src.get("active"):
            continue
        links_path = RAW_TSU / key / "_links.json"
        if not links_path.exists():
            notes.append(f"{key}: _links.json が無いため比較をとばした")
            continue
        before = (json.loads(links_path.read_text(encoding="utf-8")) or {}).get("published_as_of")
        now = fetch_published_as_of(key, src)
        time.sleep(1.2)
        if now is None:
            # 文言が拾えなくなった＝国税庁側のページ構成が変わった可能性。
            # 黙って「変更なし」にすると更新を取りこぼすので、変更あつかいで人に見せる。
            notes.append(f"{key}: 掲載時点の文言を拾えなかった（ページ構成の変更か）")
            changed.append({"key": key, "name": src["name"], "abbr": src["abbr"],
                            "before": before, "after": None})
            continue
        if now != before:
            changed.append({"key": key, "name": src["name"], "abbr": src["abbr"],
                            "before": before, "after": now})
    return changed, notes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="結果をJSONで出す")
    ap.add_argument("--github", action="store_true", help="$GITHUB_OUTPUT にも書く")
    ap.add_argument("--only", choices=["laws", "tsutatsu"], help="片方だけ調べる")
    args = ap.parse_args()

    laws, law_notes = ([], []) if args.only == "tsutatsu" else check_laws()
    tsus, tsu_notes = ([], []) if args.only == "laws" else check_tsutatsu()

    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "laws": laws,
        "tsutatsu": tsus,
        "notes": law_notes + tsu_notes,
        "changed": bool(laws or tsus),
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"=== 更新チェック {report['checked_at']} ===")
        if laws:
            print(f"\n法令 {len(laws)}本に改正あり:")
            for x in laws:
                print(f"  {x['key']:12s} {x['name']}　施行 {x['enforced']}　{x['amend'] or ''}")
                print(f"    {x['before']} → {x['after']}")
        else:
            print("\n法令：改正なし")
        if tsus:
            print(f"\n通達 {len(tsus)}本に更新あり:")
            for x in tsus:
                print(f"  {x['abbr']:6s} {x['name']}")
                print(f"    {x['before']} → {x['after']}")
        else:
            print("\n通達：更新なし")
        for n in report["notes"]:
            print(f"  ※ {n}")

    if args.github and os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"changed={'true' if report['changed'] else 'false'}\n")
            f.write(f"laws_changed={'true' if laws else 'false'}\n")
            f.write(f"tsutatsu_keys={' '.join(x['key'] for x in tsus)}\n")
            f.write(f"summary={_summary(laws, tsus)}\n")
    return 0


def _summary(laws: list, tsus: list) -> str:
    parts = []
    if laws:
        parts.append("法令 " + "・".join(x["name"] for x in laws))
    if tsus:
        parts.append("通達 " + "・".join(x["abbr"] for x in tsus))
    return " ／ ".join(parts) or "変更なし"


if __name__ == "__main__":
    sys.exit(main())
