"""Authenticated multiuser queue CLI. Sessions come from users.py login."""
import argparse,json,sys
from auth import read_session,AuthenticationError
from queue_store import QueueStore
from settings import load
from server_agent import rpc

def main():
    if len(sys.argv)>1 and sys.argv[1]=='submit':
        from queue_submit import main as submit_main
        submit_main(sys.argv[2:]);return
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--session-file')
    sub=p.add_subparsers(dest='command',required=True)
    sub.add_parser('status')
    q=sub.add_parser('cancel');q.add_argument('id')
    q=sub.add_parser('reorder');q.add_argument('ids',nargs='+')
    q=sub.add_parser('move-gpu');q.add_argument('id');q.add_argument('gpu_uuid')
    q=sub.add_parser('logs');q.add_argument('id');q.add_argument('--cursor',type=int,default=0)
    a=p.parse_args();config=load();session=read_session(a.session_file)
    request=dict(action='state' if a.command=='status' else 'move_gpu' if a.command=='move-gpu' else a.command,token=session['token'],csrf=session['csrf'])
    if a.command in ('cancel','logs','move-gpu'):request['id']=a.id
    if a.command=='move-gpu':request['gpu_uuid']=a.gpu_uuid
    if a.command=='logs':request['cursor']=a.cursor
    if a.command in ('cancel','reorder','move-gpu'):
        state=rpc(dict(action='state',token=session['token']),QueueStore(config['data_path']))
        request['revision']=state['revision']
    if a.command=='reorder':request['ids']=a.ids
    print(json.dumps(rpc(request,QueueStore(config['data_path'])),ensure_ascii=False,indent=2))
if __name__=='__main__':
    try:main()
    except (AuthenticationError,OSError,ValueError,PermissionError,KeyError) as exc:
        print(str(exc),file=sys.stderr);sys.exit(1)
