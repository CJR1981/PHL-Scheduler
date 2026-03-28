"""
PHL Catering Scheduler — Supabase Client
Handles all database operations: sessions, equity tracking, bid periods.
"""
import os
from supabase import create_client, Client
from typing import Optional, List, Dict
import datetime

SUPABASE_URL = os.environ.get('SUPABASE_URL', '')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY', '')

_client: Optional[Client] = None
_supabase_ok: Optional[bool] = None  # None = untested, True = ok, False = failed

def check_connection() -> dict:
    """Test Supabase connectivity. Called on app startup."""
    global _supabase_ok
    try:
        sb = get_client()
        r = sb.table('bid_periods').select('id,name').limit(1).execute()
        _supabase_ok = True
        return {'ok': True, 'bid_periods': r.data}
    except Exception as e:
        _supabase_ok = False
        return {'ok': False, 'error': str(e)}

def is_connected() -> bool:
    return _supabase_ok is True

def get_client() -> Client:
    global _client
    if _client is None:
        _client = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _client

# ── Sessions ──────────────────────────────────────────────────────────────

def save_session_db(session_id: str, data: dict, day_of_week: str = None,
                    schedule_date: str = None) -> bool:
    """Persist a session to Supabase. Returns True on success."""
    try:
        sb = get_client()
        # Strip non-serialisable keys
        payload = {
            'id':            session_id,
            'day_of_week':   day_of_week,
            'schedule_date': schedule_date,
            'result':        data.get('result'),
            'agent_history': data.get('agent_history'),
            'stats':         data.get('result', {}).get('stats'),
        }
        sb.table('sessions').upsert(payload).execute()
        return True
    except Exception as e:
        print(f"[Supabase] save_session failed: {e}")
        return False

def load_session_db(session_id: str) -> Optional[dict]:
    """Load a session from Supabase."""
    try:
        sb = get_client()
        r = sb.table('sessions').select('*').eq('id', session_id).single().execute()
        if r.data:
            return {
                'result':        r.data.get('result'),
                'agent_history': r.data.get('agent_history') or [],
                'day_of_week':   r.data.get('day_of_week'),
                'schedule_date': r.data.get('schedule_date'),
            }
        return None
    except Exception as e:
        print(f"[Supabase] load_session failed: {e}")
        return None

def list_sessions_db(limit: int = 20) -> List[dict]:
    """List recent sessions for the session picker UI."""
    try:
        sb = get_client()
        r = (sb.table('sessions')
               .select('id, created_at, day_of_week, schedule_date, stats')
               .order('created_at', desc=True)
               .limit(limit)
               .execute())
        return r.data or []
    except Exception as e:
        print(f"[Supabase] list_sessions failed: {e}")
        return []

# ── Bid Periods ───────────────────────────────────────────────────────────

def get_active_bid_period() -> Optional[dict]:
    """Get the currently active bid period."""
    try:
        sb = get_client()
        r = sb.table('bid_periods').select('*').eq('is_active', True).limit(1).execute()
        return r.data[0] if r.data else None
    except Exception as e:
        print(f"[Supabase] get_active_bid_period failed: {e}")
        return None

def create_bid_period(name: str, start_date: str, end_date: str) -> Optional[dict]:
    """Create a new bid period (and deactivate the current one)."""
    try:
        sb = get_client()
        sb.table('bid_periods').update({'is_active': False}).eq('is_active', True).execute()
        r = sb.table('bid_periods').insert({
            'name': name, 'start_date': start_date,
            'end_date': end_date, 'is_active': True
        }).execute()
        return r.data[0] if r.data else None
    except Exception as e:
        print(f"[Supabase] create_bid_period failed: {e}")
        return None

# ── Agent Equity ──────────────────────────────────────────────────────────

def get_agent_equity_deltas(bid_period_id: int) -> Dict[str, float]:
    """
    Returns {aa_id: equity_delta} for all agents in the bid period.
    equity_delta > 0 = overloaded, < 0 = underloaded.
    Uses the LATEST equity record per agent (most recent running total).
    """
    try:
        sb = get_client()
        r = (sb.table('agent_equity')
               .select('aa_id, equity_delta, running_total, period_target')
               .eq('bid_period_id', bid_period_id)
               .order('schedule_date', desc=True)
               .execute())
        # Latest record per agent
        seen = {}
        for row in (r.data or []):
            aa_id = row['aa_id']
            if aa_id not in seen:
                seen[aa_id] = row['equity_delta'] or 0.0
        return seen
    except Exception as e:
        print(f"[Supabase] get_agent_equity_deltas failed: {e}")
        return {}

def get_equity_leaderboard(bid_period_id: int) -> List[dict]:
    """Full leaderboard for the equity dashboard tab."""
    try:
        sb = get_client()
        r = (sb.table('agent_equity')
               .select('aa_id, last_name, first_name, running_total, period_target, equity_delta, schedule_date, points_earned, flights_assigned, team_id, points_breakdown')
               .eq('bid_period_id', bid_period_id)
               .order('schedule_date', desc=True)
               .execute())
        # Aggregate per agent: sum points, count flights, latest delta
        agents: Dict[str, dict] = {}
        for row in (r.data or []):
            aa = row['aa_id']
            if aa not in agents:
                agents[aa] = {
                    'aa_id':          aa,
                    'last_name':      row.get('last_name', ''),
                    'first_name':     row.get('first_name', ''),
                    'total_points':   0.0,
                    'total_flights':  0,
                    'equity_delta':   row.get('equity_delta', 0.0),
                    'period_target':  row.get('period_target', 0.0),
                    'days_worked':    0,
                    'daily_history':  [],
                }
            agents[aa]['total_points']  += row.get('points_earned', 0) or 0
            agents[aa]['total_flights'] += row.get('flights_assigned', 0) or 0
            agents[aa]['days_worked']   += 1
            agents[aa]['daily_history'].append({
                'date':     row.get('schedule_date'),
                'points':   row.get('points_earned', 0),
                'flights':  row.get('flights_assigned', 0),
                'team':     row.get('team_id'),
                'breakdown': row.get('points_breakdown', {}),
            })
        # Sort by equity_delta descending (most overloaded first)
        return sorted(agents.values(), key=lambda x: x['equity_delta'], reverse=True)
    except Exception as e:
        print(f"[Supabase] get_equity_leaderboard failed: {e}")
        return []

