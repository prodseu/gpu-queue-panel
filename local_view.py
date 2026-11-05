"""Local-only browser service. All server reads/queue edits travel through SSH."""
import json
import os
import queue
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from auth import AuthenticationError
from pathlib import Path
from settings import load,remote_bridge

HERE=Path(__file__).resolve().parent
COOKIE='gqp_rc_session'

class RemoteError(Exception):
    def __init__(self,status,message):
        self.status=status
        super().__init__(message)


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
                if not response['ok']:raise RemoteError(response.get('status',409),response['error'])
                return response['result']
            except (OSError,ConnectionError,queue.Empty,json.JSONDecodeError) as exc:
                self.process.terminate()
                self.process=None
                raise OSError('SSH连接暂不可用，正在重新连接') from exc


def create_server(client,port=8789):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def send(self,status,value,mime='application/json; charset=utf-8',cookie=None):
            body=value if isinstance(value,bytes) else json.dumps(value,ensure_ascii=False,allow_nan=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type',mime)
            self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'")
            if cookie:self.send_header('Set-Cookie',cookie)
            self.end_headers();self.wfile.write(body)
        def origin(self):
            actual=self.server.server_port
            hosts=(f'127.0.0.1:{actual}',f'localhost:{actual}')
            return self.headers.get('Host') in hosts and self.headers.get('Origin') in (None,*(f'http://{h}' for h in hosts))
        def token(self):
            try:
                cookie=SimpleCookie();cookie.load(self.headers.get('Cookie',''))
                return cookie[COOKIE].value if COOKIE in cookie else ''
            except Exception:return ''
        def failed(self,exc):
            if isinstance(exc,RemoteError):status=exc.status
            elif isinstance(exc,AuthenticationError):status=401
            elif isinstance(exc,PermissionError):status=403
            elif isinstance(exc,(ValueError,KeyError,TypeError)):status=409
            else:status=503
            self.send(status,{'error':str(exc)},cookie=(COOKIE+'=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0') if status==401 else None)
        def do_GET(self):
            if not self.origin():return self.send(403,{'error':'仅供本机访问'})
            if self.path=='/health':return self.send(200,{'application':'gpu-queue-panel-multiuser','version':'0.5.0'})
            if self.path=='/api/me':
                try:self.send(200,client.request(dict(action='me',token=self.token())))
                except Exception as exc:self.failed(exc)
                return
            files={'/':('index.html','text/html; charset=utf-8'),'/app.js':('app.js','text/javascript; charset=utf-8'),'/style.css':('style.css','text/css; charset=utf-8')}
            if self.path not in files:return self.send(404,{'error':'未知地址'})
            file,mime=files[self.path];self.send(200,(HERE/'web'/file).read_bytes(),mime)
        def do_POST(self):
            if not self.origin():return self.send(403,{'error':'请求来源不匹配'})
            try:
                if self.headers.get('Content-Type','').split(';')[0]!='application/json':raise ValueError('请使用JSON请求')
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<65536:raise ValueError('请求大小不合法')
                request=json.loads(self.rfile.read(size))
                if not isinstance(request,dict):raise ValueError('请求须为JSON对象')
                if self.path=='/api/login':
                    result=client.request(dict(action='login',username=request.get('username'),password=request.get('password')))
                    token=result.pop('token')
                    self.send(200,result,cookie=COOKIE+'='+token+'; Path=/; HttpOnly; SameSite=Strict; Max-Age=28800')
                elif self.path=='/api/logout':
                    result=client.request(dict(action='logout',token=self.token(),csrf=self.headers.get('X-CSRF-Token','')))
                    self.send(200,result,cookie=COOKIE+'=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0')
                elif self.path=='/api/queue':
                    if request.get('action') not in ('state','cancel','reorder','configure','logs','move_gpu','queue_control','start'):raise ValueError('网页仅支持查询、日志、等待项操作及管理员设置')
                    request.update(token=self.token(),csrf=self.headers.get('X-CSRF-Token',''))
                    self.send(200,client.request(request))
                else:self.send(404,{'error':'未知地址'})
            except Exception as exc:self.failed(exc)
    server=ThreadingHTTPServer(('127.0.0.1',port),Handler)
    server.daemon_threads=True
    return server


def serve():
    config=load();client=SSH(config)
    server=create_server(client,int(config['local_port']))
    print(f'多用户队列界面 http://127.0.0.1:{server.server_port}/ （用户名/密码登录）',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:
        server.server_close()
        if client.process:client.process.terminate()


if __name__=='__main__':serve()
