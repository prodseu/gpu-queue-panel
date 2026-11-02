"""Core queue and CPU-only execution checks; no server credentials or CUDA needed."""
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from queue_store import QueueStore
from worker import Controller

class IdleReached(BaseException):pass

class DrainFixture(Controller):
    def phase(self,phase,**extra):
        super().phase(phase,**extra)
        if phase.startswith('队列为空'):raise IdleReached()


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.store=QueueStore(self.root/'runtime')
        self.tasks=[]
        for task_id in ('task-a','task-b','task-c'):
            work=self.root/task_id;work.mkdir()
            self.tasks.append(dict(id=task_id,name=task_id,root=str(work),gpu=None,gpu_uuid='',epochs=1,updates=1,
                argv=[sys.executable,'-c',"import sys;open('result.txt','w').write(sys.argv[1])",'literal argument with spaces']))
        self.store.initialize(self.tasks,[t['id'] for t in self.tasks])

    def tearDown(self):self.temp.cleanup()

    def test_cancel_middle_keeps_independent_jobs_executable(self):
        self.store.cancel('task-b')
        with self.assertRaises(IdleReached):
            DrainFixture(self.store,dict(refresh_seconds=.01,stop_on_failure=True)).run()
        value=self.store.read()
        self.assertEqual(value['tasks']['task-b']['state'],'canceled')
        self.assertEqual(value['tasks']['task-a']['state'],'completed')
        self.assertEqual(value['tasks']['task-c']['state'],'completed')
        self.assertFalse((self.root/'task-b/result.txt').exists())
        self.assertEqual((self.root/'task-c/result.txt').read_text(),'literal argument with spaces')

    def test_reorder_invalidates_old_gpu_candidate_and_protects_running(self):
        old=self.store.read()
        self.store.reorder(['task-c','task-b','task-a'],revision=old['revision'])
        self.assertIsNone(self.store.claim_if('task-a',old['revision']))
        with self.assertRaises(ValueError):self.store.cancel('task-b',revision=old['revision'])
        self.assertEqual(QueueStore(self.store.directory).claim()['id'],'task-c')
        with self.assertRaises(ValueError):self.store.cancel('task-c')
        self.assertEqual(self.store.read()['order'],['task-b','task-a'])

    def test_failure_does_not_mark_siblings_failed_or_restart_job(self):
        def fail_first(value):value['tasks']['task-a']['argv']=[sys.executable,'-c','raise SystemExit(3)']
        self.store.change(fail_first)
        Controller(self.store,dict(refresh_seconds=.01,stop_on_failure=True)).run()
        value=self.store.read()
        self.assertEqual(value['tasks']['task-a']['state'],'failed')
        self.assertEqual(value['order'],['task-b','task-c'])
        self.assertTrue(all(value['tasks'][i]['state']=='waiting' for i in value['order']))
        self.assertEqual(json.loads((self.store.directory/'jobs/task-a/receipt.json').read_text())['returncode'],3)

    def test_empty_queue_remains_alive_and_runs_later_CPU_commands(self):
        for task in self.tasks:self.store.cancel(task['id'])
        idle=threading.Event();stop=threading.Event()
        class PersistentFixture(Controller):
            def phase(self,phase,**extra):
                super().phase(phase,**extra)
                if phase.startswith('队列为空'):idle.set()
                if stop.is_set():raise SystemExit()
        worker=PersistentFixture(self.store,dict(refresh_seconds=.01,stop_on_failure=True))
        thread=threading.Thread(target=worker.run);thread.start()
        try:
            self.assertTrue(idle.wait(2));self.assertTrue(thread.is_alive())
            for task_id in ['late-a','late-b']:
                idle.clear()
                def append(value):
                    value['tasks'][task_id]=dict(self.tasks[0],id=task_id,state='waiting')
                    value['order'].append(task_id)
                self.store.change(append)
                deadline=time.monotonic()+4
                while self.store.read()['tasks'][task_id]['state']!='completed':
                    self.assertTrue(thread.is_alive())
                    if time.monotonic()>deadline:self.fail('late CPU task did not complete')
                    time.sleep(.01)
                self.assertTrue(idle.wait(2))
                self.assertEqual(json.loads((self.store.directory/'jobs'/task_id/'receipt.json').read_text())['returncode'],0)
            self.assertTrue(thread.is_alive())
        finally:stop.set();thread.join(2)
        self.assertFalse(thread.is_alive())


if __name__=='__main__':unittest.main(verbosity=2)
