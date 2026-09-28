"""EVM Engine (PRD §15) & Forecasting (PRD §16).

Definisi yang dipakai (mengikuti PRD):
  BAC = total approved baseline (cost baseline, termasuk contingency)
  PV  = Planned % × BAC      Planned % = Σ(item.amount × planned_fraction) / Σ item.amount
  EV  = Earned %  × BAC      Earned %  = Σ(item.amount × earned_fraction)  / Σ item.amount
  AC  = labor timesheet approved (work_date ≤ as_of) + expense posted (expense_date ≤ as_of)

Planned fraction item: linear per hari kalender antara planned_start dan planned_end.
Earned fraction task mengikuti progress method yang DIBEKUKAN di baseline:
  0/100 & milestone_weighting → 0 atau 1 (task done)
  50/50                        → 0 / 0.5 (sudah mulai) / 1 (done)
  physical_pct & weighted_deliverable → progress_pct / 100
Item expense: earned mengikuti progress WBS-nya (tertimbang MD), atau progress project.
"""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (WBS, Baseline, BaselineItem, BaselineStatus, DocStatus, Expense,
                      ProgressMethod, Project, Task, TaskStatus, Timesheet)
from . import costing

IDX = Decimal("0.0001")
METHODS = {
    "cpi": "EAC = BAC / CPI",
    "cpi_spi": "EAC = AC + (BAC − EV) / (CPI × SPI)",
    "budget_rate": "EAC = AC + (BAC − EV)",
    "bottom_up": "EAC = AC + ETC bottom-up (sisa MD × rate + sisa expense)",
}


def idx(x) -> Decimal | None:
    return None if x is None else Decimal(x).quantize(IDX, ROUND_HALF_UP)


def safe_div(a, b):
    return (Decimal(a) / Decimal(b)) if b else None


# ---------------- Fungsi murni ----------------
def planned_fraction(start: date, end: date, as_of: date) -> Decimal:
    if as_of < start:
        return Decimal(0)
    if as_of >= end:
        return Decimal(1)
    return Decimal((as_of - start).days + 1) / Decimal((end - start).days + 1)


def earned_fraction(method: ProgressMethod, status: TaskStatus, progress_pct: Decimal) -> Decimal:
    done = status == TaskStatus.DONE or progress_pct >= 100
    if method in (ProgressMethod.ZERO_HUNDRED, ProgressMethod.MILESTONE):
        return Decimal(1 if done else 0)
    if method == ProgressMethod.FIFTY_FIFTY:
        if done:
            return Decimal(1)
        started = status in (TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED) or progress_pct > 0
        return Decimal("0.5") if started else Decimal(0)
    return min(Decimal(progress_pct) / 100, Decimal(1))


def core_metrics(bac, pv, ev, ac) -> dict:
    return {"bac": bac, "pv": pv, "ev": ev, "ac": ac, "sv": ev - pv, "cv": ev - ac,
            "spi": safe_div(ev, pv), "cpi": safe_div(ev, ac)}


def forecast(bac, ev, ac, cpi, spi, bottom_up_etc=None) -> dict:
    """Semua metode EAC sekaligus. Mengembalikan dict method → {eac, etc, vac, tcpi_eac, note}."""
    remaining_work = bac - ev
    out = {}

    def pack(eac, note=None):
        eac = costing.money(eac)
        etc = eac - ac
        return {"eac": eac, "etc": costing.money(etc), "vac": costing.money(bac - eac),
                "tcpi_eac": idx(safe_div(remaining_work, etc)) if etc > 0 else None,
                "formula": None, "note": note}

    if cpi is None:
        out["cpi"] = pack(bac, "Belum ada AC; EAC = BAC")
    elif cpi == 0:
        out["cpi"] = pack(ac + remaining_work, "CPI = 0; fallback ke budget rate")
    else:
        out["cpi"] = pack(bac / cpi)
    if cpi and spi:
        out["cpi_spi"] = pack(ac + remaining_work / (cpi * spi))
    else:
        out["cpi_spi"] = pack(ac + remaining_work, "CPI/SPI belum tersedia; fallback ke budget rate")
    out["budget_rate"] = pack(ac + remaining_work)
    if bottom_up_etc is not None:
        out["bottom_up"] = pack(ac + bottom_up_etc)
    for k, v in out.items():
        v["formula"] = METHODS[k]
    return out


def health(cpi, spi) -> str:
    vals = [v for v in (cpi, spi) if v is not None]
    if not vals:
        return "grey"
    worst = min(vals)
    return "green" if worst >= Decimal("0.95") else "amber" if worst >= Decimal("0.90") else "red"


