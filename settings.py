"""Keep machine-specific configuration outside the source repository."""
import json
import os
from pathlib import Path,PurePosixPath

ROOT=Path(__file__).resolve().parent


def load():
    path=Path(os.environ.get('GPU_QUEUE_CONFIG',str(ROOT/'config.json'))).expanduser().resolve()
    if not path.is_file():
        raise ValueError('请复制config.example.json为config.json，并将XXX替换为自己的配置')
    config=json.loads(path.read_text(encoding='utf-8-sig'))
    config.setdefault('local_port',8789)
    config.setdefault('tmux_session','gpu-queue-panel-rc')
    config.setdefault('refresh_seconds',3)
    config.setdefault('stop_on_failure',False)
    config.setdefault('hold_rules',[])
    config['data_path']=str((path.parent/Path(config.get('data_dir','runtime-data-rc'))).resolve())
    return config


def remote_bridge(config):
    import shlex
    values=[config.get(k,'XXX') for k in ('ssh_host','remote_dir','remote_python')]
    if any('XXX' in v for v in values):
        raise ValueError('请在config.json中填写SSH别名、服务器项目目录和Python路径')
    return shlex.join([config['remote_python'],str(PurePosixPath(config['remote_dir'])/'server_agent.py'),'bridge'])
