#!/usr/bin/env python3
"""Single-JSON command presets, tmux dispatch, and a terminal GPU monitor."""
import argparse
import contextlib
import csv
import io
import json
import math
import os
from pathlib import Path
import secrets
import shlex
import subprocess
import sys
import time

VERSION = '0.7.1'
ACTIVE = {'launching', 'running', 'orphaned'}
FINISHED = {'done', 'failed', 'canceled', 'interrupted'}
DEFAULT_LIMITS = dict(window=1, reserve_mib=8192, memory_percent=90, utilization=-1)


def identity(pid):
    """Only identities registered by this tool, never arbitrary process arguments."""
    try:
        root = Path('/proc') / str(pid)
        if root.stat().st_uid != os.getuid():
            return None
        fields = (root / 'stat').read_text().rsplit(')', 1)[1].split()
        if fields[0] in ('Z', 'X'):
            return None
        return dict(pid=int(pid), ticks=int(fields[19]),
                    boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip())
    except (FileNotFoundError, ProcessLookupError):
        return None


def alive(saved):
    return bool(saved and identity(saved['pid']) == saved)


class State:
    def __init__(self, path, expected_revision=None):
        self.path = Path(path).expanduser().resolve()
        self.expected_revision = expected_revision

    @contextlib.contextmanager
    def locked(self):
        import fcntl
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix('.lock').open('a+b') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def read(self):
        value = json.loads(self.path.read_text())
        if value.get('schema') != 1:
            raise ValueError('Unsupported current.json schema')
        return value

    def save(self, value):
        # Exactly one persistent JSON. A same-directory temporary file is replaced atomically.
        temp = self.path.with_name(self.path.name + '.tmp')
        try:
            with temp.open('w', encoding='utf-8') as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            temp.replace(self.path)
        finally:
            temp.unlink(missing_ok=True)

    def change(self, action):
        with self.locked():
            value = self.read()
            if self.expected_revision is not None and value['revision'] != self.expected_revision:
                raise ValueError('状态已更新，请刷新后重试')
            result = action(value)
            value['revision'] += 1
            self.save(value)
            return result

    def initialize(self, root, session):
        if not session or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in session):
            raise ValueError('Session must contain only letters, numbers, _ or -')
        with self.locked():
            if self.path.exists():
                return self.read()
            root = Path(root).expanduser().resolve(strict=True)
            if not root.is_dir():
                raise ValueError('Working root must be a directory')
            self.save(dict(schema=1, revision=0, root=str(root), session=session,
                           mode='manual', paused=False, limits=dict(DEFAULT_LIMITS),
                           commands=[], terminals=[], default_target='new', monitor=None))
            return self.read()


def tmux(*args):
    return subprocess.run(['tmux', *args], check=True, capture_output=True,
                          text=True, timeout=10).stdout.strip()


def query(fields):
    output = subprocess.run(['nvidia-smi', fields, '--format=csv,noheader,nounits'],
                            check=True, capture_output=True, text=True, timeout=8)
    return list(csv.reader(io.StringIO(output.stdout), skipinitialspace=True))


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def inventory():
    devices, processes = [], []
    error = None
    try:
        for row in query('--query-gpu=index,uuid,name,memory.used,memory.total,utilization.gpu'):
            devices.append(dict(index=int(row[0]), uuid=row[1].strip(), name=row[2].strip(),
                                used=number(row[3]), total=number(row[4]), util=number(row[5])))
        for row in query('--query-compute-apps=gpu_uuid,pid,process_name,used_memory'):
            processes.append(dict(gpu=row[0].strip(), pid=int(row[1]),
                                  name=row[2].strip(), memory=number(row[3])))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        error = str(exc)
    return dict(devices=devices, processes=processes, error=error)


def resolve_gpu(value, info):
    if value.lower() == 'cpu':
        return None
    for device in info['devices']:
        if value in (str(device['index']), device['uuid']):
            return device['uuid']
    raise ValueError('GPU unavailable, use its current index/UUID or cpu')


def command(value, key):
    for item in value['commands']:
        if item['id'] == key:
            return item
    raise ValueError('Command not found: ' + key)


def pending(value, key):
    item = command(value, key)
    if item['state'] != 'pending':
        raise ValueError('Only pending commands can be changed or dispatched')
    return item


