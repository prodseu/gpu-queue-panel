"""Real browser + HTTP/RPC/QueueStore, temporary data, no SSH or GPU calls."""
import argparse,json,tempfile,threading,time
from pathlib import Path
from unittest.mock import patch
from auth import Accounts
from local_view import create_server
from queue_store import QueueStore,atomic
from server_agent import rpc


def check(out=None):
    from playwright.sync_api import sync_playwright,expect
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);store=QueueStore(root/'data');accounts=Accounts(store.directory)
        for user,role in [('alice','user'),('bob','user'),('admin','admin')]:accounts.add(user,'fixture-password-123',role)
        devices=[dict(index=i,uuid='GPU-XXX-'+str(i),name='NVIDIA GPU '+str(i),memory_used_mib=8000.,memory_total_mib=24000.,utilization=50.) for i in range(2)]
        specs=[('A1','alice',0),('B1','bob',0),('A2','alice',1),('B2','bob',1),('A3','alice',0)]
        tasks=[dict(id=k,name='Task '+k,owner_id=u,root=str(root/k),gpu=g,gpu_uuid=devices[g]['uuid'],epochs=20,updates=100) for k,u,g in specs]
        store.initialize(tasks,[t['id'] for t in tasks]);now=time.time()
        atomic(store.directory/'snapshot.json',dict(gpus=devices,sampled=now,controller='isolated fixture'))
        atomic(store.directory/'gpu_history.json',dict(updated=now,history={g['uuid']:[dict(t=now-300+i*3,compute=50.,memory=33.) for i in range(101)] for g in devices}))
        def inventory(scoped,gpus,directory,include_trusted=True):
            return dict(tasks={},observed=[dict(id='observed:fixture:900',name='python',python='/XXX/bin/python',state='observed',read_only=True,pid=900,memory_used_mib=2048.,gpu=0,gpu_uuid='GPU-XXX-0')],error=None)
        class Client:
            def request(self,value):return rpc(value,store)
        errors=[]
        with patch('process_monitor.collect',inventory),patch('gpu_transfer.gpu.snapshot',return_value=dict(ok=True,gpus=devices)):
            server=create_server(Client(),0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                with sync_playwright() as p:
                    browser=p.chromium.launch(headless=True)
                    contexts=[browser.new_context(viewport=dict(width=1440,height=1000)) for _ in range(3)]
                    alice,bob,admin=[c.new_page() for c in contexts];url=f'http://127.0.0.1:{server.server_port}/'
                    def login(page,user):
                        page.on('pageerror',lambda e:errors.append(str(e)))
                        page.goto(url);expect(page.locator('#login-panel')).to_be_visible();expect(page.locator('#app')).to_be_hidden()
                        page.locator('#login-username').fill(user);page.locator('#login-password').fill('fixture-password-123')
                        page.locator('#login-submit').click();expect(page.locator('#user-label')).to_contain_text(user)
                    for page,user in [(alice,'alice'),(bob,'bob'),(admin,'admin')]:login(page,user)
                    expect(alice.locator('.task[data-managed]')).to_have_count(3);expect(bob.locator('.task[data-managed]')).to_have_count(2)
                    expect(admin.locator('.task[data-managed]')).to_have_count(5);expect(alice.locator('.task[data-id=B1]')).to_have_count(0)
                    expect(bob.locator('.task[data-id=A1]')).to_have_count(0);expect(alice.locator('.task').first).to_have_class('task read-only')
                    expect(alice.locator('.read-only button')).to_have_count(0);expect(alice.locator('input[name=window]')).to_be_disabled()
                    expect(admin.locator('input[name=window]')).to_be_enabled();expect(alice.locator('.gpu')).to_have_count(2)
                    expect(alice.locator('canvas.gpu-chart')).to_have_count(2)
                    assert alice.evaluate('Object.values(gpuHistory).every(v=>v.length===101)')
                    assert alice.evaluate('Object.keys(localStorage).length')==0
                    assert alice.evaluate('Object.keys(sessionStorage).every(k=>k==="gpu-queue-view")')
                    cookies=contexts[0].cookies();assert len(cookies)==1 and cookies[0]['httpOnly'] and cookies[0]['sameSite']=='Strict'
                    assert 'gqp_rc_session' not in alice.evaluate('document.cookie')
                    alice.reload();expect(alice.locator('#user-label')).to_contain_text('alice')
                    alice.locator('[data-id=A1][data-move="1"]').click()
                    expect(alice.locator('.task[data-managed]').first).to_have_attribute('data-id','A2')
                    assert store.read()['order']==['A2','B1','A1','B2','A3']
                    alice.locator('[data-gpu-select="GPU-XXX-0"]').click();expect(alice.locator('.task[data-managed]')).to_have_count(2)
                    alice.locator('[data-id=A1][data-move="1"]').click()
                    expect(alice.locator('.task[data-managed]').first).to_have_attribute('data-id','A3')
                    assert store.read()['order']==['A2','B1','A3','B2','A1']
                    alice.locator('[data-gpu-target=A1]').select_option('GPU-XXX-1')
                    expect(alice.locator('.task[data-managed]')).to_have_count(1)
                    assert store.read()['tasks']['A1']['gpu_uuid']=='GPU-XXX-1'
                    assert store.read()['order']==['A2','B1','A3','B2','A1']
                    revision=store.read()['revision']
                    denied=alice.evaluate('''async revision=>{const me=await(await fetch('/api/me')).json();return(await fetch('/api/queue',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':me.csrf},body:JSON.stringify({action:'cancel',id:'B1',revision,owner_id:'alice'})})).status;}''',revision)
                    assert denied==403
                    bob.reload();expect(bob.locator('[data-cancel=B1]')).to_be_visible();bob.locator('[data-cancel=B1]').click()
                    expect(bob.locator('.task[data-managed]')).to_have_count(1);assert store.read()['tasks']['B1']['state']=='canceled'
                    admin.reload();expect(admin.locator('#user-label')).to_contain_text('admin')
                    admin.locator('input[name=window]').fill('3');admin.locator('#scheduling-form button[type=submit]').click()
                    expect(admin.locator('#notice')).to_contain_text('设置已保存');assert store.read()['metadata']['scheduling']['window']==3
                    if out:
                        Path(out).mkdir(parents=True,exist_ok=True)
                        alice.screenshot(path=str(Path(out)/'alice.png'),full_page=True);admin.screenshot(path=str(Path(out)/'admin.png'),full_page=True)
                    alice.locator('#logout').click();expect(alice.locator('#login-panel')).to_be_visible()
                    expect(alice.locator('#app')).to_be_hidden();expect(alice.locator('#tasks')).to_be_empty()
                    bob.reload();expect(bob.locator('#user-label')).to_contain_text('bob')
                    assert not errors,errors
                    browser.close()
            finally:server.shutdown();server.server_close();thread.join(2)
    return dict(passed=True,browser='Chromium',contexts=3,javascript_errors=errors,checks=['login/logout/reload','owner filtering','readonly pinned first','GPU views/5min cache','reorder preserves foreign slots','independent cancel','admin settings','HttpOnly/SameSite/no JS token storage','cross-user denial'])

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path)
    print(json.dumps(check(p.parse_args().out),ensure_ascii=False,indent=2))
