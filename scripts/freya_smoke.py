"""Calibrate selected builder tests on the saved Freya agent, one call at a time.

    python3 scripts/freya_smoke.py final-b builder-agentic/work/final-b/tests/out/tests.json --only T-01,T-02

The JSON result can be supplied to the builder with --feedback. This does not change an agent workflow.
"""
import argparse
import copy
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / 'sim')]
import freya
import selftest


def check_unique_customers(cases):
    """The mock indexes active sessions by customer, even with sequential calls."""
    seen = {}
    for case in cases:
        variables = case.get('variables') or {}
        customer = next((variables[key] for key in
                         ('customerNumber', 'customerNo', 'customer_number', 'customer_id')
                         if variables.get(key) not in (None, '')), None)
        if customer is None:
            raise ValueError(f"Case {case['id']} has no customer identifier; cannot verify session isolation")
        customer = str(customer)
        if customer in seen:
            raise ValueError(f"Cases {seen[customer]} and {case['id']} use the same bank customer; "
                             "run them in separate smoke invocations to keep their ledgers isolated")
        seen[customer] = case['id']


def success_criteria(case):
    checks = list(case['checks'])
    expected = case.get('expected') or {}
    if expected.get('outcome'):
        checks.append("The call actually ends with the semantic exit '" + expected['outcome'] + "'"
                      + (" (reason '" + expected['reason'] + "')" if expected.get('reason') else "")
                      + "; the actual workflow exit and the conversation must both fit this outcome.")
    if expected.get('end_reason'):
        checks.append("The actual workflow terminal function name is exactly '" + expected['end_reason']
                      + "'; a spoken promise to end or transfer does not satisfy this condition.")
    return 'All of these are true:\n- ' + '\n- '.join(checks)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('slot'); ap.add_argument('tests')
    ap.add_argument('--only', required=True, help='Explicit comma-separated case ids; there is no default full-suite run')
    ap.add_argument('--seconds', type=int, default=120)
    ap.add_argument('--out')
    args = ap.parse_args()
    agent = json.loads((ROOT / '.freya-agents.json').read_text())[args.slot]['agent_id']
    selected = set(args.only.split(','))
    cases = []
    stamp = str(int(time.time()))
    for raw in json.loads(pathlib.Path(args.tests).read_text()):
        if raw['id'] not in selected: continue
        c = copy.deepcopy(raw)
        c['source_id'] = c['id']
        c['id'] = 'SMOKE-' + args.slot + '-' + c['id'] + '-' + stamp
        cases.append(c)
    if {c['source_id'] for c in cases} != selected:
        raise ValueError('Some selected case ids do not exist in the supplied test set')
    check_unique_customers(cases)
    specs = [{'name': c['id'], 'prompt': selftest.persona(c),
              'successCriteria': success_criteria(c),
              'tags': ['hackathon', 'builder-calibration']} for c in cases]
    made = freya.create_personas(specs)['personas']
    ids = {c['id']: p['id'] for c, p in zip(cases, made)}
    selftest.bank('/v1/practice/register', {'cases': cases})
    sessions = {}
    started = time.time()
    result = {'agent_id': agent, 'scope': 'selected cases, one Freya run each; not full-case coverage'}
    out = pathlib.Path(args.out) if args.out else ROOT / 'results' / (args.slot + '-freya-smoke.json')
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        for c in cases:
            sessions[c['id']] = selftest.bank('/v1/practice/sessions', {'case': c['id']})['variables']
        print('Freya smoke:', ', '.join(sorted(selected)), '| concurrency 1 |', args.seconds, 'seconds per call', flush=True)
        sim_id, it_id, status = freya.run_simulation(agent, args.slot + ' calibration ' + stamp,
            {ids[c['id']]: sessions[c['id']] for c in cases}, max_seconds=args.seconds, concurrency=1)
        runs = {r['personaId']: r for r in freya.get_iteration(sim_id, it_id)['runs']}
        result.update(simulation_id=sim_id, iteration_id=it_id, status=status,
                      seconds=round(time.time()-started), rows=[])
        for c in cases:
            led = selftest.bank('/v1/practice/ledger/' + sessions[c['id']]['customer_id'])
            bad = selftest.score(c, led)
            run = runs.get(ids[c['id']], {})
            if run.get('outcome') != 'pass': bad.append('Freya conversation: ' + str(run.get('reason') or 'no verdict'))
            call = freya.get_call(run['callId']) if run.get('callId') else None
            expected_end = c.get('expected', {}).get('end_reason')
            if expected_end:
                # docs/workflow-format.md: the call's endedReason is the terminal function.name.
                # Compare exact values; do not invent a semantic mapping from platform labels.
                actual_end = call.get('endedReason') if isinstance(call, dict) else None
                if actual_end is None:
                    bad.append('Cannot verify expected.end_reason: native call metadata has no endedReason')
                elif actual_end != expected_end:
                    bad.append(f"Native call endedReason {actual_end!r}, expected {expected_end!r}")
            row = {'id': c['source_id'], 'pass': not bad, 'failed': bad,
                   'tools': [x['tool'] for x in led.get('calls', [])], 'ledger': led, 'freya_run': run}
            if call is not None: row['call'] = call
            result['rows'].append(row)
            print('PASS' if not bad else 'FAIL', c['source_id'], '|', ' > '.join(row['tools']), flush=True)
            for reason in bad: print('  -', reason[:1000], flush=True)
        result['passed'] = sum(r['pass'] for r in result['rows'])
        result['total'] = len(cases)
    except Exception as exc:
        result['error'] = str(exc)
        raise
    finally:
        for v in sessions.values():
            try: selftest.bank('/v1/practice/end', {'customer_id': v['customer_id']})
            except Exception: pass
        out.write_text(json.dumps(result, ensure_ascii=False, indent=1))
        print('Result saved:', out, flush=True)


if __name__ == '__main__': main()
