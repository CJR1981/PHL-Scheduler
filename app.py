"""PHL Catering Scheduler — Flask API server with Live Operations"""
import os, tempfile, uuid
from flask import Flask, request, jsonify, send_from_directory
from scheduler_engine import load_flights, build_teams, solve
from live_ops import (save_session, load_session,
                      handle_sick_call, handle_delay, apply_delay,
                      handle_reassign, handle_gate_change, update_event_note, undo_last_action)
from impact_engine import (
    analyze_delay, analyze_gate_change, analyze_sick_call, analyze_truck_loss
)
from recovery_engine import (
    delay_recovery_options, truck_loss_recovery_options, apply_plan,
    preview_flight_swap, apply_flight_swap, preview_run_swap, apply_run_swap,
)

app = Flask(__name__, static_folder='static')

# ── Supabase startup check ────────────────────────────────────────────────
def _check_supabase():
    try:
        from supabase_client import check_connection
        result = check_connection()
        if result['ok']:
            bps = result.get('bid_periods', [])
            bp_name = bps[0]['name'] if bps else 'none'
            print(f"  ✓ Supabase connected  (active bid period: {bp_name})")
        else:
            print(f"  ⚠ Supabase unavailable: {result.get('error','?')[:80]}")
            print(f"    Sessions will use memory-only. Install: pip3 install supabase")
    except ImportError:
        print("  ⚠ supabase package not installed — run: pip3 install supabase")
    except Exception as e:
        print(f"  ⚠ Supabase check error: {e}")

# ── Core schedule generation ──────────────────────────────────────────────
@app.route('/')
def index():
    from flask import make_response
    resp = make_response(send_from_directory('static', 'index.html'))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma']        = 'no-cache'
    resp.headers['Expires']       = '0'
    return resp

@app.route('/pwa')
def pwa():
    from flask import make_response
    resp = make_response(send_from_directory('static', 'phl-pwa.html'))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma']        = 'no-cache'
    resp.headers['Expires']       = '0'
    return resp


@app.route('/api/schedule', methods=['POST'])
def schedule():
    try:
        dep_file  = request.files.get('departures')
        arr_file  = request.files.get('arrivals')
        ft_file   = request.files.get('ft_agents')
        pt_file   = request.files.get('pt_agents')
        day       = request.form.get('day_of_week', 'Saturday')
        time_lim  = int(request.form.get('time_limit', 240))  # 240s — Render free tier is ~3x slower than local
        shift     = request.form.get('shift', 'combined')  # morning | afternoon | combined
        sched_date = request.form.get('schedule_date', str(__import__('datetime').date.today()))
        auth_token = request.form.get('auth_token') or request.headers.get('X-Auth-Token','')

        if not dep_file or not arr_file:
            return jsonify({'error': 'Departures and arrivals CSVs required'}), 400

        with tempfile.NamedTemporaryFile(suffix='.csv', delete=False, mode='wb') as d:
            dep_path = d.name; d.write(dep_file.read())
        with tempfile.NamedTemporaryFile(suffix='.csv', delete=False, mode='wb') as a:
            arr_path = a.name; a.write(arr_file.read())

        ft_path = pt_path = None
        if ft_file:
            with tempfile.NamedTemporaryFile(suffix='.csv', delete=False, mode='wb') as f:
                ft_path = f.name; f.write(ft_file.read())
        if pt_file:
            with tempfile.NamedTemporaryFile(suffix='.csv', delete=False, mode='wb') as f:
                pt_path = f.name; f.write(pt_file.read())

        flights = load_flights(dep_path, arr_path)
        teams   = build_teams(ft_csv=ft_path, pt_csv=pt_path, day_of_week=day)
        result  = solve(flights, teams, time_limit=time_lim, shift=shift)

        # Enrich short-turn assignments with arrival/ground-time strings for export
        from scheduler_engine import m2t as _m2t
        flight_lookup = {fl.flight_num: fl for fl in flights}
        for a in result['assignments']:
            fl = flight_lookup.get(a['flight'])
            if fl and fl.is_short_turn:
                a['arrival_time_str'] = _m2t(fl.arrival_time)
                a['ground_time_str']  = f"{fl.std - fl.arrival_time} min"

        # Save copies of agent CSVs for equity finalisation before deleting temp files
        import shutil as _sh
        ft_copy = pt_copy = None
        _sessions_dir = os.path.join(os.path.dirname(__file__), '_session_data')
        os.makedirs(_sessions_dir, exist_ok=True)
        session_id = str(uuid.uuid4())
        if ft_path and os.path.exists(ft_path):
            ft_copy = os.path.join(_sessions_dir, f'{session_id}_ft.csv')
            _sh.copy2(ft_path, ft_copy)
            # Always overwrite 'latest' copy so Teams tab works after page refresh
            _sh.copy2(ft_path, os.path.join(_sessions_dir, 'latest_ft.csv'))
        if pt_path and os.path.exists(pt_path):
            pt_copy = os.path.join(_sessions_dir, f'{session_id}_pt.csv')
            _sh.copy2(pt_path, pt_copy)
            _sh.copy2(pt_path, os.path.join(_sessions_dir, 'latest_pt.csv'))

        for p in [dep_path, arr_path, ft_path, pt_path]:
            if p and os.path.exists(p): os.unlink(p)

        # Get user from auth token
        user = None
        try:
            from auth import verify_token as _vt
            user = _vt(auth_token) if auth_token else None
        except Exception: pass

        # Create in-memory session for live ops
        save_session(session_id, {
            'result':        result,
            'flights':       flights,
            'teams':         teams,
            'day':           day,
            'day_of_week':   day,
            'shift':         shift,
            'schedule_date': sched_date,
            'sick_calls':    [],
            'change_log':    [],
            'ft_path':       ft_copy,
            'pt_path':       pt_copy,
        })

        # Persist to Supabase in background thread — decoupled from HTTP
        # response so a gunicorn 502 timeout can't prevent the save.
        import threading as _threading
        def _bg_save(_sd=sched_date, _sh=shift, _res=result, _day=day,
                     _uid=user['user_id'] if user else None):
            try:
                from schedule_store import save_schedule as _ss
                _ss(_sd, _sh, _res, _day, created_by=_uid, status='live')
                print(f'[app] schedule saved Supabase: {_sd} {_sh}')
            except Exception as _e:
                import traceback as _tb
                print(f'[app] save_schedule failed: {_e}')
                print(_tb.format_exc())
        _threading.Thread(target=_bg_save, daemon=True).start()

        return jsonify({**result, 'session_id': session_id,
                        'shift': shift, 'schedule_date': sched_date})

    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500


# ── Live operations endpoints ─────────────────────────────────────────────
@app.route('/api/sick-call', methods=['POST'])
def sick_call():
    """Remove a team from the schedule and reassign their flights."""
    try:
        data       = request.get_json()
        session_id = data.get('session_id')
        team_id    = data.get('team_id')
        time_lim   = data.get('time_limit', 60)

        if not session_id or not team_id:
            return jsonify({'error': 'session_id and team_id required'}), 400

        session = load_session(session_id)
        if not session:
            return jsonify({'error': 'Session expired — regenerate schedule'}), 404

        result = handle_sick_call(
            session_id, team_id,
            session['flights'], session['teams'],
            time_limit=time_lim
        )
        return jsonify(result)

    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500


