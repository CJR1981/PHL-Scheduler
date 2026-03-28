"""
PHL Catering Scheduler — Live Operations Engine v2.0
Phase 3: Disruption Handling
- handle_delay:    Full cascade detection + minimum-deviation auto-fix + apply
- handle_sick_call: Remove team, redistribute with partial solver
- handle_reassign:  Manual reassign with over-cap warning
- handle_gate_change: Recalculate timing for new gate location
"""
from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional
import threading, time, uuid, copy
from scheduler_engine import (
    Flight, Team, Run, solve, solve_partial, build_runs,
    m2t, t2m, TEAM_MIN_TURNAROUND, DOCK_RELOAD, g2g, drv,
    DRIVE_TO_GATE, FINISH_BUF, FINISH_BUF_MIN, FINISH_BUF_TIGHT,
    SVC_TIMES, DEFAULT_SVC, GROUND_OPS_MIN, TRUCK_COUNT,
    apply_run_truck_ids
)

# ── Hybrid session store (memory + Supabase) ──────────────────────────────
# Memory cache for fast access during a session; Supabase for persistence.
_sessions: Dict[str, dict] = {}
_lock = threading.Lock()

def save_session(sid: str, data: dict):
    with _lock:
        _sessions[sid] = {**data, '_ts': time.time()}
    # Persist to Supabase in background (non-blocking)
    try:
        from supabase_client import save_session_db
        import threading as _t
        _t.Thread(
            target=save_session_db,
            args=(sid, data),
            kwargs={
                'day_of_week':   data.get('day_of_week'),
                'schedule_date': data.get('schedule_date'),
            },
            daemon=True
        ).start()
    except Exception:
        pass  # Supabase unavailable — memory cache still works

def load_session(sid: str) -> Optional[dict]:
    # Try memory first (fast), fall back to Supabase (cross-restart)
    with _lock:
        cached = _sessions.get(sid)
    if cached:
        return cached
    try:
        from supabase_client import load_session_db
        remote = load_session_db(sid)
        if remote:
            with _lock:
                _sessions[sid] = {**remote, '_ts': time.time()}
            return remote
    except Exception:
        pass
    return None

def clear_old_sessions(max_age_hours: int = 12):
    cutoff = time.time() - max_age_hours * 3600
    with _lock:
        stale = [k for k,v in _sessions.items() if v.get('_ts',0) < cutoff]
        for k in stale: del _sessions[k]

def m2t_now() -> str:
    import datetime
    n = datetime.datetime.now()
    return f"{n.hour:02d}:{n.minute:02d}"


def _event_user(change_type: str) -> str:
    return 'System' if change_type in {'delay', 'gate_change', 'sick_call'} else 'Dispatcher'


def _normalize_change_entry(change_entry: dict) -> dict:
    entry = dict(change_entry or {})
    if not entry:
        return entry
    entry.setdefault('event_id', str(uuid.uuid4()))
    entry.setdefault('timestamp', m2t_now())
    entry.setdefault('status', 'applied')
    entry.setdefault('mode', 'auto' if entry.get('type') in {'delay', 'gate_change', 'sick_call'} else 'manual')
    entry.setdefault('user', _event_user(entry.get('type', '')))
    entry.setdefault('note', '')
    children = []
    for child in entry.get('children', []) or []:
        c = dict(child)
        c.setdefault('event_id', str(uuid.uuid4()))
        c.setdefault('timestamp', entry.get('timestamp'))
        c.setdefault('status', 'applied')
        c.setdefault('mode', c.get('mode') or entry.get('mode', 'auto'))
        c.setdefault('user', c.get('user') or entry.get('user') or _event_user(entry.get('type', '')))
        c.setdefault('note', '')
        children.append(c)
    entry['children'] = children
    return entry


def update_event_note(session_id: str, event_id: str, note: str) -> dict:
    session = load_session(session_id)
    if not session:
        return {'error': 'Session not found'}
    log = list(session.get('change_log', []))
    found = False
    for entry in log:
        if entry.get('event_id') == event_id:
            entry['note'] = note
            found = True
            break
        for child in entry.get('children', []) or []:
            if child.get('event_id') == event_id:
                child['note'] = note
                found = True
                break
        if found:
            break
    if not found:
        return {'error': 'Event not found'}
    updated = {**session, 'change_log': log}
    if updated.get('result') is not None:
        updated['result'] = _with_session_metadata(updated['result'], updated)
    save_session(session_id, updated)
    return {'success': True, 'event_id': event_id, 'note': note}


def undo_last_action(session_id: str) -> dict:
    session = load_session(session_id)
    if not session:
        return {'error': 'Session not found'}
    history = list(session.get('_history', []))
    if not history:
        return {'error': 'Nothing to undo'}
    prev = history.pop()
    updated = {**session}
    updated['result'] = prev.get('result', updated.get('result'))
    updated['change_log'] = list(prev.get('change_log', []))
    updated['sick_calls'] = list(prev.get('sick_calls', []))
    updated['flights'] = prev.get('flights', updated.get('flights'))
    updated['_history'] = history
    if updated.get('result') is not None:
        updated['result'] = apply_run_truck_ids(updated['result'])
        updated['result'] = _with_session_metadata(updated['result'], updated)
    save_session(session_id, updated)
    return {'success': True, 'result': updated.get('result'), 'change_log': updated.get('change_log', [])}


def _with_session_metadata(result: dict, session: dict) -> dict:
    """Persist lightweight live-ops metadata inside the result payload."""
    if not isinstance(result, dict):
        return result
    return {
        **result,
        '_change_log': list(session.get('change_log', [])),
        '_sick_calls': list(session.get('sick_calls', [])),
        '_last_live_update': m2t_now(),
    }


def _sync_session_flights(session: dict, flight_nums: Optional[List[str]] = None) -> List[Flight]:
    """Keep the canonical Flight objects aligned with committed result changes."""
    flights = session.get('flights') or []
    result = session.get('result') or {}
    lookup = {}
    for bucket in ('assignments', 'unassigned'):
        for row in result.get(bucket, []) or []:
            lookup[str(row.get('flight', ''))] = row

    target = set(str(x) for x in (flight_nums or lookup.keys()))
    synced: List[Flight] = []
    for fl in flights:
        if fl.flight_num not in target or fl.flight_num not in lookup:
            synced.append(fl)
            continue

        src = lookup[fl.flight_num]
        repl = replace(
            fl,
            flight_num=str(src.get('flight', fl.flight_num)),
            dest=src.get('dest', fl.dest),
            std=int(src.get('std_min', fl.std)),
            equip=src.get('equip', fl.equip),
            gate=src.get('gate', fl.gate),
            dep_type=src.get('type', src.get('dep_type', fl.dep_type)),
            nose=src.get('nose', fl.nose),
            is_ron=bool(src.get('is_ron', fl.is_ron)),
            arrival_time=int(src.get('arrival_min', fl.arrival_time)),
        )

        base_fields = set(getattr(fl, '__dataclass_fields__', {}).keys())
        for key, value in fl.__dict__.items():
            if key not in base_fields:
                setattr(repl, key, value)
        synced.append(repl)
    return synced


def _commit_session(session_id: str, session: dict, *, result: Optional[dict] = None,
                    change_entry: Optional[dict] = None,
                    sick_calls: Optional[List[str]] = None,
                    sync_flights: Optional[List[str]] = None) -> dict:
    """Single commit path so result, metadata, and canonical flight state stay aligned."""
    updated = {**session}
    history = list(session.get('_history', []))
    history.append({
        'result': copy.deepcopy(session.get('result')),
        'change_log': copy.deepcopy(session.get('change_log', [])),
        'sick_calls': copy.deepcopy(session.get('sick_calls', [])),
        'flights': copy.deepcopy(session.get('flights', [])),
    })
    updated['_history'] = history[-20:]

    if result is not None:
        updated['result'] = result
    if sick_calls is not None:
        updated['sick_calls'] = list(sick_calls)
    if change_entry is not None:
        updated['change_log'] = list(session.get('change_log', [])) + [_normalize_change_entry(change_entry)]

    if updated.get('result') is not None:
        # Stage 2: make run_id and truck_id backend-authoritative after every live change
        updated['result'] = apply_run_truck_ids(updated['result'])
        updated['result'] = _with_session_metadata(updated['result'], updated)
    if sync_flights is not None:
        updated['flights'] = _sync_session_flights(updated, sync_flights)

    save_session(session_id, updated)
    return updated


