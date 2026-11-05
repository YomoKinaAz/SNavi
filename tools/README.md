# 地図パック作成ツール（PC 側）

必要なもの: Python 3.13 + `pip install --user osmium shapely mapbox_earcut numpy pillow psutil`

```
# 1. 地方単位の OSM データを取得（Geofabrik）
curl -L -o data/src/kanto-latest.osm.pbf https://download.geofabrik.de/asia/japan/kanto-latest.osm.pbf
# 2. 都道府県パックを作る（ファイル名の地方名から対象県を決める。--prefs 13,14 で絞り込み可）
set PYTHONIOENCODING=utf-8
python tools/osm2pack.py data/src/kanto-latest.osm.pbf
# 3. 全国広域図（data/work/*_base.pkl から作る。県を追加したら作り直す）
python tools/makebase.py
# 4. index.json
python tools/mkindex.py
# 5. 配信（アプリの「サーバー URL」に表示された URL を入れる）
python tools/serve.py --port 8000
# 検査とプレビュー
python tools/ctmdump.py data/packs/37.ctm --png data/preview/37.png --level 12
python tools/ctmdump.py data/packs/37.ctm --png t.png --level 14 --center 134.0466,34.3505 --span 3
```

地方 → 県: hokkaido 1 / tohoku 2–7 / kanto 8–14 / chubu 15–23 / kansai 24–30 / chugoku 31–35 / shikoku 36–39 / kyushu 40–47

- 陸地は natural=coastline から作る（OSM の県境ポリゴンは領海を含むため）。
- 四国（92 MB）の処理時間は約 11 分、ピークメモリは約 0.9 GB。所要時間はおおむね PBF のサイズに比例する（関東で 1 時間前後の見込み）。
- 大きな地方でメモリが足りない場合は `--index sparse_file_array,data/work/nodes.idx` を指定して、ノード座標をディスクに置く。
