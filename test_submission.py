"""One-line enrollment, idempotence and real CPU command execution; no CUDA."""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from queue_store import QueueStore,atomic
from queue_submit import build_task,options,submit,enqueue_if_requested
from command_runner import execute
from scheduling import Admission,Dispatcher

HERE=Path(__file__).resolve().parent


class SubmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.work=self.root/'work with spaces';self.work.mkdir()
        self.store=QueueStore(self.root/'runtime')
        self.store.initialize([],[],metadata=dict(scheduler_mode='persistent',deadline=None))
        atomic(self.store.directory/'scheduler_runtime.json',dict(command_submission_version=1))

    def tearDown(self):self.temp.cleanup()

    def task(self,key='fixture',command=None):
        return dict(id=key,name=key,kind='command',root=str(self.work),gpu=None,gpu_uuid='',
                    argv=command or [sys.executable,'-c','pass'],env={},epochs=0,updates=0,
                    submission_fingerprint='fingerprint-'+key)

    def test_append_dedup_and_conflict_preserve_existing_tasks_and_order(self):
        for key in 'AB':submit(self.task(key),self.store.directory)
        self.store.claim()
        before=self.store.read()
        duplicate=submit(self.task('B'),self.store.directory)
        self.assertTrue(duplicate['duplicate'])
        self.assertEqual(self.store.read()['tasks'],before['tasks'])
        self.assertEqual(self.store.read()['order'],before['order'])
        conflicting=dict(self.task('B'),submission_fingerprint='different')
        with self.assertRaises(ValueError):submit(conflicting,self.store.directory)
        submit(self.task('C'),self.store.directory)
        after=self.store.read()
        self.assertEqual(after['running'],'A');self.assertEqual(after['order'],['B','C'])
        self.store.cancel('B');self.store.finish('A',True)
        self.assertTrue(submit(self.task('A'),self.store.directory)['duplicate'])
        self.assertEqual(self.store.read()['order'],['C'])

    def test_simultaneous_duplicate_submissions_append_once(self):
        results=[]
        threads=[threading.Thread(target=lambda:results.append(submit(self.task(),self.store.directory))) for _ in range(8)]
        for thread in threads:thread.start()
        for thread in threads:thread.join()
        self.assertEqual(len(results),8)
        self.assertEqual(sum(not r['duplicate'] for r in results),1)
        self.assertEqual(self.store.read()['order'],['fixture'])

    def test_submit_does_not_execute_and_runner_preserves_literal_argv_cwd_identity(self):
        literal="a b;$() ' quote 中文"
        code="import json,os,sys;from pathlib import Path;Path('result.json').write_text(json.dumps(dict(arg=sys.argv[1],cwd=os.getcwd(),job=os.environ['GPU_QUEUE_JOB_ID'],cuda=os.environ['CUDA_VISIBLE_DEVICES'],custom=os.environ['MY_SETTING'])))"
        task=self.task(command=[sys.executable,'-c',code,literal]);task['env']={'MY_SETTING':'value with spaces'}
        submit(task,self.store.directory)
        self.assertFalse((self.work/'result.json').exists())
        claimed=self.store.claim();execute(claimed,self.store,lambda *a,**k:None)
        value=json.loads((self.work/'result.json').read_text())
        self.assertEqual(value,dict(arg=literal,cwd=str(self.work),job='fixture',cuda='',custom='value with spaces'))
        self.assertEqual(json.loads((self.store.directory/'jobs/fixture/receipt.json').read_text())['returncode'],0)
        if os.name=='posix':self.assertIsNotNone(json.loads((self.store.directory/'jobs/fixture/launch.json').read_text())['identity'])

    def test_private_command_branch_bypasses_scientific_pipeline(self):
        if not (HERE/'existing_worker.py').exists():self.skipTest('current/private adapter only')
        from existing_worker import Controller
        task=self.task();submit(task,self.store.directory)
        worker=Controller.__new__(Controller)
        worker.store=self.store;worker.phase=lambda *a,**k:None
        worker.execute(self.store.claim())
        self.assertTrue((self.store.directory/'jobs/fixture/receipt.json').is_file())

    def test_missing_queue_or_old_private_adapter_is_not_enrolled(self):
        with self.assertRaises(ValueError):submit(self.task(),self.root/'missing')
        if (HERE/'existing_worker.py').exists():
            atomic(self.store.directory/'scheduler_runtime.json',dict(command_submission_version=0))
            with self.assertRaises(ValueError):submit(self.task(),self.store.directory)
            self.assertEqual(self.store.read()['order'],[])

    def test_opt_in_is_inert_without_flag_and_rejects_recursive_submit(self):
        self.assertFalse(enqueue_if_requested(['--epochs','20','--gpu','2']))
        with patch.dict(os.environ,{'GPU_QUEUE_JOB_ID':'current'}):
            with self.assertRaises(ValueError):enqueue_if_requested(['--queue','--queue-cpu'])

    @unittest.skipUnless(os.name=='posix','server CLI validation on Linux')
    def test_builder_resolves_GPU_env_and_keeps_argv_without_shell(self):
        with patch.dict(os.environ,{'CUDA_VISIBLE_DEVICES':'2','SECRET_FIXTURE_TOKEN':'do-not-capture'}),patch('queue_submit.resolve_gpu',return_value=(2,'GPU-XXX')) as resolve:
            parser=argparse.ArgumentParser();options(parser)
            args=parser.parse_args(['--cwd',str(self.work),'--peak-memory-mib','-1'])
            task=build_task(args,[sys.executable,'train.py','--gpu','7','--text','a ; b'])
            resolve.assert_called_once_with('2')
            self.assertEqual(task['gpu_uuid'],'GPU-XXX');self.assertEqual(task['peak_memory_mib'],-1)
            self.assertEqual(task['argv'][-4:],['--gpu','7','--text','a ; b'])
            self.assertNotIn('SECRET_FIXTURE_TOKEN',task['env'])
            self.assertEqual(task['submission_fingerprint'],build_task(args,task['argv'])['submission_fingerprint'])
            args.env=['GPU_QUEUE_JOB_ID=forged']
            with self.assertRaises(ValueError):build_task(args,[sys.executable,'-c','pass'])

    @unittest.skipUnless(os.name=='posix','server integration on Linux')
    def test_train_py_queue_flag_exits_launcher_then_runs_clean_args_once(self):
        source="""from queue_submit import enqueue_if_requested
enqueue_if_requested()
import argparse,json,os
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--epochs');p.add_argument('--note');p.add_argument('--gpu')
a=p.parse_args();Path('trained.json').write_text(json.dumps(dict(epochs=a.epochs,note=a.note,gpu=a.gpu,job=os.environ['GPU_QUEUE_JOB_ID'])))
"""
        script=self.work/'train.py';script.write_text(source)
        env=dict(os.environ,PYTHONPATH=str(HERE))
        cmd=[sys.executable,str(script),'--queue','--queue-cpu','--queue-data',str(self.store.directory),
             '--queue-id','integrated','--epochs','3','--note','literal ; arg','--gpu','7']
        result=subprocess.run(cmd,cwd=self.work,env=env,capture_output=True,text=True,check=True)
        self.assertEqual(json.loads(result.stdout)['id'],'integrated')
        self.assertFalse((self.work/'trained.json').exists())
        queued=self.store.read()['tasks']['integrated']
        self.assertNotIn('--queue',queued['argv'])
        execute(self.store.claim(),self.store,lambda *a,**k:None)
        self.store.finish('integrated',True)
        self.assertEqual(json.loads((self.work/'trained.json').read_text()),dict(epochs='3',note='literal ; arg',gpu='7',job='integrated'))
        again=subprocess.run(cmd,cwd=self.work,env=env,capture_output=True,text=True,check=True)
        self.assertTrue(json.loads(again.stdout)['duplicate']);self.assertEqual(self.store.read()['order'],[])

    @unittest.skipUnless(os.name=='posix','one-line qrun tool on Linux')
    def test_qrun_and_cli_submit_append_without_starting_another_worker(self):
        command=[sys.executable,'-c',"from pathlib import Path;Path('done').write_text('done')"]
        result=subprocess.run([sys.executable,str(HERE/'qrun'),'--cpu','--data',str(self.store.directory),
                               '--cwd',str(self.work),'--id','wrapper','--',*command],
                              capture_output=True,text=True,check=True)
        self.assertEqual(json.loads(result.stdout)['id'],'wrapper')
        self.assertFalse((self.work/'done').exists())
        if (HERE/'cli.py').exists():
            config=self.root/'config.json';config.write_text(json.dumps(dict(data_dir=str(self.store.directory))))
            entry=HERE/'cli.py';env=dict(os.environ,GPU_QUEUE_CONFIG=str(config))
        else:
            entry=HERE/'s101q.py';env=dict(os.environ)
        result=subprocess.run([sys.executable,str(entry),'submit','--cpu','--data',str(self.store.directory),
                               '--cwd',str(self.work),'--id','cli-wrapper','--',*command],
                              env=env,capture_output=True,text=True,check=True)
        self.assertEqual(json.loads(result.stdout)['id'],'cli-wrapper')
        self.assertEqual(self.store.read()['order'],['wrapper','cli-wrapper'])
        self.assertFalse((self.store.directory/'agent.json').exists())


if __name__=='__main__':unittest.main(verbosity=2)
