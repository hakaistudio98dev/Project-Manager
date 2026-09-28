from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..database import get_db
from ..models import (Customer, Employee, Project, ProjectMember, ResourceRate, Role, User,
                      Vendor)
from ..schemas import (CapacityOut, CustomerIn, CustomerOut, EmployeeIn, EmployeeOut, RateIn,
                       RateOut, VendorIn, VendorOut)
from ..security import FINANCE_VIEW, get_current_user, require_roles
from ..services import costing
from ..services.allocation import allocation_for

router = APIRouter(prefix="/api", tags=["Master Data"])
SALES_SIDE = (Role.ADMIN, Role.MANAGEMENT, Role.SALES, Role.PM, Role.FINANCE)


# ----- Customers -----
def _customer(db, user, cid) -> Customer:
    c = db.get(Customer, cid)
    if not c or c.org_id != user.org_id or c.is_deleted:
        raise HTTPException(404, "Customer not found")
    return c


@router.post("/customers", response_model=CustomerOut, status_code=201)
def create_customer(body: CustomerIn, db: Session = Depends(get_db),
                    user: User = Depends(require_roles(*SALES_SIDE))):
    c = Customer(org_id=user.org_id, **body.model_dump())
    db.add(c)
    db.flush()
    audit.log(db, user, "create", "customer", c.id, new=body.model_dump())
    db.commit()
    return c


@router.get("/customers", response_model=list[CustomerOut])
def list_customers(db: Session = Depends(get_db), user: User = Depends(require_roles(*SALES_SIDE))):
    return db.scalars(select(Customer).where(Customer.org_id == user.org_id,
                                             Customer.is_deleted.is_(False))).all()


@router.put("/customers/{cid}", response_model=CustomerOut)
def update_customer(cid: int, body: CustomerIn, db: Session = Depends(get_db),
                    user: User = Depends(require_roles(*SALES_SIDE))):
    c = _customer(db, user, cid)
    old = audit.snapshot(c)
    for k, v in body.model_dump().items():
        setattr(c, k, v)
    audit.log(db, user, "update", "customer", c.id, old=old, new=audit.snapshot(c))
    db.commit()
    return c


@router.delete("/customers/{cid}", status_code=204)
def delete_customer(cid: int, db: Session = Depends(get_db),
                    user: User = Depends(require_roles(Role.ADMIN, Role.MANAGEMENT))):
    c = _customer(db, user, cid)
    c.is_deleted = True  # soft delete
    audit.log(db, user, "soft_delete", "customer", c.id)
    db.commit()


# ----- Vendors -----
@router.post("/vendors", response_model=VendorOut, status_code=201)
def create_vendor(body: VendorIn, db: Session = Depends(get_db),
                  user: User = Depends(require_roles(Role.ADMIN, Role.FINANCE, Role.PM, Role.MANAGEMENT))):
    v = Vendor(org_id=user.org_id, **body.model_dump())
    db.add(v)
    db.flush()
    audit.log(db, user, "create", "vendor", v.id, new=body.model_dump())
    db.commit()
    return v


@router.get("/vendors", response_model=list[VendorOut])
def list_vendors(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return db.scalars(select(Vendor).where(Vendor.org_id == user.org_id,
                                           Vendor.is_deleted.is_(False))).all()


# ----- Employees -----
def _employee(db, user, eid) -> Employee:
    e = db.get(Employee, eid)
    if not e or e.org_id != user.org_id:
        raise HTTPException(404, "Employee not found")
    return e


@router.post("/employees", response_model=EmployeeOut, status_code=201)
def create_employee(body: EmployeeIn, db: Session = Depends(get_db),
                    user: User = Depends(require_roles(*FINANCE_VIEW))):
    if body.user_id:
        u = db.get(User, body.user_id)
        if not u or u.org_id != user.org_id:
            raise HTTPException(422, "user_id tidak valid")
        if db.scalar(select(Employee).where(Employee.user_id == body.user_id)):
            raise HTTPException(409, "User sudah terhubung ke employee lain")
    e = Employee(org_id=user.org_id, **body.model_dump())
    db.add(e)
    db.flush()
    audit.log(db, user, "create", "employee", e.id, new=body.model_dump())
    db.commit()
    return e


@router.get("/employees", response_model=list[EmployeeOut])
def list_employees(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return db.scalars(select(Employee).where(Employee.org_id == user.org_id).order_by(Employee.id)).all()


@router.post("/employees/{eid}/rates", response_model=RateOut, status_code=201)
def add_rate(eid: int, body: RateIn, db: Session = Depends(get_db),
             user: User = Depends(require_roles(Role.ADMIN, Role.FINANCE))):
    """Tambah versi rate baru (effective-dated). Rate lama tidak diubah."""
    e = _employee(db, user, eid)
    if db.scalar(select(ResourceRate).where(ResourceRate.employee_id == e.id,
                                            ResourceRate.effective_date == body.effective_date)):
        raise HTTPException(409, "Rate untuk effective_date ini sudah ada; buat tanggal efektif baru")
    loaded = costing.loaded_monthly_cost(body.salary, body.benefits, body.equipment_allocation,
                                         body.software_allocation, body.other_cost)
    pmd = costing.productive_md(body.working_days, body.productive_capacity)
    r = ResourceRate(employee_id=e.id, **body.model_dump(), loaded_monthly_cost=loaded,
                     productive_md=pmd, cost_per_md=costing.cost_rate_per_md(loaded, pmd),
                     created_by_id=user.id)
    db.add(r)
    db.flush()
    audit.log(db, user, "create", "resource_rate", r.id, new=audit.snapshot(r))
    db.commit()
    return r


@router.get("/employees/{eid}/rates", response_model=list[RateOut])
def list_rates(eid: int, db: Session = Depends(get_db),
               user: User = Depends(require_roles(*FINANCE_VIEW))):
    e = _employee(db, user, eid)
    return e.rates


@router.get("/employees/{eid}/capacity", response_model=CapacityOut)
def capacity(eid: int, on: date | None = None, db: Session = Depends(get_db),
             user: User = Depends(require_roles(Role.ADMIN, Role.MANAGEMENT, Role.PM, Role.FINANCE))):
    e = _employee(db, user, eid)
    on = on or date.today()
    rate = costing.rate_effective_on(db, e.id, on)
    total, rows = allocation_for(db, e.id, on)
    return CapacityOut(employee_id=e.id, name=e.name, job_role=e.job_role, on_date=on,
                       productive_md_per_month=rate.productive_md if rate else None,
                       cost_per_md=rate.cost_per_md if rate else None,
                       total_allocation_pct=total, over_allocated=total > 100, allocations=rows)
