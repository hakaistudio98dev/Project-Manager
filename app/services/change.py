"""Change Request impact analysis & penerapan ke baseline (PRD §12.1, §19, §32).

Alur: CR → Scope Impact → MD Impact → Cost Impact → Schedule Impact → Margin Impact
      → Approval → Baseline Update (versi baru; versi lama superseded)
"""
from datetime import date, timedelta
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (WBS, Baseline, BaselineItem, BaselineStatus, ChangeRequest, CRLine,
                      CRLineType, DocStatus, ProgressMethod, Project, ProjectMember, Task)
from . import costing, evm


def approved_cr_revenue(db: Session, project_id: int) -> Decimal:
    return Decimal(db.scalar(select(func.coalesce(func.sum(ChangeRequest.revenue_delta), 0)).where(
        ChangeRequest.project_id == project_id, ChangeRequest.status == DocStatus.APPROVED)) or 0)


def _margin(revenue: Decimal, cost: Decimal) -> Decimal | None:
    return evm.idx((revenue - cost) / revenue) if revenue > 0 else None


def analyze(db: Session, project: Project, bl: Baseline, lines_in, schedule_days: int,
            revenue_delta: Decimal, today: date) -> tuple[list[CRLine], dict]:
    item_rate = {i.task_id: i.rate for i in bl.items if i.task_id}
    lines: list[CRLine] = []
    for li in lines_in:
        if li.line_type == CRLineType.ADD_TASK:
            w = db.get(WBS, li.wbs_id)
            if not w or w.project_id != project.id:
                raise HTTPException(422, f"wbs_id {li.wbs_id} tidak valid")
            if not db.scalar(select(ProjectMember).where(ProjectMember.project_id == project.id,
                                                         ProjectMember.employee_id == li.employee_id)):
                raise HTTPException(422, "Employee pada add_task harus member project")
            start = li.planned_start or max(today, bl.start_date)
            rate = costing.rate_effective_on(db, li.employee_id, start)
            if not rate:
                raise HTTPException(422, f"Employee #{li.employee_id} belum punya cost rate per {start}")
            lines.append(CRLine(line_type=li.line_type, wbs_id=w.id, name=li.name, employee_id=li.employee_id,
                                md_delta=li.md_delta, planned_start=start, planned_end=li.planned_end,
                                rate=rate.cost_per_md, cost=costing.labor_cost(li.md_delta, rate.cost_per_md)))
        elif li.line_type == CRLineType.CHANGE_TASK:
            t = db.get(Task, li.task_id)
            if not t or t.project_id != project.id:
                raise HTTPException(422, f"task_id {li.task_id} tidak valid")
            if t.estimate_md + li.md_delta < 0:
                raise HTTPException(422, f"Estimasi MD task #{t.id} tidak boleh negatif")
            r = item_rate.get(t.id)
            if r is None and t.owner_employee_id:
                rr = costing.rate_effective_on(db, t.owner_employee_id, max(today, t.planned_start or today))
                r = rr.cost_per_md if rr else None
            if r is None:
                raise HTTPException(422, f"Task #{t.id} tidak punya rate (owner/baseline)")
            lines.append(CRLine(line_type=li.line_type, task_id=t.id, wbs_id=t.wbs_id, name=t.name,
                                employee_id=t.owner_employee_id, md_delta=li.md_delta, rate=r,
                                cost=costing.labor_cost(li.md_delta, r)))
        else:
            if li.wbs_id:
                w = db.get(WBS, li.wbs_id)
                if not w or w.project_id != project.id:
                    raise HTTPException(422, f"wbs_id {li.wbs_id} tidak valid")
            lines.append(CRLine(line_type=li.line_type, wbs_id=li.wbs_id, category=li.category.value,
                                amount=costing.money(li.amount), name=li.name,
                                cost=costing.money(li.amount)))

    md_impact = sum((l.md_delta or 0 for l in lines if l.line_type != CRLineType.EXPENSE), Decimal(0))
    cost_impact = sum((l.cost for l in lines), Decimal(0))
    revenue_before = project.contract_value + approved_cr_revenue(db, project.id)
    revenue_after = revenue_before + revenue_delta
    bac_after = bl.bac + cost_impact
    end_after = bl.end_date + timedelta(days=schedule_days)

    cur = evm.compute(db, project, today)
    cpi = cur.cpi if cur else None
    eac_before = cur.selected["eac"] if cur else bl.bac
    eac_after = costing.money(bac_after / cpi) if cpi else bac_after
    margin_after = _margin(revenue_after, bac_after)
    fmargin_after = _margin(revenue_after, eac_after)
    target = project.target_margin
    impact = {
        "scope": [{"type": l.line_type.value, "name": l.name, "md": l.md_delta, "cost": l.cost} for l in lines],
        "md_impact": md_impact,
        "cost_impact": costing.money(cost_impact),
        "schedule": {"days": schedule_days, "end_date_before": bl.end_date.isoformat(),
                     "end_date_after": end_after.isoformat()},
        "baseline": {"bac_before": bl.bac, "bac_after": costing.money(bac_after)},
        "revenue": {"before": costing.money(revenue_before), "after": costing.money(revenue_after),
                    "delta": costing.money(revenue_delta)},
        "margin": {"planned_before": _margin(revenue_before, bl.bac), "planned_after": margin_after,
                   "forecast_before": _margin(revenue_before, eac_before),
                   "forecast_after": fmargin_after, "target": target,
                   "basis": "Margin terhadap cost baseline / EAC (overhead dialokasikan di Tahap 3)"},
        "forecast": {"current_cpi": cpi, "eac_before": eac_before, "eac_after": eac_after,
                     "method": "EAC = BAC baru / CPI saat ini" if cpi else "Belum ada CPI; EAC = BAC"},
        "flags": [],
    }
    if target is not None and fmargin_after is not None and fmargin_after < target:
        impact["flags"].append(f"Forecast margin setelah CR ({fmargin_after:.2%}) di bawah target ({target:.2%})")
    if cost_impact > 0 and revenue_delta <= 0:
        impact["flags"].append("Tambahan biaya tanpa tambahan revenue — margin tergerus")
    return lines, impact


