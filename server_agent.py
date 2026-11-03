"""Private tmux console and cached GPU/progress snapshots. No server HTTP port."""
import argparse
import json
import os
import sys
import time
from pathlib import Path

import gpu
from progress import load
from queue_store import QueueStore, atomic
from gpu_history import update as update_history
from settings import load as load_config
import process_monitor

ROOT = Path(__file__).resolve().parent
DEFAULT_DATA = ROOT / 'runtime-data'


def snapshot(store):
    state = store.read()
    cached_path = store.directory / 'snapshot.json'
    cached = json.loads(cached_path.read_text(encoding='utf-8')) if cached_path.exists() else {}
    history_path=store.directory/'gpu_history.json'
    history=json.loads(history_path.read_text(encoding='utf-8')) if history_path.exists() else {}
    inventory=process_monitor.collect(state,cached.get('gpus',[]),store.directory)
    ids = ([state['running']] if state['running'] else []) + state['order']
    rows = []
    for task_id in ids:
        task = state['tasks'][task_id]
        progress = load(dict(status_file=str(Path(task['root']) / task.get('status_file','run/STATUS.json')),
                             progress_key=task.get('progress_key','updates'), total=task.get('updates',0)))
        rows.append(dict(id=task_id, name=task['name'], state=task['state'], gpu=task.get('gpu'),gpu_uuid=task.get('gpu_uuid'),
                         epochs=task.get('epochs',0), updates=task.get('updates',0), progress=progress,processes=inventory['tasks'].get(task_id,[]),
                         order=state['order'].index(task_id) + 1 if task_id in state['order'] else None))
    return dict(revision=state['revision'], tasks=rows, gpus=cached.get('gpus', []),monitor_processes=inventory['observed'],process_error=inventory['error'],
                gpu_history=history.get('history',{}),history_updated=history.get('updated'),history_window_seconds=300,
                sampled=cached.get('sampled'), gpu_error=cached.get('gpu_error'),
                last_change=state['last_change'], controller=cached.get('controller', '准备中'))


def rpc(request, store):
    action = request.get('action')
    if action == 'state':
        return snapshot(store)
    if action == 'cancel':
        if str(request['id']).startswith('observed:'):raise ValueError('监控进程不可操作')
        store.cancel(request['id'], request.get('revision'))
    elif action == 'reorder':
        store.reorder(request['ids'], request.get('revision'))
    else:
        raise ValueError('仅支持 state / cancel / reorder')
    return snapshot(store)


def bridge(store):
    for line in sys.stdin:
        try:
            result = rpc(json.loads(line), store)
            response = dict(ok=True, result=result)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            response = dict(ok=False, error=str(exc))
        print(json.dumps(response, ensure_ascii=False, allow_nan=False), flush=True)


def run(store,config,execute=False):
    import fcntl
    import threading
    from worker import Controller
    handle = (store.directory / 'agent.lock').open('w')
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    atomic(store.directory / 'agent.json', dict(identity=gpu.identity(os.getpid()), time=time.time()))
    if execute:
        threading.Thread(target=Controller(store,config).run,daemon=True).start()
    history_path=store.directory/'gpu_history.json'
    history=json.loads(history_path.read_text()).get('history',{}) if history_path.exists() else {}
    while True:
        info = gpu.snapshot({}, telemetry=True)
        control = store.directory / 'controller.json'
        controller = json.loads(control.read_text()).get('phase', '仅监控') if control.exists() else '仅监控'
        # GPU totals are cached here; scoped read-only process metadata is added by snapshot().
        devices = [{k: g[k] for k in ('index', 'uuid', 'name', 'memory_used_mib', 'memory_total_mib', 'utilization')}
                   for g in info['gpus']]
        atomic(store.directory / 'snapshot.json', dict(gpus=devices, gpu_error=info['error'],
                                                      sampled=time.time(), controller=controller))
        now=time.time()
        history=update_history(history,dict(gpus=devices,sampled=now),now)
        atomic(history_path,dict(window_seconds=300,updated=now,history=history))
        value = snapshot(store)
        lines = ['GPU Queue Panel     ' + time.strftime('%F %T'),
                 '本地网页通过 SSH 读取；服务器无网页端口', '']
        for g in devices:
            mem = f"{g['memory_used_mib']/1024:.1f}/{g['memory_total_mib']/1024:.1f} GiB" if g['memory_used_mib'] is not None else 'N/A'
            lines.append(f"GPU {g['index']}    显存 {mem}    计算利用率 {g['utilization']}%")
        lines += ['', '状态：' + controller, '']
        for task in value['tasks']:
            p = task['progress']['percent']
            lines.append(f"{'运行' if task['state']=='running' else str(task['order'])+'. 等待'}  {task['name']}  GPU{task['gpu']}  {p if p is not None else '—'}%")
        if not value['tasks']:
            lines.append('当前队列为空')
        print('\033[2J\033[H' + '\n'.join(lines), end='', flush=True)
        time.sleep(config['refresh_seconds'])


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data')
    p.add_argument('--execute',action='store_true',help='明确启用已导入任务的执行；默认仅监控')
    p.add_argument('command', choices=['run', 'bridge'])
    args = p.parse_args()
    config=load_config()
    store = QueueStore(args.data or config['data_path'])
    if args.command=='run':run(store,config,args.execute)
    else:bridge(store)
