"""Shared helpers for the SNavi map pack format (.ctm v1). See ../doc/format.md."""
import math, struct, zlib, tempfile, os
import numpy as np
import shapely
import mapbox_earcut as earcut

S = 2 ** 31                 # world size
EXT = 4096                  # tile extent
LINE_BUF = 128              # line clip buffer (tile units)
EARTH = 40075016.686


def to_world(lon, lat):
    lon = np.asarray(lon, dtype=np.float64)
    lat = np.radians(np.clip(np.asarray(lat, dtype=np.float64), -85.05, 85.05))
    x = (lon + 180.0) / 360.0 * S
    y = (1.0 - np.arcsinh(np.tan(lat)) / math.pi) / 2.0 * S
    return x, y


def to_lonlat(x, y):
    x = np.asarray(x, dtype=np.float64); y = np.asarray(y, dtype=np.float64)
    lon = x / S * 360.0 - 180.0
    lat = np.degrees(np.arctan(np.sinh(math.pi * (1 - 2 * y / S))))
    return lon, lat


def world_xy_transform(coords):
    """shapely.transform callback lon/lat -> world"""
    x, y = to_world(coords[:, 0], coords[:, 1])
    return np.column_stack([x, y])


def wpm_at_wy(wy):
    """world units per metre at world y"""
    _, lat = to_lonlat(0, wy)
    return S / (EARTH * math.cos(math.radians(float(lat))))


def tile_size(z):
    return S >> z


def tile_bounds(z, x, y):
    t = S >> z
    return x * t, y * t, (x + 1) * t, (y + 1) * t


