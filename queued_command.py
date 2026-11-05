"""Sync an explicitly moved job's config, then exec its original command."""
import argparse
import os

from gpu_transfer import prepare_scientific_config
from queue_store import QueueStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', required=True)
    parser.add_argument('--id', required=True)
    args = parser.parse_args()
    task = QueueStore(args.data).read()['tasks'][args.id]
    if task['state'] != 'running' or os.environ.get('GPU_QUEUE_JOB_ID') != args.id:
        raise ValueError('任务未被队列领取，拒绝启动')
    if os.environ.get('CUDA_VISIBLE_DEVICES') != (task.get('gpu_uuid') or ''):
        raise ValueError('启动设备与当前队列不一致')
    command = task.get('original_argv')
    if not isinstance(command, list) or not command or not all(
            isinstance(arg, str) and arg for arg in command):
        raise ValueError('原始任务命令无效')
    prepare_scientific_config(task)
    if os.name == 'nt':
        import subprocess
        raise SystemExit(subprocess.call(command, env=dict(os.environ)))
    os.execvpe(command[0], command, os.environ)


if __name__ == '__main__':
    main()
