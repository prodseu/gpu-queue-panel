"""Persist a rolling GPU history from the existing server snapshots, without CUDA."""
import json
from pathlib import Path
from queue_store import atomic

WINDOW_SECONDS=300


def update(history,snapshot,now):
    cutoff=now-WINDOW_SECONDS
    result={}
    for uuid,points in history.items():
        current=[p for p in points if p['t']>=cutoff]
        # Keep one real boundary sample to draw continuously at the left edge.
        previous=[p for p in points if cutoff-10<=p['t']<cutoff]
        if current:
            result[uuid]=previous[-1:]+current
    sampled=snapshot.get('sampled')
    if not isinstance(sampled,(int,float)) or not cutoff<=sampled<=now+5:
        return result
    for device in snapshot.get('gpus',[]):
        uuid=device['uuid']
        points=result.setdefault(uuid,[])
        if points and sampled<=points[-1]['t']:
            continue
        used,total=device.get('memory_used_mib'),device.get('memory_total_mib')
        memory=100*used/total if used is not None and total and total>0 else None
        points.append(dict(t=sampled,compute=device.get('utilization'),memory=memory))
    return result


