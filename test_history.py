"""Rolling retention, command-state preservation and NVIDIA-only process reads."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import gpu_history as history
import tmux_dispatch as app


class HistoryTests(unittest.TestCase):
    def test_exact_five_minute_window_dedup_and_expiry(self):
        previous={'gpu':[dict(t=t,compute=20,memory=50) for t in (699,700,900,1001)]}
        info=dict(devices=[dict(uuid='gpu',used=25,total=100,util=73)])
        result=history.update(previous,info,1000)
        self.assertEqual([p['t'] for p in result['gpu']],[700,900,1000])
        self.assertEqual(result['gpu'][-1],dict(t=1000,compute=73,memory=25))
        self.assertEqual(history.update(result,info,1000),result)
        self.assertEqual(history.prune(result,1301),{})

    def test_retention_is_bounded_and_handles_missing_values(self):
        info=dict(devices=[dict(uuid='gpu',used=None,total=100,util=None)])
        result={}
        for now in range(0,1000,2):result=history.update(result,info,now)
        self.assertLessEqual(len(result['gpu']),151)
        self.assertIsNone(result['gpu'][-1]['memory'])
        self.assertTrue(all(p['t']>=698 for p in result['gpu']))

    def test_compute_graphics_and_mixed_processes_are_all_read_only(self):
        xml='''<nvidia_smi_log><gpu><uuid>GPU-fixture</uuid><processes>
        <process_info><pid>123</pid><type>C</type><process_name>/other/env/bin/python</process_name><used_memory>100 MiB</used_memory></process_info>
        <process_info><pid>456</pid><type>G</type><process_name>display</process_name><used_memory>N/A</used_memory></process_info>
        <process_info><pid>789</pid><type>C+G</type><process_name>mixed</process_name><used_memory>42 MiB</used_memory></process_info>
        </processes></gpu></nvidia_smi_log>'''
        with patch('tmux_dispatch.identity',side_effect=AssertionError('foreign /proc inspected')):
            rows=history.process_rows(xml)
        self.assertEqual([r['type'] for r in rows],['C','G','C+G'])
        self.assertTrue(all(r['editable'] is False for r in rows))
        self.assertIsNone(rows[1]['memory'])

    def test_sparkline_places_current_sample_at_right_edge(self):
        result=history.sparkline([dict(t=1000,compute=100)],'compute',1000)
        self.assertEqual(len(result),60)
        self.assertEqual(result[-1],'@')

    @unittest.skipUnless(os.name=='posix','Linux state locks')
    def test_browser_reuses_server_cache_and_labels_foreign_rows(self):
        import web_bridge
        with tempfile.TemporaryDirectory() as root:
            store=app.State(Path(root)/'current.json');store.initialize(root,'fixture')
            info=dict(devices=[],processes=[dict(gpu='gpu',pid=999999,name='foreign',memory=5,type='G')],error=None)
            history.persist(store,info,1000,None)
            with patch('web_bridge.time.time',return_value=1001), \
                 patch('tmux_dispatch.inventory',side_effect=AssertionError('cache ignored')), \
                 patch('tmux_dispatch.identity',side_effect=AssertionError('foreign /proc inspected')):
                result=web_bridge.snapshot(store)
            self.assertFalse(result['inventory']['processes'][0]['managed'])
            self.assertFalse(result['inventory']['processes'][0]['editable'])
            self.assertEqual(result['inventory']['processes'][0]['type'],'G')

    @unittest.skipUnless(os.name=='posix','Linux state locks')
    def test_sampling_preserves_commands_revision_limits_and_single_json(self):
        with tempfile.TemporaryDirectory() as root:
            store=app.State(Path(root)/'current.json');store.initialize(root,'fixture')
            key=app.add(store,'true',None)
            app.edit(store,key,dict(peak_mib=4096))
            before=store.read()
            info=dict(devices=[dict(uuid='gpu',used=10,total=100,util=20)],processes=[],error=None)
            history.persist(store,info,1000,None)
            after=store.read()
            self.assertEqual(after['revision'],before['revision'])
            self.assertEqual(after['commands'],before['commands'])
            self.assertEqual(after['limits'],before['limits'])
            self.assertEqual(list(Path(root).glob('*.json')),[store.path])
            app.edit(store,key,dict(peak_mib=8192))
            history.persist(store,info,1301,None)
            final=store.read()
            self.assertEqual(app.command(final,key)['peak_mib'],8192)
            self.assertEqual(len(final['telemetry']['history']['gpu']),1)


if __name__=='__main__':unittest.main(verbosity=2)
