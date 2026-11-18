import argparse
import json
from pathlib import Path

from .export import export_tables
from .ingest import ingest
from .queries import overview
from .status import explain_unavailable, package_report, record_status
from .storage import open_store


def main():
    parser = argparse.ArgumentParser(description='本地仓库日常接收与核对')
    parser.add_argument('--database', default='warehouse.duckdb')
    commands = parser.add_subparsers(dest='action', required=True)
    load = commands.add_parser('load', help='接收一个业务包（可携带 corrections/revocations）')
    load.add_argument('file', type=Path)
    commands.add_parser('overview', help='查看现有业务汇总')
    report = commands.add_parser('report', help='核对某批次的逐项判定与原包总数')
    report.add_argument('batch_id')
    status = commands.add_parser('status', help='查看单个标识为何可用/不可用')
    status.add_argument('table')
    status.add_argument('partner')
    status.add_argument('record_id')
    commands.add_parser('unavailable', help='列出所有不可分发记录及原因')
    output = commands.add_parser('export', help='冻结列式导出')
    output.add_argument('destination', type=Path)
    output.add_argument('revision')
    args = parser.parse_args()
    connection = open_store(args.database)
    try:
        if args.action == 'load':
            result = ingest(connection, json.loads(args.file.read_text(encoding='utf-8')))
        elif args.action == 'export':
            result = export_tables(connection, args.destination, args.revision)
        elif args.action == 'report':
            result = package_report(connection, args.batch_id)
        elif args.action == 'status':
            result = record_status(connection, args.table, args.partner, args.record_id)
        elif args.action == 'unavailable':
            result = explain_unavailable(connection)
        else:
            result = overview(connection)
        print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    finally:
        connection.close()


if __name__ == '__main__':
    main()
