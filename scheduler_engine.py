"""
PHL Catering Scheduler — OR-Tools CP-SAT Engine v2.5
Timing constants confirmed by operations coordinator March 2026.
"""
import csv
from ortools.sat.python import cp_model
from dataclasses import dataclass, field
from typing import List, Dict, Tuple, Optional

# ═══════════════════════════════════════════════════════════════════════════════
# TIMING CONSTANTS
# [CONFIRMED] = locked in by coordinator. [CONFIRM] = needs stopwatch validation.
# ═══════════════════════════════════════════════════════════════════════════════

# Drive times kitchen → gate (minutes)
# [CONFIRMED] A=17 (coordinator: 15-20min for A-concourse international gates)
# [CONFIRM] B/C/D/E/F estimated from relative distances — verify with stopwatch
DRIVE_TO_GATE  = {
    'A': 17,   # [CONFIRMED] 15-20min coordinator confirmed → midpoint 17
    'B': 13,   # [CONFIRM] ~12-15min estimated
    'C': 13,   # [CONFIRM] ~12-15min estimated
    'D': 15,   # [CONFIRM] ~13-17min estimated
    'E': 15,   # [CONFIRM] ~13-17min estimated
    'F': 18,   # [CONFIRM] ~16-20min estimated (furthest from kitchen)
}
DEFAULT_DRIVE  = 14

G2G_SAME       = 6    # gate-to-gate same concourse [CONFIRM]
G2G_CROSS      = 10   # gate-to-gate cross concourse [CONFIRM]

# Truck dock reload: clean + reload (minutes)
# [CONFIRMED] Coordinator: DOM truck loads in ~15min
DOCK_RELOAD    = 15   # [CONFIRMED] DOM ~15min; INTL may be longer (TODO separate)
TRUCK_COUNT    = 30   # [UPDATED] 30 to handle peak morning window + false-intl buffer inflation

# Team turnaround (minutes)
# [CONFIRMED] Coordinator: grab-and-go. Team personally ready in ~5min.
# Truck loading (DOCK_RELOAD=15) is the binding constraint, not team readiness.
TEAM_MIN_TURNAROUND  = 5    # [CONFIRMED]
TEAM_READY_OFFSET    = 15   # minutes after shift start before first assignment

MAX_INTL_STD_GAP     = 35
MAX_RUN_SIZE_DOM     = 3
MAX_RUN_SIZE_INTL_NB = 2

# Aircraft service times at aircraft door (minutes)
# [CONFIRM] All three need stopwatch ramp validation
# v4 spec ranges: small=10-15, medium=15-25, widebody=40-60
SVC_TIMES = {
    'H319':10,'H205':10,'319W':10,'319S':10,'A320':10,'A319':10,   # Small NB [CONFIRM]
    '321E':15,'321K':15,'321R':15,'321N':15,'738K':15,'738R':15,'738M':15,  # Med NB [CONFIRM]
    '7878':45,'7879':45,'789P':45,'789W':45,  # Widebody — spec 40-60, using 45 [CONFIRM]
}
DEFAULT_SVC = 15

# Finish buffers: truck must finish by STD − fin_buf (minutes)
# [CONFIRMED] Coordinator:
#   DOM: aim 30-40min (using 35 target), push to 20min only if necessary
#   INTL: aim 40-60min (using 55 target), minimum 40min
FINISH_BUF         = {'dom':35, 'intl':55, 'ron':15}   # [CONFIRMED] target
FINISH_BUF_MIN     = {'dom':20, 'intl':40, 'ron':10}   # [CONFIRMED] short turn (<90min gnd)
FINISH_BUF_TIGHT   = {'dom':10, 'intl':20, 'ron': 5}   # coordinator: "needs servicing regardless"
SHORT_TURN_MAX_GND = 90   # ground time (min) threshold — below this uses FINISH_BUF_MIN
TIGHT_TURN_MAX_GND = 50   # ground time (min) threshold — below this uses FINISH_BUF_TIGHT

WIDEBODY   = {'7878','7879','789P','789W'}
INTL_TYPES = {'International','Precleared'}

# Destinations that are unambiguously US domestic — override CSV Type field if
# the source data incorrectly marks them as International.
# TPA (Tampa) and similar continental US airports occasionally appear as
# International in the AA feed due to codeshare/customs handling flags.
FORCE_DOMESTIC_DESTS = {
    'TPA','MCO','MIA','FLL','ATL','ORD','LAX','DFW','DEN','PHX',
    'LAS','SFO','SEA','BOS','JFK','LGA','EWR','CLT','IAH','HOU',
    'MSP','DTW','BWI','DCA','IAD','SAN','PDX','SLC','STL','BNA',
    'MDW','MCI','RDU','MSY','AUS','SAT','PIT','CLE','CMH','IND',
    'MKE','OMA','OKC','TUL','ABQ','ELP','BUF','ROC','SYR','ALB',
}

# Firehouse (standby) team IDs — excluded from main CP-SAT solve,
# used as last-resort tier in greedy rescue and delay swap analysis.
# TM100 = U00 FH morning (05:00–11:00), TM106 = U30 FH afternoon (15:00–21:00).
FH_TEAMS = {'TM100', 'TM106'}

# ═══════════════════════════════════════════════════════════════════════════════
# VIEW CLASSIFICATION — Morning / Afternoon / WB International
# ═══════════════════════════════════════════════════════════════════════════════
VIEW_SPLIT = 13 * 60   # 13:00 — morning/afternoon boundary

def classify_view(std_min: int, dep_type: str, equip: str) -> str:
    """Classify a flight into its primary schedule view.
    - 'wb_intl':    true international widebody only (not precleared, not NB)
    - 'intl_nb':    true international narrowbody — appears in morning/afternoon view
    - 'morning':    STD < 13:00 and domestic/precleared/intl-NB
    - 'afternoon':  STD >= 13:00 and domestic/precleared/intl-NB
    """
    is_wb = equip in WIDEBODY
    is_true_intl = dep_type == 'International'  # excludes Precleared
    if is_true_intl and is_wb:
        return 'wb_intl'
    if std_min < VIEW_SPLIT:
        return 'morning'
    return 'afternoon'

def view_tags(std_min: int, dep_type: str, equip: str) -> dict:
    """Return view classification fields for an assignment/unassigned entry."""
    primary = classify_view(std_min, dep_type, equip)
    is_wb = equip in WIDEBODY
    return {
        'view_category': primary,
        'in_morning_view':   primary == 'morning' or (primary == 'wb_intl' and std_min < VIEW_SPLIT),
        'in_afternoon_view': primary == 'afternoon' or (primary == 'wb_intl' and std_min >= VIEW_SPLIT),
        'in_wb_intl_view':   primary == 'wb_intl',
        'is_evening_wb':     primary == 'wb_intl' and is_wb and std_min >= VIEW_SPLIT,
        'is_morning_intl':   primary == 'wb_intl' and std_min < VIEW_SPLIT,
    }

# Ground ops floor: min time after block-in before catering can access aircraft
GROUND_OPS_MIN       = 20   # [CONFIRM] deplaning+cleaning start; v4 spec says 30
EARLIEST_SVC_MIN     = 180  # 3-hour pre-STD rule
OVERNIGHT_LATEST_FREE= 270  # overnight teams free by 04:30

def t2m(t):
    if not t or ':' not in str(t): return -1
    p = str(t).strip().split(':')
    return int(p[0])*60 + int(p[1])

def m2t(m):
    if m is None or m < 0: return '--:--'
    return f"{(m//60)%24:02d}:{m%60:02d}"


def drv(gate: str) -> int:
    return DRIVE_TO_GATE.get((gate or 'B')[0].upper(), DEFAULT_DRIVE)

def g2g(g1: str, g2: str) -> int:
    if not g1 or not g2: return G2G_CROSS
    return G2G_SAME if g1[0].upper() == g2[0].upper() else G2G_CROSS


def _ensure_truck_min_fields(a: dict) -> dict:
    """Ensure dock_load_min and truck_free_min exist for consistent truck/run assignment."""
    a = dict(a)
    disp = a.get('dispatch_min')
    if disp is None:
        disp = t2m(a.get('dispatch'))
    if disp is None or disp < 0:
        disp = 0

    if a.get('dock_load_min') is None:
        a['dock_load_min'] = max(0, int(disp) - DOCK_RELOAD)
    if a.get('dock_load') is None:
        a['dock_load'] = m2t(a['dock_load_min'])

    if a.get('truck_free_min') is None:
        tf = a.get('truck_free_at')
        tfm = t2m(tf)
        if tfm >= 0:
            a['truck_free_min'] = tfm
        else:
            se = a.get('svc_end_min')
            if se is None:
                se = t2m(a.get('svc_end'))
            if se is None or se < 0:
                se = int(disp)
            a['truck_free_min'] = int(se) + drv(a.get('gate', 'B')) + DOCK_RELOAD
    if a.get('truck_free_at') is None:
        a['truck_free_at'] = m2t(a['truck_free_min'])

    return a


def apply_run_truck_ids(result: dict, truck_count: int = TRUCK_COUNT) -> dict:
    """Assign stable run_id and backend truck_id for every run in a result payload."""
    if not isinstance(result, dict) or 'assignments' not in result:
        return result

    raw = [dict(a) for a in (result.get('assignments') or [])]
    if not raw:
        return result

    raw = [_ensure_truck_min_fields(a) for a in raw]

    runs: Dict[Tuple[str, int], List[dict]] = {}
    for a in raw:
        key = (a.get('team'), int(a.get('dispatch_min', 0)))
        runs.setdefault(key, []).append(a)

    run_meta = []
    for (team_id, disp), ops in runs.items():
        ops_sorted = sorted(ops, key=lambda o: int(o.get('stop_num', 1)))
        first_flight = str(ops_sorted[0].get('flight'))
        run_id = f"R-{team_id}-{first_flight}"
        start = min(int(o.get('dock_load_min', max(0, disp - DOCK_RELOAD))) for o in ops_sorted)
        end = max(int(o.get('truck_free_min', disp)) for o in ops_sorted)
        run_meta.append({'key': (team_id, disp), 'run_id': run_id, 'start': start, 'end': end})

    run_meta.sort(key=lambda r: (r['start'], r['end'], r['run_id']))
    trucks: List[Tuple[int, int]] = []  # (free_at, truck_num)
    run_to_truck: Dict[Tuple[str, int], str] = {}
    next_truck_num = 1

    for r in run_meta:
        assigned_idx = None
        assigned_num = None
        for idx, (free_at, tnum) in enumerate(trucks):
            if free_at <= r['start']:
                assigned_idx = idx
                assigned_num = tnum
                break
        if assigned_num is None:
            assigned_num = next_truck_num
            next_truck_num += 1
            trucks.append((r['end'], assigned_num))
        else:
            trucks[assigned_idx] = (r['end'], assigned_num)
        trucks.sort(key=lambda x: (x[0], x[1]))
        run_to_truck[r['key']] = f"T{assigned_num:02d}" if assigned_num <= truck_count else f"T{assigned_num:02d}*"

    for (team_id, disp), ops in runs.items():
        meta = next(m for m in run_meta if m['key'] == (team_id, disp))
        truck_id = run_to_truck[(team_id, disp)]
        for o in ops:
            o['run_id'] = meta['run_id']
            o['truck_id'] = truck_id
            o['run_start_min'] = meta['start']
            o['run_end_min'] = meta['end']

    team_ops: Dict[str, List[dict]] = {}
    for a in raw:
        tid = a.get('team')
        if tid:
            team_ops.setdefault(tid, []).append(a)
    for tid in team_ops:
        team_ops[tid].sort(key=lambda x: int(x.get('dispatch_min', 0)))

    team_summary = result.get('team_summary')
    if isinstance(team_summary, list):
        new_summary = []
        for t in team_summary:
            tid = t.get('team_id')
            ops = team_ops.get(tid, [])
            new_summary.append({**t, 'flights': [o.get('flight') for o in ops], 'operations': ops})
        team_summary = new_summary

    return {**result, 'assignments': raw, 'team_ops': team_ops, 'team_summary': team_summary}