def limit(value, minimum=0, maximum=None):
    if value != -1 and (value < minimum or (maximum is not None and value > maximum)):
        raise ValueError('Invalid limit, use -1 to disable')
    return value


def add(store, text, gpu, peak=8192, cwd=None, target=None):
    if not text.strip():
        raise ValueError('Command must not be empty')
    limit(peak)
    def change(value):
        destination = target or value['default_target']
        directory = Path(cwd or value['root']).expanduser()
        if not directory.is_absolute():
            directory = Path(value['root']) / directory
        directory = directory.resolve(strict=True)
        if not directory.is_dir():
            raise ValueError('Command working directory must be a directory')
        if destination != 'new' and not any(t['pane'] == destination for t in value['terminals']):
            raise ValueError('Use new or a terminal created with new-shell')
        key = secrets.token_hex(4)
        value['commands'].append(dict(id=key, command=text, gpu=gpu, peak_mib=peak,
                                      cwd=str(directory), target=destination, state='pending', requested=False))
        return key
    return store.change(change)


def descendants(saved):
    """Follow only registered children. Foreign NVIDIA PIDs never enter /proc reads."""
    if not alive(saved):
        return set()
    known = {saved['pid']}
    todo = list(known)
    while todo:
        pid = todo.pop()
        try:
            children = (Path('/proc') / str(pid) / 'task' / str(pid) / 'children').read_text().split()
            for child in children:
                child = int(child)
                if child not in known:
                    known.add(child)
                    todo.append(child)
        except (FileNotFoundError, ProcessLookupError):
            pass
    return known


def resource_reason(item, value, info):
    if item['target'] != 'new' and any(job['state'] in ACTIVE and job['target'] == item['target'] for job in value['commands']):
        return 'fixed terminal reserved'
    same = [job for job in value['commands'] if job['state'] in ACTIVE and job['gpu'] == item['gpu']]
    rules = value['limits']
    if rules['window'] != -1 and len(same) >= rules['window']:
        return 'per-GPU window full'
    if item['gpu'] is None:
        return ''
    if info['error']:
        return 'NVIDIA query failed'
    device = next((d for d in info['devices'] if d['uuid'] == item['gpu']), None)
    if device is None:
        return 'GPU missing'
    if rules['utilization'] != -1 and (device['util'] is None or device['util'] >= rules['utilization']):
        return 'compute utilization limit'
    ceilings = []
    if rules['reserve_mib'] != -1:
        if device['total'] is None:
            return 'GPU memory data unavailable'
        ceilings.append(device['total'] - rules['reserve_mib'])
    if rules['memory_percent'] != -1:
        if device['total'] is None:
            return 'GPU memory data unavailable'
        ceilings.append(device['total'] * rules['memory_percent'] / 100)
    if ceilings:
        if device['used'] is None:
            return 'GPU memory data unavailable'
        outstanding = 0
        for job in same:
            pids = descendants(job.get('child'))
            measured = sum(p['memory'] or 0 for p in info['processes'] if p['pid'] in pids and p['gpu'] == item['gpu'])
            outstanding += max(0, job['peak_mib'] - measured)
        projected = device['used'] + outstanding + max(0, item['peak_mib'])
        if projected > min(ceilings):
            return 'memory protection (%d MiB projected)' % projected
    return ''


def shell_command(store, item):
    return shlex.join([sys.executable, str(Path(__file__).resolve()), '--state', str(store.path),
                       'worker', item['id'], item['nonce']])


def create_terminal(value, name='terminal'):
    pane = tmux('new-window', '-d', '-P', '-F', '#{pane_id}', '-t', value['session'] + ':',
                '-n', name, '-c', value['root'], '/bin/bash --noprofile --norc')
    pid = int(tmux('display-message', '-p', '-t', pane, '#{pane_pid}'))
    return dict(pane=pane, shell=identity(pid))


def new_shell(store):
    def create(value):
        terminal = create_terminal(value)
        value['terminals'].append(terminal)
        return terminal['pane']
    return store.change(create)


