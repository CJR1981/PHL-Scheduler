"""
PHL Catering Scheduler — Conversational Agent
Uses the Anthropic API with tool use to give coordinators a natural language
interface to the live schedule. The agent can read schedule state, explain
decisions, and take actions (reassign, delay, sick call, gate change).
"""
import json, requests as _req
from typing import Optional

ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
MODEL         = "claude-sonnet-4-20250514"

SYSTEM_PROMPT = """You are the PHL Catering Operations Assistant for American Airlines at Philadelphia International Airport.

You help shift coordinators manage the daily catering truck schedule. You have deep knowledge of how the operation works:

OPERATIONS KNOWLEDGE:
- Catering trucks service aircraft between arrival and departure. Each run takes 35-55 min for domestic.
- International flights need dedicated truck loads — 60 min buffer before departure (precleared = same as intl).
- Short turns: aircraft with tight ground time. These still get serviced regardless — it is urgent.
- Teams are pairs of agents driving a catering truck. FT teams: 5 flight cap. PT teams: 4 flight cap.
- The morning bank (03:00-09:00) is dense with RON and early departures. Evening bank (18:40-21:25) is heavy widebody internationals.
- Concourses: A (international, 17 min drive), B/C (13 min), D/E/F (15-18 min).
- Truck pool: 25 trucks. NO_TRUCK_AVAILABLE is the most common unassigned reason during peak windows.

YOUR BEHAVIOUR:
- Be direct and operational. Coordinators are busy — no fluff.
- When asked about a specific flight or team, call the appropriate tool FIRST, then answer.
- Before taking any action (reassign, delay, sick call), briefly confirm what you're about to do.
- After taking an action, summarise what changed and flag any downstream impacts.
- If something cannot be done (timing conflict, no available teams), explain why clearly.
- Use flight numbers as AA{number}, team IDs as-is (e.g. TM211).
- Times are always in 24h format (e.g. 14:35).

IMPORTANT: You are operating on a LIVE schedule. Actions you take are real and update the database immediately. Always be precise about flight numbers and team IDs."""

TOOLS = [
    {
        "name": "get_schedule_summary",
        "description": "Get a high-level summary of the current schedule: coverage %, quality score, unassigned count, team utilisation by shift window, and recent changes.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "get_flight",
        "description": "Get full details for a specific flight: assignment status, team, timing, gate, equipment, inbound details, and ground time.",
        "input_schema": {
            "type": "object",
            "properties": {
                "flight_num": {"type": "string", "description": "Flight number without AA prefix e.g. '1452'"}
            },
            "required": ["flight_num"]
        }
    },
    {
        "name": "get_team",
        "description": "Get a team's full schedule: all assigned flights in order, dispatch times, service times, workload count vs cap, shift start/end.",
        "input_schema": {
            "type": "object",
            "properties": {
                "team_id": {"type": "string", "description": "Team identifier e.g. 'TM211'"}
            },
            "required": ["team_id"]
        }
    },
    {
        "name": "get_unassigned",
        "description": "List all flights that could not be assigned, with reason (NO_TRUCK_AVAILABLE, OUTSIDE_SHIFT_WINDOW, INSUFFICIENT_GROUND_TIME) and dispatch window needed.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "find_available_teams",
        "description": "Find which teams are available and capable of serving a specific flight. Returns teams sorted by availability showing current load, shift, and whether they can make the dispatch deadline.",
        "input_schema": {
            "type": "object",
            "properties": {
                "flight_num": {"type": "string", "description": "Flight number to find available teams for"}
            },
            "required": ["flight_num"]
        }
    },
    {
        "name": "reassign_flight",
        "description": "Move a flight from its current team to a different team. Validates timing feasibility. Use when coordinator wants to redistribute work.",
        "input_schema": {
            "type": "object",
            "properties": {
                "flight_num": {"type": "string", "description": "Flight number to reassign"},
                "team_id":    {"type": "string", "description": "Team to assign the flight to"}
            },
            "required": ["flight_num", "team_id"]
        }
    },
    {
        "name": "apply_delay",
        "description": "Record a flight delay with a new departure time. Detects cascade conflicts and auto-reassigns where possible.",
        "input_schema": {
            "type": "object",
            "properties": {
                "flight_num": {"type": "string", "description": "Flight number being delayed"},
                "new_std":    {"type": "string", "description": "New scheduled departure time HH:MM e.g. '14:35'"}
            },
            "required": ["flight_num", "new_std"]
        }
    },
    {
        "name": "apply_gate_change",
        "description": "Update a flight gate. Recalculates dispatch timing for the new drive time and detects cascade conflicts.",
        "input_schema": {
            "type": "object",
            "properties": {
                "flight_num": {"type": "string", "description": "Flight number with gate change"},
                "new_gate":   {"type": "string", "description": "New gate e.g. 'B14', 'A8'"}
            },
            "required": ["flight_num", "new_gate"]
        }
    },
    {
        "name": "apply_sick_call",
        "description": "Remove a team from the schedule due to sick call. Their flights are automatically redistributed to available teams.",
        "input_schema": {
            "type": "object",
            "properties": {
                "team_id": {"type": "string", "description": "Team ID of sick team e.g. 'TM211'"}
            },
            "required": ["team_id"]
        }
    }
]


