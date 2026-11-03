"""Execute one registered foreground argv with isolated log/identity/exit receipt."""
import json
import os
import subprocess
import time
from pathlib import Path

import gpu
from queue_store import atomic


def execute(task, store, phase):
    directory=store.directory/'jobs'/task['id']
    directory.mkdir(parents=True,exist_ok=False)
    root=Path(task['root'])
    env=dict(os.environ,**task.get('env',{}))
    env.update(CUDA_VISIBLE_DEVICES=task.get('gpu_uuid') or '',PYTHONUNBUFFERED='1',
               GPU_QUEUE_JOB_ID=task['id'],GPU_QUEUE_DATA_DIR=str(store.directory))
    with (directory/'output.log').open('xb') as log:
        child=subprocess.Popen(task['argv'],cwd=root,env=env,stdin=subprocess.DEVNULL,
                               stdout=log,stderr=subprocess.STDOUT)
        atomic(directory/'launch.json',dict(pid=child.pid,time=time.time(),argv=task['argv'],identity=gpu.identity(child.pid)))
        phase('执行中：'+task['name'],task=task['id'],child_pid=child.pid)
        code=child.wait()
    atomic(directory/'receipt.json',dict(returncode=code,finished=time.time()))
    if code!=0:raise RuntimeError(f'任务退出码：{code}')
    if task.get('completion_file'):
        completion=json.loads((root/task['completion_file']).read_text(encoding='utf-8'))
        if completion.get('complete') is not True:
            raise ValueError('完成文件的complete字段不是true')