# ---------------- Perhitungan dari database ----------------
def active_baseline(db: Session, project_id: int) -> Baseline | None:
    return db.scalar(select(Baseline).where(Baseline.project_id == project_id,
                                            Baseline.status == BaselineStatus.ACTIVE))


@dataclass
class EVMResult:
    project_id: int
    baseline_id: int
    baseline_version: int
    as_of: date
    bac: Decimal
    pv: Decimal
    ev: Decimal
    ac: Decimal
    sv: Decimal
    cv: Decimal
    spi: Decimal | None
    cpi: Decimal | None
    planned_pct: Decimal
    earned_pct: Decimal
    tcpi: Decimal | None
    forecasts: dict
    method: str
    health: str
    by_wbs: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    @property
    def selected(self) -> dict:
        return self.forecasts[self.method]

    def as_dict(self) -> dict:
        s = self.selected
        return {"project_id": self.project_id, "baseline_id": self.baseline_id,
                "baseline_version": self.baseline_version, "as_of": self.as_of.isoformat(),
                "bac": self.bac, "pv": self.pv, "ev": self.ev, "ac": self.ac, "sv": self.sv, "cv": self.cv,
                "spi": self.spi, "cpi": self.cpi, "planned_pct": self.planned_pct,
                "earned_pct": self.earned_pct, "tcpi": self.tcpi, "eac_method": self.method,
                "eac": s["eac"], "etc": s["etc"], "vac": s["vac"], "health": self.health,
                "forecasts": self.forecasts, "by_wbs": self.by_wbs, "warnings": self.warnings}


def _wbs_progress(tasks: list[Task]) -> dict[int | None, Decimal]:
    md, earned = defaultdict(Decimal), defaultdict(Decimal)
    for t in tasks:
        md[t.wbs_id] += t.estimate_md
        earned[t.wbs_id] += t.estimate_md * min(t.progress_pct, Decimal(100)) / 100
        md[None] += t.estimate_md
        earned[None] += t.estimate_md * min(t.progress_pct, Decimal(100)) / 100
    return {k: (earned[k] / md[k] if md[k] else Decimal(0)) for k in md}


