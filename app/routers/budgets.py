"""Costing & Budgeting (PRD §10) dengan versioning + approval.

Alur: generate (draft) → submit → approve/reject.
Approve versi baru otomatis men-supersede versi approved sebelumnya.
Versi approved/superseded tidak bisa diubah (immutable).
"""
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import audit
from ..database import get_db
from ..models import WBS, Budget, BudgetLine, DocStatus, Employee, Task, User, utcnow
from ..schemas import BudgetDetailOut, BudgetGenerateIn, BudgetOut, DecisionIn
from ..security import APPROVERS_BUDGET, get_current_user, load_project
from ..services import costing

router = APIRouter(prefix="/api", tags=["Budget"])


def _budget(db, user, bid, manage=False) -> Budget:
    b = db.get(Budget, bid)
    if not b:
        raise HTTPException(404, "Budget not found")
    load_project(db, user, b.project_id, manage=manage)
    return b


@router.post("/projects/{pid}/budgets/generate", response_model=BudgetDetailOut, status_code=201)
def generate_budget(pid: int, body: BudgetGenerateIn, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    """Hitung budget draft: labor = Σ(task.estimate_md × cost rate owner pada tanggal rencana)."""
    p = load_project(db, user, pid, manage=True)
    tasks = db.scalars(select(Task).where(Task.project_id == p.id).order_by(Task.id)).all()
    if not tasks:
        raise HTTPException(422, "Project belum memiliki task untuk dihitung")

    problems, lines = [], []
    for t in tasks:
        if t.estimate_md <= 0:
            continue
        if not t.owner_employee_id:
            problems.append(f"Task #{t.id} '{t.name}' belum punya owner")
            continue
        on = t.planned_start or p.start_date
        if not on:
            problems.append(f"Task #{t.id} tidak punya planned_start dan project tidak punya start_date")
            continue
        rate = costing.rate_effective_on(db, t.owner_employee_id, on)
        if not rate:
            emp = db.get(Employee, t.owner_employee_id)
            problems.append(f"Employee '{emp.name}' belum punya cost rate per {on}")
            continue
        lines.append(BudgetLine(wbs_id=t.wbs_id, task_id=t.id, employee_id=t.owner_employee_id,
                                category="labor", description=t.name, md=t.estimate_md,
                                rate=rate.cost_per_md,
                                amount=costing.labor_cost(t.estimate_md, rate.cost_per_md)))
    if problems:
        # Lebih aman menolak daripada menghasilkan budget yang diam-diam terlalu kecil
        raise HTTPException(422, {"message": "Budget tidak lengkap", "issues": problems})

    # BOQ / procurement: qty × harga beli per WBS (otomatis)
    from ..models import PROC_TO_EXPENSE, ProcStatus, ProcurementItem
    for it in db.scalars(select(ProcurementItem).where(ProcurementItem.project_id == p.id,
                                                       ProcurementItem.status != ProcStatus.CANCELLED)
                         .order_by(ProcurementItem.id)).all():
        uc = it.actual_unit_cost if it.actual_unit_cost is not None else it.unit_cost
        lines.append(BudgetLine(wbs_id=it.wbs_id, category=PROC_TO_EXPENSE[it.category.value],
                                description=it.name, procurement_item_id=it.id, qty=it.qty, unit=it.unit,
                                rate=uc, amount=costing.money(it.qty * uc)))

    for e in body.planned_expenses:
        if e.wbs_id:
            w = db.get(WBS, e.wbs_id)
            if not w or w.project_id != p.id:
                raise HTTPException(422, f"wbs_id {e.wbs_id} tidak valid")
        lines.append(BudgetLine(wbs_id=e.wbs_id, category=e.category.value,
                                description=e.description, amount=costing.money(e.amount)))

    labor = sum((l.amount for l in lines if l.category == "labor"), Decimal(0))
    expense = sum((l.amount for l in lines if l.category != "labor"), Decimal(0))
    procurement = sum((l.amount for l in lines if l.procurement_item_id), Decimal(0))
    total_md = sum((l.md for l in lines if l.md), Decimal(0))
    direct = labor + expense
    if body.contingency_pct is not None:
        contingency = costing.money(direct * body.contingency_pct)
    else:
        contingency = costing.money(body.contingency_amount or 0)
    baseline = direct + contingency
    mr = costing.money(body.management_reserve_amount)

    version = (db.scalar(select(func.max(Budget.version)).where(Budget.project_id == p.id)) or 0) + 1
    b = Budget(project_id=p.id, version=version, status=DocStatus.DRAFT, total_md=total_md,
               labor_cost=labor, expense_cost=expense, procurement_cost=procurement, direct_cost=direct, contingency=contingency,
               cost_baseline=baseline, management_reserve=mr, total_budget=baseline + mr,
               notes=body.notes, created_by_id=user.id, lines=lines)
    db.add(b)
    db.flush()
    audit.log(db, user, "create", "budget", b.id, new=BudgetOut.model_validate(b).model_dump())
    db.commit()
    return b


@router.get("/projects/{pid}/budgets", response_model=list[BudgetOut])
def list_budgets(pid: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    p = load_project(db, user, pid)
    return db.scalars(select(Budget).where(Budget.project_id == p.id).order_by(Budget.version)).all()


@router.get("/budgets/{bid}", response_model=BudgetDetailOut)
def get_budget(bid: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _budget(db, user, bid)


@router.post("/budgets/{bid}/submit", response_model=BudgetOut)
def submit_budget(bid: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    b = _budget(db, user, bid, manage=True)
    if b.status != DocStatus.DRAFT:
        raise HTTPException(409, f"Budget berstatus {b.status.value}, tidak bisa di-submit")
    b.status = DocStatus.SUBMITTED
    audit.log(db, user, "submit", "budget", b.id, old={"status": "draft"}, new={"status": "submitted"})
    db.commit()
    return b


@router.post("/budgets/{bid}/approve", response_model=BudgetOut)
def approve_budget(bid: int, body: DecisionIn, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    if user.role not in APPROVERS_BUDGET:
        raise HTTPException(403, "Hanya finance/management/admin yang dapat approve budget")
    b = _budget(db, user, bid)
    if b.status != DocStatus.SUBMITTED:
        raise HTTPException(409, "Hanya budget berstatus submitted yang dapat di-approve")
    if b.created_by_id == user.id:
        raise HTTPException(403, "Segregation of duties: pembuat budget tidak boleh meng-approve")
    prev = db.scalars(select(Budget).where(Budget.project_id == b.project_id,
                                           Budget.status == DocStatus.APPROVED)).all()
    for old in prev:
        old.status = DocStatus.SUPERSEDED
        audit.log(db, user, "supersede", "budget", old.id, old={"status": "approved"},
                  new={"status": "superseded", "by_version": b.version})
    b.status, b.approved_by_id, b.approved_at = DocStatus.APPROVED, user.id, utcnow()
    audit.log(db, user, "approve", "budget", b.id, old={"status": "submitted"},
              new={"status": "approved"}, reason=body.reason)
    db.commit()
    return b


@router.post("/budgets/{bid}/reject", response_model=BudgetOut)
def reject_budget(bid: int, body: DecisionIn, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    if user.role not in APPROVERS_BUDGET:
        raise HTTPException(403, "Hanya finance/management/admin yang dapat reject budget")
    b = _budget(db, user, bid)
    if b.status != DocStatus.SUBMITTED:
        raise HTTPException(409, "Hanya budget berstatus submitted yang dapat di-reject")
    if not body.reason:
        raise HTTPException(422, "Alasan reject wajib diisi")
    b.status = DocStatus.REJECTED
    audit.log(db, user, "reject", "budget", b.id, old={"status": "submitted"},
              new={"status": "rejected"}, reason=body.reason)
    db.commit()
    return b
