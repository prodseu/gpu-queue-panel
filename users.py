"""Server operator account setup and authenticated CLI login. No default password."""
import argparse
import getpass
import json
import sys

from auth import Accounts,AuthenticationError,read_session,save_session,session_path,require_rc,username
from queue_store import QueueStore
from settings import load


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data');parser.add_argument('--session-file')
    sub=parser.add_subparsers(dest='action',required=True)
    for action in ('bootstrap','add','login','password','disable'):
        p=sub.add_parser(action);p.add_argument('username')
        if action=='add':p.add_argument('--admin',action='store_true')
    sub.add_parser('list');sub.add_parser('logout')
    p=sub.add_parser('assign');p.add_argument('task_id');p.add_argument('username')
    args=parser.parse_args();store=QueueStore(args.data or load()['data_path'])
    if not store.path.exists():
        if args.action!='bootstrap':raise ValueError('请先bootstrap初始化预发布队列')
        store.initialize([],[],metadata={'multiuser_rc':True})
    require_rc(store);accounts=Accounts(store.directory)
    if args.action=='bootstrap':
        if accounts.list():raise PermissionError('账号已存在，不能再次bootstrap')
        password=getpass.getpass('管理员密码（至少12字符）: ')
        if password!=getpass.getpass('再次输入: '):raise ValueError('两次密码不一致')
        accounts.add(args.username,password,'admin')
        session=accounts.login(args.username,password);save_session(session,args.session_file)
        result=dict(user=session['user'],initialized=True)
    elif args.action=='login':
        session=accounts.login(args.username,getpass.getpass('密码: '));save_session(session,args.session_file)
        result=dict(user=session['user'],expires=session['expires'])
    else:
        session=read_session(args.session_file);actor,_=accounts.session(session['token'])
        if args.action=='logout':
            accounts.logout(session['token']);session_path(args.session_file).unlink(missing_ok=True)
            result=dict(logged_out=True)
        else:
            if not actor.admin:raise PermissionError('仅管理员可管理账号或任务归属')
            if args.action=='list':result=accounts.list()
            elif args.action=='add':
                password=getpass.getpass('新用户密码（至少12字符）: ')
                if password!=getpass.getpass('再次输入: '):raise ValueError('两次密码不一致')
                result=accounts.add(args.username,password,'admin' if args.admin else 'user')
            elif args.action=='password':
                password=getpass.getpass('新密码（至少12字符）: ')
                if password!=getpass.getpass('再次输入: '):raise ValueError('两次密码不一致')
                accounts.reset_password(args.username,password);result=dict(password_reset=True,sessions_revoked=True)
            elif args.action=='disable':
                if username(args.username)==actor.username:raise ValueError('不能停用当前管理员自身')
                accounts.disable(args.username);result=dict(disabled=True,sessions_revoked=True)
            else:
                target=username(args.username)
                if not accounts.enabled(target):raise ValueError('目标账号不存在或已停用')
                def assign(value):
                    task=value['tasks'][args.task_id]
                    if task['state']!='waiting':raise ValueError('只可分配等待任务，运行项不变')
                    task['owner_id']=target;value['last_change']='管理员分配等待任务归属'
                store.change(assign);result=dict(id=args.task_id,owner_id=target)
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    try:main()
    except (AuthenticationError,OSError,ValueError,PermissionError,KeyError) as exc:
        print(str(exc),file=sys.stderr);sys.exit(1)
