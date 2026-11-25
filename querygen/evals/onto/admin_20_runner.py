"""Admin 20 evaluation. Run --validate normally; execute --live inside Odoo shell.

PowerShell: $env:BI_EVAL_BANK='C:/.../evals/onto/admin_20.json'
             $env:BI_EVAL_LIVE='1'
             Get-Content evals/onto/admin_20_runner.py | python odoo-bin shell -d DB
Only aggregate scores and case IDs are printed; questions and Odoo rows stay in memory.
"""
import json
import math
import os
from pathlib import Path
import sys


def validate(cases):
    errors = []
    if len(cases) != 20 or len({c['id'] for c in cases}) != 20:
        errors.append('expected 20 unique cases')
    for c in cases:
        if not all(k in c for k in ('id', 'question', 'expected_metric', 'refusal', 'truth')):
            errors.append('missing required field')
        truth = c.get('truth')
        if truth and any(not isinstance(truth.get(k, False), bool) for k in ('normalize_month', 'ignore_extra_zero_rows', 'match_pos_display_name', 'empty_as_zero')):
            errors.append(c['id'] + ': comparison flags must be boolean')
        if truth and (truth.get('kind') not in ('scalar', 'rows') or not truth.get('sql', '').lower().lstrip().startswith('select ')):
            errors.append(c['id'] + ': invalid truth')
    return errors


def key(value, normalize_month=False):
    if isinstance(value, (list, tuple)) and value:
        value = value[0]
    result = str(value).strip().lower()
    return result[:7] if normalize_month else result


def compare(truth, cols, rows, metric, tolerance, normalize_month=False, ignore_extra_zero_rows=False, match_pos_display_name=False, fixed_dimension=None):
    """Return an outcome and aggregate-only reason for one evidence result."""
    if not rows or metric not in cols:
        return 'not_comparable', {'reason': 'missing_rows_or_metric'}
    index = cols.index(metric)
    dimensions = [i for i in range(len(cols)) if i != index]
    if fixed_dimension and fixed_dimension.get('op') == '=' and fixed_dimension.get('prop') in cols:
        fixed_index = cols.index(fixed_dimension['prop'])
        if fixed_index in dimensions:
            if not all(key(row[fixed_index]) == key(fixed_dimension['value']) for row in rows):
                return 'not_comparable', {'reason': 'fixed_dimension_values_differ', 'dimension': fixed_dimension['prop']}
            dimensions.remove(fixed_index)
    if truth and len(truth[0]) - 1 != len(dimensions):
        return 'not_comparable', {'reason': 'dimension_count', 'expected': len(truth[0]) - 1, 'actual': len(dimensions), 'actual_dimensions': [cols[i] for i in dimensions]}
    def mapping(data, indices, val_index):
        result = {}
        for row in data:
            k = tuple(key(row[i], normalize_month) for i in indices)
            if k in result or row[val_index] is None:
                raise ValueError('ambiguous or null')
            result[k] = float(row[val_index])
        return result
    try:
        expected = mapping(truth, list(range(len(truth[0]) - 1)), len(truth[0]) - 1) if truth else {}
        actual = mapping(rows, dimensions, index)
        if match_pos_display_name:
            resolved = {}
            for k, value in actual.items():
                choices = [e for e in expected if len(k) == len(e) == 1 and (k == e or (k[0].startswith(e[0] + ' (') and k[0].endswith(')')))]
                target = choices[0] if len(choices) == 1 else k
                if target in resolved:
                    raise ValueError('ambiguous display name')
                resolved[target] = value
            actual = resolved
        if ignore_extra_zero_rows:
            actual = {k: v for k, v in actual.items() if k in expected or not math.isclose(v, 0, abs_tol=tolerance)}
        missing, extra = set(expected) - set(actual), set(actual) - set(expected)
        unequal = sum(not math.isclose(actual[k], expected[k], rel_tol=0, abs_tol=tolerance) for k in set(actual) & set(expected))
        detail = {'reason': 'row_comparison', 'missing': len(missing), 'extra': len(extra), 'unequal': unequal, 'expected_count': len(expected), 'actual_count': len(actual)}
        return ('different' if missing or extra or unequal else 'pass'), detail
    except (ValueError, TypeError, IndexError):
        return 'not_comparable', {'reason': 'duplicate_null_or_invalid_value'}


def has_filter(filters, required):
    """Accept equality and singleton membership only when they mean the same thing."""
    for item in filters or []:
        if item.get('prop') != required.get('prop'):
            continue
        if item == required:
            return True
        if required.get('op') == '=' and item.get('op') == 'in' and item.get('value') == [required.get('value')]:
            return True
    return False


