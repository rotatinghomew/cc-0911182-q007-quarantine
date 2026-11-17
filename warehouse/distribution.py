import json
from .supply import available
from .storage import canonical


def publish(connection,release,day):
    prior=connection.execute('SELECT business_day,rows_json FROM distribution_releases WHERE release_id=?',[release]).fetchone()
    if prior:
        if prior[0]!=day:
            raise ValueError('分发版本对应不同日期')
        return json.loads(prior[1])
    rows=available(connection,day)
    connection.execute('INSERT INTO distribution_releases VALUES (?,?,?)',[release,day,canonical(rows)])
    return rows


def read_release(connection,release):
    row=connection.execute('SELECT rows_json FROM distribution_releases WHERE release_id=?',[release]).fetchone()
    if row is None:
        raise ValueError('分发版本不存在')
    return json.loads(row[0])