def compute(db: Session, project: Project, as_of: date, method: str = "cpi") -> EVMResult | None:
    if method not in METHODS:
        raise ValueError(f"Metode forecast tidak dikenal: {method}")
    bl = active_baseline(db, project.id)
    if not bl:
        return None
    tasks = db.scalars(select(Task).where(Task.project_id == project.id)).all()
    task_by_id = {t.id: t for t in tasks}
    wbs_prog = _wbs_progress(tasks)
    total_items = sum((i.amount for i in bl.items), Decimal(0))
    # BAC: default PRD = cost baseline (termasuk contingency, didistribusi proporsional).
    # Opsi: contingency dipegang terpisah → BAC = Σ item work package (PMB).
    from .pricing import settings_for
    include_cont = settings_for(db, project.org_id, create=False).evm_bac_includes_contingency
    bac_eff = bl.bac if include_cont else total_items
    scale = (bac_eff / total_items) if total_items else Decimal(0)
    warnings = []

    pv_w, ev_w, bac_w = defaultdict(Decimal), defaultdict(Decimal), defaultdict(Decimal)
    planned_sum = earned_sum = Decimal(0)
    for it in bl.items:
        pf = planned_fraction(it.planned_start, it.planned_end, as_of)
        if it.task_id:
            t = task_by_id.get(it.task_id)
            ef = earned_fraction(it.progress_method, t.status, t.progress_pct) if t else Decimal(0)
        else:
            ef = wbs_prog.get(it.wbs_id, wbs_prog.get(None, Decimal(0)))
        planned_sum += it.amount * pf
        earned_sum += it.amount * ef
        bac_w[it.wbs_id] += it.amount * scale
        pv_w[it.wbs_id] += it.amount * pf * scale
        ev_w[it.wbs_id] += it.amount * ef * scale

    planned_pct = planned_sum / total_items if total_items else Decimal(0)
    earned_pct = earned_sum / total_items if total_items else Decimal(0)

    ac_w = defaultdict(Decimal)
    for wid, amt in db.execute(select(Timesheet.wbs_id, func.sum(Timesheet.labor_cost))
                               .where(Timesheet.project_id == project.id,
                                      Timesheet.status == DocStatus.APPROVED,
                                      Timesheet.work_date <= as_of).group_by(Timesheet.wbs_id)).all():
        ac_w[wid] += Decimal(amt or 0)
    for wid, amt in db.execute(select(Expense.wbs_id, func.sum(Expense.amount))
                               .where(Expense.project_id == project.id, Expense.status == DocStatus.POSTED,
                                      Expense.expense_date <= as_of).group_by(Expense.wbs_id)).all():
        ac_w[wid] += Decimal(amt or 0)

    bac = costing.money(bac_eff)
    pv = costing.money(planned_pct * bac)
    ev = costing.money(earned_pct * bac)
    ac = costing.money(sum(ac_w.values(), Decimal(0)))
    m = core_metrics(bac, pv, ev, ac)

    # Bottom-up ETC: sisa MD task saat ini × rate + sisa expense baseline
    etc_bu = Decimal(0)
    item_rate = {i.task_id: i.rate for i in bl.items if i.task_id}
    for t in tasks:
        remaining_md = t.estimate_md * (1 - min(t.progress_pct, Decimal(100)) / 100)
        if remaining_md <= 0:
            continue
        rate = None
        if t.owner_employee_id:
            r = costing.rate_effective_on(db, t.owner_employee_id, max(as_of, t.planned_start or as_of))
            rate = r.cost_per_md if r else None
        rate = rate or item_rate.get(t.id)
        if rate is None:
            warnings.append(f"Task #{t.id} '{t.name}' tanpa rate; tidak masuk ETC bottom-up")
            continue
        etc_bu += remaining_md * rate
    for it in bl.items:
        if not it.task_id:
            ef = wbs_prog.get(it.wbs_id, wbs_prog.get(None, Decimal(0)))
            etc_bu += it.amount * (1 - ef)

    fc = forecast(bac, ev, ac, m["cpi"], m["spi"], costing.money(etc_bu))
    tcpi = safe_div(bac - ev, bac - ac) if bac - ac > 0 else None

    # Breakdown per WBS dengan roll-up ke parent
    nodes = {w.id: w for w in db.scalars(select(WBS).where(WBS.project_id == project.id))}
    agg = defaultdict(lambda: defaultdict(Decimal))
    for src, key in ((bac_w, "bac"), (pv_w, "pv"), (ev_w, "ev"), (ac_w, "ac")):
        for wid, val in src.items():
            cur = wid
            while cur is not None and cur in nodes:
                agg[cur][key] += val
                cur = nodes[cur].parent_id
            if wid is None or wid not in nodes:
                agg[None][key] += val
    by_wbs = []
    for wid, v in agg.items():
        w = nodes.get(wid)
        b, p, e, a = (costing.money(v[k]) for k in ("bac", "pv", "ev", "ac"))
        cpi_w, spi_w = idx(safe_div(e, a)), idx(safe_div(e, p))
        by_wbs.append({"wbs_id": wid, "code": w.code if w else None,
                       "name": w.name if w else "(tanpa WBS)",
                       "parent_id": w.parent_id if w else None, "bac": b, "pv": p, "ev": e, "ac": a,
                       "sv": e - p, "cv": e - a, "spi": spi_w, "cpi": cpi_w, "health": health(cpi_w, spi_w)})
    by_wbs.sort(key=lambda r: [x.zfill(6) for x in r["code"].split(".")] if r["code"] else ["~"])

    cpi_q, spi_q = idx(m["cpi"]), idx(m["spi"])
    return EVMResult(project_id=project.id, baseline_id=bl.id, baseline_version=bl.version, as_of=as_of,
                     bac=bac, pv=pv, ev=ev, ac=ac, sv=m["sv"], cv=m["cv"], spi=spi_q, cpi=cpi_q,
                     planned_pct=idx(planned_pct), earned_pct=idx(earned_pct), tcpi=idx(tcpi),
                     forecasts=fc, method=method, health=health(cpi_q, spi_q), by_wbs=by_wbs,
                     warnings=warnings)


def pv_at(bl: Baseline, as_of: date, bac: Decimal | None = None) -> Decimal:
    total = sum((i.amount for i in bl.items), Decimal(0))
    if not total:
        return Decimal(0)
    planned = sum((i.amount * planned_fraction(i.planned_start, i.planned_end, as_of) for i in bl.items),
                  Decimal(0))
    return costing.money(planned / total * (bac if bac is not None else bl.bac))


def ac_at(db: Session, project_id: int, as_of: date) -> Decimal:
    lab = db.scalar(select(func.coalesce(func.sum(Timesheet.labor_cost), 0)).where(
        Timesheet.project_id == project_id, Timesheet.status == DocStatus.APPROVED,
        Timesheet.work_date <= as_of))
    exp = db.scalar(select(func.coalesce(func.sum(Expense.amount), 0)).where(
        Expense.project_id == project_id, Expense.status == DocStatus.POSTED, Expense.expense_date <= as_of))
    return costing.money(Decimal(lab or 0) + Decimal(exp or 0))
