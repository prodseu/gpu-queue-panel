"""Queue a foreground command, or opt train.py in with --queue before imports.

Runs on the server. Submission never runs the command or starts another worker.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

from queue_store import QueueStore
from auth import AuthenticationError

HERE = Path(__file__).resolve().parent
ENV_KEYS = ('PATH', 'PYTHONPATH', 'LD_LIBRARY_PATH', 'CONDA_PREFIX', 'VIRTUAL_ENV')


def data_directory(override=None):
    if override:
        return Path(override).expanduser().resolve()
    if (HERE/'settings.py').exists():
        from settings import load
        return Path(load()['data_path'])
    return HERE/'personal-data'


def options(parser, prefix=''):
    def flag(name):return '--'+prefix+name
    inherited=os.environ.get('CUDA_VISIBLE_DEVICES','').strip()
    inherited=inherited if inherited and ',' not in inherited and inherited!='-1' else None
    device=parser.add_mutually_exclusive_group(required=inherited is None)
    device.add_argument(flag('gpu'),dest='gpu',default=inherited,help='GPU编号或完整UUID；可继承单卡CUDA_VISIBLE_DEVICES')
    device.add_argument(flag('cpu'),dest='cpu',action='store_true')
    for name in ('id','name','cwd','data','status-file','completion-file','session-file'):
        parser.add_argument(flag(name),dest=name.replace('-','_'))
    parser.add_argument(flag('peak-memory-mib'),dest='peak_memory_mib',type=int)
    parser.add_argument(flag('progress-key'),dest='progress_key',default='updates')
    parser.add_argument(flag('updates'),dest='updates',type=int,default=0)
    parser.add_argument(flag('epochs'),dest='epochs',type=int,default=0)
    parser.add_argument(flag('env'),dest='env',action='append',default=[],metavar='KEY=VALUE')


def resolve_gpu(selector):
    import gpu
    devices=gpu.query(['--query-gpu=index,uuid,name'])
    matches=[row for row in devices if len(row)>=3 and selector in (row[0].strip(),row[1].strip())]
    if len(matches)!=1:
        raise ValueError('GPU编号/UUID未匹配到唯一显卡')
    return int(matches[0][0]),matches[0][1].strip()


def build_task(args, command):
    if os.name!='posix':
        raise ValueError('请在Linux服务器提交；本机通过SSH运行该命令')
    command=list(command)
    if command[:1]==['--']:command=command[1:]
    if not command or not all(isinstance(arg,str) and arg for arg in command):
        raise ValueError('请在 -- 后提供完整前台命令，例如 python train.py --epochs 20')
    root=Path(args.cwd or os.getcwd()).expanduser().resolve()
    if not root.is_dir():raise ValueError('工作目录不存在')
    if '/' in command[0]:
        executable=shutil.which(str(root/command[0]))
    else:
        executable=shutil.which(command[0])
    if not executable:raise ValueError('当前环境找不到命令：'+command[0])
    # Keep the virtualenv executable path: resolving its symlink changes sys.prefix.
    command[0]=os.path.abspath(executable)
    index,uuid=(None,'') if args.cpu else resolve_gpu(args.gpu)
    if args.updates<0 or args.epochs<0:raise ValueError('updates/epochs不能小于0')
    if args.peak_memory_mib is not None and args.peak_memory_mib!=-1 and args.peak_memory_mib<1024:
        raise ValueError('峰值显存须为 -1 或至少1024 MiB')
    env={key:os.environ[key] for key in ENV_KEYS if key in os.environ}
    for item in args.env:
        key,separator,value=item.partition('=')
        if not separator or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',key):
            raise ValueError('--env需要KEY=VALUE')
        if key in ('CUDA_VISIBLE_DEVICES','GPU_QUEUE_JOB_ID','GPU_QUEUE_DATA_DIR'):
            raise ValueError('设备和托管身份由队列设置，不能用--env覆盖')
        env[key]=value
    task=dict(kind='command',root=str(root),argv=command,gpu=index,gpu_uuid=uuid,
              env=env,updates=args.updates,epochs=args.epochs,progress_key=args.progress_key)
    for key in ('status_file','completion_file'):
        value=getattr(args,key)
        if value:task[key]=str((root/value).resolve())
    if args.peak_memory_mib is not None:task['peak_memory_mib']=args.peak_memory_mib
    fingerprint=hashlib.sha256(json.dumps(task,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    stem=Path(command[1]).stem if len(command)>1 and command[1].endswith('.py') else Path(command[0]).stem
    stem=re.sub('[^A-Za-z0-9_-]','-',stem).strip('-_')[:40] or 'command'
    task_id=args.id or stem+'-'+fingerprint[:16]
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}',task_id):
        raise ValueError('任务ID须为1–80位字母、数字、下划线或连字符，首位为字母/数字')
    task.update(id=task_id,name=args.name or stem,submission_fingerprint=fingerprint)
    return task


def enroll(task,store,actor,auto_id=False):
    from auth import require_rc
    require_rc(store)
    if not isinstance(task,dict):raise ValueError('task须为对象')
    fields={'id','name','root','argv','gpu','gpu_uuid','env','updates','epochs','progress_key','status_file','completion_file','peak_memory_mib','submission_fingerprint','kind'}
    if set(task)-fields:raise PermissionError('任务归属与状态只能由后端确定')
    if not isinstance(task.get('id'),str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}',task['id']):raise ValueError('任务ID格式不合法')
    argv=task.get('argv')
    if not isinstance(argv,list) or not argv or not all(isinstance(x,str) and x for x in argv):raise ValueError('argv必须是非空字符串数组')
    if not isinstance(task.get('root'),str) or not Path(task['root']).is_absolute() or not Path(task['root']).is_dir():raise ValueError('root必须是存在的绝对目录')
    env=task.get('env',{})
    if not isinstance(env,dict) or not all(isinstance(k,str) and isinstance(v,str) for k,v in env.items()):raise ValueError('env格式不合法')
    if not all(re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',k) for k in env):raise ValueError('env变量名不合法')
    if set(env)&{'CUDA_VISIBLE_DEVICES','GPU_QUEUE_JOB_ID','GPU_QUEUE_DATA_DIR'}:raise ValueError('设备和托管身份由队列设置')
    for key in ('epochs','updates'):
        if type(task.get(key,0)) is not int or task.get(key,0)<0:raise ValueError(key+'须为非负整数')
    for key in ('status_file','completion_file','progress_key'):
        if key in task and (not isinstance(task[key],str) or not task[key]):raise ValueError(key+'须为非空字符串')
    if 'name' in task and (not isinstance(task['name'],str) or not task['name']):raise ValueError('name须为非空字符串')
    if task.get('gpu') is not None and (type(task['gpu']) is not int or task['gpu']<0):raise ValueError('gpu须为非负整数或null')
    if not isinstance(task.get('gpu_uuid',''),str):raise ValueError('gpu_uuid须为字符串')
    if 'peak_memory_mib' in task:
        from scheduling import memory_needed,DEFAULTS
        memory_needed(task,DEFAULTS)
    task=dict(task,kind='command',name=task.get('name',task['id']))
    task.pop('submission_fingerprint',None)
    fingerprint=hashlib.sha256(json.dumps({k:v for k,v in task.items() if k not in ('id','name')},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    if auto_id:task['id']=actor.username+'-'+task['id'][:46]
    task.update(owner_id=actor.username,submission_fingerprint=fingerprint)
    def append(value):
        existing=value['tasks'].get(task['id'])
        if existing:
            if existing.get('owner_id')!=actor.username or existing.get('submission_fingerprint')!=fingerprint:
                raise PermissionError('任务ID不可用或与已有内容冲突')
            return dict(id=existing['id'],owner_id=actor.username,state=existing['state'],duplicate=True)
        from gpu_transfer import wrap_command
        wrap_command(task,store.directory)
        value['tasks'][task['id']]=dict(task,state='waiting',registered=time.time())
        value['order'].append(task['id'])
        value['last_change']='登记授权任务：'+task['name']
        return dict(id=task['id'],owner_id=actor.username,state='waiting',duplicate=False)
    return store.change(append)


def submit(task,directory,session_file=None,auto_id=False):
    from auth import read_session
    from server_agent import rpc
    session=read_session(session_file)
    return rpc(dict(action='submit',task=task,auto_id=auto_id,token=session['token'],csrf=session['csrf']),QueueStore(directory))


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    options(parser)
    parser.add_argument('command',nargs=argparse.REMAINDER)
    args=parser.parse_args(argv)
    result=submit(build_task(args,args.command),data_directory(args.data),args.session_file,auto_id=args.id is None)
    print(json.dumps(result,ensure_ascii=False),flush=True)
    return result


def enqueue_if_requested(argv=None):
    """Call at train.py's top, before heavy imports, argument parsing or CUDA.

    No --queue: leave argv untouched. With --queue: submit a clean invocation and
    exit this launcher. The worker executes train.py without the submission flags.
    """
    original=list(sys.argv[1:] if argv is None else argv)
    if '--queue' not in original:return False
    if os.environ.get('GPU_QUEUE_JOB_ID'):
        raise ValueError('已托管的子进程不能再次--queue提交')
    parser=argparse.ArgumentParser(add_help=False,allow_abbrev=False)
    parser.add_argument('--queue',action='store_true')
    options(parser,prefix='queue-')
    args,training_args=parser.parse_known_args(original)
    command=[sys.executable,str(Path(sys.argv[0]).absolute()),*training_args]
    result=submit(build_task(args,command),data_directory(args.data),args.session_file,auto_id=args.id is None)
    print(json.dumps(result,ensure_ascii=False),flush=True)
    raise SystemExit(0)


if __name__=='__main__':
    try:main()
    except (OSError,ValueError,KeyError,TypeError,AuthenticationError) as exc:
        print(str(exc),file=sys.stderr);sys.exit(1)
