"""Privacy boundary and immutable observed processes, without real GPU queries."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch,call
import gpu
import process_monitor
from queue_store import QueueStore,atomic
from server_agent import rpc


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.store=QueueStore(self.root/'data')
        self.task=dict(id='R',name='our job',root=str(self.root/'job'),gpu=0,gpu_uuid='GPU-XXX-0',epochs=20,updates=100)
        self.store.initialize([self.task],[],running='R')
        self.saved=dict(pid=101,start_ticks=11,boot='XXX')
        self.proc=dict(self.saved,state='R',uid=1000,args=['/XXX/bin/python','-u',(self.root/'job/train.py').as_posix()])
        atomic(self.root/'job/LAUNCH.json',dict(pid=101,identity=self.saved))
        self.devices=[dict(index=0,uuid='GPU-XXX-0'),dict(index=1,uuid='GPU-XXX-1')]

    def tearDown(self):self.temp.cleanup()

    def inventory(self):
        rows=[['GPU-XXX-0','101','/XXX/bin/python','1024'],
              ['GPU-XXX-1','909','/OTHER/bin/python','4096'],
              ['GPU-XXX-0','808','/XXX/bin/python','3072']]
        # Even processes sharing our Unix UID are untrusted unless explicitly
        # registered. Neither unknown PID may be inspected via /proc.
        with patch('gpu.query',return_value=rows),patch('gpu.own_process_ids',return_value={101,808,909}),\
            patch('gpu.identity',return_value=self.proc) as identity:
            result=process_monitor.collect(self.store.read(),self.devices,self.store.directory)
            self.assertEqual(identity.call_args_list,[call(101,expected=self.saved)])
        return result

    def test_unknown_processes_are_NVIDIA_metadata_only(self):
        result=self.inventory()
        self.assertIsNone(result['error'])
        self.assertEqual(result['tasks']['R'][0]['pid'],101)
        self.assertEqual(result['tasks']['R'][0]['name'],'train.py')
        self.assertEqual({p['pid'] for p in result['observed']},{808,909})
        for item in result['observed']:
            self.assertTrue(item['read_only']);self.assertNotIn('args',item)
            self.assertNotIn('root',item);self.assertNotIn('environment',item)
        external=next(p for p in result['observed'] if p['pid']==909)
        self.assertEqual(external['python'],'/OTHER/bin/python')
        self.assertEqual(external['memory_used_mib'],4096)

    def test_forged_cancel_and_reorder_do_not_modify_queue(self):
        item=self.inventory()['observed'][0];before=self.store.read()
        with self.assertRaisesRegex(ValueError,'不可操作'):rpc(dict(action='cancel',id=item['id']),self.store)
        with self.assertRaises(ValueError):rpc(dict(action='reorder',ids=[item['id']]),self.store)
        with self.assertRaises(ValueError):rpc(dict(action='kill',pid=item['pid']),self.store)
        self.assertEqual(before,self.store.read())

    def test_old_PID_without_identity_is_not_inspected(self):
        atomic(self.root/'job/LAUNCH.json',dict(pid=101))
        with patch('gpu.query',return_value=[['GPU-XXX-0','101','/XXX/bin/python','100']]),\
            patch('gpu.own_process_ids',return_value={101}),patch('gpu.identity') as identity:
            result=process_monitor.collect(self.store.read(),self.devices,self.store.directory)
            identity.assert_not_called();self.assertFalse(result['tasks']);self.assertEqual(len(result['observed']),1)

    def test_inventory_failure_does_not_change_tasks(self):
        before=self.store.read()
        with patch('gpu.query',side_effect=subprocess.CalledProcessError(1,'nvidia-smi')):
            result=process_monitor.collect(before,self.devices,self.store.directory)
        self.assertTrue(result['error']);self.assertEqual(before,self.store.read())

    def test_scheduler_does_not_inspect_unregistered_same_UID_processes(self):
        def query(args):
            if args[0].startswith('--query-gpu'):return [['0','GPU-XXX-0','NVIDIA GPU']]
            return [['GPU-XXX-0','909','/OTHER/bin/python']]
        with patch('gpu.query',side_effect=query),patch('gpu.own_process_ids',return_value={909}),patch('gpu.identity') as identity:
            result=gpu.snapshot({'hold_rules':[]})
            identity.assert_not_called();self.assertFalse(result['gpus'][0]['processes'][0]['hold'])


if __name__=='__main__':unittest.main(verbosity=2)
