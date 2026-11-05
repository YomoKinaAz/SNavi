#!/usr/bin/env python3
"""Write index.json for all .ctm packs in a directory (default ../data/packs)."""
import os, sys, json, hashlib, struct, argparse

HERE = os.path.dirname(os.path.abspath(__file__))


def header(path):
    with open(path, 'rb') as f:
        d = f.read(512)
    assert d[:4] == b'CTM1', path
    ver, code = struct.unpack_from('<HH', d, 4)
    nl, = struct.unpack_from('<H', d, 24)
    name = d[26:26 + nl].decode('utf-8')
    date, = struct.unpack_from('<I', d, 26 + nl)
    return code, name, date


def sha1(path):
    h = hashlib.sha1()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('dir', nargs='?', default=os.path.join(os.path.dirname(HERE), 'data', 'packs'))
    a = ap.parse_args()
    packs = []
    for fn in sorted(os.listdir(a.dir)):
        if not fn.endswith('.ctm'):
            continue
        p = os.path.join(a.dir, fn)
        code, name, date = header(p)
        packs.append(dict(code=code, name=name, file=fn, size=os.path.getsize(p), sha1=sha1(p), date=date))
    packs.sort(key=lambda x: x['code'])
    idx = dict(version=str(max([p['date'] for p in packs], default=0)), packs=packs)
    out = os.path.join(a.dir, 'index.json')
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(idx, f, ensure_ascii=False, indent=1)
    for p in packs:
        print('%02d %-8s %-12s %8.1f MB' % (p['code'], p['name'], p['file'], p['size'] / 1e6))
    print('wrote', out)


if __name__ == '__main__':
    main()
