"""Loopback browser UI. The server is reached exclusively through SSH stdio."""
import argparse
import hashlib
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path, PurePosixPath
import queue
import secrets
import shlex
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parent
COOKIE = 'gqd_browser'


class RemoteError(ValueError):
    pass


class SSH:
    def __init__(self, host, directory, python='python3'):
        self.host = host
        self.command = shlex.join([python, str(PurePosixPath(directory)/'web_bridge.py')])
        self.fingerprint = hashlib.sha256((host+'\n'+self.command).encode()).hexdigest()[:16]
        self.process = None
        self.responses = None
        self.lock = threading.Lock()

    def close(self):
        if self.process:
            self.process.terminate()
            self.process = None

    def start(self):
        flags = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
        self.process = subprocess.Popen(['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=12',
            '-o', 'ServerAliveInterval=10', '-o', 'ServerAliveCountMax=2', self.host, self.command],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding='utf-8', bufsize=1, **flags)
        self.responses = queue.Queue()
        process, responses = self.process, self.responses
        def read():
            try:
                for line in process.stdout:
                    responses.put(line)
            finally:
                responses.put(None)
        threading.Thread(target=read, daemon=True).start()

    def request(self, request):
        with self.lock:
            if self.process is None or self.process.poll() is not None:
                self.start()
            try:
                self.process.stdin.write(json.dumps(request, ensure_ascii=False)+'\n')
                self.process.stdin.flush()
                line = self.responses.get(timeout=40)
                if line is None:
                    raise OSError('SSH连接已关闭')
                response = json.loads(line)
                if not response['ok']:
                    raise RemoteError(response['error'])
                return response['result']
            except (OSError, queue.Empty, json.JSONDecodeError) as exc:
                self.close()
                raise OSError('SSH连接暂不可用，请稍后刷新。未自动重发操作。') from exc


def create_server(client, port):
    csrf = secrets.token_urlsafe(32)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, value, mime='application/json; charset=utf-8', cookie=None):
            body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'")
            if cookie:
                self.send_header('Set-Cookie', cookie)
            self.end_headers()
            self.wfile.write(body)

        def origin(self):
            hosts = (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}')
            return self.headers.get('Host') in hosts and self.headers.get('Origin') in (None, *(f'http://{h}' for h in hosts))

        def do_GET(self):
            if not self.origin():
                return self.send(403, dict(error='请求来源不匹配'))
            if self.path == '/health':
                return self.send(200, dict(application='gpu-tmux-web', version='0.7.1', target=getattr(client, 'fingerprint', '')))
            if self.path == '/api/session':
                return self.send(200, dict(csrf=csrf), cookie=COOKIE+'_'+str(self.server.server_port)+'='+csrf+'; Path=/; HttpOnly; SameSite=Strict')
            files = {'/': ('index.html', 'text/html; charset=utf-8'),
                     '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                     '/style.css': ('style.css', 'text/css; charset=utf-8')}
            if self.path not in files:
                return self.send(404, dict(error='未知地址'))
            name, mime = files[self.path]
            self.send(200, (ROOT/'web'/name).read_bytes(), mime)

        def do_POST(self):
            if not self.origin():
                return self.send(403, dict(error='请求来源不匹配'))
            try:
                cookies = SimpleCookie()
                cookies.load(self.headers.get('Cookie', ''))
                name = COOKIE+'_'+str(self.server.server_port)
                token = cookies[name].value if name in cookies else ''
                if not secrets.compare_digest(token, csrf) or not secrets.compare_digest(self.headers.get('X-CSRF-Token', ''), csrf):
                    return self.send(403, dict(error='请刷新网页后重试'))
                if self.path != '/api':
                    return self.send(404, dict(error='未知地址'))
                if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    raise ValueError('请使用JSON请求')
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 131072:
                    raise ValueError('请求大小不合法')
                request = json.loads(self.rfile.read(size))
                self.send(200, client.request(request))
            except (ValueError, TypeError, KeyError) as exc:
                self.send(409, dict(error=str(exc)))
            except OSError as exc:
                self.send(503, dict(error=str(exc)))
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ssh', required=True)
    parser.add_argument('--remote-dir', required=True)
    parser.add_argument('--remote-python', default='python3')
    parser.add_argument('--port', type=int, default=8790)
    parser.add_argument('--serve', action='store_true', help='Run viewer in foreground')
    args = parser.parse_args()
    if args.serve:
        client = SSH(args.ssh, args.remote_dir, args.remote_python)
        server = create_server(client, args.port)
        try:
            server.serve_forever()
        finally:
            server.server_close()
            client.close()
        return
    base = f'http://127.0.0.1:{args.port}'
    expected = SSH(args.ssh, args.remote_dir, args.remote_python).fingerprint
    def ready():
        try:
            with urllib.request.urlopen(base+'/health', timeout=2) as response:
                value = json.load(response)
                return value.get('application') == 'gpu-tmux-web' and value.get('target') == expected
        except (OSError, ValueError):
            return False
    if not ready():
        try:
            connection = socket.create_connection(('127.0.0.1', args.port), timeout=1)
        except OSError:
            flags = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session': True}
            subprocess.Popen([sys.executable, str(Path(__file__).resolve()), *sys.argv[1:], '--serve'],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **flags)
        else:
            connection.close()
            raise ValueError('端口已有其他服务，请选择 --port')
        for _ in range(30):
            if ready():
                break
            time.sleep(.2)
        else:
            raise OSError('网页启动失败，使用 --serve 查看错误')
    webbrowser.open(base)
    print(base)


if __name__ == '__main__':
    main()
