"""Offline synthetic contract checks; does not execute dbt, DAX or source adapters.

Requires PyYAML. Edge identity resolution models an approved stable key/revision
assumption only; production resolution remains blocked until those exist.
"""
import csv
import re
from datetime import date, datetime
from pathlib import Path
from statistics import median

import yaml

ROOT = Path(__file__).resolve().parent


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def load_yaml(path):
    return yaml.safe_load(path.read_text())


def resolve(rows, key):
    winners = {}
    versions = {}
    for row in rows:
        if not row[key]:
            return None
        identity = (row[key], row['revision'])
        if identity in versions and versions[identity] != row:
            return None
        versions[identity] = row
        if row[key] not in winners or row['revision'] > winners[row[key]]['revision']:
            winners[row[key]] = row
    return list(winners.values())


def parse_iso_date(value, message):
    check(re.fullmatch(r'\d{4}-\d{2}-\d{2}', value or ''), message)
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise AssertionError(message) from None


def validate_export_manifest(contract, tables):
    """Check export metadata against the contract and the parsed snapshot fixtures.

    The manifest is metadata, not a mapped business source: its shape comes from the
    top-level export_manifest section, and its claims are bound to the fixture rows.
    """
    snapshot_sources = sorted(name for name in contract['sources'] if name.endswith('_snapshot'))
    check(snapshot_sources, 'contract declares no snapshot sources')
    actual = {}
    for source in snapshot_sources:
        check(source in tables, f'missing snapshot fixture: {source}')
        for row in tables[source]:
            pair = (source, row['snapshot_month_end'])
            actual[pair] = actual.get(pair, 0) + 1
    # Every snapshot source owes a record for every period any snapshot fixture covers,
    # so a source with no rows for a period must be declared empty rather than omitted.
    periods = {period for _, period in actual}
    required = {(source, period) for source in snapshot_sources for period in periods}
    extract_ids = set()
    records = {}
    for number, row in enumerate(tables['export_manifest'], start=2):
        where = f'export manifest line {number}'
        source = row['source_name']
        check(source in contract['sources'], f'{where}: unknown source_name')
        extract_id = row['extract_id']
        check(re.fullmatch(r'MOCK[-_][A-Za-z0-9][A-Za-z0-9._-]*', extract_id), f'{where}: extract_id must be synthetic')
        check(extract_id not in extract_ids, f'{where}: duplicate extract_id')
        extract_ids.add(extract_id)
        stamp = row['extracted_at_utc']
        check(re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', stamp), f'{where}: extracted_at_utc must be an ISO UTC timestamp')
        try:
            datetime.strptime(stamp, '%Y-%m-%dT%H:%M:%SZ')
        except ValueError:
            raise AssertionError(f'{where}: extracted_at_utc must be an ISO UTC timestamp') from None
        start = parse_iso_date(row['covered_start_date'], f'{where}: covered_start_date must be an ISO date')
        end = parse_iso_date(row['covered_end_date'], f'{where}: covered_end_date must be an ISO date')
        check(start <= end, f'{where}: coverage ends before it starts')
        check(re.fullmatch(r'0|[1-9]\d*', row['row_count']), f'{where}: row_count must be a nonnegative integer')
        row_count = int(row['row_count'])
        evidence = row['completeness_evidence']
        check(evidence == evidence.strip() and evidence, f'{where}: completeness_evidence is required')
        check(re.search(r'[A-Za-z]', evidence) and 'row_count' not in evidence.lower(),
              f'{where}: completeness_evidence must be independent of the row count')
        if source not in snapshot_sources:
            # Snapshot-only fields do not apply to event or transaction exports.
            check(row['snapshot_month_end'] == '' and row['is_complete_snapshot'] == '',
                  f'{where}: snapshot fields apply only to snapshot exports')
            pair = (source, row['covered_start_date'], row['covered_end_date'])
            check(pair not in records, f'{where}: duplicate source/period record')
            records[pair] = row
            continue
        month_end = parse_iso_date(row['snapshot_month_end'], f'{where}: snapshot_month_end must be an ISO date')
        check(start <= month_end <= end, f'{where}: snapshot_month_end outside coverage')
        check(row['is_complete_snapshot'] in ('true', 'false'), f'{where}: is_complete_snapshot must be true or false')
        pair = (source, row['snapshot_month_end'])
        check(pair not in records, f'{where}: duplicate source/period record')
        records[pair] = row
        check(row_count == actual.get(pair, 0),
              f'{where}: row_count {row_count} does not match {actual.get(pair, 0)} fixture rows')
        empty_evidence = 'complete_empty' in evidence.lower()
        if row_count == 0:
            check(row['is_complete_snapshot'] == 'true' and empty_evidence,
                  f'{where}: zero-row snapshot requires explicit complete-empty evidence')
        else:
            check(not empty_evidence, f'{where}: complete-empty evidence on a non-empty snapshot')
    missing = sorted(required - records.keys())
    check(not missing, f'export manifest missing required snapshot records: {missing}')


def validate_public_content():
    # Validate the shape of the public design, without retaining source vocabulary.
    contract = load_yaml(ROOT / 'source-contract.yml')
    for source in contract['sources'].values():
        for key in ('source_header_allowlist', 'normalized_column_allowlist'):
            columns = source[key]
            check(len(columns) == len(set(columns)), 'duplicate contract column')
            check(all(re.fullmatch(r'[a-z][a-z0-9_]*', c) for c in columns),
                  'invalid generic column name')
    def identifiers(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if (key == 'key' or key.endswith(('_id', '_key', '_code', '_identifier', '_ids', '_keys'))) and value is not None:
                    check(isinstance(value, str) and re.fullmatch(r'MOCK[-_][A-Za-z0-9][A-Za-z0-9._-]*', value),
                          'edge fixture identifier must be synthetic')
                identifiers(value)
        elif isinstance(node, list):
            for value in node:
                identifiers(value)
    identifiers(load_yaml(ROOT / 'fixtures/edge_cases.yml'))


def main():
    validate_public_content()
    documents = {p.name: load_yaml(p) for p in ROOT.glob('*.yml')}
    contract = documents['source-contract.yml']
    mapping = documents['source-mapping.example.yml']
    model = documents['semantic-model.yml']
    for name, table in mapping['tables'].items():
        source = contract['sources'][name]
        check(all(k == v for k, v in table['source_headers'].items()), 'use canonical example headers')
        check(set(source['source_header_allowlist']) == set(table['source_headers']), name)
        check(set(source['normalized_column_allowlist']) == set(table['source_headers'].values()), name)
    facts = {f['name'] for f in model['facts']}
    check(len(facts) == len(model['facts']), 'duplicate logical fact')
    for report in documents['report-crosswalk.yml']['outputs']:
        check(set(report['target_facts']) <= facts, 'unknown crosswalk fact')
    check(model['consumption_models']['leadership']['source_tables'] == ['FactLeadershipMonthlyKPI'], 'leadership detail leak')
    semantic_tables = {f['semantic_table']: set(f['required_columns']) for f in model['facts']}
    check(len(semantic_tables) == len(facts), 'duplicate semantic table')
    check(len({f['model_name'] for f in model['facts']}) == len(facts), 'duplicate Gold model')
    check(all(re.fullmatch(r'fact_[a-z_]+', f['model_name']) for f in model['facts']), 'invalid Gold name')
    check(set(semantic_tables) | {'FactSnapshotManifest'} == set(model['consumption_models']['hr_analyst']['source_tables']), 'analyst table mismatch')
    semantic_tables.update({t['name']: set(t['required_columns']) for t in model['support_tables']})
    semantic_tables.update({'DimDate': {'Date', 'MonthEnd'}, 'DimLeaveType': {'LeaveType'}})
    dax = (ROOT / 'semantic/measures.dax').read_text()
    dax = re.sub(r'//[^\n]*', '', dax)
    blocks = dict(re.findall(r'^([^\n]+?) :=\n(.*?)(?=^[^\n]+? :=\n|\Z)', dax, re.M | re.S))
    check(len(blocks) == len(re.findall(r' :=', dax)), 'duplicate DAX measure')
    for table, column in re.findall(r'(\w+)\[(\w+)\]', dax):
        check(table in semantic_tables and column in semantic_tables[table], f'unknown DAX column: {table}[{column}]')
    for table in re.findall(r'\bFact\w+\b', dax):
        check(table in semantic_tables, f'unknown DAX table: {table}')
    for relationship in model['relationships']:
        table, column = relationship['to'].split('.')
        check(table in semantic_tables and column in semantic_tables[table], 'unknown relationship target')
    coverage = {
        'WorkforceSnapshot': ['Headcount (Analyst)'],
        'RequisitionSnapshot': ['Open Requisitions (Analyst)', 'Open Positions (Analyst)', 'Applicants (Analyst)', 'Hired Applicants (Analyst)'],
        'HireEvent': ['Hires (Analyst)', 'Median Time To Hire (Days)'],
        'TerminationEvent': ['Terminations (Analyst)', 'Monthly Turnover Rate (Analyst)'],
        'ScheduleActual': ['Scheduled Hours (Analyst)', 'Actual Hours (Analyst)', 'Hours Variance (Analyst)'],
        'TimeEntry': ['Absence Hours (Analyst)'],
        'LeaveBalanceSnapshot': ['Annual Leave Hours Remaining (Analyst)', 'Annual Leave Hours Accrued (Analyst)', 'Annual Leave Hours Taken YTD (Analyst)', 'Annual Leave Hours Scheduled (Analyst)'],
    }
    for report in documents['report-crosswalk.yml']['outputs']:
        expected = {name for fact in report['target_facts'] for name in coverage[fact]}
        check(set(report['draft_measures']) == expected, f"crosswalk measure mismatch: {report['report']}")
        check(expected <= blocks.keys(), f"missing report coverage: {report['report']}")
    snapshot_sources = {'FactWorkforceSnapshot': 'employee_snapshot', 'FactRequisitionSnapshot': 'requisition_snapshot', 'FactLeaveBalanceSnapshot': 'leave_balance_snapshot'}
    for name, body in blocks.items():
        for table, source in snapshot_sources.items():
            if table + '[' in body:
                check('HASONEVALUE ( DimDate[MonthEnd] ) && Complete' in body, f'missing single-month guard: {name}')
                check(f'FactSnapshotManifest[SourceName] = "{source}"' in body, f'wrong manifest: {name}')
                check('SELECTEDVALUE ( FactSnapshotManifest[IsComplete], FALSE () )' in body, f'missing completeness guard: {name}')
                check('REMOVEFILTERS ( DimDate ), DimDate[Date] = MonthEnd' in body, f'missing month-end context: {name}')
    median_body = blocks['Median Time To Hire (Days)']
    for token in ('NOT ISBLANK ( FactHireEvent[EmployeeStartDate] )', 'NOT ISBLANK ( FactHireEvent[RequisitionCreatedDate] )', 'FactHireEvent[EmployeeStartDate] >= FactHireEvent[RequisitionCreatedDate]', 'MEDIANX', 'DATEDIFF'):
        check(token in median_body, 'missing median date guard')
    check({name for name in blocks if 'absence' in name.lower()} == {'Absence Hours (Analyst)'},
          'only category-filtered absence hours are supported in DAX')
    check(all(model['measures'][name]['status'].startswith('blocked_') for name in ('absence_hours', 'absence_rate')), 'Absenteeism must remain blocked')

    absence_body = blocks['Absence Hours (Analyst)']
    check('KEEPFILTERS ( FactTimeEntry[EntryCategory] = "ABSENCE" )' in absence_body,
          'absence hours must exclude overtime and ordinary time')
    check(all('_' not in name and name[0].isupper() for name in blocks), 'unreadable DAX display name')

    tables = {}
    csv_count = 0
    for path in sorted((ROOT / 'fixtures').rglob('*.csv')):
        with path.open(newline='') as stream:
            reader = csv.DictReader(stream)
            check(len(reader.fieldnames) == len(set(reader.fieldnames)), str(path))
            if path.parent.name == 'input' and path.stem == 'export_manifest':
                # Metadata described by the top-level export_manifest section, not a mapped source.
                check(reader.fieldnames == contract['export_manifest']['required_per_file'],
                      f'input header mismatch: {path.name}; expected exactly {contract["export_manifest"]["required_per_file"]}')
            elif path.parent.name == 'input':
                check(path.stem in contract['sources'], f'unknown input source: {path.name}')
                source_columns = set(contract['sources'][path.stem]['source_header_allowlist'])
                rules = contract['fixture_input_policy']['sources'][path.stem]
                optional = set(rules['optional_source_columns'])
                check(optional <= source_columns, f'unknown optional source column: {path.name}')
                required = source_columns - optional
                def normalized(columns):
                    return {'employee_key' if c == 'employee_key_from_restricted_map' else c for c in columns}
                required = normalized(required) | set(rules['required_derived_columns'])
                allowed = normalized(source_columns) | set(rules['required_derived_columns'])
                header = set(reader.fieldnames)
                check(required <= header <= allowed,
                      f'input header mismatch: {path.name}; missing={sorted(required-header)}, extra={sorted(header-allowed)}')
            rows = list(reader)
            check(all(None not in r and all(v is not None for v in r.values()) for r in rows), str(path))
            for row in rows:
                for column, value in row.items():
                    prefixes = {
                        'employee_key': 'MOCK-WORKER-', 'requisition_key': 'MOCK-REQ-',
                        'hire_event_key': 'MOCK-EVENT-', 'termination_event_key': 'MOCK-EVENT-',
                        'company': 'MOCK-ORG-', 'location': 'MOCK-SITE-',
                        'entry_type_code': 'MOCK_',
                    }
                    if column in prefixes or column.endswith(('_id', '_key', '_code', '_identifier')):
                        check(re.fullmatch(r'MOCK[-_][A-Za-z0-9][A-Za-z0-9._-]*', value),
                              f'non-mock identifier: {path.name}/{column}')
            tables[path.stem] = rows
            csv_count += 1
    check('export_manifest' in tables, 'missing export manifest fixture')
    validate_export_manifest(contract, tables)
    employees = tables['employee_snapshot']
    def hc(month):
        return len({r['employee_key'] for r in employees if r['snapshot_month_end'] == month and r['worker_status'] == 'active'})
    req = [r for r in tables['requisition_snapshot'] if r['snapshot_month_end'] == '2032-10-31']
    hires = tables['time_to_hire']
    scheduled = sum(float(r['scheduled_hours']) for r in tables['schedule_actual'])
    actual = sum(float(r['actual_hours']) for r in tables['schedule_actual'])
    terms = len({r['termination_event_key'] for r in tables['termination_event']})
    absence = sum(float(r['hours']) for r in tables['time_entry'] if r['entry_category'] == 'ABSENCE')
    values = dict(active_headcount=hc('2032-10-31'), hires=len({r['hire_event_key'] for r in hires}),
                  terminations=terms, open_requisitions=sum(r['requisition_status'] == 'open' for r in req),
                  open_positions=sum(int(r['number_of_openings']) for r in req if r['requisition_status'] == 'open'),
                  applicants=sum(int(r['applicant_count']) for r in req),
                  hired_applicants=sum(int(r['hired_applicant_count']) for r in req),
                  turnover_rate=terms / ((hc('2032-09-30') + hc('2032-10-31')) / 2),
                  median_time_to_hire=median((date.fromisoformat(r['employee_start_date']) - date.fromisoformat(r['requisition_created_date'])).days for r in hires),
                  scheduled_hours=scheduled, actual_hours=actual, hours_variance=actual-scheduled,
                  absence_hours=absence, absence_rate=absence/scheduled,
                  annual_leave_hours_remaining=sum(float(r['hours_remaining']) for r in tables['leave_balance_snapshot'] if r['snapshot_month_end'] == '2032-10-31' and r['leave_type'] == 'ANNUAL'))
    for row in tables['monthly_hr_kpis']:
        if row['period'] == '2032-10':
            check(abs(values[row['kpi']] - float(row['value'])) < 0.000001, row['kpi'])
        else:
            check(row['value'] == '' and row['status'] == 'not_computable_missing_snapshot', 'missing prior')
    edge = load_yaml(ROOT / 'fixtures/edge_cases.yml')
    for case in edge['snapshot_cases']:
        single = len(case['selected_months']) == 1
        closing = case['current_statuses'].count('active')
        opening = case['prior_statuses'].count('active')
        headcount = closing if single and case['current_complete'] else None
        denominator = (opening + closing) / 2
        turnover = case['terminations']/denominator if single and case['current_complete'] and case['prior_complete'] and denominator else None
        check(headcount == case['expected_headcount'], case['name'])
        check(turnover is None if case['expected_turnover'] is None else abs(turnover-case['expected_turnover']) < 0.000001, case['name'])
    for case in edge['identity_cases']:
        resolved = resolve(case['rows'], 'episode_id')
        dates = sorted(r['start_date'] for r in resolved) if resolved is not None else None
        check(dates == case['expected_dates'], case['name'])
    for case in edge['overlap_cases']:
        resolved = resolve(case['rows'], 'record_key')
        hours = sum(r['hours'] for r in resolved) if resolved is not None else None
        check(hours == case['expected_hours'], case['name'])
    for case in edge['duration_cases']:
        start = date.fromisoformat(case['start_date']) if case['start_date'] else None
        created = case['requisition_created_date']
        days = (start-date.fromisoformat(created)).days if start and created else None
        hires = None if start is None or (days is not None and days < 0) else 1
        days = days if hires else None
        check(hires == case['expected_hires'] and days == case['expected_days'], case['name'])
    for case in edge['median_cases']:
        durations = [(date.fromisoformat(r['start']) - date.fromisoformat(r['created'])).days
                     for r in case['rows'] if r['start'] and r['created'] and r['start'] >= r['created']]
        result = median(durations) if durations else None
        check(result == case['expected_median'], case['name'])
    for case in edge['requisition_snapshot_cases']:
        rows = case['rows']
        result = None
        if len(case['selected_months']) == 1 and case['complete']:
            selected = [r for r in rows if r['month'] == case['selected_months'][0]]
            opened = [r for r in selected if r['status'] == 'open']
            result = [len({r['key'] for r in opened}), sum(r['openings'] for r in opened),
                      sum(r['applicants'] for r in selected), sum(r['hired'] for r in selected)]
        check(result == case['expected'], case['name'])
    count = sum(len(edge[k]) for k in ('snapshot_cases', 'identity_cases', 'overlap_cases', 'duration_cases', 'median_cases', 'requisition_snapshot_cases'))
    print(f'PASS: {len(documents)+1} YAML files; {csv_count} CSV files; {len(values)} baseline KPIs; {count} edge cases')


if __name__ == '__main__':
    main()
