import csv, io, json, zipfile
from app.engine import evaluate
from app.config import ROOT

HEADER = ['event_id','subject','visit','box','action','at','evidence','replaces','protocol']
DAY1 = '2026-10-08T08:00:00+08:00'


def csv_text(rows):
    buffer = io.StringIO(newline='')
    writer = csv.writer(buffer, lineterminator='\n')
    writer.writerow(HEADER)
    writer.writerows(rows)
    return buffer.getvalue()


def run(rows):
    # 用手写交接证据覆盖清批样例回传；未写到的访视按无发药处理，便于逐访视断言。
    return evaluate({'dataset': 'clean', 'dispense_csv_text': csv_text(rows)})


def verdict(result, subject, visit):
    return next(v for v in result['dispense']['verdicts']
                if v['subject'] == subject and v['visit'] == visit)


def test_clean_handover_settles_all(clean):
    assert clean['dispense']['status'] == '可放行'
    assert clean['dispense']['settled_visits'] == 40
    assert clean['dispense']['totals']['reconcile']
    assert clean['balance']['status'] == '可锁库'


def test_clean_totals_match_boxes(clean):
    totals = clean['dispense']['totals']
    assert totals == {'dispensed': 40, 'returned': 20, 'destroyed': 20, 'replaced': 0,
                      'in_hand': 0, 'missing': 0, 'accounted': 40, 'reconcile': True}


def test_destroy_with_evidence_is_terminal(sample):
    # 00009 只写“中心已销毁”但有销毁凭证，不要求先回收。
    v = verdict(sample, '00009', 'V1')
    assert v['settled'] and v['resolution'] == '已销毁'


def test_destroy_without_evidence_blocks(sample):
    v = verdict(sample, '00004', 'V1')
    assert not v['settled'] and v['code'] == 'DESTROY_WITHOUT_EVIDENCE'
    assert any(x['subject'] == '00004' and x['visit'] == 'V1' for x in sample['dispense']['deviations'])


def test_box_in_hand_blocks_visit(sample):
    v = verdict(sample, '00007', 'V1')
    assert not v['settled'] and v['code'] == 'BOX_IN_HAND'
    assert v['resolution'] == '手中'


def test_return_without_evidence_blocks(sample):
    assert verdict(sample, '00010', 'V1')['code'] == 'RETURN_WITHOUT_EVIDENCE'


def test_excursion_without_evidence_blocks(sample):
    assert verdict(sample, '00005', 'V2')['code'] == 'EXCURSION_WITHOUT_EVIDENCE'


def test_replacement_missing_old_box_blocks(sample):
    v = verdict(sample, '00006', 'V1')
    assert not v['settled'] and v['code'] == 'REPLACEMENT_OLD_BOX_MISSING'


def test_replacement_chain_settles(clean):
    totals = clean['dispense']['totals']
    assert totals['destroyed'] + totals['returned'] == 40
    # 00002 换成 B2 后再回收，旧盒的换发链也结清。
    result = run([
        ['d', '00002', 'V1', 'B1', 'DISPENSE', DAY1, 'e1', '', 'auto'],
        ['x', '00002', 'V1', 'B1', 'EXCURSION', '2026-10-08T12:00:00+08:00', 'e2', '', 'auto'],
        ['p', '00002', 'V1', 'B2', 'REPLACE', '2026-10-08T13:00:00+08:00', 'e3', 'B1', 'auto'],
        ['r', '00002', 'V1', 'B2', 'RETURN', '2026-10-09T09:00:00+08:00', 'e4', '', 'auto'],
    ])
    v = verdict(result, '00002', 'V1')
    assert v['settled'], v['reasons']
    assert v['chain'] == 'B1,B2'
    states = {b['box']: b['state'] for b in result['dispense']['boxes']}
    assert states['B1'] == '已换发' and states['B2'] == '已回收'