def _call_api(method, url, **kwargs):
    try:
        if method == 'GET':
            r = _req.get(url, timeout=90, **kwargs)
        else:
            r = _req.post(url, timeout=90, **kwargs)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {'error': str(e)}


def execute_tool(tool_name, tool_input, schedule_data, session_id, api_base):
    assignments  = schedule_data.get('assignments', [])
    unassigned   = schedule_data.get('unassigned', [])
    team_ops     = schedule_data.get('team_ops', {})
    team_summary = schedule_data.get('team_summary', [])
    stats        = schedule_data.get('stats', {})

    def norm_fn(raw):
        s = str(raw).lstrip('0') or '0'
        return s.zfill(4)

    if tool_name == 'get_schedule_summary':
        from collections import defaultdict
        windows = defaultdict(list)
        for t in team_summary:
            h = t['shift_start'] // 60
            windows[h].append({'team': t['team_id'], 'load': t['flight_count'], 'cap': t['cap']})
        return {
            'coverage_pct':    stats.get('coverage_pct'),
            'quality_score':   stats.get('quality_score'),
            'quality_grade':   stats.get('quality_grade'),
            'assigned':        stats.get('assigned'),
            'total':           stats.get('total'),
            'unassigned_count': stats.get('unassigned', 0),
            'unassigned_reasons': stats.get('unassigned_reasons', {}),
            'intl_assigned':   stats.get('intl_assigned'),
            'intl_total':      stats.get('intl_total'),
            'teams_deployed':  stats.get('teams_deployed'),
            'util_stddev':     stats.get('util_stddev'),
            'balance_passes':  stats.get('balance_iterations', 0),
            'shift_utilisation': [
                {'hour': f"{h:02d}:00", 'teams': len(v),
                 'avg_util': round(sum(x['load']/x['cap'] for x in v)/len(v)*100),
                 'detail': ', '.join(f"{x['team']} {x['load']}/{x['cap']}" for x in v[:3])}
                for h, v in sorted(windows.items())
            ],
        }

    if tool_name == 'get_flight':
        fn  = norm_fn(tool_input.get('flight_num',''))
        raw = tool_input.get('flight_num','').lstrip('0') or '0'
        a   = next((x for x in assignments if norm_fn(x['flight']) == fn), None)
        u   = next((x for x in unassigned  if norm_fn(x['flight']) == fn), None)
        if a: return {'status': 'assigned', **a}
        if u: return {'status': 'unassigned', **u}
        return {'error': f"Flight {tool_input['flight_num']} not found"}

    if tool_name == 'get_team':
        tid  = tool_input.get('team_id','').upper()
        ops  = team_ops.get(tid, [])
        meta = next((t for t in team_summary if t['team_id'] == tid), None)
        if not ops and not meta:
            return {'error': f"Team {tid} not found or has no flights"}
        return {
            'team_id': tid,
            'shift':        meta['shift'] if meta else '?',
            'team_type':    meta['team_type'] if meta else '?',
            'flight_count': len(ops),
            'cap':          meta['cap'] if meta else 5,
            'operations':   sorted(ops, key=lambda x: x.get('dispatch_min', 0)),
        }

    if tool_name == 'get_unassigned':
        return {
            'count': len(unassigned),
            'flights': [
                {'flight': u['flight'], 'dest': u['dest'], 'std': u['std'],
                 'equip': u.get('equip',''), 'gate': u.get('gate','?'),
                 'reason': u.get('reason',''), 'is_intl': u.get('is_intl', False),
                 'ground_mins': u.get('ground_mins', -1)}
                for u in sorted(unassigned, key=lambda x: x.get('std_min', 0))
            ]
        }

    if tool_name == 'find_available_teams':
        fn     = norm_fn(tool_input.get('flight_num',''))
        target = next((x for x in assignments + unassigned
                       if norm_fn(x['flight']) == fn), None)
        if not target:
            return {'error': f"Flight {fn} not found"}
        from scheduler_engine import FINISH_BUF, SVC_TIMES, DEFAULT_SVC, drv, m2t
        std_min = target.get('std_min', 0)
        is_intl = target.get('is_intl', False)
        svc     = SVC_TIMES.get(target.get('equip',''), DEFAULT_SVC)
        drv_t   = drv(target.get('gate','B'))
        fb      = FINISH_BUF['intl'] if is_intl else FINISH_BUF['dom']
        latest_d = std_min - fb - svc - drv_t
        available = []
        for t in team_summary:
            if t['shift_start'] > latest_d: continue
            team_free = max(
                (op.get('team_free_min', 0) for op in team_ops.get(t['team_id'], [])),
                default=t['shift_start']
            )
            available.append({
                'team_id':    t['team_id'],
                'team_type':  t['team_type'],
                'shift':      t['shift'],
                'load':       t['flight_count'],
                'cap':        t['cap'],
                'at_cap':     t['flight_count'] >= t['cap'],
                'free_at':    m2t(team_free),
                'can_reach':  team_free <= latest_d,
            })
        available.sort(key=lambda x: (x['at_cap'], not x['can_reach'], x['load']))
        return {'flight': fn, 'dispatch_deadline': m2t(latest_d), 'teams': available[:10]}

    # Action tools
    if tool_name == 'reassign_flight':
        fn  = norm_fn(tool_input['flight_num'])
        tid = tool_input['team_id'].upper()
        return _call_api('POST', f"{api_base}/api/reassign",
                         json={'session_id': session_id, 'flight': fn, 'team': tid})

    if tool_name == 'apply_delay':
        fn  = norm_fn(tool_input['flight_num'])
        std = tool_input['new_std']
        ana = _call_api('POST', f"{api_base}/api/delay",
                        json={'session_id': session_id, 'flight': fn, 'new_std': std})
        if not ana.get('success'):
            return ana
        apl = _call_api('POST', f"{api_base}/api/delay/apply",
                        json={'session_id': session_id, 'delay_result': ana,
                              'apply_auto_fixes': True})
        if apl.get('success'):
            apl['cascade_count']   = ana.get('cascade_count', 0)
            apl['auto_fixable']    = ana.get('auto_fixable', 0)
            apl['manual_required'] = ana.get('manual_required', 0)
        return apl

    if tool_name == 'apply_gate_change':
        fn   = norm_fn(tool_input['flight_num'])
        gate = tool_input['new_gate'].upper()
        return _call_api('POST', f"{api_base}/api/gate-change",
                         json={'session_id': session_id, 'flight': fn, 'new_gate': gate})

    if tool_name == 'apply_sick_call':
        tid = tool_input['team_id'].upper()
        return _call_api('POST', f"{api_base}/api/sick-call",
                         json={'session_id': session_id, 'team_id': tid, 'time_limit': 60})

    return {'error': f"Unknown tool: {tool_name}"}