def build_result_summary_stats(assignments: list, unassigned: list, flights: list, teams: list,
                               team_ops: dict, *, status: str = 'LIVE', solve_time: float = 0.0,
                               truck_count: int = TRUCK_COUNT, model: str = 'multi-stop v2.5') -> tuple:
    """Rebuild team summary + analytics from a live result state."""
    import statistics as _stats
    from collections import defaultdict as _dd

    assignments = assignments or []
    unassigned = unassigned or []
    flights = flights or []
    team_ops = team_ops or {}

    flight_map = {f.flight_num: f for f in flights}
    assigned_set = {a.get('flight') for a in assignments if a.get('flight')}
    N = len(flights) if flights else (len(assignments) + len(unassigned))
    N_assigned = len(assigned_set) if flights else len(assignments)
    cov_pct = round(100 * N_assigned / max(N, 1), 1)

    ft=[t for t in teams if t.team_type=='FT']
    pt=[t for t in teams if t.team_type=='PT']
    fh=[t for t in teams if t.team_type=='FH']
    ft_fc=[len(team_ops.get(t.team_id,[])) for t in ft]
    pt_fc=[len(team_ops.get(t.team_id,[])) for t in pt]
    fh_fc=[len(team_ops.get(t.team_id,[])) for t in fh]
    rcounts={}
    for u in unassigned:
        rcounts[u.get('reason','?')] = rcounts.get(u.get('reason','?'),0)+1

    intl_a  = sum(1 for a in assignments if a.get('is_intl') and not a.get('is_precleared'))
    intl_t  = sum(1 for f in flights if getattr(f, 'is_intl', False) and not getattr(f, 'is_precleared', False)) if flights else (intl_a + sum(1 for u in unassigned if u.get('is_intl') and not u.get('is_precleared')))
    intl_pct = round(intl_a/max(intl_t,1)*100,1) if intl_t else 100.0

    qs_coverage = round(cov_pct/100*40, 1)
    qs_intl     = 5.0 if intl_pct == 100 else round(intl_pct/100*5, 1)

    def util_score(load, cap):
        if cap == 0:
            return 1.0
        u = load / cap
        if 0.60 <= u <= 0.85:
            return 1.0
        if u < 0.60:
            return u / 0.60
        return max(0, 1.0 - (u - 0.85) / 0.15)

    ft_util  = [util_score(x, t.cap) for x, t in zip(ft_fc, ft)]
    pt_util  = [util_score(x, t.cap) for x, t in zip(pt_fc, pt) if t.cap]
    qs_workload = round(((sum(ft_util)/max(len(ft_util),1))*0.7 + (sum(pt_util)/max(len(pt_util),1))*0.3) * 25, 1)
    ft_std = round(_stats.stdev(ft_fc),2) if len(ft_fc)>1 else 0.0

    multi_count = sum(1 for a in assignments if (a.get('run_size') or 1) > 1)
    buf_violations=0; buf_tight=0; short_turn_count=0
    for a in assignments:
        key='intl' if (a.get('is_intl') and not a.get('is_precleared')) else 'dom'
        if a.get('is_tight_turn'):
            fb=FINISH_BUF_TIGHT[key]
        elif a.get('is_short_turn'):
            fb=FINISH_BUF_MIN[key]; short_turn_count+=1
        else:
            fb=FINISH_BUF[key]
        deadline=a.get('std_min',0)-fb
        se=a.get('svc_end_min',0)
        if se>deadline:
            buf_violations+=1
        elif deadline-se<5:
            buf_tight+=1
    clean_pct=(len(assignments)-buf_violations-buf_tight*0.5)/max(len(assignments),1)
    qs_buffer = round(clean_pct*20, 1)
    qs_truck = round(multi_count/max(len(assignments),1)*15, 1)

    quality_score = round(qs_coverage+qs_intl+qs_workload+qs_buffer+qs_truck, 1)
    quality_grade = 'A' if quality_score>=90 else 'B' if quality_score>=75 else 'C' if quality_score>=60 else 'D'

    gap_analysis = []
    for u in unassigned:
        f = flight_map.get(u.get('flight'))
        if not f:
            continue
        on_shift = [t for t in teams if t.shift_start<=f.latest_dispatch and t.shift_end>=f.latest_dispatch]
        at_cap   = [t for t in on_shift if len(team_ops.get(t.team_id,[]))>=t.cap]
        gap_analysis.append({
            'flight': f.flight_num, 'dest': f.dest, 'std': m2t(f.std), 'std_min': f.std,
            'dispatch_window': f"{m2t(f.earliest_dispatch)}–{m2t(f.latest_dispatch)}",
            'reason': u.get('reason',''), 'on_shift': len(on_shift), 'at_cap': len(at_cap),
            'at_cap_teams': [t.team_id for t in at_cap[:4]],
            'fix_options': [
                f"Additional truck ready by {m2t(f.latest_dispatch-15)}",
                ("Extend cap on: " + ', '.join(t.team_id for t in at_cap[:3])) if at_cap else "Add crossover shift team",
            ],
        })

    shift_util = _dd(list)
    for t in teams:
        h = t.shift_start // 60
        load = len(team_ops.get(t.team_id,[]))
        shift_util[h].append({'team_id':t.team_id,'load':load,'cap':t.cap,'util':round(load/t.cap*100) if t.cap else 0})
    shift_windows = [{'hour':h,'label':f"{h:02d}:00",'teams':v,'avg_util':round(sum(x['util'] for x in v)/len(v))} for h,v in sorted(shift_util.items())]

    active_loads = [len(team_ops.get(t.team_id,[])) for t in teams if team_ops.get(t.team_id) and t.team_type in ('FT','PT')]
    util_stddev = round(_stats.stdev(active_loads), 3) if len(active_loads) > 1 else 0.0

    total_runs = len(set((a.get('team'), a.get('dispatch_min')) for a in assignments))
    stats = {
        'total':N,'assigned':N_assigned,'unassigned':len(unassigned),
        'coverage_pct':cov_pct,
        'intl_assigned':intl_a,'intl_total':intl_t,'intl_pct':intl_pct,
        'teams_deployed':sum(1 for t in teams if team_ops.get(t.team_id)),
        'ft_min_ops':min(ft_fc) if ft_fc else 0,'ft_max_ops':max(ft_fc) if ft_fc else 0,
        'ft_avg_ops':round(sum(ft_fc)/len(ft_fc),1) if ft_fc else 0,
        'ft_std':ft_std,'util_stddev':util_stddev,
        'multi_stop_flights':multi_count,'total_runs':total_runs,
        'status':status,'solve_time':round(solve_time,1),
        'truck_count':truck_count,'unassigned_reasons':rcounts,
        'model':model,
        'quality_score':quality_score,'quality_grade':quality_grade,
        'qs_coverage':qs_coverage,'qs_intl':qs_intl,'qs_workload':qs_workload,'qs_buffer':qs_buffer,'qs_truck':qs_truck,
        'buf_violations':buf_violations,'buf_tight':buf_tight,'short_turn_count':short_turn_count,
        'gap_analysis':gap_analysis,'shift_windows':shift_windows,
    }

    summary=[]
    for t in sorted(teams,key=lambda x:x.shift_start):
        ops=sorted(team_ops.get(t.team_id,[]), key=lambda x: x.get('dispatch_min',0))
        if not ops:
            continue
        run_count = len(set(o.get('dispatch_min') for o in ops))
        summary.append({
            'team_id':t.team_id,'shift':f"{m2t(t.shift_start)}–{m2t(t.shift_end%1440)}",
            'shift_start':t.shift_start,'team_type':t.team_type,
            'ops':run_count,'flight_count':len(ops),'cap':t.cap,
            'first_std':min(o.get('std','--:--') for o in ops),'last_std':max(o.get('std','--:--') for o in ops),
            'flights':[o.get('flight') for o in ops],'operations':ops,
        })

    return summary, stats

@dataclass
class Flight:
    flight_num:str; dest:str; std:int; equip:str; gate:str
    dep_type:str; nose:str; is_ron:bool=False
    arrival_time:int = -1   # inbound arrival (-1 = RON or no data)

    @property
    def is_intl(self):        return self.dep_type in INTL_TYPES
    @property
    def is_precleared(self):  return self.dep_type == 'Precleared'
    @property
    def is_true_intl(self):   return self.is_intl and not self.is_precleared
    @property
    def _buf_key(self):       return 'intl' if self.is_true_intl else 'dom'
    @property
    def is_wb(self):          return self.equip in WIDEBODY
    @property
    def drv_out(self):  return drv(self.gate)
    @property
    def drv_back(self): return drv(self.gate)
    @property
    def svc_time(self): return SVC_TIMES.get(self.equip, DEFAULT_SVC)
    @property
    def ground_mins(self) -> int:
        """Ground time in minutes. -1 for RON."""
        if self.is_ron or self.arrival_time < 0:
            return -1
        return self.std - self.arrival_time

    @property
    def is_short_turn(self) -> bool:
        """True when the target buffer makes the service window negative.
        These flights use FINISH_BUF_MIN automatically. No hardcoded threshold —
        classification is based on whether the target buffer fits the ground time."""
        if self.is_ron or self.arrival_time < 0: return False
        gnd = self.ground_mins
        svc = self.svc_time
        key = self._buf_key   # precleared uses 'dom' buffers, not 'intl'
        return (gnd - GROUND_OPS_MIN - svc - FINISH_BUF[key]) < 0

    @property
    def is_tight_turn(self) -> bool:
        """True when even FINISH_BUF_MIN makes the service window negative.
        These flights use FINISH_BUF_TIGHT — coordinator: 'needs servicing regardless'.
        Classification is dynamic based on feasibility, not a hardcoded threshold."""
        if not self.is_short_turn: return False
        gnd = self.ground_mins
        svc = self.svc_time
        key = self._buf_key   # precleared uses 'dom' buffers, not 'intl'
        return (gnd - GROUND_OPS_MIN - svc - FINISH_BUF_MIN[key]) < 0

    @property
    def fin_buf(self):
        """Three-tier finish buffer selection.
        Tight turn (<50min): FINISH_BUF_TIGHT — coordinator: 'needs servicing regardless'
        Short turn (<90min): FINISH_BUF_MIN  — coordinator: 'push to 20min if necessary'
        Normal:              FINISH_BUF       — coordinator: 'aim for 30-40min'
        """
        key = 'ron' if self.is_ron else self._buf_key  # precleared uses 'dom', not 'intl'
        if self.is_tight_turn:
            return FINISH_BUF_TIGHT[key]
        elif self.is_short_turn:
            return FINISH_BUF_MIN[key]
        else:
            return FINISH_BUF[key]
    @property
    def latest_svc_end(self): return self.std - self.fin_buf
    @property
    def latest_dispatch(self):
        return self.latest_svc_end - self.svc_time - self.drv_out
    @property
    def earliest_svc_start(self):
        """Earliest catering service can begin.
        - 3-hour pre-STD rule: never earlier than STD - EARLIEST_SVC_MIN.
        - Turn flights: service cannot start until arrival + GROUND_OPS_MIN
          (aircraft must deplane/clean before catering accesses the door).
        - Pre-positioning: the truck can drive to the gate BEFORE the aircraft
          arrives and wait. Drive time does NOT count against the service window.
          The floor here is on SERVICE START, not on dispatch.
        - RON flights: only the 3-hour rule applies."""
        three_hr_floor = self.std - EARLIEST_SVC_MIN
        if self.is_ron or self.arrival_time < 0:
            return three_hr_floor
        # Truck pre-positions — arrival floor applies to when service CAN start only.
        # Drive time is separate: dispatch = svc_start - drv_out (can be before arrival).
        arrival_floor = self.arrival_time + GROUND_OPS_MIN
        return max(arrival_floor, three_hr_floor)

    @property
    def earliest_dispatch(self):
        """Earliest the truck can leave the kitchen.
        With pre-positioning: truck leaves in time to arrive at gate when service can start.
        earliest_dispatch = earliest_svc_start - drv_out
        This can be BEFORE the flight's arrival — truck waits at gate.
        (Previously subtracted svc_time too, which was incorrect.)"""
        return self.earliest_svc_start - self.drv_out


@dataclass
class Run:
    """A truck run: 1, 2, or 3 flights served sequentially."""
    flights: List[Flight]
    run_id:  int = 0

    @property
    def is_intl(self): return any(f.is_intl for f in self.flights)
    @property
    def is_dom(self):  return all(not f.is_intl for f in self.flights)

    def truck_busy(self) -> int:
        """DOCK_RELOAD + drive_to_first + sum(svc) + sum(g2g) + drive_back_from_last"""
        t = DOCK_RELOAD + drv(self.flights[0].gate)
        for k, f in enumerate(self.flights):
            if k > 0: t += g2g(self.flights[k-1].gate, f.gate)
            t += f.svc_time
        t += drv(self.flights[-1].gate)
        return t

    def team_busy(self) -> int:
        """drive_to_first + sum(svc) + sum(g2g) + drive_back_from_last + turnaround"""
        t = drv(self.flights[0].gate)
        for k, f in enumerate(self.flights):
            if k > 0: t += g2g(self.flights[k-1].gate, f.gate)
            t += f.svc_time
        t += drv(self.flights[-1].gate) + TEAM_MIN_TURNAROUND
        return t

    def earliest_feasible_dispatch(self, team_ready: int) -> Optional[int]:
        """
        Earliest dispatch where team can serve ALL flights before their deadlines.
        RON flights: team can start as early as team_ready.
        Turn flights: standard latest_dispatch constraint.
        Returns None if infeasible.
        """
        # Dispatch must be ≥ team_ready
        dispatch = team_ready
        # Simulate forward through each stop
        for k, f in enumerate(self.flights):
            if k == 0:
                gate_arrive = dispatch + f.drv_out
            else:
                gate_arrive = svc_end_prev + g2g(self.flights[k-1].gate, f.gate)
            svc_end = gate_arrive + f.svc_time
            if svc_end > f.latest_svc_end:
                return None   # misses this flight's finish deadline
            svc_end_prev = svc_end
        return dispatch

    def latest_feasible_dispatch(self) -> int:
        """Latest dispatch where all flights make deadlines AND respect 3-hour rule + arrival floors."""
        min_latest = 99999
        running_offset = 0
        for k, f in enumerate(self.flights):
            if k == 0:
                running_offset = f.drv_out + f.svc_time
            else:
                running_offset += g2g(self.flights[k-1].gate, f.gate) + f.svc_time
            # Deadline: dispatch + running_offset <= latest_svc_end
            latest_for_deadline = f.latest_svc_end - running_offset
            min_latest = min(min_latest, latest_for_deadline)
            # 3-hour rule + arrival floor: svc_start >= earliest_svc_start
            # svc_start = dispatch + (running_offset - svc_time)
            # → dispatch >= earliest_svc_start - (running_offset - svc_time)
            earliest_for_this = f.earliest_svc_start - (running_offset - f.svc_time)
            if earliest_for_this > latest_for_deadline:
                return -1   # impossible: floor and deadline conflict
        return min_latest


