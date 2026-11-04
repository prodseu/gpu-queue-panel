"""Isolated account/RPC/HTTP/CPU checks. Never connects to SSH or real GPUs."""
import http.client
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from auth import Accounts, Actor, AuthenticationError, SESSION_SECONDS
from local_view import create_server
from queue_store import QueueStore, atomic
from scheduling import DEFAULTS, Admission, Dispatcher
from server_agent import rpc, snapshot
from worker import Controller

PASSWORD='fixture-password-123'


class RPCClient:
    def __init__(self,store):self.store=store
    def request(self,request):return rpc(request,self.store)


class MultiuserTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.store=QueueStore(self.root/'data')
        self.store.initialize([],[])
        self.accounts=Accounts(self.store.directory)
        self.sessions={}
        for name,role in [('alice','user'),('bob','user'),('admin','admin')]:
            self.accounts.add(name,PASSWORD,role)
            self.sessions[name]=self.accounts.login(name,PASSWORD)
        self.monitor=patch('process_monitor.collect',return_value=dict(tasks={},observed=[],error=None))
        self.mock_monitor=self.monitor.start()

    def tearDown(self):self.monitor.stop();self.temp.cleanup()

    def request(self,user,action,**payload):
        s=self.sessions[user]
        return rpc(dict(action=action,token=s['token'],csrf=s['csrf'],**payload),self.store)

    def task(self,key):
        return dict(id=key,name='Task '+key,root=str(self.root),gpu=None,gpu_uuid='',
                    argv=[sys.executable,'-c','print("CPU fixture")'],env={})

    def add(self,user,key):return self.request(user,'submit',task=self.task(key))

    def test_hashes_sessions_expiry_and_logout(self):
        with self.accounts.connect() as c:
            rows=list(c.execute('SELECT salt,hash FROM users'))
            self.assertEqual(len({r['salt'] for r in rows}),3)
            stored=list(c.execute('SELECT hash FROM sessions'))
            self.assertNotIn(self.sessions['alice']['token'],[r['hash'] for r in stored])
        self.assertNotIn(PASSWORD.encode(),self.accounts.path.read_bytes())
        token=self.sessions['alice']['token']
        future=Accounts(self.store.directory,clock=lambda:time.time()+SESSION_SECONDS+1)
        with self.assertRaises(AuthenticationError):future.session(token)
        self.request('alice','logout')
        with self.assertRaises(AuthenticationError):self.request('alice','state')

    def test_login_lockout_reset_disable_revoke(self):
        now=[1000.];accounts=Accounts(self.store.directory,clock=lambda:now[0])
        for _ in range(5):
            with self.assertRaises(AuthenticationError):accounts.login('alice','wrong')
        with self.assertRaisesRegex(AuthenticationError,'次数过多'):accounts.login('alice',PASSWORD)
        now[0]+=61
        accounts.login('alice',PASSWORD)
        accounts.reset_password('alice','replacement-password-123')
        with self.assertRaises(AuthenticationError):self.request('alice','me')
        self.accounts.disable('bob')
        with self.assertRaises(AuthenticationError):self.request('bob','state')

    def test_anonymous_rpc_and_missing_csrf_denied(self):
        for action in ('me','state','logs','submit','cancel','reorder','configure'):
            with self.assertRaises(AuthenticationError):rpc(dict(action=action),self.store)
        before=self.store.read()
        with self.assertRaises(PermissionError):
            rpc(dict(action='submit',token=self.sessions['alice']['token'],task=self.task('a')),self.store)
        self.assertEqual(before,self.store.read())

    def test_snapshot_filters_before_progress_and_process_reads(self):
        self.add('alice','a');self.add('bob','b')
        seen=[]
        def progress(spec):
            seen.append(spec['status_file']);return dict(percent=None,status={})
        # Give foreign tasks a distinct root to detect accidental file reads.
        self.store.change(lambda v:v['tasks']['b'].update(root=str(self.root/'PRIVATE_BOB')))
        with patch('server_agent.load',side_effect=progress):
            state=self.request('alice','state')
        self.assertEqual([t['id'] for t in state['tasks']],['a'])
        self.assertTrue(all('PRIVATE_BOB' not in p for p in seen))
        args,kwargs=self.mock_monitor.call_args
        self.assertEqual(set(args[0]['tasks']),{'a'})
        self.assertFalse(kwargs['include_trusted'])
        self.assertNotIn('"b"',json.dumps(state['scheduler']))
        self.assertEqual({t['id'] for t in self.request('admin','state')['tasks']},{'a','b'})
        self.assertTrue(self.mock_monitor.call_args.kwargs['include_trusted'])

    def test_foreign_logs_cancel_configure_and_forged_owner_do_not_mutate(self):
        self.add('alice','a');self.add('bob','b');before=self.store.read()
        atomic(self.store.directory/'jobs/b/receipt.json',dict(private=True))
        log=self.store.directory/'jobs/b/output.log';log.write_text('BOB SECRET')
        for action,payload in [('logs',dict(id='b')),('cancel',dict(id='b',revision=before['revision'])),
            ('configure',dict(settings=DEFAULTS,revision=before['revision'])),
            ('submit',dict(task=dict(self.task('evil'),owner_id='bob'))),
            ('submit',dict(task=dict(self.task('evil'),state='running')))]:
            with self.subTest(action=action,payload=payload):
                with self.assertRaises(PermissionError):self.request('alice',action,**payload)
                self.assertEqual(before,self.store.read())
        # Denial must occur before log open, even for a valid foreign ID.
        original=Path.open
        def guarded(path,*args,**kwargs):
            if path==log:raise AssertionError('foreign log read')
            return original(path,*args,**kwargs)
        with patch('pathlib.Path.open',guarded):
            with self.assertRaises(PermissionError):self.request('alice','logs',id='b')

    def test_owned_logs_bounded_and_foreign_id_is_generic(self):
        self.add('alice','a');self.add('bob','b')
        directory=self.store.directory/'jobs/a';directory.mkdir(parents=True)
        (directory/'output.log').write_text('hello-world')
        self.assertEqual(self.request('alice','logs',id='a',cursor=2,max_bytes=3)['text'],'llo')
        for key in ('b','missing','../b'):
            with self.assertRaisesRegex(PermissionError,'^无权访问该任务$'):self.request('alice','logs',id=key)
        with self.assertRaises(ValueError):self.request('alice','logs',id='a',max_bytes=999999)

    def test_reorder_preserves_other_users_exact_slots_and_cancel_independent(self):
        for user,key in [('alice','a1'),('bob','b1'),('alice','a2'),('bob','b2'),('alice','a3')]:self.add(user,key)
        self.request('alice','reorder',ids=['a3','a1','a2'],revision=self.store.read()['revision'])
        self.assertEqual(self.store.read()['order'],['a3','b1','a1','b2','a2'])
        before=self.store.read()
        with self.assertRaises(ValueError):self.request('alice','reorder',ids=['a1','b1','a2'],revision=before['revision'])
        self.assertEqual(before,self.store.read())
        self.request('alice','cancel',id='a1',revision=before['revision'])
        self.assertEqual(self.store.read()['order'],['a3','b1','b2','a2'])
        self.request('admin','reorder',ids=['b2','a2','b1','a3'],revision=self.store.read()['revision'])
        self.assertEqual(self.store.read()['order'],['b2','a2','b1','a3'])

    def test_submit_owner_dedup_collision_and_env_validation(self):
        self.add('alice','a')
        self.assertTrue(self.add('alice','a')['duplicate'])
        with self.assertRaises(PermissionError):self.add('bob','a')
        result=self.request('bob','submit',task=self.task('auto'),auto_id=True)
        self.assertEqual(result['id'],'bob-auto')
        self.assertEqual(self.store.read()['tasks']['bob-auto']['owner_id'],'bob')
        with self.assertRaises(ValueError):self.request('alice','submit',task=dict(self.task('bad'),env={'GPU_QUEUE_JOB_ID':'evil'}))
        with self.assertRaises(ValueError):self.request('alice','submit',task=dict(self.task('bad'),peak_memory_mib=0))

    def test_ownerless_disabled_tasks_never_claimed(self):
        self.add('alice','a');self.add('bob','b')
        self.store.change(lambda v:v['tasks']['a'].pop('owner_id'))
        self.assertEqual(self.request('alice','state')['tasks'],[])
        self.assertIsNone(self.store.claim())
        self.store.change(lambda v:v['tasks']['a'].update(owner_id='alice'))
        self.accounts.disable('alice');self.assertIsNone(self.store.claim())
        self.assertEqual(self.store.read()['order'],['a','b'])

    def test_refuses_existing_single_user_queue(self):
        self.store.change(lambda v:v['metadata'].pop('multiuser_rc'))
        before=self.store.read()
        with self.assertRaisesRegex(ValueError,'拒绝接管'):self.request('alice','state')
        self.assertEqual(before,self.store.read())

    def test_real_cpu_jobs_different_owners_failure_continues(self):
        self.request('alice','submit',task=dict(self.task('fail'),argv=[sys.executable,'-c','raise SystemExit(3)']))
        self.add('bob','success')
        worker=Controller(self.store,dict(refresh_seconds=.01))
        dispatcher=Dispatcher(self.store,lambda t:worker,lambda *a,**k:None,Admission(self.store.directory),interval=.01)
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            value=dispatcher.tick()
            if not value['order'] and not dispatcher.active:break
            time.sleep(.01)
        self.assertFalse(dispatcher.active);value=self.store.read()
        self.assertEqual(value['tasks']['fail']['state'],'failed')
        self.assertEqual(value['tasks']['success']['state'],'completed')
        self.assertEqual(value['tasks']['fail']['owner_id'],'alice')
        self.assertEqual(value['tasks']['success']['owner_id'],'bob')


