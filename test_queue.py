"""Core queue and CPU-only execution checks; no server credentials or CUDA needed."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from queue_store import QueueStore
from worker import Controller


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
        Controller(self.store,dict(refresh_seconds=.01,stop_on_failure=True)).run()
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


if __name__=='__main__':unittest.main(verbosity=2)
