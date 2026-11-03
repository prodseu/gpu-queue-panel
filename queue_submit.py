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
    for name in ('id','name','cwd','data','status-file','completion-file'):
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


def submit(task, directory):
    store=QueueStore(directory)
    if not store.path.is_file():raise ValueError('队列未初始化；先导入空队列或已有计划并启动服务器执行器')
    # Current/private tool needs its command adapter loaded before enrollment.
    if (HERE/'existing_worker.py').exists():
        runtime=store.directory/'scheduler_runtime.json'
        if not runtime.exists() or json.loads(runtime.read_text()).get('command_submission_version',0)<1:
            raise ValueError('当前执行器尚未加载命令托管适配器；请先完成安全更新，不登记无法执行的任务')
    def append(value):
        existing=value['tasks'].get(task['id'])
        if existing:
            if existing.get('submission_fingerprint')!=task['submission_fingerprint']:
                raise ValueError('同一任务ID已登记其他命令，请使用新ID')
            return dict(id=existing['id'],state=existing['state'],duplicate=True,
                        note='已登记，未重复入队；有意重跑请显式使用新的--id')
        value['tasks'][task['id']]=dict(task,state='waiting',registered=time.time())
        value['order'].append(task['id'])
        value['last_change']='命令行登记授权任务：'+task['name']
        return dict(id=task['id'],state='waiting',duplicate=False,
                    note='已加入队尾；常驻执行器按窗口和资源保护自动领取')
    return store.change(append)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    options(parser)
    parser.add_argument('command',nargs=argparse.REMAINDER)
    args=parser.parse_args(argv)
    result=submit(build_task(args,args.command),data_directory(args.data))
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
    result=submit(build_task(args,command),data_directory(args.data))
    print(json.dumps(result,ensure_ascii=False),flush=True)
    raise SystemExit(0)


if __name__=='__main__':
    try:main()
    except (OSError,ValueError,KeyError,TypeError) as exc:
        print(str(exc),file=sys.stderr);sys.exit(1)
