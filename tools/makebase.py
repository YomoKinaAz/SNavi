#!/usr/bin/env python3
"""Build the nationwide overview pack 00_base.ctm (levels 6 and 8) from data/work/NN_base.pkl."""
import os, sys, glob, pickle, argparse, time
import numpy as np
import shapely
from ctmlib import *

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BASE_LEVELS = {6: dict(tol=300.0, roads={0}, labels={0}), 8: dict(tol=60.0, roads={0, 1}, labels={0, 1})}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--work', default=os.path.join(ROOT, 'data', 'work'))
    ap.add_argument('--out', default=os.path.join(ROOT, 'data', 'packs'))
    a = ap.parse_args()
    items = []
    for p in sorted(glob.glob(os.path.join(a.work, '*_base.pkl'))):
        with open(p, 'rb') as f:
            items.append(pickle.load(f))
    if not items:
        sys.exit('no work/*_base.pkl')
    print('prefectures:', ' '.join('%02d%s' % (d['code'], d['name']) for d in items))
    wpm = float(np.mean([d['wpm'] for d in items]))
    lands = [shapely.from_wkb(d['land']) for d in items]
    roads = [(cl, shapely.from_wkb(w)) for d in items for cl, w in d['roads']]
    labels = [l for d in items for l in d['labels']]
    bbox = (min(d['bbox'][0] for d in items), min(d['bbox'][1] for d in items),
            max(d['bbox'][2] for d in items), max(d['bbox'][3] for d in items))
    date = max(d['date'] for d in items)
    tiles = TileStore()
    for z, prm in BASE_LEVELS.items():
        tol = prm['tol'] * wpm
        land = [p for g in lands for p in polygons_of(shapely.make_valid(shapely.simplify(g, tol, preserve_topology=True)))]
        lines = {}
        for cl, g in roads:
            if cl in prm['roads']:
                for ln in lines_of(shapely.simplify(g, tol)):
                    co = shapely.get_coordinates(ln)
                    if len(co) >= 2:
                        lines.setdefault(cl, []).append((0, co))
        labs = [l for l in labels if l[2] in prm['labels']]
        t = S >> z
        a0, b0, c0, d0 = tile_range(z, *bbox)
        cand = [(x, y) for x in range(a0, c0 + 1) for y in range(b0, d0 + 1)]
        n0 = len(tiles.idx)
        for tx, ty, raw in build_level_tiles(z, cand, {0: land}, lines, labs):
            tiles.add(z, tx, ty, raw)
        print('z%d: %d tiles' % (z, len(tiles.idx) - n0))
    srch = encode_srch([(x, y, k, nm, '') for x, y, k, pr, nm in labels])
    path = os.path.join(a.out, '00_base.ctm')
    write_ctm(path, 0, '全国（広域図）', bbox, date, tiles, rout=None, srch=srch)
    print('wrote %s %.2f MB' % (path, os.path.getsize(path) / 1e6))


if __name__ == '__main__':
    main()