@dataclass
class Team:
    team_id:str; shift_start:int; shift_end:int; cap:int; team_type:str
    @property
    def ready_at(self): return self.shift_start + TEAM_READY_OFFSET


def build_runs(flights: List[Flight]) -> List[Run]:
    """
    DOM: greedy grouper targeting 3 per truck, with solo fallback for every flight.
    Grouped runs given strong objective bonus (+200/extra stop) so solver 
    strongly prefers triples > pairs > solos while staying feasible.
    INTL NB: pairs + solos. INTL WB: solo only.
    """
    runs   = []
    run_id = 0

    dom     = sorted([f for f in flights if not f.is_true_intl], key=lambda f: f.std)
    intl_nb = [f for f in flights if f.is_true_intl and not f.is_wb]
    intl_wb = [f for f in flights if f.is_wb]

    def service_window(f) -> int:
        return f.latest_svc_end - f.earliest_svc_start

    def tight(f) -> bool:
        """< 15 min service window — must run solo."""
        return service_window(f) < 15

    def run_latest_dispatch(flight_list):
        fs = sorted(flight_list, key=lambda f: f.latest_svc_end)
        min_latest = 99999
        offset = 0
        for k, f in enumerate(fs):
            if k == 0: offset = f.drv_out + f.svc_time
            else: offset += g2g(fs[k-1].gate, f.gate) + f.svc_time
            min_latest = min(min_latest, f.latest_svc_end - offset)
            # 3-hour rule + arrival floor: dispatch cannot be earlier than floor
            floor_dispatch = f.earliest_svc_start - (offset - f.svc_time) - f.drv_out
            min_latest = min(min_latest, floor_dispatch)
        return min_latest

    def can_add(current_run, candidate):
        if len(current_run) >= 3: return False
        if tight(candidate): return False
        if any(tight(f) for f in current_run): return False
        last_std = max(f.std for f in current_run)
        if abs(candidate.std - last_std) > 90: return False
        return run_latest_dispatch(current_run + [candidate]) >= 0

    # ── DOM: greedy grouper ───────────────────────────────────────────────
    used_dom = set()
    dom_groups = []

    for f in dom:
        if f.flight_num in used_dom: continue
        added = False
        for r in dom_groups:
            if can_add(r, f):
                r.append(f)
                used_dom.add(f.flight_num)
                added = True
                break
        if not added:
            dom_groups.append([f])
            used_dom.add(f.flight_num)

    # Add grouped runs (triples + pairs) + solo fallback for EVERY DOM flight
    # Step 1: emit the greedy-grouped runs (triples first, then any pairs)
    grouped_flights = set()
    for flight_list in dom_groups:
        if len(flight_list) > 1:
            ordered = sorted(flight_list, key=lambda f: f.latest_svc_end)
            runs.append(Run(ordered, run_id)); run_id += 1
            for f in flight_list: grouped_flights.add(f.flight_num)
        for f in flight_list:
            runs.append(Run([f], run_id)); run_id += 1

    # Pair generation removed — adds too many candidates causing solver timeout

    # ── INTL NB: pairs + solos ────────────────────────────────────────────
    intl_pair_set = set()
    for i in range(len(intl_nb)):
        runs.append(Run([intl_nb[i]], run_id)); run_id += 1
        for j in range(i+1, len(intl_nb)):
            pair = sorted([intl_nb[i], intl_nb[j]], key=lambda f: f.std)
            if pair[1].std - pair[0].std > MAX_INTL_STD_GAP: continue
            if tight(pair[0]) or tight(pair[1]): continue  # solo only for tight windows
            key = (pair[0].flight_num, pair[1].flight_num)
            if key in intl_pair_set: continue
            intl_pair_set.add(key)
            r = Run(pair, run_id)
            if r.latest_feasible_dispatch() >= 0:
                runs.append(r); run_id += 1

    # ── INTL WB: solo only ────────────────────────────────────────────────
    for f in intl_wb:
        runs.append(Run([f], run_id)); run_id += 1

    return runs
def load_flights(dep_path, arr_path):
    with open(dep_path, newline='', encoding='utf-8-sig') as f:
        deps = list(csv.DictReader(f))
    with open(arr_path, newline='', encoding='utf-8-sig') as f:
        arrs = list(csv.DictReader(f))

    # Build arrival lookup by nose number → full arrival record
    arr_by_nose = {}
    for a in arrs:
        n = a.get('Nose #','').strip()
        t = t2m(a.get('Scheduled',''))
        if n and t >= 0:
            arr_by_nose.setdefault(n, []).append({
                'sta': t,
                'flight': a.get('Flight','').strip(),
                'origin': a.get('Origin','').strip(),
                'pax': a.get('Total Customers','').strip(),
            })

    flights = []
    for d in deps:
        std = t2m(d.get('Scheduled',''))
        if std < 0 or d.get('Carrier','AA').strip() != 'AA': continue
        nose = d.get('Nose #','').strip()
        valid_arrivals = [r for r in arr_by_nose.get(nose,[]) if r['sta'] < std]
        is_ron = len(valid_arrivals) == 0
        inbound = max(valid_arrivals, key=lambda r: r['sta']) if valid_arrivals else None
        arrival_time = inbound['sta'] if inbound else -1
        f = Flight(
            flight_num=d.get('Flight','').strip(), dest=d.get('Dest.','').strip(),
            std=std, equip=d.get('Equip.','').strip(), gate=d.get('Gate','B1').strip(),
            dep_type=('Domestic' if d.get('Dest.','').strip().upper() in FORCE_DOMESTIC_DESTS else d.get('Type','Domestic').strip()), nose=nose,
            is_ron=is_ron, arrival_time=arrival_time)
        # Store inbound details as extra attrs for enrichment
        f._inbound_flight  = inbound['flight']  if inbound else None
        f._inbound_origin  = inbound['origin']  if inbound else None
        f._inbound_sta     = m2t(inbound['sta']) if inbound else None
        f._dep_pax         = d.get('Total Customers','').strip()
        flights.append(f)
    return sorted(flights, key=lambda f: f.std)


def build_teams(ft_csv=None, pt_csv=None, day_of_week='Saturday'):
    raw = []
    day_map = {'Monday':'Mon','Tuesday':'Tues','Wednesday':'Wed',
               'Thursday':'Thurs','Friday':'Fri','Saturday':'Sat','Sunday':'Sun'}
    dk = day_map.get(day_of_week,'Sat')
    seen = set()
    for path, is_pt in [(ft_csv,False),(pt_csv,True)]:
        if not path: continue
        try:
            with open(path, newline='', encoding='utf-8-sig') as f:
                rows = list(csv.DictReader(f))
            for row in rows:
                shift = (row.get(day_of_week,'') or row.get(dk,'')).strip()
                dept  = (row.get(f'{day_of_week} Department','') or
                         row.get(f'{dk} Department','')).strip()
                tid   = (row.get(f'{day_of_week} Team','') or
                         row.get(f'{dk} Team','')).strip()
                if not tid or 'TRK' not in dept or shift in ('','Off'): continue
                if tid in seen: continue
                seen.add(tid)
                parts = shift.split('-')
                if len(parts) < 2: continue
                def pt(s):
                    s=s.strip()
                    return t2m(s) if ':' in s else (t2m(s[:2]+':'+s[2:]) if len(s)==4 else -1)
                start,end = pt(parts[0]),pt(parts[1])
                if start<0 or end<0: continue
                ovn = start>=22*60 or start<=60
                ttype='OVERNIGHT' if ovn else ('PT' if is_pt or 'U00' in dept else 'FT')
                # Firehouse teams get their own type — excluded from main solve,
                # available as last-resort in greedy rescue and delay swap analysis.
                if tid in FH_TEAMS:
                    ttype = 'FH'
                cap=3 if ovn else (4 if ttype in ('PT','FH') else 6)
                if end<start: end+=24*60
                raw.append((tid,start,end,cap,ttype))
        except: pass
    if not raw:
        raw=[('TM200',t2m('03:00'),t2m('11:30'),6,'FT'),('TM201',t2m('04:00'),t2m('12:30'),6,'FT'),
             ('TM202',t2m('05:00'),t2m('13:30'),6,'FT'),('TM203',t2m('05:00'),t2m('13:30'),6,'FT'),
             ('TM204',t2m('05:00'),t2m('13:30'),6,'FT'),('TM100',t2m('05:00'),t2m('11:00'),4,'FH'),
             ('TM101',t2m('05:00'),t2m('11:00'),4,'PT'),('TM205',t2m('06:00'),t2m('14:30'),6,'FT'),
             ('TM206',t2m('06:00'),t2m('14:30'),6,'FT'),('TM207',t2m('07:00'),t2m('15:30'),6,'FT'),
             ('TM208',t2m('07:00'),t2m('15:30'),6,'FT'),('TM102',t2m('08:00'),t2m('14:00'),4,'PT'),
             ('TM103',t2m('08:00'),t2m('14:00'),4,'PT'),('TM210',t2m('12:00'),t2m('20:30'),6,'FT'),
             ('TM211',t2m('13:00'),t2m('21:30'),6,'FT'),('TM212',t2m('13:00'),t2m('21:30'),6,'FT'),
             ('TM214',t2m('13:00'),t2m('21:30'),6,'FT'),('TM215',t2m('13:00'),t2m('21:30'),6,'FT'),
             ('TM216',t2m('13:00'),t2m('21:30'),6,'FT'),('TM217',t2m('13:00'),t2m('21:30'),6,'FT'),
             ('TM218',t2m('13:00'),t2m('21:30'),6,'FT'),('TM219',t2m('13:00'),t2m('21:30'),6,'FT'),
             ('TM220',t2m('13:00'),t2m('21:30'),6,'FT'),('TM104',t2m('14:00'),t2m('20:00'),4,'PT'),
             ('TM105',t2m('14:00'),t2m('20:00'),4,'PT'),('TM221',t2m('14:00'),t2m('22:30'),6,'FT'),
             ('TM222',t2m('14:00'),t2m('22:30'),6,'FT'),('TM223',t2m('14:00'),t2m('22:30'),6,'FT'),
             ('TM224',t2m('14:00'),t2m('22:30'),6,'FT'),('TM106',t2m('15:00'),t2m('21:00'),4,'FH'),
             ('TM107',t2m('15:00'),t2m('21:00'),4,'PT'),('TM108',t2m('15:00'),t2m('21:00'),4,'PT'),
             ('TM110',t2m('23:00'),t2m('05:00')+1440,3,'OVERNIGHT'),
             ('TM111',t2m('23:00'),t2m('05:00')+1440,3,'OVERNIGHT')]
    return [Team(tid,s,e,cap,tt) for tid,s,e,cap,tt in raw]


