#!/usr/bin/env python3
"""Validate a .ctm pack and optionally render a PNG preview.

usage: python ctmdump.py 37.ctm [--png out.png] [--level 12] [--center LON,LAT --span KM] [--width 2000]
"""
import argparse, struct, sys, collections
import numpy as np
from ctmlib import *

AREA_COL = {0: (242, 239, 233), 1: (160, 200, 240), 2: (200, 230, 180), 3: (170, 210, 160), 4: (235, 235, 205),
            5: (245, 235, 190)}
LINE_COL = {0: (230, 120, 60), 1: (240, 160, 70), 2: (250, 200, 90), 3: (250, 230, 120), 4: (255, 255, 255),
            5: (255, 255, 255), 6: (250, 250, 250), 10: (120, 120, 120), 11: (140, 180, 230)}
LINE_W = {0: 5, 1: 4, 2: 4, 3: 3, 4: 3, 5: 2, 6: 1, 10: 2, 11: 2}


def check_rout(d, o, l):
    magic, nN, nE, nP, nNm, nb = struct.unpack_from('<6I', d, o)
    assert magic == 0x48505247
    p = o + 24
    nx = np.frombuffer(d, '<i4', nN, p); p += 4 * nN
    ny = np.frombuffer(d, '<i4', nN, p); p += 4 * nN
    fe = np.frombuffer(d, '<u4', nN + 1, p); p += 4 * (nN + 1)
    oid = np.frombuffer(d, '<i8', nN, p); p += 8 * nN
    nf = np.frombuffer(d, 'u1', nN, p); p += nN
    p += (-(p - o)) % 4
    edt = np.dtype([('to', '<u4'), ('len', '<u4'), ('ps', '<u4'), ('pc', '<u2'), ('cls', 'u1'), ('fl', 'u1'),
                    ('name', '<u4'), ('ms', 'u1'), ('p1', 'u1'), ('p2', '<u2')])
    E = np.frombuffer(d, edt, nE, p); p += 24 * nE
    pts = np.frombuffer(d, '<i4', 2 * nP, p).reshape(-1, 2); p += 8 * nP
    noff = np.frombuffer(d, '<u4', nNm + 1, p); p += 4 * (nNm + 1)
    p += nb
    assert p - o == l, 'ROUT length %d != %d' % (p - o, l)
    assert fe[0] == 0 and fe[-1] == nE and np.all(np.diff(fe.astype(np.int64)) >= 0)
    assert E['to'].max(initial=0) < nN
    assert np.all(E['ps'].astype(np.int64) + E['pc'] <= nP)
    nm = E['name'][E['name'] != 0xFFFFFFFF]
    assert nm.max(initial=0) < nNm and noff[-1] == nb
    # geometry continuity: first shape point should be near from-node (forward edges)
    deg = np.diff(fe.astype(np.int64))
    frm = np.repeat(np.arange(nN), deg)
    print('ROUT: nodes %d edges %d pts %d names %d | border %d signals %d | total %.1f km (directed)' % (
        nN, nE, nP, nNm, int((nf & 1).astype(bool).sum()), int((nf & 2).astype(bool).sum()), E['len'].sum() / 1e5))
    print('      edge cls histogram', dict(collections.Counter(E['cls'].tolist())), 'oneway-ish reversed', int((E['fl'] & 1).sum()))
    return dict(nx=nx, ny=ny, fe=fe, E=E, pts=pts, frm=frm)