def deliver(store, item):
    value = store.read()
    text = shell_command(store, item)
    if item['target'] == 'new':
        # Each command gets its own visible terminal, kept open after command exit.
        shell = text + '; exec /bin/bash --noprofile --norc'
        pane = tmux('new-window', '-d', '-P', '-F', '#{pane_id}', '-t', value['session'] + ':',
                    '-n', 'train-' + item['id'], '-c', item['cwd'], shell)
        return pane
    terminal = next((t for t in value['terminals'] if t['pane'] == item['target']), None)
    if terminal is None or not alive(terminal['shell']):
        raise ValueError('Fixed terminal is unavailable')
    pid = terminal['shell']['pid']
    if (Path('/proc') / str(pid) / 'task' / str(pid) / 'children').read_text().strip():
        raise ValueError('Fixed terminal is busy')
    details = tmux('display-message', '-p', '-t', terminal['pane'], '#{pane_pid} #{pane_current_command}')
    if details != str(pid) + ' bash':
        raise ValueError('Fixed terminal identity changed')
    tmux('send-keys', '-t', terminal['pane'], '-l', text)
    tmux('send-keys', '-t', terminal['pane'], 'Enter')
    return terminal['pane']


def reconcile(store):
    with store.locked():
        value = store.read()
        changed = False
        for item in value['commands']:
            if item['state'] not in ACTIVE:
                continue
            if alive(item.get('runner')):
                continue
            if item['state'] == 'launching' and time.time() - item['dispatched'] < 30:
                continue
            state = 'orphaned' if alive(item.get('child')) else 'interrupted'
            if item['state'] != state:
                item.update(state=state, reason='Runner disappeared, review terminal before any new submission')
                changed = True
        if changed:
            value['revision'] += 1
            store.save(value)


def dispatch(store, info):
    reconcile(store)
    selected = None
    with store.locked():
        value = store.read()
        if value['paused']:
            return 'Dispatch paused'
        heads = set()
        reasons = []
        for item in value['commands']:
            if item['state'] != 'pending':
                continue
            if value['mode'] == 'manual':
                if not item['requested']:
                    continue
            elif item['gpu'] in heads:
                continue
            heads.add(item['gpu'])
            reason = resource_reason(item, value, info)
            if reason:
                reasons.append(item['id'] + ' ' + reason)
                continue
            item.update(state='launching', nonce=secrets.token_hex(16), dispatched=time.time(), requested=False)
            value['revision'] += 1
            store.save(value)
            selected = dict(item)
            break
    if selected is None:
        return ' | '.join(reasons) or 'Waiting for commands / manual dispatch'
    try:
        pane = deliver(store, selected)
        def record(value):
            for item in value['commands']:
                if item['id'] == selected['id']:
                    item['pane'] = pane
        store.change(record)
        return 'Dispatched ' + selected['id'] + ' to ' + pane
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        def fail(value):
            item = next((j for j in value['commands'] if j['id'] == selected['id']), None)
            # A delivered worker may already be running even if tmux's reply was lost.
            if item and item['state'] == 'launching' and not item.get('runner'):
                item.update(state='interrupted', reason=str(exc))
        store.change(fail)
        return str(exc)


def worker(store, key, nonce):
    runner = identity(os.getpid())
    with store.locked():
        value = store.read()
        item = command(value, key)
        if item['state'] != 'launching' or item.get('nonce') != nonce or item.get('runner'):
            raise ValueError('Dispatch already consumed or invalid')
        item.update(runner=runner, state='running', started=time.time())
        value['revision'] += 1
        store.save(value)
        item = dict(item)
    print('Command ' + key + '\nGPU ' + str(item['gpu']) + '\nCWD ' + item['cwd'] + '\n' + item['command'], flush=True)
    code = None
    error = None
    try:
        env = dict(os.environ, CUDA_VISIBLE_DEVICES=item['gpu'] or '')
        # The runner remains foreground in tmux. The command has its own process session.
        child = subprocess.Popen(['/bin/bash', '--noprofile', '--norc', '-c', item['command']],
                                 cwd=item['cwd'], env=env, start_new_session=True)
        try:
            store.change(lambda v: command(v, key).update(child=identity(child.pid)))
        except OSError as exc:
            # Still wait for the launched child, even if recording its PID fails.
            print('Cannot record child identity: ' + str(exc), file=sys.stderr, flush=True)
        code = child.wait()
    except (OSError, ValueError) as exc:
        error = str(exc)
    def finish(value):
        current = command(value, key)
        current.update(state='done' if code == 0 else 'failed', exit_code=code,
                       reason=error, finished=time.time())
    store.change(finish)
    print('\nFinished %s, exit %s' % (key, code), flush=True)
    return 0 if code == 0 else 1


