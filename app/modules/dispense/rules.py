"""药物交接裁决。

目标：按“计划访视”结清发药责任，而不是按总量放行，也不是要求逐盒闭环。
- 某访视只有在首次访视完成、且该访视发出的每个药盒都有可复核终态时才结清；
- 合法终态：已回收、已销毁、已换发（旧盒须有温控超限证据）。销毁/换发本身就是
  依据，不强制先回收，否则销毁与换发永远锁不了库；
- 只有 DISPENSE 而没有终态的药盒记为“手中”，访视不结清；
- 证据字段为空的回收/销毁/超限一律阻断；
- 换发未登记旧盒、旧盒无发药记录，都按去向缺失阻断；
- 同一回传重试（同事件号同内容，或同业务键同内容）只计一次盒；
- 任何无法归到合法受试者/访视的证据行进入交接隔离，保留原始行。

裁决结果同时供方案版本、访视完成、偏离与锁库使用。
"""
from collections import defaultdict
from app.coerce import instant
from app.errors import BusinessError
from app.modules.protocol.rules import choose_protocol

ACTIONS = ('DISPENSE', 'RETURN', 'DESTROY', 'EXCURSION', 'REPLACE')
PRIORITY = [
    'REPLACEMENT_OLD_BOX_MISSING', 'CHAIN_MISSING', 'REPLACEMENT_WITHOUT_EXCURSION',
    'DESTROY_WITHOUT_EVIDENCE', 'RETURN_WITHOUT_EVIDENCE', 'EXCURSION_WITHOUT_EVIDENCE',
    'BOX_CONFLICT', 'DISPENSE_PROTOCOL_MISMATCH', 'BOX_IN_HAND', 'NO_DISPENSE',
]
SHARED_DETAIL = {
    'MISSING_PRIMARY': '未完成首次访视，发药责任无完成访视可挂靠',
    'DUPLICATE_PRIMARY': '首次访视重复，发药责任无法唯一挂靠',
    'WINDOW_EARLY': '首次访视早于窗口，发药责任不结清',
    'WINDOW_LATE': '首次访视晚于窗口，发药责任不结清',
}


def _text(row, key):
    value = row.get(key, '')
    return value.strip() if isinstance(value, str) else ('' if value is None else str(value).strip())


def _canonical(row):
    return tuple(_text(row, k) for k in ('subject', 'visit', 'box', 'action', 'at',
                                          'evidence', 'replaces', 'protocol'))


def _quarantine(state, row, code, detail):
    state.setdefault('dispense_quarantine', []).append({
        'line': row.get('_line', 0),
        'id': _text(row, 'event_id') or _text(row, 'box'),
        'code': code,
        'detail': detail,
        'raw': {k: v for k, v in row.items() if not k.startswith('_')},
    })


def _dedupe(state):
    """回传重试去重：先按事件号，再按业务键；内容不一致才算冲突。

    被去重的行进入 retries 证据集，归档保留，但不参与计盒。
    """
    rows = state.get('dispense_events') or []
    counts = state.setdefault('dispense_counts', {'input': 0, 'blank': 0,
                                                  'repeated_header': 0, 'retransmission': 0})
    retries = state.setdefault('dispense_retries', [])
    by_event = defaultdict(list)
    for row in rows:
        by_event[_text(row, 'event_id')].append(row)
    accepted, conflicts = [], []
    for event_id in sorted(by_event, key=lambda e: (by_event[e][0].get('_line', 0), e)):
        group = by_event[event_id]
        if len({_canonical(r) for r in group}) > 1:
            conflicts.extend((r, 'DUPLICATE_CONFLICT', '同一事件号存在不同原值') for r in group)
        else:
            accepted.append(group[0])
            retries.extend(dict(r, _reason='同事件号同内容') for r in group[1:])
            counts['retransmission'] += len(group) - 1
    by_business = defaultdict(list)
    for row in accepted:
        # 业务键含时刻与凭证：同一药盒的多次超限/多次交接收不同事件，只有完全同值才是重试。
        by_business[(_text(row, 'subject'), _text(row, 'visit'), _text(row, 'box'),
                     _text(row, 'action'), _text(row, 'at'), _text(row, 'evidence'),
                     _text(row, 'replaces'))].append(row)
    unique = []
    for key in sorted(by_business, key=lambda k: (by_business[k][0].get('_line', 0), k)):
        group = by_business[key]
        if len({_canonical(r) for r in group}) > 1:
            conflicts.extend((r, 'DUPLICATE_CONFLICT', '同一药盒动作存在不同原值') for r in group)
        else:
            unique.append(group[0])
            retries.extend(dict(r, _reason='同业务键同内容') for r in group[1:])
            counts['retransmission'] += len(group) - 1
    for row, code, detail in conflicts:
        _quarantine(state, row, code, detail)
    return unique


