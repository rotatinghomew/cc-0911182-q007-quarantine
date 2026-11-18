from .status import explain_unavailable
from .storage import records
from .supply import available


def overview(connection, day='2026-11-15'):
    return {
        'distribution': available(connection, day),
        'packages': records(connection,
                            'SELECT package_id,partner,state FROM supply_packages ORDER BY package_id'),
        'unavailable': explain_unavailable(connection, day),
        'pending': records(connection,
                           'SELECT table_name,partner,record_id,state,reason,batch_id '
                           'FROM pending_rows ORDER BY table_name,row_key'),
        'revocations': records(connection,
                               'SELECT table_name,partner,record_id,batch_id,reason '
                               'FROM revocations ORDER BY table_name,row_key'),
    }
