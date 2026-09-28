"""Data model MVP 1 — mengikuti ERD PRD §23.

Prinsip governance yang diterapkan di level model:
- ResourceRate berversi (effective_date), tidak di-update, hanya ditambah versi baru.
- Budget berversi; versi approved bersifat immutable, versi lama menjadi 'superseded'.
- Timesheet menyimpan snapshot cost rate saat dihitung (tidak berubah bila rate berubah).
- Expense yang sudah posted tidak dihapus/diubah: koreksi via reversal (amount negatif).
- AuditLog untuk semua entitas kritis.
"""
import enum
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import (Boolean, Date, DateTime, Enum, ForeignKey, Integer, Numeric,
                        String, Text, UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base

Money = Numeric(18, 2)
Qty = Numeric(12, 4)
Pct = Numeric(7, 4)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _enum(e):
    return Enum(e, native_enum=False, values_callable=lambda x: [i.value for i in x], length=30)


# ---------- Enums ----------
class Role(str, enum.Enum):
    ADMIN = "admin"
    MANAGEMENT = "management"
    PM = "pm"
    FINANCE = "finance"
    SALES = "sales"
    STAFF = "staff"  # Developer / QA / UI-UX / DevOps


class ProjectStatus(str, enum.Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    ON_HOLD = "on_hold"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class ProgressMethod(str, enum.Enum):
    ZERO_HUNDRED = "0/100"
    FIFTY_FIFTY = "50/50"
    MILESTONE = "milestone_weighting"
    PHYSICAL = "physical_pct"
    WEIGHTED_DELIVERABLE = "weighted_deliverable"


class TaskStatus(str, enum.Enum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    DONE = "done"


class DocStatus(str, enum.Enum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"  # budget
    POSTED = "posted"          # expense


class ExpenseCategory(str, enum.Enum):
    CLOUD = "cloud"
    API = "api"
    HOSTING = "hosting"
    TRANSPORT = "transport"
    ACCOMMODATION = "accommodation"
    HARDWARE = "hardware"
    SOFTWARE = "software"
    VENDOR = "vendor"
    OTHER = "other"


# ---------- Organization & RBAC ----------
class Organization(Base):
    __tablename__ = "organizations"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    currency: Mapped[str] = mapped_column(String(3), default="IDR")
    timezone: Mapped[str] = mapped_column(String(50), default="Asia/Jakarta")
    standard_hours_per_day: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("8"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    email: Mapped[str] = mapped_column(String(200), unique=True)
    full_name: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(300))
    role: Mapped[Role] = mapped_column(_enum(Role))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ---------- Master data ----------
class Customer(Base):
    __tablename__ = "customers"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    billing_info: Mapped[str | None] = mapped_column(Text)
    tax_info: Mapped[str | None] = mapped_column(Text)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Vendor(Base):
    __tablename__ = "vendors"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    contact: Mapped[str | None] = mapped_column(Text)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)


class Employee(Base):
    __tablename__ = "employees"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    job_role: Mapped[str] = mapped_column(String(100))  # UI/UX, Frontend, Backend, DevOps, QA, PM
    department: Mapped[str | None] = mapped_column(String(100))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    rates: Mapped[list["ResourceRate"]] = relationship(back_populates="employee",
                                                       order_by="ResourceRate.effective_date")


class ResourceRate(Base):
    """PRD §9: Loaded Monthly Cost → Productive MD → Cost Rate / MD. Berversi via effective_date."""
    __tablename__ = "resource_rates"
    __table_args__ = (UniqueConstraint("employee_id", "effective_date"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    effective_date: Mapped[date] = mapped_column(Date)
    salary: Mapped[Decimal] = mapped_column(Money)
    benefits: Mapped[Decimal] = mapped_column(Money, default=0)
    equipment_allocation: Mapped[Decimal] = mapped_column(Money, default=0)
    software_allocation: Mapped[Decimal] = mapped_column(Money, default=0)
    other_cost: Mapped[Decimal] = mapped_column(Money, default=0)
    loaded_monthly_cost: Mapped[Decimal] = mapped_column(Money)
    working_days: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    productive_capacity: Mapped[Decimal] = mapped_column(Pct)
    productive_md: Mapped[Decimal] = mapped_column(Numeric(6, 2))
    cost_per_md: Mapped[Decimal] = mapped_column(Money)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    employee: Mapped[Employee] = relationship(back_populates="rates")


# ---------- Project / WBS / Task ----------
class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("org_id", "code"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    code: Mapped[str] = mapped_column(String(50))
    name: Mapped[str] = mapped_column(String(200))
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"))
    pm_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    project_type: Mapped[str | None] = mapped_column(String(100))
    contract_value: Mapped[Decimal] = mapped_column(Money, default=0)
    currency: Mapped[str] = mapped_column(String(3), default="IDR")
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[ProjectStatus] = mapped_column(_enum(ProjectStatus), default=ProjectStatus.DRAFT)
    priority: Mapped[str] = mapped_column(String(20), default="medium")
    target_margin: Mapped[Decimal | None] = mapped_column(Pct)
    tax_rate: Mapped[Decimal | None] = mapped_column(Pct)  # configurable, tidak di-hardcode
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ProjectMember(Base):
    __tablename__ = "project_members"
    __table_args__ = (UniqueConstraint("project_id", "employee_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    project_role: Mapped[str | None] = mapped_column(String(100))
    allocation_pct: Mapped[Decimal] = mapped_column(Numeric(6, 2), default=Decimal("100"))
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)


class WBS(Base):
    __tablename__ = "wbs"
    __table_args__ = (UniqueConstraint("project_id", "code"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("wbs.id"))
    code: Mapped[str] = mapped_column(String(50))
    name: Mapped[str] = mapped_column(String(200))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    progress_method: Mapped[ProgressMethod] = mapped_column(_enum(ProgressMethod),
                                                            default=ProgressMethod.PHYSICAL)


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    wbs_id: Mapped[int] = mapped_column(ForeignKey("wbs.id"), index=True)
    name: Mapped[str] = mapped_column(String(300))
    owner_employee_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id"))
    planned_start: Mapped[date | None] = mapped_column(Date)
    planned_end: Mapped[date | None] = mapped_column(Date)
    estimate_md: Mapped[Decimal] = mapped_column(Qty, default=0)
    status: Mapped[TaskStatus] = mapped_column(_enum(TaskStatus), default=TaskStatus.TODO)
    progress_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=0)
    actual_start: Mapped[date | None] = mapped_column(Date)
    actual_end: Mapped[date | None] = mapped_column(Date)
    acceptance_criteria: Mapped[str | None] = mapped_column(Text)
    is_milestone: Mapped[bool] = mapped_column(Boolean, default=False)


class TaskDependency(Base):
    __tablename__ = "task_dependencies"
    __table_args__ = (UniqueConstraint("predecessor_id", "successor_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    predecessor_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"))
    successor_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"))
    dep_type: Mapped[str] = mapped_column(String(2), default="FS")


# ---------- Budget ----------
class Budget(Base):
    """PRD §10: Direct Cost → +Contingency = Cost Baseline → +Management Reserve = Project Budget."""
    __tablename__ = "budgets"
    __table_args__ = (UniqueConstraint("project_id", "version"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[DocStatus] = mapped_column(_enum(DocStatus), default=DocStatus.DRAFT)
    labor_cost: Mapped[Decimal] = mapped_column(Money, default=0)
    expense_cost: Mapped[Decimal] = mapped_column(Money, default=0)   # termasuk procurement
    procurement_cost: Mapped[Decimal] = mapped_column(Money, default=0)
    direct_cost: Mapped[Decimal] = mapped_column(Money, default=0)
    contingency: Mapped[Decimal] = mapped_column(Money, default=0)
    cost_baseline: Mapped[Decimal] = mapped_column(Money, default=0)
    management_reserve: Mapped[Decimal] = mapped_column(Money, default=0)
    total_budget: Mapped[Decimal] = mapped_column(Money, default=0)
    total_md: Mapped[Decimal] = mapped_column(Qty, default=0)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    approved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    lines: Mapped[list["BudgetLine"]] = relationship(cascade="all, delete-orphan")


class BudgetLine(Base):
    __tablename__ = "budget_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    budget_id: Mapped[int] = mapped_column(ForeignKey("budgets.id"), index=True)
    wbs_id: Mapped[int | None] = mapped_column(ForeignKey("wbs.id"))
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id"))
    employee_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id"))
    category: Mapped[str] = mapped_column(String(30))  # 'labor' atau ExpenseCategory
    description: Mapped[str | None] = mapped_column(String(300))
    procurement_item_id: Mapped[int | None] = mapped_column(ForeignKey("procurement_items.id"))
    qty: Mapped[Decimal | None] = mapped_column(Qty)
    unit: Mapped[str | None] = mapped_column(String(20))
    md: Mapped[Decimal | None] = mapped_column(Qty)
    rate: Mapped[Decimal | None] = mapped_column(Money)
    amount: Mapped[Decimal] = mapped_column(Money)


# ---------- Actuals ----------
class Timesheet(Base):
    __tablename__ = "timesheets"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    wbs_id: Mapped[int] = mapped_column(ForeignKey("wbs.id"))
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id"))
    work_date: Mapped[date] = mapped_column(Date, index=True)
    hours: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[DocStatus] = mapped_column(_enum(DocStatus), default=DocStatus.DRAFT)
    actual_md: Mapped[Decimal] = mapped_column(Qty)
    rate_id: Mapped[int] = mapped_column(ForeignKey("resource_rates.id"))
    cost_rate: Mapped[Decimal] = mapped_column(Money)   # snapshot
    labor_cost: Mapped[Decimal] = mapped_column(Money)
    reject_reason: Mapped[str | None] = mapped_column(Text)
    approved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Expense(Base):
    __tablename__ = "expenses"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    wbs_id: Mapped[int | None] = mapped_column(ForeignKey("wbs.id"))
    vendor_id: Mapped[int | None] = mapped_column(ForeignKey("vendors.id"))
    category: Mapped[ExpenseCategory] = mapped_column(_enum(ExpenseCategory))
    amount: Mapped[Decimal] = mapped_column(Money)
    expense_date: Mapped[date] = mapped_column(Date)
    description: Mapped[str | None] = mapped_column(Text)
    reference: Mapped[str | None] = mapped_column(String(100))  # no. PO / invoice vendor
    status: Mapped[DocStatus] = mapped_column(_enum(DocStatus), default=DocStatus.DRAFT)
    reversal_of_id: Mapped[int | None] = mapped_column(ForeignKey("expenses.id"), unique=True)
    reject_reason: Mapped[str | None] = mapped_column(Text)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    approved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    posted_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PeriodLock(Base):
    __tablename__ = "period_locks"
    __table_args__ = (UniqueConstraint("org_id", "period"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    period: Mapped[str] = mapped_column(String(7))  # YYYY-MM
    locked_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    locked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(50))
    entity: Mapped[str] = mapped_column(String(50), index=True)
    entity_id: Mapped[int] = mapped_column(Integer, index=True)
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# =====================================================================
# TAHAP 2 (MVP 2): Baseline, EVM, Forecast, Risk, Change Request
# =====================================================================
class BaselineStatus(str, enum.Enum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"


class RiskStatus(str, enum.Enum):
    OPEN = "open"
    MITIGATING = "mitigating"
    OCCURRED = "occurred"
    CLOSED = "closed"


class CRLineType(str, enum.Enum):
    ADD_TASK = "add_task"
    CHANGE_TASK = "change_task"
    EXPENSE = "expense"


class Baseline(Base):
    """PMB yang dibekukan dari budget approved. Hanya berubah lewat Change Request (PRD §32)."""
    __tablename__ = "baselines"
    __table_args__ = (UniqueConstraint("project_id", "version"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    budget_id: Mapped[int] = mapped_column(ForeignKey("budgets.id"))
    change_request_id: Mapped[int | None] = mapped_column(ForeignKey("change_requests.id"))
    status: Mapped[BaselineStatus] = mapped_column(_enum(BaselineStatus), default=BaselineStatus.ACTIVE)
    bac: Mapped[Decimal] = mapped_column(Money)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    reason: Mapped[str | None] = mapped_column(Text)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    items: Mapped[list["BaselineItem"]] = relationship(cascade="all, delete-orphan")


class BaselineItem(Base):
    __tablename__ = "baseline_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    baseline_id: Mapped[int] = mapped_column(ForeignKey("baselines.id"), index=True)
    wbs_id: Mapped[int | None] = mapped_column(ForeignKey("wbs.id"))
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id"))
    employee_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id"))
    category: Mapped[str] = mapped_column(String(30))
    description: Mapped[str | None] = mapped_column(String(300))
    planned_start: Mapped[date] = mapped_column(Date)
    planned_end: Mapped[date] = mapped_column(Date)
    md: Mapped[Decimal | None] = mapped_column(Qty)
    rate: Mapped[Decimal | None] = mapped_column(Money)
    amount: Mapped[Decimal] = mapped_column(Money)
    progress_method: Mapped[ProgressMethod] = mapped_column(_enum(ProgressMethod))


class EVMSnapshot(Base):
    __tablename__ = "evm_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    baseline_id: Mapped[int] = mapped_column(ForeignKey("baselines.id"))
    baseline_version: Mapped[int] = mapped_column(Integer)
    as_of: Mapped[date] = mapped_column(Date, index=True)
    bac: Mapped[Decimal] = mapped_column(Money)
    pv: Mapped[Decimal] = mapped_column(Money)
    ev: Mapped[Decimal] = mapped_column(Money)
    ac: Mapped[Decimal] = mapped_column(Money)
    sv: Mapped[Decimal] = mapped_column(Money)
    cv: Mapped[Decimal] = mapped_column(Money)
    spi: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    cpi: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    eac: Mapped[Decimal] = mapped_column(Money)
    etc: Mapped[Decimal] = mapped_column(Money)
    vac: Mapped[Decimal] = mapped_column(Money)
    tcpi: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    eac_method: Mapped[str] = mapped_column(String(30))
    planned_pct: Mapped[Decimal] = mapped_column(Numeric(7, 4))
    earned_pct: Mapped[Decimal] = mapped_column(Numeric(7, 4))
    source: Mapped[str] = mapped_column(String(10), default="user")  # user / system
    detail: Mapped[str | None] = mapped_column(Text)  # JSON: input + breakdown WBS
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Risk(Base):
    __tablename__ = "risks"
    __table_args__ = (UniqueConstraint("project_id", "code"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    code: Mapped[str] = mapped_column(String(20))
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(50))
    probability: Mapped[int] = mapped_column(Integer)  # 1-5
    impact: Mapped[int] = mapped_column(Integer)       # 1-5
    score: Mapped[int] = mapped_column(Integer)
    level: Mapped[str] = mapped_column(String(10))
    owner_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    mitigation: Mapped[str | None] = mapped_column(Text)
    contingency_plan: Mapped[str | None] = mapped_column(Text)
    cost_impact: Mapped[Decimal | None] = mapped_column(Money)
    schedule_impact_days: Mapped[int | None] = mapped_column(Integer)
    due_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[RiskStatus] = mapped_column(_enum(RiskStatus), default=RiskStatus.OPEN)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ChangeRequest(Base):
    """PRD §12.1: CR → Scope → MD → Cost → Schedule → Margin Impact → Approval → Baseline Update."""
    __tablename__ = "change_requests"
    __table_args__ = (UniqueConstraint("project_id", "code"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    code: Mapped[str] = mapped_column(String(20))
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    requested_by: Mapped[str | None] = mapped_column(String(200))
    reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[DocStatus] = mapped_column(_enum(DocStatus), default=DocStatus.DRAFT)
    schedule_impact_days: Mapped[int] = mapped_column(Integer, default=0)
    revenue_delta: Mapped[Decimal] = mapped_column(Money, default=0)  # approved change order
    md_impact: Mapped[Decimal] = mapped_column(Qty, default=0)
    cost_impact: Mapped[Decimal] = mapped_column(Money, default=0)
    impact: Mapped[str | None] = mapped_column(Text)  # JSON analisis dampak terakhir
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    decided_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)
    decision_reason: Mapped[str | None] = mapped_column(Text)
    new_baseline_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    lines: Mapped[list["CRLine"]] = relationship(cascade="all, delete-orphan")


class CRLine(Base):
    __tablename__ = "change_request_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    change_request_id: Mapped[int] = mapped_column(ForeignKey("change_requests.id"), index=True)
    line_type: Mapped[CRLineType] = mapped_column(_enum(CRLineType))
    wbs_id: Mapped[int | None] = mapped_column(ForeignKey("wbs.id"))
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id"))
    name: Mapped[str | None] = mapped_column(String(300))
    employee_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id"))
    md_delta: Mapped[Decimal | None] = mapped_column(Qty)
    planned_start: Mapped[date | None] = mapped_column(Date)
    planned_end: Mapped[date | None] = mapped_column(Date)
    category: Mapped[str | None] = mapped_column(String(30))
    amount: Mapped[Decimal | None] = mapped_column(Money)
    rate: Mapped[Decimal | None] = mapped_column(Money)
    cost: Mapped[Decimal] = mapped_column(Money, default=0)


# =====================================================================
# TAHAP 3 (V1): Pricing, Tax, Quotation, Contract, Billing, Invoice, Payment
# =====================================================================
class TaxKind(str, enum.Enum):
    VAT = "vat"                # PPN — ditambahkan ke invoice, bukan revenue perusahaan
    WITHHOLDING = "withholding"  # PPh dipotong customer (mis. PPh 23) — mengurangi kas diterima


class QuoteStatus(str, enum.Enum):
    DRAFT = "draft"
    PENDING_FINANCE = "pending_finance"
    PENDING_MANAGEMENT = "pending_management"
    APPROVED = "approved"
    REJECTED = "rejected"
    SENT = "sent"
    ACCEPTED = "accepted"
    LOST = "lost"
    SUPERSEDED = "superseded"


class InvoiceStatus(str, enum.Enum):
    DRAFT = "draft"
    ISSUED = "issued"
    PARTIALLY_PAID = "partially_paid"
    PAID = "paid"
    CANCELLED = "cancelled"
    DISPUTED = "disputed"


class MilestoneStatus(str, enum.Enum):
    PLANNED = "planned"
    INVOICED = "invoiced"


class FinanceSetting(Base):
    """Konfigurasi keuangan per organisasi (tidak di-hardcode)."""
    __tablename__ = "finance_settings"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), unique=True)
    is_pkp: Mapped[bool] = mapped_column(Boolean, default=True)
    default_vat_code: Mapped[str | None] = mapped_column(String(30))
    default_wht_code: Mapped[str | None] = mapped_column(String(30))
    overhead_pct: Mapped[Decimal] = mapped_column(Pct, default=Decimal("0.10"))  # % dari direct cost
    quote_mgmt_threshold: Mapped[Decimal] = mapped_column(Money, default=Decimal("500000000"))
    price_rounding: Mapped[Decimal] = mapped_column(Money, default=Decimal("1000000"))
    payment_term_days: Mapped[int] = mapped_column(Integer, default=30)
    evm_bac_includes_contingency: Mapped[bool] = mapped_column(Boolean, default=True)
    opening_cash: Mapped[Decimal] = mapped_column(Money, default=0)


class TaxRate(Base):
    """Master tarif pajak ber-effective date. Contoh: PPN 12% dengan DPP nilai lain 11/12 (disimpan sebagai pecahan agar tepat)."""
    __tablename__ = "tax_rates"
    __table_args__ = (UniqueConstraint("org_id", "code", "effective_date"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    code: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[TaxKind] = mapped_column(_enum(TaxKind))
    rate: Mapped[Decimal] = mapped_column(Pct)
    dpp_num: Mapped[int] = mapped_column(Integer, default=1)   # DPP = harga × num/den (mis. 11/12)
    dpp_den: Mapped[int] = mapped_column(Integer, default=1)
    effective_date: Mapped[date] = mapped_column(Date)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)


class Quotation(Base):
    __tablename__ = "quotations"
    __table_args__ = (UniqueConstraint("org_id", "code", "version"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"))
    budget_id: Mapped[int] = mapped_column(ForeignKey("budgets.id"))
    code: Mapped[str] = mapped_column(String(30))
    version: Mapped[int] = mapped_column(Integer, default=1)
    title: Mapped[str] = mapped_column(String(300))
    scope: Mapped[str | None] = mapped_column(Text)
    deliverables: Mapped[str | None] = mapped_column(Text)
    timeline: Mapped[str | None] = mapped_column(Text)
    assumptions: Mapped[str | None] = mapped_column(Text)
    exclusions: Mapped[str | None] = mapped_column(Text)
    valid_until: Mapped[date | None] = mapped_column(Date)
    # cost basis (snapshot saat quotation dibuat)
    direct_cost: Mapped[Decimal] = mapped_column(Money)
    contingency: Mapped[Decimal] = mapped_column(Money)
    overhead: Mapped[Decimal] = mapped_column(Money)
    management_reserve: Mapped[Decimal] = mapped_column(Money, default=0)
    total_cost: Mapped[Decimal] = mapped_column(Money)
    target_margin: Mapped[Decimal] = mapped_column(Pct)
    procurement_margin: Mapped[Decimal | None] = mapped_column(Pct)  # margin khusus barang/lisensi
    list_price: Mapped[Decimal] = mapped_column(Money)       # dari cost / (1 − margin), dibulatkan
    discount: Mapped[Decimal] = mapped_column(Money, default=0)
    subtotal: Mapped[Decimal] = mapped_column(Money)         # harga jual sebelum pajak
    margin: Mapped[Decimal] = mapped_column(Pct)             # margin aktual setelah diskon
    vat_code: Mapped[str | None] = mapped_column(String(30))
    vat_rate: Mapped[Decimal] = mapped_column(Pct, default=0)
    vat_dpp_factor: Mapped[Decimal] = mapped_column(Numeric(10, 8), default=Decimal(1))
    vat_amount: Mapped[Decimal] = mapped_column(Money, default=0)
    total: Mapped[Decimal] = mapped_column(Money)
    wht_code: Mapped[str | None] = mapped_column(String(30))
    wht_rate: Mapped[Decimal] = mapped_column(Pct, default=0)
    wht_amount: Mapped[Decimal] = mapped_column(Money, default=0)
    status: Mapped[QuoteStatus] = mapped_column(_enum(QuoteStatus), default=QuoteStatus.DRAFT)
    requires_management: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    finance_approved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    management_approved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    decision_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    terms: Mapped[list["QuotationTerm"]] = relationship(cascade="all, delete-orphan",
                                                        order_by="QuotationTerm.seq")
    lines: Mapped[list["QuotationLine"]] = relationship(cascade="all, delete-orphan",
                                                        order_by="QuotationLine.seq")


class QuotationTerm(Base):
    __tablename__ = "quotation_terms"
    id: Mapped[int] = mapped_column(primary_key=True)
    quotation_id: Mapped[int] = mapped_column(ForeignKey("quotations.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(200))
    pct: Mapped[Decimal] = mapped_column(Pct)
    amount: Mapped[Decimal] = mapped_column(Money)
    offset_days: Mapped[int | None] = mapped_column(Integer)  # rencana tagih: hari setelah start project


class Contract(Base):
    __tablename__ = "contracts"
    __table_args__ = (UniqueConstraint("org_id", "contract_no"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), unique=True)
    quotation_id: Mapped[int | None] = mapped_column(ForeignKey("quotations.id"))
    contract_no: Mapped[str] = mapped_column(String(60))
    signed_date: Mapped[date] = mapped_column(Date)
    contract_value: Mapped[Decimal] = mapped_column(Money)  # sebelum pajak
    vat_code: Mapped[str | None] = mapped_column(String(30))
    wht_code: Mapped[str | None] = mapped_column(String(30))
    payment_term_days: Mapped[int] = mapped_column(Integer, default=30)
    sla: Mapped[str | None] = mapped_column(Text)
    warranty_months: Mapped[int | None] = mapped_column(Integer)
    change_mechanism: Mapped[str | None] = mapped_column(Text)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class BillingMilestone(Base):
    __tablename__ = "billing_milestones"
    id: Mapped[int] = mapped_column(primary_key=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("contracts.id"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(200))
    pct: Mapped[Decimal | None] = mapped_column(Pct)
    amount: Mapped[Decimal] = mapped_column(Money)
    planned_date: Mapped[date | None] = mapped_column(Date)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id"))  # syarat penagihan
    change_request_id: Mapped[int | None] = mapped_column(ForeignKey("change_requests.id"))
    status: Mapped[MilestoneStatus] = mapped_column(_enum(MilestoneStatus), default=MilestoneStatus.PLANNED)


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (UniqueConstraint("org_id", "invoice_no"),
                      UniqueConstraint("org_id", "idempotency_key"))
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    contract_id: Mapped[int | None] = mapped_column(ForeignKey("contracts.id"))
    milestone_id: Mapped[int | None] = mapped_column(ForeignKey("billing_milestones.id"), unique=True)
    invoice_no: Mapped[str | None] = mapped_column(String(40))  # diberikan saat issue
    description: Mapped[str] = mapped_column(String(300))
    issue_date: Mapped[date | None] = mapped_column(Date)
    due_date: Mapped[date | None] = mapped_column(Date)
    subtotal: Mapped[Decimal] = mapped_column(Money)
    vat_code: Mapped[str | None] = mapped_column(String(30))
    vat_rate: Mapped[Decimal] = mapped_column(Pct, default=0)
    dpp: Mapped[Decimal] = mapped_column(Money)
    vat_amount: Mapped[Decimal] = mapped_column(Money, default=0)
    total: Mapped[Decimal] = mapped_column(Money)
    wht_code: Mapped[str | None] = mapped_column(String(30))
    wht_rate: Mapped[Decimal] = mapped_column(Pct, default=0)
    wht_amount: Mapped[Decimal] = mapped_column(Money, default=0)  # perkiraan dipotong customer
    status: Mapped[InvoiceStatus] = mapped_column(_enum(InvoiceStatus), default=InvoiceStatus.DRAFT)
    status_reason: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(String(100))
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    issued_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Payment(Base):
    """Pelunasan = kas diterima + PPh dipotong (bukti potong). Koreksi via reversal, tidak dihapus."""
    __tablename__ = "payments"
    __table_args__ = (UniqueConstraint("org_id", "idempotency_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    payment_date: Mapped[date] = mapped_column(Date, index=True)
    amount: Mapped[Decimal] = mapped_column(Money)          # kas masuk
    wht_amount: Mapped[Decimal] = mapped_column(Money, default=0)  # PPh dipotong customer
    method: Mapped[str | None] = mapped_column(String(30))
    reference: Mapped[str | None] = mapped_column(String(100))
    reversal_of_id: Mapped[int | None] = mapped_column(ForeignKey("payments.id"), unique=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(100))
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# =====================================================================
# TAHAP 3b: Katalog, BOQ / Procurement per WBS, rincian penawaran
# =====================================================================
class ProcCategory(str, enum.Enum):
    HARDWARE = "hardware"
    SOFTWARE_LICENSE = "software_license"
    CLOUD_SUBSCRIPTION = "cloud_subscription"
    SUBCONTRACT = "subcontract"
    MATERIAL = "material"
    OTHER = "other"


# Pemetaan kategori pengadaan → kategori biaya aktual (expense)
PROC_TO_EXPENSE = {"hardware": "hardware", "software_license": "software", "cloud_subscription": "cloud",
                   "subcontract": "vendor", "material": "other", "other": "other"}


class ProcStatus(str, enum.Enum):
    PLANNED = "planned"
    ORDERED = "ordered"
    RECEIVED = "received"
    CANCELLED = "cancelled"


class CatalogItem(Base):
    """Master item pengadaan: harga beli acuan, satuan, vendor, lead time."""
    __tablename__ = "catalog_items"
    __table_args__ = (UniqueConstraint("org_id", "code"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    code: Mapped[str] = mapped_column(String(50))
    name: Mapped[str] = mapped_column(String(300))
    category: Mapped[ProcCategory] = mapped_column(_enum(ProcCategory))
    unit: Mapped[str] = mapped_column(String(20), default="unit")
    unit_cost: Mapped[Decimal] = mapped_column(Money)
    vendor_id: Mapped[int | None] = mapped_column(ForeignKey("vendors.id"))
    lead_time_days: Mapped[int] = mapped_column(Integer, default=0)
    spec: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ProcurementItem(Base):
    """Baris BOQ project: kebutuhan barang/lisensi/subkon per WBS."""
    __tablename__ = "procurement_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    wbs_id: Mapped[int] = mapped_column(ForeignKey("wbs.id"), index=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id"))
    catalog_item_id: Mapped[int | None] = mapped_column(ForeignKey("catalog_items.id"))
    name: Mapped[str] = mapped_column(String(300))
    category: Mapped[ProcCategory] = mapped_column(_enum(ProcCategory))
    spec: Mapped[str | None] = mapped_column(Text)
    qty: Mapped[Decimal] = mapped_column(Qty)
    unit: Mapped[str] = mapped_column(String(20), default="unit")
    unit_cost: Mapped[Decimal] = mapped_column(Money)          # estimasi harga beli
    vendor_id: Mapped[int | None] = mapped_column(ForeignKey("vendors.id"))
    lead_time_days: Mapped[int] = mapped_column(Integer, default=0)
    need_by_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[ProcStatus] = mapped_column(_enum(ProcStatus), default=ProcStatus.PLANNED)
    po_number: Mapped[str | None] = mapped_column(String(60))
    order_date: Mapped[date | None] = mapped_column(Date)
    actual_unit_cost: Mapped[Decimal | None] = mapped_column(Money)
    received_date: Mapped[date | None] = mapped_column(Date)
    expense_id: Mapped[int | None] = mapped_column(ForeignKey("expenses.id"))
    notes: Mapped[str | None] = mapped_column(Text)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class QuotationLine(Base):
    """Rincian harga penawaran per WBS (jasa + item pengadaan). unit_cost/cost = internal."""
    __tablename__ = "quotation_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    quotation_id: Mapped[int] = mapped_column(ForeignKey("quotations.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    wbs_id: Mapped[int | None] = mapped_column(ForeignKey("wbs.id"))
    wbs_code: Mapped[str | None] = mapped_column(String(50))
    wbs_name: Mapped[str | None] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20))   # service / procurement / expense
    description: Mapped[str] = mapped_column(String(300))
    qty: Mapped[Decimal] = mapped_column(Qty)
    unit: Mapped[str] = mapped_column(String(20))
    unit_cost: Mapped[Decimal] = mapped_column(Money)
    cost: Mapped[Decimal] = mapped_column(Money)
    unit_price: Mapped[Decimal] = mapped_column(Money)
    amount: Mapped[Decimal] = mapped_column(Money)
