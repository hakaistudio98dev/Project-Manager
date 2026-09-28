from collections import defaultdict
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import audit
from ..database import get_db
from ..models import (WBS, Customer, DocStatus, Employee, Project, ProjectMember, Role, Task,
                      TaskDependency, Timesheet, User)
from ..schemas import (DependencyIn, MemberAddOut, MemberIn, MemberOut, ProjectIn, ProjectOut,
                       ProjectUpdate, TaskIn, TaskOut, TaskUpdate, WBSIn, WBSNode, WBSOut)
from ..security import PROJECT_MANAGE, employee_of, get_current_user, load_project, require_roles
from ..services.allocation import allocation_for

router = APIRouter(prefix="/api", tags=["Project, WBS & Task"])

# PRD §8.2 — struktur WBS standar software project
STANDARD_WBS = ["Initiation", "Requirement", "UI/UX", "Backend", "Frontend", "DevOps",
                "QA", "UAT", "Deployment", "Handover"]


def _out(p: Project, user: User) -> ProjectOut:
    o = ProjectOut.model_validate(p)
    if user.role == Role.STAFF:  # nilai kontrak disembunyikan dari staff
        o.contract_value = None
        o.target_margin = None
    return o


def _check_refs(db, user, customer_id=None, pm_id=None):
    if customer_id:
        c = db.get(Customer, customer_id)
        if not c or c.org_id != user.org_id or c.is_deleted:
            raise HTTPException(422, "customer_id tidak valid")
    if pm_id:
        pm = db.get(User, pm_id)
        if not pm or pm.org_id != user.org_id or pm.role not in PROJECT_MANAGE:
            raise HTTPException(422, "pm_id harus user dengan role pm/management/admin")


# ---------- Project ----------
@router.post("/projects", response_model=ProjectOut, status_code=201)
def create_project(body: ProjectIn, db: Session = Depends(get_db),
                   user: User = Depends(require_roles(*PROJECT_MANAGE))):
    if db.scalar(select(Project).where(Project.org_id == user.org_id, Project.code == body.code)):
        raise HTTPException(409, "Project code sudah dipakai")
    data = body.model_dump()
    if user.role == Role.PM:
        data["pm_id"] = user.id
    _check_refs(db, user, data["customer_id"], data["pm_id"])
    p = Project(org_id=user.org_id, **data)
    db.add(p)
    db.flush()
    audit.log(db, user, "create", "project", p.id, new=audit.snapshot(p))
    db.commit()
    return _out(p, user)