def _validate(state, row, context):
    subject, visit = _text(row, 'subject'), _text(row, 'visit')
    box, action = _text(row, 'box'), _text(row, 'action').upper()
    if subject not in context['subjects']:
        _quarantine(state, row, 'UNKNOWN_SUBJECT', '受试者未入组'); return None
    if visit not in context['required_visits']:
        _quarantine(state, row, 'UNKNOWN_VISIT', '访视不在方案规定内'); return None
    if action not in ACTIONS:
        _quarantine(state, row, 'BAD_ACTION', '交接动作无效'); return None
    if not box:
        _quarantine(state, row, 'MISSING_BOX', '药盒号缺失'); return None
    try:
        at = instant(_text(row, 'at'))
    except BusinessError:
        _quarantine(state, row, 'BAD_TIME', '交接时刻缺少有效偏移'); return None
    if at < instant(context['subjects'][subject]):
        _quarantine(state, row, 'INVALID_RANGE', '交接早于入组'); return None
    try:
        chosen = choose_protocol(subject, at, context)
    except BusinessError as error:
        _quarantine(state, row, error.code, error.message); return None
    declared = _text(row, 'protocol') or 'auto'
    if declared not in {'auto', chosen}:
        _quarantine(state, row, 'DISPENSE_PROTOCOL_MISMATCH',
                    f'交接时点为{chosen}，记录申报{declared}'); return None
    return {'event_id': _text(row, 'event_id'), 'subject': subject, 'visit': visit,
            'box': box, 'action': action, 'at': at.isoformat(),
            'evidence': _text(row, 'evidence'), 'replaces': _text(row, 'replaces'),
            'protocol': chosen, 'line': row.get('_line', 0)}


def _box_state(box):
    """返回 (去向, 阻断原因[(code, detail, line)])。"""
    reasons = []
    if box['replaced_by']:
        state = '已换发'
        if not box['excursion_ok']:
            reasons.append(('REPLACEMENT_WITHOUT_EXCURSION', '旧盒换发但无温控超限证据',
                            box['replace_line']))
    elif box['destroys'] and box['returns']:
        state = '已销毁'
        reasons.append(('BOX_CONFLICT', '同一药盒既有回收又有销毁证据', box['destroys'][0]['line']))
    elif box['destroys']:
        state = '已销毁'
        if any(not e['evidence'] for e in box['destroys']):
            reasons.append(('DESTROY_WITHOUT_EVIDENCE', '中心销毁缺少销毁证据',
                            next(e['line'] for e in box['destroys'] if not e['evidence'])))
    elif box['returns']:
        state = '已回收'
        if any(not e['evidence'] for e in box['returns']):
            reasons.append(('RETURN_WITHOUT_EVIDENCE', '回收缺少回收证据',
                            next(e['line'] for e in box['returns'] if not e['evidence'])))
    else:
        state = '手中'
        reasons.append(('BOX_IN_HAND', '药盒仍在受试者手中，未回收、销毁或换发', box['line']))
    if box['excursions'] and not box['replaced_by'] and any(not e['evidence'] for e in box['excursions']):
        reasons.append(('EXCURSION_WITHOUT_EVIDENCE', '温控超限缺少超限证据',
                        next(e['line'] for e in box['excursions'] if not e['evidence'])))
    return state, reasons


