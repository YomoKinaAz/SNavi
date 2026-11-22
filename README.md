# SNavi — 地図データとアプリの配布

OpenStreetMap を使ったオフラインのカーナビ **SNavi**（Android、1280×720 の車載機向け）の配布用リポジトリ。

## ダウンロード
最新版は [Releases](../../releases/latest) にある。
- `00_base.ctm`: 全国の広域図（約 0.6MB）
- `01.ctm`〜`47.ctm`: 都道府県ごとの地図（番号は都道府県コード）
- `101.ctm`〜`147.ctm`: 都道府県ごとの施設データ（検索用、番号は 100＋都道府県コード）
- `201.ctm`〜`247.ctm`: 都道府県ごとの住所データ（番地・号まで選べる住所検索用、番号は 200＋都道府県コード）
- `SNavi.apk`: アプリ本体（Android）
- `index.json`: 配信一覧。アプリのダウンロード画面が読む

全国 47 都道府県の地図・施設データ・住所データを配布している。アプリで都道府県を選ぶと、3 つまとめてダウンロードされる。

## 地図の入れ方
1. **アプリから**: メニュー →「地図のダウンロード」→ 都道府県をタップ。配信元 URL の既定値は `https://github.com/YomoKinaAz/SNavi/releases/latest/download/`。
2. **ファイルから**: `.ctm` を USB メモリなどにコピーし、「地図のダウンロード」→「ファイルから追加」で選ぶ（複数まとめて選べる）。

## 地図データの作り方
`tools/osm2pack.py` が Geofabrik の地方別 OSM データ（PBF）から県ごとの `.ctm` を作る。形式は `doc/format.md` を参照。

GitHub Actions（`.github/workflows/maps.yml`）が毎日 03:00（日本時間）に動き、曜日ごとに 1 地方の地図を最新の OSM データで作り直して差し替える（日 北海道・月 東北・火 関東・水 中部・木 関西・金 中国四国・土 九州）。各地方の地図は週 1 回更新される。施設データと住所データは自動では更新しない。

```
pip install osmium shapely mapbox_earcut numpy pillow
python tools/osm2pack.py chubu-latest.osm.pbf --prefs 21,23
python tools/makebase.py
python tools/mkindex.py
```

## ライセンス
- 地図データ: © OpenStreetMap contributors、[ODbL 1.0](https://opendatacommons.org/licenses/odbl/)。`.ctm` は OpenStreetMap から作った派生データベースで、ODbL に従って配布する。
- 元データ: [Geofabrik](https://download.geofabrik.de/asia/japan.html) の日本の地方別抽出（取れないときは [openstreetmap.fr](https://download.openstreetmap.fr/extracts/asia/japan/) の同じ抽出）。
- 施設データ（`1xx.ctm`）: [Overture Maps Foundation](https://overturemaps.org/) の Places（CDLA Permissive 2.0 ほか、各データの出典に従う）。OSM のデータは含まない。
- 住所データ（`2xx.ctm`）: 「街区レベル位置参照情報・大字・町丁目レベル位置参照情報」国土交通省、「アドレス・ベース・レジストリ」デジタル庁、「登記所備付地図データ」法務省 を加工して作成。読み仮名は日本郵便の郵便番号データを使用。
