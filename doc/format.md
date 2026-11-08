# SNavi 地図パック形式（.ctm, version 1）

すべてリトルエンディアン。PC側 `tools/osm2pack.py` が作り、アプリ `com.snavi.Pack` が読む。

## 座標
- ワールド座標（int32）: Web メルカトルを 0..2^31 に正規化。
  - `wx = (lon + 180) / 360 * 2^31`
  - `wy = (1 - ln(tan(lat) + 1/cos(lat)) / π) / 2 * 2^31`（北が小さい）
- タイル (z, x, y) は wx ∈ [x·2^(31-z), (x+1)·2^(31-z)) を覆う。
- タイル内ローカル座標: `(w - origin) * 4096 / 2^(31-z)` → int16。範囲 0..4096（線は ±128 のはみ出しを許す）。

## タイルレベル
| パック | レベル | 内容 |
|---|---|---|
| 都道府県 | 9  | land, water(>1km²), forest(>1km²), 道路 class 0–2（簡略化 30m）, rail無し, label city |
| 都道府県 | 12 | land, water(>0.02km²), park/forest/farm(>0.02km²), 道路 class 0–4（簡略化 5m）, rail, label city/town/suburb/station |
| 都道府県 | 14 | 全部（建物含む、簡略化 0.5m） |
| 全国(base, code 0) | 6 | land（簡略化 300m）, 道路 class 0（motorway） |
| 全国(base, code 0) | 8 | land（簡略化 60m）, 道路 class 0–1, label 都道府県名/city |

- 都道府県パックの land = その都道府県の行政界ポリゴン（admin_level=4）。描画時に「詳細パックの land が塗った範囲」をステンシルに書き、範囲外は base で補う。
- 都道府県パックのタイルは、その都道府県ポリゴンと交わるものだけ。データが空でも land があれば存在する。

## ファイル構造
```
char[4] "CTM1"
u16 version (=1)
u16 prefCode            (1..47, 0 = 全国 base)
i32 minX, minY, maxX, maxY   (ワールド座標の範囲)
u16 nameLen, utf8 name  (例 "東京都")
u32 buildDate           (yyyymmdd)
u16 nSections
  nSections × { char[4] id, u64 offset, u64 length }   (offset はファイル先頭から)
```
セクション: `TIDX`（タイル索引）, `TDAT`（タイル本体）, `ROUT`（経路グラフ, base には無い）, `SRCH`（検索索引）, `CAMS`（速度取締機, base には無い）, `TRES`（右左折禁止）, `TCON`（時間帯通行止め）。`LANE`（レーン案内）, `POIC`（POI 分類）, `JNAM`（交差点名）。TRES/TCON/LANE/POIC/JNAM は任意（無ければ無いものとして扱う）。セクションは各 8 バイト境界から始まる。

### TIDX
```
u32 n
n × { u8 z, u8 0, u16 0, u32 x, u32 y, u64 offset(ファイル絶対), u32 clen, u32 ulen }   // 28 バイト, (z,x,y) 昇順
```
タイル本体は zlib（deflate, zlib ヘッダ付き）で圧縮。

### タイル本体（展開後）
```
u8 nLayers
layer × nLayers:
  u8 type      1=AREA 2=LINE 3=BUILDING 4=LABEL
  u8 cls
  u32 byteLen  (この後のバイト数)
  AREA:     u32 nVerts; i16[2·nVerts]            三角形リスト（3頂点ごと）
  LINE:     u32 nLines; nLines × { u8 flags, u16 nPts, i16[2·nPts] }
  BUILDING: u32 n; n × { u16 heightDm, u16 nRings, nRings × { u16 nPts, i16[2·nPts] }, u16 nIdx, u16 idx[nIdx] }
            idx は全リングの点を連結した配列への屋根三角形インデックス。リングは閉じ点を含まない。
  LABEL:    u32 n; n × { i16 x, i16 y, u8 kind, u8 prio, u16 len, utf8 }
```
レイヤーの並び順は描画順（AREA → LINE → BUILDING → LABEL、各タイプ内は cls 昇順）。

