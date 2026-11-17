from .supply import available
from .storage import records


def overview(connection):
    return {'distribution':available(connection,'2026-11-15'),'packages':records(connection,'SELECT package_id,partner,state FROM supply_packages ORDER BY package_id')}
