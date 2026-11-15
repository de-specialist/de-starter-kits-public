"""Regression checks for the public HR fixture contract."""
import contextlib
import importlib.util
import io
from pathlib import Path
import shutil
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2] / 'migration/templates/hr-analytics'
spec = importlib.util.spec_from_file_location('hr_fixtures', ROOT / 'validate_fixtures.py')
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)


class HRFixtureContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'hr'
        shutil.copytree(ROOT, self.root)
        previous = validator.ROOT
        validator.ROOT = self.root
        self.addCleanup(setattr, validator, 'ROOT', previous)

    def validate(self):
        with contextlib.redirect_stdout(io.StringIO()):
            validator.main()

    def test_every_input_rejects_missing_and_unapproved_headers(self):
        for path in (self.root / 'fixtures/input').glob('*.csv'):
            original = path.read_text()
            header, rest = original.split('\n', 1)
            for field in ('employee_key', 'requisition_key', 'extract_id'):
                if field in header.split(','):
                    break
            else:
                self.fail(f'no known field to mutate in {path.name}')
            renamed = ','.join('wrong_key' if column == field else column for column in header.split(','))
            for changed in (header.replace(',', ',unapproved_column,', 1), renamed):
                with self.subTest(source=path.stem, header=changed):
                    path.write_text(changed + '\n' + rest)
                    with self.assertRaisesRegex(AssertionError, 'input header mismatch'):
                        self.validate()
            path.write_text(original)

    def mutate_manifest(self, transform):
        path = self.root / 'fixtures/input/export_manifest.csv'
        lines = path.read_text().splitlines()
        path.write_text('\n'.join(transform(lines)) + '\n')

    def test_export_manifest_header_order_is_exact(self):
        def swap(lines):
            columns = lines[0].split(',')
            columns[0], columns[1] = columns[1], columns[0]
            return [','.join(columns)] + [','.join([r.split(',')[1], r.split(',')[0]] + r.split(',')[2:]) for r in lines[1:]]
        self.mutate_manifest(swap)
        with self.assertRaisesRegex(AssertionError, 'input header mismatch: export_manifest.csv'):
            self.validate()

    def test_export_manifest_rejects_invalid_records(self):
        first = 'employee_snapshot,MOCK-EMPLOYEE-2032-09,2032-09-30T23:59:59Z,2032-09-01,2032-09-30,2032-09-30,true,3,complete_authored_synthetic_fixture'
        cases = {
            'unknown source': ('employee_snapshot,MOCK-EMPLOYEE-2032-09', 'export_manifest,MOCK-EMPLOYEE-2032-09', 'unknown source_name'),
            'non-synthetic id': ('MOCK-EMPLOYEE-2032-09', 'EMPLOYEE-2032-09', 'non-mock identifier'),
            'duplicate id': ('MOCK-EMPLOYEE-2032-10', 'MOCK-EMPLOYEE-2032-09', 'duplicate extract_id'),
            'local timestamp': ('2032-09-30T23:59:59Z,2032-09-01', '2032-09-30T23:59:59+01:00,2032-09-01', 'extracted_at_utc'),
            'impossible timestamp': ('2032-09-30T23:59:59Z,2032-09-01', '2032-09-31T23:59:59Z,2032-09-01', 'extracted_at_utc'),
            'non-ISO start': (',2032-09-01,', ',01/09/2032,', 'covered_start_date'),
            'impossible end': (',2032-09-01,2032-09-30,', ',2032-09-01,2032-09-31,', 'covered_end_date'),
            'inverted coverage': (',2032-09-01,2032-09-30,', ',2032-10-01,2032-09-30,', 'coverage ends before it starts'),
            'month end outside coverage': (',2032-09-01,2032-09-30,', ',2032-09-01,2032-09-29,', 'snapshot_month_end outside coverage'),
            'non-boolean completeness': (',true,3,', ',yes,3,', 'is_complete_snapshot'),
            'negative row count': (',true,3,', ',true,-3,', 'row_count must be a nonnegative integer'),
            'row count drift': (',true,3,', ',true,4,', 'does not match 3 fixture rows'),
            'blank evidence': (',3,complete_authored_synthetic_fixture', ',3,', 'completeness_evidence is required'),
            'row-count evidence': (',3,complete_authored_synthetic_fixture', ',3,row_count_matches', 'independent of the row count'),
            'empty evidence on rows': (',3,complete_authored_synthetic_fixture', ',3,complete_empty_owner_attestation', 'non-empty snapshot'),
        }
        path = self.root / 'fixtures/input/export_manifest.csv'
        original = path.read_text()
        self.assertIn(first, original)
        for name, (old, new, message) in cases.items():
            with self.subTest(case=name):
                self.assertIn(old, first if old in first else original)
                changed = original.replace(first, first.replace(old, new)) if old in first else original.replace(old, new, 1)
                self.assertNotEqual(changed, original)
                path.write_text(changed)
                with self.assertRaisesRegex(AssertionError, message):
                    self.validate()
        path.write_text(original)

    def test_export_manifest_requires_one_record_per_snapshot_period(self):
        path = self.root / 'fixtures/input/export_manifest.csv'
        original = path.read_text()
        lines = original.splitlines()
        path.write_text('\n'.join(lines[:-1]) + '\n')
        with self.assertRaisesRegex(AssertionError, 'missing required snapshot records'):
            self.validate()
        path.write_text(original + lines[-1].replace('MOCK-LEAVE-2032-10', 'MOCK-LEAVE-2032-10-B') + '\n')
        with self.assertRaisesRegex(AssertionError, 'duplicate source/period record'):
            self.validate()

    def test_zero_row_snapshot_requires_complete_empty_evidence(self):
        leave = self.root / 'fixtures/input/leave_balance_snapshot.csv'
        leave.write_text('\n'.join(line for line in leave.read_text().splitlines() if not line.startswith('2032-09-30')) + '\n')
        with self.assertRaisesRegex(AssertionError, 'does not match 0 fixture rows'):
            self.validate()
        record = 'leave_balance_snapshot,MOCK-LEAVE-2032-09,2032-09-30T23:59:59Z,2032-09-01,2032-09-30,2032-09-30,true,1,complete_authored_synthetic_fixture'
        path = self.root / 'fixtures/input/export_manifest.csv'
        original = path.read_text()
        self.assertIn(record, original)
        zero = record.replace(',true,1,', ',true,0,')
        for name, changed in {
            'no attestation': zero,
            'incomplete': zero.replace(',true,0,complete_authored_synthetic_fixture', ',false,0,complete_empty_owner_attestation'),
        }.items():
            with self.subTest(case=name):
                path.write_text(original.replace(record, changed))
                with self.assertRaisesRegex(AssertionError, 'zero-row snapshot requires explicit complete-empty evidence'):
                    self.validate()
        path.write_text(original.replace(record, zero.replace('complete_authored_synthetic_fixture', 'complete_empty_owner_attestation')))
        self.validate()

    def test_edge_identifiers_are_checked_recursively(self):
        path = self.root / 'fixtures/edge_cases.yml'
        original = path.read_text()
        for field in ('key', 'episode_id', 'record_key', 'employee_key', 'requisition_key', 'entry_type_code'):
            with self.subTest(field=field):
                path.write_text(original + f'\nextra_synthetic_case:\n  nested:\n    - {field}: invalid\n')
                with self.assertRaisesRegex(AssertionError, 'edge fixture identifier'):
                    self.validate()
        path.write_text(original)

    def test_nonabsence_time_entries_do_not_change_absence_kpis(self):
        self.validate()
        path = self.root / 'fixtures/input/time_entry.csv'
        path.write_text(path.read_text().replace(',2.00,', ',999.00,').replace(',7.00,', ',888.00,'))
        self.validate()


if __name__ == '__main__':
    unittest.main()