def test_retransmission_counts_once():
    rows = [['d', '00002', 'V1', 'B1', 'DISPENSE', DAY1, 'e1', '', 'auto'],
            ['d', '00002', 'V1', 'B1', 'DISPENSE', DAY1, 'e1', '', 'auto'],
            ['r', '00002', 'V1', 'B1', 'RETURN', '2026-10-09T09:00:00+08:00', 'e2', '', 'auto'],
            ['r', '00002', 'V1', 'B1', 'RETURN', '2026-10-09T09:00:00+08:00', 'e2', '', 'auto']]
    result = run(rows)
    assert result['dispense']['counts']['retransmission'] == 2
    assert result['dispense']['totals']['dispensed'] == 1
    assert len(result['dispense']['retries']) == 2
    assert verdict(result, '00002', 'V1')['settled']


def test_same_event_conflicting_content_blocks():
    rows = [['d', '00002', 'V1', 'B1', 'DISPENSE', DAY1, 'e1', '', 'auto'],
            ['d', '00002', 'V1', 'B9', 'DISPENSE', DAY1, 'e1', '', 'auto']]
    result = run(rows)
    assert any(q['code'] == 'DUPLICATE_CONFLICT' for q in result['dispense']['quarantine'])
    assert result['dispense']['status'] == '阻断'


def test_conflicting_retransmission_keeps_raw_evidence():
    rows = [['d', '00002', 'V1', 'B1', 'DISPENSE', DAY1, 'e1', '', 'auto'],
            ['r', '00002', 'V1', 'B1', 'RETURN', DAY1, 'e2', '', 'auto'],
            ['r', '00002', 'V1', 'B1', 'RETURN', DAY1, 'e3', '', 'auto']]
    result = run(rows)
    assert result['dispense']['quarantine']
    assert all(set(q['raw']) >= {'event_id', 'box'} for q in result['dispense']['quarantine'])


def test_missing_handover_blocks_lock(clean):
    result = evaluate({'dataset': 'clean', 'dispense_csv_text': ''})
    assert result['dispense']['status'] == '阻断'
    assert result['dispense']['file_issue']['code'] == 'EMPTY_FILE'
    assert result['balance']['status'] == '待处理'
    assert result['archive']['status'] == '待处理'


def test_bad_handover_header_blocks_without_losing_source():
    result = evaluate({'dataset': 'clean', 'dispense_csv_text': 'wrong,header\n1,2\n'})
    assert result['dispense']['file_issue']['code'] == 'BAD_HEADER'
    assert result['dispense']['quarantine'][0]['code'] == 'BAD_HEADER'
    assert result['balance']['status'] == '待处理'


def test_no_dispense_for_completed_visit_blocks():
    rows = [['d', '00002', 'V1', 'B1', 'DISPENSE', DAY1, 'e1', '', 'auto'],
            ['r', '00002', 'V1', 'B1', 'RETURN', '2026-10-09T09:00:00+08:00', 'e2', '', 'auto']]
    v = verdict(run(rows), '00001', 'V1')
    assert not v['settled'] and v['code'] == 'NO_DISPENSE'


def test_unknown_reference_isolated():
    rows = [['d', '00002', 'V1', 'B1', 'DISPENSE', DAY1, 'e1', '', 'auto'],
            ['r', '00002', 'V1', 'GHOST', 'RETURN', '2026-10-09T09:00:00+08:00', 'e2', '', 'auto']]
    result = run(rows)
    v = verdict(result, '00002', 'V1')
    assert not v['settled'] and v['code'] == 'CHAIN_MISSING'
    assert any(b['box'] == 'GHOST' and b['state'] == '去向缺失' for b in result['dispense']['boxes'])


