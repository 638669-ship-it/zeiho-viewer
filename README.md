# 税務六法 Web（zeiho-viewer）

関根稔弁護士作成「国税法令＆通達集」（税務六法アウトライン表示ソフト）と同等の機能を、
現代のWeb技術で再構築するプロジェクト。税理士業務中に iPad・PC のブラウザから
条文・通達を即座に参照するためのツール。

- 発注者：福島会計事務所 税理士 福島健人
- 画面デザイン正本：`税務六法Web_表示イメージ.html`（v3カラフル版）
- 着手指示書：`税法条文ビューア_着手指示書_v2.md`

## 現在の状況

**Phase 0（基盤づくり）完了。** 対象24法令の生JSONを取得済み。

| Phase | 内容 | 状態 |
|---|---|---|
| 0 | 法令ID確定・取得スクリプト・生データ取得 | 完了 |
| 1 | パーサ＋ビューア公開（本丸） | 未着手 |
| 2 | 通達の取り込み | 未着手 |
| 3 | 横断全文検索・更新自動化 | 未着手 |

## セットアップ

Python 3.10+ のみ。追加パッケージは不要（標準ライブラリだけで動く）。

```powershell
python scripts/fetch_laws.py      # e-Gov から24本の全文JSONを data/raw/ に取得
python scripts/verify_raw.py      # 取得結果の検収（条数・ランドマーク条文の確認）
```

`data/raw/` は約97MBあり再取得可能なため git 管理外（`.gitignore`）。
取得の記録（取得日時・条数・サイズ）だけ `data/raw/_fetch_log.json` に残る。

## リポジトリ構成

```
zeiho-viewer/
├── scripts/
│   ├── fetch_laws.py        # e-Gov API v2 から取得（実装済み）
│   ├── verify_raw.py        # 取得結果の検収（実装済み）
│   ├── parse_laws.py        # Phase 1: 階層JSON化・分割
│   ├── fetch_tsutatsu.py    # Phase 2: 国税庁サイト取得
│   └── build_local_zip.py   # Phase 1: ローカル版生成
├── data/
│   ├── raw/                 # 取得生データ（gitignore）
│   └── json/                # Phase 1: ビューアが読む分割済みJSON
├── docs/                    # Phase 1: GitHub Pages 公開ディレクトリ
├── laws_master.json         # 対象法令ID・名称・略称・色の台帳
└── README.md
```

## API仕様（2026-07-15 に実地確認）

`lawapi-v2.yaml` version **2.1.139**、base URL `https://laws.e-gov.go.jp/api/2`。

取得は `GET /law_data/{law_id}` に次のパラメータを付ける：

| パラメータ | 値 | 理由 |
|---|---|---|
| `response_format` | `json` | |
| `law_full_text_format` | `json` | `response_format` と食い違うと `law_full_text` が **Base64** で返る |
| `json_format` | `full` | `light` は要素の属性を落とし `Paragraph` の `Num` しか残さない。**条番号（`Article/@Num`）が消える**ため `full` 必須 |

`asof` で時点指定ができるが、指示書6により**現行法令のみ**を扱う（Phase 3以降の拡張候補）。

## データ構造の要点（Phase 1 パーサ向けの申し送り）

実データ確認で判明した、パーサが必ず対応すべき点：

1. **`Article/@Num` は3形式ある**
   - `"22"` … 第二十二条
   - `"22_2"` … 第二十二条の二（枝番は `_` 連結）
   - `"4:7"` … 第四条から第七条まで（「削除」等でまとめられた範囲。`:` 連結）。
     国税徴収法8件・地方税法34件など計約100件が該当。
2. **条番号は法令内で一意ではない。** 附則（`SupplProvision`）にも `Num="22"` の条が多数あり
   （法人税法の場合、本則262条に対し全文では1111条）。条の検索・ジャンプは
   `MainProvision` に限定し、附則は改正法令（`SupplProvision/@AmendLawNum`）ごとに分けて持つこと。
3. **見出し（`ArticleCaption`）は全条にあるわけではない。** 法人税法79%・所得税法93%・措置法88%。
   法法22・法法22の2 には見出しが無い（「各事業年度の所得の金額の計算の通則」は
   条見出しではなく**款名**）。モックHTMLの `midashi` は暫定値なので、
   実装では見出しが無い場合の表示（款・節名で代替するか空にするか）を決めること。

## 出典・ライセンス

- 法令データ：**e-Gov法令検索（デジタル庁）** 法令API v2 より取得。
  [政府標準利用規約（第2.0版）](https://www.e-gov.go.jp/) に基づき出典を明示して利用。
- 通達（Phase 2）：**国税庁「法令解釈通達」** https://www.nta.go.jp/law/tsutatsu/
- 法令・通達は著作権法第13条により著作権の目的とならない。
- 本リポジトリのスクリプト・ビューアのコードは福島会計事務所に帰属。

法令データは加工して表示しています。正確な法令は必ず
[e-Gov法令検索](https://laws.e-gov.go.jp/) の原文および官報で確認してください。
