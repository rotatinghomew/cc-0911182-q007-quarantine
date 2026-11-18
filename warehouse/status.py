"""运营核对视图：记录为何可用/不可用、批次逐项判定与原始总数。"""
import json

from .decisions import RECORD_ID_COLUMN
from .storage import canonical
from .tables import TABLES


def package_report(connection, batch_id):
    """返回某批次的逐项判定计数；total_rows 即原包记录总数，可核对部分成功。"""
    row = connection.execute('SELECT summary_json FROM batch_reports WHERE batch_id=?',
                             [batch_id]).fetchone()
    if row is None:
        raise ValueError('批次不存在：' + batch_id)
    return json.loads(row[0])


def explain_unavailable(connection, day=None):
    """列出所有当前不可分发的业务标识及其原因分类。"""
    result = []
    for table, (_, _) in TABLES.items():
        id_column = RECORD_ID_COLUMN[table]
        result.extend(_explain_table(connection, table, id_column, day))
    return sorted(result, key=lambda item: (item['table'], item['partner'], item['record_id']))


def _explain_table(connection, table, id_column, day):
    items = []
    revoked_keys = {row[0] for row in connection.execute(
        'SELECT row_key FROM revocations WHERE table_name=?', [table]).fetchall()}
    business_keys = {row[0] for row in connection.execute(
        'SELECT row_key FROM source_rows WHERE table_name=?', [table]).fetchall()}
    for key, partner, record_id, state, reason, payload_json in connection.execute(
            'SELECT row_key,partner,record_id,state,reason,payload FROM pending_rows '
            'WHERE table_name=? ORDER BY row_key', [table]).fetchall():
        if key in business_keys:
            # 已入仓标识的隔离重送只保留事件，不改变当前可用性
            continue
        payload = json.loads(payload_json)
        items.append({
            'table': table, 'partner': partner, 'record_id': record_id,
            'status': state, 'self_error': reason if state == 'invalid' else None,
            'waiting_on': _waiting_parent(connection, table, payload),
            'blocked_by': None, 'payload': payload,
        })
    for key, payload_json in connection.execute(
            'SELECT row_key,payload FROM source_rows WHERE table_name=? ORDER BY row_key', [table]).fetchall():
        payload = json.loads(payload_json)
        if key in revoked_keys:
            items.append({
                'table': table, 'partner': payload['partner'],
                'record_id': payload[id_column], 'status': 'revoked',
                'self_error': None, 'waiting_on': None,
                'blocked_by': _revocation_chain(connection, table, key), 'payload': payload,
            })
            continue
        blocker = _ancestor_revocation(connection, table, payload)
        if blocker is not None:
            items.append({
                'table': table, 'partner': payload['partner'],
                'record_id': payload[id_column], 'status': 'blocked',
                'self_error': None, 'waiting_on': None, 'blocked_by': blocker, 'payload': payload,
            })
        elif table == 'programs' and not payload['active']:
            items.append({
                'table': table, 'partner': payload['partner'],
                'record_id': payload[id_column], 'status': 'inactive',
                'self_error': None, 'waiting_on': None, 'blocked_by': None, 'payload': payload,
            })
        elif table == 'grants' and day is not None and not (
                payload['valid_from'] <= day < payload['valid_to']):
            items.append({
                'table': table, 'partner': payload['partner'],
                'record_id': payload[id_column], 'status': 'outside_window',
                'self_error': None, 'waiting_on': None, 'blocked_by': None, 'payload': payload,
            })
    return items