def _truck_window(op: dict) -> tuple:
    start = op.get('dock_load_min')
    if start is None:
        start = max(0, op.get('dispatch_min', 0) - DOCK_RELOAD)

    end = op.get('truck_free_min')
    if end is None:
        tf = t2m(op.get('truck_free_at', ''))
        if tf >= 0:
            end = tf
        else:
            end = op.get('svc_end_min', op.get('dispatch_min', 0)) + drv(op.get('gate', 'B')) + DOCK_RELOAD
    return int(start), int(end)


def _truck_slot_available(assignments: List[dict], start_min: int, end_min: int,
                          exclude_flights: Optional[set] = None) -> bool:
    exclude_flights = exclude_flights or set()
    start_min = max(0, int(start_min))
    end_min = max(start_min, int(end_min))
    if end_min <= start_min:
        return True

    for minute in range(start_min, end_min):
        active = 0
        for op in assignments:
            if op.get('flight') in exclude_flights:
                continue
            ws, we = _truck_window(op)
            if ws <= minute < we:
                active += 1
                if active >= TRUCK_COUNT:
                    return False
    return True


def _find_team_slot(flight: Flight, team: Team, team_ops: List[dict], all_assignments: List[dict],
                    exclude_flights: Optional[set] = None) -> dict:
    """Find the earliest feasible dispatch gap that also fits the pooled truck limit."""
    exclude_flights = exclude_flights or set()
    existing_ops = sorted(team_ops or [], key=lambda o: o.get('dispatch_min', 0))
    busy = flight.drv_out + flight.svc_time + flight.drv_back + TEAM_MIN_TURNAROUND
    flight_end_busy = flight.drv_out + flight.svc_time + flight.drv_back + DOCK_RELOAD

    candidate_windows = []
    free_from = team.ready_at
    latest_dispatch_for_shift = team.shift_end - busy

    for op in existing_ops:
        op_dispatch = op.get('dispatch_min', 0)
        gap_end = op_dispatch - 1
        lo = max(free_from, flight.earliest_dispatch)
        hi = min(gap_end - busy + 1, flight.latest_dispatch, latest_dispatch_for_shift)
        if lo <= hi:
            candidate_windows.append((lo, hi))
        free_from = max(free_from, op.get('team_free_min', op_dispatch + busy))

    lo = max(free_from, flight.earliest_dispatch)
    hi = min(flight.latest_dispatch, latest_dispatch_for_shift)
    if lo <= hi:
        candidate_windows.append((lo, hi))

    if not candidate_windows:
        return {'dispatch': None, 'reason_code': 'NO_TEAM_GAP'}

    saw_truck_block = False
    for lo, hi in candidate_windows:
        for cand in range(lo, hi + 1):
            truck_start = max(0, cand - DOCK_RELOAD)
            truck_end = cand + flight_end_busy
            if _truck_slot_available(all_assignments, truck_start, truck_end, exclude_flights=exclude_flights):
                return {'dispatch': cand, 'reason_code': None}
            saw_truck_block = True

    return {'dispatch': None, 'reason_code': 'TRUCK_POOL_FULL' if saw_truck_block else 'NO_TEAM_GAP'}


# ── Timing helpers ─────────────────────────────────────────────────────────
def _fin_buf(f_or_dict) -> int:
    """Get finish buffer for a flight (dict or Flight object)."""
    if isinstance(f_or_dict, dict):
        is_ron   = f_or_dict.get('is_ron', False)
        is_intl  = f_or_dict.get('is_intl', False)
        is_tight = f_or_dict.get('is_tight_turn', False)
        is_short = f_or_dict.get('is_short_turn', False)
    else:
        is_ron = f_or_dict.is_ron; is_intl = f_or_dict.is_intl
        is_tight = f_or_dict.is_tight_turn; is_short = f_or_dict.is_short_turn
    key = 'ron' if is_ron else ('intl' if is_intl else 'dom')
    if is_tight:  return FINISH_BUF_TIGHT[key]
    if is_short:  return FINISH_BUF_MIN[key]
    return FINISH_BUF[key]

def _recalc_timing(assignment: dict, new_std: int) -> dict:
    """Recalculate all timing fields for an assignment given a new STD."""
    equip    = assignment.get('equip', '')
    gate     = assignment.get('gate', 'B')
    drv_t    = drv(gate)
    svc      = SVC_TIMES.get(equip, DEFAULT_SVC)
    fb       = _fin_buf(assignment)
    dispatch = assignment.get('dispatch_min', 0)  # dispatch stays the same

    svc_start  = dispatch + drv_t
    svc_end    = svc_start + svc
    team_free  = svc_end + drv_t + TEAM_MIN_TURNAROUND
    truck_free = svc_end + drv_t + DOCK_RELOAD

    return {**assignment,
        'std': m2t(new_std), 'std_min': new_std,
        'svc_start': m2t(svc_start), 'svc_end': m2t(svc_end),
        'team_free_at': m2t(team_free), 'truck_free_at': m2t(truck_free),
        'svc_start_min': svc_start, 'svc_end_min': svc_end,
        'team_free_min': team_free,
    }

def _cascade(team_id: str, orig_team_free: int, new_team_free: int,
             team_ops: dict, shift_end: int) -> List[dict]:
    """
    Find all ops on this team that conflict after a timing shift.
    Checks every op dispatched after orig_team_free — the window that
    used to be clear may now overlap with the pushed-out team_free.
    Returns list of conflict dicts.
    """
    ops = sorted(team_ops.get(team_id, []), key=lambda x: x.get('dispatch_min', 0))
    conflicts = []
    # Walk forward from new_team_free, carrying the free-time through each op
    prev_free = new_team_free
    for op in ops:
        d = op.get('dispatch_min', 0)
        # Only consider ops that were previously fine (dispatched after orig_team_free)
        if d < orig_team_free: continue
        if prev_free > d:
            conflicts.append({
                'flight':           op['flight'],
                'dest':             op.get('dest', ''),
                'std':              op.get('std', ''),
                'std_min':          op.get('std_min', 0),
                'dispatch':         op.get('dispatch', ''),
                'dispatch_min':     d,
                'gate':             op.get('gate', ''),
                'equip':            op.get('equip', ''),
                'is_intl':          op.get('is_intl', False),
                'overlap_mins':     prev_free - d,
                'team_free_needed': m2t(prev_free),
                'message': f"AA{op['flight']} dispatches {m2t(d)}, "
                           f"team not free until {m2t(prev_free)} "
                           f"({prev_free - d}min conflict)"
            })
        prev_free = max(prev_free, op.get('team_free_min', d))
    return conflicts


