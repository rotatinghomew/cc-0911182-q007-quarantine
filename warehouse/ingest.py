from .decisions import RECORD_ID_COLUMN, admit_row, apply_correction, apply_revocation, release_dependents
from .storage import canonical, digest, transaction
from .tables import TABLES


def _validate_envelope(payload):
    if not isinstance(payload, dict) or not {'batch_id', 'tables'} <= set(payload):
        raise ValueError('报送必须包含批号和数据表')
    if set(payload) - {'batch_id', 'tables', 'corrections', 'revocations'}:
        raise ValueError('报送包含未知字段')
    batch = payload['batch_id']
    if not isinstance(batch, str) or not batch.strip():
        raise ValueError('批号不能为空')
    if not isinstance(payload['tables'], dict):
        raise ValueError('数据表必须为对象')
    corrections = payload.get('corrections', {})
    revocations = payload.get('revocations', {})
    if not isinstance(corrections, dict) or not isinstance(revocations, dict):
        raise ValueError('修正与撤销必须按表组织为对象')
    for name, section in (('corrections', corrections), ('revocations', revocations)):
        unknown = set(section) - set(TABLES)
        if unknown:
            raise ValueError('未知数据表：' + ','.join(sorted(unknown)))
        for rows in section.values():
            if not isinstance(rows, list):
                raise ValueError(name + '记录必须为数组')
    return batch, corrections, revocations


def ingest(connection, payload):
    batch, corrections, revocations = _validate_envelope(payload)
    signature = digest(payload)
    prepared = []
    for table, rows in payload['tables'].items():
        if table not in TABLES:
            raise ValueError('未知数据表：' + table)
        if not isinstance(rows, list):
            raise ValueError('表记录必须为数组')
        for original in rows:
            prepared.append((table, original))

    counts = {'accepted': 0, 'invalid': 0, 'waiting': 0, 'replayed': 0,
              'corrected': 0, 'revoked': 0, 'blocked': 0}
    with transaction(connection):
        prior = connection.execute(
            'SELECT r.signature,b.summary_json FROM receipts r '
            'LEFT JOIN batch_reports b ON r.batch_id=b.batch_id WHERE r.batch_id=?',
            [batch]).fetchone()
        if prior:
            if prior[0] != signature:
                raise ValueError('相同批号内容不一致')
            return {'batch_id': batch, 'replayed': True, 'report': _load_report(connection, prior[1])}

        # 先逐行处理投递：合法行进入集市，自身错误隔离，缺依赖的进入等待
        for table, original in prepared:
            verdict = admit_row(connection, table, original, batch)
            counts[verdict] += 1

        # 合作方按原记录标识发来的修正：补正成功即释放关联等待
        for table, rows in corrections.items():
            for original in rows:
                verdict = apply_correction(connection, table, original, batch)
                counts[verdict] += 1
                if verdict == 'accepted':
                    counts['corrected'] += 1

        # 正式撤销：业务行保留为历史事实，自身与下游停止可用
        for table, rows in revocations.items():
            for marker in rows:
                _check_revocation_marker(marker)
                verdict, blocked = apply_revocation(
                    connection, table, marker['partner'], marker[RECORD_ID_COLUMN[table]],
                    batch, marker.get('reason'))
                counts[verdict] += 1
                counts['blocked'] += blocked

        # 投递阶段后序行补齐父记录、或修正补正后，循环释放所有满足依赖的等待行
        released, released_same_batch = release_dependents(connection, batch)
        counts['accepted'] += released
        counts['waiting'] -= released_same_batch

        total_rows = sum(len(rows) for rows in payload['tables'].values())
        # 新批号但所有内容均已存在（或仅在等待/隔离中重复），整信封视为重放
        is_replay = (counts['accepted'] == 0 and counts['invalid'] == 0 and counts['waiting'] == 0
                     and counts['corrected'] == 0 and counts['revoked'] == 0)
        report = {
            'batch_id': batch,
            'total_rows': total_rows,
            'accepted': counts['accepted'],
            'invalid': counts['invalid'],
            'waiting': counts['waiting'],
            'revoked': counts['revoked'],
            'blocked': counts['blocked'],
            'replayed': counts['replayed'],
            'corrected': counts['corrected'],
        }
        connection.execute('INSERT INTO receipts VALUES (?,?,?)',
                           [batch, signature, counts['accepted']])
        connection.execute('INSERT INTO batch_reports VALUES (?,?,?,?,?,?,?,?)',
                           [batch, total_rows, counts['accepted'], counts['invalid'],
                            counts['waiting'], counts['revoked'], counts['blocked'], canonical(report)])
    return {'batch_id': batch, 'inserted': counts['accepted'], 'replayed': is_replay, 'report': report}


def _check_revocation_marker(marker):
    if not isinstance(marker, dict) or 'partner' not in marker:
        raise ValueError('撤销标记必须包含供稿方与记录标识')
    if 'reason' in marker and not isinstance(marker['reason'], str):
        raise ValueError('撤销原因必须为文本')


def _load_report(connection, summary_json):
    import json
    return json.loads(summary_json)
