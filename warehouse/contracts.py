import re
from datetime import date
from .checks import text


def normalize(table,original):
    row=dict(original)
    for field,value in row.items():
        if field in {'active','duration'}:
            continue
        text(value)
    if table=='programs' and type(row['active']) is not bool:
        raise ValueError('节目有效状态应为布尔值')
    if table=='editions':
        if not re.fullmatch('[a-z]{2}(?:-[A-Z]{2})?',row['language']):
            raise ValueError('语种标识无效')
        if type(row['duration']) is not int or row['duration']<=0:
            raise ValueError('内容时长无效')
    if table=='grants':
        row['valid_from']=date.fromisoformat(row['valid_from']).isoformat()
        row['valid_to']=date.fromisoformat(row['valid_to']).isoformat()
        if row['valid_from']>=row['valid_to']:
            raise ValueError('授权范围为空')
    return row