# ── Sick call handler ──────────────────────────────────────────────────────
def handle_sick_call(session_id: str, sick_team_id: str,
                     flights: List[Flight], teams: List[Team],
                     time_limit: int = 60) -> dict:
    """Remove a team, redistribute their flights using minimum-deviation approach.
    Tries to keep affected flights on the closest available teams before re-solving."""
    session = load_session(session_id)
    if not session:
        return {'error': 'Session not found'}

    current     = session['result']
    sick_ops    = current['team_ops'].get(sick_team_id, [])
    if not sick_ops:
        return {'error': f'{sick_team_id} has no flights assigned'}

    affected_nums    = {o['flight'] for o in sick_ops}
    affected_flights = [f for f in flights if f.flight_num in affected_nums]
    available_teams  = [t for t in teams if t.team_id != sick_team_id]

    partial_result = solve_partial(affected_flights, available_teams, time_limit=time_limit)

    new_assignments = [a for a in current['assignments'] if a['flight'] not in affected_nums]
    new_assignments.extend(partial_result['assignments'])

    new_team_ops: Dict[str, List] = {t.team_id: [] for t in available_teams}
    for a in new_assignments:
        tid = a.get('team')
        if tid and tid in new_team_ops:
            new_team_ops[tid].append(a)
    for tid in new_team_ops:
        new_team_ops[tid].sort(key=lambda x: x.get('dispatch_min', 0))

    new_unassigned = [u for u in current.get('unassigned', [])
                      if u['flight'] not in affected_nums]
    new_unassigned.extend(partial_result.get('unassigned', []))

    new_summary = _rebuild_summary(available_teams, new_team_ops)
    new_stats   = _rebuild_stats(new_assignments, new_unassigned, flights, available_teams, new_team_ops)

    new_result  = {**current,
                   'assignments': new_assignments, 'unassigned': new_unassigned,
                   'team_ops': new_team_ops, 'team_summary': new_summary, 'stats': new_stats}

    # Build human-readable reassignment map
    reassignment_map = []
    teams_receiving: Dict[str, List] = {}
    for a in partial_result['assignments']:
        old_op = next((o for o in sick_ops if o['flight'] == a['flight']), {})
        reassignment_map.append({'flight': a['flight'], 'dest': a.get('dest',''),
                                  'std': a.get('std',''), 'new_team': a['team'],
                                  'is_intl': a.get('is_intl', False),
                                  'is_short_turn': a.get('is_short_turn', False)})
        teams_receiving.setdefault(a['team'], []).append(a)

    _commit_session(
        session_id,
        session,
        result=new_result,
        sick_calls=list(session.get('sick_calls', [])) + [sick_team_id],
        change_entry={
            'type': 'sick_call', 'team': sick_team_id,
            'reassigned': len(partial_result['assignments']), 'reason': 'Team removed from schedule due to sick call',
            'children': [{'type':'auto_reassign','flight':a['flight'],'old_team':sick_team_id,'new_team':a['team'],'old_std':a.get('std'),'new_std':a.get('std'),'old_gate':a.get('gate'),'new_gate':a.get('gate'),'reason':'Coverage moved after sick call','mode':'auto'} for a in partial_result['assignments']],
            'timestamp': m2t_now(),
        },
        sync_flights=list(affected_nums),
    )

    return {
        'success': True,
        'sick_team': sick_team_id,
        'affected': len(affected_flights),
        'reassigned': len(partial_result['assignments']),
        'still_unassigned': len(partial_result['unassigned']),
        'reassignment_map': reassignment_map,
        'teams_receiving': {k: v for k, v in teams_receiving.items()},
        'unassigned_map': [{'flight': u['flight'], 'dest': u.get('dest',''),
                             'std': u.get('std',''), 'reason': u.get('reason','')}
                            for u in partial_result['unassigned']],
        'result': new_result,
    }