def check_srch(d, o, l):
    n, = struct.unpack_from('<I', d, o)
    p = o + 4; kinds = collections.Counter(); ex = []
    for i in range(n):
        x, y, k, ln = struct.unpack_from('<iiBH', d, p); p += 11
        nm = d[p:p + ln].decode('utf-8'); p += ln
        l2, = struct.unpack_from('<H', d, p); p += 2
        rd = d[p:p + l2].decode('utf-8'); p += l2
        kinds[k] += 1
        if len(ex) < 6 and k in (0, 1, 6):
            lon, lat = to_lonlat(x, y)
            ex.append('%s(%s) k%d %.5f,%.5f' % (nm, rd, k, lon, lat))
    assert p - o == l, 'SRCH length mismatch'
    print('SRCH: %d entries %s' % (n, dict(sorted(kinds.items()))))
    print('      e.g.', '; '.join(ex))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('ctm'); ap.add_argument('--png'); ap.add_argument('--level', type=int)
    ap.add_argument('--center'); ap.add_argument('--span', type=float, default=10.0)
    ap.add_argument('--width', type=int, default=2000)
    a = ap.parse_args()
    f = CtmFile(a.ctm)
    d = f.data
    lon0, lat0 = to_lonlat(f.bbox[0], f.bbox[3]); lon1, lat1 = to_lonlat(f.bbox[2], f.bbox[1])
    print('%s: code %d "%s" v%d date %d bbox lon %.3f..%.3f lat %.3f..%.3f size %.1f MB' % (
        a.ctm, f.code, f.name, f.version, f.date, lon0, lon1, lat0, lat1, len(d) / 1e6))
    for s, (o, l) in f.sections.items():
        print('  section %s off %d len %d' % (s, o, l))
        assert o + l <= len(d)
    stats = collections.defaultdict(collections.Counter)
    tiles = {}
    for key in sorted(f.tiles):
        raw = f.tile_raw(key)
        lays = parse_tile(raw)
        tiles[key] = lays
        st = stats[key[0]]
        st['tiles'] += 1; st['bytes'] += f.tiles[key][1]
        for t, cls, pl in lays:
            if t == 1: st['area%d_tris' % cls] += len(pl) // 3
            elif t == 2: st['line%d' % cls] += len(pl)
            elif t == 3: st['buildings'] += len(pl)
            elif t == 4: st['labels'] += len(pl)
    for z in sorted(stats):
        s = stats[z]
        print('  z%d: %d tiles %.1f MB | %s' % (z, s['tiles'], s['bytes'] / 1e6,
              ', '.join('%s=%d' % (k, v) for k, v in sorted(s.items()) if k not in ('tiles', 'bytes'))))
    if 'ROUT' in f.sections:
        check_rout(d, *f.sections['ROUT'])
    if 'SRCH' in f.sections:
        check_srch(d, *f.sections['SRCH'])
    nE = struct.unpack_from('<I', d, f.sections['ROUT'][0] + 8)[0] if 'ROUT' in f.sections else 0
    if 'TRES' in f.sections:
        o, l = f.sections['TRES']
        n, = struct.unpack_from('<I', d, o)
        assert l == 4 + 8 * n, 'TRES length'
        pr = np.frombuffer(d, '<u4', 2 * n, o + 4).reshape(-1, 2)
        assert n == 0 or (pr.max() < nE and np.all(np.diff(pr[:, 0].astype(np.int64) * nE + pr[:, 1]) > 0))
        print('TRES: %d forbidden turn pairs' % n)
    if 'TCON' in f.sections:
        o, l = f.sections['TCON']
        n, = struct.unpack_from('<I', d, o)
        assert l == 4 + 25 * n, 'TCON length'
        es = [struct.unpack_from('<I', d, o + 4 + 25 * i)[0] for i in range(n)]
        assert all(e < nE for e in es) and es == sorted(es)
        print('TCON: %d conditional edges' % n)
    if 'LANE' in f.sections:
        o, l = f.sections['LANE']
        n, = struct.unpack_from('<I', d, o); p = o + 4; ex = []; last = -1
        for i in range(n):
            e, ln = struct.unpack_from('<IH', d, p); p += 6
            v = d[p:p + ln].decode('utf-8'); p += ln
            assert e < nE and e > last; last = e
            if len(ex) < 3: ex.append(v)
        assert p - o == l, 'LANE length'
        print('LANE: %d edges, e.g. %s' % (n, ex))
    if 'POIC' in f.sections:
        o, l = f.sections['POIC']
        n, = struct.unpack_from('<I', d, o)
        assert l == 4 + n
        ns, = struct.unpack_from('<I', d, f.sections['SRCH'][0])
        assert n == ns, 'POIC count != SRCH count'
        print('POIC: %s' % dict(sorted(collections.Counter(d[o + 4:o + 4 + n]).items())))
    if 'JNAM' in f.sections:
        o, l = f.sections['JNAM']
        nN = struct.unpack_from('<I', d, f.sections['ROUT'][0] + 4)[0]
        n, = struct.unpack_from('<I', d, o); p = o + 4; ex = []; last = -1
        for i in range(n):
            g, ln = struct.unpack_from('<IH', d, p); p += 6
            v = d[p:p + ln].decode('utf-8'); p += ln
            assert g < nN and g > last; last = g
            if len(ex) < 5: ex.append(v)
        assert p - o == l, 'JNAM length'
        print('JNAM: %d named intersections, e.g. %s' % (n, ex))
    if 'CAMS' in f.sections:
        o, l = f.sections['CAMS']
        n, = struct.unpack_from('<I', d, o)
        assert l == 4 + 12 * n, 'CAMS length'
        cams = [struct.unpack_from('<iihBB', d, o + 4 + 12 * i) for i in range(n)]
        assert all(-1 <= c[2] < 360 and c[3] in (0, 1) for c in cams)
        print('CAMS: %d cameras, with direction %d, enforcement %d' % (
            n, sum(c[2] >= 0 for c in cams), sum(c[3] == 1 for c in cams)))
        for c in cams[:5]:
            lo, la = to_lonlat(c[0], c[1])
            print('      %.5f,%.5f dir %d type %d max %d' % (lo, la, c[2], c[3], c[4]))
    print('OK')
    if a.png:
        render(f, tiles, a)