@app.route('/api/delay', methods=['POST'])
def delay():
    """Analyse a flight delay — returns full cascade impact + auto-fix recommendations.
    Does NOT commit changes. Call /api/delay/apply to commit."""
    try:
        data       = request.get_json()
        session_id = data.get('session_id')
        flight_num = data.get('flight')
        new_std    = data.get('new_std')

        if not all([session_id, flight_num, new_std]):
            return jsonify({'error': 'session_id, flight, new_std required'}), 400

        session = load_session(session_id)
        if not session:
            return jsonify({'error': 'Session expired — regenerate schedule'}), 404

        result = handle_delay(session_id, flight_num, new_std,
                               session['flights'], session['teams'])
        return jsonify(result)

    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500


@app.route('/api/delay/apply', methods=['POST'])
def delay_apply():
    """Commit a delay to the schedule. Optionally apply auto-fixes for cascade conflicts."""
    try:
        data           = request.get_json()
        session_id     = data.get('session_id')
        delay_result   = data.get('delay_result')   # the result from /api/delay
        auto_fix       = data.get('apply_auto_fixes', True)

        if not all([session_id, delay_result]):
            return jsonify({'error': 'session_id and delay_result required'}), 400

        session = load_session(session_id)
        if not session:
            return jsonify({'error': 'Session expired — regenerate schedule'}), 404

        result = apply_delay(session_id, delay_result, session['teams'], auto_fix)
        return jsonify(result)

    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500


@app.route('/api/gate-change', methods=['POST'])
def gate_change():
    """Update a flight's gate and recalculate all timing. Commits immediately."""
    try:
        data       = request.get_json()
        session_id = data.get('session_id')
        flight_num = data.get('flight')
        new_gate   = data.get('new_gate')

        if not all([session_id, flight_num, new_gate]):
            return jsonify({'error': 'session_id, flight, new_gate required'}), 400

        session = load_session(session_id)
        if not session:
            return jsonify({'error': 'Session expired'}), 404

        result = handle_gate_change(session_id, flight_num, new_gate,
                                     session['flights'], session['teams'])
        return jsonify(result)

    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500


@app.route('/api/reassign', methods=['POST'])
def reassign():
    """Manually move a flight to a different team."""
    try:
        data       = request.get_json()
        session_id = data.get('session_id')
        flight_num = data.get('flight')
        new_team   = data.get('team')

        if not all([session_id, flight_num, new_team]):
            return jsonify({'error': 'session_id, flight, team required'}), 400

        session = load_session(session_id)
        if not session:
            return jsonify({'error': 'Session expired — regenerate schedule'}), 404

        result = handle_reassign(
            session_id, flight_num, new_team,
            session['flights'], session['teams'],
            force=data.get('force', False)
        )
        return jsonify(result)

    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500




# ── Stage 3 impact-analysis endpoints ─────────────────────────────────────
@app.route('/api/live/event/analyze-delay', methods=['POST'])
def analyze_delay_event():
    try:
        data = request.get_json() or {}
        session_id = data.get('session_id')
        flight_num = data.get('flight')
        new_std = data.get('new_std')
        if not all([session_id, flight_num, new_std]):
            return jsonify({'error': 'session_id, flight, new_std required'}), 400
        session = load_session(session_id)
        if not session:
            return jsonify({'error': 'Session expired — regenerate schedule'}), 404
        return jsonify(analyze_delay(session_id, flight_num, new_std, session['flights'], session['teams']))
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500


@app.route('/api/live/event/analyze-gate-change', methods=['POST'])
def analyze_gate_change_event():
    try:
        data = request.get_json() or {}
        session_id = data.get('session_id')
        flight_num = data.get('flight')
        new_gate = data.get('new_gate')
        if not all([session_id, flight_num, new_gate]):
            return jsonify({'error': 'session_id, flight, new_gate required'}), 400
        session = load_session(session_id)
        if not session:
            return jsonify({'error': 'Session expired — regenerate schedule'}), 404
        return jsonify(analyze_gate_change(session_id, flight_num, new_gate, session['flights'], session['teams']))
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500


@app.route('/api/live/event/analyze-sick-call', methods=['POST'])
def analyze_sick_call_event():
    try:
        data = request.get_json() or {}
        session_id = data.get('session_id')
        team_id = data.get('team')
        if not all([session_id, team_id]):
            return jsonify({'error': 'session_id and team required'}), 400
        session = load_session(session_id)
        if not session:
            return jsonify({'error': 'Session expired — regenerate schedule'}), 404
        return jsonify(analyze_sick_call(session_id, team_id, session['flights'], session['teams']))
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500


@app.route('/api/live/event/analyze-truck-loss', methods=['POST'])
def analyze_truck_loss_event():
    try:
        data = request.get_json() or {}
        session_id = data.get('session_id')
        truck_id = data.get('truck_id')
        if not all([session_id, truck_id]):
            return jsonify({'error': 'session_id and truck_id required'}), 400
        session = load_session(session_id)
        if not session:
            return jsonify({'error': 'Session expired — regenerate schedule'}), 404
        return jsonify(analyze_truck_loss(session_id, truck_id, session['flights'], session['teams']))
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500



# ── Stage 4 recovery endpoints ────────────────────────────────────────────
@app.route('/api/live/recovery/delay-options', methods=['POST'])
def live_delay_recovery_options():
    try:
        data = request.get_json() or {}
        session_id = data.get('session_id')
        flight_num = data.get('flight')
        new_std = data.get('new_std')
        if not all([session_id, flight_num, new_std]):
            return jsonify({'error': 'session_id, flight, new_std required'}), 400
        session = load_session(session_id)
        if not session:
            return jsonify({'error': 'Session expired — regenerate schedule'}), 404
        return jsonify(delay_recovery_options(session_id, flight_num, new_std, session['flights'], session['teams']))
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500


@app.route('/api/live/recovery/truck-loss-options', methods=['POST'])
def live_truck_loss_recovery_options():
    try:
        data = request.get_json() or {}
        session_id = data.get('session_id')
        truck_id = data.get('truck_id')
        if not all([session_id, truck_id]):
            return jsonify({'error': 'session_id and truck_id required'}), 400
        session = load_session(session_id)
        if not session:
            return jsonify({'error': 'Session expired — regenerate schedule'}), 404
        return jsonify(truck_loss_recovery_options(session_id, truck_id, session['flights'], session['teams']))
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500


@app.route('/api/live/action/apply-plan', methods=['POST'])
def live_apply_plan():
    try:
        data = request.get_json() or {}
        session_id = data.get('session_id')
        plan = data.get('plan') or {}
        if not session_id or not plan:
            return jsonify({'error': 'session_id and plan required'}), 400
        session = load_session(session_id)
        if not session:
            return jsonify({'error': 'Session expired — regenerate schedule'}), 404
        return jsonify(apply_plan(session_id, plan, session['flights'], session['teams']))
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500


@app.route('/api/live/action/swap-flights/preview', methods=['POST'])
def live_preview_swap_flights():
    try:
        data = request.get_json() or {}
        session_id = data.get('session_id')
        flight_a = data.get('flight_a')
        flight_b = data.get('flight_b')
        if not all([session_id, flight_a, flight_b]):
            return jsonify({'error': 'session_id, flight_a, flight_b required'}), 400
        session = load_session(session_id)
        if not session:
            return jsonify({'error': 'Session expired — regenerate schedule'}), 404
        return jsonify(preview_flight_swap(session_id, flight_a, flight_b, session['flights'], session['teams']))
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500


@app.route('/api/live/action/swap-flights', methods=['POST'])
def live_apply_swap_flights():
    try:
        data = request.get_json() or {}
        session_id = data.get('session_id')
        flight_a = data.get('flight_a')
        flight_b = data.get('flight_b')
        if not all([session_id, flight_a, flight_b]):
            return jsonify({'error': 'session_id, flight_a, flight_b required'}), 400
        session = load_session(session_id)
        if not session:
            return jsonify({'error': 'Session expired — regenerate schedule'}), 404
        return jsonify(apply_flight_swap(session_id, flight_a, flight_b, session['flights'], session['teams'], force=bool(data.get('force', False))))
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500


