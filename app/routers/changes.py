"""Risk Register (PRD §19) & Change Request (PRD §12.1)."""
import json
from datetime import date
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import audit
from ..database import get_db
from ..models import ChangeRequest, DocStatus, ExpenseCategory, Risk, Role, User, utcnow
from ..schemas import CRDetailOut, CRIn, CRLineOut, CROut, DecisionIn, RiskIn, RiskOut, RiskUpdate
from ..security import get_current_user, load_project
from ..services import change, evm

router = APIRouter(prefix="/api", tags=["Risk & Change Request"])
CR_APPROVERS = (Role.ADMIN, Role.MANAGEMENT)


def risk_level(score: int) -> str:
    return "critical" if score >= 15 else "high" if score >= 10 else "medium" if score >= 5 else "low"


# ---------------- Risk ----------------
def _risk(db, user, rid, manage=False) -> Risk:
    r = db.get(Risk, rid)
    if not r:
        raise HTTPException(404, "Risk not found")
    load_project(db, user, r.project_id, manage=manage)
    return r


@router.post("/projects/{pid}/risks", response_model=RiskOut, status_code=201)
def create_risk(pid: int, body: RiskIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    p = load_project(db, user, pid, manage=True)
    n = (db.scalar(select(func.count(Risk.id)).where(Risk.project_id == p.id)) or 0) + 1
    score = body.probability * body.impact
    r = Risk(project_id=p.id, code=f"R-{n:03d}", score=score, level=risk_level(score), **body.model_dump())
    db.add(r)
    db.flush()
    audit.log(db, user, "create", "risk", r.id, new=audit.snapshot(r))
    db.commit()
    return r


@router.get("/projects/{pid}/risks", response_model=list[RiskOut])
def list_risks(pid: int, include_closed: bool = False, db: Session = Depends(get_db),
               user: User = Depends(get_current_user)):
    p = load_project(db, user, pid)
    q = select(Risk).where(Risk.project_id == p.id)
    if not include_closed:
        q = q.where(Risk.status != "closed")
    return db.scalars(q.order_by(Risk.score.desc(), Risk.id)).all()


@router.patch("/risks/{rid}", response_model=RiskOut)
def update_risk(rid: int, body: RiskUpdate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    r = _risk(db, user, rid, manage=True)
    old = audit.snapshot(r)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(r, k, v)
    r.score = r.probability * r.impact
    r.level = risk_level(r.score)
    audit.log(db, user, "update", "risk", r.id, old=old, new=audit.snapshot(r))
    db.commit()
    return r


# ---------------- Change Request ----------------
def _cr(db, user, cid, manage=False) -> ChangeRequest:
    cr = db.get(ChangeRequest, cid)
    if not cr:
        raise HTTPException(404, "Change request not found")
    load_project(db, user, cr.project_id, manage=manage)
    return cr


def _detail(cr: ChangeRequest) -> CRDetailOut:
    base = CROut.model_validate(cr).model_dump()
    lines = [CRLineOut.model_validate(l) for l in cr.lines]
    return CRDetailOut(**base, lines=lines, impact=json.loads(cr.impact) if cr.impact else None)


def _as_input(l) -> SimpleNamespace:
    """CRLine tersimpan → bentuk input untuk change.analyze."""
    d = {k: getattr(l, k) for k in ("line_type", "wbs_id", "task_id", "name", "employee_id",
                                    "md_delta", "planned_start", "planned_end", "amount")}
    d["category"] = ExpenseCategory(l.category) if l.category else None
    return SimpleNamespace(**d)


def _baseline_or_409(db, p):
    bl = evm.active_baseline(db, p.id)
    if not bl:
        raise HTTPException(409, "Change Request memerlukan baseline aktif. Sebelum baseline, ubah task/budget langsung")
    return bl


@router.post("/projects/{pid}/change-requests/preview")
def preview_cr(pid: int, body: CRIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """What-if analysis tanpa menyimpan apa pun. Contoh: +20 MD Backend, delay 14 hari."""
    p = load_project(db, user, pid, manage=True)
    bl = _baseline_or_409(db, p)
    _, impact = change.analyze(db, p, bl, body.lines, body.schedule_impact_days, body.revenue_delta, date.today())
    db.rollback()
    return impact


@router.post("/projects/{pid}/change-requests", response_model=CRDetailOut, status_code=201)
def create_cr(pid: int, body: CRIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    p = load_project(db, user, pid, manage=True)
    bl = _baseline_or_409(db, p)
    if not body.lines and not body.schedule_impact_days and not body.revenue_delta:
        raise HTTPException(422, "CR harus memiliki dampak scope, schedule, atau revenue")
    lines, impact = change.analyze(db, p, bl, body.lines, body.schedule_impact_days, body.revenue_delta, date.today())
    n = (db.scalar(select(func.count(ChangeRequest.id)).where(ChangeRequest.project_id == p.id)) or 0) + 1
    cr = ChangeRequest(project_id=p.id, code=f"CR-{n:03d}", title=body.title, description=body.description,
                       requested_by=body.requested_by, reason=body.reason,
                       schedule_impact_days=body.schedule_impact_days, revenue_delta=body.revenue_delta,
                       md_impact=impact["md_impact"], cost_impact=impact["cost_impact"],
                       impact=json.dumps(impact, default=audit._default), created_by_id=user.id, lines=lines)
    db.add(cr)
    db.flush()
    audit.log(db, user, "create", "change_request", cr.id,
              new={"code": cr.code, "cost_impact": cr.cost_impact, "days": cr.schedule_impact_days})
    db.commit()
    return _detail(cr)


@router.get("/projects/{pid}/change-requests", response_model=list[CROut])
def list_crs(pid: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    p = load_project(db, user, pid)
    return db.scalars(select(ChangeRequest).where(ChangeRequest.project_id == p.id).order_by(ChangeRequest.id)).all()


@router.get("/change-requests/{cid}", response_model=CRDetailOut)
def get_cr(cid: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _detail(_cr(db, user, cid))


@router.post("/change-requests/{cid}/submit", response_model=CROut)
def submit_cr(cid: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    cr = _cr(db, user, cid, manage=True)
    if cr.status != DocStatus.DRAFT:
        raise HTTPException(409, "Hanya CR draft yang dapat di-submit")
    cr.status = DocStatus.SUBMITTED
    audit.log(db, user, "submit", "change_request", cr.id)
    db.commit()
    return cr


@router.post("/change-requests/{cid}/approve", response_model=CRDetailOut)
def approve_cr(cid: int, body: DecisionIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Approve → dampak dihitung ulang dengan data terbaru → baseline versi baru dibuat."""
    if user.role not in CR_APPROVERS:
        raise HTTPException(403, "Hanya management/admin yang dapat approve Change Request")
    cr = _cr(db, user, cid)
    if cr.status != DocStatus.SUBMITTED:
        raise HTTPException(409, "Hanya CR submitted yang dapat di-approve")
    if cr.created_by_id == user.id:
        raise HTTPException(403, "Segregation of duties: pembuat CR tidak boleh meng-approve")
    p = load_project(db, user, cr.project_id)
    bl = _baseline_or_409(db, p)
    today = date.today()
    # hitung ulang dampak agar approval memakai rate & baseline terkini
    fresh, impact = change.analyze(db, p, bl, [_as_input(l) for l in cr.lines], cr.schedule_impact_days,
                                   cr.revenue_delta, today)
    for old, new in zip(cr.lines, fresh):
        old.rate, old.cost = new.rate, new.cost
    cr.md_impact, cr.cost_impact = impact["md_impact"], impact["cost_impact"]
    cr.impact = json.dumps(impact, default=audit._default)
    nb = change.apply(db, p, bl, cr, user.id, today)
    cr.status, cr.decided_by_id, cr.decided_at = DocStatus.APPROVED, user.id, utcnow()
    cr.decision_reason, cr.new_baseline_id = body.reason, nb.id
    audit.log(db, user, "approve", "change_request", cr.id, reason=body.reason,
              new={"new_baseline_version": nb.version, "bac": nb.bac, "end_date": nb.end_date})
    audit.log(db, user, "rebaseline", "baseline", nb.id, old={"version": bl.version, "bac": bl.bac},
              new={"version": nb.version, "bac": nb.bac}, reason=f"{cr.code} approved")
    db.commit()
    return _detail(cr)


@router.post("/change-requests/{cid}/reject", response_model=CROut)
def reject_cr(cid: int, body: DecisionIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if user.role not in CR_APPROVERS:
        raise HTTPException(403, "Hanya management/admin yang dapat reject Change Request")
    cr = _cr(db, user, cid)
    if cr.status != DocStatus.SUBMITTED:
        raise HTTPException(409, "Hanya CR submitted yang dapat di-reject")
    if not body.reason:
        raise HTTPException(422, "Alasan reject wajib diisi")
    cr.status, cr.decided_by_id, cr.decided_at, cr.decision_reason = DocStatus.REJECTED, user.id, utcnow(), body.reason
    audit.log(db, user, "reject", "change_request", cr.id, reason=body.reason)
    db.commit()
    return cr