def _new_box(box_id, event):
    return {'box': box_id, 'subject': event['subject'], 'visit': event['visit'],
            'dispense': event, 'origin': event['action'], 'line': event['line'],
            'returns': [], 'destroys': [], 'excursions': [], 'replaced_by': [],
            'excursion_ok': False, 'replace_line': 0}


def adjudicate(state):
    context = state['context']
    events = []
    for row in _dedupe(state):
        event = _validate(state, row, context)
        if event is not None:
            events.append(event)

    boxes, visit_issues = {}, defaultdict(list)
    for event in events:
        if event['action'] not in {'DISPENSE', 'REPLACE'}:
            continue
        existing = boxes.get(event['box'])
        if existing is not None:
            if (existing['subject'], existing['visit']) != (event['subject'], event['visit']):
                _quarantine(state, {'event_id': event['event_id'], 'box': event['box'],
                                    'line': event['line'], 'action': event['action']},
                            'BOX_CONFLICT', '同一药盒出现在不同受试者或访视')
                visit_issues[(event['subject'], event['visit'])].append(
                    ('BOX_CONFLICT', '药盒号被其他受试者或访视占用', event['line']))
            else:
                # 同盒同访视再次发药：回传重试已在去重阶段合并，能到这里的只能是新证据冲突。
                _quarantine(state, {'event_id': event['event_id'], 'box': event['box'],
                                    'line': event['line'], 'action': event['action']},
                            'DUPLICATE_CONFLICT', '同一药盒在同一访视重复发药且凭证不同')
                visit_issues[(event['subject'], event['visit'])].append(
                    ('BOX_CONFLICT', '同一药盒在同一访视重复发药', event['line']))
            continue
        boxes[event['box']] = _new_box(event['box'], event)

    orphan_refs = {}
    # 先落回收/销毁/超限证据，再落换发链，避免回传行序影响“旧盒是否有超限证据”的判定。
    ordered = ([e for e in events if e['action'] != 'REPLACE'] +
               [e for e in events if e['action'] == 'REPLACE'])
    for event in ordered:
        action = event['action']
        if action in {'RETURN', 'DESTROY', 'EXCURSION'}:
            box = boxes.get(event['box'])
            if box is None:
                orphan_refs.setdefault(event['box'], event)
                visit_issues[(event['subject'], event['visit'])].append(
                    ('CHAIN_MISSING', f'药盒{event["box"]}被引用但没有发药记录', event['line']))
                continue
            {'RETURN': box['returns'], 'DESTROY': box['destroys'],
             'EXCURSION': box['excursions']}[action].append(event)
        elif action == 'REPLACE':
            if not event['replaces']:
                visit_issues[(event['subject'], event['visit'])].append(
                    ('REPLACEMENT_OLD_BOX_MISSING', '换发未登记被替换的旧盒', event['line']))
                continue
            old = boxes.get(event['replaces'])
            if old is None:
                orphan_refs.setdefault(event['replaces'], event)
                visit_issues[(event['subject'], event['visit'])].append(
                    ('CHAIN_MISSING', f'被替换旧盒{event["replaces"]}没有发药记录', event['line']))
                continue
            if old['subject'] != event['subject']:
                visit_issues[(event['subject'], event['visit'])].append(
                    ('BOX_CONFLICT', '换发跨受试者引用', event['line']))
                continue
            old['replaced_by'].append(event['box'])
            old['replace_line'] = event['line']
            old['excursion_ok'] = bool(event['evidence']) or any(
                e['evidence'] for e in old['excursions'])
            orphan_refs.pop(event['replaces'], None)

    visit_protocol = {}
    for row in state.get('windows', []):
        if row['kind'] == 'PRIMARY':
            visit_protocol.setdefault((row['subject'], row['visit']), row['selected_protocol'])
    completed = {(r['subject'], r['visit']) for r in state.get('completed', [])}
    shared_code = {}
    for row in state.get('deviations', []):
        shared_code.setdefault((row['subject'], row['visit']), row['code'])

    verdicts, deviations = [], []
    for subject in sorted(context['subjects']):
        for visit in context['required_visits']:
            key = (subject, visit)
            protocol = visit_protocol.get(key)
            own = sorted((b for b in boxes.values() if (b['subject'], b['visit']) == key),
                         key=lambda b: (b['dispense']['at'], b['box']))
            reasons = list(visit_issues.get(key, []))
            chain, resolution, box_id = [], '', ''
            covered = set()
            for base in own:
                if base['box'] in covered:
                    continue
                position, current, terminal = [], base, ''
                while True:
                    position.append(current['box'])
                    covered.add(current['box'])
                    state_of, box_reasons = _box_state(current)
                    reasons.extend(box_reasons)
                    terminal = state_of
                    if not current['replaced_by']:
                        break
                    nxt = boxes.get(current['replaced_by'][0])
                    if nxt is None:
                        reasons.append(('CHAIN_MISSING', '换发链在下游断开', base['replace_line']))
                        break
                    if nxt['box'] in position:
                        reasons.append(('BOX_CONFLICT', '换发链成环', nxt['line']))
                        break
                    current = nxt
                if not chain:
                    chain, box_id, resolution = position, base['box'], terminal
                if base['dispense']['protocol'] and protocol and \
                        base['dispense']['protocol'] != protocol:
                    reasons.append(('DISPENSE_PROTOCOL_MISMATCH',
                                    f'发药时点为{base["dispense"]["protocol"]}，该访视方案为{protocol}',
                                    base['line']))

            if key not in completed:
                code = shared_code.get(key, 'MISSING_PRIMARY')
                verdicts.append(_verdict(subject, visit, protocol, False, box_id, resolution,
                                         ','.join(chain), False, code,
                                         SHARED_DETAIL.get(code, '访视未完成，发药责任不结清'),
                                         True, reasons))
                continue
            if not own:
                reasons.append(('NO_DISPENSE', '该访视没有发药记录', 0))
            if not reasons:
                verdicts.append(_verdict(subject, visit, protocol, True, box_id, resolution,
                                         ','.join(chain), True, '', '', False, []))
                continue
            reasons.sort(key=lambda r: PRIORITY.index(r[0]) if r[0] in PRIORITY else len(PRIORITY))
            code, detail, line = reasons[0][0], reasons[0][1], (reasons[0][2] if len(reasons[0]) > 2 else 0)
            verdicts.append(_verdict(subject, visit, protocol, True, box_id, resolution,
                                     ','.join(chain), False, code, detail, False, reasons))
            deviations.append({'subject': subject, 'visit': visit, 'id': box_id,
                               'code': 'DISPENSE_' + code, 'detail': detail,
                               'line': line, 'box': box_id, 'protocol': protocol})

    box_rows = []
    for box in sorted(boxes.values(), key=lambda b: (b['subject'], b['visit'], b['box'])):
        state_of, reasons = _box_state(box)
        box_rows.append({'box': box['box'], 'subject': box['subject'], 'visit': box['visit'],
                         'state': state_of, 'line': box['line'],
                         'evidence': box['dispense']['evidence'],
                         'reason': '；'.join(r[1] for r in reasons)})
    for box_id, event in sorted(orphan_refs.items(), key=lambda item: item[1]['line']):
        box_rows.append({'box': box_id, 'subject': event['subject'], 'visit': event['visit'],
                         'state': '去向缺失', 'line': event['line'],
                         'evidence': event['evidence'], 'reason': '被引用但没有发药记录'})

    def total(state_name):
        return sum(1 for b in box_rows if b['state'] == state_name)

    returned, destroyed, replaced, in_hand = (total('已回收'), total('已销毁'),
                                              total('已换发'), total('手中'))
    missing = total('去向缺失')
    dispensed = len(boxes)
    accounted = returned + destroyed + replaced + in_hand
    # 总量守恒不等于逐盒齐全：被引用却没有发药记录的药盒同样让对账失败。
    reconcile = dispensed == accounted and missing == 0
    blocked_visits = sum(1 for v in verdicts if not v['settled'])
    file_ok = not state.get('dispense_file_issue')
    return {
        'verdicts': verdicts,
        'boxes': box_rows,
        'deviations': deviations,
        'events': sorted(events, key=lambda e: (e['line'], e['event_id'])),
        'retries': sorted(({k: v for k, v in r.items() if not k.startswith('_')}
                           for r in state.get('dispense_retries', [])),
                          key=lambda r: r.get('event_id', '')),
        'quarantine': state.get('dispense_quarantine', []),
        'counts': state.get('dispense_counts', {}),
        'encoding': state.get('dispense_encoding'),
        'file_issue': state.get('dispense_file_issue'),
        'expected_visits': len(context['subjects']) * len(context['required_visits']),
        'settled_visits': len(verdicts) - blocked_visits,
        'blocked_visits': blocked_visits,
        'shared_blocked_visits': sum(1 for v in verdicts if not v['settled'] and v['shared']),
        'own_blocked_visits': sum(1 for v in verdicts if not v['settled'] and not v['shared']),
        'totals': {'dispensed': dispensed, 'returned': returned, 'destroyed': destroyed,
                   'replaced': replaced, 'in_hand': in_hand,
                   'missing': missing, 'accounted': accounted,
                   'reconcile': reconcile},
        'status': '可放行' if file_ok and blocked_visits == 0 and not state.get('dispense_quarantine')
                  else '阻断',
    }