def _solve_core(flights: List[Flight], teams: List[Team],
                truck_count: int, time_limit: int,
                floors: dict = None,          # {team_id: min_flight_count}
                iteration: int = 0) -> dict:
    runs = build_runs(flights)
    flight_index = {f.flight_num: i for i, f in enumerate(flights)}
    N, M, R = len(flights), len(teams), len(runs)

    if floors is None: floors = {}

    if iteration == 0:
        print(f"Engine v2.5 — {N} flights, {M} teams, {truck_count} Hi-Lift trucks")
        print(f"  Generated {R} candidate runs "
              f"({sum(1 for r in runs if len(r.flights)==1)} solo, "
              f"{sum(1 for r in runs if len(r.flights)==2)} pairs, "
              f"{sum(1 for r in runs if len(r.flights)==3)} triples)")
        print(f"  Drive A={DRIVE_TO_GATE['A']} B/C={DRIVE_TO_GATE['B']} "
              f"g2g={G2G_SAME}/{G2G_CROSS}  turnaround={TEAM_MIN_TURNAROUND}min")
        ft_aftn = sum(1 for t in teams if t.team_type == 'FT' and t.shift_start >= 12*60)
        pt_count = sum(1 for t in teams if t.team_type == 'PT')
        ovn_count = sum(1 for t in teams if t.team_type == 'OVERNIGHT')
        print(f"  Soft targets (pass 0): {ft_aftn} FT-aftn(≥3) + {pt_count} PT(≥2) + {ovn_count} OVN(≥2) — no hard floors this pass")
    else:
        active = {k: (f"≥{v[0] or '-'}/≤{v[1] or '-'}" if isinstance(v, tuple) else f"≥{v}")
                  for k,v in floors.items() if v}
        print(f"  Rebalance pass {iteration}: constraints={active}")

    model  = cp_model.CpModel()
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit
    solver.parameters.num_search_workers  = 8
    HORIZON = 30*60

    # ── Each flight covered by at most one run ─────────────────────────────
    # flight_covered[i] = which run covers flight i (at most 1 per flight)
    run_active = {}   # (r, j) -> BoolVar: run r assigned to team j

    # Feasibility: can team j execute run r?
    run_team_ok = {}
    for r_idx, run in enumerate(runs):
        for j, t in enumerate(teams):
            # FH teams are excluded from the main CP-SAT solve.
            # They are available as a last-resort tier in _greedy_rescue
            # and in the delay swap analysis only.
            if t.team_type == 'FH':
                run_team_ok[(r_idx,j)] = False; continue
            if t.team_type == 'OVERNIGHT':
                if any(not f.is_ron for f in run.flights):
                    run_team_ok[(r_idx,j)] = False; continue
                dlb = t.ready_at
                raw_dub = run.latest_feasible_dispatch() + 1440
                dub = min(raw_dub, t.shift_end - run.team_busy(),
                          OVERNIGHT_LATEST_FREE - run.team_busy() + 1440)
                if dlb > dub:
                    run_team_ok[(r_idx,j)] = False; continue
                # Also reject if 3hr floor for any flight exceeds dub
                feasible = True
                running_offset = run.flights[0].drv_out
                for k, f in enumerate(run.flights):
                    if k > 0: running_offset += g2g(run.flights[k-1].gate, f.gate)
                    floor_d = f.earliest_svc_start - (running_offset - f.svc_time) + 1440
                    if floor_d > dub:
                        feasible = False; break
                    running_offset += f.svc_time
                if not feasible:
                    run_team_ok[(r_idx,j)] = False; continue
            else:
                dlb = t.ready_at
                dub = run.latest_feasible_dispatch()
                if dlb > dub:
                    run_team_ok[(r_idx,j)] = False; continue
                # Early-start teams (before 10:00) must return 30 min before shift end.
                # This prevents AM teams from taking 12:00-13:00 flights that keep
                # them out until 13:15+ when they need to be back and turning over.
                return_margin = 30 if t.shift_start < 10*60 else 0
                effective_end = t.shift_end - return_margin
                if effective_end < dub + run.team_busy():
                    dub = effective_end - run.team_busy()
                if dlb > dub:
                    run_team_ok[(r_idx,j)] = False; continue
            run_team_ok[(r_idx,j)] = True
            run_active[(r_idx,j)] = model.NewBoolVar(f'ra{r_idx}_{j}')

    # Each flight covered at most once (by exactly one active run)
    flight_covered = [[] for _ in range(N)]
    for r_idx, run in enumerate(runs):
        for f in run.flights:
            fi = flight_index[f.flight_num]
            for j in range(M):
                if (r_idx,j) in run_active:
                    flight_covered[fi].append(run_active[(r_idx,j)])

    for i in range(N):
        if flight_covered[i]:
            model.Add(sum(flight_covered[i]) <= 1)

    # Team capacity: total FLIGHTS assigned (not runs)
    # A triple costs 3, pair costs 2, solo costs 1 towards the cap
    for j, t in enumerate(teams):
        flight_costs = []
        for r_idx, run in enumerate(runs):
            if (r_idx,j) in run_active:
                flight_costs.append(run_active[(r_idx,j)] * len(run.flights))
        if flight_costs:
            model.Add(sum(flight_costs) <= t.cap)

    # ── Interval variables: team and truck ─────────────────────────────────
    team_intervals  = {j:[] for j in range(M)}
    truck_intervals = []
    truck_demands   = []
    dispatch_vars   = {}

    for r_idx, run in enumerate(runs):
        tb = run.team_busy()
        trb = run.truck_busy()
        for j, t in enumerate(teams):
            if (r_idx,j) not in run_active: continue
            pres = run_active[(r_idx,j)]
            if t.team_type == 'OVERNIGHT':
                dlb = t.ready_at
                raw_dub = run.latest_feasible_dispatch() + 1440
                dub = min(raw_dub, t.shift_end - run.team_busy())
            else:
                dlb = t.ready_at
                raw_dub = run.latest_feasible_dispatch()
                dub = min(raw_dub, t.shift_end - run.team_busy())
            if dlb > dub: continue

            # Dispatch variable
            sv = model.NewIntVar(dlb, dub, f'sv{r_idx}_{j}')
            dispatch_vars[(r_idx,j)] = sv

            # TEAM interval
            te = model.NewIntVar(dlb+tb, dub+tb, f'te{r_idx}_{j}')
            tiv = model.NewOptionalIntervalVar(sv, tb, te, pres, f'tiv{r_idx}_{j}')
            team_intervals[j].append(tiv)

            # Verify each flight's service-end deadline
            offset = run.flights[0].drv_out
            for k, f in enumerate(run.flights):
                if k > 0: offset += g2g(run.flights[k-1].gate, f.gate)
                offset += f.svc_time
                se = model.NewIntVar(dlb+offset, HORIZON, f'se{r_idx}_{j}_{k}')
                model.Add(se == sv + offset - f.svc_time + f.svc_time).OnlyEnforceIf(pres)
                # Actually: svc_end = sv + (drive_to_gate + cumulative_g2g_and_svc up to this flight)
                # Simpler: compute running offset to svc_end of flight k
                run_offset = run.flights[0].drv_out
                for kk in range(k+1):
                    if kk > 0: run_offset += g2g(run.flights[kk-1].gate, run.flights[kk].gate)
                    run_offset += run.flights[kk].svc_time
                se2 = model.NewIntVar(dlb+run_offset, HORIZON, f'se2_{r_idx}_{j}_{k}')
                model.Add(se2 == sv + run_offset).OnlyEnforceIf(pres)
                # Overnight: deadlines are on 30-hour clock (+1440)
                deadline = f.latest_svc_end + (1440 if t.team_type == 'OVERNIGHT' else 0)
                model.Add(se2 <= deadline).OnlyEnforceIf(pres)

                # ── EARLIEST SERVICE CONSTRAINT (arrival floor + 3-hour rule) ──
                # earliest_svc_start now covers BOTH arrival floor AND 3-hr pre-STD rule.
                # Applies to all flights including RON.
                run_offset_start = run_offset - f.svc_time
                # svc_start = sv + run_offset_start
                # Constraint: sv + run_offset_start >= f.earliest_svc_start
                # ⟺ sv >= f.earliest_svc_start - run_offset_start
                floor_dispatch = f.earliest_svc_start - run_offset_start
                # Overnight: adjust floor to 30-hour clock
                if t.team_type == 'OVERNIGHT':
                    floor_dispatch += 1440
                if floor_dispatch > dub:
                    model.Add(pres == 0)   # impossible window — mark inactive
                elif floor_dispatch > dlb:
                    model.Add(sv >= floor_dispatch).OnlyEnforceIf(pres)

            # TRUCK interval: starts at dispatch - DOCK_RELOAD
            trs_lb = max(0, dlb - DOCK_RELOAD)
            trs = model.NewIntVar(trs_lb, max(trs_lb,dub), f'trs{r_idx}_{j}')
            model.Add(trs == sv - DOCK_RELOAD).OnlyEnforceIf(pres)
            tre = model.NewIntVar(trs_lb+trb, dub+trb, f'tre{r_idx}_{j}')
            triv = model.NewOptionalIntervalVar(trs, trb, tre, pres, f'triv{r_idx}_{j}')
            truck_intervals.append(triv)
            truck_demands.append(1)

    # No-overlap per team
    for j in range(M):
        if len(team_intervals[j]) > 1:
            model.AddNoOverlap(team_intervals[j])

    # Truck pool constraint
    if truck_intervals:
        model.AddCumulative(truck_intervals, truck_demands, truck_count)

    # Overnight teams: no additional constraint needed.
    # The 30-hour clock in feasibility + interval bounds already ensures correct timing.
    # RON-only filter applied in run_team_ok above.

    # ── Per-team floor + ceiling constraints (from iterative rebalancer) ───
    # constraints dict: {team_id: (floor, ceiling)} — either can be None.
    # Hard floors ensure starved teams get flights.
    # Ceilings cap overloaded teams so starved teams get the shared pool.
    _ft_aftn_targets: dict = {}   # j → (soft_target, team_runs_list) for FT afternoon teams
    for j, t in enumerate(teams):
        floor, ceiling = None, None
        if floors and t.team_id in floors:
            val = floors[t.team_id]
            if isinstance(val, tuple):
                floor, ceiling = val
            else:
                floor = val  # backwards compat: plain int → floor only

        # ── Floor/ceiling logic ─────────────────────────────────────────────
        #
        # Pass 0 (initial solve): ALL minimums are SOFT — encoded as objective
        # rewards, never as hard model constraints. This guarantees the initial
        # solve is always feasible: CP-SAT can always find at least the trivial
        # solution (assign nothing) and then optimise upward from there.
        #
        # Root cause of the full-day INFEASIBLE bug:
        #   In the combined (full-day) solve, all teams share one truck pool
        #   simultaneously. Hard floors of ≥2 on every OVERNIGHT + PT team, plus
        #   ≥3 on every FT afternoon team (~14 teams × 3 = 42 mandatory flights)
        #   created a combined hard requirement the truck pool couldn't satisfy.
        #   CP-SAT detected the conflict during propagation and returned INFEASIBLE
        #   in <1 second, triggering the greedy fallback and 23 unassigned flights.
        #   Partial-day runs avoided this because teams were split across two
        #   independent solves rather than sharing one truck pool.
        #
        # Pass 1+ (rebalancer): hard floors from _compute_balance_floors() are
        #   applied normally — by that point we know coverage is feasible.

        # Soft targets for ALL team types (used in objective below)
        if t.team_type == 'OVERNIGHT':
            _soft_target = 2
        elif t.team_type == 'PT':
            _soft_target = 2
        elif t.team_type == 'FT' and t.shift_start >= 12*60:
            _soft_target = 3
        else:
            _soft_target = 0

        team_runs = [run_active[(r_idx,j)] * len(runs[r_idx].flights)
                     for r_idx in range(R) if (r_idx,j) in run_active]
        if not team_runs: continue

        # Hard floors: ONLY from the iterative rebalancer (pass 1+), never in pass 0.
        # This ensures pass 0 is always feasible regardless of truck-pool pressure.
        if iteration > 0:
            if floor and floor > 0 and len(team_runs) >= floor:
                model.Add(sum(team_runs) >= floor)
        if ceiling and ceiling > 0 and ceiling < t.cap:
            model.Add(sum(team_runs) <= ceiling)

        # Soft target stored for objective use below
        if _soft_target > 0:
            _ft_aftn_targets[j] = (_soft_target, team_runs)

    # ── Objective ──────────────────────────────────────────────────────────
    # ── Objective: Coverage first, then smart balance ──────────────────────
    # Coverage: 1000 pts per flight (always dominates balance)
    total_flights = []
    for i in range(N):
        if flight_covered[i]:
            covered = model.NewBoolVar(f'cov{i}')
            model.Add(sum(flight_covered[i]) >= 1).OnlyEnforceIf(covered)
            model.Add(sum(flight_covered[i]) == 0).OnlyEnforceIf(covered.Not())
            total_flights.append(covered)

    # Grouping bonus: reward multi-stop runs for truck efficiency
    # 500 pts per extra stop: pair=500, triple=1000
    # Strong enough that solver always prefers triple over 3 solos when feasible
    # (1000 bonus > any combination of balance adjustments)
    grouping_bonus = []
    for r_idx, run in enumerate(runs):
        extra_stops = len(run.flights) - 1
        if extra_stops > 0:
            for j in range(M):
                if (r_idx,j) in run_active:
                    # Direct coefficient — no extra IntVar
                    grouping_bonus.append(extra_stops * 500 * run_active[(r_idx,j)])

    # Smart balance: diminishing returns per team
    # FT target = 4.  Reward: flights 1-4 → 300pts each, flight 5 → 50pts
    # PT target = 3.  Reward: flights 1-3 → 280pts each, flight 4 → 80pts
    # Result: solver prefers PT 1-3 (280) over FT 5th slot (50)
    # and prefers FT 1-4 (300) over PT 1-3 (280) when both are available.
    # This naturally balances across the shift window.
    balance_rewards = []
    for j, t in enumerate(teams):
        if t.team_type not in ('FT', 'PT'): continue
        # Count flights for this team
        flight_costs = [run_active[(r_idx,j)] * len(runs[r_idx].flights)
                        for r_idx in range(R) if (r_idx,j) in run_active]
        if not flight_costs: continue
        load = model.NewIntVar(0, t.cap, f'bal_load{j}')
        model.Add(load == sum(flight_costs))

        if t.team_type == 'FT':
            target = 5
            rate_below = 300  # pts per flight up to target
            rate_above = 50   # pts per flight above target (6th slot)
        else:  # PT
            target = 3
            rate_below = 280
            rate_above = 80

        # below_target = min(load, target)
        below = model.NewIntVar(0, target, f'bal_below{j}')
        model.AddMinEquality(below, [load, model.NewConstant(target)])
        # above_target = max(load - target, 0)
        above = model.NewIntVar(0, t.cap, f'bal_above{j}')
        model.Add(above == load - below)

        reward = model.NewIntVar(0, t.cap * rate_below, f'bal_rew{j}')
        model.Add(reward == below * rate_below + above * rate_above)
        balance_rewards.append(reward)

    # ── Phase-split alignment penalty ──────────────────────────────────────
    # Use shift_START (not shift_end) to classify teams:
    #   Early-start teams: shift_start < 10:00 (03:00-09:59 starters)
    #   Afternoon teams:   shift_start >= 12:00
    #
    # Rule 1 — Early-start team on afternoon flight (latest_dispatch > 12:00):
    #   Penalty -800 pts/flight. Strong preference for afternoon team, but early
    #   team is last resort if no afternoon team available (net +200 vs unassigned).
    #
    # Rule 2 — Afternoon team on early-morning flight (latest_dispatch < 09:00):
    #   Penalty -800 pts/flight. Keeps afternoon capacity reserved for PM bank.
    PHASE_SPLIT        = 12 * 60   # 12:00 — dispatch boundary (noon)
    EARLY_CUTOFF       =  9 * 60   # 09:00 — early-morning dispatch boundary
    EARLY_START_MAX    = 10 * 60   # teams starting before 10:00 = "early-start"
    LATE_EARLY_MIN     =  7 * 60   # 07:00 — "late-early" teams: 07:00-09:59 starters
    CROSSOVER_CUTOFF   = 12 * 60   # 12:00 — tightened: flights dispatching after 12:00 must go to later teams
    AFTN_START_MIN     = 12 * 60   # teams starting at/after 12:00 = "afternoon"
    PHASE_PENALTY      = 800       # pts penalty per misaligned flight

    # Late-early teams (07:00-09:59) get a BONUS for crossover flights (12:00-14:00).
    # These teams (TM207 07:00-15:30, TM208 07:00-15:30, TM102/103 08:00-14:00) have
    # shift_ends that naturally cover the 12:00-14:00 gap. Without this, the algorithm
    # orphans crossover flights because AM teams are penalised and PM teams not yet on shift.
    CROSSOVER_BONUS    = 20        # pts per crossover flight on a late-early team

    phase_penalties = []
    for r_idx, run in enumerate(runs):
        run_latest_d = run.latest_feasible_dispatch()
        n_flights    = len(run.flights)

        for j, t in enumerate(teams):
            if (r_idx, j) not in run_active:
                continue

            # OVERNIGHT teams handle their own RON-only constraint — skip phase penalty
            if t.team_type == 'OVERNIGHT':
                continue

            penalty_pts = 0

            later_team_available = any(teams[jj].shift_start >= 11 * 60 for jj in range(len(teams)) if (r_idx, jj) in run_active)

            if t.shift_start < EARLY_START_MAX and run_latest_d > PHASE_SPLIT:
                # Keep crossover narrow. After 13:00, later teams should own the work if feasible.
                if t.shift_start >= LATE_EARLY_MIN and run_latest_d <= CROSSOVER_CUTOFF:
                    penalty_pts = CROSSOVER_BONUS * n_flights
                else:
                    penalty_pts = -PHASE_PENALTY * n_flights

                # Strong extra penalty if a later-start team can feasibly own this later run.
                if run_latest_d >= 13 * 60 and later_team_available:
                    penalty_pts -= 700 * n_flights

            # Afternoon team (12:00+) taking early-morning flight
            elif t.shift_start >= AFTN_START_MIN and run_latest_d < EARLY_CUTOFF:
                penalty_pts = -PHASE_PENALTY * n_flights

            # Positive preference for later-start teams on late-bank work.
            elif t.shift_start >= 11 * 60 and run_latest_d >= 12 * 60:
                penalty_pts += 400 * n_flights

            if penalty_pts != 0:
                # Direct coefficient — no extra IntVar needed
                phase_penalties.append(penalty_pts * run_active[(r_idx, j)])

    # ── Soft target rewards (replaces all hard floor constraints in pass 0) ──
    # OVERNIGHT target=2: 400 pts each flight toward target (strong pull, small fleet)
    # PT target=2:        350 pts each flight toward target
    # FT afternoon tgt=3: 350 pts each flight toward target
    # Above target: 50 pts (same as FT 5th-slot balance rate)
    #
    # At 350-400 pts, the solver strongly fills these teams toward their targets,
    # but never at the cost of leaving a flight unserved (1000 pts per covered flight).
    # Hard floors are applied in rebalance passes 1+ once feasibility is confirmed.
    ft_aftn_soft_rewards = []
    FT_AFTN_ABOVE_RATE = 50
    for j, (soft_tgt, t_runs) in _ft_aftn_targets.items():
        t = teams[j]
        if not t_runs: continue
        target_rate = 400 if t.team_type == 'OVERNIGHT' else 350
        load_v = model.NewIntVar(0, t.cap, f'fta_load{j}')
        model.Add(load_v == sum(t_runs))
        below_v = model.NewIntVar(0, soft_tgt, f'fta_below{j}')
        model.AddMinEquality(below_v, [load_v, model.NewConstant(soft_tgt)])
        above_v = model.NewIntVar(0, t.cap, f'fta_above{j}')
        model.Add(above_v == load_v - below_v)
        rew_v = model.NewIntVar(0, t.cap * target_rate, f'fta_rew{j}')
        model.Add(rew_v == below_v * target_rate + above_v * FT_AFTN_ABOVE_RATE)
        ft_aftn_soft_rewards.append(rew_v)

    model.Maximize(
        sum(total_flights) * 1000
        + sum(balance_rewards)
        + sum(grouping_bonus)
        + sum(phase_penalties)
        + sum(ft_aftn_soft_rewards)
    )

    sc = solver.Solve(model)
    sn = {cp_model.OPTIMAL:'OPTIMAL',cp_model.FEASIBLE:'FEASIBLE',
          cp_model.INFEASIBLE:'INFEASIBLE',cp_model.UNKNOWN:'UNKNOWN'}.get(sc,'UNKNOWN')
    print(f"Status: {sn}  ({solver.WallTime():.1f}s)")

    if sc not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        # Solver timed out or went infeasible — fall back to pure greedy for all flights
        print(f"  ⚠ Solver returned {sn} — running greedy fallback for all {N} flights")
        all_unassigned = [{'flight':f.flight_num,'dest':f.dest,'std':m2t(f.std),
            'std_min':f.std,'type':f.dep_type,'equip':f.equip,'gate':f.gate,
            'is_ron':f.is_ron,'is_short_turn':f.is_short_turn,'is_tight_turn':f.is_tight_turn,
            'ground_mins':f.ground_mins,'is_intl':f.is_intl,'is_wb':f.is_wb,
            'team':None,'reason':sn,'nose':f.nose,
            'pax':getattr(f,'_dep_pax',None),
            'inbound_flight':getattr(f,'_inbound_flight',None),
            'inbound_origin':getattr(f,'_inbound_origin',None),
            'inbound_sta':getattr(f,'_inbound_sta',None),
            'ground_time': m2t(f.std - f.arrival_time) if f.arrival_time > 0 else None,
            **view_tags(f.std, f.dep_type, f.equip),
        } for f in flights]
        fallback_ops  = {t.team_id: [] for t in teams}
        fallback_asgn = []
        fallback_done = set()
        rescued = _greedy_rescue(all_unassigned, teams, fallback_ops, fallback_asgn, fallback_done)
        for r in rescued:
            fallback_asgn.append(r)
            fallback_done.add(r['flight'])
            tid = r.get('team')
            if tid:
                fallback_ops.setdefault(tid, []).append(r)
        still_unassigned = [u for u in all_unassigned if u['flight'] not in fallback_done]

        # Build minimal stats so the UI always has numbers
        import statistics as _stats_fb
        N_a   = len(fallback_done)
        cov   = round(100 * N_a / max(N, 1), 1)
        ft_fb = [t for t in teams if t.team_type == 'FT']
        ft_fc_fb = [len(fallback_ops.get(t.team_id, [])) for t in ft_fb]
        util_sd = round(_stats_fb.stdev(ft_fc_fb), 3) if len(ft_fc_fb) > 1 else 0.0
        fb_stats = {
            'total': N, 'assigned': N_a, 'unassigned': len(still_unassigned),
            'coverage_pct': cov, 'solve_time': round(solver.WallTime(), 1),
            'status': f'GREEDY ({sn})', 'balance_iterations': 0,
            'quality_score': None, 'quality_grade': '—',
            'util_stddev': util_sd, 'ft_min_ops': min(ft_fc_fb) if ft_fc_fb else 0,
            'ft_max_ops': max(ft_fc_fb) if ft_fc_fb else 0,
            'ft_avg_ops': round(sum(ft_fc_fb)/len(ft_fc_fb),1) if ft_fc_fb else 0,
            'intl_assigned': sum(1 for fn in fallback_done
                                 if next((f for f in flights if f.flight_num==fn), None) and
                                    next(f for f in flights if f.flight_num==fn).is_intl),
            'intl_total': sum(1 for f in flights if f.is_intl),
            'buf_violations': 0, 'buf_tight': 0,
            'unassigned_reasons': {sn: len(still_unassigned)},
        }
        # Build team_summary from fallback_ops
        fb_summary = []
        for t in teams:
            ops = fallback_ops.get(t.team_id, [])
            if ops:
                fb_summary.append({
                    'team_id': t.team_id, 'shift': f"{m2t(t.shift_start)}-{m2t(t.shift_end)}",
                    'team_type': t.team_type, 'cap': t.cap,
                    'flight_count': len(ops), 'ops': len(ops),
                    'flights': [o['flight'] for o in ops],
                    'operations': ops,
                })
        return {'status': f'GREEDY ({sn})', 'assignments': fallback_asgn,
                'unassigned': still_unassigned, 'team_ops': fallback_ops,
                'team_summary': fb_summary, 'stats': fb_stats}

    # Extract solution
    assignments=[]; unassigned=[]; team_ops={t.team_id:[] for t in teams}
    flights_assigned = set()

    for r_idx, run in enumerate(runs):
        for j, t in enumerate(teams):
            if (r_idx,j) not in run_active: continue
            if solver.Value(run_active[(r_idx,j)]) != 1: continue
            disp = solver.Value(dispatch_vars[(r_idx,j)]) if (r_idx,j) in dispatch_vars else t.ready_at

            # Compute per-flight timing within the run
            running_offset = run.flights[0].drv_out
            for k, f in enumerate(run.flights):
                if k > 0:
                    running_offset += g2g(run.flights[k-1].gate, f.gate)
                svc_s = disp + running_offset
                running_offset += f.svc_time
                svc_e = disp + running_offset

                dock_load  = disp - DOCK_RELOAD
                team_ret   = disp + run.team_busy() - TEAM_MIN_TURNAROUND
                team_free  = disp + run.team_busy()
                truck_free = disp + run.truck_busy()

                entry = {
                    'flight':f.flight_num,'dest':f.dest,'std':m2t(f.std),'std_min':f.std,
                    'type':f.dep_type,'equip':f.equip,'gate':f.gate,'is_ron':f.is_ron,'is_short_turn':f.is_short_turn,'is_tight_turn':f.is_tight_turn,'ground_mins':f.ground_mins,
                    'is_intl':f.is_intl,'is_precleared':f.is_precleared,'is_wb':f.is_wb,'team':t.team_id,
                    'run_size':len(run.flights),'stop_num':k+1,
                    'dock_load':m2t(dock_load),'dock_load_min':dock_load,
                    'dispatch':m2t(disp),
                    'svc_start':m2t(svc_s),'svc_end':m2t(svc_e),
                    'team_free_at':m2t(team_free),'truck_free_at':m2t(truck_free),
                    'dispatch_min':disp,'svc_start_min':svc_s,'svc_end_min':svc_e,
                    'team_free_min':team_free,'truck_free_min':truck_free,'reason':None,
                    # Inbound / turn details
                    'nose':f.nose,
                    'pax':getattr(f,'_dep_pax',None),
                    'inbound_flight':getattr(f,'_inbound_flight',None),
                    'inbound_origin':getattr(f,'_inbound_origin',None),
                    'inbound_sta':getattr(f,'_inbound_sta',None),
                    'ground_time': m2t(f.std - f.arrival_time) if f.arrival_time > 0 else None,
                    # View classification
                    **view_tags(f.std, f.dep_type, f.equip),
                }
                assignments.append(entry)
                team_ops[t.team_id].append(entry)
                flights_assigned.add(f.flight_num)

    # Unassigned
    for f in flights:
        if f.flight_num not in flights_assigned:
            eligible=[j for j,t in enumerate(teams)
                      if any(run_team_ok.get((r_idx,j)) for r_idx,r in enumerate(runs)
                             if any(rf.flight_num==f.flight_num for rf in r.flights))]
            # Determine actual reason for non-assignment
            if not eligible:
                reason = 'OUTSIDE_SHIFT_WINDOW'
            else:
                # Check if any eligible team has capacity (not at cap)
                has_capacity = any(
                    len([a for a in assignments if a.get('team') == teams[j].team_id]) < teams[j].cap
                    for j in eligible
                )
                reason = 'NO_TEAM_CAPACITY' if has_capacity is False else 'NO_TRUCK_AVAILABLE'
            unassigned.append({'flight':f.flight_num,'dest':f.dest,'std':m2t(f.std),
                'std_min':f.std,'type':f.dep_type,'equip':f.equip,'gate':f.gate,
                'is_ron':f.is_ron,'is_short_turn':f.is_short_turn,'is_tight_turn':f.is_tight_turn,'ground_mins':f.ground_mins,'is_intl':f.is_intl,'is_precleared':f.is_precleared,'is_wb':f.is_wb,'team':None,'reason':reason,
                'nose':f.nose,
                'pax':getattr(f,'_dep_pax',None),
                'inbound_flight':getattr(f,'_inbound_flight',None),
                'inbound_origin':getattr(f,'_inbound_origin',None),
                'inbound_sta':getattr(f,'_inbound_sta',None),
                'ground_time': m2t(f.std - f.arrival_time) if f.arrival_time > 0 else None,
                **view_tags(f.std, f.dep_type, f.equip),
            })

    for tid in team_ops: team_ops[tid].sort(key=lambda x:x['dispatch_min'] or 0)

    # Stats
    import statistics as _stats
    multi_count = sum(1 for a in assignments if a['run_size']>1)
    ft=[t for t in teams if t.team_type=='FT']
    pt=[t for t in teams if t.team_type=='PT']
    ft_fc=[len(team_ops[t.team_id]) for t in ft]
    pt_fc=[len(team_ops[t.team_id]) for t in pt]
    fc = ft_fc  # backwards compat
    rcounts={}
    for u in unassigned: rcounts[u.get('reason','?')]=rcounts.get(u.get('reason','?'),0)+1

    # ── Quality Score (0-105) ───────────────────────────────────────────────
    N_assigned = len(flights_assigned)
    cov_pct = round(100*N_assigned/max(N,1),1)
    flight_map = {f.flight_num: f for f in flights}
    intl_a  = sum(1 for fn in flights_assigned if flight_map.get(fn) and flight_map[fn].is_intl)
    intl_t  = sum(1 for f in flights if f.is_intl)
    intl_pct = round(intl_a/max(intl_t,1)*100,1)

    # Coverage (0-40) + INTL bonus (0-5)
    qs_coverage = round(cov_pct/100*40, 1)
    qs_intl     = 5.0 if intl_pct == 100 else round(intl_pct/100*5, 1)

    # Workload balance (0-25): score based on capacity utilisation ratio
    # Target utilisation: 60-85%. Under 50% = underloaded. Over 90% = overloaded.
    # This handles teams with different shift lengths naturally.
    def util_score(load, cap):
        if cap == 0: return 1.0
        u = load / cap
        if 0.60 <= u <= 0.85: return 1.0    # ideal range
        if u < 0.60: return u / 0.60         # linear ramp up
        return max(0, 1.0 - (u - 0.85) / 0.15)  # penalise >85%, zero at 100%
    ft_util  = [util_score(x, t.cap) for x, t in zip(ft_fc, ft)]   # use actual cap
    pt_util  = [util_score(x, t.cap) for x, t in zip(pt_fc, [t for t in teams if t.team_type=='PT']) if t.cap]
    qs_workload = round(((sum(ft_util)/max(len(ft_util),1))*0.7
                         + (sum(pt_util)/max(len(pt_util),1))*0.3) * 25, 1)
    ft_std = round(_stats.stdev(ft_fc),2) if len(ft_fc)>1 else 0.0

    # Buffer compliance (0-20): violations and tight margins
    buf_violations=0; buf_tight=0; short_turn_count=0
    for a in assignments:
        key='intl' if (a.get('is_intl') and not a.get('is_precleared')) else 'dom'
        if a.get('is_tight_turn'):   fb=FINISH_BUF_TIGHT[key]
        elif a.get('is_short_turn'): fb=FINISH_BUF_MIN[key]; short_turn_count+=1
        else:                        fb=FINISH_BUF[key]
        deadline=a['std_min']-fb; se=a.get('svc_end_min',0)
        if se>deadline: buf_violations+=1
        elif deadline-se<5: buf_tight+=1
    clean_pct=(N_assigned-buf_violations-buf_tight*0.5)/max(N_assigned,1)
    qs_buffer = round(clean_pct*20, 1)

    # Truck utilisation (0-15): reward multi-stop efficiency
    qs_truck = round(multi_count/max(N_assigned,1)*15, 1)

    quality_score = round(qs_coverage+qs_intl+qs_workload+qs_buffer+qs_truck, 1)
    quality_grade = 'A' if quality_score>=90 else 'B' if quality_score>=75 else 'C' if quality_score>=60 else 'D'

    # ── Staffing gap analysis ───────────────────────────────────────────────
    gap_analysis = []
    for u in unassigned:
        f = next((fl for fl in flights if fl.flight_num==u['flight']), None)
        if not f: continue
        on_shift = [t for t in teams
                    if t.shift_start<=f.latest_dispatch and t.shift_end>=f.latest_dispatch]
        at_cap   = [t for t in on_shift if len(team_ops.get(t.team_id,[]))>=t.cap]
        gap_analysis.append({
            'flight':   f.flight_num, 'dest': f.dest, 'std': m2t(f.std),
            'std_min':  f.std,
            'dispatch_window': f"{m2t(f.earliest_dispatch)}–{m2t(f.latest_dispatch)}",
            'reason':   u.get('reason',''),
            'on_shift': len(on_shift), 'at_cap': len(at_cap),
            'at_cap_teams': [t.team_id for t in at_cap[:4]],
            'fix_options': [
                f"Additional truck ready by {m2t(f.latest_dispatch-15)}",
                "Extend cap on: " + ', '.join(t.team_id for t in at_cap[:3]) if at_cap else "Add crossover shift team",
            ]
        })

    # ── Team utilisation by shift window ───────────────────────────────────
    from collections import defaultdict as _dd
    shift_util = _dd(list)
    for t in teams:
        h = t.shift_start // 60
        load = len(team_ops.get(t.team_id,[]))
        shift_util[h].append({'team_id':t.team_id,'load':load,'cap':t.cap,
                               'util':round(load/t.cap*100) if t.cap else 0})
    shift_windows = [{'hour':h,'label':f"{h:02d}:00",'teams':v,
                       'avg_util':round(sum(x['util'] for x in v)/len(v))}
                     for h,v in sorted(shift_util.items())]

    import statistics as _stats_mod
    active_loads = [len(team_ops.get(t.team_id,[])) for t in teams
                    if team_ops.get(t.team_id) and t.team_type in ('FT','PT')]
    util_stddev = round(_stats_mod.stdev(active_loads), 3) if len(active_loads) > 1 else 0.0

    stats={'total':N,'assigned':N_assigned,'unassigned':len(unassigned),
           'coverage_pct':cov_pct,
           'intl_assigned':intl_a,'intl_total':intl_t,'intl_pct':intl_pct,
           'teams_deployed':sum(1 for t in teams if team_ops.get(t.team_id)),
           'ft_min_ops':min(ft_fc) if ft_fc else 0,'ft_max_ops':max(ft_fc) if ft_fc else 0,
           'ft_avg_ops':round(sum(ft_fc)/len(ft_fc),1) if ft_fc else 0,
           'ft_std':ft_std, 'util_stddev': util_stddev,
           'multi_stop_flights':multi_count,'total_runs':R,
           'status':sn,'solve_time':round(solver.WallTime(),1),
           'truck_count':truck_count,'unassigned_reasons':rcounts,
           'model':'multi-stop v2.5',
           # Quality score
           'quality_score':quality_score,'quality_grade':quality_grade,
           'qs_coverage':qs_coverage,'qs_intl':qs_intl,
           'qs_workload':qs_workload,'qs_buffer':qs_buffer,'qs_truck':qs_truck,
           'buf_violations':buf_violations,'buf_tight':buf_tight,
           'short_turn_count':short_turn_count,
           # Staffing gap
           'gap_analysis':gap_analysis,
           'shift_windows':shift_windows,
           }

    summary=[]
    for t in sorted(teams,key=lambda x:x.shift_start):
        ops=team_ops.get(t.team_id,[])
        if not ops: continue
        run_count = len(set(o['dispatch_min'] for o in ops))
        summary.append({'team_id':t.team_id,'shift':f"{m2t(t.shift_start)}–{m2t(t.shift_end%1440)}",
            'shift_start':t.shift_start,'team_type':t.team_type,
            'ops':run_count,'flight_count':len(ops),'cap':t.cap,
            'first_std':min(o['std'] for o in ops),'last_std':max(o['std'] for o in ops),
            'flights':[o['flight'] for o in ops],'operations':ops})

    # ── Post-processing pass: greedy rescue of remaining unassigned flights ──
    # The CP-SAT solver maximises coverage globally but can miss individual flights
    # during dense windows when there are tight timing conflicts. This greedy pass
    # iterates over unassigned flights in dispatch-deadline order and tries each
    # on-shift team in order of availability. No CP overhead — pure feasibility check.
    if unassigned:
        rescued = _greedy_rescue(unassigned, teams, team_ops, assignments, flights_assigned)
        if rescued:
            unassigned = [u for u in unassigned if u['flight'] not in {r['flight'] for r in rescued}]
            assignments.extend(rescued)
            for r in rescued:
                flights_assigned.add(r['flight'])
                team_ops.setdefault(r['team'], []).append(r)
                team_ops[r['team']].sort(key=lambda x: x['dispatch_min'] or 0)
            # Rebuild stats and summary after rescue
            rcounts = {}
            for u in unassigned: rcounts[u.get('reason','?')] = rcounts.get(u.get('reason','?'),0)+1
            stats.update({
                'assigned': len(flights_assigned),
                'unassigned': len(unassigned),
                'coverage_pct': round(100*len(flights_assigned)/max(N,1),1),
                'unassigned_reasons': rcounts,
                'rescued_by_postpass': len(rescued),
            })
            # Rebuild summary
            summary = []
            for t in sorted(teams, key=lambda x: x.shift_start):
                ops = team_ops.get(t.team_id, [])
                if not ops: continue
                run_count = len(set(o['dispatch_min'] for o in ops))
                summary.append({'team_id':t.team_id,'shift':f"{m2t(t.shift_start)}–{m2t(t.shift_end%1440)}",
                    'shift_start':t.shift_start,'team_type':t.team_type,
                    'ops':run_count,'flight_count':len(ops),'cap':t.cap,
                    'first_std':min(o['std'] for o in ops),'last_std':max(o['std'] for o in ops),
                    'flights':[o['flight'] for o in ops],'operations':ops})

    # ── Per-view statistics ──────────────────────────────────────────────────
    def _view_stats(view_key, filter_fn):
        va = [a for a in assignments if filter_fn(a)]
        vu = [u for u in unassigned  if filter_fn(u)]
        total = len(va) + len(vu)
        return {
            'total': total, 'assigned': len(va), 'unassigned': len(vu),
            'coverage_pct': round(100*len(va)/max(total,1),1) if total else 0,
            'intl_count': sum(1 for a in va if a.get('is_intl')),
            'wb_count':   sum(1 for a in va if a.get('is_wb')),
        }
    stats['view_stats'] = {
        'morning':   _view_stats('morning',   lambda x: x.get('in_morning_view')),
        'afternoon': _view_stats('afternoon', lambda x: x.get('in_afternoon_view')),
        'wb_intl':   _view_stats('wb_intl',   lambda x: x.get('in_wb_intl_view')),
    }

    moved_shift_fit = _shift_fit_rebalance(assignments, team_ops, teams, flights, truck_count)
    if moved_shift_fit:
        summary, stats = build_result_summary_stats(assignments, unassigned, flights, teams, team_ops, status=sn, solve_time=solver.WallTime(), truck_count=truck_count)
        stats['shift_fit_rebalanced'] = moved_shift_fit

    result = {'status':sn,'assignments':assignments,'unassigned':unassigned,
              'team_ops':team_ops,'team_summary':summary,'stats':stats}
    return apply_run_truck_ids(result)


