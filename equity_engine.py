"""
PHL Catering Scheduler — Equity Engine
Calculates agent points from a completed schedule and provides
equity deltas to the solver for next-day bias adjustments.

Point values (per flight):
  DOM solo:       1.0
  DOM pair:       1.2  (back-to-back service, tighter timing)
  DOM triple:     1.5  (hardest DOM run)
  INTL NB:        2.0  (dedicated load, longer service window)
  INTL WB:        3.0  (full galley, high pax, strict timing)
  Short turn:    +0.5  bonus (any type)
"""
from typing import Dict, List, Optional, Tuple
import csv, os

# ── Point values ─────────────────────────────────────────────────────────
PTS_DOM_SOLO   = 1.0
PTS_DOM_PAIR   = 1.2
PTS_DOM_TRIPLE = 1.5
PTS_INTL_NB    = 2.0
PTS_INTL_WB    = 3.0
PTS_SHORT_TURN = 0.5   # bonus added to base

# Equity delta thresholds for solver bias
EQUITY_CAP_REDUCE  =  8.0   # delta above this → cap -1
EQUITY_CAP_RESTORE = -8.0   # delta below this → cap +1 and higher reward
EQUITY_REST_DAY    = 15.0   # delta above this → minimum flights (2)


def flight_points(assignment: dict) -> Tuple[float, dict]:
    """
    Calculate points for a single assignment dict.
    Returns (total_points, breakdown_dict).
    """
    is_intl     = assignment.get('is_intl', False)
    is_pre      = assignment.get('is_precleared', False)
    is_wb       = assignment.get('is_wb', False)
    run_size    = assignment.get('run_size', 1)
    is_short    = assignment.get('is_short_turn', False) or assignment.get('is_tight_turn', False)

    breakdown = {
        'dom_solo': 0, 'dom_pair': 0, 'dom_triple': 0,
        'intl_nb': 0, 'intl_wb': 0, 'short_turn_bonus': 0,
    }

    if is_wb:
        base = PTS_INTL_WB
        breakdown['intl_wb'] = base
    elif is_intl and not is_pre:
        base = PTS_INTL_NB
        breakdown['intl_nb'] = base
    elif run_size == 3:
        base = PTS_DOM_TRIPLE
        breakdown['dom_triple'] = base
    elif run_size == 2:
        base = PTS_DOM_PAIR
        breakdown['dom_pair'] = base
    else:
        base = PTS_DOM_SOLO
        breakdown['dom_solo'] = base

    bonus = PTS_SHORT_TURN if is_short else 0
    breakdown['short_turn_bonus'] = bonus

    return round(base + bonus, 2), breakdown


