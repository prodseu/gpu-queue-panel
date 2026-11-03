"""GPU process inventory. Foreign processes are NVIDIA metadata only, never /proc."""
import json
import subprocess
from pathlib import Path,PurePosixPath
import gpu


def same_process(a,b):
    return bool(a and b and a['pid']==b['pid'] and all(a.get(k)==b.get(k) for k in ['start_ticks','boot']))


def describe(name,identity=None):
    base=PurePosixPath(name).name
    python=name if base.lower().startswith(('python','pypy')) else None
    if identity:
        argv=identity.get('args',[])
        if argv:
            executable=PurePosixPath(argv[0]).name.lower()
            if executable.startswith(('python','pypy')):
                python=argv[0]
                for i,arg in enumerate(argv[1:],1):
                    if arg=='-c':return 'Python命令',python
                    if arg=='-m' and i+1<len(argv):return argv[i+1],python
                    if arg.endswith('.py'):return PurePosixPath(arg).name,python
    return base,python


def collect(state,devices,directory):
    tasks={};observed=[]
    try:
        rows=gpu.query(['--query-compute-apps=gpu_uuid,pid,process_name,used_memory'])
        own=gpu.own_process_ids();identities={}
        def identity(pid,saved):
            if pid not in own:return None
            if pid not in identities:
                proc=gpu.identity(pid,expected=saved)
                identities[pid]=proc if proc and proc.get('state') not in ('Z','X') else None
            return identities[pid]
        trusted_path=Path(directory)/'monitored_own_processes.json'
        if trusted_path.is_file():
            for record in json.loads(trusted_path.read_text()):
                saved=record.get('identity')
                if saved:identity(saved['pid'],saved)
        launches={}
        for task in state['tasks'].values():
            if task['state']!='running':continue
            root=Path(task['root'])
            paths=[Path(directory)/'jobs'/task['id']/'launch.json',root/'TRAINING_LAUNCH.json',root/'LAUNCH.json']
            for path in paths:
                if not path.is_file():continue
                try:launch=json.loads(path.read_text(encoding='utf-8'))
                except (OSError,ValueError):continue
                saved=launch.get('identity')
                if not saved:continue  # An old PID alone is not proof that a process is ours.
                proc=identity(launch.get('pid'),saved)
                if not proc:continue
                if saved and not same_process(saved,proc):continue
                launches[task['id']]=proc;break
        by_uuid={g['uuid']:g for g in devices}
        seen=set()
        for row in rows:
            if len(row)<4:continue
            uuid,pid,raw_name,memory=row[:4];pid=int(pid)
            uuid=uuid.strip();raw_name=raw_name.strip()
            if uuid not in by_uuid or (uuid,pid) in seen:continue
            seen.add((uuid,pid));proc=identities.get(pid)
            try:used=float(memory)
            except ValueError:used=None
            name,python=describe(raw_name,proc)
            item=dict(id=f'observed:{uuid}:{pid}',pid=pid,name=name,python=python,
                memory_used_mib=used,gpu_uuid=uuid,gpu=by_uuid[uuid]['index'],
                owned=proc is not None,read_only=True,state='observed')
            linked=next((key for key,launch in launches.items() if same_process(launch,proc)),None)
            if linked:
                tasks.setdefault(linked,[]).append(item)
            else:observed.append(item)
        # Managed CPU phases can have no live GPU allocation. Only an already
        # validated own launch is inspected; never search other job directories.
        for key,proc in launches.items():
            if key in tasks:continue
            task=state['tasks'][key];name,python=describe(proc['args'][0],proc)
            tasks[key]=[dict(pid=proc['pid'],name=name,python=python,memory_used_mib=None,
                gpu=task.get('gpu'),gpu_uuid=task.get('gpu_uuid'),owned=True,read_only=True)]
        return dict(tasks=tasks,observed=observed,error=None)
    except (OSError,ValueError,TypeError,KeyError,subprocess.SubprocessError) as exc:
        return dict(tasks={},observed=[],error='进程信息暂不可用：'+str(exc))
