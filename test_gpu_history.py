"""Rolling history semantics, with real timestamps and no browser involvement."""
import json
import tempfile
import unittest
from pathlib import Path
from gpu_history import update
from queue_store import atomic


class HistoryTests(unittest.TestCase):
    def test_window_boundary_dedup_and_real_values(self):
        history={'gpu':[dict(t=t,compute=20,memory=50) for t in (600,698,701,900)]}
        sample=dict(sampled=1000,gpus=[dict(uuid='gpu',utilization=73,memory_used_mib=25,memory_total_mib=100)])
        result=update(history,sample,1000)
        self.assertEqual([p['t'] for p in result['gpu']],[698,701,900,1000])
        self.assertEqual(result['gpu'][-1],dict(t=1000,compute=73,memory=25))
        self.assertEqual(update(result,sample,1000),result)

    def test_persist_reload_and_expiry_without_new_snapshot(self):
        sample=dict(sampled=1000,gpus=[dict(uuid='gpu',utilization=0,memory_used_mib=0,memory_total_mib=100)])
        result=update({},sample,1000)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'gpu_history.json'
            atomic(path,dict(history=result))
            restored=json.loads(path.read_text())['history']
        self.assertEqual(restored,result)
        self.assertEqual(update(restored,sample,1301),{})


if __name__=='__main__':unittest.main(verbosity=2)
