"""Stage 3 — Disruption impact analysis engine.
Creates non-destructive impact reports for live events before apply.
"""
from typing import Dict, List, Optional

from scheduler_engine import (
    Flight, Team, solve_partial, drv, m2t, t2m,
    TEAM_MIN_TURNAROUND, DOCK_RELOAD, SVC_TIMES, DEFAULT_SVC
)
from live_ops import load_session, handle_delay


def _risk_level(score: int) -> str:
    if score >= 70:
        return 'critical'
    if score >= 40:
        return 'high'
    if score >= 20:
        return 'medium'
    return 'low'


def _rows_to_runs(rows: List[dict]) -> List[dict]:
    runs = {}
    for row in rows or []:
        run_id = row.get('run_id') or f"R-{row.get('team','?')}-{row.get('flight','?')}"
        item = runs.setdefault(run_id, {
            'run_id': run_id,
            'team': row.get('team'),
            'truck_id': row.get('truck_id'),
            'dispatch_min': row.get('dispatch_min', 0),
            'run_start_min': row.get('run_start_min', row.get('dock_load_min', 0)),
            'run_end_min': row.get('run_end_min', row.get('truck_free_min', 0)),
            'flights': [],
        })
        item['dispatch_min'] = min(item['dispatch_min'], row.get('dispatch_min', 0))
        item['run_start_min'] = min(item['run_start_min'], row.get('run_start_min', row.get('dock_load_min', 0)))
        item['run_end_min'] = max(item['run_end_min'], row.get('run_end_min', row.get('truck_free_min', 0)))
        item['flights'].append(row.get('flight'))
    return sorted(runs.values(), key=lambda r: (r['run_start_min'], r['run_id']))


def _recommendation(title: str, detail: str, priority: str = 'medium') -> dict:
    return {'title': title, 'detail': detail, 'priority': priority}


def _truck_snapshot(result: dict) -> List[dict]:
    rows = result.get('assignments', []) or []
    trucks = {}
    for row in rows:
        tid = row.get('truck_id')
        if not tid:
            continue
        t = trucks.setdefault(tid, {'truck_id': tid, 'runs': set(), 'flights': [], 'busy_from': 10**9, 'busy_to': -1})
        t['runs'].add(row.get('run_id'))
        t['flights'].append(row.get('flight'))
        t['busy_from'] = min(t['busy_from'], int(row.get('dock_load_min', 0)))
        t['busy_to'] = max(t['busy_to'], int(row.get('truck_free_min', 0)))
    out=[]
    for tid, t in trucks.items():
        out.append({
            'truck_id': tid,
            'run_count': len(t['runs']),
            'flights': t['flights'],
            'busy_window': f"{m2t(t['busy_from'])}–{m2t(t['busy_to'])}",
            'busy_from_min': t['busy_from'],
            'busy_to_min': t['busy_to'],
        })
    return sorted(out, key=lambda x: (x['busy_from_min'], x['truck_id']))


def analyze_delay(session_id: str, flight_num: str, new_std_str: str,
                  flights: List[Flight], teams: List[Team]) -> dict:
    base = handle_delay(session_id, flight_num, new_std_str, flights, teams)
    if not base.get('success'):
        return base

    session = load_session(session_id)
    current = (session or {}).get('result', {})
    impacted_flights = [flight_num] + [f['flight'] for f in base.get('fixes', [])]
    impacted_rows = [r for r in current.get('assignments', []) if r.get('flight') in impacted_flights]
    impacted_runs = _rows_to_runs(impacted_rows + [base.get('updated_assignment', {})])
    impacted_trucks = sorted({r.get('truck_id') for r in impacted_rows if r.get('truck_id')})

    score = 0
    if base.get('missed_deadline'):
        score += 35
    if base.get('shift_exceeded'):
        score += 20
    score += min(30, base.get('cascade_count', 0) * 8)
    score += min(20, base.get('manual_required', 0) * 10)
    score += min(10, base.get('auto_fixable', 0) * 3)

    recommendations = []
    if base.get('recommended_for_flight'):
        rf = base['recommended_for_flight']
        recommendations.append(_recommendation(
            'Reassign delayed flight',
            f"Move AA{flight_num} to {rf['team_id']} if current team can no longer protect the new STD.",
            'high' if (base.get('missed_deadline') or base.get('shift_exceeded')) else 'medium'
        ))
    if base.get('auto_fixable'):
        recommendations.append(_recommendation(
            'Apply auto-fixes',
            f"{base['auto_fixable']} downstream conflict(s) can be reassigned automatically.",
            'medium'
        ))
    if base.get('manual_required'):
        recommendations.append(_recommendation(
            'Manual intervention required',
            f"{base['manual_required']} downstream conflict(s) still need dispatcher review.",
            'high'
        ))
    if not recommendations:
        recommendations.append(_recommendation(
            'No schedule action required',
            'The current team can absorb the delay without breaking downstream coverage.',
            'low'
        ))

    return {
        **base,
        'event_type': 'delay',
        'impact_summary': {
            'risk_score': score,
            'risk_level': _risk_level(score),
            'impacted_flight_count': len(impacted_flights),
            'impacted_run_count': len(impacted_runs),
            'impacted_truck_count': len(impacted_trucks),
            'manual_actions_needed': base.get('manual_required', 0),
        },
        'impacted_flights': impacted_flights,
        'impacted_runs': impacted_runs,
        'impacted_trucks': impacted_trucks,
        'recommendations': recommendations,
    }