def calc_agent_equity(assignments: list, ft_csv: str = None, pt_csv: str = None,
                       day_of_week: str = 'Tuesday') -> List[dict]:
    """
    Given the solver's assignments and agent bid CSVs, calculate per-agent
    equity rows for the day.

    Returns list of agent dicts ready for commit_day_equity().
    """
    # Build team → agents mapping from bid CSVs
    team_agents: Dict[str, List[dict]] = {}  # team_id → [{'aa_id', 'last_name', 'first_name'}]

    day_map = {
        'Monday':'Mon','Tuesday':'Tues','Wednesday':'Wed',
        'Thursday':'Thurs','Friday':'Fri','Saturday':'Sat','Sunday':'Sun'
    }
    day_key = day_map.get(day_of_week, 'Tues')

    for csv_path in [ft_csv, pt_csv]:
        if not csv_path or not os.path.exists(csv_path):
            continue
        try:
            with open(csv_path, newline='', encoding='utf-8-sig') as f:
                for row in csv.DictReader(f):
                    team_col  = f"{day_key} Team"
                    shift_col = day_key
                    team_id   = (row.get(team_col) or '').strip()
                    shift     = (row.get(shift_col) or '').strip()
                    if not team_id or not shift or shift.lower() in ('off', ''):
                        continue
                    aa_id = str(row.get('AA ID', '') or row.get('aa_id', '')).strip()
                    if not aa_id:
                        continue
                    if team_id not in team_agents:
                        team_agents[team_id] = []
                    team_agents[team_id].append({
                        'aa_id':      aa_id,
                        'last_name':  (row.get('Last Name') or row.get('last_name', '')).strip(),
                        'first_name': (row.get('First Name') or row.get('first_name', '')).strip(),
                    })
        except Exception as e:
            print(f"[Equity] Error reading {csv_path}: {e}")

    # Accumulate points per team from assignments
    team_points: Dict[str, dict] = {}
    for a in assignments:
        team = a.get('team')
        if not team:
            continue
        if team not in team_points:
            team_points[team] = {
                'flights': 0, 'points': 0.0,
                'breakdown': {'dom_solo':0,'dom_pair':0,'dom_triple':0,
                               'intl_nb':0,'intl_wb':0,'short_turn_bonus':0},
            }
        pts, breakdown = flight_points(a)
        team_points[team]['flights'] += 1
        team_points[team]['points']  += pts
        for k, v in breakdown.items():
            team_points[team]['breakdown'][k] = round(
                team_points[team]['breakdown'].get(k, 0) + v, 2)

    # Build per-agent rows (full points to each agent)
    agent_rows: List[dict] = []
    assigned_agents = set()
    for team_id, tp in team_points.items():
        agents = team_agents.get(team_id, [])
        if not agents:
            # No agent mapping — create a placeholder row for the team
            agent_rows.append({
                'aa_id':          f'TEAM_{team_id}',
                'last_name':      team_id,
                'first_name':     '',
                'team_id':        team_id,
                'flights_assigned': tp['flights'],
                'points_earned':  round(tp['points'], 2),
                'points_breakdown': tp['breakdown'],
            })
            continue
        for agent in agents:
            if agent['aa_id'] in assigned_agents:
                continue   # Shouldn't happen but guard against duplicates
            assigned_agents.add(agent['aa_id'])
            agent_rows.append({
                'aa_id':          agent['aa_id'],
                'last_name':      agent['last_name'],
                'first_name':     agent['first_name'],
                'team_id':        team_id,
                'flights_assigned': tp['flights'],
                'points_earned':  round(tp['points'], 2),
                'points_breakdown': tp['breakdown'],
            })

    return agent_rows


def equity_solver_adjustments(equity_deltas: Dict[str, float],
                               team_agents: Dict[str, List[str]],
                               base_caps: Dict[str, int]) -> Dict[str, Tuple[int, int]]:
    """
    Convert agent equity deltas into per-team (floor, ceiling) adjustments
    for the solver.

    team_agents: {team_id: [aa_id, ...]}
    base_caps:   {team_id: cap}
    Returns:     {team_id: (floor, ceiling)}
    """
    adjustments = {}
    for team_id, agents in team_agents.items():
        if not agents:
            continue
        base_cap = base_caps.get(team_id, 5)
        # Average equity delta across team members
        deltas = [equity_deltas.get(aa, 0.0) for aa in agents]
        avg_delta = sum(deltas) / len(deltas)

        floor   = None
        ceiling = None

        if avg_delta >= EQUITY_REST_DAY:
            # Very overloaded — minimum flights
            ceiling = 2
        elif avg_delta >= EQUITY_CAP_REDUCE:
            # Overloaded — soft cap reduction
            ceiling = max(2, base_cap - 1)
        elif avg_delta <= EQUITY_CAP_RESTORE:
            # Underloaded — soft cap boost and floor
            ceiling = base_cap   # full cap
            floor   = 3          # ensure they get at least 3

        if floor is not None or ceiling is not None:
            adjustments[team_id] = (floor, ceiling)

    return adjustments


def equity_balance_multipliers(equity_deltas: Dict[str, float],
                                team_agents: Dict[str, List[str]]) -> Dict[str, float]:
    """
    Returns per-team balance reward multiplier.
    Underloaded teams get higher multiplier (more attractive to solver).
    Overloaded teams get lower multiplier.
    Normal range: 0.5x to 2.0x applied to balance_reward.
    """
    multipliers = {}
    for team_id, agents in team_agents.items():
        if not agents:
            multipliers[team_id] = 1.0
            continue
        deltas = [equity_deltas.get(aa, 0.0) for aa in agents]
        avg = sum(deltas) / len(deltas)
        # Linear scale: delta -15 → 2.0x, delta 0 → 1.0x, delta +15 → 0.5x
        mult = max(0.5, min(2.0, 1.0 - avg / 30.0))
        multipliers[team_id] = round(mult, 2)
    return multipliers
