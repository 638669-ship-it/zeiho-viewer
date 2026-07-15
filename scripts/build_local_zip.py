"""公開中のビューア（docs/）をそのまま zip 化し、オフラインで使える形にする。

  python scripts/build_local_zip.py            # dist/ に zip を出力
  python scripts/build_local_zip.py --open     # 出力後にフォルダを開く

指示書2-3「同一生成物を zip 化したローカルHTML版も毎回出力（オフライン保険）」。
GitHub Pages に上げるものと1バイトも変えずに固める（別物を作らない）。

■ なぜ起動用の .bat を同梱するのか
  ビューアは条文JSONを fetch() で遅延読み込みする。ブラウザは file:// の fetch を
  CORS で禁止しているため、index.html をダブルクリックしただけでは白画面になる。
  そこで zip には「税務六法をひらく.bat」を入れ、ローカルHTTPサーバ（Pythonの
  標準機能）で開く。ネット接続は不要＝オフラインで完結する。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import zipfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
DIST = ROOT / "dist"
TOP = "税務六法"  # zip を展開したときにできるフォルダ名

LAUNCHER = """@echo off
rem 税務六法 Web（ローカル版）ランチャ
rem ブラウザは file:// では条文データを読めない（fetch がCORSで禁止される）ため、
rem このバッチが手元でHTTPサーバを立ててから既定のブラウザで開く。
rem ネット接続は不要。閉じるときはこの黒い窓を閉じる。

setlocal
cd /d "%~dp0"

set PORT=8777
set PY=

where py >nul 2>&1 && set PY=py
if "%PY%"=="" ( where python >nul 2>&1 && set PY=python )
if "%PY%"=="" (
  echo.
  echo  Python が見つかりませんでした。
  echo  https://www.python.org/downloads/windows/ からインストールするか、
  echo  GitHub Pages 版をブラウザで開いてください。
  echo.
  pause
  exit /b 1
)

echo.
echo   税務六法 Web ローカル版
echo   http://127.0.0.1:%PORT%/ で開きます。
echo   終了するにはこの窓を閉じてください。
echo.

start "" http://127.0.0.1:%PORT%/
%PY% -m http.server %PORT% --bind 127.0.0.1
"""

README_TXT = """税務六法 Web ─ ローカル版（オフライン用）
========================================

■ 使い方（Windows）
   「税務六法をひらく.bat」をダブルクリックしてください。
   黒い窓が開き、続けて既定のブラウザで税務六法が開きます。
   終了するときは黒い窓を閉じてください。

   ※ index.html を直接ダブルクリックしても条文は表示されません。
      ブラウザの制限（file:// では条文データを読み込めない）のためです。
      かならず上の .bat から開いてください。

■ 使い方（iPad・Mac など）
   このローカル版は Windows PC 用の保険です。
   iPad からは GitHub Pages 版（オンライン）をご利用ください。

■ 収録内容
   {laws}法令／本則 {arts} 条
   データ生成日：{built}
   施行日基準：現行法令（未施行の改正は収録していません）

■ 出典
   e-Gov法令検索（デジタル庁）法令API v{api} より取得し、加工して表示しています。
   政府標準利用規約（第2.0版）に基づき、出典を明示して利用しています。
   法令は著作権法第13条により著作権の目的となりません。

   正確な法令は、かならず e-Gov法令検索 の原文および官報で確認してください。
   https://laws.e-gov.go.jp/

福島会計事務所
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--open", action="store_true", help="出力後に dist フォルダを開く")
    args = ap.parse_args()

    index_path = DOCS / "data" / "index.json"
    if not index_path.exists():
        print("docs/data/index.json がありません。先に python scripts/parse_laws.py を実行してください。",
              file=sys.stderr)
        return 1
    index = json.loads(index_path.read_text(encoding="utf-8"))

    files = sorted(p for p in DOCS.rglob("*") if p.is_file())
    if not files:
        print("docs/ が空です。", file=sys.stderr)
        return 1

    DIST.mkdir(exist_ok=True)
    out = DIST / f"zeiho-viewer-local-{date.today():%Y%m%d}.zip"

    readme = README_TXT.format(
        laws=len(index["laws"]),
        arts=sum(x["articles"] for x in index["laws"]),
        built=(index.get("built_at") or "")[:10],
        api=index["source"]["api_version"],
    )

    # deflate はテキストJSONによく効く（実測 64MB → 約9MB）
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in files:
            z.write(f, f"{TOP}/{f.relative_to(DOCS).as_posix()}")
        z.writestr(f"{TOP}/税務六法をひらく.bat", LAUNCHER.encode("cp932"))
        z.writestr(f"{TOP}/はじめにお読みください.txt", readme.encode("utf-8-sig"))

    mb = out.stat().st_size / 1e6
    src = sum(f.stat().st_size for f in files) / 1e6
    print(f"{out}")
    print(f"  収録 {len(files)} ファイル {src:.1f}MB → zip {mb:.1f}MB（{mb / src * 100:.0f}%）")
    print(f"  展開すると「{TOP}」フォルダができ、税務六法をひらく.bat で起動します。")

    if args.open:
        subprocess.run(["explorer", str(DIST)], check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
