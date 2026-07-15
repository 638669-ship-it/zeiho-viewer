"""e-Gov 法令API v2 から対象法令の全文JSONを取得し data/raw/ に保存する。

  python scripts/fetch_laws.py             # 未取得のものだけ取得
  python scripts/fetch_laws.py --force     # 全件を取り直す
  python scripts/fetch_laws.py --only houjin_act shotoku_act

API仕様は 2026-07-15 に lawapi-v2.yaml（version 2.1.139）を実地確認した内容に基づく。
標準ライブラリのみで動作する（追加パッケージ不要）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MASTER = ROOT / "laws_master.json"
RAW_DIR = ROOT / "data" / "raw"

BASE = "https://laws.e-gov.go.jp/api/2"
UA = "zeiho-viewer/0.1 (personal tax-law reference tool; +https://github.com/)"

# 指示書 5-3：連続リクエストは間隔を空ける（サーバ負荷配慮）
SLEEP_SEC = 2.0
RETRY = 3
TIMEOUT = 180


def load_master() -> dict:
    with MASTER.open(encoding="utf-8") as f:
        return json.load(f)


def fetch_law(law_id: str, params_cfg: dict) -> bytes:
    """law_data エンドポイントから1法令の全文JSONを取得する。"""
    query = {
        "response_format": params_cfg["response_format"],
        "law_full_text_format": params_cfg["law_full_text_format"],
        "json_format": params_cfg["json_format"],
    }
    url = f"{BASE}/law_data/{urllib.parse.quote(law_id)}?" + urllib.parse.urlencode(query)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})

    last_err: Exception | None = None
    for attempt in range(1, RETRY + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            body = e.read()[:400].decode("utf-8", "replace")
            last_err = RuntimeError(f"HTTP {e.code}: {body}")
            if e.code < 500:  # 4xx はリトライしても無駄
                break
        except Exception as e:  # noqa: BLE001 - タイムアウト等
            last_err = e
        if attempt < RETRY:
            wait = SLEEP_SEC * (2**attempt)
            print(f"    retry {attempt}/{RETRY - 1} in {wait:.0f}s ({last_err})")
            time.sleep(wait)
    raise RuntimeError(f"取得失敗 {law_id}: {last_err}")


def count_tag(node, tag: str) -> int:
    """full形式JSONツリー中の指定タグの出現数を数える。"""
    n = 0
    if isinstance(node, dict):
        if node.get("tag") == tag:
            n += 1
        for v in node.values():
            n += count_tag(v, tag)
    elif isinstance(node, list):
        for v in node:
            n += count_tag(v, tag)
    return n


def find_tag(node, tag: str):
    """深さ優先で最初に見つかった指定タグのノードを返す。"""
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


def summarize(raw: bytes) -> dict:
    """取得したJSONから検収用のサマリを作る。"""
    d = json.loads(raw.decode("utf-8"))
    full = d.get("law_full_text")
    main = find_tag(full, "MainProvision")
    return {
        "bytes": len(raw),
        "law_title": (d.get("revision_info") or {}).get("law_title"),
        "law_num": (d.get("law_info") or {}).get("law_num"),
        "law_revision_id": (d.get("revision_info") or {}).get("law_revision_id"),
        "amendment_enforcement_date": (d.get("revision_info") or {}).get(
            "amendment_enforcement_date"
        ),
        "updated": (d.get("revision_info") or {}).get("updated"),
        "articles_main": count_tag(main, "Article") if main else 0,
        "articles_total": count_tag(full, "Article"),
        "paragraphs_total": count_tag(full, "Paragraph"),
        "suppl_provisions": count_tag(full, "SupplProvision"),
        "appdx_tables": count_tag(full, "AppdxTable"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="既存ファイルも取り直す")
    ap.add_argument("--only", nargs="*", help="laws_master.json の key を指定して部分取得")
    args = ap.parse_args()

    master = load_master()
    params_cfg = master["fetch_params"]
    laws = master["laws"]
    if args.only:
        laws = [x for x in laws if x["key"] in args.only]
        if not laws:
            print("該当する key がありません", file=sys.stderr)
            return 1

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    log_path = RAW_DIR / "_fetch_log.json"
    log = json.loads(log_path.read_text(encoding="utf-8")) if log_path.exists() else {}

    ok = skipped = failed = 0
    for i, law in enumerate(laws, 1):
        key, law_id = law["key"], law["law_id"]
        out = RAW_DIR / f"{law_id}.json"
        head = f"[{i}/{len(laws)}] {law['name']}（{law['abbr']} / {law_id}）"

        if out.exists() and not args.force:
            print(f"{head} … skip（取得済み）")
            skipped += 1
            continue

        print(f"{head} … 取得中")
        try:
            raw = fetch_law(law_id, params_cfg)
        except Exception as e:  # noqa: BLE001
            print(f"    !! {e}", file=sys.stderr)
            failed += 1
            time.sleep(SLEEP_SEC)
            continue

        try:
            info = summarize(raw)
        except Exception as e:  # noqa: BLE001
            print(f"    !! JSON解析失敗: {e}", file=sys.stderr)
            failed += 1
            time.sleep(SLEEP_SEC)
            continue

        out.write_bytes(raw)
        log[key] = {
            "law_id": law_id,
            "name": law["name"],
            "abbr": law["abbr"],
            "file": out.name,
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **info,
        }
        print(
            f"    OK {info['bytes'] / 1024 / 1024:.2f}MB  "
            f"本則{info['articles_main']}条 / 総Article {info['articles_total']}  "
            f"施行日 {info['amendment_enforcement_date']}"
        )
        ok += 1
        time.sleep(SLEEP_SEC)

    log_path.write_text(
        json.dumps(log, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(f"\n完了: 取得 {ok} / skip {skipped} / 失敗 {failed}　→ {log_path}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
