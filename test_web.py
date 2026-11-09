"""Shared JSON, revision/terminal boundaries, and loopback HTTP checks."""
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import tmux_dispatch as app
import web_bridge
import web_view


@unittest.skipUnless(os.name == 'posix', 'Linux JSON file locks')
class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = app.State(self.root/'current.json')
        self.store.initialize(self.root, 'fixture')
        self.inventory = patch('tmux_dispatch.inventory',return_value=dict(devices=[],processes=[],error=None))
        self.inventory.start()
        self.sample = patch('gpu_history.sample',return_value=dict(devices=[],processes=[],error=None))
        self.sample.start()

    def tearDown(self):
        self.inventory.stop()
        self.sample.stop()
        self.temp.cleanup()

    def request(self, action, **values):
        return web_bridge.rpc(self.store.path, dict(action=action,revision=self.store.read()['revision'],**values))

    def add(self):
        return self.request('add',command='printf fixture',gpu='cpu',peak_mib=4096,target='new')['result']

    def test_browser_and_terminal_share_commands_controls_and_limits(self):
        key=self.add()
        self.assertEqual(app.command(self.store.read(),key)['state'],'pending')
        app.edit(self.store,key,dict(command='printf changed'))
        self.assertEqual(self.request('state')['commands'][0]['command'],'printf changed')
        self.request('control',mode='auto',paused=True)
        self.assertEqual(self.store.read()['mode'],'auto')
        self.assertTrue(self.store.read()['paused'])
        self.request('limits',values=dict(window=2,utilization=-1,memory_percent=-1,reserve_mib=-1))
        self.assertEqual(self.store.read()['limits']['window'],2)
        self.assertEqual(list(self.root.glob('*.json')),[self.store.path])

    def test_stale_browser_revision_cannot_overwrite_terminal_edit(self):
        key=self.add();revision=self.store.read()['revision']
        app.edit(self.store,key,dict(command='terminal update'))
        with self.assertRaises(ValueError):
            web_bridge.rpc(self.store.path,dict(action='edit',id=key,revision=revision,values=dict(command='stale')))
        self.assertEqual(app.command(self.store.read(),key)['command'],'terminal update')

    def test_setting_current_mode_does_not_revoke_manual_request(self):
        key=self.add()
        app.request_start(self.store,key)
        self.request('control',mode='manual')
        self.assertTrue(app.command(self.store.read(),key)['requested'])

    def test_stale_new_terminal_has_no_tmux_side_effect(self):
        self.add()
        with patch('tmux_dispatch.create_terminal') as create:
            with self.assertRaises(ValueError):
                web_bridge.rpc(self.store.path,dict(action='new_shell',revision=0))
        create.assert_not_called()

    def test_cancel_one_reorder_and_running_edit_rejected(self):
        a=self.add();b=self.add()
        self.request('move',id=b,offset=-1)
        self.assertEqual(self.store.read()['commands'][0]['id'],b)
        self.request('cancel',id=a)
        self.assertEqual(app.command(self.store.read(),b)['state'],'pending')
        self.store.change(lambda value:app.command(value,b).update(state='running'))
        with self.assertRaises(ValueError):self.request('edit',id=b,values=dict(peak_mib=1234))
        with self.assertRaises(ValueError):self.request('edit',id=b,values=dict(state='pending'))

    def test_terminal_output_requires_tool_created_pane(self):
        with patch('tmux_dispatch.tmux') as tmux:
            with self.assertRaises(ValueError):self.request('terminal',pane='%foreign')
        tmux.assert_not_called()
        self.store.change(lambda value:value['terminals'].append(dict(pane='%fixture',shell=None)))
        with patch('tmux_dispatch.tmux',return_value='output'):
            self.assertEqual(self.request('terminal',pane='%fixture')['text'],'output')


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.calls=[]
        calls=self.calls
        class Client:
            def request(self, request):
                calls.append(request)
                return dict(result='ok')
        self.server=web_view.create_server(Client(),0)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port)

    def tearDown(self):
        self.connection.close();self.server.shutdown();self.server.server_close();self.thread.join()

    def test_same_origin_session_required_for_api(self):
        self.connection.request('GET','/api/session')
        response=self.connection.getresponse()
        cookie=response.getheader('Set-Cookie').split(';')[0]
        token=json.loads(response.read())['csrf']
        self.assertIn('HttpOnly',response.getheader('Set-Cookie'))
        body=json.dumps(dict(action='state'))
        self.connection.request('POST','/api',body,{'Content-Type':'application/json'})
        response=self.connection.getresponse();self.assertEqual(response.status,403);response.read()
        headers={'Content-Type':'application/json','Cookie':cookie,'X-CSRF-Token':token,'Origin':'https://foreign.invalid'}
        self.connection.request('POST','/api',body,headers)
        response=self.connection.getresponse();self.assertEqual(response.status,403);response.read()
        del headers['Origin']
        self.connection.request('POST','/api',body,headers)
        response=self.connection.getresponse();self.assertEqual(response.status,200);response.read()
        self.assertEqual(len(self.calls),1)

    def test_foreign_host_is_rejected_and_listener_is_loopback(self):
        self.connection.request('GET','/health',headers={'Host':'foreign.invalid'})
        response=self.connection.getresponse();self.assertEqual(response.status,403);response.read()
        self.assertEqual(self.server.server_address[0],'127.0.0.1')


if __name__ == '__main__':unittest.main(verbosity=2)