def _greedy_rescue(unassigned: list, teams: list, team_ops: dict,
                   assignments: list, flights_assigned: set) -> list:
    """
    Greedy post-processing pass: try to assign each unassigned flight to any
    feasible on-shift team. Sorted by latest_dispatch ascending (tightest first).
    Respects team no-overlap by tracking current team free times.

    Returns list of new assignment entries that were successfully rescued.
    """
    # Build live team availability from current assignments
    team_free: dict[str, int] = {}  # team_id → minute team is next free
    team_load: dict[str, int] = {}  # team_id → current flight count
    for t in teams:
        team_free[t.team_id] = t.ready_at
        team_load[t.team_id] = len(team_ops.get(t.team_id, []))

    # Update from existing assignments
    for ops_list in team_ops.values():
        for op in ops_list:
            tid = op.get('team')
            if tid and op.get('team_free_min'):
                team_free[tid] = max(team_free.get(tid, 0), op['team_free_min'])

    # Sort unassigned by latest_dispatch ascending (tightest deadline first)
    flight_map = {}
    for u in unassigned:
        flight_map[u['flight']] = u

    # We need Flight objects for timing calculations — rebuild from unassigned dicts
    rescued = []
    seen = set()

    for u in sorted(unassigned, key=lambda x: x['std_min']):
        fn = u['flight']
        if fn in seen: continue

        # Reconstruct timing from entry fields
        std_min    = u['std_min']
        svc        = SVC_TIMES.get(u.get('equip',''), DEFAULT_SVC)
        gate       = u.get('gate','B')
        drv_t      = drv(gate)
        is_intl    = u.get('is_intl', False)
        is_pre     = u.get('is_precleared', False)
        is_st      = u.get('is_short_turn', False)
        is_tight   = u.get('is_tight_turn', False)

        # fin_buf — use what the Flight object would compute
        # precleared is_intl=True but uses domestic buffers
        _buf_key = 'intl' if (is_intl and not is_pre) else 'dom'
        if is_tight:
            fb = FINISH_BUF_TIGHT[_buf_key]
        elif is_st:
            fb = FINISH_BUF_MIN[_buf_key]
        else:
            fb = FINISH_BUF[_buf_key]

        latest_svc_end  = std_min - fb
        latest_dispatch = latest_svc_end - svc - drv_t

        # Sort teams: shift-aligned first (morning teams last resort for PM flights),
        # then by free time ascending within each alignment tier.
        _PM_SPLIT    = 12 * 60   # 12:00 dispatch boundary
        _EARLY_MAX   = 10 * 60   # teams starting < 10:00 = early-start
        _AFTN_MIN    = 12 * 60   # teams starting >= 12:00 = afternoon
        _EARLY_DISP  =  9 * 60   # < this = early-morning flight

        def _align(t, ld):
            if t.shift_start < _EARLY_MAX and ld > _PM_SPLIT:
                return 2
            if t.shift_start >= _AFTN_MIN and ld < _EARLY_DISP:
                return 2
            return 0

        def _late_fit_penalty(t, ld):
            if ld >= 13 * 60:
                if t.shift_start < 10 * 60:
                    return 2
                if t.shift_start < 11 * 60:
                    return 1
            return 0

        candidates = sorted(
            [t for t in teams
             if t.shift_start <= latest_dispatch
             and t.shift_end  >= latest_dispatch + svc + drv_t
             and team_load.get(t.team_id, 0) < t.cap
             and t.team_type != 'FH'],   # FH excluded from regular candidate pool
            key=lambda t: (_align(t, latest_dispatch),
                           _late_fit_penalty(t, latest_dispatch),
                           round(team_load.get(t.team_id, 0) / max(t.cap, 1), 3),
                           team_free.get(t.team_id, t.ready_at))
        )

        # FH last-resort tier: only considered if no regular candidate succeeds.
        fh_candidates = sorted(
            [t for t in teams
             if t.team_type == 'FH'
             and t.shift_start <= latest_dispatch
             and t.shift_end  >= latest_dispatch + svc + drv_t
             and team_load.get(t.team_id, 0) < t.cap],
            key=lambda t: team_free.get(t.team_id, t.ready_at)
        )

        for t in candidates + fh_candidates:
            tf = team_free.get(t.team_id, t.ready_at)
            # Earliest dispatch is when team is free (pre-positioning: no arrival floor on dispatch)
            dispatch = max(tf, latest_dispatch - (latest_dispatch - tf))
            # Actually: use latest_dispatch as target (service as close to deadline as possible)
            # But must be ≥ team_free_at
            dispatch = max(tf, latest_dispatch)
            # Check dispatch is within shift (early-start teams need 30-min return margin)
            return_margin = 30 if t.shift_start < 10*60 else 0
            if dispatch > t.shift_end - return_margin - svc - drv_t: continue
            # Check truck constraint: need DOCK_RELOAD before dispatch
            # Simple check: dispatch is ≥ tf (team available), truck cycle respected via gap
            svc_start  = dispatch + drv_t
            svc_end    = svc_start + svc
            team_free_after = svc_end + drv_t + TEAM_MIN_TURNAROUND

            if svc_end > latest_svc_end: continue  # misses deadline
            if dispatch < tf: continue              # team not free

            # Assign
            dock_load  = dispatch - DOCK_RELOAD
            truck_free = svc_end + drv_t + DOCK_RELOAD
            is_fh_dispatch = t.team_type == 'FH'

            entry = {
                'flight':  fn,
                'dest':    u.get('dest',''),
                'std':     u.get('std',''),
                'std_min': std_min,
                'type':    u.get('type','Domestic'),
                'equip':   u.get('equip',''),
                'gate':    gate,
                'is_ron':  u.get('is_ron', False),
                'is_short_turn':  is_st,
                'is_tight_turn':  is_tight,
                'ground_mins':    u.get('ground_mins', -1),
                'is_intl': is_intl,
                'is_wb':   u.get('is_wb', False),
                'team':    t.team_id,
                'run_size': 1, 'stop_num': 1,
                'dock_load':       m2t(dock_load),
                'dispatch':        m2t(dispatch),
                'svc_start':       m2t(svc_start),
                'svc_end':         m2t(svc_end),
                'team_free_at':    m2t(team_free_after),
                'truck_free_at':   m2t(truck_free),
                'dispatch_min':    dispatch,
                'svc_start_min':   svc_start,
                'svc_end_min':     svc_end,
                'team_free_min':   team_free_after,
                'reason':          None,
                'rescued':         True,        # flag so UI can highlight
                'is_fh_dispatch':  is_fh_dispatch,  # FH last-resort badge
                'nose':            u.get('nose'),
                'pax':             u.get('pax'),
                'inbound_flight':  u.get('inbound_flight'),
                'inbound_origin':  u.get('inbound_origin'),
                'inbound_sta':     u.get('inbound_sta'),
                'ground_time':     u.get('ground_time'),
                **view_tags(std_min, u.get('type','Domestic'), u.get('equip','')),
            }
            rescued.append(entry)
            seen.add(fn)
            # Update team state
            team_free[t.team_id]  = team_free_after
            team_load[t.team_id]  = team_load.get(t.team_id, 0) + 1
            break  # flight assigned, move to next

    return rescued


