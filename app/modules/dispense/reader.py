import csv, io

FIELDS = ['event_id', 'subject', 'visit', 'box', 'action', 'at', 'evidence', 'replaces', 'protocol']
ALIASES = {'事件号': 'event_id', '受试者': 'subject', '访视': 'visit', '药盒': 'box',
           '动作': 'action', '时刻': 'at', '证据': 'evidence', '替换旧盒': 'replaces', '方案': 'protocol'}


def file_issue(state, code, detail):
    """交接回传层面的证据缺陷不当成整批失败：保留原文并让裁决阻断。"""
    state['dispense_file_issue'] = {'code': code, 'detail': detail}
    state.setdefault('dispense_quarantine', []).append(
        {'line': 0, 'id': '', 'code': code, 'detail': detail, 'raw': {}})


def read_dispense(state):
    raw = state.get('dispense_raw_bytes') or b''
    state['dispense_quarantine'] = []
    state['dispense_counts'] = {'input': 0, 'blank': 0, 'repeated_header': 0, 'retransmission': 0}
    state['dispense_events'] = []
    state['dispense_retries'] = []
    state['dispense_file_issue'] = None
    if not raw:
        file_issue(state, 'EMPTY_FILE', '未提供药物交接回传')
        state['dispense_encoding'] = None
        return
    if not raw.strip(b'\xef\xbb\xbf\r\n \t'):
        file_issue(state, 'BLANK_FILE', '药物交接文件只有空行')
        state['dispense_encoding'] = None
        return
    text = None
    for encoding in ('utf-8-sig', 'gb18030'):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        file_issue(state, 'BAD_ENCODING', '药物交接文件编码无法识别')
        state['dispense_encoding'] = None
        return
    state['dispense_encoding'] = encoding
    stream = csv.reader(io.StringIO(text, newline=''), strict=True)
    try:
        original = next(stream)
        header = [ALIASES.get(v.strip(), v.strip()) for v in original]
        if header != FIELDS:
            file_issue(state, 'BAD_HEADER', '药物交接字段或顺序不匹配')
            return
        events = []
        for cells in stream:
            line = stream.line_num
            state['dispense_counts']['input'] += 1
            if not cells or not any(v.strip() for v in cells):
                state['dispense_counts']['blank'] += 1
                continue
            if cells == original:
                state['dispense_counts']['repeated_header'] += 1
                continue
            row = dict(zip(header, cells))
            row['_line'] = line
            if len(cells) != len(header):
                state['dispense_quarantine'].append({
                    'line': line, 'id': row.get('event_id', ''), 'code': 'COLUMN_COUNT',
                    'detail': f'需要{len(header)}列，实际{len(cells)}列', 'raw': row.copy()})
                continue
            if not row.get('event_id', '').strip():
                state['dispense_quarantine'].append({
                    'line': line, 'id': '', 'code': 'MISSING_ID',
                    'detail': '事件号不能为空', 'raw': row.copy()})
                continue
            row['event_id'] = row['event_id'].strip()
            events.append(row)
    except csv.Error:
        file_issue(state, 'BROKEN_CSV', '药物交接 CSV 引号或末行损坏')
        return
    if not state['dispense_counts']['input']:
        file_issue(state, 'HEADER_ONLY', '药物交接文件只有表头')
        return
    state['dispense_events'] = events