def _verdict(subject, visit, protocol, completed, box, resolution, chain, settled,
             code, detail, shared, reasons):
    return {'subject': subject, 'visit': visit, 'protocol': protocol,
            'completed': completed, 'box': box, 'resolution': resolution,
            'chain': chain, 'settled': settled, 'code': code, 'detail': detail,
            'shared': shared,
            'reasons': [{'code': r[0], 'detail': r[1], 'line': r[2] if len(r) > 2 else 0}
                        for r in reasons]}


def _display_verdict(verdict):
    return {'subject': verdict['subject'], 'visit': verdict['visit'],
            'protocol': verdict['protocol'] or '',
            'completed': '是' if verdict['completed'] else '否',
            'box': verdict['box'], 'chain': verdict['chain'],
            'resolution': verdict['resolution'],
            'settled': '已结清' if verdict['settled'] else '阻断',
            'code': verdict['code'], 'detail': verdict['detail']}


def _display_deviation(row):
    return {'subject': row.get('subject', ''), 'visit': row.get('visit', ''),
            'id': row.get('id', ''), 'code': row.get('code', ''),
            'detail': row.get('detail', ''), 'line': row.get('line', row.get('_line', 0))}


def apply(state):
    result = adjudicate(state)
    state['dispense'] = result
    state.setdefault('views', {})['dispense'] = {
        'title': '药物交接',
        'rows': [_display_verdict(v) for v in result['verdicts']],
        'boxes': result['boxes'],
        'deviations': result['deviations'],
        'quarantine': result['quarantine'],
        'retries': result['retries'],
        'metrics': {'计划访视': result['expected_visits'],
                    '已结清': result['settled_visits'],
                    '阻断访视': result['blocked_visits'],
                    '在手中': result['totals']['in_hand']},
    }
    settlement = {(v['subject'], v['visit']): v for v in result['verdicts']}
    # 方案版本页与访视页共用同一结算结论，避免各页各算一套。
    protocol_view = state.get('views', {}).get('protocol')
    if protocol_view:
        for row in protocol_view['rows']:
            verdict = settlement.get((row['subject'], row['visit']))
            if verdict:
                row['settled'] = '已结清' if verdict['settled'] else '未结清'
                row['resolution'] = verdict['resolution']
    windows_view = state.get('views', {}).get('windows')
    if windows_view:
        for row in windows_view['rows']:
            verdict = settlement.get((row['subject'], row['visit']))
            if verdict:
                row['settled'] = '已结清' if verdict['settled'] else '未结清'
    # 偏离页展示药盒级问题；锁库按 handle 结论单独计数，避免同一访视重复阻断。
    deviation_view = state.get('views', {}).get('deviation')
    if deviation_view:
        deviations = list(state.get('deviations', []))
        deviations.extend(result['deviations'])
        deviation_view['rows'] = [_display_deviation(row) for row in deviations]
        deviation_view['metrics'] = {'完成访视': len(state.get('completed', [])),
                                     '缺失或偏离': len(deviations),
                                     '缺首次访视': sum(x.get('code') == 'MISSING_PRIMARY'
                                                    for x in deviations)}
