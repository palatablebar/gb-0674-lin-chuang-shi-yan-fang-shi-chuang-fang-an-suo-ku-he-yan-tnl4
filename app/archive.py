import io, csv, json, zipfile, hashlib, base64
from app.errors import BusinessError

def serialize(result):
    if not result.get('manifest') or not result.get('views'):
        raise BusinessError('INCOMPLETE_RUN','批次尚未完成')
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('manifest.json',json.dumps({**result['manifest'],'revision':result.get('revision',1),'status':result.get('status','待复核')},ensure_ascii=False,indent=2))
        archive.writestr('evidence.json',json.dumps({'counts':result['counts'],'views':result['views'],'quarantine':result['quarantine'],'dispense':result.get('dispense',{})},ensure_ascii=False,indent=2))
        # 原始源与交接回传原样入包，重算失败或被隔离时仍有可复核的原始证据。
        snapshot=result.get('input_snapshot') or {}
        if snapshot.get('bytes_b64'):
            archive.writestr('source.csv',base64.b64decode(snapshot['bytes_b64']))
        if snapshot.get('dispense_b64'):
            archive.writestr('dispense.csv',base64.b64decode(snapshot['dispense_b64']))
    return buffer.getvalue()