def _shift_fit_rebalance(assignments: list, team_ops: dict, teams: list, flights: list, truck_count: int = TRUCK_COUNT) -> int:
    """Move late solo runs off early teams onto better-fit later teams when feasible."""
    team_map = {t.team_id: t for t in teams}
    flight_map = {f.flight_num: f for f in flights}
    moved = 0

    def _truck_ok(candidate_entry, exclude_flight):
        start = int(candidate_entry['dock_load_min'])
        end = int(candidate_entry['truck_free_min'])
        for minute in range(start, end):
            active = 0
            for op in assignments:
                if op.get('flight') == exclude_flight:
                    continue
                ws = int(op.get('dock_load_min', max(0, op.get('dispatch_min', 0) - DOCK_RELOAD)))
                we = int(op.get('truck_free_min', op.get('svc_end_min', op.get('dispatch_min', 0)) + drv(op.get('gate', 'B')) + DOCK_RELOAD))
                if ws <= minute < we:
                    active += 1
                    if active >= truck_count:
                        return False
        return True

    # Lower std threshold from 13:00 to 12:30 to catch late-crossover flights on early teams
    candidates = sorted([a for a in assignments if (a.get('run_size',1) == 1 and a.get('std_min',0) >= 12*60+30 and team_map.get(a.get('team')) and team_map[a.get('team')].shift_start < 10*60)], key=lambda a: a.get('std_min',0))
    for a in candidates:
        old_team = team_map.get(a.get('team'))
        f = flight_map.get(a.get('flight'))
        if not old_team or not f:
            continue
        later_teams = sorted([t for t in teams if t.shift_start >= 11*60 and t.team_id != old_team.team_id and len(team_ops.get(t.team_id, [])) < t.cap and t.team_type not in ('OVERNIGHT', 'FH')], key=lambda t: (len(team_ops.get(t.team_id, []))/max(t.cap,1), t.shift_start))
        for t in later_teams:
            dispatch = min(f.latest_dispatch, max(f.earliest_dispatch, max([op.get('team_free_min', t.ready_at) for op in team_ops.get(t.team_id, [])] + [t.ready_at])))
            if dispatch > f.latest_dispatch:
                continue
            if dispatch + f.drv_out + f.svc_time > f.latest_svc_end:
                continue
            if dispatch > t.shift_end - (f.drv_out + f.svc_time + f.drv_back + TEAM_MIN_TURNAROUND):
                continue
            svc_start = dispatch + f.drv_out
            svc_end = svc_start + f.svc_time
            team_free = svc_end + f.drv_back + TEAM_MIN_TURNAROUND
            truck_free = svc_end + f.drv_back + DOCK_RELOAD
            candidate = {**a,
                'team': t.team_id,
                'dispatch': m2t(dispatch), 'dispatch_min': dispatch,
                'dock_load': m2t(max(0, dispatch - DOCK_RELOAD)), 'dock_load_min': max(0, dispatch - DOCK_RELOAD),
                'svc_start': m2t(svc_start), 'svc_start_min': svc_start,
                'svc_end': m2t(svc_end), 'svc_end_min': svc_end,
                'team_free_at': m2t(team_free), 'team_free_min': team_free,
                'truck_free_at': m2t(truck_free), 'truck_free_min': truck_free,
                'shift_fit_rebalanced': True,
            }
            if not _truck_ok(candidate, a.get('flight')):
                continue
            # apply move
            for ops in team_ops.values():
                ops[:] = [candidate if op.get('flight') == a.get('flight') else op for op in ops if not (op.get('flight') == a.get('flight') and op.get('team') == old_team.team_id)]
            team_ops.setdefault(t.team_id, []).append(candidate)
            team_ops[t.team_id].sort(key=lambda x: x.get('dispatch_min',0))
            for idx, op in enumerate(assignments):
                if op.get('flight') == a.get('flight'):
                    assignments[idx] = candidate
                    break
            moved += 1
            break
    # normalize old team lists, removing stray duplicates
    for tid in list(team_ops.keys()):
        dedup = {}
        for op in sorted(team_ops.get(tid, []), key=lambda x: x.get('dispatch_min',0)):
            if op.get('flight') not in dedup:
                dedup[op.get('flight')] = op
        team_ops[tid] = list(dedup.values())
    return moved


