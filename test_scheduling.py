"""CPU command concurrency and NVIDIA admission fixtures. No CUDA allocations."""
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from auth import Accounts,Actor,save_session
ADMIN=Actor("fixture","admin")
from queue_store import QueueStore
from scheduling import Admission, DEFAULTS, Dispatcher, policy, running_ids


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = QueueStore(self.root/'data')
        self.tasks = [dict(id=k, name=k, root=str(self.root), gpu_uuid='',
                           argv=[sys.executable, '-c', 'pass']) for k in 'ABCD']
        Accounts(self.store.directory).add("fixture","fixture-password-123","admin")
        for task in self.tasks:task["owner_id"]="fixture"
        self.store.initialize(self.tasks, list('ABCD'))

    def tearDown(self):
        self.temp.cleanup()

    def test_atomic_capacity_shrink_finish_order_cancel_reorder(self):
        self.assertEqual(policy(self.store.read())['window'], 2)
        barrier = threading.Barrier(5)
        results = []
        def claim():
            barrier.wait()
            results.append(self.store.claim())
        threads = [threading.Thread(target=claim) for _ in range(4)]
        for t in threads:t.start()
        barrier.wait()
        for t in threads:t.join()
        self.assertEqual(sum(x is not None for x in results), 2)
        self.store.configure(dict(DEFAULTS, window=1),actor=ADMIN)
        self.assertIsNone(self.store.claim())
        self.assertEqual(running_ids(self.store.read()), list('AB'))
        with self.assertRaises(ValueError):self.store.cancel('A',actor=ADMIN)
        self.store.reorder(list('DC'),actor=ADMIN)
        self.store.cancel('C',actor=ADMIN)
        self.store.finish('B', True)
        self.assertEqual(running_ids(self.store.read()), ['A'])
        self.assertEqual(self.store.read()['running'], 'A')
        self.assertIsNone(self.store.claim())
        self.store.finish('A', True)
        self.assertEqual(self.store.claim()['id'], 'D')
        with self.assertRaises(ValueError):self.store.finish('B', True)

    def test_settings_revision_validation_does_not_touch_jobs(self):
        old = self.store.read()
        self.store.configure(dict(DEFAULTS, window=3), old['revision'],actor=ADMIN)
        new = self.store.read()
        self.assertEqual(new['tasks'], old['tasks'])
        self.assertEqual(new['order'], old['order'])
        with self.assertRaises(ValueError):self.store.configure(DEFAULTS, old['revision'],actor=ADMIN)
        for key, bad in [('window', 0), ('window', True), ('window', 9),
                         ('utilization_limit', 100), ('reserve_mib', 0), ('stable_seconds', float('nan'))]:
            with self.assertRaises(ValueError):self.store.configure(dict(DEFAULTS, **{key:bad}),actor=ADMIN)

    def test_real_two_CPU_commands_overlap_and_shrink_preserves_them(self):
        # Actual foreground processes wait on separate files; both must be live.
        from worker import Controller
        def command(value):
            for key in 'ABCD':
                value['tasks'][key]['argv'] = [sys.executable, '-c',
                    "import pathlib,time,sys;p=pathlib.Path(sys.argv[1]);p.with_suffix('.started').touch();\nwhile not p.exists():time.sleep(.01)", str(self.root/key)]
        self.store.change(command)
        dispatcher = Dispatcher(self.store, lambda task:Controller(self.store, {}),
                                lambda *a,**k:None, Admission(self.store.directory))
        try:
            dispatcher.tick(); dispatcher.tick(); dispatcher.tick()
            limit = time.monotonic()+5
            while not all((self.root/(key+'.started')).exists() for key in 'AB'):
                if time.monotonic()>limit:self.fail('CPU commands did not overlap')
                time.sleep(.02)
            self.store.configure(dict(DEFAULTS, window=1),actor=ADMIN)
            dispatcher.tick()
            self.assertEqual(len(running_ids(self.store.read())), 2)
            (self.root/'B').touch()
            limit = time.monotonic()+5
            while self.store.read()['tasks']['B']['state'] != 'completed':
                dispatcher.tick()
                if time.monotonic()>limit:self.fail('B did not finish independently')
                time.sleep(.01)
            self.assertEqual(running_ids(self.store.read()), ['A'])
            self.assertFalse((self.root/'C.started').exists())
            self.store.reorder(list('DC'),actor=ADMIN);self.store.cancel('C',actor=ADMIN)
        finally:
            for key in 'ABCD':(self.root/key).touch()
            for _, thread, _ in dispatcher.active.values():thread.join(5)
        dispatcher.tick()
        self.assertEqual(self.store.read()['tasks']['C']['state'], 'canceled')
        for _, thread, _ in dispatcher.active.values():thread.join(5)
        dispatcher.tick()

    def test_failure_halts_pickup_while_sibling_finishes(self):
        release = threading.Event()
        class Worker:
            def execute(_, task):
                if task['id']=='A':
                    release.wait(2)
                else:
                    raise RuntimeError('fixture failure')
        d = Dispatcher(self.store, lambda task:Worker(), lambda *a,**k:None, Admission(self.store.directory),stop_on_failure=lambda task:True)
        try:
            d.tick();d.tick()
            deadline=time.monotonic()+2
            while not d.halted:
                d.tick()
                if time.monotonic()>deadline:self.fail('failure not reaped')
                time.sleep(.01)
            self.assertEqual(running_ids(self.store.read()), ['A'])
            self.assertEqual(self.store.read()['order'], list('CD'))
        finally:
            release.set()
            for _, thread, _ in d.active.values():thread.join(3)
        d.tick()
        self.assertEqual(self.store.read()['tasks']['A']['state'], 'completed')
        self.assertEqual(self.store.read()['order'], list('CD'))

    def test_gpu_admission_peak_reservation_unknown_process_and_utilization(self):
        self.store.change(lambda v:[v['tasks'][k].update(gpu_uuid='GPU-XXX', peak_memory_mib=24576) for k in 'ABCD'])
        a=self.store.claim()
        self.assertEqual(a['reserved_peak_memory_mib'],24576)
        self.store.configure(dict(DEFAULTS,default_peak_mib=1024,utilization_limit=85),actor=ADMIN)
        from scheduling import memory_needed
        self.assertEqual(memory_needed(self.store.read()['tasks']['A'],policy(self.store.read())),24576)
        now=time.time()+120
        state=self.store.read();task=state['tasks']['B']
        g=dict(uuid='GPU-XXX', index=0, memory_used_mib=10000., memory_total_mib=81920., utilization=20.)
        inventory=dict(tasks={'A':[dict(pid=10,memory_used_mib=1000)]}, observed=[], error=None)
        processes=[dict(pid=10,hold=False)]
        def snapshot(config,telemetry=False):
            return dict(ok=True,gpus=[dict(g,processes=processes)],error=None)
        admission=Admission(self.store.directory)
        def check():
            admission.samples['GPU-XXX']=[dict(t=now-i,compute=20) for i in range(35,0,-3)]
            return admission.check(task,state,now)
        with patch('scheduling.gpu.snapshot',side_effect=snapshot), patch('scheduling.process_monitor.collect',return_value=inventory):
            self.assertTrue(check()[0])
            # 40 GiB currently used appears to fit a 24 GiB job, but the active
            # job's unallocated 23 GiB reservation makes this unsafe.
            g['memory_used_mib']=40960
            self.assertIn('显存保护',check()[1])
            g['memory_used_mib']=10000
            processes.append(dict(pid=99,hold=False))
            self.assertIn('未纳管',check()[1]);processes.pop()
            g['utilization']=99
            self.assertIn('利用率',check()[1]);g['utilization']=20
            state['tasks']['A']['started']=now-5
            self.assertIn('冷却',check()[1])
            state['tasks']['A']['started']=now-120
            admission.samples={}
            self.assertIn('采样',admission.check(task,state,now)[1])
            g['memory_used_mib']=None
            self.assertIn('不可用',check()[1])

    def test_minus_one_settings_and_unlimited_atomic_window(self):
        for key in DEFAULTS:
            self.store.configure(dict(DEFAULTS, **{key:-1}),actor=ADMIN)
            self.assertEqual(policy(self.store.read())[key], -1)
            for invalid in (-2, 0):
                with self.assertRaises(ValueError):self.store.configure(dict(DEFAULTS, **{key:invalid}),actor=ADMIN)
        self.store.configure(dict(DEFAULTS,window=-1),actor=ADMIN)
        for key in 'ABCD':self.assertEqual(self.store.claim()['id'],key)
        self.assertEqual(len(running_ids(self.store.read())),4)

    def test_each_disabled_gpu_gate_and_memory_budget_sentinel(self):
        self.store.change(lambda v:[v['tasks'][k].update(gpu_uuid='GPU-XXX') for k in 'ABCD'])
        now=time.time()
        g=dict(uuid='GPU-XXX',index=0,memory_used_mib=40000.,memory_total_mib=81920.,utilization=100.)
        processes=[]
        def snapshot(config,telemetry=False):
            return dict(ok=True,gpus=[dict(g,processes=processes)],error=None)
        inventory=dict(tasks={},observed=[],error=None)
        admission=Admission(self.store.directory)
        def check(**settings):
            self.store.configure(dict(DEFAULTS,**settings),actor=ADMIN)
            value=self.store.read()
            return admission.check(value['tasks']['A'],value,now)
        with patch('scheduling.gpu.snapshot',side_effect=snapshot),patch('scheduling.process_monitor.collect',return_value=inventory):
            # Default ignores both 100% utilization and missing utilization data.
            self.assertTrue(check()[0]);g['utilization']=None
            self.assertTrue(check()[0]);g['utilization']=10
            self.assertIn('采样',check(utilization_limit=85)[1])
            self.assertTrue(check(utilization_limit=85,stable_seconds=-1)[0])
            g['utilization']=100
            self.assertIn('利用率',check(utilization_limit=85,stable_seconds=-1)[1])
            # Each memory ceiling acts independently; -1 does not become arithmetic.
            g['memory_used_mib']=55000
            self.assertFalse(check(reserve_mib=-1)[0])
            self.assertFalse(check(memory_limit_percent=-1)[0])
            self.assertTrue(check(default_peak_mib=-1)[0])
            g['memory_used_mib']=None
            self.assertTrue(check(reserve_mib=-1,memory_limit_percent=-1)[0])
            processes.append(dict(pid=999,hold=False))
            self.assertIn('未纳管',check(**{key:-1 for key in DEFAULTS})[1]);processes.clear()
            # A recently finished task still enforces cooldown unless disabled.
            self.store.change(lambda v:v['tasks']['D'].update(state='completed',started=now-1))
            self.assertIn('冷却',check()[1])
            self.assertTrue(check(reserve_mib=-1,memory_limit_percent=-1,launch_interval_seconds=-1)[0])
        self.store.configure(dict(DEFAULTS,default_peak_mib=-1),actor=ADMIN)
        task=self.store.claim()
        self.assertEqual(task['reserved_peak_memory_mib'],-1)
        from scheduling import memory_needed
        self.assertEqual(memory_needed(task,DEFAULTS),-1)


if __name__=='__main__':unittest.main(verbosity=2)