def request_start(store, key):
    def change(value):
        if value['paused']:
            raise ValueError('Dispatch is paused, press p to resume')
        pending(value, key)['requested'] = True
    store.change(change)


def toggle(store, field):
    def change(value):
        if field == 'mode':
            value['mode'] = 'auto' if value['mode'] == 'manual' else 'manual'
        else:
            value['paused'] = not value['paused']
        for item in value['commands']:
            item['requested'] = False
    store.change(change)


def move(store, key, offset):
    def change(value):
        item = pending(value, key)
        items = value['commands']
        index = items.index(item)
        candidates = [i for i, job in enumerate(items) if job['state'] == 'pending']
        position = candidates.index(index) + offset
        if 0 <= position < len(candidates):
            other = candidates[position]
            items[index], items[other] = items[other], items[index]
    store.change(change)


def configure(store, values):
    bounds = dict(window=(1, None), reserve_mib=(0, None), memory_percent=(1, 100), utilization=(1, 100))
    for name, value in values.items():
        if name not in bounds:
            raise ValueError('Unknown setting')
        limit(value, *bounds[name])
    store.change(lambda v: v['limits'].update(values))


def edit(store, key, values):
    if 'peak_mib' in values:
        limit(values['peak_mib'])
    if 'command' in values and not values['command'].strip():
        raise ValueError('Command must not be empty')
    def change(value):
        item = pending(value, key)
        target = values.get('target', item['target'])
        if target != 'new' and not any(t['pane'] == target for t in value['terminals']):
            raise ValueError('Use new or a terminal created with new-shell')
        item.update(values)
    store.change(change)


def monitor(store):
    import curses
    import fcntl
    with store.path.with_suffix('.monitor.lock').open('a+b') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('A monitor/dispatcher is already running') from None
        store.change(lambda v: v.update(monitor=identity(os.getpid())))
        curses.wrapper(screen, store)


