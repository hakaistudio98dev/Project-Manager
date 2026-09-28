"""Dashboard (PRD §22) + alert live berbasis aturan PRD §20.

Alert di sini dihitung saat dashboard dibuka. Engine automation terjadwal
(notifikasi email/WA, rule builder) dibangun di Tahap 4 (V1.5).
"""
from datetime import date, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import (ChangeRequest, DocStatus, Employee, Project, ProjectMember, ProjectStatus, Risk,
                      RiskStatus, Role, Task, TaskStatus, User)
from ..security import employee_of, get_current_user
from ..models import Invoice, ProcStatus, ProcurementItem
from ..services import change, costing, evm, finance
from ..services.allocation import allocation_for
from ..services.pricing import settings_for

router = APIRouter(prefix="/api", tags=["Dashboard"])
PORTFOLIO_ROLES = (Role.ADMIN, Role.MANAGEMENT, Role.FINANCE)


def _alert(project, severity, rule, message, notify):
    return {"project_id": project.id, "project_code": project.code, "severity": severity,
            "rule": rule, "message": message, "notify": notify}


def project_card(db: Session, p: Project, today: date) -> tuple[dict, list[dict]]:
    tasks = db.scalars(select(Task).where(Task.project_id == p.id)).all()
    tot = sum((t.estimate_md for t in tasks), Decimal(0))
    progress = (sum((t.estimate_md * min(t.progress_pct, Decimal(100)) / 100 for t in tasks), Decimal(0)) / tot
                if tot else Decimal(0))
    overdue = [t for t in tasks if t.planned_end and t.planned_end < today and t.status != TaskStatus.DONE]
    milestones = [t for t in tasks if t.is_milestone and t.status != TaskStatus.DONE and t.planned_end
                  and today <= t.planned_end <= today + timedelta(days=7)]
    risks = db.scalars(select(Risk).where(Risk.project_id == p.id, Risk.status != RiskStatus.CLOSED)).all()
    pending_cr = db.scalars(select(ChangeRequest).where(ChangeRequest.project_id == p.id,
                                                        ChangeRequest.status == DocStatus.SUBMITTED)).all()
    over_alloc = []
    for m, e in db.execute(select(ProjectMember, Employee).join(Employee, Employee.id == ProjectMember.employee_id)
                           .where(ProjectMember.project_id == p.id)).all():
        total, _ = allocation_for(db, e.id, today)
        if total > 100:
            over_alloc.append({"employee": e.name, "allocation_pct": total})

    r = evm.compute(db, p, today)
    revenue = p.contract_value + change.approved_cr_revenue(db, p.id)
    oh = settings_for(db, p.org_id, create=False).overhead_pct or Decimal(0)
    card = {"id": p.id, "code": p.code, "name": p.name, "status": p.status.value, "pm_id": p.pm_id,
            "end_date": p.end_date, "progress_pct": evm.idx(progress * 100), "has_baseline": r is not None,
            "overdue_tasks": len(overdue), "upcoming_milestones": [
                {"task_id": t.id, "name": t.name, "due": t.planned_end} for t in milestones],
            "open_risks": len(risks), "high_risks": sum(1 for x in risks if x.level in ("high", "critical")),
            "pending_change_requests": len(pending_cr), "over_allocated": over_alloc, "revenue": revenue}
    if r:
        eac = r.selected["eac"]
        card.update({"bac": r.bac, "pv": r.pv, "ev": r.ev, "ac": r.ac, "cpi": r.cpi, "spi": r.spi, "eac": eac,
                     "vac": r.selected["vac"], "health": r.health,
                     "budget_burn_pct": evm.idx(r.ac / r.bac * 100) if r.bac else None,
                     "eac_with_overhead": costing.money(eac * (1 + oh)),
                     "forecast_margin": evm.idx((revenue - eac * (1 + oh)) / revenue) if revenue else None})
    else:
        card.update({"health": "grey", "cpi": None, "spi": None})

    # ---- Aturan alert PRD §20 ----
    alerts = []
    if r:
        burn = r.ac / r.bac if r.bac else Decimal(0)
        if burn > Decimal("0.8") and progress < Decimal("0.7"):
            alerts.append(_alert(p, "high", "budget_burn",
                                 f"Actual cost {burn:.0%} dari budget, progress baru {progress:.0%}", ["PM", "Finance"]))
        if r.cpi is not None and r.cpi < Decimal("0.90"):
            alerts.append(_alert(p, "high", "cpi", f"Cost warning: CPI {r.cpi:.2f}", ["PM"]))
        if r.spi is not None and r.spi < Decimal("0.90"):
            alerts.append(_alert(p, "high", "spi", f"Schedule warning: SPI {r.spi:.2f}", ["PM"]))
        fm = card.get("forecast_margin")
        if p.target_margin is not None and fm is not None and fm < p.target_margin:
            alerts.append(_alert(p, "medium", "forecast_margin",
                                 f"Forecast margin {fm:.1%} < target {p.target_margin:.1%}", ["PM", "Management"]))
    for o in over_alloc:
        alerts.append(_alert(p, "medium", "over_allocation",
                             f"{o['employee']} teralokasi {o['allocation_pct']}%", ["PM"]))
    if overdue:
        alerts.append(_alert(p, "medium", "overdue_task", f"{len(overdue)} task melewati tenggat", ["Owner", "PM"]))
    for cr in pending_cr:
        alerts.append(_alert(p, "low", "cr_pending", f"{cr.code} menunggu approval", ["Approver"]))
    for t in milestones:
        alerts.append(_alert(p, "low", "milestone_due", f"Milestone '{t.name}' jatuh tempo {t.planned_end}", ["PM"]))

    # ---- Billing (Tahap 3) ----
    outstanding = overdue_amt = Decimal(0)
    for inv in db.scalars(select(Invoice).where(Invoice.project_id == p.id, Invoice.status.in_(finance.OPEN))).all():
        v = finance.invoice_view(db, inv, today)
        outstanding += v["outstanding"]
        if v["display_status"] == "overdue":
            overdue_amt += v["outstanding"]
            alerts.append(_alert(p, "high", "invoice_overdue", f"Invoice {inv.invoice_no} lewat jatuh tempo "
                                 f"{v['days_past_due']} hari (sisa {costing.money(v['outstanding']):,.0f})", ["Finance"]))
        elif v["display_status"] == "due":
            alerts.append(_alert(p, "medium", "invoice_due", f"Invoice {inv.invoice_no} jatuh tempo {inv.due_date}, "
                                 "belum lunas", ["Finance"]))
    # ---- Pengadaan: item belum dipesan padahal batas pesan (need-by − lead time) sudah dekat ----
    for it in db.scalars(select(ProcurementItem).where(ProcurementItem.project_id == p.id,
                                                       ProcurementItem.status == ProcStatus.PLANNED,
                                                       ProcurementItem.need_by_date.is_not(None))).all():
        order_by = it.need_by_date - timedelta(days=it.lead_time_days or 0)
        if order_by <= today + timedelta(days=7):
            late = order_by < today
            alerts.append(_alert(p, "high" if late else "medium", "procurement_order",
                                 f"{'Terlambat pesan' if late else 'Segera pesan'}: {it.name} × {it.qty.normalize()} "
                                 f"(batas pesan {order_by}, dibutuhkan {it.need_by_date})", ["PM", "Finance"]))
    card["ar_outstanding"] = costing.money(outstanding)
    card["ar_overdue"] = costing.money(overdue_amt)
    return card, alerts