**AREA cls**: 0 land, 1 water, 2 park/grass, 3 forest, 4 farmland/meadow, 5 sand/beach
**LINE cls**: 0 motorway, 1 trunk, 2 primary, 3 secondary, 4 tertiary, 5 minor（unclassified/residential/living_street/road）, 6 service, 10 rail, 11 river/canal
**LINE flags**: bit0 tunnel, bit1 bridge, bit2 oneway, bit3 toll, bit4 link（*_link）
**LABEL kind**: 0 都道府県, 1 city, 2 town, 3 village, 4 suburb/quarter, 5 neighbourhood, 6 station, 7 IC/JCT（motorway_junction）, 8 POI, 9 水域名
**LABEL prio**: 大きいほど優先（0..255）

### ROUT（非圧縮、mmap 前提、4 バイト境界）
```
u32 'GRPH'(0x48505247), u32 nNodes, u32 nEdges, u32 nPts, u32 nNames, u32 namesBytes
i32 nodeX[nNodes]
i32 nodeY[nNodes]
u32 firstEdge[nNodes+1]          // ノード i の出辺は edges[firstEdge[i] .. firstEdge[i+1])
i64 osmId[nNodes]
u8  nodeFlags[nNodes]            // bit0 境界（県境をまたぐ way のノード）, bit1 信号（そのノード自身が traffic_signals、または同じ way 上 20 m 以内に traffic_signals ノードがある）, bit2 踏切（railway=level_crossing。同じ way 上 20 m 以内も含む）, bit3 一時停止（highway=stop。同じ way 上 20 m 以内も含む）
pad → 4 バイト境界
edges[nEdges] × 24 バイト {
  u32 to, u32 lengthCm, u32 ptStart, u16 ptCount, u8 cls, u8 flags,
  u32 nameIdx (0xFFFFFFFF=名無し), u8 maxspeedKmh(0=不明), u8 0, u16 0 }
i32 pts[2·nPts]                  // 中間形状点（from/to は含まない）を順方向で
u32 nameOff[nNames+1]; utf8 names[namesBytes]
```
- 有向辺。両方向通行なら 2 本、一方通行は 1 本。逆向きの辺は flags bit0=1 で、同じ ptStart の点列を逆順に使う。
- **edge cls** は LINE cls と同じ 0–6。LINE タイルの flags には etcOnly を入れない。**edge flags**: bit0 reversed, bit1 toll, bit2 tunnel, bit3 bridge, bit4 roundabout, bit5 link, bit6 etcOnly（スマート IC の *_link。名前に「スマート」を含む motorway_junction から link だけをたどって到達した辺。そのジャンクションノードから 1.5 km 以内。本線・trunk には戻らない）
- 対象: highway = motorway, trunk, primary, secondary, tertiary, unclassified, residential, living_street, service, road, *_link。access=no/private（motor_vehicle/motorcar=yes を除く）や area=yes は除く。junction=roundabout と motorway は一方通行とみなす。oneway=-1 は逆向き。

### SRCH
```
u32 n
n × { i32 x, i32 y, u8 kind, u16 len, utf8 name, u16 len2, utf8 reading }   // reading は name:ja-Hira / name:ja_kana など（無ければ空）
```
kind は LABEL kind と同じ番号。

### CAMS（速度取締機）
```
u32 n
n × { i32 x, i32 y, i16 directionDeg, u8 type, u8 maxspeedKmh }   // 12 バイト
```
- 都道府県ポリゴン（領海込み）を 200 m 広げた範囲内のもの。
- **type**: 0 = highway=speed_camera ノード（固定式・オービス）, 1 = type=enforcement リレーションの device
- **directionDeg**: 取り締まる向き（車の進行方位、北 0・時計回り 0..359）, -1 = 不明。決め方の優先順:
  タグ direction / camera:direction（数値または N, NE…）→ リレーションの direction → リレーションの from→device の方位 →
  所属する道路の forward/backward または一方通行の向き。