def test_protocol_mismatch_isolated():
    rows = [['d', '00001', 'V1', 'B1', 'DISPENSE', '2026-10-11T08:00:00+08:00', 'e1', '', 'v1'],
            ['r', '00001', 'V1', 'B1', 'RETURN', '2026-10-12T09:00:00+08:00', 'e2', '', 'auto']]
    result = run(rows)
    # 10月11日 00001 已转 v2，申报 v1 的交接证据不采信。
    assert any(q['code'] == 'DISPENSE_PROTOCOL_MISMATCH' for q in result['dispense']['quarantine'])
    assert not verdict(result, '00001', 'V1')['settled']


def test_visit_completion_shared_between_modules(sample):
    v = verdict(sample, '00001', 'V1')
    assert v['shared'] and v['code'] == 'WINDOW_LATE'
    assert v['code'] in {'WINDOW_LATE', 'MISSING_PRIMARY', 'DUPLICATE_PRIMARY'}
    gate = next(r for r in sample['views']['protocol']['rows']
                if r['subject'] == '00001' and r['visit'] == 'V1')
    assert gate['settled'] == '未结清'
    window = next(r for r in sample['views']['windows']['rows'] if r['id'] == '00001-V1')
    assert window['settled'] == '未结清'


def test_shared_block_not_double_counted(sample):
    balance = sample['balance']
    assert sample['dispense']['shared_blocked_visits'] == 2
    assert balance['dispense_blocked'] == sample['dispense']['own_blocked_visits']
    assert balance['blocked_total'] == balance['blocked'] + balance['dispense_blocked']


def test_handover_is_part_of_run_digest():
    a = evaluate({'dataset': 'clean'})
    b = evaluate({'dataset': 'clean', 'dispense_csv_text': open(ROOT / 'sample_data/dispense_dirty.csv').read()})
    assert a['manifest']['run_id'] != b['manifest']['run_id']
    again = evaluate({'dataset': 'clean'})
    assert again['manifest']['run_id'] == a['manifest']['run_id']


def test_rerun_keeps_snapshot_handover(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main, store
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'rerun.sqlite'))
    client = TestClient(main.app)
    created = client.post('/api/runs', json={'dataset': 'clean'}).json()
    run_id = created['manifest']['run_id']
    replay = client.post(f'/api/runs/{run_id}/rerun', json={})
    assert replay.status_code == 200
    assert replay.json()['dispense']['settled_visits'] == 40
    assert replay.json()['manifest']['sha256'] == created['manifest']['sha256']


def test_rerun_failure_preserves_previous(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main, store
    path = str(tmp_path / 'fail.sqlite')
    monkeypatch.setattr(main, 'DB_PATH', path)
    client = TestClient(main.app)
    created = client.post('/api/runs', json={'dataset': 'clean'}).json()
    run_id = created['manifest']['run_id']
    corrupt = dict(created['input_snapshot'])
    corrupt['bytes_b64'] = ''  # 记录源缺失，重算必然失败
    with store.connect(path) as con:
        con.execute('UPDATE runs SET result=? WHERE id=?',
                    (json.dumps({**created, 'input_snapshot': corrupt}, ensure_ascii=False), run_id))
    response = client.post(f'/api/runs/{run_id}/rerun', json={})
    assert response.status_code == 422
    assert response.json()['code'] == 'RERUN_FAILED'
    kept = store.get(path, run_id)
    assert kept['dispense']['settled_visits'] == 40
    assert kept['manifest']['sha256'] == created['manifest']['sha256']


def test_archive_contains_handover(sample):
    from app.archive import serialize
    payload = serialize(sample)
    bundle = zipfile.ZipFile(io.BytesIO(payload))
    evidence = json.loads(bundle.read('evidence.json'))
    assert evidence['dispense']['boxes']
    assert evidence['dispense']['verdicts']
    # 原始交接回传随包，重算失败也能复核。
    assert b'event_id,subject,visit,box' in bundle.read('dispense.csv')
    assert bundle.read('source.csv')


def test_handover_page_shows_states(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'page.sqlite'))
    client = TestClient(main.app)
    html = client.get('/dispense').text
    for word in ('手中', '已销毁', '已回收', '阻断原因'):
        assert word in html
