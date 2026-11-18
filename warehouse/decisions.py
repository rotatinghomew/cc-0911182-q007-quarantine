"""逐行判定与部分入仓生命周期。

记录去向：
- 进入业务表（programs/editions/grants）：字段合格且父依赖当前可关联；
- pending_rows.state='invalid'：记录自身字段错误或业务键冲突，不进集市；
- pending_rows.state='waiting'：字段合格但引用的父记录尚未可关联；
- revocations：正式撤销；业务行保留为历史事实但停止可用，其下游依赖一并阻断。

所有判定只追加写入 row_events，历史包原始内容与判定依据不被覆盖。
"""
import json

from .contracts import normalize
from .storage import canonical
from .tables import TABLES

RECORD_ID_COLUMN = {'programs': 'program_id', 'editions': 'edition_id', 'grants': 'grant_id'}
PARENT_COLUMN = {'editions': 'program_id', 'grants': 'edition_id'}
CHILD_TABLE = {'programs': 'editions', 'editions': 'grants'}


def key_for(table, row):
    _, keys = TABLES[table]
    return canonical([row[key] for key in keys])


def dependency_of(table, row):
    """返回 (父表名, 父业务键)；programs 无父依赖。"""
    if table not in PARENT_COLUMN:
        return None
    parent_table = 'programs' if table == 'editions' else 'editions'
    return parent_table, canonical([row['partner'], row[PARENT_COLUMN[table]]])


def _key_match(table):
    return ' AND '.join('"' + column + '"=?' for column in TABLES[table][1])


def _key_params(table, key):
    return json.loads(key)


def _in_business(connection, table, key):
    return connection.execute(
        'SELECT 1 FROM "' + table + '" WHERE ' + _key_match(table), _key_params(table, key)).fetchone() is not None


def _is_revoked(connection, table, key):
    return connection.execute(
        'SELECT 1 FROM revocations WHERE table_name=? AND row_key=?', [table, key]).fetchone() is not None


def _parent_available(connection, table, row):
    dependency = dependency_of(table, row)
    if dependency is None:
        return True
    parent_table, parent_key = dependency
    return (_in_business(connection, parent_table, parent_key)
            and not _is_revoked(connection, parent_table, parent_key))


def _append_event(connection, batch_id, table, key, action, verdict, payload, reason=None):
    connection.execute(
        'INSERT INTO row_events VALUES (nextval(\'row_events_seq\'),?,?,?,?,?,?,?)',
        [batch_id, table, key, action, verdict, reason, canonical(payload)])


def _insert_business(connection, table, row, batch_id):
    columns, _ = TABLES[table]
    names = ','.join('"' + column + '"' for column in columns)
    marks = ','.join('?' for _ in columns)
    connection.execute('INSERT INTO "' + table + '" (' + names + ') VALUES (' + marks + ')',
                       [row[column] for column in columns])
    connection.execute('INSERT OR REPLACE INTO source_rows VALUES (?,?,?,?)',
                       [table, key_for(table, row), canonical(row), batch_id])


def _put_pending(connection, table, row, batch_id, state, reason):
    key = key_for(table, row)
    connection.execute('INSERT OR REPLACE INTO pending_rows VALUES (?,?,?,?,?,?,?,?)',
                       [table, key, row['partner'], row[RECORD_ID_COLUMN[table]],
                        batch_id, state, reason, canonical(row)])


def _all_source_rows(connection, table):
    return [(row[0], json.loads(row[1])) for row in connection.execute(
        'SELECT row_key,payload FROM source_rows WHERE table_name=?', [table]).fetchall()]


def blocked_dependents(connection, table, key):
    """父记录撤销时，仍在业务表中被连带阻断的下游记录（版本/授权）数量。"""
    chain = [(table, key)]
    total = 0
    while chain:
        parent_table, parent_key = chain.pop(0)
        child_table = CHILD_TABLE.get(parent_table)
        if child_table is None:
            continue
        parent = next((payload for row_key, payload in _all_source_rows(connection, parent_table)
                       if row_key == parent_key), None)
        if parent is None:
            continue
        id_column = RECORD_ID_COLUMN[parent_table]
        for child_key, child in _all_source_rows(connection, child_table):
            if child['partner'] != parent['partner'] or child[PARENT_COLUMN[child_table]] != parent[id_column]:
                continue
            if _in_business(connection, child_table, child_key):
                total += 1
                chain.append((child_table, child_key))
    return total