def commit_day_equity(session_id: str, schedule_date: str, day_of_week: str,
                      bid_period_id: int, agent_rows: List[dict]) -> bool:
    """
    Write one equity row per agent for this day.
    Called when a schedule is finalised.
    agent_rows: list of dicts with keys:
      aa_id, last_name, first_name, team_id, flights_assigned,
      points_earned, points_breakdown
    """
    try:
        sb = get_client()
        # Get existing running totals for all agents in this period
        existing = get_agent_equity_deltas(bid_period_id)
        # Calculate what the period target should be at this date
        bp = (sb.table('bid_periods').select('*')
                .eq('id', bid_period_id).single().execute())
        if not bp.data:
            return False
        start = datetime.date.fromisoformat(bp.data['start_date'])
        end   = datetime.date.fromisoformat(bp.data['end_date'])
        today = datetime.date.fromisoformat(schedule_date)
        period_days  = (end - start).days + 1
        elapsed_days = (today - start).days + 1
        # Get total working agents (those in agent_rows are the active ones today)
        # Period target = cumulative equal share up to today
        total_pts_today = sum(r['points_earned'] for r in agent_rows)
        per_agent_target_today = total_pts_today / max(len(agent_rows), 1)

        rows_to_upsert = []
        for agent in agent_rows:
            aa_id       = agent['aa_id']
            prev_total  = existing.get(aa_id, 0.0)
            # Fetch actual previous running_total from DB
            prev_r = (sb.table('agent_equity')
                       .select('running_total, period_target')
                       .eq('aa_id', aa_id)
                       .eq('bid_period_id', bid_period_id)
                       .order('schedule_date', desc=True)
                       .limit(1)
                       .execute())
            prev_running = prev_r.data[0]['running_total'] if prev_r.data else 0.0
            prev_target  = prev_r.data[0]['period_target']  if prev_r.data else 0.0

            new_running = prev_running + agent['points_earned']
            new_target  = prev_target  + per_agent_target_today
            new_delta   = new_running  - new_target

            rows_to_upsert.append({
                'aa_id':            aa_id,
                'last_name':        agent.get('last_name', ''),
                'first_name':       agent.get('first_name', ''),
                'session_id':       session_id,
                'schedule_date':    schedule_date,
                'day_of_week':      day_of_week,
                'bid_period_id':    bid_period_id,
                'team_id':          agent.get('team_id', ''),
                'flights_assigned': agent['flights_assigned'],
                'points_earned':    agent['points_earned'],
                'points_breakdown': agent['points_breakdown'],
                'running_total':    new_running,
                'period_target':    new_target,
                'equity_delta':     new_delta,
            })

        sb.table('agent_equity').upsert(rows_to_upsert,
                                         on_conflict='aa_id,schedule_date').execute()
        return True
    except Exception as e:
        print(f"[Supabase] commit_day_equity failed: {e}")
        return False

# ── Modifications Log ─────────────────────────────────────────────────────

def log_modification(session_id: str, mod_type: str, summary: str, payload: dict) -> bool:
    try:
        sb = get_client()
        sb.table('schedule_modifications').insert({
            'session_id': session_id,
            'type':       mod_type,
            'summary':    summary,
            'payload':    payload,
        }).execute()
        return True
    except Exception as e:
        print(f"[Supabase] log_modification failed: {e}")
        return False

# ── Team Roster Modifications ─────────────────────────────────────────────

def save_team_modification(schedule_date: str, mod: dict) -> bool:
    """Save a team roster change (sick call / add coverage) for a date."""
    try:
        sb = get_client()
        sb.table('team_modifications').insert({
            'schedule_date': schedule_date,
            'team_id':       mod.get('team_id'),
            'type':          mod.get('type'),
            'aa_id':         mod.get('aa_id'),
            'first':         mod.get('first'),
            'last':          mod.get('last'),
            'ln':            mod.get('ln'),
            'shift':         mod.get('shift'),
            'dept':          mod.get('dept'),
            'is_pt':         mod.get('is_pt', False),
            'note':          mod.get('note',''),
            'created_by':    mod.get('created_by'),
            'created_name':  mod.get('created_name'),
        }).execute()
        return True
    except Exception as e:
        print(f"[Supabase] save_team_modification failed: {e}")
        return False

def get_team_modifications(schedule_date: str) -> list:
    """Load all team roster modifications for a date."""
    try:
        sb = get_client()
        r = sb.table('team_modifications').select('*')\
               .eq('schedule_date', schedule_date)\
               .order('created_at').execute()
        return r.data or []
    except Exception as e:
        print(f"[Supabase] get_team_modifications failed: {e}")
        return []

def get_off_duty_agents(schedule_date: str) -> list:
    """Return agents added as coverage on this date (for display)."""
    try:
        sb = get_client()
        r = sb.table('team_modifications').select('*')\
               .eq('schedule_date', schedule_date)\
               .eq('type', 'add_coverage').execute()
        return r.data or []
    except Exception as e:
        return []
