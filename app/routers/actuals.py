"""Timesheet & Actual Cost (PRD §13), Expense (PRD §14), period closing, cost summary."""
from collections import defaultdict
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import audit
from ..database import get_db
from ..models import (WBS, AuditLog, Budget, DocStatus, Employee, Expense, Organization,
                      PeriodLock, ProjectMember, Role, Task, Timesheet, User, Vendor, utcnow)
from ..schemas import (AuditOut, DecisionIn, ExpenseIn, ExpenseOut, PeriodIn, PeriodOut,
                       TimesheetIn, TimesheetOut, TimesheetUpdate)
from ..security import (APPROVERS_EXPENSE, FINANCE_VIEW, employee_of, get_current_user,
                        load_project, require_roles)
from ..services import costing

router = APIRouter(prefix="/api", tags=["Actuals: Timesheet, Expense, Period"])


def _assert_open(db: Session, org_id: int, d: date):
    period = d.strftime("%Y-%m")
    if db.scalar(select(PeriodLock).where(PeriodLock.org_id == org_id, PeriodLock.period == period)):
        raise HTTPException(423, f"Periode {period} sudah dikunci (closed)")


def _ts_out(t: Timesheet, user: User) -> TimesheetOut:
    o = TimesheetOut.model_validate(t)
    if user.role not in FINANCE_VIEW + (Role.PM,):  # staff tidak melihat cost rate
        o.cost_rate = None
        o.labor_cost = None
    return o


# ================= TIMESHEET =================
def _price_timesheet(db: Session, t: Timesheet, org: Organization):
    rate = costing.rate_effective_on(db, t.employee_id, t.work_date)
    if not rate:
        raise HTTPException(422, f"Employee belum punya cost rate yang berlaku per {t.work_date}")
    t.actual_md = costing.actual_md(t.hours, org.standard_hours_per_day)
    t.rate_id, t.cost_rate = rate.id, rate.cost_per_md
    t.labor_cost = costing.labor_cost(t.actual_md, rate.cost_per_md)


def _check_daily_hours(db, employee_id, work_date, hours, exclude_id=None):
    q = select(func.coalesce(func.sum(Timesheet.hours), 0)).where(
        Timesheet.employee_id == employee_id, Timesheet.work_date == work_date,
        Timesheet.status != DocStatus.REJECTED)
    if exclude_id:
        q = q.where(Timesheet.id != exclude_id)
    if Decimal(db.scalar(q)) + hours > 24:
        raise HTTPException(422, "Total jam per hari melebihi 24")