def analyze_gate_change(session_id: str, flight_num: str, new_gate: str,
                        flights: List[Flight], teams: List[Team]) -> dict:
    session = load_session(session_id)
    if not session:
        return {'error': 'Session not found'}
    current = session.get('result', {})
    assignment = next((a for a in current.get('assignments', []) if a.get('flight') == flight_num), None)
    if not assignment:
        return {'error': f'AA{flight_num} not found or not assigned'}

    old_gate = assignment.get('gate', 'B')
    old_drv = drv(old_gate)
    new_drv = drv(new_gate)
    svc = SVC_TIMES.get(assignment.get('equip', ''), DEFAULT_SVC)
    std_min = assignment.get('std_min', 0)
    fb = 55 if assignment.get('is_intl') else 35
    old_dispatch = assignment.get('dispatch_min', 0)
    new_svc_start = old_dispatch + new_drv
    new_svc_end = new_svc_start + svc
    new_team_free = new_svc_end + new_drv + TEAM_MIN_TURNAROUND
    orig_team_free = assignment.get('team_free_min', new_team_free)
    team_id = assignment.get('team')
    cascade = []
    for op in sorted(current.get('team_ops', {}).get(team_id, []), key=lambda x: x.get('dispatch_min', 0)):
        d = op.get('dispatch_min', 0)
        if op.get('flight') == flight_num or d < orig_team_free:
            continue
        if new_team_free > d:
            cascade.append({
                'flight': op.get('flight'),
                'dispatch': op.get('dispatch'),
                'dispatch_min': d,
                'overlap_mins': new_team_free - d,
                'run_id': op.get('run_id'),
                'truck_id': op.get('truck_id'),
            })
        new_team_free = max(new_team_free, op.get('team_free_min', d))

    score = abs(new_drv - old_drv) * 2 + len(cascade) * 8 + (20 if new_svc_end > (std_min - fb) else 0)
    impacted_rows = [assignment] + [r for r in current.get('assignments', []) if r.get('flight') in {c['flight'] for c in cascade}]

    recommendations = []
    if cascade:
        recommendations.append(_recommendation('Review downstream run', f"Gate move creates {len(cascade)} downstream conflict(s) on {team_id}.", 'high'))
    if new_svc_end > (std_min - fb):
        recommendations.append(_recommendation('Re-time or reassign flight', f"AA{flight_num} would miss its finish buffer from {new_gate} using the current dispatch time.", 'high'))
    if not recommendations:
        recommendations.append(_recommendation('Update gate only', 'Current team can absorb the gate change without further run edits.', 'low'))

    return {
        'success': True,
        'event_type': 'gate_change',
        'flight': flight_num,
        'old_gate': old_gate,
        'new_gate': new_gate,
        'drive_delta_mins': new_drv - old_drv,
        'missed_deadline': new_svc_end > (std_min - fb),
        'cascade_conflicts': cascade,
        'impacted_flights': [r.get('flight') for r in impacted_rows],
        'impacted_runs': _rows_to_runs(impacted_rows),
        'impacted_trucks': sorted({r.get('truck_id') for r in impacted_rows if r.get('truck_id')}),
        'impact_summary': {
            'risk_score': score,
            'risk_level': _risk_level(score),
            'impacted_flight_count': len(impacted_rows),
            'impacted_run_count': len(_rows_to_runs(impacted_rows)),
            'impacted_truck_count': len({r.get('truck_id') for r in impacted_rows if r.get('truck_id')}),
        },
        'recommendations': recommendations,
    }