def live(cases, env):
    import time
    import uuid
    import odoo.addons.odoo_dashboards_bi.models.evidence as evidence
    import odoo.addons.odoo_dashboards_bi.models.thread as thread
    original = evidence.execute
    traces = []
    def spy(env_, catalog, attachment, tool):
        out = original(env_, catalog, attachment, tool)
        if isinstance(out, dict):
            traces.append(({**(tool.get('arguments') or {}), '_tool': tool.get('name')}, [x.get('key') for x in out.get('columns') or []], out.get('rows') or []))
        return out
    evidence.execute = thread.evidence.execute = spy
    admin = env(user=env.ref('base.user_admin'))
    Thread = admin['native.bi.thread']
    try:
        for case in cases:
            traces.clear()
            try:
                tid = Thread.create_thread()
                tid = tid['id'] if isinstance(tid, dict) else tid
                result = Thread.start_turn(tid, case['question'], uuid.uuid4().hex)
                turn_id = result.get('turn_id', result.get('id')) if isinstance(result, dict) else result
                turn = env['native.bi.turn'].sudo().browse(turn_id)
                for _ in range(14):
                    turn._advance()
                    if turn.status not in ('queued', 'running'):
                        break
                card = turn.card or {}
                refused = turn.status == 'refused' or bool(card.get('refusal')) or card.get('kind') == 'refusal'
                successful = turn.status in ('done', 'refused')
                expected_metric = case['expected_metric']
                matches = [(a, c, r) for a, c, r in traces if expected_metric is not None and a.get('metric') == expected_metric]
                if case['id'] == 'inv-latepo-supplier':
                    matches = [(a, c, r) for a, c, r in traces if a.get('_tool') == 'outstanding_purchase_lines' and a.get('mode') == 'suppliers' and a.get('before') == '2026-11-01']
                dims = case.get('expected_dims') or []
                required_filter = case.get('expected_filter')
                metric_score = ('pass' if (refused and (case['refusal'] or case.get('allow_refusal'))) else
                    'different' if refused or case['refusal'] else
                    'not_scored' if expected_metric is None else
                    'pass' if any(all(d in (a.get('dims') or []) for d in dims) and (not required_filter or has_filter(a.get('filters'), required_filter)) for a, _, _ in matches) else 'different')
                data_score = 'not_scored'
                data_detail = {'reason': 'not_scored'}
                truth_summary = None
                truth = case.get('truth')
                if truth and not refused:
                    env.cr.execute('SAVEPOINT admin_eval_truth')
                    try:
                        env.cr.execute(truth['sql'])
                        actual_truth = env.cr.fetchall()
                        if case['id'] == 'inv-latepo-supplier':
                            truth_summary = {'overdue_lines': int(sum(row[-1] for row in actual_truth)), 'suppliers': len(actual_truth), 'fixture_baseline': [7, 4]}
                        env.cr.execute('RELEASE SAVEPOINT admin_eval_truth')
                        data_score = 'not_comparable'
                        data_detail = {'reason': 'no_matching_evidence', 'candidates': 0}
                        candidates = []
                        for args, cols, rows in matches:
                            if case.get('expected_period') and args.get('period') != case['expected_period']:
                                continue
                            if dims and not set(dims).issubset(set(args.get('dims') or [])):
                                continue
                            if required_filter and not has_filter(args.get('filters'), required_filter):
                                continue
                            if case.get('expected_top_n'):
                                actual_rank = args.get('top_n') or {}
                                expected_rank = case['expected_top_n']
                                if actual_rank.get('n') != expected_rank.get('n') or actual_rank.get('dir', 'desc') != expected_rank.get('dir', 'desc'):
                                    continue
                            if truth.get('empty_as_zero') and not rows and expected_metric in cols and len(actual_truth) == 1 and len(actual_truth[0]) == 1 and actual_truth[0][0] == 0:
                                candidates.append(('pass', {'reason': 'scoped_empty_as_zero'}))
                                continue
                            candidates.append(compare(actual_truth, cols, rows, 'line_count' if case['id'] == 'inv-latepo-supplier' else expected_metric, truth.get('tolerance', 0.001), truth.get('normalize_month', False), truth.get('ignore_extra_zero_rows', False), truth.get('match_pos_display_name', False), required_filter))
                        for outcome in ('pass', 'different', 'not_comparable'):
                            selected = next((detail for result, detail in candidates if result == outcome), None)
                            if selected is not None:
                                data_score, data_detail = outcome, {**selected, 'candidates': len(candidates)}
                                break
                    except Exception:
                        env.cr.execute('ROLLBACK TO SAVEPOINT admin_eval_truth')
                        env.cr.execute('RELEASE SAVEPOINT admin_eval_truth')
                        data_score = 'truth_error'
                print(json.dumps({'id': case['id'], 'status': 'done' if successful else 'failed', 'metric': metric_score, 'data': data_score, 'data_detail': data_detail, 'truth_summary': truth_summary, 'evidence': 'present' if traces else 'absent'}), flush=True)
            except Exception as exc:
                print(json.dumps({'id': case['id'], 'status': 'error', 'error_type': type(exc).__name__, 'metric': 'not_scored', 'data': 'not_scored'}), flush=True)
    finally:
        evidence.execute = thread.evidence.execute = original
        env.cr.rollback()


bank = Path(os.environ.get('BI_EVAL_BANK', 'evals/onto/admin_20.json'))
cases = json.loads(bank.read_text(encoding='utf-8'))
errors = validate(cases)
if errors:
    raise ValueError('; '.join(errors))
selected = os.environ.get('BI_EVAL_CASES')
if selected is not None:
    requested = {item.strip() for item in selected.split(',') if item.strip()}
    known = {case['id'] for case in cases}
    if not requested or requested - known:
        raise ValueError('BI_EVAL_CASES must contain known case IDs')
    cases = [case for case in cases if case['id'] in requested]
if os.environ.get('BI_EVAL_LIVE') == '1':
    live(cases, env)
else:
    print('Validated full 20-case bank; selected %d cases; no Odoo calls made.' % len(cases))