# ── Delay handler v2 — full cascade + apply ───────────────────────────────
def handle_delay(session_id: str, flight_num: str, new_std_str: str,
                 flights: List[Flight], teams: List[Team]) -> dict:
    """
    Phase 3 delay handler:
    1. Validate and compute new timing
    2. Detect ALL cascade conflicts downstream on the team
    3. For each conflict, find the minimum-deviation fix:
       a. Auto-fix: reassign conflicted flight to another available team
       b. If no auto-fix: flag for manual intervention
    4. Return full impact report with recommended actions
    NOTE: Does NOT auto-commit. Call apply_delay() to commit.
    """
    session = load_session(session_id)
    if not session:
        return {'error': 'Session not found'}

    current = session['result']
    new_std = t2m(new_std_str)
    if new_std < 0:
        return {'error': f'Invalid time: {new_std_str}'}

    assignment = next((a for a in current['assignments'] if a['flight'] == flight_num), None)
    if not assignment:
        ua = next((u for u in current.get('unassigned', []) if u['flight'] == flight_num), None)
        if ua:
            return {'warning': f'AA{flight_num} is unassigned. Delay noted.', 'flight': flight_num,
                    'new_std': new_std_str, 'unassigned': True}
        return {'error': f'AA{flight_num} not found'}

    f_obj = next((f for f in flights if f.flight_num == flight_num), None)
    if not f_obj:
        return {'error': f'AA{flight_num} not in flight data'}

    orig_std   = assignment['std_min']
    delay_mins = new_std - orig_std
    if delay_mins <= 0:
        return {'error': f'New STD {new_std_str} must be later than current {m2t(orig_std)}'}

    team_id    = assignment['team']
    dispatch   = assignment.get('dispatch_min', 0)
    drv_t      = drv(assignment.get('gate', 'B'))
    svc        = SVC_TIMES.get(assignment.get('equip', ''), DEFAULT_SVC)

    # ── Correct delayed timing ────────────────────────────────────────────
    # When a flight is delayed, the service window shifts forward:
    # - Latest service end = new_std - finish_buffer  (deadline moves later)
    # - Service start = latest_svc_end - svc_time     (service pushed later)
    # - Team stays at gate waiting, then services, then drives back
    # The team is NOT free until: new_svc_end + drive_back + turnaround
    fb             = _fin_buf(assignment)
    new_latest_svc_end = new_std - fb
    new_svc_start  = new_latest_svc_end - svc   # service pushed to latest feasible
    new_svc_end    = new_latest_svc_end
    new_team_free  = new_svc_end + drv_t + TEAM_MIN_TURNAROUND

    # Check: if team was dispatched but delay is so long they could return and re-dispatch
    orig_svc_end   = assignment.get('svc_end_min', dispatch + drv_t + svc)
    orig_team_free = assignment.get('team_free_min', orig_svc_end + drv_t + TEAM_MIN_TURNAROUND)

    # Service deadline check: can dispatch + drive still reach gate before new service window?
    missed_deadline = new_svc_end > new_std  # can't finish before departure

    # Build the delayed assignment before analysing alternate fixes.
    # This lets truck-pool checks include the longer truck occupancy.
    new_dispatch = max(dispatch, new_svc_start - drv_t)  # may push dispatch later
    updated = {**assignment,
        'std': m2t(new_std), 'std_min': new_std,
        'delayed': True, 'original_std': m2t(orig_std), 'delay_mins': delay_mins,
        'dispatch': m2t(new_dispatch), 'dispatch_min': new_dispatch,
        'svc_start': m2t(new_svc_start), 'svc_start_min': new_svc_start,
        'svc_end': m2t(new_svc_end), 'svc_end_min': new_svc_end,
        'team_free_at': m2t(new_team_free), 'team_free_min': new_team_free,
        'dock_load': m2t(new_dispatch - DOCK_RELOAD),
        'dock_load_min': max(0, new_dispatch - DOCK_RELOAD),
        'truck_free_at': m2t(new_svc_end + drv_t + DOCK_RELOAD),
        'truck_free_min': new_svc_end + drv_t + DOCK_RELOAD,
    }
    simulated_assignments = [updated if a['flight'] == flight_num else a for a in current['assignments']]

    # Shift-exceeded check: does the new team_free push beyond the team's shift end?
    team_obj     = next((t for t in teams if t.team_id == team_id), None)
    team_shift_end = team_obj.shift_end if team_obj else 1440
    shift_exceeded = new_team_free > team_shift_end

    # Cascade: find all ops on this team that now conflict
    # Pass orig_team_free so we catch ops dispatched between old-free and new-free
    cascade_conflicts = _cascade(team_id, orig_team_free, new_team_free,
                                  current['team_ops'], team_shift_end)

    # Minimum-deviation fix: for each conflict, find best alternative team.
    # prov_loads tracks flights already recommended this pass so the same team
    # is never suggested for two conflicts, preventing stacked-over-cap assignments.
    fixes = []
    prov_loads: Dict[str, int] = {}
    for conflict in cascade_conflicts:
        cflight_num = conflict['flight']
        cflight_a = next((a for a in current['assignments'] if a['flight'] == cflight_num), None)
        cflight_f = next((f for f in flights if f.flight_num == cflight_num), None)
        if not cflight_a or not cflight_f:
            continue

        best_team = None
        for t in sorted(teams, key=lambda t: len(current['team_ops'].get(t.team_id, []))):
            if t.team_id == team_id:
                continue
            if t.team_type == 'FH':
                continue  # FH excluded from regular pass — tried below if needed
            # Hard cap gate: committed load + provisional recommendations must stay < cap
            committed_load = len(current['team_ops'].get(t.team_id, []))
            provisional    = prov_loads.get(t.team_id, 0)
            if committed_load + provisional >= t.cap:
                continue
            slot = _find_team_slot(
                cflight_f,
                t,
                current['team_ops'].get(t.team_id, []),
                simulated_assignments,
                exclude_flights={cflight_num},
            )
            if slot.get('dispatch') is None:
                continue
            earliest_d = slot['dispatch']
            earliest_se = earliest_d + cflight_f.drv_out + cflight_f.svc_time
            curr_load = committed_load
            best_team = {'team_id': t.team_id,
                         'team_type': t.team_type,
                         'shift': f"{m2t(t.shift_start)}–{m2t(t.shift_end%1440)}",
                         'current_load': curr_load, 'cap': t.cap,
                         'free_at': m2t(earliest_d),
                         'dispatch': m2t(earliest_d),
                         'would_finish_at': m2t(earliest_se),
                         'reason_code': slot.get('reason_code'),
                         'is_fh_dispatch': False}
            break

        # ── FH last-resort: only if no regular team could take the flight ──
        if not best_team:
            for t in sorted([t for t in teams if t.team_type == 'FH'],
                            key=lambda t: len(current['team_ops'].get(t.team_id, []))):
                committed_load = len(current['team_ops'].get(t.team_id, []))
                provisional    = prov_loads.get(t.team_id, 0)
                if committed_load + provisional >= t.cap:
                    continue
                slot = _find_team_slot(
                    cflight_f,
                    t,
                    current['team_ops'].get(t.team_id, []),
                    simulated_assignments,
                    exclude_flights={cflight_num},
                )
                if slot.get('dispatch') is None:
                    continue
                earliest_d = slot['dispatch']
                earliest_se = earliest_d + cflight_f.drv_out + cflight_f.svc_time
                curr_load = committed_load
                best_team = {'team_id': t.team_id,
                             'team_type': t.team_type,
                             'shift': f"{m2t(t.shift_start)}–{m2t(t.shift_end%1440)}",
                             'current_load': curr_load, 'cap': t.cap,
                             'free_at': m2t(earliest_d),
                             'dispatch': m2t(earliest_d),
                             'would_finish_at': m2t(earliest_se),
                             'reason_code': slot.get('reason_code'),
                             'is_fh_dispatch': True}  # supervisor awareness flag
                break

        if best_team:
            prov_loads[best_team['team_id']] = prov_loads.get(best_team['team_id'], 0) + 1

        fixes.append({
            **conflict,
            'auto_fix': best_team,
            'fix_type': 'REASSIGN_TO_ALT' if best_team else 'MANUAL_INTERVENTION_REQUIRED',
        })

    # ── Recommended team for the DELAYED FLIGHT ITSELF ──────────────────
    # Needed when: shift_exceeded OR missed_deadline (current team can't do it)
    # Also computed when there are no conflicts, as a "best available" suggestion.
    recommended_for_flight = None
    fb_this  = _fin_buf(assignment)
    deadline_this = new_std - fb_this   # latest svc_end allowed
    for t in sorted(teams, key=lambda t: len(current['team_ops'].get(t.team_id, []))):
        if t.team_id == team_id: continue          # not the current team
        if t.team_type == 'FH': continue           # FH excluded from regular pass — tried below
        if t.shift_end < new_std: continue         # shift must cover new departure
        # Must start before now + ready time allows: team must be free by dispatch
        t_free = max(t.ready_at,
                     max((op.get('team_free_min', 0)
                          for op in current['team_ops'].get(t.team_id, [])),
                         default=t.ready_at))
        new_disp_needed  = new_svc_start - drv_t
        if t_free > new_disp_needed: continue      # too late to dispatch in time
        earliest_se = new_disp_needed + drv_t + svc
        if earliest_se > deadline_this: continue   # can't finish before deadline
        curr_load = len(current['team_ops'].get(t.team_id, []))
        recommended_for_flight = {
            'team_id':        t.team_id,
            'team_type':      t.team_type,
            'shift':          f"{m2t(t.shift_start)}–{m2t(t.shift_end % 1440)}",
            'current_load':   curr_load,
            'cap':            t.cap,
            'free_at':        m2t(t_free),
            'dispatch_by':    m2t(new_disp_needed),
            'would_finish':   m2t(earliest_se),
            'is_fh_dispatch': False,
        }
        break   # first (least loaded) valid team

    # ── FH last-resort for the delayed flight itself ──────────────────────
    # Only reached if no regular team could take it.
    if not recommended_for_flight:
        for t in sorted([t for t in teams if t.team_type == 'FH'],
                        key=lambda t: len(current['team_ops'].get(t.team_id, []))):
            if t.team_id == team_id: continue
            if t.shift_end < new_std: continue
            t_free = max(t.ready_at,
                         max((op.get('team_free_min', 0)
                              for op in current['team_ops'].get(t.team_id, [])),
                             default=t.ready_at))
            new_disp_needed = new_svc_start - drv_t
            if t_free > new_disp_needed: continue
            earliest_se = new_disp_needed + drv_t + svc
            if earliest_se > deadline_this: continue
            curr_load = len(current['team_ops'].get(t.team_id, []))
            recommended_for_flight = {
                'team_id':        t.team_id,
                'team_type':      t.team_type,
                'shift':          f"{m2t(t.shift_start)}–{m2t(t.shift_end % 1440)}",
                'current_load':   curr_load,
                'cap':            t.cap,
                'free_at':        m2t(t_free),
                'dispatch_by':    m2t(new_disp_needed),
                'would_finish':   m2t(earliest_se),
                'is_fh_dispatch': True,  # supervisor awareness flag
            }
            break

    # Current-team status summary
    can_current_team_serve = (not shift_exceeded) and (not missed_deadline)

    return {
        'success': True,
        'flight': flight_num,
        'dest': assignment.get('dest', ''),
        'team': team_id,
        'team_shift': f"{m2t(team_obj.shift_start)}–{m2t(team_shift_end % 1440)}" if team_obj else '—',
        'gate': assignment.get('gate', ''),
        'equip': assignment.get('equip', ''),
        'original_std': m2t(orig_std),
        'new_std': new_std_str,
        'delay_mins': delay_mins,
        'missed_deadline': missed_deadline,
        'shift_exceeded': shift_exceeded,
        'shift_end': m2t(team_shift_end % 1440),
        'can_current_team_serve': can_current_team_serve,
        'recommended_for_flight': recommended_for_flight,
        # Before / after timeline for the delayed flight
        'before': {
            'std': m2t(orig_std),
            'dispatch': assignment.get('dispatch', ''),
            'svc_start': assignment.get('svc_start', ''),
            'svc_end': assignment.get('svc_end', ''),
            'team_free': assignment.get('team_free_at', ''),
        },
        'after': {
            'std': m2t(new_std),
            'dispatch': m2t(new_dispatch),
            'svc_start': m2t(new_svc_start),
            'svc_end': m2t(new_svc_end),
            'team_free': m2t(new_team_free),
        },
        'team_impact': {
            'team_id': team_id,
            'original_free': m2t(orig_team_free),
            'new_free': m2t(new_team_free),
            'delay_to_team': new_team_free - orig_team_free,
            'remaining_ops': len([o for o in current['team_ops'].get(team_id, [])
                                  if o.get('dispatch_min', 0) > orig_team_free]),
        },
        'cascade_conflicts': cascade_conflicts,
        'cascade_count': len(cascade_conflicts),
        'fixes': fixes,
        'auto_fixable': sum(1 for f in fixes if f['auto_fix']),
        'manual_required': sum(1 for f in fixes if not f['auto_fix']),
        'action_required': missed_deadline or shift_exceeded or len(cascade_conflicts) > 0,
        'updated_assignment': updated,
        # Store pending state for apply_delay()
        '_pending_new_std': new_std,
        '_pending_assignment': updated,
    }


