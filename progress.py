"""Read the existing task's progress file; do not run inference or training."""
import json
import math
from pathlib import Path


def load(spec):
    result=dict(percent=None,status={},updated=None,error=None)
    try:
        path=Path(spec['status_file'])
        status=json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(status,dict):raise ValueError('进度文件应为JSON对象')
        result.update(status=status,updated=path.stat().st_mtime)
        done=status.get(spec.get('progress_key','updates'))
        total=spec['total']
        if isinstance(done,(int,float)) and math.isfinite(done) and total>0:
            result['percent']=max(0,min(100,100*done/total))
    except (OSError,ValueError,KeyError,TypeError) as exc:
        result['error']=str(exc)
    return result
