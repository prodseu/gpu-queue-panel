"""Isolated CPU/tmux regressions. Never submits a training or touches a real session."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import tmux_dispatch as app


def gpu_info(used=0, util=0):
    return dict(error=None, processes=[], devices=[dict(index=i, uuid='GPU-'+str(i),
                name='fixture', used=used, total=32768, util=util) for i in (0,1)])


@unittest.skipUnless(os.name == 'posix', 'Linux file locks and tmux')
class Commands(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = app.State(self.root/'current.json')
        self.store.initialize(self.root, 'fixture')

    def tearDown(self):
        self.temp.cleanup()

    def save(self, text='true', gpu=None, peak=1024):
        return app.add(self.store, text, gpu, peak)

    def test_single_json_and_constant_lock_size(self):
        key = self.save()
        for _ in range(30):
            app.edit(self.store, key, dict(peak_mib=2048))
        self.assertEqual(list(self.root.glob('*.json')), [self.root/'current.json'])
        self.assertEqual((self.root/'current.lock').stat().st_size, 0)
        self.assertEqual(app.command(self.store.read(),key)['peak_mib'],2048)

    def test_cancel_reorder_and_gpu_move_are_independent(self):
        a,b,c = [self.save() for _ in range(3)]
        app.move(self.store,c,-1)
        app.edit(self.store,b,dict(state='canceled'))
        app.edit(self.store,a,dict(gpu='GPU-1'))
        value=self.store.read()
        self.assertEqual([j['id'] for j in value['commands']],[a,c,b])
        self.assertEqual(app.command(value,b)['state'],'canceled')
        self.assertEqual(app.command(value,c)['state'],'pending')
        self.assertEqual(app.command(value,a)['gpu'],'GPU-1')

    def test_manual_pause_and_mode_revoke_requests(self):
        key=self.save()
        self.assertIn('Waiting',app.dispatch(self.store,gpu_info()))
        app.request_start(self.store,key)
        app.toggle(self.store,'paused')
        self.assertFalse(app.command(self.store.read(),key)['requested'])
        self.assertEqual(app.dispatch(self.store,gpu_info()),'Dispatch paused')
        with self.assertRaises(ValueError):app.request_start(self.store,key)
        app.toggle(self.store,'paused')
        app.request_start(self.store,key)
        app.toggle(self.store,'mode')
        self.assertFalse(app.command(self.store.read(),key)['requested'])

    def test_auto_can_dispatch_other_card_while_first_is_blocked(self):
        a=self.save(gpu='GPU-0',peak=40000)
        b=self.save(gpu='GPU-1')
        app.toggle(self.store,'mode')
        with patch('tmux_dispatch.deliver',return_value='%fixture') as deliver:
            app.dispatch(self.store,gpu_info())
        self.assertEqual(deliver.call_args.args[1]['id'],b)
        self.assertEqual(app.command(self.store.read(),a)['state'],'pending')

    def test_disabled_limits_and_individual_guards(self):
        key=self.save(gpu='GPU-0',peak=-1)
        app.configure(self.store,dict(window=-1,reserve_mib=-1,memory_percent=-1,utilization=-1))
        value=self.store.read();item=app.command(value,key)
        self.assertEqual(app.resource_reason(item,value,gpu_info(32768,100)),'')
        value['limits']['reserve_mib']=8192
        self.assertIn('memory',app.resource_reason(item,value,gpu_info(32768)))
        value['limits']['reserve_mib']=-1;value['limits']['memory_percent']=90
        self.assertIn('memory',app.resource_reason(item,value,gpu_info(32768)))
        value['limits']['memory_percent']=-1;value['limits']['utilization']=50
        self.assertIn('compute',app.resource_reason(item,value,gpu_info(util=60)))
        for values in (dict(window=0),dict(memory_percent=101),dict(reserve_mib=-2)):
            with self.assertRaises(ValueError):app.configure(self.store,values)

    def test_window_is_per_card_and_outstanding_memory_is_reserved(self):
        a=self.save(gpu='GPU-0',peak=16000)
        b=self.save(gpu='GPU-0',peak=16000)
        c=self.save(gpu='GPU-1')
        value=self.store.read();app.command(value,a)['state']='running'
        self.assertIn('window',app.resource_reason(app.command(value,b),value,gpu_info()))
        self.assertEqual(app.resource_reason(app.command(value,c),value,gpu_info()),'')
        value['limits']['window']=2
        self.assertIn('memory',app.resource_reason(app.command(value,b),value,gpu_info()))

    def test_dispatch_failure_is_terminal_and_not_retried(self):
        key=self.save();app.request_start(self.store,key)
        with patch('tmux_dispatch.deliver',side_effect=OSError('tmux failed')) as deliver:
            app.dispatch(self.store,gpu_info());app.dispatch(self.store,gpu_info())
        self.assertEqual(deliver.call_count,1)
        self.assertEqual(app.command(self.store.read(),key)['state'],'interrupted')

    def test_lost_runner_does_not_replay_command(self):
        key=self.save()
        self.store.change(lambda v: app.command(v,key).update(state='running',runner=None,child=None))
        app.reconcile(self.store)
        self.assertEqual(app.command(self.store.read(),key)['state'],'interrupted')
        with patch('tmux_dispatch.deliver') as deliver:app.dispatch(self.store,gpu_info())
        deliver.assert_not_called()

    def test_duplicate_nonce_cannot_execute_again(self):
        key=self.save()
        self.store.change(lambda v: app.command(v,key).update(state='launching',nonce='once',dispatched=time.time()))
        self.assertEqual(app.worker(self.store,key,'once'),0)
        with self.assertRaises(ValueError):app.worker(self.store,key,'once')

    def test_corrupt_current_json_is_preserved(self):
        self.store.path.write_bytes(b'\x00'*64)
        with self.assertRaises(ValueError):self.store.read()
        self.assertEqual(self.store.path.read_bytes(),b'\x00'*64)

    def test_concurrent_saves_and_dispatch_only_deliver_once(self):
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            ids=list(pool.map(lambda _:self.save(),range(8)))
        self.assertEqual(len(self.store.read()['commands']),8)
        app.request_start(self.store,ids[0])
        with patch('tmux_dispatch.deliver',return_value='%fixture') as deliver:
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda _:app.dispatch(self.store,gpu_info()),range(8)))
        self.assertEqual(deliver.call_count,1)

    def test_foreign_nvidia_pids_are_not_read_from_proc(self):
        key=self.save(gpu='GPU-0')
        info=gpu_info();info['processes']=[dict(gpu='GPU-0',pid=999999999,name='foreign',memory=2000)]
        with patch('tmux_dispatch.identity',side_effect=AssertionError('foreign proc inspected')):
            self.assertEqual(app.resource_reason(app.command(self.store.read(),key),self.store.read(),info),'')


@unittest.skipUnless(os.name == 'posix', 'real Linux tmux')
class TmuxCommands(Commands):
    def setUp(self):
        super().setUp()
        self.session='gqp-test-'+str(os.getpid())+'-'+str(time.time_ns())
        self.store.change(lambda v:v.update(session=self.session))
        app.tmux('new-session','-d','-s',self.session,'-c',str(self.root),'/bin/bash --noprofile --norc')

    def tearDown(self):
        app.tmux('kill-session','-t','='+self.session)
        super().tearDown()

    def wait_finished(self,key):
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            item=app.command(self.store.read(),key)
            if item['state'] in app.FINISHED:return item
            time.sleep(.05)
        self.fail('CPU fixture did not finish')

    def test_new_window_literal_command_cwd_gpu_and_no_repeat(self):
        script="import os,pathlib;pathlib.Path('receipt').write_text(os.getcwd()+'|'+os.environ['CUDA_VISIBLE_DEVICES'])"
        key=self.save(shlex.join([sys.executable,'-c',script]),gpu='GPU-0')
        app.request_start(self.store,key)
        app.dispatch(self.store,gpu_info())
        self.assertEqual(self.wait_finished(key)['exit_code'],0)
        self.assertEqual((self.root/'receipt').read_text(),str(self.root)+'|GPU-0')
        self.assertIn('train-'+key,app.tmux('list-windows','-t','='+self.session,'-F','#{window_name}'))
        self.assertIn('Waiting',app.dispatch(self.store,gpu_info()))

    def test_fixed_terminal_and_failure_does_not_block_late_command(self):
        pane=app.new_shell(self.store)
        key=app.add(self.store,'exit 7',None,target=pane)
        app.request_start(self.store,key);app.dispatch(self.store,gpu_info())
        self.assertEqual(self.wait_finished(key)['exit_code'],7)
        time.sleep(.1)
        late=app.add(self.store,'printf ok > late',None,target=pane)
        app.request_start(self.store,late);app.dispatch(self.store,gpu_info())
        self.assertEqual(self.wait_finished(late)['exit_code'],0)
        self.assertEqual((self.root/'late').read_text(),'ok')

    def test_fixed_terminal_busy_is_not_interrupted(self):
        pane=app.new_shell(self.store)
        app.tmux('send-keys','-t',pane,'sleep 2','Enter')
        time.sleep(.1)
        key=app.add(self.store,'printf wrong > bad',None,target=pane)
        app.request_start(self.store,key);app.dispatch(self.store,gpu_info())
        self.assertEqual(app.command(self.store.read(),key)['state'],'interrupted')
        self.assertFalse((self.root/'bad').exists())
        time.sleep(2)

    def test_start_opens_terminal_monitor_and_duplicate_is_rejected(self):
        app.start(self.store)
        deadline=time.monotonic()+8
        while not app.alive(self.store.read().get('monitor')) and time.monotonic()<deadline:time.sleep(.05)
        self.assertTrue(app.alive(self.store.read()['monitor']))
        self.assertNotEqual(self.store.read()['default_target'],'new')
        key=self.save()
        self.assertEqual(app.command(self.store.read(),key)['target'],self.store.read()['default_target'])
        before=app.tmux('list-windows','-t','='+self.session,'-F','#{window_id}')
        app.start(self.store)
        self.assertEqual(before,app.tmux('list-windows','-t','='+self.session,'-F','#{window_id}'))
        pane=app.tmux('list-panes','-t',self.session+':gpu-monitor','-F','#{pane_id}')
        app.tmux('send-keys','-t',pane,'q')


if __name__ == '__main__':unittest.main(verbosity=2)