@app.route('/api/live/action/swap-runs/preview', methods=['POST'])
def live_preview_swap_runs():
    try:
        data = request.get_json() or {}
        session_id = data.get('session_id')
        run_id_a = data.get('run_id_a')
        run_id_b = data.get('run_id_b')
        if not all([session_id, run_id_a, run_id_b]):
            return jsonify({'error': 'session_id, run_id_a, run_id_b required'}), 400
        session = load_session(session_id)
        if not session:
            return jsonify({'error': 'Session expired — regenerate schedule'}), 404
        return jsonify(preview_run_swap(session_id, run_id_a, run_id_b, session['flights'], session['teams']))
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500


@app.route('/api/live/action/swap-runs', methods=['POST'])
def live_apply_swap_runs():
    try:
        data = request.get_json() or {}
        session_id = data.get('session_id')
        run_id_a = data.get('run_id_a')
        run_id_b = data.get('run_id_b')
        if not all([session_id, run_id_a, run_id_b]):
            return jsonify({'error': 'session_id, run_id_a, run_id_b required'}), 400
        session = load_session(session_id)
        if not session:
            return jsonify({'error': 'Session expired — regenerate schedule'}), 404
        return jsonify(apply_run_swap(session_id, run_id_a, run_id_b, session['flights'], session['teams'], force=bool(data.get('force', False))))
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500


