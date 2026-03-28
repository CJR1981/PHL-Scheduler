"""Stage 4 — Recovery engine.
Provides scored recovery options plus explicit swap actions.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Dict, List, Optional

from scheduler_engine import Flight, Team, m2t, DOCK_RELOAD, TEAM_MIN_TURNAROUND
from impact_engine import analyze_delay, analyze_truck_loss
from live_ops import (
    load_session,
    handle_delay,
    apply_delay,
    handle_reassign,
    handle_run_edit,
    _find_team_slot,
    _rebuild_summary,
    _rebuild_stats,
    _commit_session,
    m2t_now,
)


def _score(priority_base: int, penalties: List[int]) -> int:
    return max(0, min(100, priority_base - sum(penalties)))


def _plan(plan_id: str, action: str, title: str, detail: str, score: int,
          payload: Optional[dict] = None, warnings: Optional[List[str]] = None) -> dict:
    return {
        'plan_id': plan_id,
        'action': action,
        'title': title,
        'detail': detail,
        'score': score,
        'payload': payload or {},
        'warnings': warnings or [],
    }


def delay_recovery_options(session_id: str, flight_num: str, new_std: str,
                           flights: List[Flight], teams: List[Team]) -> dict:
    impact = analyze_delay(session_id, flight_num, new_std, flights, teams)
    if not impact.get('success'):
        return impact

    plans: List[dict] = []
    penalties = [
        18 if impact.get('missed_deadline') else 0,
        12 if impact.get('shift_exceeded') else 0,
        impact.get('cascade_count', 0) * 6,
        impact.get('manual_required', 0) * 10,
    ]

    if not impact.get('action_required'):
        plans.append(_plan(
            'delay_keep_current',
            'apply_delay_current',
            f'Keep AA{flight_num} on current team',
            'Current team can absorb this delay without downstream damage.',
            _score(96, penalties),
            {'flight': flight_num, 'new_std': new_std, 'apply_auto_fixes': False},
        ))

    plans.append(_plan(
        'delay_apply_auto',
        'apply_delay_auto',
        f'Apply delay and auto-fix downstream conflicts',
        f"Apply the delay to AA{flight_num} and use the current auto-fix logic for {impact.get('auto_fixable', 0)} downstream conflict(s).",
        _score(90, penalties),
        {'flight': flight_num, 'new_std': new_std, 'apply_auto_fixes': True},
        [] if not impact.get('manual_required') else [f"{impact.get('manual_required')} conflict(s) may still need dispatcher review."]
    ))

    rec = impact.get('recommended_for_flight')
    if rec:
        plans.append(_plan(
            'delay_reassign_flight',
            'delay_reassign_flight',
            f"Move delayed flight to {rec['team_id']}",
            f"Apply the delay, then reassign AA{flight_num} to {rec['team_id']} to protect the new STD.",
            _score(88, penalties + [max(0, impact.get('auto_fixable', 0) * 2)]),
            {
                'flight': flight_num,
                'new_std': new_std,
                'target_team': rec['team_id'],
                'apply_auto_fixes': True,
            },
            [f"{rec['team_id']} is currently free at {rec.get('free_at', '—')}."]
        ))

    if impact.get('cascade_conflicts'):
        first = impact['cascade_conflicts'][0]
        if first.get('run_id'):
            plans.append(_plan(
                'delay_split_run',
                'split_run',
                f"Split impacted run {first['run_id']}",
                'Pull one stop out of the impacted downstream run so dispatch can manually re-sequence it.',
                _score(72, penalties + [8]),
                {
                    'team_id': first.get('team'),
                    'flight': first.get('flight'),
                },
                ['Lower-confidence option. Best used when simple delay apply still leaves manual conflicts.']
            ))

    plans.sort(key=lambda p: (-p['score'], p['title']))
    return {
        'success': True,
        'event_type': 'delay_recovery',
        'flight': flight_num,
        'new_std': new_std,
        'impact': impact,
        'plans': plans,
        'best_plan': plans[0] if plans else None,
    }



def truck_loss_recovery_options(session_id: str, truck_id: str,
                                flights: List[Flight], teams: List[Team]) -> dict:
    impact = analyze_truck_loss(session_id, truck_id, flights, teams)
    if not impact.get('success'):
        return impact

    plans: List[dict] = []
    backups = impact.get('backup_trucks', []) or []
    impacted_runs = impact.get('impacted_runs', []) or []
    base_penalties = [len(impacted_runs) * 8, len(impact.get('impacted_flights', [])) * 3]

    if backups:
        for idx, run in enumerate(impacted_runs[:3], start=1):
            plans.append(_plan(
                f'truckloss_split_{idx}',
                'split_run',
                f"Split run {run['run_id']} for manual recovery",
                f"Use backup truck capacity after splitting run {run['run_id']} into smaller pieces.",
                _score(78, base_penalties + [(idx - 1) * 4]),
                {
                    'team_id': run.get('team'),
                    'flight': (run.get('flights') or [None])[0],
                    'truck_id': truck_id,
                },
                [f"Backup truck(s) visible before first impacted run: {', '.join(b['truck_id'] for b in backups[:3])}"]
            ))

    plans.append(_plan(
        'truckloss_manual',
        'manual_review',
        f'Review truck loss on {truck_id}',
        'Use the impacted runs list to choose a manual split, merge, or reassignment.',
        _score(60 if backups else 52, base_penalties),
        {'truck_id': truck_id},
        ['Stage 4 does not lock a specific replacement truck because backend truck IDs are still recomputed from run timing.']
    ))

    plans.sort(key=lambda p: (-p['score'], p['title']))
    return {
        'success': True,
        'event_type': 'truck_loss_recovery',
        'truck_id': truck_id,
        'impact': impact,
        'plans': plans,
        'best_plan': plans[0] if plans else None,
    }



def _make_entry_from_slot(f: Flight, row: dict, new_team_id: str, dispatch_min: int) -> dict:
    svc_s = dispatch_min + f.drv_out
    svc_e = svc_s + f.svc_time
    team_free = svc_e + f.drv_back + TEAM_MIN_TURNAROUND
    truck_free = svc_e + f.drv_back + DOCK_RELOAD
    dock_load = max(0, dispatch_min - DOCK_RELOAD)
    return {
        **row,
        'team': new_team_id,
        'dispatch': m2t(dispatch_min),
        'dispatch_min': dispatch_min,
        'dock_load': m2t(dock_load),
        'dock_load_min': dock_load,
        'svc_start': m2t(svc_s),
        'svc_start_min': svc_s,
        'svc_end': m2t(svc_e),
        'svc_end_min': svc_e,
        'team_free_at': m2t(team_free),
        'team_free_min': team_free,
        'truck_free_at': m2t(truck_free),
        'truck_free_min': truck_free,
        'run_size': 1,
        'stop_num': 1,
    }



def preview_flight_swap(session_id: str, flight_a: str, flight_b: str,
                        flights: List[Flight], teams: List[Team]) -> dict:
    session = load_session(session_id)
    if not session:
        return {'error': 'Session not found'}
    current = session.get('result', {})
    row_a = next((a for a in current.get('assignments', []) if a.get('flight') == flight_a), None)
    row_b = next((a for a in current.get('assignments', []) if a.get('flight') == flight_b), None)
    if not row_a or not row_b:
        return {'error': 'Both flights must already be assigned for a swap'}
    if row_a.get('team') == row_b.get('team'):
        return {'error': 'Flights are already on the same team'}

    f_lookup = {f.flight_num: f for f in flights}
    t_lookup = {t.team_id: t for t in teams}
    fa = f_lookup.get(flight_a)
    fb = f_lookup.get(flight_b)
    ta = t_lookup.get(row_a.get('team'))
    tb = t_lookup.get(row_b.get('team'))
    if not fa or not fb or not ta or not tb:
        return {'error': 'Flight or team data missing for swap analysis'}

    exclude = {flight_a, flight_b}
    slot_a = _find_team_slot(fa, tb, [o for o in current.get('team_ops', {}).get(tb.team_id, []) if o.get('flight') not in exclude], current.get('assignments', []), exclude_flights=exclude)
    slot_b = _find_team_slot(fb, ta, [o for o in current.get('team_ops', {}).get(ta.team_id, []) if o.get('flight') not in exclude], current.get('assignments', []), exclude_flights=exclude)

    feasible = slot_a.get('dispatch') is not None and slot_b.get('dispatch') is not None
    warnings = []
    if slot_a.get('dispatch') is None:
        warnings.append(f"{tb.team_id} cannot cleanly absorb AA{flight_a} ({slot_a.get('reason_code', 'NO_SLOT')}).")
    if slot_b.get('dispatch') is None:
        warnings.append(f"{ta.team_id} cannot cleanly absorb AA{flight_b} ({slot_b.get('reason_code', 'NO_SLOT')}).")

    return {
        'success': True,
        'action': 'swap_flights',
        'feasible': feasible,
        'flight_a': flight_a,
        'flight_b': flight_b,
        'from_team_a': ta.team_id,
        'from_team_b': tb.team_id,
        'candidate_dispatch_a': m2t(slot_a['dispatch']) if slot_a.get('dispatch') is not None else None,
        'candidate_dispatch_b': m2t(slot_b['dispatch']) if slot_b.get('dispatch') is not None else None,
        'score': _score(84, [0 if feasible else 28]),
        'warnings': warnings,
    }



def apply_flight_swap(session_id: str, flight_a: str, flight_b: str,
                      flights: List[Flight], teams: List[Team], force: bool = False) -> dict:
    preview = preview_flight_swap(session_id, flight_a, flight_b, flights, teams)
    if preview.get('error'):
        return preview
    if not preview.get('feasible') and not force:
        return {
            'error': 'Swap is not cleanly feasible with current timing',
            'can_force': True,
            'preview': preview,
        }

    session = load_session(session_id)
    current = session.get('result', {})
    row_a = next(a for a in current.get('assignments', []) if a.get('flight') == flight_a)
    row_b = next(a for a in current.get('assignments', []) if a.get('flight') == flight_b)
    f_lookup = {f.flight_num: f for f in flights}
    fa = f_lookup[flight_a]
    fb = f_lookup[flight_b]

    disp_a = preview.get('candidate_dispatch_a')
    disp_b = preview.get('candidate_dispatch_b')
    from scheduler_engine import t2m
    disp_a_min = t2m(disp_a) if disp_a else fa.latest_dispatch
    disp_b_min = t2m(disp_b) if disp_b else fb.latest_dispatch

    new_a = _make_entry_from_slot(fa, row_a, row_b.get('team'), disp_a_min)
    new_b = _make_entry_from_slot(fb, row_b, row_a.get('team'), disp_b_min)

    new_assignments = []
    for row in current.get('assignments', []):
        if row.get('flight') == flight_a:
            new_assignments.append(new_a)
        elif row.get('flight') == flight_b:
            new_assignments.append(new_b)
        else:
            new_assignments.append(row)

    new_team_ops = {tid: list(ops) for tid, ops in current.get('team_ops', {}).items()}
    for tid in (row_a.get('team'), row_b.get('team')):
        new_team_ops[tid] = [o for o in new_team_ops.get(tid, []) if o.get('flight') not in {flight_a, flight_b}]
    new_team_ops.setdefault(new_a['team'], []).append(new_a)
    new_team_ops.setdefault(new_b['team'], []).append(new_b)
    for tid in new_team_ops:
        new_team_ops[tid].sort(key=lambda o: o.get('dispatch_min', 0))

    new_result = {
        **current,
        'assignments': new_assignments,
        'team_ops': new_team_ops,
        'team_summary': _rebuild_summary(teams, new_team_ops),
        'stats': _rebuild_stats(new_assignments, current.get('unassigned', []), flights, teams, new_team_ops),
    }

    _commit_session(
        session_id,
        session,
        result=new_result,
        change_entry={
            'type': 'swap_flights',
            'flight_a': flight_a,
            'flight_b': flight_b,
            'team_a': row_a.get('team'),
            'team_b': row_b.get('team'),
            'timestamp': m2t_now(),
        },
        sync_flights=[flight_a, flight_b],
    )

    return {
        'success': True,
        'action': 'swap_flights',
        'flight_a': flight_a,
        'flight_b': flight_b,
        'preview': preview,
        'result': new_result,
    }



def preview_run_swap(session_id: str, run_id_a: str, run_id_b: str,
                     flights: List[Flight], teams: List[Team]) -> dict:
    session = load_session(session_id)
    if not session:
        return {'error': 'Session not found'}
    current = session.get('result', {})
    run_a = [a for a in current.get('assignments', []) if a.get('run_id') == run_id_a]
    run_b = [a for a in current.get('assignments', []) if a.get('run_id') == run_id_b]
    if not run_a or not run_b:
        return {'error': 'Both runs must exist to swap'}
    team_a = run_a[0].get('team')
    team_b = run_b[0].get('team')
    if team_a == team_b:
        return {'error': 'Runs are already on the same team'}

    t_lookup = {t.team_id: t for t in teams}
    ta = t_lookup.get(team_a)
    tb = t_lookup.get(team_b)
    warnings = []
    if ta and any(int(o.get('team_free_min', 0)) > ta.shift_end for o in run_b):
        warnings.append(f'{team_a} shift may be too short for incoming run {run_id_b}.')
    if tb and any(int(o.get('team_free_min', 0)) > tb.shift_end for o in run_a):
        warnings.append(f'{team_b} shift may be too short for incoming run {run_id_a}.')

    # check overlap against other runs kept on each team
    def _window(rows):
        return min(int(r.get('run_start_min', r.get('dock_load_min', 0))) for r in rows), max(int(r.get('run_end_min', r.get('truck_free_min', 0))) for r in rows)
    a_start, a_end = _window(run_a)
    b_start, b_end = _window(run_b)

    others_a = [o for o in current.get('team_ops', {}).get(team_a, []) if o.get('run_id') != run_id_a]
    others_b = [o for o in current.get('team_ops', {}).get(team_b, []) if o.get('run_id') != run_id_b]
    if any(not (int(o.get('run_end_min', o.get('truck_free_min', 0))) <= b_start or int(o.get('run_start_min', o.get('dock_load_min', 0))) >= b_end) for o in others_a):
        warnings.append(f'{team_a} has other work overlapping incoming run {run_id_b}.')
    if any(not (int(o.get('run_end_min', o.get('truck_free_min', 0))) <= a_start or int(o.get('run_start_min', o.get('dock_load_min', 0))) >= a_end) for o in others_b):
        warnings.append(f'{team_b} has other work overlapping incoming run {run_id_a}.')

    feasible = not warnings
    return {
        'success': True,
        'action': 'swap_runs',
        'run_id_a': run_id_a,
        'run_id_b': run_id_b,
        'team_a': team_a,
        'team_b': team_b,
        'flight_count_a': len(run_a),
        'flight_count_b': len(run_b),
        'feasible': feasible,
        'score': _score(82, [0 if feasible else 26]),
        'warnings': warnings,
    }



def apply_run_swap(session_id: str, run_id_a: str, run_id_b: str,
                   flights: List[Flight], teams: List[Team], force: bool = False) -> dict:
    preview = preview_run_swap(session_id, run_id_a, run_id_b, flights, teams)
    if preview.get('error'):
        return preview
    if not preview.get('feasible') and not force:
        return {
            'error': 'Run swap has conflicts',
            'can_force': True,
            'preview': preview,
        }

    session = load_session(session_id)
    current = session.get('result', {})
    team_a = preview['team_a']
    team_b = preview['team_b']
    swap_flights = []
    new_assignments = []
    for row in current.get('assignments', []):
        if row.get('run_id') == run_id_a:
            swap_flights.append(row.get('flight'))
            new_assignments.append({**row, 'team': team_b})
        elif row.get('run_id') == run_id_b:
            swap_flights.append(row.get('flight'))
            new_assignments.append({**row, 'team': team_a})
        else:
            new_assignments.append(row)

    new_team_ops = {}
    for row in new_assignments:
        tid = row.get('team')
        new_team_ops.setdefault(tid, []).append(row)
    for tid in new_team_ops:
        new_team_ops[tid].sort(key=lambda o: o.get('dispatch_min', 0))

    new_result = {
        **current,
        'assignments': new_assignments,
        'team_ops': new_team_ops,
        'team_summary': _rebuild_summary(teams, new_team_ops),
        'stats': _rebuild_stats(new_assignments, current.get('unassigned', []), flights, teams, new_team_ops),
    }

    _commit_session(
        session_id,
        session,
        result=new_result,
        change_entry={
            'type': 'swap_runs',
            'run_id_a': run_id_a,
            'run_id_b': run_id_b,
            'team_a': team_a,
            'team_b': team_b,
            'timestamp': m2t_now(),
        },
        sync_flights=swap_flights,
    )
    return {
        'success': True,
        'action': 'swap_runs',
        'preview': preview,
        'result': new_result,
    }



def apply_plan(session_id: str, plan: dict, flights: List[Flight], teams: List[Team]) -> dict:
    action = (plan or {}).get('action')
    payload = (plan or {}).get('payload') or {}
    if not action:
        return {'error': 'Plan action missing'}

    if action in ('apply_delay_current', 'apply_delay_auto'):
        delay_result = handle_delay(session_id, payload.get('flight'), payload.get('new_std'), flights, teams)
        if delay_result.get('error'):
            return delay_result
        return apply_delay(session_id, delay_result, teams, apply_auto_fixes=bool(payload.get('apply_auto_fixes', action == 'apply_delay_auto')))

    if action == 'delay_reassign_flight':
        delay_result = handle_delay(session_id, payload.get('flight'), payload.get('new_std'), flights, teams)
        if delay_result.get('error'):
            return delay_result
        applied = apply_delay(session_id, delay_result, teams, apply_auto_fixes=bool(payload.get('apply_auto_fixes', True)))
        if applied.get('error'):
            return applied
        return handle_reassign(session_id, payload.get('flight'), payload.get('target_team'), flights, teams, force=bool(payload.get('force', False)))

    if action == 'split_run':
        team_id = payload.get('team_id')
        flight = payload.get('flight')
        if not team_id or not flight:
            return {'error': 'split_run plan requires team_id and flight'}
        return handle_run_edit(session_id, team_id, 'split', [flight], flights, teams)

    if action == 'merge_run':
        team_id = payload.get('team_id')
        flight_ids = payload.get('flights') or []
        return handle_run_edit(session_id, team_id, 'merge', flight_ids, flights, teams, merge_dispatch=payload.get('merge_dispatch_min'))

    if action == 'swap_flights':
        return apply_flight_swap(session_id, payload.get('flight_a'), payload.get('flight_b'), flights, teams, force=bool(payload.get('force', False)))

    if action == 'swap_runs':
        return apply_run_swap(session_id, payload.get('run_id_a'), payload.get('run_id_b'), flights, teams, force=bool(payload.get('force', False)))

    if action == 'manual_review':
        return {'success': True, 'action': 'manual_review', 'message': 'Manual review plan acknowledged. No schedule change applied.'}

    return {'error': f'Unsupported plan action: {action}'}