def apply_delay(session_id: str, delay_result: dict, teams: List[Team],
                apply_auto_fixes: bool = True) -> dict:
    """
    Commit a delay to the session state.
    Optionally auto-applies the minimum-deviation fixes from handle_delay().
    This is the 'apply' step that handle_delay() prepares but doesn't commit.
    """
    session = load_session(session_id)
    if not session:
        return {'error': 'Session not found'}

    if not delay_result.get('success'):
        return {'error': 'Cannot apply an unsuccessful delay result'}

    current      = session['result']
    flight_num   = delay_result['flight']
    updated_a    = delay_result['_pending_assignment']
    team_id      = delay_result['team']
    flight_lookup = {f.flight_num: f for f in session.get('flights', [])}

    # Apply the delayed flight's new std
    new_assignments = []
    for a in current['assignments']:
        if a['flight'] == flight_num:
            new_assignments.append(updated_a)
        else:
            new_assignments.append(a)

    new_team_ops = {tid: list(ops) for tid, ops in current['team_ops'].items()}
    if team_id in new_team_ops:
        new_team_ops[team_id] = [
            (updated_a if op['flight'] == flight_num else op)
            for op in new_team_ops[team_id]
        ]

    auto_fixes_applied = []
    manual_needed = []

    # Apply auto-fixes (minimum-deviation: reassign conflicted flights).
    # apply_prov_loads tracks flights committed THIS loop so cap is enforced
    # even if handle_delay recommended the same team for multiple conflicts.
    # This is the hard gate — cap is a non-negotiable rule here.
    if apply_auto_fixes:
        apply_prov_loads: Dict[str, int] = {}
        for fix in delay_result.get('fixes', []):
            if fix.get('auto_fix'):
                cfn   = fix['flight']
                c_a   = next((a for a in new_assignments if a['flight'] == cfn), None)
                c_f   = flight_lookup.get(cfn)
                if not c_a or not c_f:
                    manual_needed.append(cfn)
                    continue
                new_team_id = fix['auto_fix']['team_id']
                t_obj = next((t for t in teams if t.team_id == new_team_id), None)
                if not t_obj:
                    manual_needed.append(cfn)
                    continue

                # ── Hard cap enforcement ───────────────────────────────────
                committed_load = len(new_team_ops.get(new_team_id, []))
                provisional    = apply_prov_loads.get(new_team_id, 0)
                if committed_load + provisional >= t_obj.cap:
                    # Recommended team is now at or over cap — find next best
                    fallback = None
                    for t_fb in sorted(teams, key=lambda t: len(new_team_ops.get(t.team_id, []))):
                        if t_fb.team_id in (team_id, new_team_id):
                            continue
                        fb_committed = len(new_team_ops.get(t_fb.team_id, []))
                        fb_prov      = apply_prov_loads.get(t_fb.team_id, 0)
                        if fb_committed + fb_prov >= t_fb.cap:
                            continue
                        fb_slot = _find_team_slot(
                            c_f, t_fb,
                            new_team_ops.get(t_fb.team_id, []),
                            new_assignments,
                            exclude_flights={cfn},
                        )
                        if fb_slot.get('dispatch') is not None:
                            fallback = t_fb
                            new_team_id = t_fb.team_id
                            t_obj = t_fb
                            break
                    if fallback is None:
                        manual_needed.append(cfn)
                        continue
                # ── End cap enforcement ────────────────────────────────────

                slot = _find_team_slot(
                    c_f,
                    t_obj,
                    new_team_ops.get(new_team_id, []),
                    new_assignments,
                    exclude_flights={cfn},
                )
                if slot.get('dispatch') is None:
                    manual_needed.append(cfn)
                    continue

                new_disp = slot['dispatch']
                new_ss   = new_disp + c_f.drv_out
                new_se   = new_ss + c_f.svc_time
                new_tf   = new_se + c_f.drv_back + TEAM_MIN_TURNAROUND
                new_truck_free = new_se + c_f.drv_back + DOCK_RELOAD

                reassigned_a = {**c_a,
                    'team': new_team_id,
                    'dispatch': m2t(new_disp), 'dispatch_min': new_disp,
                    'svc_start': m2t(new_ss),  'svc_start_min': new_ss,
                    'svc_end':   m2t(new_se),  'svc_end_min': new_se,
                    'team_free_at': m2t(new_tf), 'team_free_min': new_tf,
                    'dock_load': m2t(new_disp - DOCK_RELOAD),
                    'dock_load_min': max(0, new_disp - DOCK_RELOAD),
                    'truck_free_at': m2t(new_truck_free),
                    'truck_free_min': new_truck_free,
                    'auto_reassigned': True,
                    'auto_reassign_reason': f'Delay cascade from AA{flight_num}',
                }

                # Remove from old team, add to new
                old_team = c_a['team']
                new_assignments = [(reassigned_a if a['flight'] == cfn else a)
                                    for a in new_assignments]
                if old_team in new_team_ops:
                    new_team_ops[old_team] = [o for o in new_team_ops[old_team] if o['flight'] != cfn]
                new_team_ops.setdefault(new_team_id, []).append(reassigned_a)
                new_team_ops[new_team_id].sort(key=lambda x: x.get('dispatch_min', 0))
                apply_prov_loads[new_team_id] = apply_prov_loads.get(new_team_id, 0) + 1
                auto_fixes_applied.append({'flight': cfn, 'from': old_team, 'to': new_team_id})
            else:
                manual_needed.append(fix['flight'])

    new_summary = _rebuild_summary(teams, new_team_ops)
    new_stats   = _rebuild_stats(new_assignments, current.get('unassigned', []),
                                  [], teams, new_team_ops)

    new_result = {**current,
        'assignments': new_assignments,
        'team_ops': new_team_ops,
        'team_summary': new_summary,
        'stats': new_stats,
    }

    _commit_session(
        session_id,
        session,
        result=new_result,
        change_entry={
            'type': 'delay',
            'flight': flight_num,
            'dest': delay_result.get('dest', ''),
            'old_team': delay_result.get('team'),
            'new_team': delay_result.get('team'),
            'original_std': delay_result['original_std'],
            'new_std': delay_result['new_std'],
            'old_gate': delay_result.get('gate'),
            'new_gate': delay_result.get('gate'),
            'delay_mins': delay_result['delay_mins'],
            'auto_fixed': len(auto_fixes_applied),
            'manual_needed': len(manual_needed),
            'reason': f"Flight delayed from {delay_result['original_std']} to {delay_result['new_std']}",
            'children': [
                {
                    'type': 'auto_reassign',
                    'flight': fix['flight'],
                    'old_team': fix.get('from'),
                    'new_team': fix.get('to'),
                    'old_std': next((a.get('std') for a in current['assignments'] if a.get('flight') == fix['flight']), ''),
                    'new_std': next((a.get('std') for a in new_assignments if a.get('flight') == fix['flight']), ''),
                    'old_gate': next((a.get('gate') for a in current['assignments'] if a.get('flight') == fix['flight']), ''),
                    'new_gate': next((a.get('gate') for a in new_assignments if a.get('flight') == fix['flight']), ''),
                    'reason': 'Moved to under-cap feasible team after delay impact',
                    'mode': 'auto',
                    'status': 'applied',
                }
                for fix in auto_fixes_applied
            ] + [
                {
                    'type': 'manual_required',
                    'flight': fnum,
                    'old_team': next((a.get('team') for a in current['assignments'] if a.get('flight') == fnum), ''),
                    'new_team': '',
                    'old_std': next((a.get('std') for a in current['assignments'] if a.get('flight') == fnum), ''),
                    'new_std': next((a.get('std') for a in current['assignments'] if a.get('flight') == fnum), ''),
                    'old_gate': next((a.get('gate') for a in current['assignments'] if a.get('flight') == fnum), ''),
                    'new_gate': next((a.get('gate') for a in current['assignments'] if a.get('flight') == fnum), ''),
                    'reason': 'No clean under-cap auto-fix available',
                    'mode': 'auto',
                    'status': 'needs_review',
                }
                for fnum in manual_needed
            ],
            'timestamp': m2t_now(),
        },
        sync_flights=[flight_num] + [f['flight'] for f in auto_fixes_applied],
    )

    return {
        'success': True,
        'flight': flight_num,
        'delay_mins': delay_result['delay_mins'],
        'auto_fixes_applied': auto_fixes_applied,
        'manual_intervention_needed': manual_needed,
        'result': new_result,
    }


