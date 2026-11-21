from importlib import import_module
from pathlib import Path
from copy import deepcopy
import json, time, base64
from app.config import ROOT, MODULES, MAX_BYTES
from app.errors import BusinessError
from app.manifest import digest, make_manifest

def evaluate(payload=None, until=8, snapshot=None):
    payload=payload or {}
    dataset=payload.get('dataset','dirty')
    if dataset not in {'dirty','clean','legacy'}: raise BusinessError('BAD_DATASET','样例名称无效')
    if not isinstance(payload.get('overrides',{}),dict): raise BusinessError('BAD_CONTEXT','参数须为对象')
    if snapshot is not None:
        raw=base64.b64decode(snapshot['bytes_b64'],validate=True)
        handover=base64.b64decode(snapshot.get('dispense_b64',''),validate=True)
    else:
        raw=(str(payload['csv_text']).encode() if 'csv_text' in payload else (ROOT/'sample_data'/f'{dataset}.csv').read_bytes())
        if 'dispense_csv_text' in payload:
            handover=str(payload['dispense_csv_text']).encode()
        elif 'csv_text' in payload:
            # 自备记录时不得借用仓内样例交接证据，否则会把样例盒当成这批的盒。
            handover=b''
        else:
            handover=(ROOT/'sample_data'/f'dispense_{dataset}.csv').read_bytes()
    if len(raw)>MAX_BYTES: raise BusinessError('TOO_LARGE','文件超过2MB',413)
    if len(handover)>MAX_BYTES: raise BusinessError('TOO_LARGE','药物交接文件超过2MB',413)
    context=deepcopy(snapshot['context']) if snapshot is not None else json.loads((ROOT/'sample_data/context.json').read_text())
    context.update(deepcopy(payload.get('overrides',{})))
    start=time.perf_counter()
    state={'raw_bytes':raw,'dispense_raw_bytes':handover,'context':context,'records':[],'quarantine':[], 'counts':{},'views':{},'source':dataset}
    for index,key in enumerate(MODULES,1):
        if index>until: break
        import_module(f'app.modules.{key}.rules').apply(state)
    state['input_snapshot']={'bytes_b64':base64.b64encode(raw).decode(),'dispense_b64':base64.b64encode(handover).decode(),'context':deepcopy(context)}
    state.pop('raw_bytes',None);state.pop('dispense_raw_bytes',None)
    state['manifest']=make_manifest(digest(raw,context,handover),dataset,time.perf_counter()-start)
    return state