def record_status(connection, table, partner, record_id):
    """单个标识的完整状态：当前位置、自身错误、关联等待、阻断来源、判定历史。"""
    if table not in TABLES:
        raise ValueError('未知数据表：' + table)
    key = canonical([partner, record_id])
    id_column = RECORD_ID_COLUMN[table]
    pending = connection.execute(
        'SELECT state,reason,payload,batch_id FROM pending_rows WHERE table_name=? AND row_key=?',
        [table, key]).fetchone()
    source = connection.execute(
        'SELECT payload,batch_id FROM source_rows WHERE table_name=? AND row_key=?', [table, key]).fetchone()
    revocation = connection.execute(
        'SELECT reason,batch_id FROM revocations WHERE table_name=? AND row_key=?', [table, key]).fetchone()
    history = [
        {'seq': seq, 'batch_id': batch_id, 'action': action, 'verdict': verdict, 'reason': reason}
        for seq, batch_id, action, verdict, reason in connection.execute(
            'SELECT seq,batch_id,action,verdict,reason FROM row_events '
            'WHERE table_name=? AND row_key=? ORDER BY seq', [table, key]).fetchall()
    ]
    if revocation:
        payload = json.loads(source[0]) if source else None
        return {'table': table, 'partner': partner, 'record_id': record_id, 'status': 'revoked',
                'self_error': None, 'waiting_on': None,
                'blocked_by': _revocation_chain(connection, table, key),
                'payload': payload, 'history': history}
    if source:
        payload = json.loads(source[0])
        blocker = _ancestor_revocation(connection, table, payload)
        if blocker is not None:
            status, blocked_by = 'blocked', blocker
        elif table == 'programs' and not payload['active']:
            status, blocked_by = 'inactive', None
        else:
            status, blocked_by = 'accepted', None
        # 已入仓标识若又收到隔离重送，附带最近一次隔离原因，但不改变其可用事实
        latest_pending = connection.execute(
            'SELECT state,reason FROM pending_rows WHERE table_name=? AND row_key=?',
            [table, key]).fetchone()
        return {'table': table, 'partner': partner, 'record_id': record_id, 'status': status,
                'self_error': latest_pending[1] if latest_pending and latest_pending[0] == 'invalid' else None,
                'waiting_on': None, 'blocked_by': blocked_by,
                'payload': payload, 'history': history}
    if pending:
        state, reason, payload_json, batch_id = pending
        payload = json.loads(payload_json)
        return {'table': table, 'partner': partner, 'record_id': record_id, 'status': state,
                'self_error': reason if state == 'invalid' else None,
                'waiting_on': _waiting_parent(connection, table, payload),
                'blocked_by': None, 'payload': payload, 'history': history}
    return {'table': table, 'partner': partner, 'record_id': record_id, 'status': 'unknown',
            'self_error': None, 'waiting_on': None, 'blocked_by': None,
            'payload': None, 'history': history}


def _waiting_parent(connection, table, payload):
    from .decisions import dependency_of
    dependency = dependency_of(table, payload)
    if dependency is None:
        return None
    parent_table, parent_key = dependency
    parent = connection.execute(
        'SELECT state,reason FROM pending_rows WHERE table_name=? AND row_key=?',
        [parent_table, parent_key]).fetchone()
    if parent:
        return {'table': parent_table, 'row_key': parent_key,
                'state': parent[0], 'reason': parent[1]}
    if connection.execute('SELECT 1 FROM revocations WHERE table_name=? AND row_key=?',
                          [parent_table, parent_key]).fetchone():
        return {'table': parent_table, 'row_key': parent_key, 'state': 'revoked', 'reason': None}
    return {'table': parent_table, 'row_key': parent_key, 'state': 'missing', 'reason': None}


def _ancestor_revocation(connection, table, payload):
    from .decisions import dependency_of
    seen = set()
    current_table, current_row = table, payload
    while True:
        dependency = dependency_of(current_table, current_row)
        if dependency is None:
            return None
        parent_table, parent_key = dependency
        if parent_key in seen:
            return None
        seen.add(parent_key)
        revoked = connection.execute(
            'SELECT partner,record_id FROM revocations WHERE table_name=? AND row_key=?',
            [parent_table, parent_key]).fetchone()
        if revoked:
            return {'table': parent_table, 'partner': revoked[0], 'record_id': revoked[1]}
        source = connection.execute(
            'SELECT payload FROM source_rows WHERE table_name=? AND row_key=?',
            [parent_table, parent_key]).fetchone()
        if source is None:
            return None
        current_table, current_row = parent_table, json.loads(source[0])


def _revocation_chain(connection, table, key):
    row = connection.execute(
        'SELECT partner,record_id FROM revocations WHERE table_name=? AND row_key=?',
        [table, key]).fetchone()
    return {'table': table, 'partner': row[0], 'record_id': row[1]}