- **maxspeedKmh**: リレーションまたはノードの maxspeed（0 = 不明）

### TRES（右左折禁止, 任意）
```
u32 n
n × { u32 fromEdge, u32 toEdge }      // (fromEdge, toEdge) 昇順。番号は同じパックの ROUT の辺番号
```
- type=restriction リレーション（restriction=* または restriction:motorcar=*。except に motorcar / motor_vehicle を含むものは除く）。via はノードだけ対応し、via が way のものは無視。
- fromEdge = via ノードで終わる from-way の辺、toEdge = via ノードから始まる to-way の辺。そのペアの通過を禁止する。
- no_* : その (from, to) を禁止。only_* : via ノードから出る辺のうち to-way 以外を全部禁止（from-way を戻る U ターンも含む）。

### TCON（時間帯通行止め, 任意）
```
u32 n
n × { u32 edge, u8 hours[21] }        // 168 ビットの週マスク。edge 昇順
```
- bit (day·24 + hour) = 1 のとき、その 1 時間は通行止め（1 時間の一部でも止まるなら 1）。day 0 = 月曜。bit i は hours[i>>3] の (i&7) ビット目。
- motor_vehicle:conditional / motorcar:conditional / vehicle:conditional / access:conditional の値が no / private の規則（例 `no @ (Mo-Fr 07:00-09:00)`、`no @ (07:30-08:30)`、`;` 区切りで複数可、曜日は Mo-Fr / Sa,Su 形式、PH は無視、日付をまたぐ時間帯も可）。way の両方向の辺に付ける。

### LANE（レーン案内, 任意）
```
u32 n
n × { u32 edge, u16 len, utf8 value }   // edge 昇順。value は OSM の turn:lanes 文字列（例 "left|through|through;right"）
```
- 有向辺ごと。順方向の辺は turn:lanes:forward、逆向きの辺（flags bit0）は turn:lanes:backward。
  一方通行の道は turn:lanes をその通行方向の辺に使う（oneway=-1 なら逆向きの辺）。
- その way のその向きの辺すべてに付ける（交差点で使うのは、その交差点ノードで終わる辺）。

### POIC（POI 分類, 任意）
```
u32 n                 // = SRCH の件数
u8 cat[n]             // SRCH の i 番目と 1:1
```
**cat**: 0xFF なし, 0 給油所（amenity=fuel）, 1 コンビニ（shop=convenience）, 2 駐車場（amenity=parking）,
3 道の駅（名前に「道の駅」を含む、または highway=services + services=michi_no_eki）, 4 飲食（restaurant / fast_food / cafe / food_court）,
5 病院（hospital / clinic / doctors）, 6 トイレ・休憩所（amenity=toilets, highway=services / rest_area）
- SRCH の対象は名前のある POI だけ（名前の無い駐車場やトイレは入らない）。highway=services / rest_area も SRCH と LABEL（kind 8）に入る。

### JNAM（交差点名, 任意）
```
u32 n
n × { u32 node, u16 len, utf8 name }   // node = ROUT のノード番号、昇順
```
- name を持つ highway=traffic_signals / stop / give_way、junction=yes のノード（日本の交差点名はたいてい信号ノードの name）。
  グラフノードでなければ、同じ way 上 30 m 以内のいちばん近いグラフノードに付ける。highway=crossing はグラフノードそのものの場合だけ。
- 1 つのグラフノードに複数あるときは、いちばん近いものを使う。

## 配信（index.json）
サーバーのベース URL 直下に置く。アプリはこれを読んでダウンロード一覧を作る。
```json
{ "version": "20261105",
  "packs": [ { "code": 0,  "name": "全国（広域図）", "file": "00_base.ctm", "size": 1234, "sha1": "…", "date": 20261105 },
             { "code": 13, "name": "東京都", "file": "13.ctm", "size": 5678, "sha1": "…", "date": 20261105 } ] }
```
