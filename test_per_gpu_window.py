"""Independent device capacities, per-card FIFO and atomic claims."""
import tempfile,threading,unittest
from pathlib import Path
from auth import Accounts,Actor
from queue_store import QueueStore
from scheduling import DEFAULTS,Dispatcher,queue_heads,running_in_queue

class PerGPUWindowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=QueueStore(Path(self.temp.name)/'data')
        Accounts(self.store.directory).add('alice','fixture-password-123','admin');self.actor=Actor('alice','admin')
        tasks=[dict(id=key,name=key,owner_id='alice',root=self.temp.name,gpu=index,gpu_uuid=uuid) for key,index,uuid in [('A1',0,'GPU-A'),('A2',0,'GPU-A'),('B1',1,'GPU-B'),('B2',1,'GPU-B'),('CPU',None,'')]]
        self.store.initialize(tasks,[t['id'] for t in tasks]);self.store.configure(dict(DEFAULTS,window=1),actor=self.actor)
        self.release=threading.Event();self.workers=[]
        class Worker:
            def execute(inner,task):self.release.wait(5)
        class Admission:
            def check(inner,task,value):return True,'ok'
        self.dispatcher=Dispatcher(self.store,lambda task:Worker(),lambda *a,**k:None,Admission(),interval=.01)
        self.addCleanup(self.cleanup_workers)
    def cleanup_workers(self):
        self.release.set()
        for _,thread,_ in self.dispatcher.active.values():thread.join(2)
    def test_each_card_and_CPU_can_fill_own_window(self):
        for _ in range(3):self.dispatcher.tick()
        value=self.store.read()
        self.assertEqual(set(self.dispatcher.active),{'A1','B1','CPU'})
        self.assertEqual(value['order'],['A2','B2'])
        self.assertEqual(len(running_in_queue(value,value['tasks']['A2'])),1)
        self.assertIsNone(self.store.claim(expected_id='A2'))
        self.store.configure(dict(DEFAULTS,window=-1),actor=self.actor)
        self.assertIsNotNone(self.store.claim(expected_id='B2'))
    def test_blocked_head_does_not_block_other_card_or_skip_same_card(self):
        seen=[]
        class Admission:
            def check(inner,task,value):
                seen.append(task['id']);return task['id']!='A1','blocked' if task['id']=='A1' else 'ok'
        self.dispatcher.admission=Admission();self.dispatcher.tick()
        self.assertEqual(seen,['A1','B1']);self.assertEqual(set(self.dispatcher.active),{'B1'})
        self.assertEqual(self.store.read()['order'],['A1','A2','B2','CPU'])
    def test_concurrent_claims_cannot_exceed_each_card_window(self):
        barrier=threading.Barrier(5);results=[]
        def claim(key):barrier.wait();results.append(self.store.claim(expected_id=key))
        threads=[threading.Thread(target=claim,args=(key,)) for key in ['A1','A2','B1','B2']]
        for thread in threads:thread.start()
        barrier.wait()
        for thread in threads:thread.join()
        value=self.store.read();self.assertEqual(sum(t is not None for t in results),2)
        self.assertEqual(len(running_in_queue(value,value['tasks']['A2'])),1)
        self.assertEqual(len(running_in_queue(value,value['tasks']['B2'])),1)
    def test_UUID_numbering_and_nonhead_rejection(self):
        self.assertIsNone(self.store.claim(expected_id='A2'))
        self.assertIsNotNone(self.store.claim(expected_id='A1'))
        self.store.change(lambda v:v['tasks']['A2'].update(gpu=9))
        self.assertIsNone(self.store.claim(expected_id='A2'))
        self.assertIsNotNone(self.store.claim(expected_id='B1'))
        self.assertEqual([t['id'] for t in queue_heads(self.store.read())],['A2','B2','CPU'])
