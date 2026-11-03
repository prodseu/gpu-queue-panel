"""Bounded concurrent foreground commands; waiting jobs remain independent."""
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
        from command_runner import execute
        return execute(task,self.store,self.phase)

    def run(self):
        from scheduling import Admission, Dispatcher
        def make_worker(task):
            worker=Controller(self.store,self.config)
            worker.phase=lambda phase,**extra: atomic(self.store.directory/'jobs'/task['id']/'controller.json',
                dict(phase=phase,pid=os.getpid(),time=time.time(),**extra))
            return worker
        try:
            Dispatcher(self.store,make_worker,self.phase,
                Admission(self.store.directory,self.config.get('hold_rules',[])),
                interval=self.config['refresh_seconds'],
                stop_on_failure=lambda task:self.config.get('stop_on_failure',False)).run()
        except Exception:
            self.phase('协调异常，未自动重试；运行项不发信号',error=traceback.format_exc())
