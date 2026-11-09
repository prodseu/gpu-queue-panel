"""Independent NVIDIA sampler. A rolling five-minute cache in current.json."""
import argparse
import math
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

import tmux_dispatch as app

WINDOW = 300


def prune(history, now):
    result = {}
    for uuid, points in history.items():
        valid = [point for point in points if now-WINDOW <= point['t'] <= now]
        if valid:
            result[uuid] = valid
    return result


def update(history, info, sampled):
    history = prune(history, sampled)
    for device in info['devices']:
        points = history.setdefault(device['uuid'], [])
        if points and sampled <= points[-1]['t']:
            continue
        used, total = device['used'], device['total']
        memory = 100*used/total if used is not None and total and total > 0 else None
        points.append(dict(t=sampled, compute=device['util'], memory=memory))
    return history


def process_rows(xml):
    rows = []
    for device in ET.fromstring(xml).findall('gpu'):
        uuid = device.findtext('uuid')
        for item in device.findall('./processes/process_info'):
            pid = item.findtext('pid')
            if not uuid or not pid or not pid.isdecimal():
                continue
            memory = item.findtext('used_memory', '').split()
            rows.append(dict(gpu=uuid, pid=int(pid), name=item.findtext('process_name') or 'N/A',
                             memory=app.number(memory[0]) if memory else None,
                             type=item.findtext('type') or 'N/A', editable=False))
    return rows


def sample():
    devices = []
    try:
        for row in app.query('--query-gpu=index,uuid,name,memory.used,memory.total,utilization.gpu'):
            devices.append(dict(index=int(row[0]), uuid=row[1].strip(), name=row[2].strip(),
                                used=app.number(row[3]), total=app.number(row[4]), util=app.number(row[5])))
        # Includes compute, graphics and mixed processes, without reading foreign /proc.
        result = subprocess.run(['nvidia-smi', '-q', '-x'], check=True, capture_output=True,
                                text=True, timeout=8)
        return dict(devices=devices, processes=process_rows(result.stdout), error=None)
    except (OSError, ValueError, ET.ParseError, subprocess.SubprocessError) as exc:
        return dict(devices=devices, processes=[], error=str(exc))


def persist(store, info, sampled, owner):
    with store.locked():
        value = store.read()
        previous = value.get('telemetry', {})
        value['telemetry'] = dict(sampled=sampled, identity=owner, inventory=info,
                                 history=update(previous.get('history', {}), info, sampled))
        # Telemetry does not advance the command revision or invalidate browser edits.
        store.save(value)
        return value['telemetry']


def sparkline(points, field, now, width=60):
    buckets = [None]*width
    for point in points:
        position = min(width-1, int((point['t']-(now-WINDOW))*width/WINDOW))
        value = point.get(field)
        if 0 <= position < width and isinstance(value, (int, float)) and math.isfinite(value):
            buckets[position] = value
    symbols = '._:-=+*#%@'
    return ''.join(' ' if value is None else symbols[min(9, max(0, round(value*9/100)))] for value in buckets)


def run(store):
    import fcntl
    with store.path.with_suffix('.history.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        owner = app.identity(os.getpid())
        with store.locked():
            value = store.read()
            value.setdefault('telemetry', {})['identity'] = owner
            store.save(value)
        while True:
            info = sample()
            now = time.time()
            telemetry = persist(store, info, now, owner)
            lines = ['GPU HISTORY · last 5 min · 0–100% · '+time.strftime('%H:%M:%S'),
                     'Each row spans exactly 5 min. Old samples expire automatically.', '']
            for device in info['devices']:
                points = telemetry['history'].get(device['uuid'], [])
                lines.append('GPU %d  %s' % (device['index'], device['name']))
                lines.append('UTIL |'+sparkline(points, 'compute', now)+'| now')
                lines.append('MEM  |'+sparkline(points, 'memory', now)+'| now')
            lines.append('\nGPU PROCESSES · 不可编辑')
            for process in info['processes']:
                name = '/'.join(Path(process['name']).parts[-3:])
                lines.append('%s PID %-8s %s MiB [%s] %s [不可编辑]' %
                             (process['gpu'][:12], process['pid'], process['memory'], process['type'], name))
            if info['error']:
                lines.append(info['error'])
            print('\033[2J\033[H'+'\n'.join(lines), flush=True)
            time.sleep(2)


def start(store):
    value = store.read()
    if app.alive(value.get('telemetry', {}).get('identity')):
        print('GPU history sampler already running')
        return
    session = value['session']
    names = app.tmux('list-windows', '-t', '='+session, '-F', '#{window_name}').splitlines()
    if 'gpu-history' in names:
        raise ValueError('gpu-history window exists, inspect it before restarting')
    command = shlex.join([sys.executable, str(Path(__file__).resolve()),
                          '--state', str(store.path), 'run'])
    app.tmux('new-window', '-d', '-t', session+':', '-n', 'gpu-history', '-c', value['root'],
             command+'; exec /bin/bash --noprofile --norc')
    for _ in range(40):
        if app.alive(store.read().get('telemetry', {}).get('identity')):
            break
        time.sleep(.1)
    else:
        raise ValueError('采样器启动未完成，请查看 '+session+':gpu-history')
    print('GPU history sampler launched in '+session+':gpu-history')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', default=str(Path(__file__).resolve().parent/'current.json'))
    parser.add_argument('command', choices=['run', 'start'])
    args = parser.parse_args()
    store = app.State(args.state)
    (start if args.command == 'start' else run)(store)


if __name__ == '__main__':
    main()