def solve(flights: List[Flight], teams: List[Team],
          truck_count: int = TRUCK_COUNT, time_limit: int = 120,
          shift: str = 'combined') -> dict:
    """
    shift: 'morning'  → flights before 13:00, teams starting before 10:00
           'afternoon' → flights 13:00+, teams starting 11:00+
           'combined'  → all flights and teams (default, backward-compatible)
    """
    SHIFT_SPLIT = 13 * 60   # 13:00

    if shift == 'morning':
        flights = [f for f in flights if f.std < SHIFT_SPLIT]
        teams   = [t for t in teams   if t.shift_start < 10 * 60]
    elif shift == 'afternoon':
        flights = [f for f in flights if f.std >= SHIFT_SPLIT]
        teams   = [t for t in teams   if t.shift_start >= 11 * 60]
    # 'combined' → no filtering
    """
    Iterative rebalancing solver.

    Pass 0  — initial solve, no floors beyond hard minimums.
    Pass 1+ — analyse utilisation, compute floor constraints for starved teams,
               re-solve. Accept if coverage maintained AND stddev improves.
               Repeat up to MAX_BALANCE_ITERS times.

    The engine sees its own output and corrects itself. If no rebalancing
    can improve balance without dropping coverage, the best result is returned
    with a 'balance_iterations' count in stats.
    """
    MAX_ITERS     = 3      # max rebalance passes
    STDDEV_TARGET = 0.90   # stop when utilisation stddev < this
    # Time budget: 70% initial, 10% per rebalance pass.
    # Initial solve gets the lion's share — on slower hardware (Render free tier)
    # this ensures the solver reaches OPTIMAL before rebalancing kicks in.
    # With time_limit=240: initial=168s, each rebalance=24s.
    iter_times = [int(time_limit * 0.70)] + [int(time_limit * 0.10)] * MAX_ITERS
    team_map   = {t.team_id: t for t in teams}

    best = _solve_core(flights, teams, truck_count, iter_times[0],
                       floors={}, iteration=0)
    best['stats']['balance_iterations'] = 0

    for iteration in range(1, MAX_ITERS + 1):
        current_stddev = best['stats'].get('util_stddev', 99)
        if current_stddev <= STDDEV_TARGET:
            print(f"  Balance converged at σ={current_stddev} (target {STDDEV_TARGET})")
            break

        floors = _compute_balance_floors(best, teams, flights)
        if not floors:
            print(f"  No floor adjustments needed at iteration {iteration}")
            break

        candidate = _solve_core(flights, teams, truck_count, iter_times[iteration],
                                 floors=floors, iteration=iteration)

        cand_status    = candidate['stats'].get('status', 'UNKNOWN')
        cand_assigned  = candidate['stats'].get('assigned', 0)
        best_assigned  = best['stats'].get('assigned', 0)
        cand_stddev    = candidate['stats'].get('util_stddev', 99)

        # Reject immediately if solver timed out or went infeasible
        if cand_status in ('INFEASIBLE', 'UNKNOWN') or cand_assigned == 0:
            print(f"  ✗ Pass {iteration}: {cand_status} — keeping best")
            # Relaxed retry: loosen constraints by 1 step
            if iteration < MAX_ITERS:
                relaxed = {}
                for k, v in floors.items():
                    if isinstance(v, tuple):
                        f2 = max(2, v[0]-1) if v[0] is not None else None
                        c2 = min(team_map[k].cap, v[1]+1) if v[1] is not None else None
                        relaxed[k] = (f2, c2)
                    else:
                        relaxed[k] = max(1, v-1)
                candidate2 = _solve_core(flights, teams, truck_count, iter_times[iteration],
                                          floors=relaxed, iteration=iteration)
                c2_status   = candidate2['stats'].get('status', 'UNKNOWN')
                c2_assigned = candidate2['stats'].get('assigned', 0)
                c2_stddev   = candidate2['stats'].get('util_stddev', 99)
                if (c2_status in ('OPTIMAL','FEASIBLE') and
                        c2_assigned >= best_assigned - 1 and c2_stddev < current_stddev - 0.02):
                    print(f"    ✓ Relaxed pass accepted: σ {current_stddev:.2f}→{c2_stddev:.2f}")
                    best = candidate2
                    best['stats']['balance_iterations'] = iteration
            break

        # Accept: coverage not worse AND stddev meaningfully better
        if cand_assigned >= best_assigned - 1 and cand_stddev < current_stddev - 0.02:
            print(f"  ✓ Pass {iteration}: σ {current_stddev:.2f}→{cand_stddev:.2f}  "
                  f"coverage {best_assigned}→{cand_assigned}")
            best = candidate
            best['stats']['balance_iterations'] = iteration
            if cand_stddev <= STDDEV_TARGET:
                break
        else:
            print(f"  ✗ Pass {iteration}: σ {cand_stddev:.2f} vs {current_stddev:.2f} "
                  f"(no improvement, keeping best)")
            # Relaxed retry: loosen by 1 step
            if iteration < MAX_ITERS:
                relaxed = {}
                for k, v in floors.items():
                    if isinstance(v, tuple):
                        f2 = max(2, v[0]-1) if v[0] is not None else None
                        c2 = min(team_map[k].cap, v[1]+1) if v[1] is not None else None
                        relaxed[k] = (f2, c2)
                    else:
                        relaxed[k] = max(1, v-1)
                candidate2 = _solve_core(flights, teams, truck_count, iter_times[iteration],
                                          floors=relaxed, iteration=iteration)
                c2_status   = candidate2['stats'].get('status', 'UNKNOWN')
                c2_assigned = candidate2['stats'].get('assigned', 0)
                c2_stddev   = candidate2['stats'].get('util_stddev', 99)
                if (c2_status in ('OPTIMAL','FEASIBLE') and
                        c2_assigned >= best_assigned - 1 and c2_stddev < current_stddev - 0.02):
                    print(f"    ✓ Relaxed pass accepted: σ {current_stddev:.2f}→{c2_stddev:.2f}")
                    best = candidate2
                    best['stats']['balance_iterations'] = iteration
            break

    return best


