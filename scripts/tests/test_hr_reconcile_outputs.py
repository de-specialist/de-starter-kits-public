"""Regression checks for the HR semantic-model reconciliation tool."""
import contextlib
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2] / 'migration/templates/hr-analytics'
spec = importlib.util.spec_from_file_location('hr_reconcile', ROOT / 'reconcile_outputs.py')
reconcile = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reconcile)

EXPECTED = (
    'period,kpi,value\n'
    '2024-01,headcount,10\n'
    '2024-01,turnover_rate,0.125\n'
    '2024-02,headcount,11\n'
)


class HRReconcileOutputsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.expected = self.write('expected.csv', EXPECTED)

    def write(self, name, text):
        path = self.root / name
        path.write_text(text, encoding='utf-8')
        return path

    def run_main(self, actual_text):
        actual = self.write('actual.csv', actual_text)
        argv = ['reconcile_outputs.py', str(actual), '--expected', str(self.expected)]
        output = io.StringIO()
        with mock.patch('sys.argv', argv), contextlib.redirect_stdout(output):
            return reconcile.main(), output.getvalue()

    def test_normalize_period(self):
        cases = {
            '2024-01-31': '2024-01',
            ' 2024-02-29 ': '2024-02',
            '2024-01': '2024-01',
            '2023-02-29': '2023-02-29',
            '2024-13-01': '2024-13-01',
            '2024-01-31T00:00:00': '2024-01-31T00:00:00',
            '20240131': '20240131',
        }
        for raw, want in cases.items():
            with self.subTest(period=raw):
                self.assertEqual(reconcile.normalize_period(raw), want)

    def test_date_periods_reconcile_with_month_keys(self):
        code, output = self.run_main(
            'period,kpi,value\n'
            '2024-01-31,headcount,10\n'
            '2024-01-01,turnover_rate,0.125\n'
            '2024-02,headcount,11\n'
        )
        self.assertEqual(code, 0, output)
        self.assertIn('Reconciled 3 synthetic KPI rows.', output)

    def test_value_mismatch_still_fails_after_normalization(self):
        code, output = self.run_main(
            'period,kpi,value\n'
            '2024-01-31,headcount,12\n'
            '2024-01-31,turnover_rate,0.125\n'
            '2024-02-29,headcount,11\n'
        )
        self.assertEqual(code, 1)
        self.assertIn("2024-01 headcount: value expected '10'; actual '12'", output)

    def test_duplicate_keys_after_normalization_are_rejected(self):
        for periods in (('2024-01-01', '2024-01-31'), ('2024-01', '2024-01-31')):
            with self.subTest(periods=periods):
                actual = (
                    'period,kpi,value\n'
                    f'{periods[0]},headcount,10\n'
                    f'{periods[1]},headcount,10\n'
                    '2024-01,turnover_rate,0.125\n'
                    '2024-02,headcount,11\n'
                )
                with self.assertRaisesRegex(ValueError, 'duplicate period/KPI key'):
                    self.run_main(actual)

    def test_missing_kpi_remains_a_failure(self):
        code, output = self.run_main(
            'period,kpi,value\n'
            '2024-01-31,headcount,10\n'
            '2024-02-29,headcount,11\n'
        )
        self.assertEqual(code, 1)
        self.assertIn('2024-01 turnover_rate: missing actual row', output)

    def test_extra_kpi_remains_a_failure(self):
        code, output = self.run_main(
            'period,kpi,value\n'
            '2024-01-31,headcount,10\n'
            '2024-01-31,turnover_rate,0.125\n'
            '2024-02-29,headcount,11\n'
            '2024-02-29,unlisted_kpi,1\n'
        )
        self.assertEqual(code, 1)
        self.assertIn('2024-02 unlisted_kpi: unexpected actual row', output)

    def test_invalid_date_period_is_not_normalized(self):
        code, output = self.run_main(
            'period,kpi,value\n'
            '2024-01-31,headcount,10\n'
            '2024-01-31,turnover_rate,0.125\n'
            '2024-02-30,headcount,11\n'
        )
        self.assertEqual(code, 1)
        self.assertIn('2024-02 headcount: missing actual row', output)
        self.assertIn('2024-02-30 headcount: unexpected actual row', output)


if __name__ == '__main__':
    unittest.main()
