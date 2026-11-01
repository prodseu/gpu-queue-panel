"""One CLI for the queue: initialize an explicit plan, inspect, cancel or reorder."""
import argparse
import json
import os
import re
import sys
import shlex
import subprocess
from pathlib import Path
from settings import load


def initialize(plan_path,config):
    from queue_store import QueueStore
    plan=json.loads(Path(plan_path).read_text(encoding='utf-8-sig'))
    tasks=plan['tasks']
    for task in tasks:
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}',task['id']):
            raise ValueError('任务ID仅允许字母、数字、下划线与连字符')
        if 'XXX' in json.dumps(task):raise ValueError('请先替换任务示例中的XXX')
        if not Path(task['root']).is_dir():raise ValueError('任务工作目录不存在')
        if not isinstance(task['argv'],list) or not task['argv'] or not all(isinstance(x,str) and x for x in task['argv']):
            raise ValueError('argv应为非空字符串数组')
        task.setdefault('gpu',None)
        task.setdefault('gpu_uuid','')
        task.setdefault('epochs',0)
        task.setdefault('updates',0)
    return QueueStore(config['data_path']).initialize(tasks,plan.get('order',[t['id'] for t in tasks]))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    sub=p.add_subparsers(dest='command',required=True)
    a=sub.add_parser('init');a.add_argument('plan')
    sub.add_parser('status')
    sub.add_parser('console')
    a=sub.add_parser('cancel');a.add_argument('id')
    a=sub.add_parser('reorder');a.add_argument('ids',nargs='+')
    args=p.parse_args();config=load()
    if args.command=='console':
        command=shlex.join(['tmux','attach-session','-t',config['tmux_session']])
        raise SystemExit(subprocess.call(['ssh','-t',config['ssh_host'],command]))
    if args.command=='init':
        if os.name!='posix':raise ValueError('请在Linux服务器导入已授权的任务计划')
        result=initialize(args.plan,config)
    else:
        request={'action':'state' if args.command=='status' else args.command}
        if args.command=='cancel':request['id']=args.id
        elif args.command=='reorder':request['ids']=args.ids
        if os.name=='posix':
            from server_agent import rpc
            from queue_store import QueueStore
            result=rpc(request,QueueStore(config['data_path']))
        else:
            from local_view import SSH
            client=SSH(config)
            try:result=client.request(request)
            finally:
                if client.process:client.process.terminate()
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':
    try:main()
    except (OSError,ValueError,KeyError,TypeError) as exc:
        print(str(exc),file=sys.stderr);sys.exit(1)
