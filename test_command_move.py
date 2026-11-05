"""Reproduce config/UUID assertion and exercise the already-loaded runner path."""
import inspect,json,os,sys,unittest
from unittest.mock import patch
import command_runner
import gpu_transfer
from auth import Actor
import test_multiuser as fixtures
from gpu_transfer import prepare_scientific_config

class CommandMoveRegression(unittest.TestCase):
    setUp=fixtures.MultiuserTests.setUp
    tearDown=fixtures.MultiuserTests.tearDown
    request=fixtures.MultiuserTests.request
    task=fixtures.MultiuserTests.task
    def old_runner(self):
        source=inspect.getsource(command_runner.execute)
        source=source.replace('    from gpu_transfer import prepare_scientific_config\n    prepare_scientific_config(task)\n','')
        namespace=dict(command_runner.__dict__);exec(source,namespace)
        return namespace['execute']
    def test_move_then_old_loaded_runner_syncs_config_and_execs_original(self):
        config=dict(gpu=0,gpu_uuid='GPU-OLD',authorized_physical_gpu=0,epochs=20,seed=123)
        path=self.root/'config.json';path.write_text(json.dumps(config))
        code="import json,os,sys;from pathlib import Path;c=json.loads(Path('config.json').read_text());assert c['gpu_uuid']==os.environ['CUDA_VISIBLE_DEVICES'];Path('ok.json').write_text(json.dumps(dict(pid=os.getpid(),args=sys.argv[1:],cuda=os.environ['CUDA_VISIBLE_DEVICES'],config=c)))"
        argv=[sys.executable,'-c',code,'literal ; $() 中文']
        # Exact pre-fix runner+unchanged config fails before executing any model.
        baseline=dict(self.task('before'),argv=argv,gpu=2,gpu_uuid='GPU-NEW')
        with self.assertRaisesRegex(RuntimeError,'1'):self.old_runner()(baseline,self.store,lambda *a,**k:None)
        self.assertFalse((self.root/'ok.json').exists())
        self.request('alice','submit',task=dict(self.task('after'),argv=argv,gpu=0,gpu_uuid='GPU-OLD'))
        for index,uuid in [(1,'GPU-MIDDLE'),(2,'GPU-NEW')]:
            with patch('gpu_transfer.gpu.snapshot',return_value=dict(ok=True,gpus=[dict(index=index,uuid=uuid)])):
                move_source=inspect.getsource(gpu_transfer.move).replace('        wrap_command(task, store.directory)\n','')
                namespace=dict(gpu_transfer.__dict__);exec(move_source,namespace)
                namespace['move'](self.store,'after',uuid,self.store.read()['revision'],Actor('alice','user'))
        self.assertEqual(json.loads(path.read_text()),config)
        claimed=self.store.claim();self.assertEqual(claimed['original_argv'],argv)
        self.old_runner()(claimed,self.store,lambda *a,**k:None)
        actual=json.loads((self.root/'ok.json').read_text())
        self.assertEqual(actual['args'],['literal ; $() 中文']);self.assertEqual(actual['cuda'],'GPU-NEW')
        self.assertEqual(actual['config'],dict(config,gpu=2,gpu_uuid='GPU-NEW',authorized_physical_gpu=2))
        self.assertEqual(json.loads((self.root/'GPU_ASSIGNMENT_ORIGINAL_CONFIG.json').read_text()),config)
        launch=json.loads((self.store.directory/'jobs/after/launch.json').read_text())
        if os.name=='posix':self.assertEqual(actual['pid'],launch['pid'])
        self.assertEqual(json.loads((self.store.directory/'jobs/after/receipt.json').read_text())['returncode'],0)
    def test_generic_command_with_run_directory_has_no_device_config_mutation(self):
        (self.root/'run').mkdir();path=self.root/'config.json';path.write_text('{"epochs": 20}')
        prepare_scientific_config(dict(self.task('generic'),gpu_assignment=dict(index=2),gpu=2,gpu_uuid='GPU-NEW'))
        self.assertEqual(path.read_text(),'{"epochs": 20}')
        self.assertFalse((self.root/'GPU_ASSIGNMENT_ORIGINAL_CONFIG.json').exists())