def _compute_balance_floors(result: dict, teams: List[Team],
                             flights: List[Flight]) -> dict:
    """
    Compute (floor, ceiling) constraints using global PM-pool percentiles.

    Incremental approach: each pass asks for ONE step of improvement, not a
    jump to the median. This prevents INFEASIBLE when too many teams get floors
    simultaneously. The iterative loop in solve() handles convergence.

    Returns {team_id: (floor, ceiling)} — either value may be None.
    """
    import statistics as _s
    team_ops = result.get('team_ops', {})
    team_map = {t.team_id: t for t in teams}

    pm_loads: dict = {}
    for t in teams:
        if t.team_type not in ('FT', 'PT'): continue  # excludes FH, OVERNIGHT
        if t.shift_start < 12 * 60: continue
        pm_loads[t.team_id] = len(team_ops.get(t.team_id, []))

    if len(pm_loads) < 4:
        return {}

    # ── Early-team ceiling: cap early teams that are holding PM-eligible work ──
    # If an early team (shift_start < 10:00) has any assigned flights with
    # STD >= 12:30 AND a later team is under-utilised, impose a ceiling of
    # (current_load - late_flights) so those flights are freed for PM teams.
    # This runs alongside the PM-pool balancer, not instead of it.
    _early_ceilings: dict = {}
    _pm_avg = sum(pm_loads.values()) / max(len(pm_loads), 1)
    _pm_has_room = any(load < team_map[tid].cap - 1 for tid, load in pm_loads.items())
    if _pm_has_room:
        for t in teams:
            if t.team_type not in ('FT', 'PT'): continue
            if t.shift_start >= 10 * 60: continue   # only early teams
            ops = team_ops.get(t.team_id, [])
            # Count flights with STD >= 12:30 on this early team
            late_on_early = sum(1 for op in ops if op.get('std_min', 0) >= 12 * 60 + 30)
            if late_on_early > 0:
                # Ceiling = current load minus late flights (free those slots for PM teams)
                new_ceil = len(ops) - late_on_early
                new_ceil = max(new_ceil, 3)   # never cap below 3 (preserve morning coverage)
                new_ceil = min(new_ceil, t.cap)
                if new_ceil < len(ops):
                    _early_ceilings[t.team_id] = new_ceil

    vals   = sorted(pm_loads.values())
    median = _s.median(vals)
    q1     = _s.quantiles(vals, n=4)[0]
    q3     = _s.quantiles(vals, n=4)[2]
    std    = _s.stdev(vals) if len(vals) > 1 else 1.0

    if std < 0.6:
        return {}   # already balanced enough

    # Reachable flight count per PM team (caps feasible floor)
    runs_cache = build_runs(flights)
    reachable: dict = {}
    for t in teams:
        if t.team_id not in pm_loads: continue
        cnt = 0
        for r in runs_cache:
            if all(t.ready_at <= f.latest_dispatch and
                   t.shift_end >= f.latest_dispatch + f.svc_time + f.drv_out
                   for f in r.flights):
                cnt += len(r.flights)
            if cnt >= t.cap: break
        reachable[t.team_id] = min(cnt, t.cap)

    constraints: dict = {}
    # Inject early-team ceilings computed above
    for tid, ceil in _early_ceilings.items():
        constraints[tid] = (None, ceil)
    for tid, load in pm_loads.items():
        t   = team_map[tid]
        rch = reachable.get(tid, 0)
        floor = ceiling = None

        # Underloaded: below Q1, can take more
        # Floor = load + 1 (incremental — one step up, not a jump to median)
        # Capped by: reachable, cap-1, and Q3 (don't overshoot the upper quartile)
        if load < q1 and rch > load:
            floor = min(load + 1, int(q3), t.cap - 1, rch)
            if floor <= load: floor = None

        # Overloaded: at or above Q3 AND significantly above median
        # Ceiling = load - 1 (one step down — incremental, not a jump to median)
        # But never below max(2, int(median)-1) to avoid over-constraining
        if load >= q3 and load > median + 1.0:
            ceiling = load - 1
            ceiling = max(ceiling, max(2, int(median) - 1))
            ceiling = min(ceiling, t.cap)
            if ceiling >= load: ceiling = None

        if floor is not None or ceiling is not None:
            constraints[tid] = (floor, ceiling)

    if constraints:
        summary = {k: f"\u2265{v[0] or '-'}/\u2264{v[1] or '-'}" for k,v in constraints.items()}
        print(f"  Balance constraints: {summary}  "
              f"(PM median={median:.1f} Q1={q1:.1f} Q3={q3:.1f} \u03c3={std:.2f})")
    return constraints


def solve_partial(flights, teams, time_limit=60):
    """
    Lightweight solver for sick call re-assignment.
    Solves a small sub-set of flights WITHOUT the FT/PT minimum constraints
    that apply to the full schedule. Just maximises coverage.
    """
    from ortools.sat.python import cp_model as _cp
    model  = _cp.CpModel()
    solver = _cp.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit
    solver.parameters.num_search_workers  = 8
    N, M = len(flights), len(teams)
    HORIZON = 30*60

    runs = build_runs(flights)
    R = len(runs)
    flight_index = {f.flight_num: i for i, f in enumerate(flights)}

    # Feasibility
    run_active = {}
    for r_idx, run in enumerate(runs):
        for j, t in enumerate(teams):
            if t.team_type == 'OVERNIGHT':
                if any(not f.is_ron for f in run.flights): continue
                dlb = t.ready_at
                dub = min(run.latest_feasible_dispatch() + 1440, t.shift_end - run.team_busy(), OVERNIGHT_LATEST_FREE - run.team_busy() + 1440)
            else:
                dlb = t.ready_at
                dub = min(run.latest_feasible_dispatch(), t.shift_end - run.team_busy())
            if dlb > dub: continue
            run_active[(r_idx,j)] = model.NewBoolVar(f'ra{r_idx}_{j}')

    # Each flight covered at most once
    flight_covered = [[] for _ in range(N)]
    for r_idx, run in enumerate(runs):
        for f in run.flights:
            fi = flight_index.get(f.flight_num)
            if fi is None: continue
            for j in range(M):
                if (r_idx,j) in run_active:
                    flight_covered[fi].append(run_active[(r_idx,j)])
    for i in range(N):
        if flight_covered[i]:
            model.Add(sum(flight_covered[i]) <= 1)

    # Team capacity (flight-based)
    for j, t in enumerate(teams):
        costs = [run_active[(r_idx,j)] * len(runs[r_idx].flights)
                 for r_idx in range(R) if (r_idx,j) in run_active]
        if costs:
            model.Add(sum(costs) <= t.cap)

    # Interval no-overlap (team + truck)
    team_ivs  = {j:[] for j in range(M)}
    truck_ivs, truck_dmds = [], []
    dispatch_v = {}

    for r_idx, run in enumerate(runs):
        for j, t in enumerate(teams):
            if (r_idx,j) not in run_active: continue
            p = run_active[(r_idx,j)]
            if t.team_type == 'OVERNIGHT':
                dlb = t.ready_at
                dub = min(run.latest_feasible_dispatch() + 1440, t.shift_end - run.team_busy(), OVERNIGHT_LATEST_FREE - run.team_busy() + 1440)
            else:
                dlb = t.ready_at
                dub = min(run.latest_feasible_dispatch(), t.shift_end - run.team_busy())
            if dlb > dub: continue
            tb, trb = run.team_busy(), run.truck_busy()
            sv = model.NewIntVar(dlb, dub, f'sv{r_idx}_{j}')
            te = model.NewIntVar(dlb+tb, dub+tb, f'te{r_idx}_{j}')
            tiv = model.NewOptionalIntervalVar(sv, tb, te, p, f'tiv{r_idx}_{j}')
            team_ivs[j].append(tiv)
            dispatch_v[(r_idx,j)] = sv
            run_offset = runs[r_idx].flights[0].drv_out
            for k, f in enumerate(runs[r_idx].flights):
                if k > 0: run_offset += g2g(runs[r_idx].flights[k-1].gate, f.gate)
                run_offset += f.svc_time
                se2 = model.NewIntVar(dlb+run_offset, HORIZON, f'se{r_idx}_{j}_{k}')
                model.Add(se2 == sv + run_offset).OnlyEnforceIf(p)
                deadline = f.latest_svc_end + (1440 if t.team_type == 'OVERNIGHT' else 0)
                model.Add(se2 <= deadline).OnlyEnforceIf(p)
            trs_lb = max(0, dlb - DOCK_RELOAD)
            trs = model.NewIntVar(trs_lb, max(trs_lb,dub), f'trs{r_idx}_{j}')
            model.Add(trs == sv - DOCK_RELOAD).OnlyEnforceIf(p)
            tre = model.NewIntVar(trs_lb+trb, dub+trb, f'tre{r_idx}_{j}')
            triv = model.NewOptionalIntervalVar(trs, trb, tre, p, f'triv{r_idx}_{j}')
            truck_ivs.append(triv); truck_dmds.append(1)

    for j in range(M):
        if len(team_ivs[j]) > 1: model.AddNoOverlap(team_ivs[j])
    if truck_ivs:
        model.AddCumulative(truck_ivs, truck_dmds, TRUCK_COUNT)

    # Objective: maximise coverage only (no minimum constraints)
    total_flights_vars = []
    for i in range(N):
        if flight_covered[i]:
            cv = model.NewBoolVar(f'cov{i}')
            model.Add(sum(flight_covered[i]) >= 1).OnlyEnforceIf(cv)
            model.Add(sum(flight_covered[i]) == 0).OnlyEnforceIf(cv.Not())
            total_flights_vars.append(cv)
    model.Maximize(sum(total_flights_vars) * 1000)

    sc = solver.Solve(model)
    sn = {_cp.OPTIMAL:'OPTIMAL', _cp.FEASIBLE:'FEASIBLE',
          _cp.INFEASIBLE:'INFEASIBLE', _cp.UNKNOWN:'UNKNOWN'}.get(sc, 'UNKNOWN')

    if sc not in (_cp.OPTIMAL, _cp.FEASIBLE):
        return {'status':sn,'assignments':[],'unassigned':list({'flight':f.flight_num,
            'dest':f.dest,'std':m2t(f.std),'std_min':f.std,'type':f.dep_type,
            'equip':f.equip,'gate':f.gate,'is_ron':f.is_ron,'is_intl':f.is_intl,
            'is_wb':f.is_wb,'team':None,'reason':'NO_TEAM_AVAILABLE'} for f in flights),
            'stats':{'status':sn,'assigned':0,'total':N,'coverage_pct':0.0,
                     'solve_time':round(solver.WallTime(),1)}}

    assignments=[]; unassigned=[]; team_ops={t.team_id:[] for t in teams}
    assigned_set = set()
    for r_idx, run in enumerate(runs):
        for j, t in enumerate(teams):
            if (r_idx,j) not in run_active: continue
            if solver.Value(run_active[(r_idx,j)]) != 1: continue
            disp = solver.Value(dispatch_v[(r_idx,j)]) if (r_idx,j) in dispatch_v else t.ready_at
            off = run.flights[0].drv_out
            for k, f in enumerate(run.flights):
                if k > 0:
                    off += g2g(run.flights[k-1].gate, f.gate)
                ss = disp + off
                off += f.svc_time
                se_ = disp + off
                tf = se_ + f.drv_back + TEAM_MIN_TURNAROUND
                dock_load = disp - DOCK_RELOAD
                truck_free = se_ + f.drv_back + DOCK_RELOAD
                entry = {
                    'flight':f.flight_num,'dest':f.dest,'std':m2t(f.std),
                    'std_min':f.std,'type':f.dep_type,'equip':f.equip,'gate':f.gate,
                    'is_ron':f.is_ron,'is_short_turn':f.is_short_turn,'is_tight_turn':f.is_tight_turn,
                    'ground_mins':f.ground_mins,'is_intl':f.is_intl,'is_wb':f.is_wb,
                    'team':t.team_id,'run_size':len(run.flights),'stop_num':k+1,
                    'dock_load':m2t(dock_load),'dock_load_min':dock_load,
                    'dispatch':m2t(disp),
                    'svc_start':m2t(ss),'svc_end':m2t(se_),
                    'team_free_at':m2t(tf),
                    'truck_free_at':m2t(truck_free),'truck_free_min':truck_free,
                    'dispatch_min':disp,'svc_start_min':ss,'svc_end_min':se_,
                    'team_free_min':tf,'reason':None
                }
                assignments.append(entry)
                team_ops[t.team_id].append(entry)
                assigned_set.add(f.flight_num)

    for f in flights:
        if f.flight_num not in assigned_set:
            unassigned.append({'flight':f.flight_num,'dest':f.dest,'std':m2t(f.std),
                'std_min':f.std,'type':f.dep_type,'equip':f.equip,'gate':f.gate,
                'is_ron':f.is_ron,'is_short_turn':f.is_short_turn,'is_tight_turn':f.is_tight_turn,
                'ground_mins':f.ground_mins,'is_intl':f.is_intl,'is_wb':f.is_wb,
                'team':None,'reason':'NO_TRUCK_AVAILABLE'})

    for tid in team_ops:
        team_ops[tid].sort(key=lambda x: x.get('dispatch_min', 0) or 0)

    partial_result = {'status':sn,'assignments':assignments,'unassigned':unassigned,
            'team_ops':team_ops,
            'stats':{'status':sn,'assigned':len(assigned_set),'total':N,
                     'coverage_pct':round(100*len(assigned_set)/max(N,1),1),
                     'solve_time':round(solver.WallTime(),1)}}
    return apply_run_truck_ids(partial_result)