@router.get("/projects", response_model=list[ProjectOut])
def list_projects(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    q = select(Project).where(Project.org_id == user.org_id, Project.is_deleted.is_(False))
    if user.role == Role.PM:
        q = q.where(Project.pm_id == user.id)
    elif user.role == Role.STAFF:
        emp = employee_of(db, user)
        if not emp:
            return []
        q = q.join(ProjectMember, ProjectMember.project_id == Project.id).where(
            ProjectMember.employee_id == emp.id)
    return [_out(p, user) for p in db.scalars(q.order_by(Project.id)).all()]


@router.get("/projects/{pid}", response_model=ProjectOut)
def get_project(pid: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _out(load_project(db, user, pid), user)


@router.patch("/projects/{pid}", response_model=ProjectOut)
def update_project(pid: int, body: ProjectUpdate, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    p = load_project(db, user, pid, manage=True)
    data = body.model_dump(exclude_unset=True)
    if "pm_id" in data and user.role == Role.PM:
        raise HTTPException(403, "PM tidak bisa memindahkan project ke PM lain")
    _check_refs(db, user, data.get("customer_id"), data.get("pm_id"))
    old = audit.snapshot(p)
    for k, v in data.items():
        setattr(p, k, v)
    if p.start_date and p.end_date and p.end_date < p.start_date:
        raise HTTPException(422, "end_date harus >= start_date")
    audit.log(db, user, "update", "project", p.id, old=old, new=audit.snapshot(p))
    db.commit()
    return _out(p, user)


# ---------- Members / Resource ----------
@router.post("/projects/{pid}/members", response_model=MemberAddOut, status_code=201)
def add_member(pid: int, body: MemberIn, db: Session = Depends(get_db),
               user: User = Depends(get_current_user)):
    p = load_project(db, user, pid, manage=True)
    e = db.get(Employee, body.employee_id)
    if not e or e.org_id != user.org_id or not e.is_active:
        raise HTTPException(422, "employee_id tidak valid")
    if db.scalar(select(ProjectMember).where(ProjectMember.project_id == p.id,
                                             ProjectMember.employee_id == e.id)):
        raise HTTPException(409, "Employee sudah menjadi member")
    m = ProjectMember(project_id=p.id, **body.model_dump())
    db.add(m)
    db.flush()
    audit.log(db, user, "create", "project_member", m.id, new=audit.snapshot(m))
    db.commit()
    check_date = body.start_date or p.start_date or date.today()
    total, _ = allocation_for(db, e.id, check_date)
    return MemberAddOut(**MemberOut.model_validate(m).model_dump(),
                        total_allocation_pct=total, over_allocated=total > 100)


@router.get("/projects/{pid}/members", response_model=list[MemberOut])
def list_members(pid: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    p = load_project(db, user, pid)
    return db.scalars(select(ProjectMember).where(ProjectMember.project_id == p.id)).all()


@router.get("/projects/{pid}/resources")
def resource_summary(pid: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Ringkasan per member: alokasi, planned MD (dari task), actual MD (timesheet approved)."""
    p = load_project(db, user, pid)
    planned = dict(db.execute(select(Task.owner_employee_id, func.sum(Task.estimate_md))
                              .where(Task.project_id == p.id).group_by(Task.owner_employee_id)).all())
    actual = dict(db.execute(select(Timesheet.employee_id, func.sum(Timesheet.actual_md))
                             .where(Timesheet.project_id == p.id, Timesheet.status == DocStatus.APPROVED)
                             .group_by(Timesheet.employee_id)).all())
    rows = []
    today = date.today()
    for m, e in db.execute(select(ProjectMember, Employee).join(Employee, Employee.id == ProjectMember.employee_id)
                           .where(ProjectMember.project_id == p.id)).all():
        total, _ = allocation_for(db, e.id, today)
        rows.append({"employee_id": e.id, "name": e.name, "job_role": e.job_role,
                     "project_role": m.project_role, "allocation_pct": str(m.allocation_pct),
                     "total_allocation_pct_all_projects": str(total), "over_allocated": total > 100,
                     "planned_md": str(Decimal(planned.get(e.id) or 0)),
                     "actual_md": str(Decimal(actual.get(e.id) or 0))})
    unassigned = planned.get(None)
    return {"project_id": p.id, "members": rows,
            "unassigned_task_md": str(Decimal(unassigned or 0))}


# ---------- WBS ----------
def _next_code(db, project_id, parent: WBS | None) -> str:
    cond = (WBS.parent_id == parent.id) if parent else WBS.parent_id.is_(None)
    siblings = db.scalar(select(func.count(WBS.id)).where(WBS.project_id == project_id, cond))
    n = (siblings or 0) + 1
    return f"{parent.code}.{n}" if parent else str(n)


@router.post("/projects/{pid}/wbs", response_model=WBSOut, status_code=201)
def create_wbs(pid: int, body: WBSIn, db: Session = Depends(get_db),
               user: User = Depends(get_current_user)):
    p = load_project(db, user, pid, manage=True)
    parent = None
    if body.parent_id:
        parent = db.get(WBS, body.parent_id)
        if not parent or parent.project_id != p.id:
            raise HTTPException(422, "parent_id tidak valid")
    code = body.code or _next_code(db, p.id, parent)
    if db.scalar(select(WBS).where(WBS.project_id == p.id, WBS.code == code)):
        raise HTTPException(409, f"Kode WBS {code} sudah ada")
    w = WBS(project_id=p.id, parent_id=body.parent_id, code=code, name=body.name,
            sort_order=body.sort_order, progress_method=body.progress_method)
    db.add(w)
    db.flush()
    audit.log(db, user, "create", "wbs", w.id, new=audit.snapshot(w))
    db.commit()
    return w


@router.post("/projects/{pid}/wbs/template", response_model=list[WBSOut], status_code=201)
def apply_wbs_template(pid: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Buat 10 fase WBS standar (PRD §8.2). Hanya untuk project yang belum punya WBS."""
    p = load_project(db, user, pid, manage=True)
    if db.scalar(select(func.count(WBS.id)).where(WBS.project_id == p.id)):
        raise HTTPException(409, "Project sudah memiliki WBS")
    items = [WBS(project_id=p.id, code=str(i), name=n, sort_order=i)
             for i, n in enumerate(STANDARD_WBS, start=1)]
    db.add_all(items)
    db.flush()
    audit.log(db, user, "apply_template", "project", p.id, new={"wbs": STANDARD_WBS})
    db.commit()
    return items


@router.get("/projects/{pid}/wbs", response_model=list[WBSNode])
def wbs_tree(pid: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Tree WBS dengan roll-up estimate MD dan progress (tertimbang MD)."""
    p = load_project(db, user, pid)
    nodes = db.scalars(select(WBS).where(WBS.project_id == p.id)
                       .order_by(WBS.sort_order, WBS.code)).all()
    tasks = db.scalars(select(Task).where(Task.project_id == p.id)).all()
    by_wbs = defaultdict(list)
    for t in tasks:
        by_wbs[t.wbs_id].append(t)
    children = defaultdict(list)
    for n in nodes:
        children[n.parent_id].append(n)

    def build(n: WBS) -> tuple[WBSNode, Decimal, Decimal, int]:
        md = sum((t.estimate_md for t in by_wbs[n.id]), Decimal(0))
        earned = sum((t.estimate_md * t.progress_pct / 100 for t in by_wbs[n.id]), Decimal(0))
        count = len(by_wbs[n.id])
        kids = []
        for c in children[n.id]:
            node, cmd, cearned, ccount = build(c)
            kids.append(node)
            md, earned, count = md + cmd, earned + cearned, count + ccount
        prog = (earned / md * 100).quantize(Decimal("0.01")) if md else Decimal(0)
        return (WBSNode(**WBSOut.model_validate(n).model_dump(), estimate_md=md,
                        progress_pct=prog, task_count=count, children=kids), md, earned, count)

    return [build(n)[0] for n in children[None]]


# ---------- Task ----------
def _task(db, user, tid, manage=False) -> Task:
    t = db.get(Task, tid)
    if not t:
        raise HTTPException(404, "Task not found")
    load_project(db, user, t.project_id, manage=manage)
    return t


def _check_owner(db, project_id, owner_id):
    if owner_id and not db.scalar(select(ProjectMember).where(
            ProjectMember.project_id == project_id, ProjectMember.employee_id == owner_id)):
        raise HTTPException(422, "Owner task harus member project")


@router.post("/projects/{pid}/tasks", response_model=TaskOut, status_code=201)
def create_task(pid: int, body: TaskIn, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    p = load_project(db, user, pid, manage=True)
    w = db.get(WBS, body.wbs_id)
    if not w or w.project_id != p.id:
        raise HTTPException(422, "wbs_id tidak valid")
    if body.planned_start and body.planned_end and body.planned_end < body.planned_start:
        raise HTTPException(422, "planned_end harus >= planned_start")
    _check_owner(db, p.id, body.owner_employee_id)
    t = Task(project_id=p.id, **body.model_dump())
    db.add(t)
    db.flush()
    audit.log(db, user, "create", "task", t.id, new=audit.snapshot(t))
    db.commit()
    return t


@router.get("/projects/{pid}/tasks", response_model=list[TaskOut])
def list_tasks(pid: int, wbs_id: int | None = None, db: Session = Depends(get_db),
               user: User = Depends(get_current_user)):
    p = load_project(db, user, pid)
    q = select(Task).where(Task.project_id == p.id)
    if wbs_id:
        q = q.where(Task.wbs_id == wbs_id)
    return db.scalars(q.order_by(Task.id)).all()


@router.patch("/tasks/{tid}", response_model=TaskOut)
def update_task(tid: int, body: TaskUpdate, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    t = db.get(Task, tid)
    if not t:
        raise HTTPException(404, "Task not found")
    data = body.model_dump(exclude_unset=True)
    # Staff (owner) hanya boleh update status/progress/actual date task miliknya
    if user.role == Role.STAFF:
        load_project(db, user, t.project_id)
        emp = employee_of(db, user)
        allowed = {"status", "progress_pct", "actual_start", "actual_end"}
        if not emp or t.owner_employee_id != emp.id or set(data) - allowed:
            raise HTTPException(403, "Staff hanya bisa update progress task miliknya")
    else:
        load_project(db, user, t.project_id, manage=True)
    if "owner_employee_id" in data:
        _check_owner(db, t.project_id, data["owner_employee_id"])
    old = audit.snapshot(t)
    for k, v in data.items():
        setattr(t, k, v)
    if t.status.value == "done" and "progress_pct" not in data:
        t.progress_pct = Decimal(100)
    if t.status.value == "in_progress" and not t.actual_start:
        t.actual_start = date.today()
    audit.log(db, user, "update", "task", t.id, old=old, new=audit.snapshot(t))
    db.commit()
    return t


@router.post("/tasks/{tid}/dependencies", status_code=201)
def add_dependency(tid: int, body: DependencyIn, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    succ = _task(db, user, tid, manage=True)
    pred = db.get(Task, body.predecessor_id)
    if not pred or pred.project_id != succ.project_id or pred.id == succ.id:
        raise HTTPException(422, "predecessor tidak valid")
    # cegah siklus: telusuri predecessor dari pred, tidak boleh bertemu succ
    edges = defaultdict(list)
    for d in db.scalars(select(TaskDependency)).all():
        edges[d.successor_id].append(d.predecessor_id)
    stack, seen = [pred.id], set()
    while stack:
        cur = stack.pop()
        if cur == succ.id:
            raise HTTPException(422, "Dependency membuat siklus")
        if cur not in seen:
            seen.add(cur)
            stack.extend(edges[cur])
    d = TaskDependency(predecessor_id=pred.id, successor_id=succ.id, dep_type=body.dep_type)
    db.add(d)
    db.commit()
    return {"id": d.id, "predecessor_id": pred.id, "successor_id": succ.id, "dep_type": d.dep_type}
