"""
PHL Catering — Daily Schedule Store
Handles all Supabase operations for the daily_schedules + daily_modifications tables.
Now includes view-filtering for Morning / Afternoon / WB International / Full Day.
"""
import datetime
from typing import Optional, List, Dict
from supabase_client import get_client

SHIFT_MORNING   = 'morning'
SHIFT_AFTERNOON = 'afternoon'
SHIFT_COMBINED  = 'combined'

# View keys used by the frontend tabs
VIEW_MORNING   = 'morning'
VIEW_AFTERNOON = 'afternoon'
VIEW_WB_INTL   = 'wb_intl'
VIEW_FULL      = 'full'

# ── Save / load schedule ───────────────────────────────────────────────────

def save_schedule(schedule_date: str, shift: str, result: dict,
                  day_of_week: str, created_by: int = None,
                  status: str = 'live') -> bool:
    """Upsert a schedule for a date+shift. Overwrites any existing record."""
    try:
        sb = get_client()
        sb.table('daily_schedules').upsert({
            'schedule_date': schedule_date,
            'shift':         shift,
            'day_of_week':   day_of_week,
            'result':        result,
            'status':        status,
            'created_by':    created_by,
            'updated_at':    datetime.datetime.utcnow().isoformat(),
        }, on_conflict='schedule_date,shift').execute()
        return True
    except Exception as e:
        print(f"[ScheduleStore] save_schedule error: {e}")
        return False

def load_schedule(schedule_date: str, shift: str) -> Optional[dict]:
    """Load a single shift schedule for a date."""
    try:
        sb = get_client()
        r = sb.table('daily_schedules').select('*') \
               .eq('schedule_date', schedule_date) \
               .eq('shift', shift).limit(1).execute()
        return r.data[0] if r.data else None
    except Exception as e:
        print(f"[ScheduleStore] load_schedule error: {e}")
        return None

