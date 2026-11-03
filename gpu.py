"""Read-only NVIDIA discovery. No CUDA allocation, memory probe, or process signals."""
import csv
import io
import os
import subprocess
from pathlib import Path


def identity(pid,expected=None):
    try:
        p = Path('/proc') / str(int(pid))
        if p.stat().st_uid!=os.getuid():return None
        f = (p / 'stat').read_text().rsplit(')', 1)[1].split()
        boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        if expected and (int(f[19])!=expected.get('start_ticks') or boot!=expected.get('boot')):return None
        return dict(pid=int(pid), state=f[0], start_ticks=int(f[19]),
                    group=int(f[2]), uid=p.stat().st_uid,
                    boot=boot,
                    args=[s.decode(errors='replace') for s in (p / 'cmdline').read_bytes().split(b'\0') if s])
    except FileNotFoundError:
        return None


def own_process_ids():
    """Ask the OS for this UID's PIDs only; never enumerate other users' argv."""
    try:
        result=subprocess.run(['ps','-u',str(os.getuid()),'-o','pid='],check=True,capture_output=True,text=True,timeout=5)
        return {int(p) for p in result.stdout.split()}
    except (OSError,ValueError,subprocess.SubprocessError,AttributeError):return set()


def alive(saved):
    if not saved:
        return False
    current = identity(saved['pid'],expected=saved)
    return bool(current and current['state'] not in ('Z', 'X') and
                all(current[k] == saved[k] for k in ('start_ticks', 'boot')))


def is_hold(proc, rule, gpu):
    """Ignore only an explicitly configured, own-UID, exact script/device match."""
    if not proc or proc['uid'] != os.getuid():
        return False
    args = proc['args']
    if rule['script'] not in args:
        return False
    for flag, value in (('--gpu', gpu['index']), ('--gpu-uuid', gpu['uuid'])):
        if flag in args:
            i = args.index(flag)
            if i + 1 < len(args) and args[i + 1] == str(value):
                return True
    return False


def query(args):
    out = subprocess.run(['nvidia-smi', *args, '--format=csv,noheader,nounits'],
                         check=True, capture_output=True, text=True, timeout=8)
    return list(csv.reader(io.StringIO(out.stdout), skipinitialspace=True))


def snapshot(config, telemetry=False):
    try:
        fields = 'index,uuid,name,memory.used,memory.total,utilization.gpu' if telemetry else 'index,uuid,name'
        devices = []
        for r in query(['--query-gpu=' + fields]):
            if not r:
                continue
            device = dict(index=int(r[0]), uuid=r[1].strip(), name=r[2].strip(), processes=[])
            if telemetry:
                def number(s):
                    try:return float(s)
                    except ValueError:return None
                device.update(memory_used_mib=number(r[3]), memory_total_mib=number(r[4]), utilization=number(r[5]))
            devices.append(device)
        if telemetry:
            return dict(ok=True,error=None,gpus=devices)
        by_uuid = {g['uuid']: g for g in devices}
        owned=own_process_ids()
        for row in query(['--query-compute-apps=gpu_uuid,pid,process_name']):
            if not row or row[0].strip() not in by_uuid:
                continue
            gpu = by_uuid[row[0].strip()]
            trusted=next((rule['identity'] for rule in config.get('hold_rules',[])
                if rule.get('identity',{}).get('pid')==int(row[1])),None)
            proc = identity(int(row[1]),expected=trusted) if trusted and int(row[1]) in owned else None
            if proc and proc['state'] in ('Z', 'X'):
                continue
            hold = any(is_hold(proc, rule, gpu) for rule in config.get('hold_rules', []))
            gpu['processes'].append(dict(pid=int(row[1]), name=row[2].strip(),
                                         identity=proc, hold=hold))
        return dict(ok=True, error=None, gpus=devices)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return dict(ok=False, error=str(exc), gpus=[])
