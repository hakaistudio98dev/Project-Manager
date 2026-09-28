from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Project, ProjectMember, ProjectStatus

INACTIVE = (ProjectStatus.CLOSED, ProjectStatus.CANCELLED)


def _active_on(m: ProjectMember, p: Project, on: date) -> bool:
    start = m.start_date or p.start_date
    end = m.end_date or p.end_date
    return (start is None or start <= on) and (end is None or on <= end)


def allocation_for(db: Session, employee_id: int, on: date) -> tuple[Decimal, list[dict]]:
    """Total alokasi (%) seorang employee di semua project aktif pada tanggal tertentu."""
    rows = db.execute(select(ProjectMember, Project).join(Project, Project.id == ProjectMember.project_id)
                      .where(ProjectMember.employee_id == employee_id,
                             Project.is_deleted.is_(False), Project.status.not_in(INACTIVE))).all()
    total, out = Decimal(0), []
    for m, p in rows:
        if _active_on(m, p, on):
            total += m.allocation_pct
            out.append({"project_id": p.id, "project_code": p.code,
                        "allocation_pct": str(m.allocation_pct)})
    return total, out
