# CTnavi — 地図データとアプリの配布

OpenStreetMap を使ったオフラインのカーナビ **CTnavi**（Android、1280×720 の車載機向け）の配布用リポジトリ。

## ダウンロード
最新版は [Releases](../../releases/latest) にある。
- `00_base.ctm`: 全国の広域図（約 0.6MB）
- `21.ctm`: 岐阜県の地図（21 は都道府県コード）
- `index.json`: 配信一覧。アプリのダウンロード画面が読む

いま配布している都道府県の地図は岐阜県だけ。

## 地図の入れ方
1. **アプリから**: メニュー →「地図のダウンロード」→ 都道府県をタップ。配信元 URL の既定値は `https://github.com/YomoKinaAz/ctnavi/releases/latest/download/`。
2. **ファイルから**: `.ctm` を USB メモリなどにコピーし、「地図のダウンロード」→「ファイルから追加」で選ぶ（複数まとめて選べる）。

## 地図データの作り方
`tools/osm2pack.py` が Geofabrik の地方別 OSM データ（PBF）から県ごとの `.ctm` を作る。形式は `doc/format.md` を参照。

GitHub Actions（`.github/workflows/maps.yml`）で 8 地方を変換して Releases に置ける。いまは無効にしてある。

```
pip install osmium shapely mapbox_earcut numpy pillow
python tools/osm2pack.py chubu-latest.osm.pbf --prefs 21,23
python tools/makebase.py
python tools/mkindex.py
```

## ライセンス
- 地図データ: © OpenStreetMap contributors、[ODbL 1.0](https://opendatacommons.org/licenses/odbl/)。`.ctm` は OpenStreetMap から作った派生データベースで、ODbL に従って配布する。
- 元データ: [Geofabrik](https://download.geofabrik.de/asia/japan.html) の日本の地方別抽出。