def _fmt_minute(v):
    try:
        v = int(v or 0)
    except Exception:
        return ''
    h = (v // 60) % 24
    m = v % 60
    return f"{h:02d}:{m:02d}"


def _dispatch_board_payload(session: dict) -> dict:
    result = session.get('result', {}) or {}
    assignments = list(result.get('assignments', []) or [])
    unassigned = list(result.get('unassigned', []) or [])
    change_log = list(session.get('change_log', []) or result.get('_change_log', []) or [])
    team_summary = {t.get('team_id'): t for t in result.get('team_summary', []) or []}

    try:
        from schedule_store import load_schedule as _load_schedule
        daily_saved = bool(_load_schedule(session.get('schedule_date'), session.get('shift')))
    except Exception:
        daily_saved = False

    flight_rows = {}
    for row in assignments + unassigned:
        flight_rows[str(row.get('flight'))] = row

    teams = {}
    for row in assignments:
        tid = row.get('team') or 'UNASSIGNED'
        tmeta = team_summary.get(tid, {})
        team = teams.setdefault(tid, {
            'team_id': tid,
            'shift': tmeta.get('shift', ''),
            'shift_start': tmeta.get('shift_start', 0),
            'cap': tmeta.get('cap', 0),
            'team_type': tmeta.get('team_type', ''),
            'truck_id': row.get('truck_id') or 'UNASSIGNED',
            'current_count': 0,
            'flights': [],
        })
        team['current_count'] += 1
        team['truck_id'] = row.get('truck_id') or team['truck_id']
        team['flights'].append({
            'flight': row.get('flight'),
            'dest': row.get('dest',''),
            'std': row.get('std',''),
            'gate': row.get('gate',''),
            'dispatch': row.get('dispatch',''),
            'run_id': row.get('run_id',''),
            'truck_id': row.get('truck_id',''),
            'type': row.get('type') or row.get('dep_type',''),
            'delayed': bool(row.get('delayed')),
            'auto_reassigned': bool(row.get('auto_reassigned')),
            'original_team': row.get('original_team') or '',
            'original_std': row.get('original_std') or '',
            'change_pills': [],
        })

    # annotate changed flights from change log
    changed_flights = set()
    unresolved = 0
    auto_actions = 0
    manual_actions = 0
    for entry in change_log:
        if entry.get('status') in {'needs_review', 'warning'}:
            unresolved += 1
        if entry.get('mode') == 'auto':
            auto_actions += 1
        else:
            manual_actions += 1
        for event in [entry] + list(entry.get('children', []) or []):
            fl = str(event.get('flight') or '')
            if not fl:
                continue
            changed_flights.add(fl)
            row = flight_rows.get(fl, {})
            tid = row.get('team')
            if tid in teams:
                for item in teams[tid]['flights']:
                    if str(item.get('flight')) == fl:
                        typ = event.get('type', '')
                        label = 'REVIEW' if event.get('status') == 'needs_review' else typ.replace('_',' ').upper()
                        if label and label not in item['change_pills']:
                            item['change_pills'].append(label)

    for team in teams.values():
        team['flights'].sort(key=lambda x: x.get('dispatch') or x.get('std') or '')

    near_cap = sum(1 for t in teams.values() if t.get('cap') and t['current_count'] >= max(t['cap'] - 1, 1))
    over_cap = sum(1 for t in teams.values() if t.get('cap') and t['current_count'] > t['cap'])

    event_log = []
    for entry in change_log[::-1]:
        parent = {
            'event_id': entry.get('event_id'),
            'timestamp': entry.get('timestamp',''),
            'event_type': entry.get('type','change'),
            'title': (entry.get('type','change').replace('_',' ').title()),
            'flight': entry.get('flight',''),
            'old_team': entry.get('old_team') or entry.get('from_team') or entry.get('team') or '',
            'new_team': entry.get('new_team') or entry.get('to_team') or entry.get('team') or '',
            'old_std': entry.get('old_std') or entry.get('original_std') or '',
            'new_std': entry.get('new_std') or '',
            'old_gate': entry.get('old_gate') or entry.get('from_gate') or '',
            'new_gate': entry.get('new_gate') or entry.get('to_gate') or '',
            'reason': entry.get('reason') or '',
            'user': entry.get('user',''),
            'mode': entry.get('mode','manual'),
            'status': entry.get('status','applied'),
            'note': entry.get('note',''),
            'children': [],
        }
        for child in entry.get('children', []) or []:
            parent['children'].append({
                'event_id': child.get('event_id'),
                'timestamp': child.get('timestamp', entry.get('timestamp','')),
                'event_type': child.get('type','change'),
                'flight': child.get('flight',''),
                'old_team': child.get('old_team') or child.get('from_team') or '',
                'new_team': child.get('new_team') or child.get('to_team') or '',
                'old_std': child.get('old_std') or '',
                'new_std': child.get('new_std') or '',
                'old_gate': child.get('old_gate') or '',
                'new_gate': child.get('new_gate') or '',
                'reason': child.get('reason') or '',
                'user': child.get('user', entry.get('user','')),
                'mode': child.get('mode', entry.get('mode','auto')),
                'status': child.get('status','applied'),
                'note': child.get('note',''),
            })
        event_log.append(parent)

    return {
        'success': True,
        'summary': {
            'changed_flights': len(changed_flights),
            'unresolved_exceptions': unresolved + len(unassigned),
            'manual_actions': manual_actions,
            'auto_actions': auto_actions,
            'near_cap_teams': near_cap,
            'over_cap_teams': over_cap,
        },
        'persistence': {
            'session_active': True,
            'session_mode': 'memory+supabase',
            'saved_to_daily': daily_saved,
            'schedule_date': session.get('schedule_date'),
            'shift': session.get('shift'),
        },
        'event_log': event_log,
        'teams': sorted(teams.values(), key=lambda x: (x.get('shift_start',0), x.get('team_id',''))),
        'unassigned': unassigned,
        'undo': {'available': bool(session.get('_history')), 'count': len(session.get('_history', []))},
    }


@app.route('/api/live/dispatch-board', methods=['GET'])
def live_dispatch_board():
    try:
        session_id = request.args.get('session_id', '').strip()
        if not session_id:
            return jsonify({'error': 'session_id required'}), 400
        session = load_session(session_id)
        if not session:
            return jsonify({'error': 'Session not found'}), 404
        return jsonify(_dispatch_board_payload(session))
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500



@app.route('/api/session/<session_id>', methods=['GET'])
def get_session(session_id):
    """Fetch current schedule state for a session."""
    session = load_session(session_id)
    if not session:
        return jsonify({'error': 'Session not found'}), 404
    return jsonify({
        'result': session['result'],
        'sick_calls': session.get('sick_calls', []),
        'change_log': session.get('change_log', []),
    })


@app.route('/api/live/event-note', methods=['POST'])
def live_event_note():
    try:
        data = request.get_json() or {}
        session_id = data.get('session_id')
        event_id = data.get('event_id')
        note = data.get('note', '')
        if not session_id or not event_id:
            return jsonify({'error': 'session_id and event_id required'}), 400
        result = update_event_note(session_id, event_id, note)
        if result.get('error'):
            return jsonify(result), 400
        return jsonify(result)
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500


@app.route('/api/live/action/undo-last', methods=['POST'])
def live_undo_last():
    try:
        data = request.get_json() or {}
        session_id = data.get('session_id')
        if not session_id:
            return jsonify({'error': 'session_id required'}), 400
        result = undo_last_action(session_id)
        if result.get('error'):
            return jsonify(result), 400
        return jsonify(result)
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500


from export import generate_excel

@app.route('/api/export', methods=['POST'])
def export():
    """Generate and download the 9-tab Excel workbook."""
    try:
        data = request.get_json()
        session_id = data.get('session_id')

        session = load_session(session_id)
        if not session:
            return jsonify({'error': 'Session not found — regenerate schedule first'}), 404

        result     = session['result']
        change_log = session.get('change_log', [])
        day        = session.get('day', 'Saturday')

        xlsx_bytes = generate_excel(result, change_log)

        date_str = __import__('datetime').datetime.now().strftime('%Y%m%d_%H%M')
        filename = f"PHL_Catering_{day}_{date_str}.xlsx"

        from flask import Response
        return Response(
            xlsx_bytes,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            headers={'Content-Disposition': f'attachment; filename="{filename}"'}
        )

    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500


@app.route('/api/agent', methods=['POST'])
def agent_chat():
    """Conversational agent endpoint. Accepts a message and returns a response."""
    try:
        data        = request.get_json()
        session_id  = data.get('session_id')
        user_msg    = data.get('message', '').strip()

        if not session_id or not user_msg:
            return jsonify({'error': 'session_id and message required'}), 400

        session = load_session(session_id)
        if not session:
            return jsonify({'error': 'Session expired — regenerate schedule first'}), 404

        from agent import run_agent_turn

        # Retrieve conversation history from session (or start fresh)
        history = session.get('agent_history', [])

        # Determine base URL for agent to call back into the API
        base_url = request.host_url.rstrip('/')

        result = run_agent_turn(
            user_message=user_msg,
            conversation_history=history,
            schedule_data=session['result'],
            session_id=session_id,
            api_base_url=base_url,
        )

        # If the agent made changes, the session result is now stale — refresh from live_ops
        # The agent.py already updates schedule_data in-place; reflect that in the session
        if result.get('schedule_updated'):
            fresh_session = load_session(session_id)
            if fresh_session:
                session = fresh_session

        # Save updated conversation history back to session
        save_session(session_id, {
            **session,
            'agent_history': result['messages'],
        })

        return jsonify({
            'response':        result['response'],
            'actions_taken':   result['actions_taken'],
            'schedule_updated': result['schedule_updated'],
            # If schedule changed, send back updated result for frontend to re-render
            'result':          session.get('result') if result['schedule_updated'] else None,
        })

    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500


@app.route('/api/agent/reset', methods=['POST'])
def agent_reset():
    """Clear the agent conversation history for a session."""
    try:
        data = request.get_json()
        sid  = data.get('session_id')
        session = load_session(sid)
        if session:
            save_session(sid, {**session, 'agent_history': []})
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Session listing (Supabase) ─────────────────────────────────────────────
@app.route('/api/sessions', methods=['GET'])
def list_sessions():
    """List recent sessions from Supabase for the session picker."""
    try:
        from supabase_client import list_sessions_db
        sessions = list_sessions_db(limit=30)
        return jsonify({'sessions': sessions})
    except Exception as e:
        return jsonify({'sessions': [], 'error': str(e)})


# get_session already defined above


# ── Equity endpoints ───────────────────────────────────────────────────────
@app.route('/api/equity/finalise', methods=['POST'])
def equity_finalise():
    """
    Finalise a day's schedule and commit agent equity points to Supabase.
    Body: { session_id, schedule_date, ft_agents (file), pt_agents (file) }
    """
    try:
        data        = request.get_json() or {}
        sid         = data.get('session_id')
        sched_date  = data.get('schedule_date')  # YYYY-MM-DD
        day_of_week = data.get('day_of_week', 'Tuesday')

        session = load_session(sid)
        if not session:
            return jsonify({'error': 'Session not found'}), 404

        assignments = session.get('result', {}).get('assignments', [])
        if not assignments:
            return jsonify({'error': 'No assignments in session'}), 400

        from supabase_client import get_active_bid_period, commit_day_equity
        from equity_engine   import calc_agent_equity

        bid = get_active_bid_period()
        if not bid:
            return jsonify({'error': 'No active bid period — create one first'}), 400

        # Use the bid CSVs from session metadata if available
        ft_path = session.get('ft_path')
        pt_path = session.get('pt_path')

        agent_rows = calc_agent_equity(
            assignments=assignments,
            ft_csv=ft_path, pt_csv=pt_path,
            day_of_week=day_of_week
        )

        ok = commit_day_equity(
            session_id=sid,
            schedule_date=sched_date or str(__import__('datetime').date.today()),
            day_of_week=day_of_week,
            bid_period_id=bid['id'],
            agent_rows=agent_rows
        )

        if ok:
            return jsonify({
                'success': True,
                'agents_committed': len(agent_rows),
                'bid_period': bid['name'],
                'agent_rows': agent_rows[:5],  # preview first 5
            })
        else:
            return jsonify({'error': 'Failed to commit to Supabase'}), 500

    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500


@app.route('/api/equity/leaderboard', methods=['GET'])
def equity_leaderboard():
    """Return the full equity leaderboard for the active bid period."""
    try:
        from supabase_client import get_active_bid_period, get_equity_leaderboard
        bid = get_active_bid_period()
        if not bid:
            return jsonify({'leaderboard': [], 'bid_period': None})
        board = get_equity_leaderboard(bid['id'])
        return jsonify({'leaderboard': board, 'bid_period': bid})
    except Exception as e:
        return jsonify({'error': str(e), 'leaderboard': []}), 500


@app.route('/api/equity/bid-period', methods=['POST'])
def create_bid_period():
    """Create a new bid period."""
    try:
        data = request.get_json()
        from supabase_client import create_bid_period as _create
        bp = _create(data['name'], data['start_date'], data['end_date'])
        return jsonify({'success': True, 'bid_period': bp})
    except Exception as e:
        return jsonify({'error': str(e)}), 500



# ── Auth endpoints ─────────────────────────────────────────────────────────
@app.route('/api/auth/login', methods=['POST'])
def auth_login():
    data = request.get_json() or {}
    result = __import__('auth').login(
        data.get('username',''), data.get('password',''))
    if 'error' in result:
        return jsonify(result), 401
    return jsonify(result)

@app.route('/api/auth/logout', methods=['POST'])
def auth_logout():
    data = request.get_json() or {}
    token = data.get('auth_token') or request.headers.get('X-Auth-Token','')
    __import__('auth').logout(token)
    return jsonify({'success': True})

@app.route('/api/auth/verify', methods=['POST'])
def auth_verify():
    data  = request.get_json() or {}
    token = data.get('auth_token') or request.headers.get('X-Auth-Token','')
    user  = __import__('auth').verify_token(token)
    if not user:
        return jsonify({'valid': False}), 401
    return jsonify({'valid': True, **user})

@app.route('/api/auth/users', methods=['GET'])
def auth_users():
    """Manager only — list all users."""
    token = request.headers.get('X-Auth-Token','')
    user  = __import__('auth').verify_token(token)
    if not user or user['role'] != 'manager':
        return jsonify({'error': 'Managers only'}), 403
    return jsonify({'users': __import__('auth').list_users()})

@app.route('/api/auth/users', methods=['POST'])
def auth_create_user():
    """Manager only — create a new user."""
    token = request.headers.get('X-Auth-Token','')
    user  = __import__('auth').verify_token(token)
    if not user or user['role'] != 'manager':
        return jsonify({'error': 'Managers only'}), 403
    data = request.get_json() or {}
    result = __import__('auth').create_user(
        data.get('username',''), data.get('password',''),
        data.get('display_name',''), data.get('role','agent'),
        data.get('team_id'))
    return jsonify(result)


@app.route('/api/auth/users/<user_id>', methods=['PATCH'])
def auth_update_user(user_id):
    """Manager only — update display_name, role, team_id, is_active."""
    token = request.headers.get('X-Auth-Token','')
    caller = __import__('auth').verify_token(token)
    if not caller or caller['role'] != 'manager':
        return jsonify({'error': 'Managers only'}), 403
    data = request.get_json() or {}
    allowed = {k: v for k, v in data.items()
               if k in ('display_name','role','team_id','is_active')}
    if not allowed:
        return jsonify({'error': 'No valid fields to update'}), 400
    try:
        from supabase_client import get_client
        r = get_client().table('users').update(allowed).eq('id', user_id).execute()
        return jsonify({'success': True, 'user': r.data[0] if r.data else {}})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/auth/users/<user_id>/password', methods=['POST'])
def auth_reset_password(user_id):
    """Manager only — set a new password for any user."""
    token = request.headers.get('X-Auth-Token','')
    caller = __import__('auth').verify_token(token)
    if not caller or caller['role'] != 'manager':
        return jsonify({'error': 'Managers only'}), 403
    data = request.get_json() or {}
    new_pw = data.get('password','').strip()
    if len(new_pw) < 8:
        return jsonify({'error': 'Password must be at least 8 characters'}), 400
    try:
        import bcrypt
        from supabase_client import get_client
        hashed = bcrypt.hashpw(new_pw.encode(), bcrypt.gensalt(10)).decode()
        get_client().table('users').update({'password': hashed}).eq('id', user_id).execute()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/auth/me/password', methods=['POST'])
def auth_change_own_password():
    """Any authenticated user — change their own password."""
    token = request.headers.get('X-Auth-Token','')
    caller = __import__('auth').verify_token(token)
    if not caller:
        return jsonify({'error': 'Not authenticated'}), 401
    data = request.get_json() or {}
    current_pw = data.get('current_password','')
    new_pw     = data.get('new_password','').strip()
    if len(new_pw) < 8:
        return jsonify({'error': 'New password must be at least 8 characters'}), 400
    try:
        import bcrypt
        from supabase_client import get_client
        sb = get_client()
        row = sb.table('users').select('password').eq('id', caller['user_id']).execute()
        if not row.data:
            return jsonify({'error': 'User not found'}), 404
        if not bcrypt.checkpw(current_pw.encode(), row.data[0]['password'].encode()):
            return jsonify({'error': 'Current password is incorrect'}), 403
        hashed = bcrypt.hashpw(new_pw.encode(), bcrypt.gensalt(10)).decode()
        sb.table('users').update({'password': hashed}).eq('id', caller['user_id']).execute()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Daily schedule endpoints ───────────────────────────────────────────────

@app.route('/api/daily/<schedule_date>/<shift>', methods=['GET'])
def get_daily_schedule(schedule_date, shift):
    """Load a saved schedule by date + shift."""
    try:
        token = request.headers.get('X-Auth-Token','')
        from auth import verify_token as _vt
        user = _vt(token) if token else None
        if not user:
            return jsonify({'error': 'Not authenticated'}), 401

        from schedule_store import load_schedule, load_combined
        if shift == 'combined':
            if user['role'] not in ('manager', 'admin', 'supervisor'):
                return jsonify({'error': 'Manager/admin/supervisor only'}), 403
            rec = load_combined(schedule_date)
        else:
            rec = load_schedule(schedule_date, shift)

        if not rec:
            return jsonify({'found': False, 'schedule_date': schedule_date, 'shift': shift})

        result = rec.get('result', {})
        return jsonify({
            'found':         True,
            'shift':         shift,
            'schedule_date': schedule_date,
            'day_of_week':   rec.get('day_of_week', rec.get('day', '')),
            'status':        rec.get('status','live'),
            'updated_at':    rec.get('updated_at',''),
            **result,
        })
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500


@app.route('/api/daily/<schedule_date>/view/<view_name>', methods=['GET'])
def get_daily_view(schedule_date, view_name):
    """
    Load a schedule filtered by view: morning | afternoon | wb_intl | full.
    All roles can access all views. The schedule must be saved (combined shift).
    """
    try:
        token = request.headers.get('X-Auth-Token','')
        from auth import verify_token as _vt
        user = _vt(token) if token else None
        if not user:
            return jsonify({'error': 'Not authenticated'}), 401

        if view_name not in ('morning', 'afternoon', 'wb_intl', 'full'):
            return jsonify({'error': f'Invalid view: {view_name}. '
                           f'Must be morning|afternoon|wb_intl|full'}), 400

        from schedule_store import load_full_day, filter_result_by_view
        rec = load_full_day(schedule_date)
        if not rec:
            return jsonify({'found': False, 'schedule_date': schedule_date,
                           'view': view_name})

        full_result = rec.get('result', {})
        filtered    = filter_result_by_view(full_result, view_name)

        return jsonify({
            'found':         True,
            'view':          view_name,
            'schedule_date': schedule_date,
            'day_of_week':   rec.get('day_of_week', ''),
            'status':        rec.get('status', 'live'),
            'updated_at':    rec.get('updated_at', ''),
            **filtered,
        })
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500


@app.route('/api/daily/<schedule_date>/confirm', methods=['POST'])
def confirm_schedule(schedule_date):
    """
    Confirm a schedule — sets status to 'confirmed'.
    Only admin, manager, supervisor can confirm.
    """
    try:
        token = request.headers.get('X-Auth-Token','')
        from auth import verify_token as _vt
        user = _vt(token) if token else None
        if not user:
            return jsonify({'error': 'Not authenticated'}), 401
        if user['role'] not in ('manager', 'admin', 'supervisor'):
            return jsonify({'error': 'Insufficient permissions — manager/admin/supervisor required'}), 403

        from schedule_store import get_client
        sb = get_client()

        # Update all shifts for this date to 'confirmed'
        sb.table('daily_schedules').update({
            'status': 'confirmed',
            'updated_at': __import__('datetime').datetime.utcnow().isoformat(),
        }).eq('schedule_date', schedule_date).execute()

        from schedule_store import log_modification
        log_modification(schedule_date, 'combined',
                        user.get('id', 0), user.get('display_name', user.get('username', '')),
                        'confirm', f"Schedule confirmed by {user.get('display_name', user.get('username', ''))}",
                        {'confirmed_by': user.get('username', '')})

        return jsonify({'ok': True, 'status': 'confirmed', 'schedule_date': schedule_date})
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500


@app.route('/api/daily/dates', methods=['GET'])
def get_schedule_dates():
    """Manager only — list dates with saved schedules for the date picker."""
    try:
        token = request.headers.get('X-Auth-Token','')
        from auth import verify_token as _vt
        user = _vt(token) if token else None
        if not user or user['role'] not in ('manager', 'admin', 'supervisor'):
            return jsonify({'error': 'Manager/admin/supervisor only'}), 403
        from schedule_store import list_schedule_dates
        return jsonify({'dates': list_schedule_dates(60)})
    except Exception as e:
        return jsonify({'error': str(e), 'dates': []}), 500


@app.route('/api/daily/<schedule_date>/<shift>/modifications', methods=['GET'])
def get_daily_modifications(schedule_date, shift):
    """Get the modification log for a schedule."""
    try:
        token = request.headers.get('X-Auth-Token','')
        from auth import verify_token as _vt
        user = _vt(token) if token else None
        if not user:
            return jsonify({'error': 'Not authenticated'}), 401
        from schedule_store import get_modifications
        mods = get_modifications(schedule_date, shift)
        return jsonify({'modifications': mods})
    except Exception as e:
        return jsonify({'error': str(e), 'modifications': []}), 500


@app.route('/api/daily/<schedule_date>/<shift>/liveops', methods=['POST'])
def daily_liveops(schedule_date, shift):
    """
    Apply a live ops change (sick_call / delay / reassign) to a persisted schedule.
    Updates Supabase immediately. All polling clients will pick up the change.
    """
    try:
        token = request.headers.get('X-Auth-Token','')
        from auth import verify_token as _vt
        user = _vt(token) if token else None
        if not user:
            return jsonify({'error': 'Not authenticated'}), 401
        if user['role'] not in ('manager','supervisor'):
            return jsonify({'error': 'Insufficient permissions'}), 403

        data    = request.get_json() or {}
        op_type = data.get('type')   # sick_call | delay | reassign | gate_change
        payload = data.get('payload', {})

        # Load current schedule from Supabase
        from schedule_store import load_schedule, update_schedule_result, log_modification
        rec = load_schedule(schedule_date, shift)
        if not rec:
            return jsonify({'error': 'Schedule not found'}), 404

        # Apply the live op using existing live_ops engine
        from live_ops import handle_sick_call, handle_delay, apply_delay, handle_reassign, handle_gate_change
        from scheduler_engine import Flight as _Flight, Team as _Team
        current_result = rec.get('result', {})

        # ── Reconstruct Flight objects from saved result ───────────────────
        def _hhmm(s):
            s = str(s or '').strip().replace('\u2013', '-').replace('\u2014', '-')
            if not s:
                return 0
            if ':' in s:
                hh, mm = s.split(':', 1)
                return int(hh) * 60 + int(mm)
            digits = ''.join(ch for ch in s if ch.isdigit())
            if len(digits) < 3:
                raise ValueError(f'Invalid time: {s}')
            digits = digits.zfill(4)
            return int(digits[:2]) * 60 + int(digits[2:4])

        def _persistable_result(result_payload, session_payload):
            return {
                **(result_payload or {}),
                '_change_log': list(session_payload.get('change_log', [])),
                '_sick_calls': list(session_payload.get('sick_calls', [])),
            }

        def _reconstruct_flights(result):
            flights = []
            for a in result.get('assignments', []) + result.get('unassigned', []):
                try:
                    f = _Flight(
                        flight_num  = str(a.get('flight', '')),
                        dest        = a.get('dest', '???'),
                        std         = int(a.get('std_min', 0)),
                        equip       = a.get('equip', '738'),
                        gate        = a.get('gate', 'B1'),
                        dep_type    = a.get('type', a.get('dep_type', 'Domestic')),
                        nose        = a.get('nose', ''),
                        is_ron      = bool(a.get('is_ron', False)),
                        arrival_time= int(a.get('arrival_min', -1)),
                    )
                    if a.get('pax') is not None:
                        setattr(f, '_dep_pax', a.get('pax'))
                    if a.get('inbound_flight'):
                        setattr(f, '_inbound_flight', a.get('inbound_flight'))
                    if a.get('inbound_origin'):
                        setattr(f, '_inbound_origin', a.get('inbound_origin'))
                    if a.get('inbound_sta'):
                        setattr(f, '_inbound_sta', a.get('inbound_sta'))
                    flights.append(f)
                except Exception:
                    pass
            return flights

        # ── Reconstruct Team objects from team_summary ─────────────────────
        def _reconstruct_teams(result):
            teams = []
            for t in result.get('team_summary', []):
                try:
                    raw_shift = t.get('shift', '0500-1330')
                    # Normalize em-dash (–) and en-dash (—) to plain hyphen before splitting
                    raw_shift = raw_shift.replace('\u2013', '-').replace('\u2014', '-')
                    parts = raw_shift.split('-')
                    sh_s = _hhmm(parts[0])
                    sh_e = _hhmm(parts[1])
                    teams.append(_Team(
                        team_id    = t['team_id'],
                        shift_start= sh_s,
                        shift_end  = sh_e,
                        cap        = int(t.get('cap', 8)),
                        team_type  = t.get('team_type', 'U30 TRK'),
                    ))
                except Exception:
                    pass
            return teams

        reconstructed_flights = _reconstruct_flights(current_result)
        reconstructed_teams   = _reconstruct_teams(current_result)

        # Build an in-memory session for live_ops to work on
        session_id = f'{schedule_date}_{shift}'
        save_session(session_id, {
            'result':        current_result,
            'flights':       reconstructed_flights,
            'teams':         reconstructed_teams,
            'shift':         shift,
            'schedule_date': schedule_date,
            'sick_calls':    current_result.get('_sick_calls', []),
            'change_log':    current_result.get('_change_log', []),
        })

        result = {'error': 'Unknown operation'}
        summary = op_type

        if op_type == 'sick_call':
            result = handle_sick_call(
                session_id, payload.get('team_id'),
                reconstructed_flights, reconstructed_teams,
                time_limit=payload.get('time_limit', 60)
            )
            summary = f"Sick call: {payload.get('team_id')}"
        elif op_type == 'delay':
            analysis = handle_delay(
                session_id,
                payload.get('flight'),
                payload.get('new_std') or payload.get('etd'),
                reconstructed_flights,
                reconstructed_teams,
            )
            if analysis.get('success'):
                result = apply_delay(
                    session_id,
                    analysis,
                    reconstructed_teams,
                    apply_auto_fixes=payload.get('apply_auto_fixes', True),
                )
            else:
                result = analysis
            summary = f"Delay: AA{payload.get('flight')} → {payload.get('new_std') or payload.get('etd')}"
        elif op_type == 'gate_change':
            result = handle_gate_change(
                session_id,
                payload.get('flight'),
                payload.get('new_gate') or payload.get('gate'),
                reconstructed_flights,
                reconstructed_teams,
            )
            summary = f"Gate change: AA{payload.get('flight')} → {payload.get('new_gate') or payload.get('gate')}"
        elif op_type == 'reassign':
            result = handle_reassign(session_id, payload.get('flight'), payload.get('new_team') or payload.get('team'),
                                     reconstructed_flights, reconstructed_teams,
                                     force=payload.get('force', False))
            summary = f"Reassign: AA{payload.get('flight')} → {payload.get('new_team') or payload.get('team')}"

        if 'error' in result:
            return jsonify(result), 400

        # Get updated result from session
        updated_session = load_session(session_id)
        updated_result  = _persistable_result(
            (updated_session or {}).get('result', current_result),
            updated_session or {},
        )

        # Persist to Supabase — triggers Realtime for all connected clients
        update_schedule_result(schedule_date, shift, updated_result)
        log_modification(schedule_date, shift, user['user_id'],
                         user['display_name'], op_type, summary, payload)

        return jsonify({**result, 'schedule_date': schedule_date, 'shift': shift})
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500

@app.route('/api/teams/<schedule_date>/<day_of_week>', methods=['GET'])
def get_teams(schedule_date, day_of_week):
    """
    Return team roster: each TRK team with its agents for the given day.
    Merges bid schedule data with any day-level modifications from Supabase.
    """
    try:
        token = request.headers.get('X-Auth-Token','')
        from auth import verify_token as _vt
        user = _vt(token) if token else None
        if not user: return jsonify({'error': 'Not authenticated'}), 401

        # Resolve CSV paths: session_id → session store → _session_data files
        session_id = request.args.get('session_id')
        ft_path = pt_path = None

        if session_id:
            from live_ops import load_session as _ls
            sess = _ls(session_id)
            if sess:
                ft_path = sess.get('ft_path')
                pt_path = sess.get('pt_path')

        # Also scan _session_data for the most recently modified ft/pt pair
        # (covers the case where the session expired from memory but files remain)
        if not ft_path or not os.path.exists(ft_path):
            session_data_dir = os.path.join(os.path.dirname(__file__), '_session_data')
            if os.path.isdir(session_data_dir):
                ft_candidates = sorted(
                    [f for f in os.listdir(session_data_dir) if f.endswith('_ft.csv')],
                    key=lambda f: os.path.getmtime(os.path.join(session_data_dir, f)),
                    reverse=True)
                pt_candidates = sorted(
                    [f for f in os.listdir(session_data_dir) if f.endswith('_pt.csv')],
                    key=lambda f: os.path.getmtime(os.path.join(session_data_dir, f)),
                    reverse=True)
                if ft_candidates:
                    ft_path = os.path.join(session_data_dir, ft_candidates[0])
                if pt_candidates:
                    pt_path = os.path.join(session_data_dir, pt_candidates[0])

        # Build agent → team mapping from bid CSVs
        import csv as _csv
        day_map = {'Monday':'Mon','Tuesday':'Tues','Wednesday':'Wed',
                   'Thursday':'Thurs','Friday':'Fri','Saturday':'Sat','Sunday':'Sun'}
        dk = day_map.get(day_of_week, 'Sat')

        teams_dict = {}   # team_id → {agents: [], shift, type}

        def parse_csv(path, is_pt):
            if not path or not os.path.exists(path): return
            with open(path, newline='', encoding='utf-8-sig') as f:
                for row in _csv.DictReader(f):
                    shift = (row.get(day_of_week,'') or row.get(dk,'')).strip()
                    dept  = (row.get(f'{day_of_week} Department','') or row.get(f'{dk} Department','')).strip()
                    tid   = (row.get(f'{day_of_week} Team','') or row.get(f'{dk} Team','')).strip()
                    if not tid or shift in ('','Off'): continue
                    agent = {
                        'aa_id':   row.get('AA ID','').strip(),
                        'last':    row.get('Last Name','').strip(),
                        'first':   row.get('First Name','').strip(),
                        'ln':      row.get('LN#','') or row.get('LN #',''),
                        'shift':   shift,
                        'dept':    dept,
                        'is_pt':   is_pt,
                        'status':  'active',
                    }
                    if tid not in teams_dict:
                        teams_dict[tid] = {'team_id': tid, 'shift': shift, 'dept': dept,
                                           'is_pt': is_pt, 'agents': [], 'modifications': []}
                    existing_ids = {a['aa_id'] for a in teams_dict[tid]['agents']}
                    if agent['aa_id'] not in existing_ids:
                        teams_dict[tid]['agents'].append(agent)

        ft_to_use = pt_to_use = None
        session_data_dir = os.path.join(os.path.dirname(__file__), '_session_data')

        # 1. Session CSV (most specific — from the exact generate run)
        if ft_path and os.path.exists(ft_path): ft_to_use = ft_path
        if pt_path and os.path.exists(pt_path): pt_to_use = pt_path

        # 2. Latest stable copy (written every time any schedule is generated)
        if not ft_to_use:
            p = os.path.join(session_data_dir, 'latest_ft.csv')
            if os.path.exists(p): ft_to_use = p
        if not pt_to_use:
            p = os.path.join(session_data_dir, 'latest_pt.csv')
            if os.path.exists(p): pt_to_use = p

        # 3. Most-recently-modified session file (covers old sessions before latest_ existed)
        if not ft_to_use and os.path.isdir(session_data_dir):
            cands = sorted([f for f in os.listdir(session_data_dir) if f.endswith('_ft.csv')],
                           key=lambda f: os.path.getmtime(os.path.join(session_data_dir, f)), reverse=True)
            if cands: ft_to_use = os.path.join(session_data_dir, cands[0])
        if not pt_to_use and os.path.isdir(session_data_dir):
            cands = sorted([f for f in os.listdir(session_data_dir) if f.endswith('_pt.csv')],
                           key=lambda f: os.path.getmtime(os.path.join(session_data_dir, f)), reverse=True)
            if cands: pt_to_use = os.path.join(session_data_dir, cands[0])

        # 4. Bundled bid schedule CSVs — check all realistic deployment locations
        _app_dir = os.path.dirname(os.path.abspath(__file__))
        _ft_names = ['Agents_BID_Schedule_CSV.csv', 'agents_bid_schedule_csv.csv']
        _pt_names = ['Agents_BID_Schedule_CSV_PT.csv', 'agents_bid_schedule_csv_pt.csv']
        _search_dirs = [
            _app_dir,                                    # alongside app.py
            os.path.join(_app_dir, '..'),                # one level up
            os.path.join(_app_dir, '..', '..'),          # two levels up
            os.path.join(_app_dir, 'data'),              # data/ subfolder
            '/mnt/project',                              # Claude project files
            os.path.expanduser('~'),                     # home dir
        ]
        if not ft_to_use:
            for d in _search_dirs:
                for name in _ft_names:
                    p = os.path.normpath(os.path.join(d, name))
                    if os.path.exists(p):
                        ft_to_use = p
                        break
                if ft_to_use: break
        if not pt_to_use:
            for d in _search_dirs:
                for name in _pt_names:
                    p = os.path.normpath(os.path.join(d, name))
                    if os.path.exists(p):
                        pt_to_use = p
                        break
                if pt_to_use: break

        parse_csv(ft_to_use, False)
        parse_csv(pt_to_use, True)

        # Load day-level team modifications from Supabase
        try:
            from supabase_client import get_team_modifications, get_off_duty_agents
            mods = get_team_modifications(schedule_date)
            off_duty = get_off_duty_agents(schedule_date)
        except Exception:
            mods, off_duty = [], []

        # Apply modifications (sick calls / additions from previous saves)
        for mod in mods:
            tid = mod.get('team_id')
            if tid in teams_dict:
                teams_dict[tid]['modifications'].append(mod)
                if mod.get('type') == 'sick_call':
                    for ag in teams_dict[tid]['agents']:
                        if ag['aa_id'] == mod.get('aa_id'):
                            ag['status'] = 'sick'
                elif mod.get('type') == 'add_coverage':
                    existing_ids = {a['aa_id'] for a in teams_dict[tid]['agents']}
                    if mod.get('aa_id') not in existing_ids:
                        teams_dict[tid]['agents'].append({
                            'aa_id': mod.get('aa_id',''), 'last': mod.get('last',''),
                            'first': mod.get('first',''), 'ln': mod.get('ln',''),
                            'shift': mod.get('shift',''), 'dept': mod.get('dept',''),
                            'is_pt': mod.get('is_pt', False), 'status': 'coverage',
                        })

        # Filter to only TRK teams, sort by shift start
        from scheduler_engine import t2m as _t2m
        trk_teams = [v for v in teams_dict.values() if 'TRK' in v.get('dept','')]
        trk_teams.sort(key=lambda x: _t2m(x['shift'].replace('\u2013','-').replace('\u2014','-').split('-')[0]) if '-' in x['shift'].replace('\u2013','-') else 0)

        return jsonify({'teams': trk_teams, 'off_duty': off_duty, 'date': schedule_date})
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500


@app.route('/api/teams/<schedule_date>/modify', methods=['POST'])
def modify_team(schedule_date):
    """
    Apply a team modification for the day:
      type = 'sick_call'     → mark agent as sick on their team
      type = 'add_coverage'  → add off-duty agent to a team for the day
      type = 'remove_agent'  → undo a previous addition
    """
    try:
        token = request.headers.get('X-Auth-Token','')
        from auth import verify_token as _vt
        user = _vt(token) if token else None
        if not user: return jsonify({'error': 'Not authenticated'}), 401
        if user['role'] not in ('manager','supervisor'):
            return jsonify({'error': 'Insufficient permissions'}), 403

        data = request.get_json() or {}
        mod_type = data.get('type')   # sick_call | add_coverage | remove_agent
        team_id  = data.get('team_id')
        aa_id    = data.get('aa_id')

        if not mod_type or not team_id or not aa_id:
            return jsonify({'error': 'type, team_id, aa_id required'}), 400

        try:
            from supabase_client import save_team_modification
            save_team_modification(schedule_date, {
                'team_id':   team_id,
                'type':      mod_type,
                'aa_id':     aa_id,
                'first':     data.get('first',''),
                'last':      data.get('last',''),
                'ln':        data.get('ln',''),
                'shift':     data.get('shift',''),
                'dept':      data.get('dept',''),
                'is_pt':     data.get('is_pt', False),
                'note':      data.get('note',''),
                'created_by': user['user_id'],
                'created_name': user['display_name'],
            })
        except Exception as e:
            return jsonify({'error': f'Supabase save failed: {e}'}), 500

        return jsonify({'ok': True, 'type': mod_type, 'team_id': team_id, 'aa_id': aa_id})
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500


@app.route('/api/agents/off-duty/<schedule_date>/<day_of_week>', methods=['GET'])
def get_off_duty_agents_endpoint(schedule_date, day_of_week):
    """Return agents who are off on the given day — available for coverage call-in."""
    try:
        token = request.headers.get('X-Auth-Token','')
        from auth import verify_token as _vt
        user = _vt(token) if token else None
        if not user: return jsonify({'error': 'Not authenticated'}), 401
        if user['role'] not in ('manager','supervisor'):
            return jsonify({'error': 'Insufficient permissions'}), 403

        import csv as _csv
        day_map = {'Monday':'Mon','Tuesday':'Tues','Wednesday':'Wed',
                   'Thursday':'Thurs','Friday':'Fri','Saturday':'Sat','Sunday':'Sun'}
        dk = day_map.get(day_of_week,'Sat')
        off_duty = []

        # Resolve CSV paths using same multi-location search as get_teams
        _app_dir2 = os.path.dirname(os.path.abspath(__file__))
        _search_dirs2 = [_app_dir2, os.path.join(_app_dir2,'..'), os.path.join(_app_dir2,'..','..'),
                         os.path.join(_app_dir2,'data'), '/mnt/project', os.path.expanduser('~')]
        def _find_csv(names):
            for d in _search_dirs2:
                for n in names:
                    p = os.path.normpath(os.path.join(d, n))
                    if os.path.exists(p): return p
            return None

        ft_path = _find_csv(['Agents_BID_Schedule_CSV.csv','agents_bid_schedule_csv.csv'])
        pt_path = _find_csv(['Agents_BID_Schedule_CSV_PT.csv','agents_bid_schedule_csv_pt.csv'])

        for path in [ft_path, pt_path]:
            if not os.path.exists(path): continue
            with open(path, newline='', encoding='utf-8-sig') as f:
                for row in _csv.DictReader(f):
                    shift = (row.get(day_of_week,'') or row.get(dk,'')).strip()
                    if shift.lower() in ('off', ''):
                        off_duty.append({
                            'aa_id': row.get('AA ID','').strip(),
                            'last':  row.get('Last Name','').strip(),
                            'first': row.get('First Name','').strip(),
                            'ln':    row.get('LN#','') or row.get('LN #',''),
                        })

        off_duty.sort(key=lambda x: x['last'])
        return jsonify({'agents': off_duty})
    except Exception as e:
        return jsonify({'error': str(e), 'agents': []}), 500


@app.route('/api/run-edit', methods=['POST'])
def run_edit():
    """
    Edit a truck run: merge, split, or reorder stops.
    Body: {
      session_id,
      team_id,
      action: 'reorder' | 'split' | 'merge',
      flights: [flight_num, ...]   -- ordered list for this run
      merge_dispatch_min: int      -- (merge only) dispatch minute for combined run
    }
    Returns the full updated result on success.
    """
    try:
        token = request.headers.get('X-Auth-Token','')
        from auth import verify_token as _vt
        user = _vt(token) if token else None
        if not user: return jsonify({'error': 'Not authenticated'}), 401
        if user['role'] not in ('manager','supervisor'):
            return jsonify({'error': 'Insufficient permissions'}), 403

        data       = request.get_json() or {}
        session_id = data.get('session_id')
        team_id    = data.get('team_id')
        action     = data.get('action')   # reorder | split | merge
        flight_ids = data.get('flights', [])

        if not session_id or not team_id or not action or not flight_ids:
            return jsonify({'error': 'session_id, team_id, action, flights required'}), 400

        from live_ops import handle_run_edit, load_session
        session = load_session(session_id)
        if not session:
            return jsonify({'error': 'Session not found — regenerate schedule'}), 404

        result = handle_run_edit(
            session_id   = session_id,
            team_id      = team_id,
            action       = action,
            flight_ids   = flight_ids,
            flights      = session.get('flights', []),
            teams        = session.get('teams', []),
            merge_dispatch = data.get('merge_dispatch_min'),
        )
        return jsonify(result)
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500


@app.route('/api/health')
def health():
    try:
        from supabase_client import is_connected
        sb_status = 'connected' if is_connected() else 'unavailable'
    except Exception:
        sb_status = 'not installed'
    return jsonify({'status': 'ok', 'engine': 'OR-Tools CP-SAT v2.5 + Agent', 'supabase': sb_status})


if __name__ == '__main__':
    os.makedirs('static', exist_ok=True)
    PORT = 5050
    _check_supabase()
    print(f"\n  PHL Catering Scheduler + Agent at http://localhost:{PORT}\n")
    app.run(debug=False, port=PORT, threaded=True)