def run_agent_turn(user_message, conversation_history, schedule_data,
                   session_id, api_base_url):
    import os, requests
    api_key = os.environ.get('ANTHROPIC_API_KEY', '')

    stats = schedule_data.get('stats', {})
    context = (f"[Schedule: {stats.get('coverage_pct','?')}% coverage, "
               f"{stats.get('unassigned',0)} unassigned, "
               f"quality {stats.get('quality_score','?')}/105 [{stats.get('quality_grade','?')}]]")

    messages = list(conversation_history)
    messages.append({"role": "user", "content": f"{context}\n\n{user_message}"})

    actions_taken    = []
    schedule_updated = False
    final_response   = ""

    for _loop in range(10):
        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }
        payload = {
            "model":      MODEL,
            "max_tokens": 1024,
            "system":     SYSTEM_PROMPT,
            "tools":      TOOLS,
            "messages":   messages,
        }
        try:
            r = requests.post(ANTHROPIC_API, headers=headers, json=payload, timeout=60)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            final_response = f"AI service error: {e}"
            break

        stop_reason = data.get('stop_reason', '')
        content     = data.get('content', [])
        messages.append({"role": "assistant", "content": content})

        if stop_reason == 'end_turn':
            for block in content:
                if block.get('type') == 'text':
                    final_response = block['text']
            break

        if stop_reason == 'tool_use':
            tool_results = []
            action_tools = {'reassign_flight','apply_delay','apply_gate_change','apply_sick_call'}
            for block in content:
                if block.get('type') != 'tool_use': continue
                result = execute_tool(block['name'], block.get('input', {}),
                                      schedule_data, session_id, api_base_url)
                if block['name'] in action_tools and result.get('success'):
                    schedule_updated = True
                    actions_taken.append({'tool': block['name'], 'input': block['input']})
                    try:
                        upd = _call_api('GET', f"{api_base_url}/api/session/{session_id}")
                        if upd.get('result'):
                            schedule_data = upd['result']
                    except Exception:
                        pass
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block['id'],
                    "content": json.dumps(result),
                })
            messages.append({"role": "user", "content": tool_results})
            continue

        final_response = "Unexpected response. Please try again."
        break

    # Clean context prefix from stored history
    if messages and messages[0]['role'] == 'user':
        raw = messages[0]['content']
        if isinstance(raw, str) and raw.startswith('[Schedule:'):
            messages[0]['content'] = raw.split('\n\n', 1)[-1]

    return {
        'response':         final_response or "Done.",
        'actions_taken':    actions_taken,
        'schedule_updated': schedule_updated,
        'messages':         messages,
    }