class HTTPTests(unittest.TestCase):
    setUp=MultiuserTests.setUp
    tearDown=MultiuserTests.tearDown
    request=MultiuserTests.request
    task=MultiuserTests.task
    add=MultiuserTests.add
    def test_http_cookie_csrf_origin_impersonation_and_logout(self):
        self.add('alice','a');self.add('bob','b')
        server=create_server(RPCClient(self.store),0)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def request(path,body=None,headers=None):
            c=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
            h={'Content-Type':'application/json',**(headers or {})}
            c.request('POST' if body is not None else 'GET',path,json.dumps(body) if body is not None else None,h)
            r=c.getresponse();result=(r.status,dict(r.getheaders()),json.loads(r.read()));c.close();return result
        try:
            self.assertEqual(request('/api/me')[0],401)
            status,headers,result=request('/api/login',dict(username='alice',password=PASSWORD))
            self.assertEqual(status,200);self.assertNotIn('token',result)
            cookie=headers['Set-Cookie'];self.assertIn('HttpOnly',cookie);self.assertIn('SameSite=Strict',cookie)
            h={'Cookie':cookie.split(';')[0]}
            state=request('/api/queue',dict(action='state',token=self.sessions['admin']['token'],role='admin'),h)
            self.assertEqual([t['id'] for t in state[2]['tasks']],['a'])
            self.assertEqual(state[2]['user']['role'],'user')
            revision=self.store.read()['revision']
            self.assertEqual(request('/api/queue',dict(action='cancel',id='a',revision=revision),h)[0],403)
            h['X-CSRF-Token']=result['csrf']
            self.assertEqual(request('/api/queue',dict(action='submit',task=self.task('web')),h)[0],409)
            self.assertEqual(request('/api/queue',dict(action='cancel',id='b',revision=revision),h)[0],403)
            self.assertEqual(request('/api/queue',dict(action='state'),dict(h,Origin='https://example.invalid'))[0],403)
            self.assertEqual(request('/health',headers={'Host':'evil.invalid'})[0],403)
            self.assertEqual(request('/api/queue',dict(action='cancel',id='a',revision=revision),h)[0],200)
            self.assertEqual(request('/api/logout',{},h)[0],200)
            self.assertEqual(request('/api/me',headers=h)[0],401)
        finally:server.shutdown();server.server_close();thread.join(2)


if __name__=='__main__':unittest.main(verbosity=2)
