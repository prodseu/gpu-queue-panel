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
from queue_control import controls
from scheduling import policy, running_ids
from auth import Accounts,Actor,AuthenticationError,visible,allowed_task,require_rc

ROOT = Path(__file__).resolve().parent
DEFAULT_DATA = ROOT / 'runtime-data'


def snapshot(store,actor):
    state = store.read()
    scoped=dict(state,tasks={key:t for key,t in state['tasks'].items() if visible(t,actor)})
    scoped['order']=[key for key in state['order'] if key in scoped['tasks']]
    own_running=running_ids(scoped)
    scoped['running']=own_running[0] if own_running else None
    cached_path = store.directory / 'snapshot.json'
    cached = json.loads(cached_path.read_text(encoding='utf-8')) if cached_path.exists() else {}
    history_path=store.directory/'gpu_history.json'
    history=json.loads(history_path.read_text(encoding='utf-8')) if history_path.exists() else {}
    inventory=process_monitor.collect(scoped,cached.get('gpus',[]),store.directory,include_trusted=actor.admin)
    ids = running_ids(scoped) + scoped['order']
    rows = []
    for task_id in ids:
        task = state['tasks'][task_id]
        progress = load(dict(status_file=str(Path(task['root']) / task.get('status_file','run/STATUS.json')),
                             progress_key=task.get('progress_key','updates'), total=task.get('updates',0)))
        rows.append(dict(manual_start_requested=bool(task.get('manual_start_request')),owner_id=task.get('owner_id'),id=task_id, name=task['name'], state=task['state'], gpu=task.get('gpu'),gpu_uuid=task.get('gpu_uuid'),
                         epochs=task.get('epochs',0), updates=task.get('updates',0), progress=progress,processes=inventory['tasks'].get(task_id,[]),
                         order=scoped['order'].index(task_id) + 1 if task_id in scoped['order'] else None))
    status_path=store.directory/'scheduler_status.json'
    handoff_path=store.directory/'dispatcher_handoff.json'
    if handoff_path.exists():
        handoff=json.loads(handoff_path.read_text())
        if handoff.get('state')=='active' and gpu.alive(handoff.get('new_agent')):
            status_path=store.directory/'scheduler_status_handoff.json'
            phase_path=store.directory/'controller_handoff.json'
            if phase_path.exists():cached['controller']=json.loads(phase_path.read_text()).get('phase','')
    scheduler=json.loads(status_path.read_text()) if status_path.exists() else {}
    if not actor.admin:
        scheduler={k:v for k,v in scheduler.items() if k in ('time','window','halted')}
        scheduler.update(running=own_running,reason='全局调度由管理员管理；下方仅显示本人任务')
    return dict(user=actor.public(),queue_control=controls(state),scheduling=policy(state), scheduler=scheduler, revision=state['revision'], tasks=rows, gpus=cached.get('gpus', []),monitor_processes=inventory['observed'],process_error=inventory['error'],
                gpu_history=history.get('history',{}),history_updated=history.get('updated'),history_window_seconds=300,
                sampled=cached.get('sampled'), gpu_error=cached.get('gpu_error'),
                last_change=state['last_change'] if actor.admin else '个人队列视图', controller=cached.get('controller','准备中') if actor.admin else '仅本人任务可取消与排序；其他进程只读')


def rpc(request, store):
    require_rc(store)
    accounts=Accounts(store.directory)
    action=request.get('action')
    if action=='login':return accounts.login(request.get('username'),request.get('password'))
    mutating=action in ('logout','cancel','reorder','configure','submit','move_gpu','queue_control','start')
    actor,session=accounts.session(request.get('token'))
    if mutating:actor,session=accounts.session(request.get('token'),request.get('csrf',''))
    if action=='me':return dict(user=actor.public(),**session)
    if action=='logout':
        accounts.logout(request['token']);return dict(logged_out=True)
    if action=='state':return dict(snapshot(store,actor),csrf=session['csrf'])
    if action=='logs':
        allowed_task(store.read(),request.get('id'),actor)
        maximum=request.get('max_bytes',8192);cursor=request.get('cursor',0)
        if type(maximum)is not int or not 1<=maximum<=32768 or type(cursor)is not int or cursor<0:raise ValueError('日志范围不合法')
        path=store.directory/'jobs'/request['id']/'output.log'
        if not path.is_file():return dict(id=request['id'],text='',next_cursor=0)
        with path.open('rb') as f:
            f.seek(cursor);body=f.read(maximum)
        return dict(id=request['id'],text=body.decode('utf-8',errors='replace'),next_cursor=cursor+len(body))
    if action in ('queue_control','start'):
        if type(request.get('revision')) is not int:raise ValueError('请先刷新队列')
        from queue_control import configure,request_start
        if action=='start':request_start(store,request.get('id'),request['revision'],actor)
        else:configure(store,request.get('mode'),request.get('paused'),request['revision'],actor)
        return dict(snapshot(store,actor),csrf=session['csrf'])
    if action=='move_gpu':
        from gpu_transfer import move
        move(store,request.get('id'),request.get('gpu_uuid'),request.get('revision'),actor)
        return dict(snapshot(store,actor),csrf=session['csrf'])
    if action=='submit':
        from queue_submit import enroll
        return enroll(request['task'],store,actor,request.get('auto_id',False))
    if action not in ('cancel','reorder','configure'):raise ValueError('不支持的操作')
    if type(request.get('revision'))is not int:raise ValueError('必须提供最新revision')
    if action=='cancel':store.cancel(request['id'],request['revision'],actor=actor)
    elif action=='reorder':store.reorder(request['ids'],request['revision'],actor=actor)
    else:store.configure(request['settings'],request['revision'],actor=actor)
    return dict(snapshot(store,actor),csrf=session['csrf'])


def bridge(store):
    for line in sys.stdin:
        try:
            result = rpc(json.loads(line), store)
            response = dict(ok=True, result=result)
        except AuthenticationError as exc:
            response=dict(ok=False,error=str(exc),status=401)
        except PermissionError as exc:
            response=dict(ok=False,error=str(exc),status=403)
        except (OSError,ValueError,KeyError,TypeError) as exc:
            response=dict(ok=False,error=str(exc),status=409)
        print(json.dumps(response, ensure_ascii=False, allow_nan=False), flush=True)


def run(store,config,execute=False):
    require_rc(store)
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
        value = snapshot(store,Actor("system","admin"))
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