def analyze_sick_call(session_id: str, sick_team_id: str,
                      flights: List[Flight], teams: List[Team], time_limit: int = 45) -> dict:
    session = load_session(session_id)
    if not session:
        return {'error': 'Session not found'}
    current = session.get('result', {})
    sick_ops = list(current.get('team_ops', {}).get(sick_team_id, []))
    if not sick_ops:
        return {'error': f'{sick_team_id} has no flights assigned'}

    affected_nums = {o['flight'] for o in sick_ops}
    affected_flights = [f for f in flights if f.flight_num in affected_nums]
    available_teams = [t for t in teams if t.team_id != sick_team_id]
    partial_result = solve_partial(affected_flights, available_teams, time_limit=time_limit)

    score = min(40, len(sick_ops) * 8) + min(30, len(partial_result.get('unassigned', [])) * 15)
    intl_cnt = sum(1 for o in sick_ops if o.get('is_intl'))
    score += min(15, intl_cnt * 5)

    receiving = {}
    for a in partial_result.get('assignments', []):
        receiving.setdefault(a.get('team'), []).append(a.get('flight'))

    recommendations = [
        _recommendation('Redistribute affected flights', f"{len(partial_result.get('assignments', []))} flight(s) can be recovered by other teams.", 'medium' if not partial_result.get('unassigned') else 'high')
    ]
    if partial_result.get('unassigned'):
        recommendations.append(_recommendation('Create manual recovery plan', f"{len(partial_result['unassigned'])} flight(s) remain uncovered after partial solve.", 'high'))

    return {
        'success': True,
        'event_type': 'sick_call',
        'team': sick_team_id,
        'affected_flights': sorted(affected_nums),
        'impacted_runs': _rows_to_runs(sick_ops),
        'impacted_trucks': sorted({o.get('truck_id') for o in sick_ops if o.get('truck_id')}),
        'solver_preview': {
            'reassigned': len(partial_result.get('assignments', [])),
            'still_unassigned': len(partial_result.get('unassigned', [])),
            'receiving_teams': receiving,
        },
        'impact_summary': {
            'risk_score': score,
            'risk_level': _risk_level(score),
            'impacted_flight_count': len(affected_nums),
            'impacted_run_count': len(_rows_to_runs(sick_ops)),
            'impacted_truck_count': len({o.get('truck_id') for o in sick_ops if o.get('truck_id')}),
        },
        'recommendations': recommendations,
    }


def analyze_truck_loss(session_id: str, truck_id: str,
                       flights: List[Flight], teams: List[Team]) -> dict:
    session = load_session(session_id)
    if not session:
        return {'error': 'Session not found'}
    current = session.get('result', {})
    impacted_rows = [a for a in current.get('assignments', []) if a.get('truck_id') == truck_id]
    if not impacted_rows:
        return {'error': f'{truck_id} not found in current schedule'}

    impacted_runs = _rows_to_runs(impacted_rows)
    trucks = [t for t in _truck_snapshot(current) if t['truck_id'] != truck_id]
    first_start = min(r.get('run_start_min', 0) for r in impacted_runs)
    backups = []
    for t in trucks:
        if t['busy_to_min'] <= first_start:
            backups.append({'truck_id': t['truck_id'], 'available_from': m2t(t['busy_to_min'])})
    backups = backups[:5]

    score = min(50, len(impacted_rows) * 7) + min(20, len(impacted_runs) * 6)
    if not backups:
        score += 20

    recommendations = []
    if backups:
        recommendations.append(_recommendation('Use backup truck capacity', f"{len(backups)} truck(s) appear free before the first impacted run starts.", 'medium'))
    else:
        recommendations.append(_recommendation('Run-level recovery required', 'No obvious backup truck is free before the first impacted run. Expect run splits or delays.', 'high'))

    return {
        'success': True,
        'event_type': 'truck_loss',
        'truck_id': truck_id,
        'impacted_flights': [r.get('flight') for r in impacted_rows],
        'impacted_runs': impacted_runs,
        'backup_trucks': backups,
        'impact_summary': {
            'risk_score': score,
            'risk_level': _risk_level(score),
            'impacted_flight_count': len(impacted_rows),
            'impacted_run_count': len(impacted_runs),
            'impacted_truck_count': 1,
        },
        'recommendations': recommendations,
    }