# ── Gate change handler ────────────────────────────────────────────────────
def handle_gate_change(session_id: str, flight_num: str, new_gate: str,
                       flights: List[Flight], teams: List[Team]) -> dict:
    """
    Recalculate dispatch timing when a flight's gate changes.
    Drive time changes → dispatch time changes → check if team is still available.
    """
    session = load_session(session_id)
    if not session:
        return {'error': 'Session not found'}

    current    = session['result']
    assignment = next((a for a in current['assignments'] if a['flight'] == flight_num), None)
    if not assignment:
        return {'error': f'AA{flight_num} not found or not assigned'}

    old_gate   = assignment.get('gate', 'B')
    old_drv    = drv(old_gate)
    new_drv    = drv(new_gate)
    drv_delta  = new_drv - old_drv
    svc        = SVC_TIMES.get(assignment.get('equip', ''), DEFAULT_SVC)
    team_id    = assignment['team']
    std_min    = assignment['std_min']
    fb         = _fin_buf(assignment)

    # New timing
    new_latest_d  = std_min - fb - svc - new_drv
    old_dispatch  = assignment.get('dispatch_min', 0)

    # Ideal: keep dispatch the same, adjust timing
    new_svc_start  = old_dispatch + new_drv
    new_svc_end    = new_svc_start + svc
    new_team_free  = new_svc_end + new_drv + TEAM_MIN_TURNAROUND

    # Check if service still makes deadline
    missed = new_svc_end > (std_min - fb)

    # Check cascade on team
    orig_team_free = assignment.get('team_free_min', new_team_free)
    cascade = _cascade(
        team_id,
        orig_team_free,
        new_team_free,
        current['team_ops'],
        next((t.shift_end for t in teams if t.team_id == team_id), 1440),
    )

    updated_a = {**assignment,
        'gate': new_gate,
        'dispatch_min': old_dispatch,
        'svc_start': m2t(new_svc_start), 'svc_start_min': new_svc_start,
        'svc_end':   m2t(new_svc_end),   'svc_end_min': new_svc_end,
        'team_free_at': m2t(new_team_free), 'team_free_min': new_team_free,
        'truck_free_at': m2t(new_svc_end + new_drv + DOCK_RELOAD),
        'truck_free_min': new_svc_end + new_drv + DOCK_RELOAD,
        'gate_changed': True, 'original_gate': old_gate,
    }

    # Commit gate change
    new_assignments = [(updated_a if a['flight'] == flight_num else a)
                        for a in current['assignments']]
    new_team_ops = {tid: list(ops) for tid, ops in current['team_ops'].items()}
    if team_id in new_team_ops:
        new_team_ops[team_id] = [(updated_a if op['flight'] == flight_num else op)
                                  for op in new_team_ops[team_id]]

    new_result = {**current,
        'assignments': new_assignments,
        'team_ops': new_team_ops,
        'team_summary': _rebuild_summary(teams, new_team_ops),
    }

    _commit_session(
        session_id,
        session,
        result=new_result,
        change_entry={
            'type': 'gate_change', 'flight': flight_num,
            'old_gate': old_gate, 'new_gate': new_gate,
            'old_team': team_id, 'new_team': team_id,
            'old_std': assignment.get('std'), 'new_std': updated_a.get('std'),
            'drive_delta': drv_delta, 'reason': f'Gate changed from {old_gate} to {new_gate}', 'timestamp': m2t_now(),
        },
        sync_flights=[flight_num],
    )

    return {
        'success': True,
        'flight': flight_num,
        'old_gate': old_gate,
        'new_gate': new_gate,
        'drive_time_change': f"{'+' if drv_delta >= 0 else ''}{drv_delta}min",
        'missed_deadline': missed,
        'cascade_conflicts': cascade,
        'result': new_result,
    }


# ── Manual reassign ────────────────────────────────────────────────────────
def _resolve_manual_children(session_id: str, flight_num: str, resolved_by_team: str) -> None:
    """
    After a manual reassign, scan the session change_log for any
    'manual_required' children matching this flight and mark them 'resolved'.
    Also recomputes the parent entry's top-level status so the dispatch log
    card shows the correct pill without needing a page reload.

    Parent status rules (evaluated after child resolution):
      - Any child still 'needs_review'  → parent status stays 'needs_review'
      - All children 'resolved'/'applied' → parent status becomes 'resolved'
      - No manual_required children at all → status unchanged
    """
    session = load_session(session_id)
    if not session:
        return
    log = list(session.get('change_log', []))
    changed = False
    for entry in log:
        children = entry.get('children') or []
        has_manual = False
        for child in children:
            if child.get('type') == 'manual_required' and child.get('flight') == flight_num:
                has_manual = True
                if child.get('status') != 'resolved':
                    child['status'] = 'resolved'
                    child['resolved_by'] = resolved_by_team
                    child['resolved_at'] = m2t_now()
                    changed = True
        # Recompute parent status based on all children
        if has_manual:
            still_pending = any(
                c.get('type') == 'manual_required' and c.get('status') == 'needs_review'
                for c in children
            )
            new_parent_status = 'needs_review' if still_pending else 'resolved'
            if entry.get('status') != new_parent_status:
                entry['status'] = new_parent_status
                changed = True
    if changed:
        updated = {**session, 'change_log': log}
        if updated.get('result') is not None:
            updated['result'] = _with_session_metadata(updated['result'], updated)
        save_session(session_id, updated)


