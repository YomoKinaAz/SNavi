#!/usr/bin/env python3
"""OSM region extract (.osm.pbf) -> per-prefecture SNavi packs (NN.ctm). See ../doc/format.md.

usage: python osm2pack.py data/src/kanto-latest.osm.pbf [--out data/packs] [--work data/work] [--prefs 13,14]
                          [--index flex_mem|sparse_file_array,PATH]
"""
import argparse, os, sys, time, struct, pickle, re, datetime, math, unicodedata
from array import array
import numpy as np
import shapely
import osmium
from ctmlib import *
import mapbox_earcut as earcut

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

REGIONS = {'hokkaido': [1], 'tohoku': list(range(2, 8)), 'kanto': list(range(8, 15)),
           'chubu': list(range(15, 24)), 'kansai': list(range(24, 31)), 'chugoku': list(range(31, 36)),
           'shikoku': list(range(36, 40)), 'kyushu': list(range(40, 48))}

ROAD = {'motorway': 0, 'trunk': 1, 'primary': 2, 'secondary': 3, 'tertiary': 4, 'unclassified': 5,
        'residential': 5, 'living_street': 5, 'road': 5, 'service': 6}
RAIL = {'rail', 'light_rail', 'narrow_gauge', 'monorail', 'tram'}
READING_KEYS = ('name:ja-Hira', 'name:ja_kana', 'name:ja-Kana', 'name:ja-Latn', 'name:ja_rm')
PLACE_KIND = {'city': 1, 'town': 2, 'village': 3, 'suburb': 4, 'quarter': 4, 'borough': 4,
              'neighbourhood': 5, 'hamlet': 5, 'isolated_dwelling': 5, 'locality': 5}
KIND_PRIO = {0: 255, 1: 200, 2: 160, 3: 120, 4: 100, 5: 60, 6: 140, 7: 130, 8: 40, 9: 50}
POI_LABEL = {('amenity', 'hospital'), ('amenity', 'townhall'), ('amenity', 'university'), ('amenity', 'college'),
             ('amenity', 'school'), ('amenity', 'police'), ('amenity', 'fire_station'), ('amenity', 'fuel'),
             ('tourism', 'attraction'), ('tourism', 'museum'), ('tourism', 'theme_park'), ('tourism', 'zoo'),
             ('shop', 'mall'), ('shop', 'department_store'), ('leisure', 'park'), ('aeroway', 'aerodrome'),
             ('historic', 'castle'), ('amenity', 'place_of_worship')}
POI_KEYS = ('amenity', 'shop', 'tourism', 'leisure', 'railway', 'aeroway', 'office', 'historic')

# level -> parameters (tolerance in metres, road classes, other line classes, area cls->min m², label kinds)
LEVELS = {
    9: dict(tol=30.0, roads={0, 1, 2}, other=set(), areas={1: 1e6, 3: 1e6}, labels={1}),
    12: dict(tol=5.0, roads={0, 1, 2, 3, 4}, other={10}, areas={1: 2e4, 2: 2e4, 3: 2e4, 4: 2e4}, labels={1, 2, 4, 6}),
    14: dict(tol=0.5, roads={0, 1, 2, 3, 4, 5, 6}, other={10, 11}, areas={1: 0, 2: 0, 3: 0, 4: 0, 5: 0},
             labels={1, 2, 3, 4, 5, 6, 7, 8, 9}),
}
T14 = S >> 14
INVALID = 2147483647


def log(*a):
    print('[%7.1fs]' % (time.time() - T0), *a, flush=True)


def parse_m(s):
    if not s:
        return None
    m = re.match(r'\s*([0-9]+(?:\.[0-9]+)?)', s)
    return float(m.group(1)) if m else None


def reading_of(t):
    for k in READING_KEYS:
        v = t.get(k)
        if v:
            return v
    return ''


# ---------------------------------------------------------------- pass 1: prefecture boundaries
def load_munis(pbf):
    """市町村（admin_level=7）と政令市の区（8）の境界 -> [(level, name, geometry)]"""
    fab = osmium.geom.WKBFactory()
    fp = (osmium.FileProcessor(pbf).with_areas(osmium.filter.TagFilter(('admin_level', '7'), ('admin_level', '8')))
          .with_filter(osmium.filter.EntityFilter(osmium.osm.AREA)))
    out = []
    for a in fp:
        t = a.tags
        if a.from_way() or t.get('boundary') != 'administrative' or not t.get('name'):
            continue
        try:
            g = shapely.from_wkb(fab.create_multipolygon(a))
        except Exception:
            continue
        out.append((int(t.get('admin_level')), t.get('name'), shapely.make_valid(shapely.transform(g, world_xy_transform))))
    return out


def encode_munis(munis, P, wpm):
    """その県の中の市区町村（代表点が県の中）。およそ 30m で簡略化。"""
    shapely.prepare(P)
    rows = []
    for level, name, g in munis:
        if not shapely.contains(P, g.point_on_surface()):
            continue
        rings = []
        for pg in polygons_of(shapely.simplify(g, 30 * wpm)):
            for r in [pg.exterior] + list(pg.interiors):
                c = shapely.get_coordinates(r)
                if len(c) >= 4:
                    rings.append(np.round(c[:-1]).astype('<i4'))
        if rings:
            rows.append((level, name.encode('utf-8'), rings))
    out = bytearray(struct.pack('<I', len(rows)))
    for level, nb, rings in rows:
        out += struct.pack('<BH', level, len(nb)) + nb + struct.pack('<H', len(rings))
        for c in rings:
            out += struct.pack('<I', len(c)) + c.tobytes()
    log('  MUNI: %d boundaries, %.0f KB' % (len(rows), len(out) / 1024))
    return bytes(out)


def load_prefs(pbf, wanted):
    fab = osmium.geom.WKBFactory()
    fp = (osmium.FileProcessor(pbf).with_areas(osmium.filter.TagFilter(('admin_level', '4')))
          .with_filter(osmium.filter.EntityFilter(osmium.osm.AREA))
          .with_filter(osmium.filter.KeyFilter('ISO3166-2')))
    prefs = {}
    for a in fp:
        t = a.tags
        iso = t.get('ISO3166-2', '')
        if a.from_way() or t.get('boundary') != 'administrative' or not iso.startswith('JP-'):
            continue
        code = int(iso[3:])
        if wanted and code not in wanted:
            continue
        g = shapely.from_wkb(fab.create_multipolygon(a), )
        g = shapely.make_valid(shapely.transform(g, world_xy_transform))
        prefs[code] = (t.get('name', iso), g)
    missing = (set(wanted) if wanted else set()) - set(prefs)
    if missing or not wanted:
        prefs.update(fallback_prefs(pbf, wanted, set(prefs)))
    return prefs


def fallback_prefs(pbf, wanted, have):
    """Prefecture relations that osmium could not assemble (usually because remote islands lie outside the
    regional extract, e.g. Tokyo's Izu/Ogasawara islands): polygonize the outer member ways that are present
    and keep every closed ring."""
    rels = {}
    for r in (osmium.FileProcessor(pbf, osmium.osm.RELATION)
              .with_filter(osmium.filter.TagFilter(('admin_level', '4')))):
        t = r.tags
        iso = t.get('ISO3166-2', '')
        if t.get('boundary') != 'administrative' or not iso.startswith('JP-'):
            continue
        code = int(iso[3:])
        if code in have or (wanted and code not in wanted):
            continue
        ways = [m.ref for m in r.members if m.type == 'w' and m.role in ('outer', '')]
        rels[code] = (t.get('name', iso), ways)
    if not rels:
        return {}
    wanted_ids = set(w for _, ws in rels.values() for w in ws)
    geo = {}
    for w in (osmium.FileProcessor(pbf, osmium.osm.NODE | osmium.osm.WAY).with_locations()
              .with_filter(osmium.filter.EntityFilter(osmium.osm.WAY))
              .with_filter(osmium.filter.IdFilter(wanted_ids))):
        pts = [(n.x, n.y) for n in w.nodes if n.x != INVALID and n.y != INVALID]
        if len(pts) >= 2:
            a = np.array(pts, dtype=np.float64) / 1e7
            x, y = to_world(a[:, 0], a[:, 1])
            geo[w.id] = shapely.LineString(np.column_stack([x, y]))
    out = {}
    for code, (name, ways) in rels.items():
        lines = [geo[w] for w in ways if w in geo]
        if not lines:
            log('WARNING: JP-%02d %s: no member ways in extract, skipped' % (code, name))
            continue
        faces = shapely.get_parts(shapely.polygonize(list(shapely.get_parts(shapely.union_all(lines)))))
        if len(faces) == 0:
            log('WARNING: JP-%02d %s: no closed ring could be built, skipped' % (code, name))
            continue
        g = shapely.union_all(faces)
        g = shapely.make_valid(g)
        log('WARNING: JP-%02d %s: boundary relation incomplete in this extract (%d/%d member ways); '
            'using fallback polygon from %d closed ring(s)' % (code, name, len(lines), len(ways), len(faces)))
        out[code] = (name, g)
    return out


