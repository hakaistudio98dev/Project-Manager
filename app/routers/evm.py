import json
from datetime import date, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import audit
from ..database import get_db
from ..models import (WBS, Baseline, BaselineItem, Budget, DocStatus, EVMSnapshot, ProgressMethod,
                      Project, ProjectStatus, Role, Task, User)
from ..schemas import BaselineCreateIn, BaselineDetailOut, BaselineOut, SnapshotIn, SnapshotOut
from ..security import get_current_user, load_project, require_roles
from ..services import evm

router = APIRouter(prefix="/api", tags=["Baseline, EVM & Forecast"])
FIN_ROLES = (Role.ADMIN, Role.MANAGEMENT, Role.PM, Role.FINANCE)


def _method(method: str) -> str:
    if method not in evm.METHODS:
        raise HTTPException(422, f"method harus salah satu dari: {', '.join(evm.METHODS)}")
    return method


# ---------------- Baseline ----------------
@router.post("/projects/{pid}/baseline", response_model=BaselineDetailOut, status_code=201)
def create_baseline(pid: int, body: BaselineCreateIn, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    """Bekukan baseline awal dari budget approved. Perubahan selanjutnya WAJIB lewat Change Request."""
    p = load_project(db, user, pid, manage=True)
    if evm.active_baseline(db, p.id):
        raise HTTPException(409, "Baseline aktif sudah ada. Perubahan baseline hanya melalui Change Request")
    budget = db.scalar(select(Budget).where(Budget.project_id == p.id, Budget.status == DocStatus.APPROVED))
    if not budget:
        raise HTTPException(422, "Belum ada budget approved")
    if not (p.start_date and p.end_date):
        raise HTTPException(422, "Project wajib memiliki start_date dan end_date sebelum baseline")

    wbs = {w.id: w for w in db.scalars(select(WBS).where(WBS.project_id == p.id))}
    tasks = {t.id: t for t in db.scalars(select(Task).where(Task.project_id == p.id))}
    ranges = {}
    for t in tasks.values():
        s, e = t.planned_start or p.start_date, t.planned_end or p.end_date
        lo, hi = ranges.get(t.wbs_id, (s, e))
        ranges[t.wbs_id] = (min(lo, s), max(hi, e))

    bl = Baseline(project_id=p.id, version=1, budget_id=budget.id, bac=budget.cost_baseline,
                  start_date=p.start_date, end_date=p.end_date, reason=body.reason or "Baseline awal",
                  created_by_id=user.id)
    for l in budget.lines:
        w = wbs.get(l.wbs_id)
        method = w.progress_method if w else ProgressMethod.PHYSICAL
        if l.task_id:
            t = tasks[l.task_id]
            start, end = t.planned_start or p.start_date, t.planned_end or p.end_date
        else:
            start, end = ranges.get(l.wbs_id, (p.start_date, p.end_date))
        if end < start:
            raise HTTPException(422, f"Tanggal rencana tidak valid pada baris budget #{l.id}")
        bl.items.append(BaselineItem(wbs_id=l.wbs_id, task_id=l.task_id, employee_id=l.employee_id,
                                     category=l.category, description=l.description, planned_start=start,
                                     planned_end=end, md=l.md, rate=l.rate, amount=l.amount,
                                     progress_method=method))
    db.add(bl)
    db.flush()
    audit.log(db, user, "create", "baseline", bl.id,
              new={"version": 1, "bac": bl.bac, "budget_version": budget.version}, reason=body.reason)
    db.commit()
    return bl


@router.get("/projects/{pid}/baseline", response_model=BaselineDetailOut)
def get_active_baseline(pid: int, db: Session = Depends(get_db), user: User = Depends(require_roles(*FIN_ROLES))):
    p = load_project(db, user, pid)
    bl = evm.active_baseline(db, p.id)
    if not bl:
        raise HTTPException(404, "Project belum memiliki baseline")
    return bl


@router.get("/projects/{pid}/baselines", response_model=list[BaselineOut])
def list_baselines(pid: int, db: Session = Depends(get_db), user: User = Depends(require_roles(*FIN_ROLES))):
    p = load_project(db, user, pid)
    return db.scalars(select(Baseline).where(Baseline.project_id == p.id).order_by(Baseline.version)).all()


# ---------------- EVM & Forecast ----------------
def _compute(db, p, as_of, method):
    r = evm.compute(db, p, as_of or date.today(), _method(method))
    if not r:
        raise HTTPException(409, "Project belum memiliki baseline; buat baseline terlebih dahulu")
    return r


@router.get("/projects/{pid}/evm")
def get_evm(pid: int, as_of: date | None = None, method: str = "cpi", db: Session = Depends(get_db),
            user: User = Depends(require_roles(*FIN_ROLES))):
    """EVM live (tidak disimpan). Catatan: EV memakai progress task saat ini."""
    p = load_project(db, user, pid)
    return _compute(db, p, as_of, method).as_dict()


@router.get("/projects/{pid}/forecast")
def get_forecast(pid: int, as_of: date | None = None, db: Session = Depends(get_db),
                 user: User = Depends(require_roles(*FIN_ROLES))):
    p = load_project(db, user, pid)
    r = _compute(db, p, as_of, "cpi")
    return {"as_of": r.as_of, "bac": r.bac, "ev": r.ev, "ac": r.ac, "cpi": r.cpi, "spi": r.spi,
            "tcpi_bac": r.tcpi, "methods": r.forecasts, "warnings": r.warnings}


def _save_snapshot(db, p, r: evm.EVMResult, source, user_id) -> EVMSnapshot:
    s = r.selected
    snap = EVMSnapshot(project_id=p.id, baseline_id=r.baseline_id, baseline_version=r.baseline_version,
                       as_of=r.as_of, bac=r.bac, pv=r.pv, ev=r.ev, ac=r.ac, sv=r.sv, cv=r.cv, spi=r.spi,
                       cpi=r.cpi, eac=s["eac"], etc=s["etc"], vac=s["vac"], tcpi=r.tcpi, eac_method=r.method,
                       planned_pct=r.planned_pct, earned_pct=r.earned_pct, source=source,
                       detail=json.dumps({"formula": s["formula"], "forecasts": r.forecasts, "by_wbs": r.by_wbs},
                                         default=audit._default),
                       created_by_id=user_id)
    db.add(snap)
    return snap


@router.post("/projects/{pid}/evm/snapshots", response_model=SnapshotOut, status_code=201)
def create_snapshot(pid: int, body: SnapshotIn, db: Session = Depends(get_db),
                    user: User = Depends(require_roles(Role.ADMIN, Role.MANAGEMENT, Role.PM))):
    p = load_project(db, user, pid, manage=True)
    r = _compute(db, p, body.as_of, body.method)
    snap = _save_snapshot(db, p, r, "user", user.id)
    db.flush()
    audit.log(db, user, "create", "evm_snapshot", snap.id, new={"as_of": r.as_of, "cpi": r.cpi, "spi": r.spi})
    db.commit()
    return snap


@router.get("/projects/{pid}/evm/snapshots", response_model=list[SnapshotOut])
def list_snapshots(pid: int, db: Session = Depends(get_db), user: User = Depends(require_roles(*FIN_ROLES))):
    p = load_project(db, user, pid)
    return db.scalars(select(EVMSnapshot).where(EVMSnapshot.project_id == p.id)
                      .order_by(EVMSnapshot.as_of, EVMSnapshot.id)).all()


@router.post("/evm/snapshots/run-all")
def run_all_snapshots(method: str = "cpi", db: Session = Depends(get_db),
                      user: User = Depends(require_roles(Role.ADMIN))):
    """Snapshot periodik semua project aktif (panggil via cron/scheduler; engine otomatis di Tahap 4)."""
    today = date.today()
    done, skipped = [], []
    for p in db.scalars(select(Project).where(Project.org_id == user.org_id, Project.is_deleted.is_(False),
                                              Project.status == ProjectStatus.ACTIVE)).all():
        exists = db.scalar(select(EVMSnapshot).where(EVMSnapshot.project_id == p.id, EVMSnapshot.as_of == today,
                                                     EVMSnapshot.source == "system"))
        r = None if exists else evm.compute(db, p, today, _method(method))
        if r:
            _save_snapshot(db, p, r, "system", user.id)
            done.append(p.code)
        else:
            skipped.append(p.code)
    db.commit()
    return {"as_of": today, "snapshotted": done, "skipped": skipped}


def _points(start: date, end: date, interval: str) -> list[date]:
    pts, cur = [], start
    if interval == "month":
        while cur <= end:
            nxt = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
            pts.append(min(nxt - timedelta(days=1), end))
            cur = nxt
    else:
        step = 7 if interval == "week" else 1
        while cur <= end:
            pts.append(cur)
            cur += timedelta(days=step)
    if not pts or pts[-1] != end:
        pts.append(end)
    return pts


@router.get("/projects/{pid}/evm/curve")
def evm_curve(pid: int, interval: str = "week", db: Session = Depends(get_db),
              user: User = Depends(require_roles(*FIN_ROLES))):
    """Data S-curve PV/EV/AC + tren CPI/SPI. EV historis diambil dari snapshot."""
    if interval not in ("day", "week", "month"):
        raise HTTPException(422, "interval: day | week | month")
    p = load_project(db, user, pid)
    bl = evm.active_baseline(db, p.id)
    if not bl:
        raise HTTPException(409, "Project belum memiliki baseline")
    today = date.today()
    snaps = db.scalars(select(EVMSnapshot).where(EVMSnapshot.project_id == p.id)
                       .order_by(EVMSnapshot.as_of, EVMSnapshot.id)).all()
    ev_map = {s.as_of: s for s in snaps}  # yang terakhir per tanggal menang
    live = evm.compute(db, p, today)
    dates = set(_points(bl.start_date, bl.end_date, interval)) | set(ev_map) | {today}
    out = []
    for d in sorted(dates):
        s = ev_map.get(d)
        ev = s.ev if s else (live.ev if d == today else None)
        out.append({"date": d, "pv": evm.pv_at(bl, d, live.bac),
                    "ac": evm.ac_at(db, p.id, d) if d <= today else None, "ev": ev})
    trend = [{"date": s.as_of, "cpi": s.cpi, "spi": s.spi, "eac": s.eac} for s in ev_map.values()]
    return {"bac": live.bac, "baseline_version": bl.version, "start": bl.start_date, "end": bl.end_date,
            "today": today, "points": out, "trend": trend}