def handle_reassign(session_id: str, flight_num: str, new_team_id: str,
                    flights: List[Flight], teams: List[Team],
                    force: bool = False) -> dict:
    """Move a flight to a different team. Cap is not a hard block — warns only.
    force=True allows timing overrides when no clean slot exists."""
    session = load_session(session_id)
    if not session:
        return {'error': 'Session not found'}

    current     = session['result']
    f           = next((fl for fl in flights if fl.flight_num == flight_num), None)
    if not f:
        return {'error': f'Flight {flight_num} not found'}

    cur_assign  = next((a for a in current['assignments'] if a['flight'] == flight_num), None)
    was_unassigned = cur_assign is None
    old_team_id = cur_assign['team'] if cur_assign else None

    new_team    = next((t for t in teams if t.team_id == new_team_id), None)
    if not new_team:
        return {'error': f'Team {new_team_id} not found'}

    existing_ops = sorted(current['team_ops'].get(new_team_id, []),
                          key=lambda o: o.get('dispatch_min', 0))
    slot = _find_team_slot(
        f,
        new_team,
        existing_ops,
        current['assignments'],
        exclude_flights={flight_num},
    )
    candidate_dispatch = slot.get('dispatch')

    if candidate_dispatch is None:
        if not force:
            # Explain what's actually blocking — existing ops or timing mismatch
            if slot.get('reason_code') == 'TRUCK_POOL_FULL':
                reason = 'no truck available within the feasible service window'
            elif existing_ops:
                first_free = max(op.get('team_free_min', 0) for op in existing_ops)
                if first_free > f.latest_dispatch:
                    reason = (f'schedule full until {m2t(first_free)}, '
                              f'but AA{flight_num} needs dispatch by {m2t(f.latest_dispatch)}')
                else:
                    reason = f'no available gap before the {m2t(f.latest_dispatch)} dispatch deadline'
            else:
                reason = (f'shift starts at {m2t(new_team.shift_start)}, '
                          f'flight needs dispatch by {m2t(f.latest_dispatch)}')
            return {
                'error': f'{new_team_id} cannot service AA{flight_num}: {reason}',
                'timing_conflict': True,
                'can_force': True,
            }
        # force=True — use latest_dispatch as best-effort slot
        candidate_dispatch = f.latest_dispatch

    # Cap check (warning only)
    team_load  = len(current['team_ops'].get(new_team_id, []))
    over_cap   = team_load >= new_team.cap

    # Compute new timing using the found gap slot
    disp       = min(candidate_dispatch, f.latest_dispatch)
    disp       = max(disp, f.earliest_dispatch)
    svc_s      = disp + f.drv_out
    svc_e      = svc_s + f.svc_time
    team_free  = svc_e + f.drv_back + TEAM_MIN_TURNAROUND
    truck_free = svc_e + f.drv_back + DOCK_RELOAD
    dock_load  = disp - DOCK_RELOAD

    new_entry = {
        'flight': f.flight_num, 'dest': f.dest, 'std': m2t(f.std), 'std_min': f.std,
        'type': f.dep_type, 'equip': f.equip, 'gate': f.gate,
        'is_ron': f.is_ron, 'is_short_turn': f.is_short_turn,
        'is_tight_turn': f.is_tight_turn, 'ground_mins': f.ground_mins,
        'is_intl': f.is_intl, 'is_wb': f.is_wb,
        'team': new_team_id, 'run_size': 1, 'stop_num': 1,
        'dock_load': m2t(dock_load), 'dispatch': m2t(disp),
        'svc_start': m2t(svc_s), 'svc_end': m2t(svc_e),
        'team_free_at': m2t(team_free), 'truck_free_at': m2t(truck_free),
        'dock_load_min': max(0, dock_load), 'truck_free_min': truck_free,
        'dispatch_min': disp, 'svc_start_min': svc_s,
        'svc_end_min': svc_e, 'team_free_min': team_free, 'reason': None,
        'nose': f.nose,
        'pax': getattr(f, '_dep_pax', None),
        'inbound_flight': getattr(f, '_inbound_flight', None),
        'inbound_origin': getattr(f, '_inbound_origin', None),
        'inbound_sta': getattr(f, '_inbound_sta', None),
        'ground_time': m2t(f.std - f.arrival_time) if f.arrival_time > 0 else None,
    }

    # Update assignments
    new_assignments = [a for a in current['assignments'] if a['flight'] != flight_num]
    new_assignments.append(new_entry)

    new_unassigned = [u for u in current.get('unassigned', []) if u['flight'] != flight_num]

    new_team_ops = {tid: list(ops) for tid, ops in current['team_ops'].items()}
    if old_team_id and old_team_id in new_team_ops:
        new_team_ops[old_team_id] = [o for o in new_team_ops[old_team_id]
                                      if o['flight'] != flight_num]
    new_team_ops.setdefault(new_team_id, []).append(new_entry)
    new_team_ops[new_team_id].sort(key=lambda x: x.get('dispatch_min', 0))

    new_summary = _rebuild_summary(teams, new_team_ops)
    new_stats   = _rebuild_stats(new_assignments, new_unassigned, flights, teams, new_team_ops)

    new_result = {**current,
        'assignments': new_assignments, 'unassigned': new_unassigned,
        'team_ops': new_team_ops, 'team_summary': new_summary, 'stats': new_stats,
    }

    _commit_session(
        session_id,
        session,
        result=new_result,
        change_entry={
            'type': 'manual_reassign',
            'flight': flight_num,
            'dest': f.dest,
            'old_team': old_team_id or 'UNASSIGNED',
            'new_team': new_team_id,
            'from_team': old_team_id or 'UNASSIGNED',
            'to_team': new_team_id,
            'old_std': (cur_assign or {}).get('std', ''),
            'new_std': new_entry.get('std', ''),
            'old_gate': (cur_assign or {}).get('gate', ''),
            'new_gate': new_entry.get('gate', ''),
            'reason': 'Dispatcher manually reassigned flight',
            'mode': 'manual',
            'timestamp': m2t_now(),
        },
        sync_flights=[flight_num],
    )

    # ── Resolve any pending manual_required children in the change log ──────
    # When the dispatcher manually reassigns a flight that was flagged as
    # 'needs_review' (a manual_required child on a delay/gate_change entry),
    # mark those children 'resolved' so the parent card updates its status
    # and stops showing the REVIEW pill.
    _resolve_manual_children(session_id, flight_num, new_team_id)

    return {
        'success': True,
        'flight': flight_num,
        'from_team': old_team_id or 'UNASSIGNED',
        'to_team': new_team_id,
        'was_unassigned': was_unassigned,
        'over_cap': over_cap,
        'timing_override': force,
        'timing': {'dispatch': m2t(disp), 'svc_start': m2t(svc_s), 'svc_end': m2t(svc_e)},
        'result': new_result,
    }


# ── Rebuild helpers ────────────────────────────────────────────────────────
def _rebuild_summary(teams: List[Team], team_ops: Dict) -> list:
    from scheduler_engine import m2t
    summary = []
    for t in sorted(teams, key=lambda x: x.shift_start):
        ops = team_ops.get(t.team_id, [])
        if not ops: continue
        run_count = len(set(o['dispatch_min'] for o in ops if o.get('dispatch_min') is not None))
        summary.append({
            'team_id':     t.team_id,
            'shift':       f"{m2t(t.shift_start)}–{m2t(t.shift_end%1440)}",
            'shift_start': t.shift_start,
            'team_type':   t.team_type,
            'ops':         run_count,
            'flight_count': len(ops),
            'cap':         t.cap,
            'first_std':   min(o['std'] for o in ops),
            'last_std':    max(o['std'] for o in ops),
            'flights':     [o['flight'] for o in ops],
            'operations':  ops,
        })
    return summary


def _rebuild_stats(assignments, unassigned, flights, teams, team_ops) -> dict:
    total     = len(assignments) + len(unassigned)
    assigned  = len(assignments)
    intl_a    = sum(1 for a in assignments if a.get('is_intl'))
    intl_t    = sum(1 for a in assignments if a.get('is_intl')) + \
                sum(1 for u in unassigned if u.get('is_intl'))
    ft = [t for t in teams if t.team_type == 'FT']
    fc = [len(team_ops.get(t.team_id, [])) for t in ft]
    rcounts: dict = {}
    for u in unassigned: rcounts[u.get('reason','?')] = rcounts.get(u.get('reason','?'),0)+1
    return {
        'total': total, 'assigned': assigned, 'unassigned': len(unassigned),
        'coverage_pct': round(100*assigned/max(total,1),1),
        'intl_assigned': intl_a, 'intl_total': intl_t,
        'teams_deployed': sum(1 for t in teams if team_ops.get(t.team_id)),
        'ft_min_ops': min(fc) if fc else 0,
        'ft_max_ops': max(fc) if fc else 0,
        'ft_avg_ops': round(sum(fc)/len(fc),1) if fc else 0,
        'unassigned_reasons': rcounts,
    }


# ── Run Editor ─────────────────────────────────────────────────────────────

