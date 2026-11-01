"""Local-only browser service. All server reads/queue edits travel through SSH."""
import json
import os
import queue
import secrets
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from settings import load,remote_bridge

HERE=Path(__file__).resolve().parent


class SSH:
    def __init__(self,config=None):
        self.config=config or load()
        self.target=self.config['ssh_host']
        self.lock=threading.Lock()
        self.process=None
        self.responses=None

    def start(self):
        flags={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}
        self.process=subprocess.Popen(['ssh','-T','-o','BatchMode=yes','-o','ServerAliveInterval=10',
            '-o','ServerAliveCountMax=2',self.target,remote_bridge(self.config)],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
            text=True,encoding='utf-8',bufsize=1,**flags)
        self.responses=queue.Queue()
        proc,responses=self.process,self.responses
        def reader():
            try:
                for line in proc.stdout:responses.put(line)
            finally:responses.put(None)
        threading.Thread(target=reader,daemon=True).start()

    def request(self,request):
        with self.lock:
            if not self.process or self.process.poll() is not None:self.start()
            try:
                self.process.stdin.write(json.dumps(request,ensure_ascii=False)+'\n')
                self.process.stdin.flush()
                line=self.responses.get(timeout=15)
                if line is None:raise ConnectionError('SSH连接已关闭')
                response=json.loads(line)
                if not response['ok']:raise ValueError(response['error'])
                return response['result']
            except (OSError,ConnectionError,queue.Empty,json.JSONDecodeError) as exc:
                self.process.terminate()
                self.process=None
                raise OSError('SSH连接暂不可用，正在重新连接') from exc


def serve():
    config=load()
    port=int(config['local_port'])
    client=SSH(config)
    data=HERE/'client-data'
    data.mkdir(exist_ok=True)
    token_file=data/'local.token'
    if not token_file.exists():token_file.write_text(secrets.token_urlsafe(32),encoding='utf-8')
    token=token_file.read_text().strip()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def send(self,status,value,mime='application/json; charset=utf-8'):
            body=value if isinstance(value,bytes) else json.dumps(value,ensure_ascii=False,allow_nan=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type',mime)
            self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'")
            self.end_headers();self.wfile.write(body)
        def origin(self):
            hosts=(f'127.0.0.1:{port}',f'localhost:{port}')
            return self.headers.get('Host') in hosts and self.headers.get('Origin') in (None,*(f'http://{h}' for h in hosts))
        def do_GET(self):
            files={'/':('index.html','text/html; charset=utf-8'),'/app.js':('app.js','text/javascript; charset=utf-8'),'/style.css':('style.css','text/css; charset=utf-8')}
            if not self.origin():return self.send(403,{'error':'仅供本机访问'})
            if self.path not in files:return self.send(404,{'error':'未知地址'})
            file,mime=files[self.path]
            self.send(200,(HERE/'web'/file).read_bytes(),mime)
        def do_POST(self):
            import hmac
            if not self.origin() or not hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+token):
                return self.send(403,{'error':'本机访问令牌不匹配，请用一键打开脚本重新连接'})
            try:
                size=int(self.headers.get('Content-Length','0'))
                if self.path!='/api/queue' or not 0<size<65536:raise ValueError('不支持的请求')
                request=json.loads(self.rfile.read(size))
                if request.get('action') not in ('state','cancel','reorder'):raise ValueError('仅支持查看、取消等待项和排序')
                self.send(200,client.request(request))
            except ValueError as exc:self.send(409,{'error':str(exc)})
            except (OSError,KeyError,TypeError) as exc:self.send(503,{'error':str(exc)})
    http=ThreadingHTTPServer(('127.0.0.1',port),Handler)
    http.daemon_threads=True
    print(f'本机界面 http://127.0.0.1:{port}/#token={token}',flush=True)
    try:http.serve_forever()
    except KeyboardInterrupt:pass
    finally:
        http.server_close()
        if client.process:client.process.terminate()


if __name__=='__main__':serve()