def tile_range(z, minx, miny, maxx, maxy):
    t = S >> z
    n = (1 << z) - 1
    return (max(0, int(minx // t)), max(0, int(miny // t)), min(n, int(maxx // t)), min(n, int(maxy // t)))


def to_local(xy, z, tx, ty):
    t = S >> z
    a = np.asarray(xy, dtype=np.float64)
    out = np.empty_like(a)
    out[:, 0] = (a[:, 0] - tx * t) * (EXT / t)
    out[:, 1] = (a[:, 1] - ty * t) * (EXT / t)
    return out


def q16(a):
    return np.clip(np.rint(a), -32768, 32767).astype('<i2')


def dedupe(p):
    if len(p) < 2:
        return p
    keep = np.ones(len(p), dtype=bool)
    keep[1:] = np.any(p[1:] != p[:-1], axis=1)
    return p[keep]


# ---------------------------------------------------------------- tile encoding
def triangulate_polygon_local(poly, z, tx, ty):
    """shapely Polygon (world) -> int16 triangle-list vertices (N*3, 2)"""
    rings = [np.asarray(poly.exterior.coords)[:-1]] + [np.asarray(r.coords)[:-1] for r in poly.interiors]
    rings = [r for r in rings if len(r) >= 3]
    if not rings:
        return None
    verts = to_local(np.concatenate(rings), z, tx, ty)
    ends = np.cumsum([len(r) for r in rings]).astype(np.uint32)
    idx = earcut.triangulate_float64(verts, ends)
    if len(idx) == 0:
        return None
    return q16(verts[idx])


def polygons_of(g):
    if g is None or g.is_empty:
        return []
    t = g.geom_type
    if t == 'Polygon':
        return [g]
    if t in ('MultiPolygon', 'GeometryCollection'):
        out = []
        for p in g.geoms:
            out.extend(polygons_of(p))
        return out
    return []


def lines_of(g):
    if g is None or g.is_empty:
        return []
    t = g.geom_type
    if t == 'LineString':
        return [g]
    if t in ('MultiLineString', 'GeometryCollection'):
        out = []
        for p in g.geoms:
            out.extend(lines_of(p))
        return out
    return []


def layer(type_, cls, body):
    return struct.pack('<BBI', type_, cls, len(body)) + body


def area_layer(cls, tri_list):
    v = np.concatenate(tri_list) if tri_list else np.zeros((0, 2), '<i2')
    return layer(1, cls, struct.pack('<I', len(v)) + v.astype('<i2').tobytes())


def line_layer(cls, lines):
    """lines: list of (flags, int16 Nx2)"""
    out = bytearray()
    n = 0
    for flags, p in lines:
        for s in range(0, len(p) - 1, 65534):
            seg = p[s:s + 65535]
            if len(seg) < 2:
                continue
            out += struct.pack('<BH', flags, len(seg)) + seg.astype('<i2').tobytes()
            n += 1
    return layer(2, cls, struct.pack('<I', n) + bytes(out)), n


def label_layer(labels):
    """labels: list of (x16, y16, kind, prio, name)"""
    out = bytearray(struct.pack('<I', len(labels)))
    for x, y, kind, prio, name in labels:
        b = name.encode('utf-8')[:65535]
        out += struct.pack('<hhBBH', x, y, kind, prio, len(b)) + b
    return layer(4, 0, bytes(out))


def building_layer(count, body):
    return layer(3, 0, struct.pack('<I', count) + bytes(body))


def encode_building(outer_local, inners_local, height_dm):
    """rings as float local coords (closed rings allowed); returns bytes or None"""
    rings = []
    for r in [outer_local] + list(inners_local):
        r = dedupe(q16(r))
        if len(r) > 1 and np.array_equal(r[0], r[-1]):
            r = r[:-1]
        if len(r) >= 3:
            rings.append(r)
        elif not rings:
            return None
    if not rings:
        return None
    allp = np.concatenate(rings)
    if len(allp) > 65535:
        return None
    ends = np.cumsum([len(r) for r in rings]).astype(np.uint32)
    idx = earcut.triangulate_float64(allp.astype(np.float64), ends)
    if len(idx) == 0 or len(idx) > 65535:
        return None
    out = bytearray(struct.pack('<HH', min(65535, int(height_dm)), len(rings)))
    for r in rings:
        out += struct.pack('<H', len(r)) + r.tobytes()
    out += struct.pack('<H', len(idx)) + np.asarray(idx, '<u2').tobytes()
    return bytes(out)


def tile_blob(layers):
    return struct.pack('<B', len(layers)) + b''.join(layers)


# ---------------------------------------------------------------- pack writer
class TileStore:
    """Compresses tiles into a temp file; keeps (z,x,y,pos,clen,ulen)."""

    def __init__(self, dir_=None):
        self.f = tempfile.TemporaryFile(dir=dir_)
        self.idx = []
        self.pos = 0
        self.raw = 0

    def add(self, z, x, y, raw):
        c = zlib.compress(raw, 7)
        self.f.write(c)
        self.idx.append((z, x, y, self.pos, len(c), len(raw)))
        self.pos += len(c)
        self.raw += len(raw)


def write_ctm(path, code, name, bbox, date, tiles, rout=None, srch=None, cams=None, extra=None):
    """extra: list of (4-char id, bytes) optional sections appended after the standard ones"""
    nameb = name.encode('utf-8')
    body = [('ROUT', rout), ('SRCH', srch), ('CAMS', cams)] + list(extra or [])
    body = [(k, v) for k, v in body if v is not None]
    secs = ['TIDX', 'TDAT'] + [k for k, _ in body]
    hdr_len = 4 + 2 + 2 + 16 + 2 + len(nameb) + 4 + 2 + 20 * len(secs)
    idx = sorted(tiles.idx)

    def align(p, a=8):
        return (p + a - 1) // a * a

    off = {}
    p = align(hdr_len)
    off['TIDX'] = (p, 4 + 28 * len(idx)); p = align(p + off['TIDX'][1])
    off['TDAT'] = (p, tiles.pos); p = align(p + tiles.pos)
    for k, v in body:
        off[k] = (p, len(v)); p = align(p + len(v))
    tdat = off['TDAT'][0]
    tmp = path + '.part'
    with open(tmp, 'wb') as f:
        h = bytearray(b'CTM1' + struct.pack('<HH', 1, code) + struct.pack('<iiii', *[int(v) for v in bbox]))
        h += struct.pack('<H', len(nameb)) + nameb + struct.pack('<IH', date, len(secs))
        for s in secs:
            h += s.encode() + struct.pack('<QQ', *off[s])
        assert len(h) == hdr_len
        f.write(h)

        def pad_to(n):
            f.write(b'\0' * (n - f.tell()))
        pad_to(off['TIDX'][0])
        b = bytearray(struct.pack('<I', len(idx)))
        for z, x, y, pos, clen, ulen in idx:
            b += struct.pack('<BBHIIQII', z, 0, 0, x, y, tdat + pos, clen, ulen)
        f.write(b)
        pad_to(tdat)
        tiles.f.seek(0)
        while True:
            chunk = tiles.f.read(1 << 22)
            if not chunk:
                break
            f.write(chunk)
        for k, v in body:
            pad_to(off[k][0]); f.write(v)
    os.replace(tmp, path)


def encode_srch(entries):
    """entries: list of (x, y, kind, name, reading)"""
    out = bytearray(struct.pack('<I', len(entries)))
    for x, y, kind, name, reading in entries:
        a = name.encode('utf-8')[:65535]; r = (reading or '').encode('utf-8')[:65535]
        out += struct.pack('<iiBH', int(x), int(y), kind, len(a)) + a + struct.pack('<H', len(r)) + r
    return bytes(out)


# ---------------------------------------------------------------- generic tiling of world geometries
def build_level_tiles(z, cand_tiles, areas, lines, labels, buildings=None, max_labels=300):
    """
    cand_tiles: iterable of (tx,ty) allowed
    areas: dict cls -> list of shapely polygons (world)
    lines: dict cls -> list of (flags, Nx2 world float ndarray)
    labels: list of (x, y, kind, prio, name) world
    buildings: dict (tx,ty) -> (count, bytearray)  (z must be 14)
    yields (tx, ty, raw_bytes)
    """
    t = S >> z
    cand = set(cand_tiles)
    # areas: STRtree per class
    area_trees = {}
    for cls, polys in areas.items():
        if polys:
            area_trees[cls] = (shapely.STRtree(polys), polys)
    # lines: bucket by bbox tile range
    line_buckets = {}
    bt = LINE_BUF * t / EXT
    for cls, ls in lines.items():
        for flags, p in ls:
            x0, y0 = p.min(axis=0) - bt; x1, y1 = p.max(axis=0) + bt
            a, b, c, d = tile_range(z, x0, y0, x1, y1)
            for tx in range(a, c + 1):
                for ty in range(b, d + 1):
                    if (tx, ty) in cand:
                        line_buckets.setdefault((tx, ty), []).append((cls, flags, p))
    label_b = {}
    for lab in labels:
        k = (int(lab[0] // t), int(lab[1] // t))
        if k in cand:
            label_b.setdefault(k, []).append(lab)
    for (tx, ty) in sorted(cand):
        x0, y0, x1, y1 = tile_bounds(z, tx, ty)
        box = shapely.box(x0, y0, x1, y1)
        lay = []
        for cls in sorted(area_trees):
            tree, polys = area_trees[cls]
            hit = tree.query(box, predicate='intersects')
            tris = []
            for i in hit:
                g = polys[i]
                cg = shapely.clip_by_rect(g, x0, y0, x1, y1)
                for pg in polygons_of(cg):
                    if pg.area <= 0:
                        continue
                    tv = triangulate_polygon_local(pg, z, tx, ty)
                    if tv is not None:
                        tris.append(tv)
            if tris:
                lay.append(area_layer(cls, tris))
        lb = line_buckets.get((tx, ty))
        if lb:
            bycls = {}
            for cls, flags, p in lb:
                cg = shapely.clip_by_rect(shapely.LineString(p), x0 - bt, y0 - bt, x1 + bt, y1 + bt)
                for ln in lines_of(cg):
                    q = dedupe(q16(to_local(np.asarray(ln.coords), z, tx, ty)))
                    if len(q) >= 2:
                        bycls.setdefault(cls, []).append((flags, q))
            for cls in sorted(bycls):
                L, n = line_layer(cls, bycls[cls])
                if n:
                    lay.append(L)
        if buildings is not None and (tx, ty) in buildings:
            cnt, body = buildings[(tx, ty)]
            lay.append(building_layer(cnt, body))
        labs = label_b.get((tx, ty))
        if labs:
            labs = sorted(labs, key=lambda l: -l[3])[:max_labels]
            enc = []
            for x, y, kind, prio, name in labs:
                lp = to_local(np.array([[x, y]]), z, tx, ty)[0]
                enc.append((int(round(lp[0])), int(round(lp[1])), kind, prio, name))
            lay.append(label_layer(enc))
        if lay:
            yield tx, ty, tile_blob(lay)


# ---------------------------------------------------------------- reader
class CtmFile:
    def __init__(self, path):
        self.path = path
        with open(path, 'rb') as f:
            self.data = f.read()
        d = self.data
        assert d[:4] == b'CTM1', 'bad magic'
        self.version, self.code = struct.unpack_from('<HH', d, 4)
        self.bbox = struct.unpack_from('<iiii', d, 8)
        nl, = struct.unpack_from('<H', d, 24)
        self.name = d[26:26 + nl].decode('utf-8')
        p = 26 + nl
        self.date, ns = struct.unpack_from('<IH', d, p); p += 6
        self.sections = {}
        for _ in range(ns):
            sid = d[p:p + 4].decode(); o, l = struct.unpack_from('<QQ', d, p + 4); p += 20
            self.sections[sid] = (o, l)
        o, l = self.sections['TIDX']
        n, = struct.unpack_from('<I', d, o)
        self.tiles = {}
        for i in range(n):
            z, _, _, x, y, off, clen, ulen = struct.unpack_from('<BBHIIQII', d, o + 4 + 28 * i)
            self.tiles[(z, x, y)] = (off, clen, ulen)

    def tile_raw(self, key):
        off, clen, ulen = self.tiles[key]
        raw = zlib.decompress(self.data[off:off + clen])
        assert len(raw) == ulen, 'ulen mismatch'
        return raw


def parse_tile(raw):
    """-> list of (type, cls, payload) where payload is decoded geometry"""
    p = 0
    nl = raw[p]; p += 1
    out = []
    for _ in range(nl):
        t, cls, bl = struct.unpack_from('<BBI', raw, p); p += 6
        end = p + bl
        if t == 1:
            n, = struct.unpack_from('<I', raw, p)
            v = np.frombuffer(raw, '<i2', 2 * n, p + 4).reshape(-1, 2)
            assert n % 3 == 0 and p + 4 + 4 * n == end
            out.append((t, cls, v))
        elif t == 2:
            n, = struct.unpack_from('<I', raw, p); q = p + 4
            ls = []
            for _ in range(n):
                fl, np_ = struct.unpack_from('<BH', raw, q); q += 3
                ls.append((fl, np.frombuffer(raw, '<i2', 2 * np_, q).reshape(-1, 2))); q += 4 * np_
            assert q == end
            out.append((t, cls, ls))
        elif t == 3:
            n, = struct.unpack_from('<I', raw, p); q = p + 4
            bs = []
            for _ in range(n):
                h, nr = struct.unpack_from('<HH', raw, q); q += 4
                rings = []
                for _ in range(nr):
                    k, = struct.unpack_from('<H', raw, q); q += 2
                    rings.append(np.frombuffer(raw, '<i2', 2 * k, q).reshape(-1, 2)); q += 4 * k
                ni, = struct.unpack_from('<H', raw, q); q += 2
                idx = np.frombuffer(raw, '<u2', ni, q); q += 2 * ni
                tot = sum(len(r) for r in rings)
                assert ni % 3 == 0 and (ni == 0 or idx.max() < tot)
                bs.append((h, rings, idx))
            assert q == end
            out.append((t, cls, bs))
        elif t == 4:
            n, = struct.unpack_from('<I', raw, p); q = p + 4
            labs = []
            for _ in range(n):
                x, y, k, pr, ln = struct.unpack_from('<hhBBH', raw, q); q += 8
                labs.append((x, y, k, pr, raw[q:q + ln].decode('utf-8'))); q += ln
            assert q == end
            out.append((t, cls, labs))
        else:
            raise ValueError('bad layer type %d' % t)
        p = end
    assert p == len(raw)
    return out
