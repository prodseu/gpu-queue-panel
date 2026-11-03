"""Optional Playwright checks against an isolated real QueueStore, no SSH/GPU calls."""
import argparse
import json
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path

parser=argparse.ArgumentParser()
parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parent)
parser.add_argument('--out',type=Path)
args=parser.parse_args();ROOT=args.root.resolve();sys.path.insert(0,str(ROOT))
from queue_store import QueueStore,atomic
from server_agent import rpc
import process_monitor
from playwright.sync_api import sync_playwright


def check():
    with tempfile.TemporaryDirectory() as directory:
        data=Path(directory);store=QueueStore(data)
        devices=[dict(index=i,uuid='GPU-XXX-'+str(i),name='NVIDIA GPU '+str(i),memory_used_mib=8000.,
            memory_total_mib=24000.,utilization=50.) for i in range(3)]
        specs=[('R',0),('A',0),('B',1),('C',0),('D',1),('CPU',None)]
        tasks=[dict(id=k,name='Task '+k,root=str(data/k),gpu=g,gpu_uuid=devices[g]['uuid'] if g is not None and k!='A' else None,
            epochs=20,updates=100) for k,g in specs]
        store.initialize(tasks,['A','B','C','D','CPU'],running='R')
        atomic(data/'R/run/STATUS.json',dict(epoch=5,updates=25))
        def snapshot(gpus):atomic(data/'snapshot.json',dict(gpus=gpus,sampled=time.time(),controller='Isolated UI fixture'))
        snapshot(devices)
        now=time.time();atomic(data/'gpu_history.json',dict(updated=now,history={g['uuid']:[dict(t=now-300+i*3,compute=50.,memory=33.) for i in range(101)] for g in devices}))
        def fake_inventory(state,gpus,directory):
            available={g['uuid']:g for g in gpus}
            observed=[dict(id='observed:fixture:'+str(i),state='observed',read_only=True,owned=False,pid=900+i,
                name='python',python='/XXX/python',memory_used_mib=2048.,gpu=i,gpu_uuid='GPU-XXX-'+str(i))
                for i in [0,1] if 'GPU-XXX-'+str(i) in available]
            return dict(tasks={'R':[dict(pid=123,python='/XXX/bin/python',memory_used_mib=1024)]},observed=observed,error=None)
        process_monitor.collect=fake_inventory
        requests=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*a):pass
            def do_GET(self):
                name={'/':'index.html','/app.js':'app.js','/style.css':'style.css'}.get(self.path)
                if not name:self.send_error(404);return
                body=(ROOT/'web'/name).read_bytes();self.send_response(200)
                self.send_header('Content-Type',{'html':'text/html; charset=utf-8','js':'text/javascript; charset=utf-8','css':'text/css'}[name.rsplit('.',1)[1]])
                self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            def do_POST(self):
                request=json.loads(self.rfile.read(int(self.headers['Content-Length'])));requests.append(request)
                try:body=json.dumps(rpc(request,store)).encode();code=200
                except ValueError as e:body=json.dumps(dict(error=str(e))).encode();code=409
                self.send_response(code);self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        http=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
        try:
            with sync_playwright() as p:
                browser=p.chromium.launch(headless=True)
                page=browser.new_page(viewport=dict(width=1440,height=1200));errors=[]
                page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(f'http://127.0.0.1:{http.server_port}/')
                page.wait_for_selector('.task')
                assert page.locator('.gpu').count()==3
                assert page.locator('.task[data-managed]').count()==6
                assert page.locator('.task.read-only').count()==2
                assert page.locator('.task.read-only button').count()==0
                assert page.locator('.task.read-only[draggable=true]').count()==0
                assert all(t=='不可操作' for t in page.locator('.read-only-badge').all_text_contents())
                assert page.locator('.task.running button').count()==0
                assert page.locator('input[name=window]').input_value()=='2'
                before_settings=store.read()
                page.locator('input[name=window]').fill('3')
                page.evaluate('refresh()')
                assert page.locator('input[name=window]').input_value()=='3'
                page.locator('#scheduling-form button[type=submit]').click()
                page.wait_for_function("document.querySelector('#scheduler-usage').textContent.includes('窗口 3')")
                assert store.read()['tasks']==before_settings['tasks']
                assert store.read()['order']==before_settings['order']
                page.reload();page.wait_for_selector('.task')
                assert page.locator('input[name=window]').input_value()=='3'
                page.locator('input[name=window]').fill('1')
                page.locator('#scheduling-form button[type=submit]').click()
                page.wait_for_function("document.querySelector('#scheduler-usage').textContent.includes('窗口 1')")
                assert store.read()['running']=='R'
                assert page.locator('.task.running').count()==1
                page.locator('[data-gpu-select="GPU-XXX-0"]').click()
                assert page.locator('.task[data-managed]').count()==3
                assert page.locator('.task.read-only').count()==1
                assert page.locator('#queue-title').inner_text()=='GPU 0 · 我的队列'
                page.locator('[data-move="-1"][data-id="C"]').click()
                page.wait_for_function("document.querySelector('.task[draggable=true]').dataset.id==='C'")
                assert store.read()['order']==['C','B','A','D','CPU']
                page.locator('.task[data-id="A"]').drag_to(page.locator('.task[data-id="C"]'))
                page.wait_for_function("document.querySelector('.task[draggable=true]').dataset.id==='A'")
                assert store.read()['order']==['A','B','C','D','CPU']
                page.locator('[data-gpu-select="GPU-XXX-1"]').click()
                assert page.locator('.task[data-managed]').count()==2
                page.locator('[data-cancel="B"]').click()
                page.locator('.task[data-id="B"]').wait_for(state='detached')
                assert store.read()['order']==['A','C','D','CPU'] and store.read()['running']=='R'
                page.reload();page.wait_for_selector('.task')
                assert page.locator('#queue-title').inner_text()=='GPU 1 · 我的队列'
                page.locator('[data-gpu-select="GPU-XXX-2"]').focus();page.keyboard.press('Enter')
                assert page.locator('.task').count()==0 and '没有你的' in page.locator('#tasks').inner_text()
                page.locator('#all-queues').click()
                assert page.locator('.task[data-managed]').count()==5 and 'CPU' in page.locator('#tasks').inner_text()
                assert page.locator('canvas.gpu-chart').count()==3
                assert all(abs(v-300)<.001 for v in page.locator('canvas').evaluate_all('nodes=>nodes.map(n=>Number(n.dataset.windowEnd)-Number(n.dataset.windowStart))'))
                if args.out:
                    args.out.mkdir(parents=True,exist_ok=True);page.screenshot(path=str(args.out/'ALL_QUEUE_FIXTURE.png'),full_page=True)
                page.locator('[data-gpu-select="GPU-XXX-1"]').click()
                changed=[dict(devices[1],index=7),devices[2]];snapshot(changed)
                page.evaluate('refresh()');page.wait_for_function("document.querySelector('#queue-title').textContent==='GPU 7 · 我的队列'")
                assert page.locator('.task[data-managed]').count()==1 and page.locator('.task[data-managed] .gpu-cell').inner_text()=='GPU 7'
                snapshot([]);page.evaluate('refresh()');page.wait_for_function("document.querySelector('#queue-title').textContent==='总队列'")
                assert page.locator('.task').count()==5 and '暂未检测到' in page.locator('#gpus').inner_text()
                from scheduling import DEFAULTS
                store.configure(dict(DEFAULTS,window=2))
                assert store.claim()['id']=='A'
                page.evaluate('refresh()')
                page.wait_for_function("document.querySelectorAll('.task.running').length===2")
                assert page.locator('.task.running button').count()==0
                page.locator('input[name=window]').fill('1')
                page.locator('#scheduling-form button[type=submit]').click()
                page.wait_for_function("document.querySelector('#scheduler-usage').textContent.includes('运行 2 / 窗口 1')")
                assert store.read()['tasks']['R']['state']==store.read()['tasks']['A']['state']=='running'
                for key in DEFAULTS:
                    page.locator(f'input[name={key}]').fill('-1')
                page.locator('#scheduling-form button[type=submit]').click()
                page.wait_for_function("document.querySelector('#scheduler-usage').textContent.includes('窗口 不限制')")
                assert all(n==-1 for n in store.read()['metadata']['scheduling'].values())
                page.reload();page.wait_for_selector('.task')
                for key in DEFAULTS:
                    assert page.locator(f'input[name={key}]').input_value()=='-1'
                assert page.locator('.task.running').count()==2
                assert not errors,errors
                browser.close()
        finally:http.shutdown();http.server_close();thread.join()
        result=dict(passed=True,auto_discovery_varies_device_count=True,per_GPU_filter=True,total_includes_CPU=True,
            per_GPU_move_preserves_other_slots=True,per_GPU_drag_preserves_other_slots=True,
            cancel_preserves_siblings=True,legacy_index_fallback=True,UUID_survives_index_change=True,
            empty_GPU_view=True,no_GPU_fallback=True,reload_keeps_view=True,keyboard_selection=True,
            history_window_seconds=300,production_mutations=0,page_errors=errors)
        result.update(observed_rows_readonly=True,observed_not_draggable=True,observed_not_in_reorder=True)
        result.update(scheduling_save_reload=True,dirty_fields_survive_refresh=True,shrink_keeps_running=True,two_running_rows=True)
        result.update(all_minus_one_roundtrip=True,minus_one_memory_unit_preserved=True)
        if args.out:(args.out/'GPU_VIEWS_CHECK.json').write_text(json.dumps(result,indent=2))
        print(json.dumps(result))


if __name__=='__main__':check()
