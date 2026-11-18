
import copy
import json
import tempfile
import unittest
from pathlib import Path

from warehouse.export import export_tables, read_export
from warehouse.ingest import ingest
from warehouse.queries import overview
from warehouse.status import explain_unavailable, package_report, record_status
from warehouse.storage import open_store


class WarehouseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.connection = open_store(self.root / 'store.duckdb')
        self.payload = json.loads(Path('samples/delivery.json').read_text())

    def tearDown(self):
        self.connection.close()
        self.temp.cleanup()

    def load(self, payload=None):
        return ingest(self.connection, copy.deepcopy(payload or self.payload))

    def test_replay_keeps_source_rows(self):
        first = self.load()
        second = self.load()
        self.assertGreater(first['inserted'], 0)
        self.assertTrue(second['replayed'])
        self.assertEqual(self.connection.execute('SELECT count(*) FROM receipts').fetchone()[0], 1)
        self.assertEqual(self.connection.execute('SELECT count(*) FROM row_events').fetchone()[0], 3)

    def test_batch_collision_is_atomic(self):
        self.load()
        collision = copy.deepcopy(self.payload)
        collision['tables'] = {}
        with self.assertRaises(ValueError):
            ingest(self.connection, collision)
        self.assertEqual(self.connection.execute('SELECT count(*) FROM receipts').fetchone()[0], 1)

    def test_unknown_table_does_not_write_receipt(self):
        invalid = {'batch_id': 'bad', 'tables': {'missing_table': []}}
        with self.assertRaises(ValueError):
            ingest(self.connection, invalid)
        self.assertEqual(self.connection.execute('SELECT count(*) FROM receipts').fetchone()[0], 0)

    def test_different_batch_same_rows_is_not_duplicate(self):
        first = self.load()
        duplicate = copy.deepcopy(self.payload)
        duplicate['batch_id'] = 'retry-another-envelope'
        result = ingest(self.connection, duplicate)
        self.assertTrue(result['replayed'])
        self.assertEqual(self.connection.execute('SELECT count(*) FROM source_rows').fetchone()[0],
                         first['inserted'])

    def test_export_roundtrip_and_hash(self):
        self.load()
        result = export_tables(self.connection, self.root / 'exports', 'release1')
        for table, entry in result['tables'].items():
            actual = read_export(self.connection, self.root / 'exports', 'release1', table)
            self.assertEqual(len(actual), entry['rows'])
            self.assertEqual(set(actual[0]) if actual else set(),
                             set(self.payload['tables'][table][0]) if actual else set())
        table = next(iter(result['tables']))
        output = self.root / 'exports' / 'release1' / (table + '.parquet')
        output.write_bytes(output.read_bytes() + b'broken')
        with self.assertRaises(ValueError):
            read_export(self.connection, self.root / 'exports', 'release1', table)

    def test_export_names_and_immutability(self):
        self.load()
        with self.assertRaises(ValueError):
            export_tables(self.connection, self.root, '../escape')
        export_tables(self.connection, self.root, 'stable')
        with self.assertRaises(FileExistsError):
            export_tables(self.connection, self.root, 'stable')

    def test_reopen_preserves_data(self):
        self.load()
        expected = overview(self.connection)
        self.connection.close()
        self.connection = open_store(self.root / 'store.duckdb')
        self.assertEqual(overview(self.connection), expected)

    def test_invalid_row_is_quarantined_but_siblings_enter(self):
        # 包内含一条非法版本，但同包节目与另一条合法版本仍可入仓
        package = {
            'batch_id': 'partial-1',
            'tables': {
                'programs': [
                    {'partner': 'media-a', 'program_id': 'p1', 'title': '海上乐途', 'active': True},
                    {'partner': 'media-a', 'program_id': 'p2', 'title': '山谷回声', 'active': True},
                ],
                'editions': [
                    {'partner': 'media-a', 'edition_id': 'e-bad', 'program_id': 'p1',
                     'language': 'invalid', 'duration': 180},
                    {'partner': 'media-a', 'edition_id': 'e-good', 'program_id': 'p2',
                     'language': 'zh', 'duration': 200},
                ],
                'grants': [],
            },
        }
        report = self.load(package)['report']
        self.assertEqual(report['total_rows'], 4)
        self.assertEqual(report['accepted'], 3)
        self.assertEqual(report['invalid'], 1)
        self.assertEqual(self.connection.execute(
            "SELECT state FROM pending_rows WHERE table_name='editions' AND record_id='e-bad'").fetchone()[0],
            'invalid')
        # 合法节目/版本进入业务表，非法版本未进
        self.assertEqual(self.connection.execute('SELECT count(*) FROM editions').fetchone()[0], 1)
        self.assertEqual(self.connection.execute('SELECT count(*) FROM programs').fetchone()[0], 2)
        # 原包原始内容与判定依据保留
        original = self.connection.execute(
            "SELECT payload,verdict FROM row_events WHERE batch_id='partial-1' AND table_name='editions' "
            "AND verdict='invalid'").fetchall()
        self.assertIn('invalid', original[0][0])
        self.assertEqual(original[0][1], 'invalid')

    def test_grant_waiting_for_missing_edition_is_not_accepted(self):
        # 授权引用了不存在的版本：即使授权自身字段合格也只能等待，不能算成功
        package = {
            'batch_id': 'dangling-1',
            'tables': {
                'programs': [],
                'editions': [],
                'grants': [
                    {'partner': 'media-a', 'grant_id': 'g-lonely', 'edition_id': 'ghost',
                     'region': 'FR', 'valid_from': '2026-11-01', 'valid_to': '2026-12-01'},
                ],
            },
        }
        report = self.load(package)['report']
        self.assertEqual(report['accepted'], 0)
        self.assertEqual(report['waiting'], 1)
        self.assertEqual(self.connection.execute('SELECT count(*) FROM grants').fetchone()[0], 0)
        status = record_status(self.connection, 'grants', 'media-a', 'g-lonely')
        self.assertEqual(status['status'], 'waiting')
        self.assertEqual(status['waiting_on']['table'], 'editions')
        self.assertEqual(status['waiting_on']['state'], 'missing')

    def test_correction_releases_waiting_chain_without_resending_package(self):
        # 非法版本挡住授权 -> 修正版本 -> 版本与授权依次释放进入集市
        package = {
            'batch_id': 'chain-1',
            'tables': {
                'programs': [
                    {'partner': 'media-a', 'program_id': 'p1', 'title': '海上乐途', 'active': True},
                ],
                'editions': [
                    {'partner': 'media-a', 'edition_id': 'e1', 'program_id': 'p1',
                     'language': 'bad', 'duration': 180},
                ],
                'grants': [
                    {'partner': 'media-a', 'grant_id': 'g1', 'edition_id': 'e1',
                     'region': 'FR', 'valid_from': '2026-11-01', 'valid_to': '2026-12-01'},
                ],
            },
        }
        first = self.load(package)['report']
        self.assertEqual((first['accepted'], first['invalid'], first['waiting']), (1, 1, 1))
        from warehouse.supply import available
        self.assertEqual(available(self.connection, '2026-11-15'), [])

        fix = {
            'batch_id': 'fix-e1',
            'tables': {},
            'corrections': {
                'editions': [
                    {'partner': 'media-a', 'edition_id': 'e1', 'program_id': 'p1',
                     'language': 'zh', 'duration': 180},
                ],
            },
        }
        fix_report = self.load(fix)['report']
        self.assertEqual(fix_report['accepted'], 2)  # 修正版本 + 释放的授权
        rows = available(self.connection, '2026-11-15')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['grant_id'], 'g1')
        self.assertEqual(self.connection.execute('SELECT count(*) FROM pending_rows').fetchone()[0], 0)
        # 历史包的原始非法内容与判定仍保留，未被覆盖
        original = self.connection.execute(
            "SELECT payload,verdict FROM row_events WHERE batch_id='chain-1' AND table_name='editions'"
        ).fetchall()
        self.assertIn('bad', original[0][0])
        self.assertEqual(original[0][1], 'invalid')

    def test_duplicate_correction_is_idempotent(self):
        self.load()
        correction = {
            'batch_id': 'fix-dup', 'tables': {},
            'corrections': {
                'programs': [
                    {'partner': 'media-a', 'program_id': 'p1', 'title': '海上乐途', 'active': True},
                ],
            },
        }
        first = self.load(correction)
        second = self.load(copy.deepcopy(correction))
        self.assertTrue(second['replayed'])
        self.assertEqual(first['report']['replayed'], 1)
        self.assertEqual(self.connection.execute('SELECT count(*) FROM programs').fetchone()[0], 1)
        # 同一修正信封整体重放
        self.assertEqual(self.connection.execute('SELECT count(*) FROM receipts').fetchone()[0], 2)

    def test_revocation_stops_dependents_but_keeps_history(self):
        self.load()
        revoke = {
            'batch_id': 'revoke-e1', 'tables': {},
            'revocations': {
                'editions': [{'partner': 'media-a', 'edition_id': 'e1', 'reason': '合作方撤版'}],
            },
        }
        report = self.load(revoke)['report']
        self.assertEqual(report['revoked'], 1)
        self.assertEqual(report['blocked'], 1)  # 授权 g1 连带阻断
        from warehouse.supply import available
        self.assertEqual(available(self.connection, '2026-11-15'), [])
        # 业务行仍是历史事实，未被删除
        self.assertEqual(self.connection.execute('SELECT count(*) FROM editions').fetchone()[0], 1)
        self.assertEqual(self.connection.execute('SELECT count(*) FROM grants').fetchone()[0], 1)
        grant_status = record_status(self.connection, 'grants', 'media-a', 'g1')
        self.assertEqual(grant_status['status'], 'blocked')
        self.assertEqual(grant_status['blocked_by']['table'], 'editions')
        # 重复送达同一撤销不产生重复
        again = self.load(copy.deepcopy(revoke))
        self.assertTrue(again['replayed'])
        self.assertEqual(self.connection.execute('SELECT count(*) FROM revocations').fetchone()[0], 1)

    def test_supply_package_partial_acceptance_state(self):
        from warehouse.supply import receive_package, available
        # 第二个包用新标识：非法版本挡住其授权，同包合法节目仍发布
        broken = {
            'programs': [
                {'partner': 'media-a', 'program_id': 'p2', 'title': '山谷回声', 'active': True},
            ],
            'editions': [
                {'partner': 'media-a', 'edition_id': 'e2', 'program_id': 'p2',
                 'language': 'invalid', 'duration': 180},
            ],
            'grants': [
                {'partner': 'media-a', 'grant_id': 'g2', 'edition_id': 'e2',
                 'region': 'FR', 'valid_from': '2026-11-01', 'valid_to': '2026-12-01'},
            ],
        }
        receive_package(self.connection, 'pkg1', 'media-a', copy.deepcopy(self.payload['tables']))
        self.assertEqual(len(available(self.connection, '2026-11-15')), 1)
        result = receive_package(self.connection, 'pkg2', 'media-a', broken)
        self.assertEqual(result['state'], 'partially_accepted')
        self.assertEqual(result['report']['accepted'], 1)   # 只有节目 p2 入仓
        self.assertEqual(result['report']['invalid'], 1)    # 非法版本 e2
        self.assertEqual(result['report']['waiting'], 1)    # 授权 g2 等待
        # g2 不进集市；首个合法包的分发不受影响
        rows = available(self.connection, '2026-11-15')
        self.assertEqual([row['grant_id'] for row in rows], ['g1'])
        self.assertEqual(self.connection.execute(
            "SELECT count(*) FROM programs WHERE program_id='p2'").fetchone()[0], 1)

    def test_invalid_redelivery_does_not_overwrite_valid_record(self):
        from warehouse.supply import available
        self.load()
        # 用相同业务键重送一条非法版本：既有合法事实保留，非法内容隔离
        bad = copy.deepcopy(self.payload)
        bad['batch_id'] = 'redelivery-bad'
        bad['tables']['editions'][0]['language'] = 'invalid'
        report = self.load(bad)['report']
        self.assertEqual(report['invalid'], 1)
        self.assertEqual(len(available(self.connection, '2026-11-15')), 1)
        status = record_status(self.connection, 'editions', 'media-a', 'e1')
        self.assertEqual(status['status'], 'accepted')

    def test_grant_boundary_is_exclusive(self):
        from warehouse.supply import available
        self.load()
        self.assertEqual(available(self.connection, '2026-12-01'), [])

    def test_package_partner_boundary(self):
        from warehouse.supply import receive_package
        with self.assertRaises(ValueError):
            receive_package(self.connection, 'pkg1', 'another', self.payload['tables'])

    def test_distribution_version_is_frozen(self):
        from warehouse.distribution import publish, read_release
        self.load()
        first = publish(self.connection, 'r1', '2026-11-15')
        self.assertEqual(read_release(self.connection, 'r1'), first)
        with self.assertRaises(ValueError):
            publish(self.connection, 'r1', '2026-12-15')

    def test_same_identifier_across_partners_is_isolated(self):
        # 同一标识在不同媒体出现：按供稿方隔离，不串改
        other = copy.deepcopy(self.payload)
        other['batch_id'] = 'other-media'
        for table_rows in other['tables'].values():
            for row in table_rows:
                row['partner'] = 'media-b'
        self.load()
        self.load(other)
        self.assertEqual(self.connection.execute('SELECT count(*) FROM programs').fetchone()[0], 2)
        status_a = record_status(self.connection, 'programs', 'media-a', 'p1')
        status_b = record_status(self.connection, 'programs', 'media-b', 'p1')
        self.assertEqual(status_a['status'], 'accepted')
        self.assertEqual(status_b['status'], 'accepted')
        # 撤销 media-b 的版本不影响 media-a 的分发
        self.load({'batch_id': 'revoke-b', 'tables': {},
                   'revocations': {'editions': [{'partner': 'media-b', 'edition_id': 'e1'}]}})
        from warehouse.supply import available
        rows = available(self.connection, '2026-11-15')
        self.assertEqual([row['partner'] for row in rows], ['media-a'])

    def test_final_state_matches_clean_one_shot_import(self):
        # 带错误投递 -> 修正 -> 撤销无关项后的结果，与一次性正确导入合法修正等价
        broken = {
            'batch_id': 'messy-1',
            'tables': {
                'programs': [
                    {'partner': 'media-a', 'program_id': 'p1', 'title': '旧标题', 'active': True},
                    {'partner': 'media-a', 'program_id': 'p2', 'title': '将撤销', 'active': True},
                ],
                'editions': [
                    {'partner': 'media-a', 'edition_id': 'e1', 'program_id': 'p1',
                     'language': 'xx', 'duration': 180},
                ],
                'grants': [
                    {'partner': 'media-a', 'grant_id': 'g1', 'edition_id': 'e1',
                     'region': 'FR', 'valid_from': '2026-11-01', 'valid_to': '2026-12-01'},
                ],
            },
        }
        self.load(broken)
        self.load({'batch_id': 'fix-1', 'tables': {},
                   'corrections': {'programs': [
                       {'partner': 'media-a', 'program_id': 'p1', 'title': '海上乐途', 'active': True}],
                       'editions': [
                           {'partner': 'media-a', 'edition_id': 'e1', 'program_id': 'p1',
                            'language': 'zh', 'duration': 180}]}})
        self.load({'batch_id': 'revoke-1', 'tables': {},
                   'revocations': {'programs': [{'partner': 'media-a', 'program_id': 'p2'}]}})

        clean_store = open_store(self.root / 'clean.duckdb')
        clean = {
            'batch_id': 'one-shot',
            'tables': {
                'programs': [
                    {'partner': 'media-a', 'program_id': 'p1', 'title': '海上乐途', 'active': True},
                    {'partner': 'media-a', 'program_id': 'p2', 'title': '将撤销', 'active': True},
                ],
                'editions': [
                    {'partner': 'media-a', 'edition_id': 'e1', 'program_id': 'p1',
                     'language': 'zh', 'duration': 180},
                ],
                'grants': [
                    {'partner': 'media-a', 'grant_id': 'g1', 'edition_id': 'e1',
                     'region': 'FR', 'valid_from': '2026-11-01', 'valid_to': '2026-12-01'},
                ],
            },
            'revocations': {'programs': [{'partner': 'media-a', 'program_id': 'p2'}]},
        }
        ingest(clean_store, clean)
        from warehouse.supply import available
        self.assertEqual(available(self.connection, '2026-11-15'),
                         available(clean_store, '2026-11-15'))
        for table in ('programs', 'editions', 'grants'):
            self.assertEqual(
                self.connection.execute('SELECT * FROM "' + table + '" ORDER BY 1,2').fetchall(),
                clean_store.execute('SELECT * FROM "' + table + '" ORDER BY 1,2').fetchall())
        clean_store.close()

    def test_package_report_total_matches_original_count(self):
        package = {
            'batch_id': 'count-1',
            'tables': {
                'programs': [
                    {'partner': 'media-a', 'program_id': 'p1', 'title': '海上乐途', 'active': True},
                ],
                'editions': [
                    {'partner': 'media-a', 'edition_id': 'e-bad', 'program_id': 'p1',
                     'language': 'nope', 'duration': 180},
                ],
                'grants': [
                    {'partner': 'media-a', 'grant_id': 'g1', 'edition_id': 'e-bad',
                     'region': 'FR', 'valid_from': '2026-11-01', 'valid_to': '2026-12-01'},
                ],
            },
        }
        self.load(package)
        report = package_report(self.connection, 'count-1')
        self.assertEqual(report['total_rows'], 3)
        self.assertEqual(report['accepted'] + report['invalid'] + report['waiting'], 3)
        reasons = {item['record_id']: item for item in explain_unavailable(self.connection)}
        self.assertEqual(reasons['e-bad']['status'], 'invalid')
        self.assertIsNotNone(reasons['e-bad']['self_error'])
        self.assertEqual(reasons['g1']['status'], 'waiting')
        self.assertEqual(reasons['g1']['waiting_on']['table'], 'editions')


if __name__ == '__main__':
    unittest.main()