def admit_row(connection, table, original, batch_id, action='deliver'):
    """判定一条投递记录，返回判定字符串。"""
    columns, keys = TABLES[table]
    if not isinstance(original, dict) or set(original) != set(columns):
        reason = '记录字段与契约不符：' + table
        fallback = original if isinstance(original, dict) else {}
        key = canonical([fallback.get('partner'), fallback.get(RECORD_ID_COLUMN[table])])
        connection.execute('INSERT OR REPLACE INTO pending_rows VALUES (?,?,?,?,?,?,?,?)',
                           [table, key, fallback.get('partner'), fallback.get(RECORD_ID_COLUMN[table]),
                            batch_id, 'invalid', reason, canonical(original)])
        _append_event(connection, batch_id, table, key, action, 'invalid', original, reason)
        return 'invalid'
    try:
        row = normalize(table, original)
    except ValueError as exc:
        _put_pending(connection, table, original, batch_id, 'invalid', str(exc))
        _append_event(connection, batch_id, table, key_for(table, original), action, 'invalid', original, str(exc))
        return 'invalid'
    key = key_for(table, row)
    if _is_revoked(connection, table, key):
        reason = '业务标识已被正式撤销'
        _put_pending(connection, table, row, batch_id, 'invalid', reason)
        _append_event(connection, batch_id, table, key, action, 'invalid', row, reason)
        return 'invalid'
    if not _parent_available(connection, table, row):
        reason = '依赖的父记录尚不可关联'
        _put_pending(connection, table, row, batch_id, 'waiting', reason)
        _append_event(connection, batch_id, table, key, action, 'waiting', row, reason)
        return 'waiting'
    if _in_business(connection, table, key):
        existing = connection.execute('SELECT payload FROM source_rows WHERE table_name=? AND row_key=?',
                                      [table, key]).fetchone()[0]
        if existing != canonical(row):
            reason = '业务主键内容冲突：' + table
            _put_pending(connection, table, row, batch_id, 'invalid', reason)
            _append_event(connection, batch_id, table, key, action, 'invalid', row, reason)
            return 'invalid'
        _append_event(connection, batch_id, table, key, action, 'replayed', row)
        return 'replayed'
    _insert_business(connection, table, row, batch_id)
    _append_event(connection, batch_id, table, key, action, 'accepted', row)
    return 'accepted'


def release_dependents(connection, batch_id):
    """父记录补齐后，循环重新判定等待中的依赖。

    返回 (释放总数, 其中属于本批次的等待行数)。
    """
    released = 0
    same_batch = 0
    progressed = True
    while progressed:
        progressed = False
        waiting = connection.execute(
            'SELECT table_name,row_key,payload,batch_id FROM pending_rows WHERE state=\'waiting\' '
            'ORDER BY table_name,row_key').fetchall()
        for table, key, payload_json, origin_batch in waiting:
            row = json.loads(payload_json)
            if _is_revoked(connection, table, key) or not _parent_available(connection, table, row):
                continue
            normalized = normalize(table, row)
            if _in_business(connection, table, key):
                connection.execute('DELETE FROM pending_rows WHERE table_name=? AND row_key=?', [table, key])
            else:
                _insert_business(connection, table, normalized, origin_batch)
                connection.execute('DELETE FROM pending_rows WHERE table_name=? AND row_key=?', [table, key])
            _append_event(connection, batch_id, table, key, 'release', 'accepted', normalized)
            released += 1
            if origin_batch == batch_id:
                same_batch += 1
            progressed = True
    return released, same_batch


