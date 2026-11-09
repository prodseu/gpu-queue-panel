"""SSH stdio adapter for the same terminal commands and current.json."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

import tmux_dispatch as app
import gpu_history


def snapshot(store):
    state = store.read()
    now = time.time()
    telemetry = state.get('telemetry', {})
    info = telemetry.get('inventory') if 0 <= now-telemetry.get('sampled', 0) <= 8 else None
    if info is None:
        info = gpu_history.sample()
        now = time.time()
    managed = set()
    for item in state['commands']:
        if item['state'] in app.ACTIVE:
            managed.update(app.descendants(item.get('child')))
    info = dict(info, processes=[dict(row, managed=row['pid'] in managed, editable=False)
                                for row in info['processes']])
    commands = [{key: value for key, value in item.items() if key not in ('nonce', 'runner', 'child')}
                for item in state['commands']]
    return dict(version=app.VERSION, revision=state['revision'], session=state['session'],
                root=state['root'], mode=state['mode'], paused=state['paused'], limits=state['limits'],
                commands=commands, terminals=[item['pane'] for item in state['terminals']],
                default_target=state['default_target'], monitor_running=app.alive(state.get('monitor')),
                inventory=info, server_time=now, sampled=telemetry.get('sampled'),
                history=gpu_history.prune(telemetry.get('history', {}), now), history_window_seconds=300,
                sampler_running=app.alive(telemetry.get('identity')))


def rpc(path, request):
    if not isinstance(request, dict):
        raise ValueError('请求须为JSON对象')
    action = request.get('action')
    store = app.State(path)
    if action == 'state':
        return snapshot(store)
    if action == 'terminal':
        state = store.read()
        allowed = {item['pane'] for item in state['terminals']}
        allowed.update(item['pane'] for item in state['commands'] if item.get('pane'))
        pane = request.get('pane')
        if pane not in allowed:
            raise ValueError('仅可查看本工具创建的终端')
        return dict(pane=pane, text=app.tmux('capture-pane', '-p', '-t', pane, '-S', '-120')[-65536:])
    actions = {'add', 'edit', 'dispatch', 'cancel', 'move', 'limits', 'control', 'new_shell', 'clear'}
    if action not in actions:
        raise ValueError('不支持的操作')
    revision = request.get('revision')
    if type(revision) is not int:
        raise ValueError('请先刷新状态')
    store = app.State(path, expected_revision=revision)
    result = None
    if action == 'add':
        text = request.get('command')
        peak = request.get('peak_mib', 8192)
        if not isinstance(text, str) or type(peak) is not int:
            raise ValueError('命令与显存预算格式错误')
        gpu = app.resolve_gpu(str(request.get('gpu', 'cpu')), app.inventory())
        result = app.add(store, text, gpu, peak, request.get('cwd'), request.get('target'))
    elif action == 'edit':
        values = request.get('values')
        if not isinstance(values, dict) or set(values) - {'command', 'gpu', 'peak_mib', 'target'}:
            raise ValueError('修改字段不合法')
        values = dict(values)
        if 'command' in values and not isinstance(values['command'], str):
            raise ValueError('命令须为文本')
        if 'peak_mib' in values and type(values['peak_mib']) is not int:
            raise ValueError('显存预算须为整数')
        if 'gpu' in values:
            values['gpu'] = app.resolve_gpu(str(values['gpu']), app.inventory())
        app.edit(store, request.get('id'), values)
    elif action == 'dispatch':
        app.request_start(store, request.get('id'))
    elif action == 'cancel':
        app.edit(store, request.get('id'), dict(state='canceled', requested=False))
    elif action == 'move':
        if type(request.get('offset')) is not int or request['offset'] not in (-1, 1):
            raise ValueError('仅支持前移或后移一项')
        app.move(store, request.get('id'), request['offset'])
    elif action == 'limits':
        values = request.get('values')
        if not isinstance(values, dict) or any(type(v) is not int for v in values.values()):
            raise ValueError('资源设置须为整数')
        app.configure(store, values)
    elif action == 'control':
        def configure(value):
            mode = request.get('mode', value['mode'])
            paused = request.get('paused', value['paused'])
            if mode not in ('manual', 'auto') or type(paused) is not bool:
                raise ValueError('分发模式不合法')
            if mode != value['mode'] or paused != value['paused']:
                value.update(mode=mode, paused=paused)
                for item in value['commands']:
                    item['requested'] = False
        store.change(configure)
    elif action == 'new_shell':
        result = app.new_shell(store)
    elif action == 'clear':
        store.change(lambda value: value['commands'].__setitem__(
            slice(None), [item for item in value['commands'] if item['state'] not in app.FINISHED]))
    return dict(result=result, state=snapshot(store))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', default=str(Path(__file__).resolve().parent/'current.json'))
    args = parser.parse_args()
    for line in sys.stdin:
        try:
            result = rpc(args.state, json.loads(line))
            response = dict(ok=True, result=result)
        except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
            response = dict(ok=False, error=str(exc))
        print(json.dumps(response, ensure_ascii=False, allow_nan=False), flush=True)


if __name__ == '__main__':
    main()