def _recalc_run_timing(ordered_ops: list, dispatch_min: int) -> list:
    """
    Given an ordered list of assignment dicts and a dispatch minute,
    recompute all per-stop timing fields. Returns updated list.
    Each op must have: gate, equip, is_ron, is_intl, is_short_turn, is_tight_turn, std_min
    """
    from scheduler_engine import SVC_TIMES, DEFAULT_SVC, g2g, drv, DOCK_RELOAD, TEAM_MIN_TURNAROUND, m2t
    current = dispatch_min
    updated = []
    for k, op in enumerate(ordered_ops):
        gate   = op.get('gate', 'B')
        equip  = op.get('equip', '')
        svc    = SVC_TIMES.get(equip, DEFAULT_SVC)
        drv_t  = drv(gate)

        if k == 0:
            svc_s = current + drv_t
        else:
            prev_gate = ordered_ops[k-1].get('gate', 'B')
            svc_s = updated[-1]['svc_end_min'] + g2g(prev_gate, gate)

        svc_e      = svc_s + svc
        team_free  = svc_e + drv(ordered_ops[-1].get('gate', 'B')) + TEAM_MIN_TURNAROUND
        truck_free = svc_e + drv(ordered_ops[-1].get('gate', 'B')) + DOCK_RELOAD
        dock_load  = dispatch_min - DOCK_RELOAD

        updated.append({**op,
            'dispatch':      m2t(dispatch_min),
            'dispatch_min':  dispatch_min,
            'dock_load':     m2t(dock_load),
            'svc_start':     m2t(svc_s),
            'svc_end':       m2t(svc_e),
            'svc_start_min': svc_s,
            'svc_end_min':   svc_e,
            'team_free_at':  m2t(team_free),
            'truck_free_at': m2t(truck_free),
            'team_free_min': team_free,
            'run_size':      len(ordered_ops),
            'stop_num':      k + 1,
        })
    return updated


def _validate_run(ordered_ops: list) -> list:
    """
    Check all stops meet their service deadlines.
    Returns list of violation dicts (empty = all OK).
    """
    from scheduler_engine import m2t
    violations = []
    for op in ordered_ops:
        std_min    = op.get('std_min', 0)
        svc_end    = op.get('svc_end_min', 0)
        fb         = _fin_buf(op)
        deadline   = std_min - fb
        if svc_end > deadline:
            violations.append({
                'flight':   op['flight'],
                'dest':     op.get('dest', ''),
                'std':      op.get('std', ''),
                'svc_end':  m2t(svc_end),
                'deadline': m2t(deadline),
                'over_by':  svc_end - deadline,
            })
    return violations


def handle_run_edit(session_id: str, team_id: str, action: str,
                    flight_ids: list, flights: list, teams: list,
                    merge_dispatch: int = None) -> dict:
    """
    Reorder, split, or merge stops on a truck run.

    action='reorder': flight_ids is the new ordered stop list for ONE run.
                      Dispatch time taken from first flight's existing dispatch.
    action='split':   flight_ids contains exactly 1 flight to remove from its run.
                      That flight becomes a solo run; remainder stays together.
    action='merge':   flight_ids contains 2+ flights from DIFFERENT runs to merge.
                      merge_dispatch is the new dispatch minute (or auto-computed).
    """
    session = load_session(session_id)
    if not session:
        return {'error': 'Session not found'}

    current  = session['result']
    team_ops = {tid: list(ops) for tid, ops in current['team_ops'].items()}
    my_ops   = sorted(team_ops.get(team_id, []), key=lambda o: o.get('dispatch_min', 0))

    # Index all ops by flight number for quick lookup
    op_by_fn = {op['flight']: op for op in my_ops}

    # Validate all requested flights belong to this team
    for fn in flight_ids:
        if fn not in op_by_fn:
            return {'error': f'Flight {fn} is not on {team_id}'}

    if action == 'reorder':
        # Reorder stops within an existing run
        # All flights must share the same current dispatch_min (i.e. be on the same run)
        dispatches = {op_by_fn[fn].get('dispatch_min') for fn in flight_ids}
        if len(dispatches) > 1:
            return {'error': 'Reorder: all flights must be on the same run. '
                             'Use merge to combine flights from different runs.'}
        dispatch_min = dispatches.pop()
        ordered = [op_by_fn[fn] for fn in flight_ids]
        recalced = _recalc_run_timing(ordered, dispatch_min)
        violations = _validate_run(recalced)

        # Update team_ops: replace old run ops with recalced
        other_ops = [o for o in my_ops if o['flight'] not in flight_ids]
        team_ops[team_id] = sorted(other_ops + recalced, key=lambda o: o.get('dispatch_min', 0))

    elif action == 'split':
        # Remove one flight from its run, make it solo
        if len(flight_ids) != 1:
            return {'error': 'Split requires exactly one flight'}
        fn        = flight_ids[0]
        split_op  = op_by_fn[fn]
        old_disp  = split_op.get('dispatch_min', 0)

        # Find sibling ops (same dispatch_min)
        siblings  = [o for o in my_ops if o.get('dispatch_min') == old_disp and o['flight'] != fn]

        # Recalc the remaining run (without the split flight)
        remaining_recalced = _recalc_run_timing(siblings, old_disp) if siblings else []

        # The split flight needs its own dispatch — schedule it after all existing ops
        # that would conflict, or as a solo at a computed best slot
        all_other = [o for o in my_ops if o['flight'] != fn]
        free_after = max((o.get('team_free_min', 0) for o in all_other), default=0)
        from scheduler_engine import t2m, TEAM_MIN_TURNAROUND
        team_obj = next((t for t in teams if t.team_id == team_id), None)
        solo_disp = max(free_after, split_op.get('dispatch_min', 0),
                        team_obj.ready_at if team_obj else 0)
        solo_recalced = _recalc_run_timing([split_op], solo_disp)
        violations = _validate_run(remaining_recalced) + _validate_run(solo_recalced)

        other_ops = [o for o in my_ops if o['flight'] not in {fn} | {s['flight'] for s in siblings}]
        team_ops[team_id] = sorted(other_ops + remaining_recalced + solo_recalced,
                                   key=lambda o: o.get('dispatch_min', 0))

    elif action == 'merge':
        # Merge 2+ flights from potentially different runs into one run
        if len(flight_ids) < 2:
            return {'error': 'Merge requires at least 2 flights'}

        ordered = [op_by_fn[fn] for fn in flight_ids]

        # Auto-compute dispatch if not provided:
        # Use the earliest existing dispatch among the flights being merged
        if merge_dispatch is None:
            merge_dispatch = min(op.get('dispatch_min', 0) for op in ordered)

        recalced  = _recalc_run_timing(ordered, merge_dispatch)
        violations = _validate_run(recalced)

        merged_fns = set(flight_ids)
        other_ops  = [o for o in my_ops if o['flight'] not in merged_fns]
        team_ops[team_id] = sorted(other_ops + recalced, key=lambda o: o.get('dispatch_min', 0))

    else:
        return {'error': f'Unknown action: {action}'}

    # Rebuild assignments list from updated team_ops
    new_assignments = []
    all_assigned_fns = set()
    for tid, ops in team_ops.items():
        for op in ops:
            new_assignments.append(op)
            all_assigned_fns.add(op['flight'])

    new_unassigned = current.get('unassigned', [])
    new_summary = _rebuild_summary(teams, team_ops)
    new_stats   = _rebuild_stats(new_assignments, new_unassigned, flights, teams, team_ops)

    new_result = {**current,
        'assignments':  new_assignments,
        'unassigned':   new_unassigned,
        'team_ops':     team_ops,
        'team_summary': new_summary,
        'stats':        new_stats,
    }

    _commit_session(
        session_id,
        session,
        result=new_result,
        change_entry={
            'type':      f'run_{action}',
            'team':      team_id,
            'flights':   flight_ids,
            'reason':    f'Run {action} applied for {team_id}',
            'timestamp': m2t_now(),
        },
        sync_flights=flight_ids,
    )

    return {
        'success':    True,
        'action':     action,
        'team_id':    team_id,
        'flights':    flight_ids,
        'violations': violations,
        'result':     new_result,
    }