# ---------------------------------------------------------------- assignment grid
class Assign:
    def __init__(self, prefs):
        self.P = {c: g for c, (n, g) in prefs.items()}
        self.bit = {c: 1 << c for c in self.P}
        self.interior = {}
        self.border = {}
        for c, g in self.P.items():
            shapely.prepare(g)
            a, b, cc, d = tile_range(14, *g.bounds)
            xs, ys = np.meshgrid(np.arange(a, cc + 1), np.arange(b, d + 1))
            xs = xs.ravel(); ys = ys.ravel()
            boxes = shapely.box(xs * T14, ys * T14, (xs + 1) * T14, (ys + 1) * T14)
            inter = shapely.intersects(g, boxes)
            cont = shapely.contains(g, boxes)
            for tx, ty, i, ct in zip(xs[inter], ys[inter], inter[inter], cont[inter]):
                k = (int(tx) << 14) | int(ty)
                if ct:
                    self.interior[k] = c
                else:
                    self.border.setdefault(k, []).append(c)

    def point(self, x, y):
        k = (int(x // T14) << 14) | int(y // T14)
        c = self.interior.get(k)
        if c is not None:
            return self.bit[c]
        cands = self.border.get(k)
        m = 0
        if cands:
            for c in cands:
                if shapely.contains_xy(self.P[c], x, y):
                    m |= self.bit[c]
        return m

    def line(self, xy):
        """-> (mask, crossing)"""
        seg = np.hypot(*np.diff(xy, axis=0).T) if len(xy) > 1 else np.zeros(0)
        pts = xy
        if len(seg) and seg.max() > T14 / 2:
            pts = shapely.get_coordinates(shapely.segmentize(shapely.LineString(xy), T14 / 2))
        keys = np.unique((pts[:, 0] // T14).astype(np.int64) << 14 | (pts[:, 1] // T14).astype(np.int64))
        inside = set(); cands = set(); allint = True
        for k in keys.tolist():
            c = self.interior.get(k)
            if c is not None:
                inside.add(c)
            else:
                allint = False
                for c2 in self.border.get(k, ()):
                    cands.add(c2)
        if allint:
            if len(inside) == 1:
                return self.bit[next(iter(inside))], False
            if not inside:
                return 0, False
        ls = shapely.LineString(xy) if len(xy) > 1 else shapely.Point(xy[0])
        m = 0; hit = []
        for c in inside | cands:
            if c in inside or shapely.intersects(self.P[c], ls):
                m |= self.bit[c]; hit.append(c)
        crossing = len(hit) > 1 or (len(hit) == 1 and not shapely.contains(self.P[hit[0]], ls))
        return m, crossing

    def geom(self, g):
        a, b, c, d = tile_range(14, *g.bounds)
        cands = set(); inside = set(); allint = True
        for tx in range(a, c + 1):
            for ty in range(b, d + 1):
                k = (tx << 14) | ty
                p = self.interior.get(k)
                if p is not None:
                    inside.add(p)
                else:
                    allint = False
                    cands.update(self.border.get(k, ()))
        if allint and len(inside) == 1:
            return self.bit[next(iter(inside))]
        m = 0
        for p in inside | cands:
            if shapely.intersects(self.P[p], g):
                m |= self.bit[p]
        return m


# ---------------------------------------------------------------- pass 2: features
class Store:
    def __init__(self):
        # lines (roads, rail, river)
        self.L_start = array('q'); self.L_n = array('i'); self.L_cls = array('b'); self.L_flags = array('B')
        self.L_name = array('i'); self.L_ms = array('B'); self.L_ow = array('b'); self.L_route = array('b')
        self.L_efl = array('B'); self.L_mask = array('q'); self.L_cross = array('b')
        self.XY = array('i'); self.IDS = array('q')
        self.names = {}; self.name_list = []
        self.coast = []
        self.areas = []          # (cls, mask, world geom, name)
        self.pts = []            # (x, y, kind, name, reading, mask, label)
        self.bld = {}            # code -> {(tx,ty): [count, bytearray]}
        self.signals = array('q')
        self.nbld = 0
        self.cam_nodes = {}      # node id -> (x, y, direction tag, maxspeed)
        self.enf_devices = set()
        self.enf = []            # (device ids, from ids, maxspeed, direction tag)
        self.cams = []           # resolved: (x, y, dirDeg, type, maxspeed)
        self.smart = []
        self.L_wid = array('q')  # OSM way id per line
        self.cond = {}           # line index -> 21-byte week mask (closed hours) for motor vehicles
        self.restr = []          # (only?, from way, via node, to way)
        self.restr_v = []        # (only?, from way, [via ways], to way)
        self.lane_n = {}         # line index -> (forward lanes, backward lanes), only >=2
        self.lanes = {}          # line index -> (forward turn:lanes, backward turn:lanes)          # (node id, x, y) of Smart IC (ETC-only) motorway_junction nodes

    def name_idx(self, n):
        if not n:
            return -1
        i = self.names.get(n)
        if i is None:
            i = self.names[n] = len(self.name_list); self.name_list.append(n)
        return i


CARDINAL = {'N': 0, 'NNE': 22, 'NE': 45, 'ENE': 67, 'E': 90, 'ESE': 112, 'SE': 135, 'SSE': 157, 'S': 180,
            'SSW': 202, 'SW': 225, 'WSW': 247, 'W': 270, 'WNW': 292, 'NW': 315, 'NNW': 337}


def bearing(x0, y0, x1, y1):
    """degrees clockwise from north, world coords (y grows south)"""
    if x0 == x1 and y0 == y1:
        return -1
    return int(round(math.degrees(math.atan2(x1 - x0, -(y1 - y0))))) % 360


COND_KEYS = ('motor_vehicle:conditional', 'motorcar:conditional', 'vehicle:conditional', 'access:conditional')
DAYS = {'Mo': 0, 'Tu': 1, 'We': 2, 'Th': 3, 'Fr': 4, 'Sa': 5, 'Su': 6}


def _days(spec):
    out = set()
    for part in spec.split(','):
        part = part.strip()
        if part in ('PH', 'SH'):
            continue
        m = re.match(r'^(Mo|Tu|We|Th|Fr|Sa|Su)(?:-(Mo|Tu|We|Th|Fr|Sa|Su))?$', part)
        if not m:
            return None
        a = DAYS[m.group(1)]; b = DAYS[m.group(2)] if m.group(2) else a
        d = a
        while True:
            out.add(d)
            if d == b:
                break
            d = (d + 1) % 7
    return out


def cond_mask(value, mask=None):
    """'no @ (Mo-Fr 07:00-09:00); ...' -> bytearray(21) with bit day*24+hour set when closed (None if nothing)"""
    for rule in re.split(r';(?![^()]*\))', value):
        if '@' not in rule:
            continue
        val, cond = rule.split('@', 1)
        if val.strip().lower() not in ('no', 'private'):
            continue
        cond = cond.strip()
        if cond.startswith('(') and cond.endswith(')'):
            cond = cond[1:-1].strip()
        for sub in cond.split(';'):
            sub = sub.strip()
            m = re.match(r'^((?:(?:Mo|Tu|We|Th|Fr|Sa|Su|PH|SH)(?:-(?:Mo|Tu|We|Th|Fr|Sa|Su))?\s*,?\s*)+)?\s*'
                         r'((?:\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}\s*,?\s*)*)$', sub)
            if not m or (not m.group(1) and not m.group(2)):
                continue
            days = _days(m.group(1).replace(' ', '').rstrip(',')) if m.group(1) else set(range(7))
            if not days:
                continue
            spans = re.findall(r'(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})', m.group(2) or '')
            mins = [(int(a) * 60 + int(b), int(c) * 60 + int(d)) for a, b, c, d in spans] or [(0, 1440)]
            if mask is None:
                mask = bytearray(21)
            for d in days:
                for t0, t1 in mins:
                    segs = [(t0, t1)] if t1 > t0 else [(t0, 1440), (0, t1)]
                    for dd, (u0, u1) in zip([d, (d + 1) % 7], segs):
                        for h in range(u0 // 60, (u1 + 59) // 60):
                            if h < 24:
                                bit = dd * 24 + h
                                mask[bit >> 3] |= 1 << (bit & 7)
    return mask


def is_narrow(t, cls, ow):
    """細い道: 生活道路など、対面通行で 1 車線、中央線なし、幅 4.5m 未満（タグがあるときだけ）"""
    if cls >= 5 and t.get('highway') != 'unclassified':
        return True
    if ow == 0 and parse_lane_count(t.get('lanes')) == 1:
        return True
    if t.get('lane_markings') == 'no' or t.get('narrow') == 'yes':
        return True
    try:
        return float((t.get('width') or '').replace('m', '').strip()) < 4.5
    except ValueError:
        return False


def parse_lane_count(v):
    try:
        return max(0, min(255, int(v)))
    except (TypeError, ValueError):
        return 0


def poi_cat(t, name):
    a = t.get('amenity'); hw = t.get('highway')
    if '道の駅' in (name or '') or (hw == 'services' and t.get('services') == 'michi_no_eki'):
        return 3
    if a == 'fuel':
        return 0
    if t.get('shop') == 'convenience':
        return 1
    if a == 'parking':
        return 2
    if a in ('restaurant', 'fast_food', 'cafe', 'food_court'):
        return 4
    if a in ('hospital', 'clinic', 'doctors') or t.get('healthcare') in ('hospital', 'clinic', 'doctor'):
        return 5
    if a == 'toilets' or hw in ('services', 'rest_area'):
        return 6
    return 0xFF


def load_junction_tags(pbf):
    """nodes-only pass: names of intersections and all traffic signal ids"""
    names = {}; sig = array('q'); lx = array('q'); stop = array('q')
    for n in (osmium.FileProcessor(pbf, osmium.osm.NODE)
              .with_filter(osmium.filter.KeyFilter('highway', 'junction', 'railway'))):
        t = n.tags
        hw = t.get('highway')
        if hw == 'traffic_signals':
            sig.append(n.id)
        elif hw == 'stop':
            stop.append(n.id)
        if t.get('railway') == 'level_crossing':
            lx.append(n.id)
        nm = t.get('name')
        if nm and (hw in ('traffic_signals', 'stop', 'give_way', 'crossing') or t.get('junction') == 'yes'):
            names[n.id] = (nm, hw)
    return names, sig, lx, stop


def attach_to_graph(ids, part, cum, isg, inv, gpos, keys, limit_m):
    """for every occurrence of a node id in keys on a routable way: nearest graph point on the same way
    within limit_m (itself if it is a graph node). -> dict osm id -> (uniq index of graph node, distance m)"""
    out = {}
    occ = np.nonzero(np.isin(ids, keys))[0]
    for i in occ.tolist():
        nid = int(ids[i])
        if isg[inv[i]]:
            best = (int(inv[i]), 0.0)
        else:
            k = np.searchsorted(gpos, i)
            best = None
            for j in (k - 1, k):
                if 0 <= j < len(gpos):
                    g = int(gpos[j])
                    if part[g] != part[i]:
                        continue
                    d = abs(cum[g] - cum[i])
                    if d <= limit_m and (best is None or d < best[1]):
                        best = (int(inv[g]), d)
            if best is None:
                continue
        if nid not in out or best[1] < out[nid][1]:
            out[nid] = best
    return out


def load_restrictions(pbf, st):
    for r in (osmium.FileProcessor(pbf, osmium.osm.RELATION)
              .with_filter(osmium.filter.TagFilter(('type', 'restriction')))):
        t = r.tags
        rv = t.get('restriction:motorcar') or t.get('restriction')
        if not rv or 'motorcar' in (t.get('except') or '') or 'motor_vehicle' in (t.get('except') or ''):
            continue
        if not (rv.startswith('no_') or rv.startswith('only_')):
            continue
        fr = [m.ref for m in r.members if m.type == 'w' and m.role == 'from']
        to = [m.ref for m in r.members if m.type == 'w' and m.role == 'to']
        via = [m for m in r.members if m.role == 'via']
        if len(fr) == 1 and len(to) == 1 and via and all(m.type == 'w' for m in via):
            st.restr_v.append((rv.startswith('only_'), fr[0], [m.ref for m in via], to[0]))
            continue
        if len(fr) != 1 or len(to) != 1 or len(via) != 1 or via[0].type != 'n':
            continue
        st.restr.append((rv.startswith('only_'), fr[0], via[0].ref, to[0]))


def load_enforcements(pbf, st):
    for r in (osmium.FileProcessor(pbf, osmium.osm.RELATION)
              .with_filter(osmium.filter.TagFilter(('type', 'enforcement')))):
        t = r.tags
        if t.get('enforcement') not in (None, 'maxspeed', 'average_speed', 'mindistance'):
            continue
        dev = [m.ref for m in r.members if m.type == 'n' and m.role == 'device']
        frm = [m.ref for m in r.members if m.type == 'n' and m.role == 'from']
        if dev:
            st.enf.append((dev, frm, parse_m(t.get('maxspeed')), t.get('direction')))
            st.enf_devices.update(dev)


def way_dir_at(st, nid, tag):
    """bearing of the road at node nid from tag forward/backward or the way's oneway direction; -1 if unknown"""
    IDS = np.frombuffer(st.IDS, dtype=np.int64)
    hits = np.nonzero(IDS == nid)[0]
    if len(hits) == 0:
        return -1
    starts = np.frombuffer(st.L_start, dtype=np.int64)
    ns = np.frombuffer(st.L_n, dtype=np.int32)
    XY = np.frombuffer(st.XY, dtype=np.int32).reshape(-1, 2)
    for h in hits.tolist():
        w = int(np.searchsorted(starts, h, side='right') - 1)
        if st.L_cls[w] > 6:
            continue
        s0 = int(starts[w]); e0 = s0 + int(ns[w]) - 1
        a, b = (h - 1, h) if h == e0 else (h, h + 1)
        if a < s0:
            continue
        sign = 0
        if tag == 'forward':
            sign = 1
        elif tag == 'backward':
            sign = -1
        elif st.L_ow[w] != 0:
            sign = int(st.L_ow[w])
        if sign == 0:
            return -1
        x0, y0 = XY[a]; x1, y1 = XY[b]
        if sign < 0:
            x0, y0, x1, y1 = x1, y1, x0, y0
        return bearing(float(x0), float(y0), float(x1), float(y1))
    return -1


def parse_dir(tag):
    if not tag:
        return None
    tag = tag.strip().upper()
    if tag in CARDINAL:
        return CARDINAL[tag]
    m = re.match(r'^(-?[0-9]+(?:\.[0-9]+)?)$', tag)
    if m:
        return int(round(float(m.group(1)))) % 360
    return None


def resolve_cameras(fp, st):
    locs = fp.node_location_storage
    used = set()
    for dev, frm, ms, dtag in st.enf:
        for d in dev:
            c = st.cam_nodes.get(d)
            if c is None:
                continue
            x, y, ntag, nms = c
            deg = parse_dir(ntag) if ntag else parse_dir(dtag)
            if deg is None and frm and locs is not None:
                try:
                    l = locs.get(frm[0])
                    fx, fy = to_world(l.lon, l.lat)
                    deg = bearing(float(fx), float(fy), x, y)
                except Exception:
                    deg = None
            if deg is None:
                deg = way_dir_at(st, d, (ntag or dtag or '').lower())
            st.cams.append((x, y, deg, 1, int(min(255, ms or nms or 0))))
            used.add(d)
    for nid, (x, y, ntag, nms) in st.cam_nodes.items():
        if nid in used:
            continue
        deg = parse_dir(ntag)
        if deg is None:
            deg = way_dir_at(st, nid, (ntag or '').lower())
        st.cams.append((x, y, deg, 0, int(min(255, nms or 0))))
    log('  speed cameras: %d (%d from enforcement relations)' % (len(st.cams), len(used)))


def run_main_pass(pbf, A, st, index):
    fab = osmium.geom.WKBFactory()
    keys = ('highway', 'railway', 'waterway', 'natural', 'landuse', 'leisure', 'building', 'place', 'name',
            'amenity', 'shop', 'tourism', 'aeroway', 'office', 'historic')
    fp = (osmium.FileProcessor(pbf).with_locations(index)
          .with_areas(osmium.filter.KeyFilter('natural', 'waterway', 'landuse', 'leisure', 'building', 'amenity',
                                              'shop', 'tourism', 'aeroway', 'office', 'historic', 'railway'))
          .with_filter(osmium.filter.EntityFilter(osmium.osm.NODE | osmium.osm.WAY | osmium.osm.AREA))
          .with_filter(osmium.filter.EmptyTagFilter())
          .with_filter(osmium.filter.KeyFilter(*keys)))
    nn = nw = na = 0
    cnt = 0
    for o in fp:
        cnt += 1
        if cnt % 5000000 == 0:
            log('  %dM objects: %d lines, %d buildings, %d areas' % (cnt // 1000000, len(st.L_n), st.nbld, len(st.areas)))
        if o.is_node():
            nn += 1
            t = o.tags
            if t.get('highway') == 'speed_camera' or o.id in st.enf_devices:
                loc = o.location
                if loc.valid():
                    cx_, cy_ = to_world(loc.lon, loc.lat)
                    st.cam_nodes[o.id] = (float(cx_), float(cy_), t.get('direction') or t.get('camera:direction'),
                                          parse_m(t.get('maxspeed')))
            if t.get('highway') == 'traffic_signals':
                st.signals.append(o.id)
            name = t.get('name')
            if not name:
                continue
            kind = None; label = False
            pl = t.get('place')
            if pl in PLACE_KIND:
                kind = PLACE_KIND[pl]; label = True
            elif t.get('railway') in ('station', 'halt'):
                kind = 6; label = True
            elif t.get('highway') in ('services', 'rest_area'):
                kind = 8; label = True
            elif t.get('highway') == 'motorway_junction':
                kind = 7; label = True
                if 'スマート' in unicodedata.normalize('NFKC', name):
                    loc = o.location
                    if loc.valid():
                        sx, sy = to_world(loc.lon, loc.lat)
                        st.smart.append((o.id, float(sx), float(sy)))
            else:
                for k in POI_KEYS:
                    v = t.get(k)
                    if v:
                        kind = 8; label = (k, v) in POI_LABEL
                        break
            if kind is None:
                continue
            loc = o.location
            if not loc.valid():
                continue
            x, y = to_world(loc.lon, loc.lat)
            m = A.point(float(x), float(y))
            if m:
                st.pts.append((float(x), float(y), kind, name, reading_of(t), m, label, poi_cat(t, name)))
        elif o.is_way():
            nw += 1
            t = o.tags
            hw = t.get('highway'); cls = None; link = False
            if hw:
                if hw.endswith('_link'):
                    hw = hw[:-5]; link = True
                cls = ROAD.get(hw)
                if cls is not None and t.get('area') == 'yes':
                    cls = None
            if cls is None:
                rw = t.get('railway')
                if rw in RAIL and t.get('service') is None:
                    cls = 10
                elif t.get('waterway') in ('river', 'canal'):
                    cls = 11
            coast = t.get('natural') == 'coastline'
            if cls is None and not coast:
                continue
            nodes = [(n.ref, n.x, n.y) for n in o.nodes]
            arr = np.array(nodes, dtype=np.int64)
            arr = arr[(arr[:, 1] != INVALID) & (arr[:, 2] != INVALID)]
            if len(arr) < 2:
                continue
            wx, wy = to_world(arr[:, 1] / 1e7, arr[:, 2] / 1e7)
            xy = np.column_stack([wx, wy])
            if coast:
                st.coast.append(np.rint(xy).astype(np.int32))
            if cls is None:
                continue
            m, cross = A.line(xy)
            if not m:
                continue
            flags = 0
            tun = t.get('tunnel'); br = t.get('bridge')
            if tun and tun != 'no': flags |= 1
            if br and br != 'no': flags |= 2
            ow = 0; route = 0; efl = 0
            if cls <= 6:
                o1 = t.get('oneway')
                junc = t.get('junction')
                if o1 in ('yes', 'true', '1'): ow = 1
                elif o1 in ('-1', 'reverse'): ow = -1
                elif o1 != 'no' and (junc in ('roundabout', 'circular') or (hw == 'motorway' and not link)): ow = 1
                if ow: flags |= 4
                if t.get('toll') == 'yes': flags |= 8
                if link: flags |= 16
                acc = t.get('access'); mv = t.get('motor_vehicle') or t.get('motorcar')
                route = 1
                if acc in ('no', 'private') and mv not in ('yes', 'designated', 'permissive', 'destination'):
                    route = 0
                if mv in ('no', 'private') or hw == 'service' and t.get('service') in ('parking_aisle', 'drive-through'):
                    route = 0
                if flags & 8: efl |= 2
                if flags & 1: efl |= 4
                if flags & 2: efl |= 8
                if junc in ('roundabout', 'circular'): efl |= 16
                if link: efl |= 32
                if is_narrow(t, cls, ow): efl |= 128
            ms = parse_m(t.get('maxspeed'))
            st.L_start.append(len(st.IDS)); st.L_n.append(len(arr)); st.L_cls.append(cls); st.L_flags.append(flags)
            st.L_name.append(st.name_idx(t.get('name') or t.get('ref') if cls <= 6 else t.get('name')))
            st.L_ms.append(int(min(255, ms)) if ms else 0); st.L_ow.append(ow); st.L_route.append(route)
            st.L_efl.append(efl); st.L_mask.append(m); st.L_cross.append(1 if cross else 0)
            st.L_wid.append(o.id)
            if cls <= 6:
                tl = t.get('turn:lanes'); tf = t.get('turn:lanes:forward'); tb = t.get('turn:lanes:backward')
                if tl or tf or tb:
                    if ow == 1:
                        lf, lb = tf or tl, None
                    elif ow == -1:
                        lf, lb = None, tb or tl
                    else:
                        lf, lb = tf, tb
                    if lf or lb:
                        st.lanes[len(st.L_n) - 1] = (lf, lb)
                # direction lane count (>=2 only; a missing value means 1 lane or unknown)
                total = parse_lane_count(t.get('lanes'))
                nf = parse_lane_count(t.get('lanes:forward'))
                nb = parse_lane_count(t.get('lanes:backward'))
                if ow == 1:
                    nf, nb = nf or total, 0
                elif ow == -1:
                    nf, nb = 0, nb or total
                else:
                    nf, nb = nf or total // 2, nb or total // 2
                if nf >= 2 or nb >= 2:
                    st.lane_n[len(st.L_n) - 1] = (nf, nb)
            if cls <= 6:
                cm = None
                for ck in COND_KEYS:
                    v = t.get(ck)
                    if v:
                        cm = cond_mask(v, cm)
                if cm is not None and any(cm):
                    st.cond[len(st.L_n) - 1] = bytes(cm)
            st.IDS.extend(arr[:, 0].tolist())
            st.XY.extend(np.rint(xy).astype(np.int32).ravel().tolist())
        else:  # area
            na += 1
            t = o.tags
            bv = t.get('building')
            if bv and bv != 'no' and not t.get('natural') == 'coastline':
                add_building(o, t, bv, A, st)
            if t.get('natural') == 'coastline':
                continue
            acls = area_cls(t)
            name = t.get('name')
            poi = None
            if name:
                if t.get('railway') in ('station', 'halt'):
                    poi = (6, True)
                elif t.get('highway') in ('services', 'rest_area'):
                    poi = (8, True)
                else:
                    for k in POI_KEYS:
                        v = t.get(k)
                        if v:
                            poi = (8, (k, v) in POI_LABEL); break
            if acls is None and poi is None:
                continue
            if acls is not None:
                try:
                    g = shapely.from_wkb(fab.create_multipolygon(o))
                except Exception:
                    continue
                g = shapely.transform(g, world_xy_transform)
                if not g.is_valid:
                    g = shapely.make_valid(g)
                m = A.geom(g)
                if m:
                    st.areas.append((acls, m, g, name if acls == 1 else None))
                if poi and m:
                    c = g.point_on_surface()
                    st.pts.append((c.x, c.y, poi[0], name, reading_of(t), A.point(c.x, c.y) or m, poi[1],
                                   poi_cat(t, name)))
            else:
                for ring in o.outer_rings():
                    a = np.array([(n.x, n.y) for n in ring], dtype=np.int64)
                    a = a[(a[:, 0] != INVALID)]
                    if len(a):
                        x, y = to_world(a[:, 0].mean() / 1e7, a[:, 1].mean() / 1e7)
                        m = A.point(float(x), float(y))
                        if m:
                            st.pts.append((float(x), float(y), poi[0], name, reading_of(t), m, poi[1],
                                           poi_cat(t, name)))
                    break
    resolve_cameras(fp, st)
    log('main pass done: %d tagged nodes, %d ways, %d areas, %d lines, %d pts, %d buildings, %d areas kept' %
        (nn, nw, na, len(st.L_n), len(st.IDS), st.nbld, len(st.areas)))


def area_cls(t):
    nat = t.get('natural'); lu = t.get('landuse'); le = t.get('leisure'); ww = t.get('waterway')
    if nat == 'water' or ww in ('riverbank', 'dock') or lu in ('reservoir', 'basin'):
        return 1
    if le in ('park', 'garden', 'golf_course') or lu in ('grass', 'recreation_ground', 'village_green', 'cemetery'):
        return 2
    if lu == 'forest' or nat in ('wood', 'scrub'):
        return 3
    if lu in ('farmland', 'meadow', 'orchard', 'vineyard', 'allotments') or nat in ('grassland', 'heath'):
        return 4
    if nat in ('beach', 'sand'):
        return 5
    return None


def building_height_dm(t, bv):
    h = parse_m(t.get('height'))
    if h is None:
        lv = parse_m(t.get('building:levels'))
        if lv is not None:
            h = lv * 3.0 + (1.0 if lv > 0 else 0)
    if h is None:
        h = 7.0 if bv in ('house', 'detached', 'residential', 'semidetached_house', 'hut', 'shed', 'garage',
                          'garages', 'farm_auxiliary', 'bungalow') else 9.0
    return int(max(1.0, min(h, 6000.0)) * 10)


_K = S / 360.0
_L = 1e-7 * math.pi / 180.0
_U = EXT / T14


def _ring_world(ring):
    """NodeRef ring -> (xs, ys) world floats, or None. Pure python (faster than numpy for tiny rings)."""
    xs = []; ys = []
    asinh = math.asinh; tan = math.tan
    for n in ring:
        x = n.x; y = n.y
        if x == INVALID or y == INVALID:
            return None
        xs.append((x * 1e-7 + 180.0) * _K)
        ys.append((1.0 - asinh(tan(y * _L)) / math.pi) * 0.5 * S)
    return xs, ys


def _ring_local(xs, ys, ox, oy):
    """-> flat list of rounded int coords without consecutive duplicates / closing point"""
    out = []; px = py = None
    for x, y in zip(xs, ys):
        lx = int(round((x - ox) * _U)); ly = int(round((y - oy) * _U))
        if lx == px and ly == py:
            continue
        out.append(lx); out.append(ly); px = lx; py = ly
    if len(out) >= 4 and out[0] == out[-2] and out[1] == out[-1]:
        del out[-2:]
    return out


def add_building(o, t, bv, A, st):
    hdm = None
    for outer in o.outer_rings():
        w = _ring_world(outer)
        if w is None or len(w[0]) < 4:
            continue
        xs, ys = w
        n = len(xs) - 1
        cx = sum(xs[:n]) / n; cy = sum(ys[:n]) / n
        m = A.point(cx, cy)
        if not m:
            continue
        tx, ty = int(cx // T14), int(cy // T14)
        ox, oy = tx * T14, ty * T14
        r0 = _ring_local(xs, ys, ox, oy)
        if len(r0) < 6:
            continue
        rings = [r0]
        for inner in o.inner_rings(outer):
            wi = _ring_world(inner)
            if wi is not None and len(wi[0]) >= 4:
                ri = _ring_local(wi[0], wi[1], ox, oy)
                if len(ri) >= 6:
                    rings.append(ri)
        flat = [v for r in rings for v in r]
        if len(flat) // 2 > 65535 or max(flat) > 32767 or min(flat) < -32768:
            continue
        ends = []; acc = 0
        for r in rings:
            acc += len(r) // 2; ends.append(acc)
        idx = earcut.triangulate_float64(np.array(flat, dtype=np.float64).reshape(-1, 2), np.array(ends, dtype=np.uint32))
        if len(idx) == 0 or len(idx) > 65535:
            continue
        if hdm is None:
            hdm = building_height_dm(t, bv)
        enc = bytearray(struct.pack('<HH', min(65535, hdm), len(rings)))
        for r in rings:
            enc += struct.pack('<H', len(r) // 2) + array('h', r).tobytes()
        enc += struct.pack('<H', len(idx)) + idx.astype('<u2').tobytes()
        for code in codes_of(m):
            d = st.bld.setdefault(code, {})
            e = d.get((tx, ty))
            if e is None:
                e = d[(tx, ty)] = [0, bytearray()]
            e[0] += 1; e[1] += enc
        st.nbld += 1


def codes_of(m):
    out = []
    c = 0
    while m:
        if m & 1:
            out.append(c)
        m >>= 1; c += 1
    return out


# ---------------------------------------------------------------- land from coastline
def build_land(P, coast_lines, wpm):
    if not coast_lines:
        return P
    tree = shapely.STRtree(coast_lines)
    hit = tree.query(P, predicate='intersects')
    if len(hit) == 0:
        log('  no coastline inside -> whole polygon is land')
        return P
    lines = [coast_lines[i] for i in hit]
    # node coastline and the prefecture outline together (exact intersections), polygonize, keep faces inside P
    noded = shapely.union_all([P.boundary] + lines)
    faces = list(shapely.get_parts(shapely.polygonize(list(shapely.get_parts(noded)))))
    shapely.prepare(P)
    rp = shapely.point_on_surface(np.array(faces, dtype=object))
    faces = [f for f, ok in zip(faces, shapely.contains(P, rp)) if ok]
    log('  coast lines %d, faces %d' % (len(lines), len(faces)))
    # side votes: land on the LEFT of coastline direction (y grows south -> flip normal sign)
    eps = 0.3 * wpm
    px = []; py = []; side = []
    for ln in lines:
        c = np.asarray(ln.coords)
        d = np.diff(c, axis=0)
        L = np.hypot(d[:, 0], d[:, 1])
        ok = L > 4 * eps
        if not ok.any():
            continue
        mid = (c[:-1] + c[1:])[ok] / 2
        nrm = np.column_stack([d[ok, 1], -d[ok, 0]]) / L[ok, None]   # left normal in y-down world
        px.append(mid + nrm * eps); side.append(np.ones(ok.sum()))
        px.append(mid - nrm * eps); side.append(-np.ones(ok.sum()))
    pts = np.concatenate(px); side = np.concatenate(side)
    # query faces against a tree of sample points: the (large) faces are the prepared side -> fast
    ptree = shapely.STRtree(shapely.points(pts))
    fi, pi = ptree.query(np.array(faces, dtype=object), predicate='contains')
    votes = np.zeros(len(faces))
    np.add.at(votes, fi, side[pi])
    touched = np.zeros(len(faces), dtype=bool); touched[fi] = True
    # untouched faces adjacent to coast only via tiny slivers are rare; drop untouched faces that touch coast
    cu = shapely.union_all(lines)
    shapely.prepare(cu)
    fa = np.array(faces, dtype=object)
    unt = np.nonzero(~touched & (votes <= 0))[0]
    free = np.zeros(len(faces), dtype=bool)
    if len(unt):
        free[unt] = ~shapely.intersects(cu, fa[unt])
    keep2 = list(fa[(votes > 0) | free])
    land = shapely.make_valid(shapely.union_all(keep2))
    log('  land faces %d / %d' % (len(keep2), len(faces)))
    return land


# ---------------------------------------------------------------- per prefecture output
def gather_lines(st, sel):
    """indices -> (coords Nx2 float, part index)"""
    starts = np.frombuffer(st.L_start, dtype=np.int64)[sel]
    ns = np.frombuffer(st.L_n, dtype=np.int32)[sel].astype(np.int64)
    tot = int(ns.sum())
    off = np.repeat(starts - np.concatenate([[0], np.cumsum(ns)[:-1]]), ns) + np.arange(tot)
    XY = np.frombuffer(st.XY, dtype=np.int32).reshape(-1, 2)
    IDS = np.frombuffer(st.IDS, dtype=np.int64)
    part = np.repeat(np.arange(len(sel)), ns)
    return XY[off], IDS[off], part, ns


def tiles_intersecting(z, P):
    t = S >> z
    a, b, c, d = tile_range(z, *P.bounds)
    xs, ys = np.meshgrid(np.arange(a, c + 1), np.arange(b, d + 1))
    xs = xs.ravel(); ys = ys.ravel()
    boxes = shapely.box(xs * t, ys * t, (xs + 1) * t, (ys + 1) * t)
    hit = shapely.intersects(P, boxes)
    return list(zip(xs[hit].tolist(), ys[hit].tolist()))


def etc_links(st, sel, xy, part, ends, uniq, inv, first):
    """bool per selected way: *_link ways reachable from a Smart IC junction node via connected links only."""
    etc = np.zeros(len(sel), dtype=bool)
    smart = getattr(st, 'smart', None)
    if not smart:
        return etc
    is_link = (np.frombuffer(st.L_efl, dtype=np.uint8)[sel] & 32) != 0
    if not is_link.any():
        return etc
    nonlink_u = np.zeros(len(uniq), dtype=bool)
    nonlink_u[inv[~is_link[part]]] = True
    wcls = np.frombuffer(st.L_cls, dtype=np.int8)[sel]
    motor_u = np.zeros(len(uniq), dtype=bool)       # nodes on motorway/trunk main carriageways
    motor_u[inv[(~is_link & (wcls <= 1))[part]]] = True
    lp = np.nonzero(is_link[part])[0]
    lu = inv[lp]; lw = part[lp]
    o = np.argsort(lu, kind='stable'); lu = lu[o]; lw = lw[o]
    starts = np.searchsorted(lu, np.arange(len(uniq) + 1))
    link_nodes = np.unique(lu)
    uxy = xy[first].astype(np.float64)
    cand = link_nodes[nonlink_u[link_nodes]]
    seeds = []
    for nid, sx, sy in smart:
        k = np.searchsorted(uniq, nid)
        if k < len(uniq) and uniq[k] == nid and starts[k + 1] > starts[k]:
            seeds.append((int(k), sx, sy)); continue
        # junction node not on a link: nearest link node that also lies on a non-link road (diverge point), <=300 m
        if len(cand) == 0:
            continue
        dd = np.hypot(uxy[cand, 0] - sx, uxy[cand, 1] - sy)
        j = int(np.argmin(dd))
        if dd[j] <= 300 * wpm_at_wy(sy):
            seeds.append((int(cand[j]), sx, sy))
    for seed, sx, sy in seeds:
        lim = (1500 * wpm_at_wy(sy)) ** 2          # never wander more than 1.5 km from the junction
        seen = {seed}; q = [seed]
        while q:
            u = q.pop()
            for w in lw[starts[u]:starts[u + 1]].tolist():
                if etc[w]:
                    continue
                etc[w] = True
                for v in inv[ends[w]:ends[w + 1]].tolist():
                    # walk on through link-only nodes and through the junction with the local road
                    # (to pick up the on-ramp starting there), but never back out onto the motorway/trunk
                    if v not in seen and not motor_u[v] and                             (uxy[v, 0] - sx) ** 2 + (uxy[v, 1] - sy) ** 2 < lim:
                        seen.add(v); q.append(v)
    return etc


def build_regulations(st, sel, seg_way, seg_idx, e_from, e_to, firstEdge, gid, e_rev):
    """TRES (turn restrictions) and TCON (time-conditional closures) for the final, sorted edge list.
    seg_way: local way index per undirected segment; seg_idx: segment index per final edge."""
    e_way = seg_way[seg_idx]                      # local (sel) way index per final edge
    nE = len(e_way)
    wid = np.frombuffer(st.L_wid, dtype=np.int64)[sel] if len(st.L_wid) else np.zeros(0, np.int64)
    # --- TCON
    tcon_e = []
    cond = getattr(st, 'cond', {})
    if cond:
        loc_of = {int(g): i for i, g in enumerate(sel.tolist()) if int(g) in cond}
        if loc_of:
            locs = np.array(list(loc_of.values()))
            hit = np.nonzero(np.isin(e_way, locs))[0]
            for e in hit.tolist():
                tcon_e.append((e, cond[int(sel[e_way[e]])]))
    tcon = bytearray(struct.pack('<I', len(tcon_e)))
    for e, mk in tcon_e:
        tcon += struct.pack('<I', e) + mk
    # --- TRES
    pairs = set()
    restr = getattr(st, 'restr', [])
    nok = 0
    if restr and len(wid):
        wpos = {int(w): i for i, w in enumerate(wid.tolist())}
        in_order = np.argsort(e_to, kind='stable')
        in_sorted = e_to[in_order]
        for only, fw, vn, tw in restr:
            fl = wpos.get(fw); tl = wpos.get(tw)
            if fl is None or tl is None:
                continue
            k = np.searchsorted(gid, vn)
            if k >= len(gid) or gid[k] != vn:
                continue
            v = int(k)
            a0, a1 = np.searchsorted(in_sorted, [v, v + 1])
            ins = [int(e) for e in in_order[a0:a1] if e_way[e] == fl]
            outs = list(range(int(firstEdge[v]), int(firstEdge[v + 1])))
            tos = [e for e in outs if e_way[e] == tl]
            if not ins or not tos:
                continue
            nok += 1
            for f in ins:
                if only:
                    for t in outs:
                        if t not in tos:
                            pairs.add((f, t))
                else:
                    for t in tos:
                        pairs.add((f, t))
    pl = sorted(pairs)
    tres = bytearray(struct.pack('<I', len(pl)))
    for f, t in pl:
        tres += struct.pack('<II', f, t)
    log('  TRES: %d restrictions mapped -> %d forbidden turns; TCON: %d conditional edges' % (nok, len(pl), len(tcon_e)))
    # --- TRSV: via が way の禁止。from の辺 → via の辺（順に） → to の辺、の並びを通らせない
    seqs = set()
    nokv = 0
    restr_v = getattr(st, 'restr_v', [])
    if restr_v and len(wid):
        wpos = {int(w): i for i, w in enumerate(wid.tolist())}
        by_way = {}
        for e, w in enumerate(e_way.tolist()):
            by_way.setdefault(w, []).append(e)

        def outs_of(n):
            return range(int(firstEdge[n]), int(firstEdge[n + 1]))

        for only, fw, vws, tw in restr_v:
            fl = wpos.get(fw); tl = wpos.get(tw); vl = [wpos.get(v) for v in vws]
            if fl is None or tl is None or None in vl:
                continue
            found = False
            for f in by_way.get(fl, []):
                # 深さ優先で via を順にたどる（via の辺は 12 本まで）
                stack = [([f], 0)]
                while stack:
                    chain, vi = stack.pop()
                    if len(chain) > 13:
                        continue
                    last = chain[-1]
                    for e in outs_of(int(e_to[last])):
                        if e_to[e] == e_from[last] and e_way[e] == e_way[last]:
                            continue  # 同じ道を戻らない
                        w = int(e_way[e])
                        if vi < len(vl) and w == vl[vi]:
                            stack.append((chain + [e], vi))
                        elif vi + 1 < len(vl) and w == vl[vi + 1] and len(chain) > 1:
                            stack.append((chain + [e], vi + 1))
                        elif vi == len(vl) - 1 and len(chain) > 1:
                            if not only and w == tl:
                                seqs.add(tuple(chain + [e])); found = True
                            elif only and w != tl and w != vl[vi]:
                                seqs.add(tuple(chain + [e])); found = True
            if found:
                nokv += 1
    trsv = bytearray(struct.pack('<I', len(seqs)))
    for q in sorted(seqs):
        trsv += struct.pack('<H', len(q)) + struct.pack('<%dI' % len(q), *q)
    log('  TRSV: %d via-way restrictions mapped -> %d forbidden sequences' % (nokv, len(seqs)))
    # --- LANE
    lanes = getattr(st, 'lanes', {})
    lane_e = []
    if lanes:
        loc = {i: lanes[int(g)] for i, g in enumerate(sel.tolist()) if int(g) in lanes}
        if loc:
            hit = np.nonzero(np.isin(e_way, np.array(list(loc.keys()))))[0]
            for e in hit.tolist():
                v = loc[int(e_way[e])][1 if e_rev[e] else 0]
                if v:
                    lane_e.append((e, v.encode('utf-8')[:65535]))
    lane = bytearray(struct.pack('<I', len(lane_e)))
    for e, v in lane_e:
        lane += struct.pack('<IH', e, len(v)) + v
    log('  LANE: %d directed edges with turn:lanes' % len(lane_e))
    # --- LCNT
    lane_n = getattr(st, 'lane_n', {})
    lcnt_e = []
    if lane_n:
        loc = {i: lane_n[int(g)] for i, g in enumerate(sel.tolist()) if int(g) in lane_n}
        if loc:
            hit = np.nonzero(np.isin(e_way, np.array(list(loc.keys()))))[0]
            for e in hit.tolist():
                v = loc[int(e_way[e])][1 if e_rev[e] else 0]
                if v >= 2:
                    lcnt_e.append((e, v))
    lcnt_e.sort()
    lcnt = bytearray(struct.pack('<I', len(lcnt_e)))
    for e, v in lcnt_e:
        lcnt += struct.pack('<IB', e, v)
    log('  LCNT: %d directed edges with 2+ lanes' % len(lcnt_e))
    return bytes(tres), bytes(tcon), bytes(lane), bytes(lcnt), bytes(trsv)


def build_rout(st, code, sel_all, P, signals):
    bit = 1 << code
    mask = np.frombuffer(st.L_mask, dtype=np.int64)
    route = np.frombuffer(st.L_route, dtype=np.int8)
    sel = sel_all[(route[sel_all] == 1)]
    xy, ids, part, ns = gather_lines(st, sel)
    npt = len(ids)
    uniq, first, inv, counts = np.unique(ids, return_index=True, return_inverse=True, return_counts=True)
    ends = np.concatenate([[0], np.cumsum(ns)])
    isg = counts >= 2
    isg[inv[ends[:-1]]] = True
    isg[inv[ends[1:] - 1]] = True
    gnum = np.full(len(uniq), -1, dtype=np.int64)
    gnum[isg] = np.arange(isg.sum())
    gpt = isg[inv]
    gp = np.nonzero(gpt)[0]
    a = gp[:-1]; b = gp[1:]
    v = part[a] == part[b]
    a = a[v]; b = b[v]
    # lengths
    d = np.diff(xy.astype(np.float64), axis=0)
    _, lat = to_lonlat(0, (xy[:-1, 1] + xy[1:, 1]) / 2.0)
    seg_m = np.hypot(d[:, 0], d[:, 1]) * (EARTH * np.cos(np.radians(lat)) / S)
    cum = np.concatenate([[0.0], np.cumsum(seg_m)])
    length = cum[b] - cum[a]
    w = sel[part[a]]
    fr = gnum[inv[a]]; to = gnum[inv[b]]
    ps = a + 1; pc = b - a - 1
    ow = np.frombuffer(st.L_ow, dtype=np.int8)[w]
    cls = np.frombuffer(st.L_cls, dtype=np.int8)[w].astype(np.uint8)
    efl = np.frombuffer(st.L_efl, dtype=np.uint8)[w]
    ms = np.frombuffer(st.L_ms, dtype=np.uint8)[w]
    gname = np.frombuffer(st.L_name, dtype=np.int32)[w]
    # local name table
    un = np.unique(gname[gname >= 0])
    lname = np.full(len(st.name_list) + 1, 0xFFFFFFFF, dtype=np.uint32)
    lname[un] = np.arange(len(un), dtype=np.uint32)
    nm = np.where(gname >= 0, lname[np.maximum(gname, 0)], 0xFFFFFFFF).astype(np.uint32)
    fwd = ow >= 0; bwd = ow <= 0
    E_from = np.concatenate([fr[fwd], to[bwd]])
    E_to = np.concatenate([to[fwd], fr[bwd]])
    E_ps = np.concatenate([ps[fwd], ps[bwd]])
    E_pc = np.concatenate([pc[fwd], pc[bwd]])
    E_len = np.concatenate([length[fwd], length[bwd]])
    E_cls = np.concatenate([cls[fwd], cls[bwd]])
    etc = etc_links(st, sel, xy, part, ends, uniq, inv, first)
    efl = efl | np.where(etc[part[a]], 64, 0).astype(np.uint8)
    E_fl = np.concatenate([efl[fwd], efl[bwd] | 1])
    E_nm = np.concatenate([nm[fwd], nm[bwd]])
    E_ms = np.concatenate([ms[fwd], ms[bwd]])
    ok = ~((E_from == E_to) & (E_pc == 0))
    assert E_pc.max(initial=0) <= 65535
    order = np.argsort(E_from[ok], kind='stable')
    sel_e = np.nonzero(ok)[0][order]
    nN = int(isg.sum()); nE = len(sel_e)
    edt = np.dtype([('to', '<u4'), ('len', '<u4'), ('ps', '<u4'), ('pc', '<u2'), ('cls', 'u1'), ('fl', 'u1'),
                    ('name', '<u4'), ('ms', 'u1'), ('p1', 'u1'), ('p2', '<u2')])
    assert edt.itemsize == 24
    E = np.zeros(nE, dtype=edt)
    E['to'] = E_to[sel_e]; E['len'] = np.minimum(np.rint(E_len[sel_e] * 100), 0xFFFFFFFF).astype(np.uint32)
    E['ps'] = E_ps[sel_e]; E['pc'] = E_pc[sel_e]; E['cls'] = E_cls[sel_e]; E['fl'] = E_fl[sel_e]
    E['name'] = E_nm[sel_e]; E['ms'] = E_ms[sel_e]
    firstEdge = np.searchsorted(E_from[sel_e], np.arange(nN + 1)).astype('<u4')
    gid = uniq[isg]
    tres, tcon, lane, lcnt, trsv = build_regulations(st, sel, part[a], np.concatenate([fwd.nonzero()[0], bwd.nonzero()[0]])[sel_e],
                                         E_from[sel_e], E_to[sel_e], firstEdge, gid, (E_fl[sel_e] & 1) != 0)
    gxy = xy[first[isg]]
    # flags
    cross = np.frombuffer(st.L_cross, dtype=np.int8)[sel]
    border_u = np.zeros(len(uniq), dtype=bool)
    border_u[inv[cross[part] == 1]] = True
    nflags = np.zeros(nN, dtype=np.uint8)
    nflags[border_u[isg]] |= 1
    sig = np.frombuffer(signals, dtype=np.int64)
    nflags[np.isin(gid, sig)] |= 2
    sig_att = attach_to_graph(ids, part, cum, isg, inv, gp, sig, 20.0)
    for u, dist in sig_att.values():
        nflags[gnum[u]] |= 2
    for bitv, arr in ((4, getattr(st, 'lx', None)), (8, getattr(st, 'stops', None))):
        if arr is not None and len(arr):
            for u, dist in attach_to_graph(ids, part, cum, isg, inv, gp, np.frombuffer(arr, dtype=np.int64), 20.0).values():
                nflags[gnum[u]] |= bitv
    log('  level crossings %d, stop signs %d (graph nodes)' % (int(((nflags & 4) != 0).sum()), int(((nflags & 8) != 0).sum())))
    jn = getattr(st, 'jnames', {})
    jnam_l = []
    if jn:
        att = attach_to_graph(ids, part, cum, isg, inv, gp, np.fromiter(jn.keys(), dtype=np.int64), 30.0)
        best = {}
        for nid, (u, dist) in att.items():
            nm, hw = jn[nid]
            if hw == 'crossing' and dist > 0:
                continue
            g = int(gnum[u])
            if g not in best or dist < best[g][1]:
                best[g] = (nm, dist)
        jnam_l = sorted((g, v[0]) for g, v in best.items())
    jnam = bytearray(struct.pack('<I', len(jnam_l)))
    for g, nm in jnam_l:
        b = nm.encode('utf-8')[:65535]
        jnam += struct.pack('<IH', g, len(b)) + b
    log('  signals on graph nodes: %d (incl. %d attached within 20 m); JNAM: %d named intersections' % (
        int(((nflags & 2) != 0).sum()), sum(1 for u, d in sig_att.values() if d > 0), len(jnam_l)))
    names = [st.name_list[i].encode('utf-8') for i in un]
    noff = np.concatenate([[0], np.cumsum([len(x) for x in names])]).astype('<u4')
    nb = b''.join(names)
    out = bytearray(struct.pack('<6I', 0x48505247, nN, nE, npt, len(names), len(nb)))
    out += gxy[:, 0].astype('<i4').tobytes() + gxy[:, 1].astype('<i4').tobytes()
    out += firstEdge.tobytes() + gid.astype('<i8').tobytes() + nflags.tobytes()
    out += b'\0' * ((-len(out)) % 4)
    out += E.tobytes()
    out += xy.astype('<i4').tobytes()
    out += noff.tobytes() + nb
    log('  ROUT: %d nodes, %d edges (%d ETC-only), %d pts, %d names, %.1f MB' % (
        nN, nE, int(((E['fl'] & 64) != 0).sum()), npt, len(names), len(out) / 1e6))
    return bytes(out), tres, tcon, lane, lcnt, trsv, bytes(jnam)


def process_pref(code, name, P, st, out_dir, work_dir, date):
    log('== %02d %s' % (code, name))
    bit = 1 << code
    c = P.centroid
    wpm = wpm_at_wy(c.y)
    land = build_land(P, st.coast_geoms, wpm)
    mask = np.frombuffer(st.L_mask, dtype=np.int64)
    sel_all = np.nonzero(mask & bit)[0]
    clsarr = np.frombuffer(st.L_cls, dtype=np.int8)
    flarr = np.frombuffer(st.L_flags, dtype=np.uint8)
    xy, ids, part, ns = gather_lines(st, sel_all)
    geoms = shapely.linestrings(xy.astype(np.float64), indices=part)
    areas_p = [(a_cls, g, nm) for (a_cls, m, g, nm) in st.areas if m & bit]
    # clip area features (water/park/forest/farm/sand) to this prefecture's land (+20 m) so they don't spill
    # into the neighbour as tile-shaped blocks
    if areas_p:
        lb = shapely.buffer(shapely.simplify(land, 5 * wpm), 20 * wpm)
        shapely.prepare(lb)
        garr = np.array([g for _, g, _ in areas_p], dtype=object)
        inside = shapely.contains(lb, garr)
        out = []
        for (a_cls, g, nm), ins in zip(areas_p, inside):
            if not ins:
                g = shapely.make_valid(shapely.intersection(g, lb))
                if g.is_empty or not polygons_of(g):
                    continue
            out.append((a_cls, g, nm))
        log('  areas %d, clipped at border %d, kept %d' % (len(areas_p), int((~inside).sum()), len(out)))
        areas_p = out
    area_m2 = np.array([g.area for _, g, _ in areas_p]) / (wpm * wpm) if areas_p else np.zeros(0)
    pts_p = [p for p in st.pts if p[5] & bit]
    # labels
    labels = []
    for x, y, kind, nm, rd, m, lab, *_ in pts_p:
        if lab:
            labels.append((x, y, kind, KIND_PRIO[kind], nm))
    for (a_cls, g, nm), am in zip(areas_p, area_m2):
        if a_cls == 1 and nm and am > 2e4:
            pp = g.point_on_surface()
            labels.append((pp.x, pp.y, 9, KIND_PRIO[9], nm))
    lp = max(shapely.get_parts(land), key=lambda g: g.area).point_on_surface() if not land.is_empty else c
    labels.append((lp.x, lp.y, 0, 255, name))
    tiles = TileStore(dir_=work_dir)
    for z, prm in LEVELS.items():
        tol = prm['tol'] * wpm
        cand = tiles_intersecting(z, P)
        areas = {}
        lnd = shapely.simplify(land, tol, preserve_topology=True) if z < 14 else land
        areas[0] = polygons_of(shapely.make_valid(lnd))
        for (a_cls, g, nm), am in zip(areas_p, area_m2):
            if a_cls in prm['areas'] and am >= prm['areas'][a_cls]:
                gg = shapely.simplify(g, tol, preserve_topology=True) if z < 14 else g
                areas.setdefault(a_cls, []).extend(polygons_of(gg))
        lines = {}
        wcls = clsarr[sel_all]
        want = np.isin(wcls, list(prm['roads'] | prm['other']))
        idx = np.nonzero(want)[0]
        sg = shapely.simplify(geoms[idx], tol, preserve_topology=False)
        for i, g in zip(idx.tolist(), sg):
            cl = int(wcls[i]); fl = int(flarr[sel_all[i]])
            co = shapely.get_coordinates(g)
            if len(co) >= 2:
                lines.setdefault(cl, []).append((fl, co))
        labs = [l for l in labels if l[2] in prm['labels']]
        n0 = len(tiles.idx)
        for tx, ty, raw in build_level_tiles(z, cand, areas, lines, labs,
                                             buildings=st.bld.get(code) if z == 14 else None):
            tiles.add(z, tx, ty, raw)
        log('  z%d: %d candidate tiles -> %d tiles' % (z, len(cand), len(tiles.idx) - n0))
    rout, tres, tcon, lane, lcnt, trsv, jnam = build_rout(st, code, sel_all, P, st.signals)
    srch = encode_srch([(p[0], p[1], p[2], p[3], p[4]) for p in pts_p] + [(lp.x, lp.y, 0, name, '')])
    poic = bytes(struct.pack('<I', len(pts_p) + 1) + bytes([p[7] if len(p) > 7 else 0xFF for p in pts_p] + [0xFF]))
    path = os.path.join(out_dir, '%02d.ctm' % code)
    PB = shapely.buffer(shapely.simplify(P, 50 * wpm), 200 * wpm)
    shapely.prepare(PB)
    cams = [c for c in getattr(st, 'cams', []) if shapely.contains_xy(PB, c[0], c[1])]
    cb = bytearray(struct.pack('<I', len(cams)))
    for x, y, deg, ty, ms in cams:
        cb += struct.pack('<iihBB', int(round(x)), int(round(y)), int(deg), ty, ms)
    log('  CAMS: %d cameras' % len(cams))
    write_ctm(path, code, name, P.bounds, date, tiles, rout=rout, srch=srch, cams=bytes(cb),
              extra=[('TRES', tres), ('TCON', tcon), ('LANE', lane), ('LCNT', lcnt), ('POIC', poic), ('JNAM', jnam),
                     ('TRSV', trsv), ('MUNI', encode_munis(st.munis, P, wpm))])
    log('  wrote %s %.1f MB (tiles raw %.1f MB)' % (path, os.path.getsize(path) / 1e6, tiles.raw / 1e6))
    # intermediate for the base pack
    t60 = 60 * wpm
    land60 = shapely.make_valid(shapely.simplify(land, t60, preserve_topology=True))
    major = []
    for cl in (0, 1):
        ii = np.nonzero(clsarr[sel_all] == cl)[0]
        if len(ii):
            mg = shapely.line_merge(shapely.union_all(geoms[ii]))
            for g in lines_of(shapely.simplify(mg, t60)):
                major.append((cl, shapely.to_wkb(g)))
    blabels = [(lp.x, lp.y, 0, 255, name)] + [(x, y, k, pr, nm) for x, y, k, pr, nm in labels if k == 1]
    with open(os.path.join(work_dir, '%02d_base.pkl' % code), 'wb') as f:
        pickle.dump(dict(code=code, name=name, bbox=P.bounds, date=date, land=shapely.to_wkb(land60),
                         roads=major, labels=blabels, wpm=wpm), f)


def main():
    global T0
    T0 = time.time()
    ap = argparse.ArgumentParser()
    ap.add_argument('pbf')
    ap.add_argument('--out', default=os.path.join(ROOT, 'data', 'packs'))
    ap.add_argument('--work', default=os.path.join(ROOT, 'data', 'work'))
    ap.add_argument('--prefs', default='')
    ap.add_argument('--index', default='flex_mem')
    ap.add_argument('--reuse', action='store_true', help='cache/reuse the main pass (debugging)')
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True); os.makedirs(a.work, exist_ok=True)
    wanted = set(int(x) for x in a.prefs.split(',') if x.strip())
    if not wanted:
        base = os.path.basename(a.pbf).split('-')[0].split('.')[0].lower()
        wanted = set(REGIONS.get(base, []))
    hdr = osmium.io.Reader(a.pbf, osmium.osm.osm_entity_bits.NOTHING)
    ts = hdr.header().get('osmosis_replication_timestamp')
    hdr.close()
    date = int(ts[:10].replace('-', '')) if ts else int(datetime.date.today().strftime('%Y%m%d'))
    log('prefectures wanted:', sorted(wanted) or 'all', 'data date', date)
    prefs = load_prefs(a.pbf, wanted)
    log('boundaries:', {c: n for c, (n, g) in sorted(prefs.items())})
    if not prefs:
        sys.exit('no prefecture boundary found')
    A = Assign(prefs)
    log('assign grid: %d interior, %d border tiles' % (len(A.interior), len(A.border)))
    cache = os.path.join(a.work, os.path.basename(a.pbf) + '.%s.pass.pkl' % '_'.join(map(str, sorted(prefs))))
    if a.reuse and os.path.exists(cache):
        with open(cache, 'rb') as f:
            st = pickle.load(f)
        log('reused main pass cache', cache)
    else:
        st = Store()
        load_enforcements(a.pbf, st)
        load_restrictions(a.pbf, st)
        log('relations: %d enforcement, %d turn restrictions' % (len(st.enf), len(st.restr)))
        run_main_pass(a.pbf, A, st, a.index)
        if a.reuse:
            with open(cache, 'wb') as f:
                pickle.dump(st, f, protocol=pickle.HIGHEST_PROTOCOL)
    st.munis = load_munis(a.pbf)
    log('boundaries: %d municipalities and wards' % len(st.munis))
    st.jnames, sig2, st.lx, st.stops = load_junction_tags(a.pbf)
    st.signals = sig2
    log('junction tags: %d named intersections, %d signals, %d level crossings, %d stop signs' % (
        len(st.jnames), len(sig2), len(st.lx), len(st.stops)))
    st.coast_geoms = [shapely.LineString(c.astype(np.float64)) for c in st.coast if len(c) >= 2]
    st.coast = None
    for code in sorted(prefs):
        name, P = prefs[code]
        process_pref(code, name, P, st, a.out, a.work, date)
    try:
        import psutil
        log('done, peak memory %.0f MB' % (psutil.Process().memory_info().peak_wset / 1e6))
    except Exception:
        log('done')


if __name__ == '__main__':
    main()