def screen(window, store):
    import curses
    curses.curs_set(0)
    window.timeout(200)
    selected = 0
    notice = ''
    refreshed = 0
    info = dict(devices=[], processes=[], error=None)
    reason = ''

    def prompt(label, default=''):
        height, width = window.getmaxyx()
        window.move(height-2, 0)
        window.clrtoeol()
        window.addnstr(height-2, 0, label + (' [' + str(default) + ']' if default != '' else '') + ': ', width-1)
        curses.echo()
        curses.curs_set(1)
        window.timeout(-1)
        try:
            text = window.getstr().decode('utf-8').strip()
            return text or str(default)
        finally:
            window.timeout(200)
            curses.noecho()
            curses.curs_set(0)

    def write(row, text, style=0):
        height, width = window.getmaxyx()
        if 0 <= row < height-1 and width > 1:
            try:
                window.addnstr(row, 0, text, width-1, style)
            except curses.error:
                pass

    while True:
        if time.monotonic()-refreshed >= 2:
            info = inventory()
            reason = dispatch(store, info)
            refreshed = time.monotonic()
        value = store.read()
        jobs = value['commands']
        selected = max(0, min(selected, len(jobs)-1))
        height, width = window.getmaxyx()
        window.erase()
        write(0, 'TMUX GPU DISPATCH ' + VERSION + '  ' + value['session'] + '  ' + value['mode'].upper() +
              ('  PAUSED' if value['paused'] else '  READY') + '  ' + time.strftime('%H:%M:%S'), curses.A_BOLD)
        rules = value['limits']
        write(1, 'Per GPU window {window} | reserve {reserve_mib} MiB | memory {memory_percent}% | compute {utilization}% (-1 unlimited)'.format(**rules))
        row = 3
        for device in info['devices']:
            memory = device['used'] / device['total'] if device['used'] is not None and device['total'] else 0
            compute = device['util'] or 0
            def bar(fraction):
                count = min(10, max(0, int(fraction*10)))
                return '[' + '#' * count + '.' * (10-count) + ']'
            used = 'N/A' if device['used'] is None else '%.1f' % (device['used']/1024)
            total = 'N/A' if device['total'] is None else '%.1f' % (device['total']/1024)
            write(row, 'GPU %d MEM %s %s/%s GiB  UTIL %s %s%%' %
                  (device['index'], bar(memory), used, total, bar(compute/100), device['util']))
            row += 1
        if info['error']:
            write(row, 'NVIDIA ' + info['error']);row += 1
        write(row+1, 'GPU PROCESSES  PID       MEM MiB   NAME                       [read-only]')
        row += 2
        for process in info['processes'][:max(1, min(5, height//6))]:
            index = next((str(d['index']) for d in info['devices'] if d['uuid'] == process['gpu']), '?')
            name = '/'.join(Path(process['name']).parts[-3:])
            write(row, '%-3s %-9s %-9s [read-only] %s' % (index, process['pid'], process['memory'], name))
            row += 1
        write(row+1, 'COMMANDS  ID        STATE         GPU  PEAK MiB  TARGET   COMMAND', curses.A_BOLD)
        row += 2
        available = max(1, height-row-6)
        start = max(0, selected-available+1)
        for index in range(start, min(len(jobs), start+available)):
            item = jobs[index]
            gpu_index = next((str(d['index']) for d in info['devices'] if d['uuid'] == item['gpu']), 'cpu' if item['gpu'] is None else '?')
            text = ('%s %-8s %-13s %-4s %-9s %-8s %s' %
                    ('>' if index == selected else ' ', item['id'], item['state'], gpu_index,
                     item['peak_mib'], item.get('pane', item['target']), item['command']))
            write(row, text, curses.A_REVERSE if index == selected else 0)
            row += 1
        write(height-5, notice or reason)
        write(height-4, 'Enter run | a mode | p pause | c add | e edit | g GPU | m MiB | t target')
        write(height-3, 'j/k select | J/K order | x cancel | d clear | n terminal | l limits | q quit')
        if jobs:
            write(height-2, 'Selected ' + jobs[selected]['id'] + '  ' + str(jobs[selected].get('reason') or ''))
        window.refresh()
        key = window.getch()
        if key == -1 or key == curses.KEY_RESIZE:
            continue
        notice = ''
        try:
            item = jobs[selected] if jobs else None
            if key == ord('q'):
                return
            if key in (ord('j'), curses.KEY_DOWN):selected += 1
            elif key in (ord('k'), curses.KEY_UP):selected -= 1
            elif key == ord('a'):toggle(store, 'mode')
            elif key == ord('p'):toggle(store, 'paused')
            elif key == ord('n'):notice = 'New terminal ' + new_shell(store)
            elif key == ord('c'):
                text = prompt('Command')
                if text:
                    gpu = resolve_gpu(prompt('GPU index / UUID / cpu', '0'), info)
                    peak = int(prompt('Expected peak MiB (-1 unlimited)', '8192'))
                    notice = 'Saved ' + add(store, text, gpu, peak)
            elif key == ord('l'):
                values = {}
                for field in DEFAULT_LIMITS:
                    values[field] = int(prompt(field + ' (-1 unlimited)', rules[field]))
                configure(store, values)
            elif key == ord('d'):
                store.change(lambda v: v['commands'].__setitem__(slice(None), [j for j in v['commands'] if j['state'] not in FINISHED]))
            elif item:
                if key in (10, 13):request_start(store, item['id'])
                elif key == ord('g'):edit(store, item['id'], dict(gpu=resolve_gpu(prompt('GPU / cpu'), info)))
                elif key == ord('m'):edit(store, item['id'], dict(peak_mib=int(prompt('Peak MiB', item['peak_mib']))))
                elif key == ord('e'):edit(store, item['id'], dict(command=prompt('Command', item['command'])))
                elif key == ord('t'):edit(store, item['id'], dict(target=prompt('new or own pane ID', item['target'])))
                elif key == ord('J'):move(store, item['id'], 1)
                elif key == ord('K'):move(store, item['id'], -1)
                elif key == ord('x'):edit(store, item['id'], dict(state='canceled', requested=False))
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            notice = str(exc)


def start(store):
    import gpu_history
    value = store.read()
    if alive(value.get('monitor')):
        gpu_history.start(store)
        print('Monitor already running in ' + value['session'])
        return
    # Startup is serialized against another start and the tmux launch is not retried.
    with store.locked():
        value = store.read()
        names = tmux('list-windows', '-t', '=' + value['session'], '-F', '#{window_name}').splitlines()
        if 'gpu-monitor' in names:
            raise ValueError('gpu-monitor window already exists, inspect it before restarting')
        if value['default_target'] == 'new':
            terminal = create_terminal(value, 'train')
            value['terminals'].append(terminal)
            value['default_target'] = terminal['pane']
            value['revision'] += 1
            store.save(value)
        text = shlex.join([sys.executable, str(Path(__file__).resolve()), '--state', str(store.path), 'monitor'])
        pane = tmux('new-window', '-d', '-P', '-F', '#{pane_id}', '-t', value['session'] + ':',
                    '-n', 'gpu-monitor', '-c', value['root'], text + '; exec /bin/bash --noprofile --norc')
    print('Monitor launched in ' + value['session'] + ':gpu-monitor (' + pane + ')')
    gpu_history.start(store)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', default=str(Path(__file__).resolve().parent / 'current.json'))
    commands = parser.add_subparsers(dest='action', required=True)
    init = commands.add_parser('init')
    init.add_argument('--root', default=str(Path.cwd()))
    init.add_argument('--session', default='zhp1')
    for name in ('start', 'monitor', 'show', 'new-shell', 'clear'):
        commands.add_parser(name)
    add_parser = commands.add_parser('add')
    add_parser.add_argument('--gpu', default='0')
    add_parser.add_argument('--peak-mib', type=int, default=8192)
    add_parser.add_argument('--cwd')
    add_parser.add_argument('--target', help='Default fixed terminal, new, or a new-shell pane ID')
    add_parser.add_argument('command', help='Shell command as one quoted argument')
    for name in ('dispatch', 'cancel', 'up', 'down'):
        sub = commands.add_parser(name);sub.add_argument('id')
    edit_parser = commands.add_parser('edit')
    edit_parser.add_argument('id')
    edit_parser.add_argument('--gpu')
    edit_parser.add_argument('--peak-mib', type=int)
    edit_parser.add_argument('--command')
    edit_parser.add_argument('--target')
    mode_parser = commands.add_parser('mode');mode_parser.add_argument('mode', choices=['manual','auto'])
    pause_parser = commands.add_parser('pause');pause_parser.add_argument('value', choices=['on','off'])
    limits_parser = commands.add_parser('limits')
    for field in DEFAULT_LIMITS:
        limits_parser.add_argument('--' + field.replace('_','-'), type=int)
    worker_parser = commands.add_parser('worker')
    worker_parser.add_argument('id');worker_parser.add_argument('nonce')
    args = parser.parse_args()
    store = State(args.state)
    if args.action == 'init':
        store.initialize(args.root, args.session)
    elif args.action == 'start':start(store)
    elif args.action == 'monitor':monitor(store)
    elif args.action == 'show':print(json.dumps(store.read(), ensure_ascii=False, indent=2))
    elif args.action == 'new-shell':print(new_shell(store))
    elif args.action == 'add':
        print(add(store, args.command, resolve_gpu(args.gpu, inventory()), args.peak_mib, args.cwd, args.target))
    elif args.action == 'worker':return worker(store, args.id, args.nonce)
    elif args.action == 'dispatch':request_start(store, args.id)
    elif args.action == 'cancel':edit(store, args.id, dict(state='canceled', requested=False))
    elif args.action in ('up', 'down'):move(store, args.id, -1 if args.action == 'up' else 1)
    elif args.action == 'edit':
        values = {field:getattr(args, field) for field in ('command','target','peak_mib') if getattr(args, field) is not None}
        if args.gpu is not None:
            values['gpu'] = resolve_gpu(args.gpu, inventory())
        edit(store, args.id, values)
    elif args.action == 'limits':
        configure(store, {field:getattr(args, field) for field in DEFAULT_LIMITS if getattr(args, field) is not None})
    elif args.action == 'mode':
        if store.read()['mode'] != args.mode:toggle(store, 'mode')
    elif args.action == 'pause':
        if store.read()['paused'] != (args.value == 'on'):toggle(store, 'paused')
    elif args.action == 'clear':
        store.change(lambda v: v['commands'].__setitem__(slice(None), [j for j in v['commands'] if j['state'] not in FINISHED]))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