def apply_correction(connection, table, original, batch_id):
    """合作方按原记录标识补正；成功后释放原来被挡住的关联记录。返回判定字符串。"""
    columns, keys = TABLES[table]
    if not isinstance(original, dict) or set(original) != set(columns):
        reason = '修正记录字段与契约不符：' + table
        fallback = original if isinstance(original, dict) else {}
        key = canonical([fallback.get('partner'), fallback.get(RECORD_ID_COLUMN[table])])
        _append_event(connection, batch_id, table, key, 'correct', 'invalid', original, reason)
        if isinstance(original, dict):
            connection.execute('INSERT OR REPLACE INTO pending_rows VALUES (?,?,?,?,?,?,?,?)',
                               [table, key, fallback.get('partner'), fallback.get(RECORD_ID_COLUMN[table]),
                                batch_id, 'invalid', reason, canonical(original)])
        return 'invalid'
    try:
        row = normalize(table, original)
    except ValueError as exc:
        _put_pending(connection, table, original, batch_id, 'invalid', str(exc))
        _append_event(connection, batch_id, table, key_for(table, original), 'correct', 'invalid', original, str(exc))
        return 'invalid'
    key = key_for(table, row)
    if not _parent_available(connection, table, row):
        reason = '修正后依赖的父记录仍不可关联'
        _put_pending(connection, table, row, batch_id, 'waiting', reason)
        _append_event(connection, batch_id, table, key, 'correct', 'waiting', row, reason)
        return 'waiting'
    if _in_business(connection, table, key):
        current = connection.execute('SELECT payload FROM source_rows WHERE table_name=? AND row_key=?',
                                     [table, key]).fetchone()[0]
        if current == canonical(row):
            _append_event(connection, batch_id, table, key, 'correct', 'replayed', row)
            return 'replayed'
        sets = ','.join('"' + column + '"=?' for column in columns if column not in keys)
        params = [row[column] for column in columns if column not in keys]
        connection.execute('UPDATE "' + table + '" SET ' + sets + ' WHERE ' + _key_match(table),
                           params + [row[k] for k in keys])
        connection.execute('UPDATE source_rows SET payload=?,batch_id=? WHERE table_name=? AND row_key=?',
                           [canonical(row), batch_id, table, key])
        _append_event(connection, batch_id, table, key, 'correct', 'accepted', row)
        return 'accepted'
    pending = connection.execute('SELECT payload FROM pending_rows WHERE table_name=? AND row_key=?',
                                 [table, key]).fetchone()
    if pending and pending[0] == canonical(row):
        _append_event(connection, batch_id, table, key, 'correct', 'replayed', row)
        return 'replayed'
    _insert_business(connection, table, row, batch_id)
    connection.execute('DELETE FROM pending_rows WHERE table_name=? AND row_key=?', [table, key])
    _append_event(connection, batch_id, table, key, 'correct', 'accepted', row)
    return 'accepted'


def apply_revocation(connection, table, partner, record_id, batch_id, reason=None):
    """正式撤销；返回 ('revoked'|'replayed', 连带阻断的下游记录数)。"""
    key = canonical([partner, record_id])
    if _is_revoked(connection, table, key):
        existing = connection.execute(
            'SELECT reason FROM revocations WHERE table_name=? AND row_key=?', [table, key]).fetchone()
        if existing[0] != reason:
            raise ValueError('撤销重复送达且原因不一致')
        _append_event(connection, batch_id, table, key, 'revoke', 'replayed',
                      {'partner': partner, 'record_id': record_id}, reason)
        return 'replayed', 0
    if not _in_business(connection, table, key) and connection.execute(
            'SELECT 1 FROM pending_rows WHERE table_name=? AND row_key=?', [table, key]).fetchone() is None:
        raise ValueError('撤销目标不存在：' + table)
    blocked = blocked_dependents(connection, table, key)
    payload_row = connection.execute('SELECT payload FROM source_rows WHERE table_name=? AND row_key=?',
                                     [table, key]).fetchone()
    payload = json.loads(payload_row[0]) if payload_row else {'partner': partner, 'record_id': record_id}
    connection.execute('INSERT INTO revocations VALUES (?,?,?,?,?,?)',
                       [table, key, partner, record_id, batch_id, reason])
    connection.execute('DELETE FROM pending_rows WHERE table_name=? AND row_key=?', [table, key])
    _append_event(connection, batch_id, table, key, 'revoke', 'revoked', payload, reason)
    return 'revoked', blocked