def load_combined(schedule_date: str) -> Optional[dict]:
    """
    Load morning + afternoon and merge into a single result dict.
    Used for the manager Combined view.
    """
    try:
        am = load_schedule(schedule_date, SHIFT_MORNING)
        pm = load_schedule(schedule_date, SHIFT_AFTERNOON)
        if not am and not pm:
            return None

        am_result = (am or {}).get('result', {}) or {}
        pm_result = (pm or {}).get('result', {}) or {}

        combined_assignments = (am_result.get('assignments') or []) + \
                               (pm_result.get('assignments') or [])
        combined_unassigned  = (am_result.get('unassigned')  or []) + \
                               (pm_result.get('unassigned')  or [])

        am_stats = am_result.get('stats', {}) or {}
        pm_stats = pm_result.get('stats', {}) or {}
        total_a  = len(combined_assignments)
        total_f  = total_a + len(combined_unassigned)
        unassigned_count = len(combined_unassigned)

        # Merge team summaries — both are lists, not dicts
        am_summary = am_result.get('team_summary') or []
        pm_summary = pm_result.get('team_summary') or []
        combined_summary = am_summary + pm_summary

        # Merge team_ops dicts
        am_ops = am_result.get('team_ops') or {}
        pm_ops = pm_result.get('team_ops') or {}
        combined_ops = {**am_ops, **pm_ops}

        combined_stats = {
            # Core fields renderStats expects
            'total':              total_f,
            'assigned':           total_a,
            'unassigned':         unassigned_count,
            'coverage_pct':       round(total_a / total_f * 100, 1) if total_f else 0,
            'solve_time':         round((am_stats.get('solve_time', 0) or 0) +
                                        (pm_stats.get('solve_time', 0) or 0), 1),
            'status':             'OPTIMAL' if (am_stats.get('status') == 'OPTIMAL'
                                                and pm_stats.get('status') == 'OPTIMAL')
                                            else 'FEASIBLE',
            'balance_iterations': 0,
            # Quality — average of both halves
            'quality_score':      round(((am_stats.get('quality_score') or 0) +
                                          (pm_stats.get('quality_score') or 0)) / 2, 1),
            'quality_grade':      am_stats.get('quality_grade', '—'),
            'util_stddev':        round(max(am_stats.get('util_stddev', 0) or 0,
                                            pm_stats.get('util_stddev', 0) or 0), 3),
            # Passthrough extras
            'morning_coverage':   am_stats.get('coverage_pct', 0),
            'afternoon_coverage': pm_stats.get('coverage_pct', 0),
            'ft_min_ops':  min(am_stats.get('ft_min_ops', 0), pm_stats.get('ft_min_ops', 0)),
            'ft_max_ops':  max(am_stats.get('ft_max_ops', 0), pm_stats.get('ft_max_ops', 0)),
            'ft_avg_ops':  round(((am_stats.get('ft_avg_ops') or 0) +
                                   (pm_stats.get('ft_avg_ops') or 0)) / 2, 1),
            'intl_assigned': (am_stats.get('intl_assigned') or 0) + (pm_stats.get('intl_assigned') or 0),
            'intl_total':    (am_stats.get('intl_total') or 0) + (pm_stats.get('intl_total') or 0),
            'gap_analysis':  (am_stats.get('gap_analysis') or []) + (pm_stats.get('gap_analysis') or []),
            'shift_windows': (am_stats.get('shift_windows') or []) + (pm_stats.get('shift_windows') or []),
        }
        combined_stats['intl_pct'] = round(
            combined_stats['intl_assigned'] / combined_stats['intl_total'] * 100, 1
        ) if combined_stats['intl_total'] else 0

        return {
            'shift':        'combined',
            'schedule_date': schedule_date,
            'morning_status':   (am or {}).get('status'),
            'afternoon_status': (pm or {}).get('status'),
            'result': {
                'assignments':  combined_assignments,
                'unassigned':   combined_unassigned,
                'stats':        combined_stats,
                'team_summary': combined_summary,
                'team_ops':     combined_ops,
            }
        }
    except Exception as e:
        print(f"[ScheduleStore] load_combined error: {e}")
        return None

def list_schedule_dates(limit: int = 30) -> List[dict]:
    """
    Return list of dates that have schedules, with status per shift.
    Used to populate the manager date picker with green/grey dots.
    """
    try:
        sb = get_client()
        r = sb.table('daily_schedules') \
               .select('schedule_date,shift,status,day_of_week,updated_at') \
               .order('schedule_date', desc=True).limit(limit).execute()

        # Group by date
        dates: Dict[str, dict] = {}
        for row in (r.data or []):
            d = row['schedule_date']
            if d not in dates:
                dates[d] = {
                    'date':        d,
                    'day_of_week': row.get('day_of_week',''),
                    'morning':     None,
                    'afternoon':   None,
                    'updated_at':  row.get('updated_at'),
                }
            dates[d][row['shift']] = row['status']
            if row.get('updated_at') > dates[d]['updated_at']:
                dates[d]['updated_at'] = row['updated_at']

        return sorted(dates.values(), key=lambda x: x['date'], reverse=True)
    except Exception as e:
        print(f"[ScheduleStore] list_schedule_dates error: {e}")
        return []

# ── Update schedule result (live ops) ─────────────────────────────────────

def update_schedule_result(schedule_date: str, shift: str, result: dict) -> bool:
    """Update the result JSONB for a live schedule (sick call, delay, reassign)."""
    try:
        sb = get_client()
        sb.table('daily_schedules').update({
            'result':     result,
            'updated_at': datetime.datetime.utcnow().isoformat(),
        }).eq('schedule_date', schedule_date).eq('shift', shift).execute()
        return True
    except Exception as e:
        print(f"[ScheduleStore] update_schedule_result error: {e}")
        return False

# ── Modifications log ──────────────────────────────────────────────────────

