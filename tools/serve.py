#!/usr/bin/env python3
"""Serve data/packs over HTTP (Range supported, for resumable downloads in the app).

usage: python serve.py [--port 8000] [--dir ../data/packs]
"""
import argparse, os, re, socket, shutil, sys
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import unquote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))


class Handler(BaseHTTPRequestHandler):
    root = None

    def _resolve(self):
        path = unquote(urlparse(self.path).path).lstrip('/')
        if path == '':
            path = 'index.json'
        full = os.path.realpath(os.path.join(self.root, path))
        if not full.startswith(os.path.realpath(self.root) + os.sep) or not os.path.isfile(full):
            return None
        return full

    def do_HEAD(self):
        self.do_GET(head=True)

    def do_GET(self, head=False):
        full = self._resolve()
        if full is None:
            self.send_error(404); return
        size = os.path.getsize(full)
        start, end = 0, size - 1
        rng = self.headers.get('Range')
        status = 200
        if rng:
            m = re.match(r'bytes=(\d*)-(\d*)$', rng.strip())
            if not m:
                self.send_error(416); return
            if m.group(1):
                start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
            elif m.group(2):
                start = max(0, size - int(m.group(2)))
            if start >= size or start > end:
                self.send_response(416); self.send_header('Content-Range', 'bytes */%d' % size); self.end_headers(); return
            status = 206
        self.send_response(status)
        ctype = 'application/json; charset=utf-8' if full.endswith('.json') else 'application/octet-stream'
        self.send_header('Content-Type', ctype)
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(end - start + 1))
        if status == 206:
            self.send_header('Content-Range', 'bytes %d-%d/%d' % (start, end, size))
        self.end_headers()
        if head:
            return
        with open(full, 'rb') as f:
            f.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    chunk = f.read(min(left, 1 << 20))
                    if not chunk:
                        break
                    self.wfile.write(chunk); left -= len(chunk)
            except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
                pass


def lan_ips():
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(('10.255.255.255', 1)); ips.add(s.getsockname()[0]); s.close()
    except Exception:
        pass
    return sorted(i for i in ips if not i.startswith('127.'))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=8000)
    ap.add_argument('--dir', default=os.path.join(os.path.dirname(HERE), 'data', 'packs'))
    a = ap.parse_args()
    Handler.root = os.path.abspath(a.dir)
    srv = ThreadingHTTPServer(('0.0.0.0', a.port), Handler)
    print('serving', Handler.root)
    for ip in lan_ips():
        print('  http://%s:%d/' % (ip, a.port))
    print('  (Android emulator: http://10.0.2.2:%d/)' % a.port, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
