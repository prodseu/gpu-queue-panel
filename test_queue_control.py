"""Permissions, stale claims, manual CPU execution and pause behavior."""
import tempfile,time,sys,unittest
from pathlib import Path
from unittest.mock import patch
from auth import Accounts,Actor
from queue_store import QueueStore
from queue_control import configure,request_start
from scheduling import Dispatcher,DEFAULTS
from worker import Controller

class ControlsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.store=QueueStore(self.root/'data')
        accounts=Accounts(self.store.directory);accounts.add('alice','fixture-password-123');accounts.add('admin','fixture-password-123','admin')
        self.admin=Actor('admin','admin');self.alice=Actor('alice','user')
        tasks=[dict(id=k,name=k,root=str(self.root),owner_id='alice',gpu=None,gpu_uuid='',argv=[sys.executable,'-c',"from pathlib import Path;Path('"+k+".done').write_text('done')"]) for k in ['A','B']]
        self.store.initialize(tasks,['A','B']);self.checked=0
        class Admission:
            def check(inner,*args):self.checked+=1;return False,'blocked'
        self.dispatcher=Dispatcher(self.store,lambda task:Controller(self.store,{}),lambda *a,**k:None,Admission(),interval=.01)
    def configure(self,mode,paused=False):configure(self.store,mode,paused,self.store.read()['revision'],self.admin)
    def request(self,key):request_start(self.store,key,self.store.read()['revision'],self.alice)
    def finish_active(self):
        deadline=time.time()+5
        while self.dispatcher.active and time.time()<deadline:self.dispatcher.tick();time.sleep(.02)
        self.assertFalse(self.dispatcher.active)
    def test_auto_protection_manual_waits_then_selected_nonhead_runs(self):
        self.dispatcher.tick();self.assertEqual(self.checked,1)
        self.configure('manual');self.dispatcher.tick();self.assertEqual(self.checked,1);self.assertFalse(self.dispatcher.active)
        self.request('B');self.dispatcher.tick();self.finish_active()
        self.assertEqual(self.checked,1);self.assertTrue((self.root/'B.done').exists());self.assertFalse((self.root/'A.done').exists())
        self.assertEqual(self.store.read()['order'],['A'])
    def test_pause_mode_switch_revoke_requests_and_stale_claims(self):
        self.configure('manual');self.request('B');before=self.store.read();self.configure('manual',True)
        self.assertIsNone(self.store.claim_if('B',before['revision'],manual=True))
        self.assertIsNone(self.store.claim_if('B',self.store.read()['revision'],manual=True))
        with self.assertRaises(ValueError):self.request('B')
        self.configure('manual');self.request('B');self.configure('auto')
        self.assertIsNone(self.store.claim_if('B',self.store.read()['revision'],manual=True))
    def test_pause_keeps_running_CPU_alive(self):
        self.store.change(lambda v:v['tasks']['A'].update(argv=[sys.executable,'-c','import time;time.sleep(.3)']))
        self.configure('manual');self.request('A');self.dispatcher.tick()
        self.assertTrue(self.dispatcher.active['A'][1].is_alive())
        self.configure('manual',True);self.dispatcher.tick();self.assertTrue(self.dispatcher.active['A'][1].is_alive());self.finish_active()
        self.assertEqual(self.store.read()['tasks']['A']['state'],'completed');self.assertEqual(self.store.read()['tasks']['B']['state'],'waiting')
    def test_permissions_and_disabled_owner(self):
        with self.assertRaises(PermissionError):configure(self.store,'manual',False,0,self.alice)
        self.configure('manual')
        with self.assertRaises(PermissionError):request_start(self.store,'A',self.store.read()['revision'],Actor('bob','user'))
        self.request('A');Accounts(self.store.directory).disable('alice');self.dispatcher.tick();self.assertFalse(self.dispatcher.active)
    def test_manual_window_override_needs_request(self):
        self.store.configure(dict(DEFAULTS,window=1),actor=self.admin)
        self.configure('manual');self.request('A');self.assertIsNotNone(self.store.claim_if('A',self.store.read()['revision'],manual=True))
        self.assertIsNone(self.store.claim_if('B',self.store.read()['revision'],manual=True))
        self.request('B');self.assertIsNotNone(self.store.claim_if('B',self.store.read()['revision'],manual=True))
    def test_missing_device_rejected_without_process_inspection(self):
        self.store.change(lambda v:v['tasks']['A'].update(gpu=2,gpu_uuid='GPU-XXX'))
        self.configure('manual');self.request('A')
        with patch('gpu.snapshot',return_value=dict(ok=True,gpus=[])),patch('gpu.identity',side_effect=AssertionError('foreign identity read')):self.dispatcher.tick()
        self.assertFalse(self.dispatcher.active)