def log_modification(schedule_date: str, shift: str, user_id: int,
                     display_name: str, mod_type: str,
                     summary: str, payload: dict) -> bool:
    try:
        sb = get_client()
        sb.table('daily_modifications').insert({
            'schedule_date': schedule_date,
            'shift':         shift,
            'user_id':       user_id,
            'display_name':  display_name,
            'type':          mod_type,
            'summary':       summary,
            'payload':       payload,
        }).execute()
        return True
    except Exception as e:
        print(f"[ScheduleStore] log_modification error: {e}")
        return False

def get_modifications(schedule_date: str, shift: str) -> List[dict]:
    try:
        sb = get_client()
        r = sb.table('daily_modifications').select('*') \
               .eq('schedule_date', schedule_date) \
               .eq('shift', shift) \
               .order('created_at', desc=True).limit(50).execute()
        return r.data or []
    except Exception as e:
        print(f"[ScheduleStore] get_modifications error: {e}")
        return []


# ── View filtering ────────────────────────────────────────────────────────

VIEW_FILTERS = {
    'morning':   lambda a: a.get('in_morning_view', False),
    'afternoon': lambda a: a.get('in_afternoon_view', False),
    'wb_intl':   lambda a: a.get('in_wb_intl_view', False),
    'full':      lambda a: True,
}

def filter_result_by_view(result: dict, view: str) -> dict:
    """
    Filter a combined schedule result to only include flights/teams for a view.
    Views: 'morning', 'afternoon', 'wb_intl', 'full'.
    Returns a new result dict with filtered assignments, unassigned, team_ops,
    team_summary, and per-view stats.
    """
    if not result or view == 'full':
        return result

    filt = VIEW_FILTERS.get(view)
    if not filt:
        return result

    assignments = [a for a in (result.get('assignments') or []) if filt(a)]
    unassigned  = [u for u in (result.get('unassigned')  or []) if filt(u)]

    # Rebuild team_ops for this view only
    team_ops = {}
    for a in assignments:
        tid = a.get('team')
        if tid:
            team_ops.setdefault(tid, []).append(a)
    for tid in team_ops:
        team_ops[tid].sort(key=lambda x: x.get('dispatch_min') or 0)

    # Rebuild team_summary for teams with operations in this view
    team_summary = []
    for ts in (result.get('team_summary') or []):
        tid = ts.get('team_id')
        ops = team_ops.get(tid, [])
        if not ops:
            continue
        team_summary.append({
            **ts,
            'flight_count': len(ops),
            'ops': len(set(o.get('dispatch_min') for o in ops)),
            'flights': [o['flight'] for o in ops],
            'operations': ops,
            'first_std': min(o['std'] for o in ops),
            'last_std':  max(o['std'] for o in ops),
        })

    # Pull per-view stats if available
    all_stats = result.get('stats') or {}
    view_stat = (all_stats.get('view_stats') or {}).get(view, {})

    total_f = len(assignments) + len(unassigned)
    filtered_stats = {
        **all_stats,
        'total':        total_f,
        'assigned':     len(assignments),
        'unassigned':   len(unassigned),
        'coverage_pct': round(100*len(assignments)/max(total_f,1), 1) if total_f else 0,
        'view':         view,
        'view_detail':  view_stat,
    }

    return {
        'assignments':  assignments,
        'unassigned':   unassigned,
        'team_ops':     team_ops,
        'team_summary': team_summary,
        'stats':        filtered_stats,
    }


def load_full_day(schedule_date: str) -> Optional[dict]:
    """
    Load the combined (full-day) schedule. Uses the 'combined' shift record
    if it exists, otherwise merges morning + afternoon.
    Returns result with view tags on every assignment for client-side filtering.
    """
    # Try combined record first
    combined = load_schedule(schedule_date, 'combined')
    if combined and combined.get('result'):
        return combined

    # Fall back to merging AM/PM
    merged = load_combined(schedule_date)
    if merged:
        return merged

    return None