def render(f, tiles, a):
    from PIL import Image, ImageDraw, ImageFont
    levels = sorted(set(k[0] for k in tiles))
    z = a.level if a.level is not None else levels[len(levels) // 2]
    if a.center:
        lon, lat = map(float, a.center.split(','))
        cx, cy = to_world(lon, lat)
        half = a.span * 500 * wpm_at_wy(float(cy))
        bx = (float(cx) - half, float(cy) - half, float(cx) + half, float(cy) + half)
    else:
        bx = f.bbox
    W = a.width
    sc = W / (bx[2] - bx[0])
    H = int((bx[3] - bx[1]) * sc)
    img = Image.new('RGB', (W, H), (170, 205, 235))
    dr = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype('C:/Windows/Fonts/meiryo.ttc', 14)
    except Exception:
        font = None
    ts = S >> z

    def px(key, v):
        _, tx, ty = key
        return np.column_stack([(tx * ts + v[:, 0] * (ts / EXT) - bx[0]) * sc, (ty * ts + v[:, 1] * (ts / EXT) - bx[1]) * sc])

    keys = [k for k in tiles if k[0] == z and k[1] * ts < bx[2] and (k[1] + 1) * ts > bx[0] and k[2] * ts < bx[3] and (k[2] + 1) * ts > bx[1]]
    for phase in ('area', 'line', 'bld', 'label'):
        for key in keys:
            for t, cls, pl in tiles[key]:
                if phase == 'area' and t == 1:
                    p = px(key, pl.astype(np.float64))
                    col = AREA_COL.get(cls, (255, 0, 255))
                    for i in range(0, len(p), 3):
                        dr.polygon([tuple(q) for q in p[i:i + 3]], fill=col)
                elif phase == 'line' and t == 2:
                    for fl, ln in pl:
                        p = px(key, ln.astype(np.float64))
                        col = LINE_COL.get(cls, (255, 0, 0))
                        if fl & 1: col = tuple(int(c * 0.7) for c in col)
                        dr.line([tuple(q) for q in p], fill=col, width=max(1, int(LINE_W.get(cls, 1) * W / 2000)))
                elif phase == 'bld' and t == 3:
                    for h, rings, idx in pl:
                        p = px(key, rings[0].astype(np.float64))
                        if len(p) >= 3:
                            dr.polygon([tuple(q) for q in p], fill=(205, 195, 185), outline=(160, 150, 140))
                elif phase == 'label' and t == 4 and font:
                    for x, y, k, pr, nm in pl:
                        p = px(key, np.array([[x, y]], dtype=np.float64))[0]
                        dr.text((p[0], p[1]), nm, fill=(0, 0, 0), font=font, anchor='mm')
    img.save(a.png)
    print('rendered z%d %d tiles -> %s (%dx%d)' % (z, len(keys), a.png, W, H))


if __name__ == '__main__':
    main()
