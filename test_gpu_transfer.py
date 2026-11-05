"""Waiting-device changes preserve ownership and claim revision protection."""
import json,unittest
from unittest.mock import patch
import test_multiuser as fixtures
from gpu_transfer import prepare_scientific_config
class MoveTests(unittest.TestCase):
    setUp=fixtures.MultiuserTests.setUp
    tearDown=fixtures.MultiuserTests.tearDown
    request=fixtures.MultiuserTests.request
    task=fixtures.MultiuserTests.task
    add=fixtures.MultiuserTests.add
    def test_move_and_config(self):
        self.request('alice','submit',task=dict(self.task('a'),gpu=0,gpu_uuid='GPU-XXX-0'))
        self.add('bob','b');before=self.store.read()
        inventory=dict(ok=True,gpus=[dict(index=2,uuid='GPU-XXX-2')])
        with patch('gpu_transfer.gpu.snapshot',return_value=inventory):
            with self.assertRaises(PermissionError):self.request('bob','move_gpu',id='a',gpu_uuid='GPU-XXX-2',revision=before['revision'])
            with self.assertRaises(ValueError):self.request('alice','move_gpu',id='a',gpu_uuid='missing',revision=before['revision'])
            self.request('alice','move_gpu',id='a',gpu_uuid='GPU-XXX-2',revision=before['revision'])
            after=self.store.read();self.assertEqual(after['order'],before['order']);self.assertEqual(after['tasks']['b'],before['tasks']['b'])
            self.assertEqual(after['tasks']['a']['gpu'],2);self.assertEqual(after['tasks']['a']['owner_id'],'alice')
            self.assertIsNone(self.store.claim_if('a',before['revision']))
            task=self.store.claim()
            with self.assertRaises(ValueError):self.request('alice','move_gpu',id='a',gpu_uuid='GPU-XXX-2',revision=self.store.read()['revision'])
        path=self.root/'config.json';original=dict(gpu=0,gpu_uuid='GPU-XXX-0',authorized_physical_gpu=0,epochs=20)
        path.write_text(json.dumps(original));prepare_scientific_config(task);prepare_scientific_config(task)
        config=json.loads(path.read_text());self.assertEqual(config['gpu'],2);self.assertEqual(config['gpu_uuid'],'GPU-XXX-2');self.assertEqual(config['epochs'],20)
        self.assertEqual(json.loads((self.root/'GPU_ASSIGNMENT_ORIGINAL_CONFIG.json').read_text()),original)
        (self.root/'run').mkdir()
        with self.assertRaises(ValueError):prepare_scientific_config(task)
