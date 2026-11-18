import json

from .ingest import ingest
from .storage import canonical, records, transaction

_AVAILABLE_SQL = (
    'SELECT g.partner,g.grant_id,e.edition_id,p.program_id,p.title,e.language,g.region '
    'FROM grants g JOIN editions e ON g.partner=e.partner AND g.edition_id=e.edition_id '
    'JOIN programs p ON e.partner=p.partner AND e.program_id=p.program_id '
    'WHERE p.active AND g.valid_from<=? AND ?<g.valid_to '
    'AND NOT EXISTS (SELECT 1 FROM revocations r WHERE '
    '(r.table_name=\'programs\' AND r.partner=p.partner AND r.record_id=p.program_id) '
    'OR (r.table_name=\'editions\' AND r.partner=e.partner AND r.record_id=e.edition_id) '
    'OR (r.table_name=\'grants\' AND r.partner=g.partner AND r.record_id=g.grant_id)) '
    'ORDER BY g.partner,g.grant_id'
)


def receive_package(connection, package_id, partner, tables):
    payload = {'batch_id': package_id, 'tables': tables}
    body = canonical(payload)
    existing = connection.execute('SELECT body,state FROM supply_packages WHERE package_id=?',
                                  [package_id]).fetchone()
    if existing:
        if existing[0] != body:
            raise ValueError('供稿包标识重复且内容不同')
        return {'package_id': package_id, 'state': existing[1]}
    for rows in tables.values():
        for row in rows:
            if row.get('partner') != partner:
                raise ValueError('供稿方与记录归属不一致')
    with transaction(connection):
        connection.execute('INSERT INTO supply_packages VALUES (?,?,?,?)',
                           [package_id, partner, body, 'received'])
    try:
        result = ingest(connection, payload)
    except ValueError:
        connection.execute('UPDATE supply_packages SET state=? WHERE package_id=?', ['rejected', package_id])
        raise
    report = result['report']
    state = 'accepted' if report['invalid'] == 0 and report['waiting'] == 0 else 'partially_accepted'
    connection.execute('UPDATE supply_packages SET state=? WHERE package_id=?', [state, package_id])
    return {'package_id': package_id, 'state': state, 'rows': result['inserted'], 'report': report}


def available(connection, day):
    return records(connection, _AVAILABLE_SQL, [day, day])