@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    today = date.today()
    if user.role == Role.STAFF:
        emp = employee_of(db, user)
        tasks = db.scalars(select(Task).where(Task.owner_employee_id == (emp.id if emp else -1),
                                              Task.status != TaskStatus.DONE).order_by(Task.planned_end)).all() \
            if emp else []
        return {"role": user.role.value, "today": today, "my_tasks": [
            {"id": t.id, "project_id": t.project_id, "name": t.name, "status": t.status.value,
             "progress_pct": t.progress_pct, "planned_end": t.planned_end,
             "overdue": bool(t.planned_end and t.planned_end < today)} for t in tasks]}

    q = select(Project).where(Project.org_id == user.org_id, Project.is_deleted.is_(False),
                              Project.status.not_in([ProjectStatus.CLOSED, ProjectStatus.CANCELLED]))
    if user.role == Role.PM:
        q = q.where(Project.pm_id == user.id)
    cards, alerts = [], []
    for p in db.scalars(q.order_by(Project.code)).all():
        c, a = project_card(db, p, today)
        cards.append(c)
        alerts.extend(a)
    order = {"high": 0, "medium": 1, "low": 2}
    alerts.sort(key=lambda x: order[x["severity"]])
    out = {"role": user.role.value, "today": today, "projects": cards, "alerts": alerts}
    if user.role in PORTFOLIO_ROLES:
        with_bl = [c for c in cards if c["has_baseline"]]
        rev = sum((c["revenue"] for c in cards), Decimal(0))
        eac = sum((c["eac_with_overhead"] for c in with_bl), Decimal(0))
        rev_bl = sum((c["revenue"] for c in with_bl), Decimal(0))
        out["portfolio"] = {
            "active_projects": len(cards), "revenue": costing.money(rev),
            "bac": costing.money(sum((c["bac"] for c in with_bl), Decimal(0))),
            "ac": costing.money(sum((c["ac"] for c in with_bl), Decimal(0))),
            "eac": costing.money(eac),
            "forecast_margin": evm.idx((rev_bl - eac) / rev_bl) if rev_bl else None,
            "projects_at_risk": sum(1 for c in cards if c["health"] == "red"),
            "health": {h: sum(1 for c in cards if c["health"] == h) for h in ("green", "amber", "red", "grey")},
        }
        ar = finance.ar_aging(db, user.org_id, today)
        out["portfolio"].update({"ar_outstanding": ar["total_outstanding"], "ar_overdue": ar["overdue"],
                                 "ar_buckets": ar["buckets"]})
    return out
