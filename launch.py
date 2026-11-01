"""Start or reuse the local viewer, then open its authenticated browser page."""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path
from settings import load

ROOT=Path(__file__).resolve().parent


def main():
    config=load()
    port=int(config['local_port'])
    directory=ROOT/'client-data';directory.mkdir(exist_ok=True)
    token_file=directory/'local.token'
    base=f'http://127.0.0.1:{port}'
    def connected():
        if not token_file.exists():return False
        token=token_file.read_text().strip()
        req=urllib.request.Request(base+'/api/queue',data=b'{"action":"state"}',
            headers={'Content-Type':'application/json','Authorization':'Bearer '+token})
        try:
            with urllib.request.urlopen(req,timeout=4) as response:json.load(response)
            return True
        except OSError:return False
    if not connected():
        try:connection=socket.create_connection(('127.0.0.1',port),timeout=1)
        except OSError:
            flags={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {'start_new_session':True}
            python=shutil.which(config.get('local_python','python')) or sys.executable
            with (directory/'viewer.log').open('ab') as log:
                child=subprocess.Popen([python,'-u',str(ROOT/'local_view.py')],cwd=ROOT,
                    stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,**flags)
            (directory/'viewer.pid').write_text(str(child.pid))
        else:
            connection.close()
            raise RuntimeError('本机端口已被占用或现有界面暂未连接SSH，请检查client-data/viewer.log')
        for _ in range(20):
            if connected():break
            time.sleep(.5)
        else:raise RuntimeError('界面未连接，请检查SSH配置与client-data/viewer.log')
    webbrowser.open(base+'/#token='+token_file.read_text().strip())


if __name__=='__main__':
    try:main()
    except (OSError,ValueError,RuntimeError) as exc:
        print(str(exc),file=sys.stderr);sys.exit(1)