@router.post("/timesheets", response_model=TimesheetOut, status_code=201)
def create_timesheet(body: TimesheetIn, db: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    p = load_project(db, user, body.project_id)
    me = employee_of(db, user)
    if body.employee_id is None or (me and body.employee_id == me.id):
        if not me:
            raise HTTPException(422, "User ini tidak terhubung ke employee")
        emp_id = me.id
    else:
        if user.role not in (Role.ADMIN, Role.PM):
            raise HTTPException(403, "Hanya PM/admin yang dapat input timesheet orang lain")
        load_project(db, user, p.id, manage=True)
        emp_id = body.employee_id
    if not db.scalar(select(ProjectMember).where(ProjectMember.project_id == p.id,
                                                 ProjectMember.employee_id == emp_id)):
        raise HTTPException(422, "Employee bukan member project ini")

    wbs_id = body.wbs_id
    if body.task_id:
        task = db.get(Task, body.task_id)
        if not task or task.project_id != p.id:
            raise HTTPException(422, "task_id tidak valid")
        wbs_id = task.wbs_id
    w = db.get(WBS, wbs_id)
    if not w or w.project_id != p.id:
        raise HTTPException(422, "wbs_id tidak valid")
    _assert_open(db, user.org_id, body.work_date)
    _check_daily_hours(db, emp_id, body.work_date, body.hours)

    t = Timesheet(org_id=user.org_id, employee_id=emp_id, project_id=p.id, wbs_id=wbs_id,
                  task_id=body.task_id, work_date=body.work_date, hours=body.hours,
                  description=body.description)
    _price_timesheet(db, t, db.get(Organization, user.org_id))
    db.add(t)
    db.flush()
    audit.log(db, user, "create", "timesheet", t.id, new=audit.snapshot(t))
    db.commit()
    return _ts_out(t, user)


def _own_or_manager(db, user, t: Timesheet):
    me = employee_of(db, user)
    if me and t.employee_id == me.id:
        return
    load_project(db, user, t.project_id, manage=True)


@router.patch("/timesheets/{tid}", response_model=TimesheetOut)
def update_timesheet(tid: int, body: TimesheetUpdate, db: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    t = db.get(Timesheet, tid)
    if not t or t.org_id != user.org_id:
        raise HTTPException(404, "Timesheet not found")
    _own_or_manager(db, user, t)
    if t.status not in (DocStatus.DRAFT, DocStatus.REJECTED):
        raise HTTPException(409, "Hanya timesheet draft/rejected yang dapat diubah")
    _assert_open(db, user.org_id, t.work_date)
    data = body.model_dump(exclude_unset=True)
    if "work_date" in data:
        _assert_open(db, user.org_id, data["work_date"])
    if "task_id" in data and data["task_id"]:
        task = db.get(Task, data["task_id"])
        if not task or task.project_id != t.project_id:
            raise HTTPException(422, "task_id tidak valid")
        t.wbs_id = task.wbs_id
    old = audit.snapshot(t)
    for k, v in data.items():
        setattr(t, k, v)
    _check_daily_hours(db, t.employee_id, t.work_date, t.hours, exclude_id=t.id)
    t.status, t.reject_reason = DocStatus.DRAFT, None
    _price_timesheet(db, t, db.get(Organization, user.org_id))
    audit.log(db, user, "update", "timesheet", t.id, old=old, new=audit.snapshot(t))
    db.commit()
    return _ts_out(t, user)


@router.post("/timesheets/{tid}/submit", response_model=TimesheetOut)
def submit_timesheet(tid: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    t = db.get(Timesheet, tid)
    if not t or t.org_id != user.org_id:
        raise HTTPException(404, "Timesheet not found")
    _own_or_manager(db, user, t)
    if t.status != DocStatus.DRAFT:
        raise HTTPException(409, "Hanya timesheet draft yang dapat di-submit")
    _assert_open(db, user.org_id, t.work_date)
    t.status = DocStatus.SUBMITTED
    audit.log(db, user, "submit", "timesheet", t.id)
    db.commit()
    return _ts_out(t, user)


def _decide_timesheet(db, user, tid, approve: bool, reason: str | None):
    t = db.get(Timesheet, tid)
    if not t or t.org_id != user.org_id:
        raise HTTPException(404, "Timesheet not found")
    load_project(db, user, t.project_id, manage=True)  # PM project / management / admin
    me = employee_of(db, user)
    if me and me.id == t.employee_id:
        raise HTTPException(403, "Tidak boleh approve timesheet sendiri")
    if t.status != DocStatus.SUBMITTED:
        raise HTTPException(409, "Hanya timesheet submitted yang dapat diputuskan")
    _assert_open(db, user.org_id, t.work_date)
    if approve:
        # Harga dibekukan pada saat approve (snapshot rate yang berlaku di work_date)
        _price_timesheet(db, t, db.get(Organization, user.org_id))
        t.status, t.approved_by_id, t.approved_at = DocStatus.APPROVED, user.id, utcnow()
    else:
        if not reason:
            raise HTTPException(422, "Alasan reject wajib diisi")
        t.status, t.reject_reason = DocStatus.REJECTED, reason
    audit.log(db, user, "approve" if approve else "reject", "timesheet", t.id,
              new={"status": t.status, "labor_cost": t.labor_cost}, reason=reason)
    db.commit()
    return _ts_out(t, user)


@router.post("/timesheets/{tid}/approve", response_model=TimesheetOut)
def approve_timesheet(tid: int, body: DecisionIn, db: Session = Depends(get_db),
                      user: User = Depends(get_current_user)):
    return _decide_timesheet(db, user, tid, True, body.reason)


@router.post("/timesheets/{tid}/reject", response_model=TimesheetOut)
def reject_timesheet(tid: int, body: DecisionIn, db: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    return _decide_timesheet(db, user, tid, False, body.reason)


@router.get("/timesheets", response_model=list[TimesheetOut])
def list_timesheets(project_id: int | None = None, employee_id: int | None = None,
                    status: DocStatus | None = None, date_from: date | None = None,
                    date_to: date | None = None, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    q = select(Timesheet).where(Timesheet.org_id == user.org_id)
    if user.role == Role.STAFF:
        me = employee_of(db, user)
        q = q.where(Timesheet.employee_id == (me.id if me else -1))
    elif project_id:
        load_project(db, user, project_id)
    elif user.role == Role.PM:
        raise HTTPException(422, "PM wajib mengisi project_id")
    if project_id:
        q = q.where(Timesheet.project_id == project_id)
    if employee_id:
        q = q.where(Timesheet.employee_id == employee_id)
    if status:
        q = q.where(Timesheet.status == status)
    if date_from:
        q = q.where(Timesheet.work_date >= date_from)
    if date_to:
        q = q.where(Timesheet.work_date <= date_to)
    return [_ts_out(t, user) for t in db.scalars(q.order_by(Timesheet.work_date, Timesheet.id)).all()]


# ================= EXPENSE =================
def _expense(db, user, eid) -> Expense:
    e = db.get(Expense, eid)
    if not e or e.org_id != user.org_id:
        raise HTTPException(404, "Expense not found")
    return e


@router.post("/expenses", response_model=ExpenseOut, status_code=201)
def create_expense(body: ExpenseIn, db: Session = Depends(get_db),
                   user: User = Depends(require_roles(Role.ADMIN, Role.MANAGEMENT, Role.PM,
                                                      Role.FINANCE, Role.STAFF))):
    p = load_project(db, user, body.project_id)
    if body.wbs_id:
        w = db.get(WBS, body.wbs_id)
        if not w or w.project_id != p.id:
            raise HTTPException(422, "wbs_id tidak valid")
    if body.vendor_id:
        v = db.get(Vendor, body.vendor_id)
        if not v or v.org_id != user.org_id or v.is_deleted:
            raise HTTPException(422, "vendor_id tidak valid")
    _assert_open(db, user.org_id, body.expense_date)
    e = Expense(org_id=user.org_id, created_by_id=user.id, **body.model_dump())
    db.add(e)
    db.flush()
    audit.log(db, user, "create", "expense", e.id, new=audit.snapshot(e))
    db.commit()
    return e


@router.post("/expenses/{eid}/submit", response_model=ExpenseOut)
def submit_expense(eid: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    e = _expense(db, user, eid)
    if e.created_by_id != user.id:
        load_project(db, user, e.project_id, manage=True)
    if e.status != DocStatus.DRAFT:
        raise HTTPException(409, "Hanya expense draft yang dapat di-submit")
    e.status = DocStatus.SUBMITTED
    audit.log(db, user, "submit", "expense", e.id)
    db.commit()
    return e


@router.post("/expenses/{eid}/approve", response_model=ExpenseOut)
def approve_expense(eid: int, body: DecisionIn, db: Session = Depends(get_db),
                    user: User = Depends(require_roles(*APPROVERS_EXPENSE))):
    """Approve = Post to Project Cost (Draft → Submit → Approve → Post)."""
    e = _expense(db, user, eid)
    if e.status != DocStatus.SUBMITTED:
        raise HTTPException(409, "Hanya expense submitted yang dapat di-approve")
    if e.created_by_id == user.id:
        raise HTTPException(403, "Segregation of duties: pembuat expense tidak boleh meng-approve")
    _assert_open(db, user.org_id, e.expense_date)
    e.status, e.approved_by_id, e.posted_at = DocStatus.POSTED, user.id, utcnow()
    audit.log(db, user, "approve_post", "expense", e.id, new={"status": "posted"}, reason=body.reason)
    db.commit()
    return e


@router.post("/expenses/{eid}/reject", response_model=ExpenseOut)
def reject_expense(eid: int, body: DecisionIn, db: Session = Depends(get_db),
                   user: User = Depends(require_roles(*APPROVERS_EXPENSE))):
    e = _expense(db, user, eid)
    if e.status != DocStatus.SUBMITTED:
        raise HTTPException(409, "Hanya expense submitted yang dapat di-reject")
    if not body.reason:
        raise HTTPException(422, "Alasan reject wajib diisi")
    e.status, e.reject_reason = DocStatus.REJECTED, body.reason
    audit.log(db, user, "reject", "expense", e.id, reason=body.reason)
    db.commit()
    return e


@router.post("/expenses/{eid}/reverse", response_model=ExpenseOut, status_code=201)
def reverse_expense(eid: int, body: DecisionIn, db: Session = Depends(get_db),
                    user: User = Depends(require_roles(Role.ADMIN, Role.FINANCE))):
    """Koreksi expense posted: buat transaksi reversal (amount negatif). Asli tidak diubah."""
    e = _expense(db, user, eid)
    if e.status != DocStatus.POSTED or e.reversal_of_id:
        raise HTTPException(409, "Hanya expense posted (bukan reversal) yang dapat di-reverse")
    if db.scalar(select(Expense).where(Expense.reversal_of_id == e.id)):
        raise HTTPException(409, "Expense ini sudah pernah di-reverse")
    if not body.reason:
        raise HTTPException(422, "Alasan reversal wajib diisi")
    today = date.today()
    _assert_open(db, user.org_id, today)
    r = Expense(org_id=e.org_id, project_id=e.project_id, wbs_id=e.wbs_id, vendor_id=e.vendor_id,
                category=e.category, amount=-e.amount, expense_date=today,
                description=f"REVERSAL of #{e.id}: {body.reason}", reference=e.reference,
                status=DocStatus.POSTED, reversal_of_id=e.id, created_by_id=user.id,
                approved_by_id=user.id, posted_at=utcnow())
    db.add(r)
    db.flush()
    audit.log(db, user, "reverse", "expense", e.id, new={"reversal_id": r.id}, reason=body.reason)
    db.commit()
    return r


@router.get("/expenses", response_model=list[ExpenseOut])
def list_expenses(project_id: int, status: DocStatus | None = None, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    load_project(db, user, project_id)
    q = select(Expense).where(Expense.project_id == project_id)
    if user.role == Role.STAFF:
        q = q.where(Expense.created_by_id == user.id)
    if status:
        q = q.where(Expense.status == status)
    return db.scalars(q.order_by(Expense.expense_date, Expense.id)).all()


# ================= PERIOD CLOSING =================
@router.post("/periods/lock", response_model=PeriodOut, status_code=201)
def lock_period(body: PeriodIn, db: Session = Depends(get_db),
                user: User = Depends(require_roles(Role.ADMIN, Role.FINANCE))):
    if db.scalar(select(PeriodLock).where(PeriodLock.org_id == user.org_id,
                                          PeriodLock.period == body.period)):
        raise HTTPException(409, "Periode sudah dikunci")
    pl = PeriodLock(org_id=user.org_id, period=body.period, locked_by_id=user.id)
    db.add(pl)
    db.flush()
    audit.log(db, user, "lock", "period", pl.id, new={"period": body.period}, reason=body.reason)
    db.commit()
    return pl


@router.post("/periods/unlock", status_code=204)
def unlock_period(body: PeriodIn, db: Session = Depends(get_db),
                  user: User = Depends(require_roles(Role.ADMIN))):
    if not body.reason:
        raise HTTPException(422, "Alasan unlock wajib diisi")
    pl = db.scalar(select(PeriodLock).where(PeriodLock.org_id == user.org_id,
                                            PeriodLock.period == body.period))
    if not pl:
        raise HTTPException(404, "Periode tidak terkunci")
    audit.log(db, user, "unlock", "period", pl.id, old={"period": pl.period}, reason=body.reason)
    db.delete(pl)
    db.commit()


@router.get("/periods", response_model=list[PeriodOut])
def list_periods(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return db.scalars(select(PeriodLock).where(PeriodLock.org_id == user.org_id)
                      .order_by(PeriodLock.period)).all()


# ================= COST SUMMARY =================
@router.get("/projects/{pid}/cost-summary")
def cost_summary(pid: int, db: Session = Depends(get_db),
                 user: User = Depends(require_roles(Role.ADMIN, Role.MANAGEMENT, Role.PM, Role.FINANCE))):
    """Budget (approved) vs Actual Cost per project & per WBS. AC = labor approved + expense posted."""
    p = load_project(db, user, pid)
    budget = db.scalar(select(Budget).where(Budget.project_id == p.id,
                                            Budget.status == DocStatus.APPROVED))
    lab = db.execute(select(Timesheet.wbs_id, func.sum(Timesheet.labor_cost), func.sum(Timesheet.actual_md))
                     .where(Timesheet.project_id == p.id, Timesheet.status == DocStatus.APPROVED)
                     .group_by(Timesheet.wbs_id)).all()
    exp = db.execute(select(Expense.wbs_id, func.sum(Expense.amount))
                     .where(Expense.project_id == p.id, Expense.status == DocStatus.POSTED)
                     .group_by(Expense.wbs_id)).all()
    per = defaultdict(lambda: {"planned": Decimal(0), "planned_md": Decimal(0), "actual_labor": Decimal(0),
                               "actual_md": Decimal(0), "actual_expense": Decimal(0)})
    if budget:
        for l in budget.lines:
            per[l.wbs_id]["planned"] += l.amount
            per[l.wbs_id]["planned_md"] += l.md or 0
    for wid, cost, md in lab:
        per[wid]["actual_labor"] += Decimal(cost or 0)
        per[wid]["actual_md"] += Decimal(md or 0)
    for wid, amt in exp:
        per[wid]["actual_expense"] += Decimal(amt or 0)

    wbs_names = {w.id: (w.code, w.name) for w in db.scalars(select(WBS).where(WBS.project_id == p.id))}
    rows = []
    for wid, v in sorted(per.items(), key=lambda kv: wbs_names.get(kv[0], ("~", ""))[0]):
        ac = v["actual_labor"] + v["actual_expense"]
        code, name = wbs_names.get(wid, (None, "(tanpa WBS)"))
        rows.append({"wbs_id": wid, "wbs_code": code, "wbs_name": name,
                     "planned_cost": str(costing.money(v["planned"])), "planned_md": str(v["planned_md"]),
                     "actual_md": str(v["actual_md"]),
                     "actual_labor": str(costing.money(v["actual_labor"])),
                     "actual_expense": str(costing.money(v["actual_expense"])),
                     "actual_cost": str(costing.money(ac)),
                     "variance": str(costing.money(v["planned"] - ac))})

    actual_labor = sum((v["actual_labor"] for v in per.values()), Decimal(0))
    actual_exp = sum((v["actual_expense"] for v in per.values()), Decimal(0))
    ac = actual_labor + actual_exp
    tasks = db.scalars(select(Task).where(Task.project_id == p.id)).all()
    tot_md = sum((t.estimate_md for t in tasks), Decimal(0))
    progress = (sum((t.estimate_md * t.progress_pct / 100 for t in tasks), Decimal(0)) / tot_md * 100
                if tot_md else Decimal(0))
    baseline = budget.cost_baseline if budget else None
    burn = (ac / baseline * 100).quantize(Decimal("0.01")) if baseline else None
    alerts = []
    if burn is not None and burn > 80 and progress < 70:  # PRD §20 rule #1 (preview)
        alerts.append("Actual cost > 80% budget sementara progress < 70%")
    if baseline is not None and ac > baseline:
        alerts.append("Actual cost melebihi cost baseline")
    return {
        "project_id": p.id, "project_code": p.code,
        "approved_budget_version": budget.version if budget else None,
        "cost_baseline": str(baseline) if baseline is not None else None,
        "total_budget": str(budget.total_budget) if budget else None,
        "actual_labor": str(costing.money(actual_labor)),
        "actual_expense": str(costing.money(actual_exp)),
        "actual_cost": str(costing.money(ac)),
        "remaining_baseline": str(costing.money(baseline - ac)) if baseline is not None else None,
        "budget_burn_pct": str(burn) if burn is not None else None,
        "progress_pct": str(progress.quantize(Decimal("0.01"))),
        "alerts": alerts, "by_wbs": rows,
    }


# ================= AUDIT =================
@router.get("/audit-logs", response_model=list[AuditOut])
def audit_logs(entity: str | None = None, entity_id: int | None = None, limit: int = 100,
               db: Session = Depends(get_db),
               user: User = Depends(require_roles(Role.ADMIN, Role.MANAGEMENT, Role.FINANCE))):
    q = select(AuditLog).where(AuditLog.org_id == user.org_id)
    if entity:
        q = q.where(AuditLog.entity == entity)
    if entity_id:
        q = q.where(AuditLog.entity_id == entity_id)
    return db.scalars(q.order_by(AuditLog.id.desc()).limit(min(limit, 500))).all()
