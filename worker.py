"""Sequential foreground commands; cancellation never becomes a hidden dependency."""
import json
import os
import subprocess
import time
import traceback
from pathlib import Path
import gpu
from queue_store import atomic


class Controller:
    def __init__(self,store,config):
        self.store=store
        self.config=config

    def phase(self,phase,**extra):
        atomic(self.store.directory/'controller.json',dict(phase=phase,pid=os.getpid(),time=time.time(),**extra))

    def available(self,task):
        uuid=task.get('gpu_uuid')
        if not uuid:return True
        info=gpu.snapshot({'hold_rules':self.config.get('hold_rules',[])})
        if not info['ok']:
            self.phase('等待GPU信息恢复',error=info['error']);return False
        devices=[g for g in info['gpus'] if g['uuid']==uuid]
        return bool(devices and not any(not p['hold'] for p in devices[0]['processes']))

    def execute(self,task):
        directory=self.store.directory/'jobs'/task['id']
        directory.mkdir(parents=True,exist_ok=False)
        root=Path(task['root'])
        env=dict(os.environ,**task.get('env',{}))
        env.update(CUDA_VISIBLE_DEVICES=task.get('gpu_uuid') or '',PYTHONUNBUFFERED='1')
        with (directory/'output.log').open('xb') as log:
            child=subprocess.Popen(task['argv'],cwd=root,env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT)
            atomic(directory/'launch.json',dict(pid=child.pid,time=time.time(),argv=task['argv'],identity=gpu.identity(child.pid)))
            self.phase('执行中：'+task['name'],task=task['id'],child_pid=child.pid)
            code=child.wait()
        atomic(directory/'receipt.json',dict(returncode=code,finished=time.time()))
        if code!=0:raise RuntimeError(f"任务退出码：{code}")
        if task.get('completion_file'):
            completion=json.loads((root/task['completion_file']).read_text(encoding='utf-8'))
            if completion.get('complete') is not True:
                raise ValueError('完成文件的complete字段不是true')

    def run(self):
        try:
            if self.store.read()['running']:
                raise RuntimeError('存在先前运行项，请先检查真实进程；不会自动重跑')
            while True:
                value=self.store.read()
                if not value['order']:
                    self.phase('队列为空，持续待命；新任务加入后自动接续')
                    time.sleep(self.config['refresh_seconds']);continue
                candidate=value['tasks'][value['order'][0]]
                if not self.available(candidate):
                    self.phase('等待指定GPU空闲');time.sleep(self.config['refresh_seconds']);continue
                task=self.store.claim_if(candidate['id'],value['revision'])
                if task is None:continue
                try:self.execute(task)
                except Exception:
                    error=traceback.format_exc()
                    self.store.finish(task['id'],False,error)
                    if self.config.get('stop_on_failure',True):
                        self.phase('任务失败，后续等待项保留；未自动重试',error=error);return
                else:self.store.finish(task['id'],True)
        except Exception:self.phase('协调异常，未自动重试',error=traceback.format_exc())