def apply(db: Session, project: Project, bl: Baseline, cr: ChangeRequest, user_id: int, today: date) -> Baseline:
    """Buat baseline versi baru dari baseline aktif + dampak CR. Baseline lama → superseded."""
    new_end = bl.end_date + timedelta(days=cr.schedule_impact_days)
    nb = Baseline(project_id=project.id, version=bl.version + 1, budget_id=bl.budget_id,
                  change_request_id=cr.id, bac=bl.bac + cr.cost_impact, start_date=bl.start_date,
                  end_date=new_end, reason=f"{cr.code}: {cr.title}", created_by_id=user_id)
    for i in bl.items:
        nb.items.append(BaselineItem(wbs_id=i.wbs_id, task_id=i.task_id, employee_id=i.employee_id,
                                     category=i.category, description=i.description,
                                     planned_start=i.planned_start, planned_end=i.planned_end,
                                     md=i.md, rate=i.rate, amount=i.amount, progress_method=i.progress_method))
    by_task = {i.task_id: i for i in nb.items if i.task_id}
    for l in cr.lines:
        if l.line_type == CRLineType.ADD_TASK:
            w = db.get(WBS, l.wbs_id)
            end = l.planned_end or new_end
            t = Task(project_id=project.id, wbs_id=w.id, name=l.name, owner_employee_id=l.employee_id,
                     planned_start=l.planned_start, planned_end=end, estimate_md=l.md_delta,
                     acceptance_criteria=f"Ditambahkan melalui {cr.code}")
            db.add(t)
            db.flush()
            l.task_id = t.id
            nb.items.append(BaselineItem(wbs_id=w.id, task_id=t.id, employee_id=l.employee_id, category="labor",
                                         description=l.name, planned_start=l.planned_start, planned_end=end,
                                         md=l.md_delta, rate=l.rate, amount=l.cost,
                                         progress_method=w.progress_method))
        elif l.line_type == CRLineType.CHANGE_TASK:
            t = db.get(Task, l.task_id)
            t.estimate_md += l.md_delta
            it = by_task.get(t.id)
            if it:
                it.md = (it.md or 0) + l.md_delta
                it.amount += l.cost
                if it.amount < 0:
                    raise HTTPException(422, f"Nilai baseline task #{t.id} menjadi negatif")
                if cr.schedule_impact_days and it.planned_end >= bl.end_date:
                    it.planned_end = new_end
            else:
                w = db.get(WBS, t.wbs_id)
                nb.items.append(BaselineItem(wbs_id=t.wbs_id, task_id=t.id, employee_id=t.owner_employee_id,
                                             category="labor", description=t.name,
                                             planned_start=t.planned_start or max(today, bl.start_date),
                                             planned_end=t.planned_end or new_end, md=l.md_delta, rate=l.rate,
                                             amount=l.cost, progress_method=w.progress_method))
        else:
            start = min(max(today, bl.start_date), new_end)
            nb.items.append(BaselineItem(wbs_id=l.wbs_id, category=l.category, description=l.name or cr.title,
                                         planned_start=start, planned_end=new_end, amount=l.cost,
                                         progress_method=ProgressMethod.PHYSICAL))
    bl.status = BaselineStatus.SUPERSEDED
    db.flush()  # lepaskan status active lama sebelum yang baru ditambah
    db.add(nb)
    if cr.schedule_impact_days:
        project.end_date = new_end
    db.flush()
    return nb
