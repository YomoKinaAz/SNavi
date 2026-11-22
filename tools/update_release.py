#!/usr/bin/env python3
"""配信中の県の地図を最新の OSM データで作り直し、最新の GitHub Release の該当ファイルだけを差し替える。

対象は index.json に載っている県（コード 1〜47）だけ。全国の広域図・住所データ・施設データには触らない。
OSM のデータが配信中のものより新しくない県は差し替えない。

    python tools/update_release.py [--dry-run]      # --dry-run は作って確認するだけで、アップロードしない

定期実行では曜日ごとに担当の地域だけを作る（WEEKLY）。手動実行では配信中の県すべて（--prefs で絞れる）。
"""
import argparse
import datetime
import hashlib
import json
import os
import struct
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REGIONS = {'hokkaido': [1], 'tohoku': list(range(2, 8)), 'kanto': list(range(8, 15)),
           'chubu': list(range(15, 24)), 'kansai': list(range(24, 31)), 'chugoku': list(range(31, 36)),
           'shikoku': list(range(36, 40)), 'kyushu': list(range(40, 48))}
# 定期実行（毎日 03:00 JST）では、曜日ごとに 1 地域ずつ作り直す。各地域は 1 週間に 1 回更新される
WEEKLY = {6: ['hokkaido'], 0: ['tohoku'], 1: ['kanto'], 2: ['chubu'], 3: ['kansai'],
          4: ['chugoku', 'shikoku'], 5: ['kyushu']}   # 日 北海道・月 東北・火 関東・水 中部・木 関西・金 中国四国・土 九州
MIN_RATIO = 0.7   # 配信中より極端に小さいファイルは壊れているとみなす


MIRRORS = ['https://download.geofabrik.de/asia/japan/%s-latest.osm.pbf',
           'https://download.openstreetmap.fr/extracts/asia/japan/%s-latest.osm.pbf']


def download(region, path):
    """一時的な通信エラーでは止まらないよう、IPv4 で何度か試す。
    Geofabrik に GitHub の実行環境からつながらない日があるので、だめなら別の配布元（openstreetmap.fr）から取る"""
    for rnd in range(3):
        for m in MIRRORS:
            url = m % region
            print('download', url, flush=True)
            r = subprocess.run(['curl', '-4', '-fsSL', '--connect-timeout', '30', '--retry', '3', '--retry-delay', '20',
                                '--retry-all-errors', '-o', path, url])
            if r.returncode == 0 and os.path.exists(path) and os.path.getsize(path) > 1000000:
                return
            print('download failed (%d)' % r.returncode, flush=True)
        time.sleep(300)
    raise RuntimeError('download failed: ' + region)


def gh(*args):
    return subprocess.run(['gh'] + list(args), check=True, capture_output=True, text=True).stdout


def header(path):
    with open(path, 'rb') as f:
        d = f.read(4096)
    if d[:4] != b'CTM1':
        raise ValueError('not a pack: ' + path)
    code = struct.unpack('<H', d[6:8])[0]
    nl = struct.unpack('<H', d[24:26])[0]
    return code, struct.unpack('<I', d[26 + nl:30 + nl])[0]


def sha1(path):
    h = hashlib.sha1()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--prefs', default='', help='作る県を絞る（例: 21,23）。空なら配信中の県すべて')
    a = ap.parse_args()

    os.makedirs('out/packs', exist_ok=True)
    os.makedirs('out/work', exist_ok=True)
    os.makedirs('data/src', exist_ok=True)
    tag = gh('release', 'view', '--json', 'tagName', '--jq', '.tagName').strip()
    print('release:', tag, flush=True)
    gh('release', 'download', tag, '--pattern', 'index.json', '--dir', 'out', '--clobber')
    with open('out/index.json', encoding='utf-8') as f:
        index = json.load(f)
    packs = {p['code']: p for p in index['packs']}
    wanted = sorted(c for c in packs if 1 <= c <= 47)
    if a.prefs:
        only = {int(v) for v in a.prefs.split(',')}
        if not only <= set(wanted):
            raise ValueError('配信していない県は指定できません: %s' % sorted(only - set(wanted)))
        wanted = sorted(only)
    elif os.environ.get('GITHUB_EVENT_NAME') == 'schedule':
        jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
        today = WEEKLY[jst.weekday()]
        wanted = [c for c in wanted if any(c in REGIONS[r] for r in today)]
        print('今日（%s）の地域:' % '月火水木金土日'[jst.weekday()], today, flush=True)
    print('作る県:', wanted, flush=True)

    ready = []
    for region, codes in REGIONS.items():
        prefs = [c for c in wanted if c in codes]
        if not prefs:
            continue
        pbf = 'data/src/%s.osm.pbf' % region
        print('download', region, flush=True)
        download(region, pbf)
        subprocess.run([sys.executable, os.path.join(HERE, 'osm2pack.py'), pbf, '--out', 'out/packs', '--work', 'out/work',
                        '--prefs', ','.join(map(str, prefs))], check=True)
        os.remove(pbf)
        for c in prefs:
            old = packs[c]
            path = os.path.join('out/packs', old['file'])
            code, date = header(path)
            size = os.path.getsize(path)
            if code != c:
                raise ValueError('code mismatch: %s' % path)
            if size < old['size'] * MIN_RATIO:
                raise ValueError('%s が小さすぎます: %d -> %d' % (old['file'], old['size'], size))
            print('%s: date %d -> %d, size %d -> %d' % (old['file'], old['date'], date, old['size'], size), flush=True)
            if date > old['date']:
                ready.append((old, path, size, date))

    if not ready:
        print('新しい地図はありません')
        return
    if a.dry_run:
        print('dry run: 差し替え対象 %s（アップロードしません）' % [o['file'] for o, _, _, _ in ready])
        return
    for old, path, size, date in ready:
        gh('release', 'upload', tag, path, '--clobber')
        old.update(size=size, sha1=sha1(path), date=date)
    index['version'] = str(max(p['date'] for p in index['packs']))
    with open('out/index.json', 'w', encoding='utf-8', newline='\n') as f:
        json.dump(index, f, ensure_ascii=False, indent=1)
    gh('release', 'upload', tag, 'out/index.json', '--clobber')   # 地図を置いてから一覧を更新する
    print('差し替え完了:', [o['file'] for o, _, _, _ in ready])


if __name__ == '__main__':
    main()
