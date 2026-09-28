from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from .models import (DocStatus, ExpenseCategory, ProgressMethod, ProjectStatus, Role,
                     TaskStatus)


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------- Auth ----------
class BootstrapIn(BaseModel):
    org_name: str
    admin_email: EmailStr
    admin_name: str
    admin_password: str = Field(min_length=8)


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserIn(BaseModel):
    email: EmailStr
    full_name: str
    password: str = Field(min_length=8)
    role: Role


class UserOut(ORM):
    id: int
    org_id: int
    email: str
    full_name: str
    role: Role
    is_active: bool


# ---------- Master ----------
class CustomerIn(BaseModel):
    name: str
    billing_info: str | None = None
    tax_info: str | None = None


class CustomerOut(ORM, CustomerIn):
    id: int


class VendorIn(BaseModel):
    name: str
    contact: str | None = None


class VendorOut(ORM, VendorIn):
    id: int


class EmployeeIn(BaseModel):
    name: str
    job_role: str
    department: str | None = None
    user_id: int | None = None


class EmployeeOut(ORM, EmployeeIn):
    id: int
    is_active: bool


class RateIn(BaseModel):
    effective_date: date
    salary: Decimal = Field(ge=0)
    benefits: Decimal = Field(default=Decimal(0), ge=0)
    equipment_allocation: Decimal = Field(default=Decimal(0), ge=0)
    software_allocation: Decimal = Field(default=Decimal(0), ge=0)
    other_cost: Decimal = Field(default=Decimal(0), ge=0)
    working_days: Decimal = Field(default=Decimal(20), gt=0, le=31)
    productive_capacity: Decimal = Field(default=Decimal("0.9"), gt=0, le=1)


class RateOut(ORM, RateIn):
    id: int
    employee_id: int
    loaded_monthly_cost: Decimal
    productive_md: Decimal
    cost_per_md: Decimal


class CapacityOut(BaseModel):
    employee_id: int
    name: str
    job_role: str
    on_date: date
    productive_md_per_month: Decimal | None
    cost_per_md: Decimal | None       # boleh dilihat PM untuk costing, tanpa detail gaji
    total_allocation_pct: Decimal
    over_allocated: bool
    allocations: list[dict]


# ---------- Project ----------
class ProjectIn(BaseModel):
    code: str
    name: str
    customer_id: int | None = None
    pm_id: int | None = None
    project_type: str | None = None
    contract_value: Decimal = Field(default=Decimal(0), ge=0)
    currency: str = "IDR"
    start_date: date | None = None
    end_date: date | None = None
    status: ProjectStatus = ProjectStatus.DRAFT
    priority: str = "medium"
    target_margin: Decimal | None = Field(default=None, ge=0, lt=1)
    tax_rate: Decimal | None = Field(default=None, ge=0, lt=1)

    @model_validator(mode="after")
    def _dates(self):
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("end_date harus >= start_date")
        return self


class ProjectUpdate(BaseModel):
    name: str | None = None
    customer_id: int | None = None
    pm_id: int | None = None
    project_type: str | None = None
    contract_value: Decimal | None = Field(default=None, ge=0)
    start_date: date | None = None
    end_date: date | None = None
    status: ProjectStatus | None = None
    priority: str | None = None
    target_margin: Decimal | None = Field(default=None, ge=0, lt=1)
    tax_rate: Decimal | None = Field(default=None, ge=0, lt=1)


class ProjectOut(ORM):
    id: int
    code: str
    name: str
    customer_id: int | None
    pm_id: int | None
    project_type: str | None
    contract_value: Decimal | None
    currency: str
    start_date: date | None
    end_date: date | None
    status: ProjectStatus
    priority: str
    target_margin: Decimal | None
    tax_rate: Decimal | None


class MemberIn(BaseModel):
    employee_id: int
    project_role: str | None = None
    allocation_pct: Decimal = Field(default=Decimal(100), gt=0, le=100)
    start_date: date | None = None
    end_date: date | None = None


class MemberOut(ORM, MemberIn):
    id: int
    project_id: int


class MemberAddOut(MemberOut):
    total_allocation_pct: Decimal
    over_allocated: bool


class WBSIn(BaseModel):
    name: str
    parent_id: int | None = None
    code: str | None = None  # auto bila kosong
    sort_order: int = 0
    progress_method: ProgressMethod = ProgressMethod.PHYSICAL


class WBSOut(ORM):
    id: int
    project_id: int
    parent_id: int | None
    code: str
    name: str
    sort_order: int
    progress_method: ProgressMethod


class WBSNode(WBSOut):
    estimate_md: Decimal
    progress_pct: Decimal
    task_count: int
    children: list["WBSNode"] = []


class TaskIn(BaseModel):
    wbs_id: int
    name: str
    owner_employee_id: int | None = None
    planned_start: date | None = None
    planned_end: date | None = None
    estimate_md: Decimal = Field(default=Decimal(0), ge=0)
    acceptance_criteria: str | None = None
    is_milestone: bool = False


class TaskUpdate(BaseModel):
    name: str | None = None
    owner_employee_id: int | None = None
    planned_start: date | None = None
    planned_end: date | None = None
    estimate_md: Decimal | None = Field(default=None, ge=0)
    status: TaskStatus | None = None
    progress_pct: Decimal | None = Field(default=None, ge=0, le=100)
    actual_start: date | None = None
    actual_end: date | None = None
    acceptance_criteria: str | None = None


class TaskOut(ORM):
    id: int
    project_id: int
    wbs_id: int
    name: str
    owner_employee_id: int | None
    planned_start: date | None
    planned_end: date | None
    estimate_md: Decimal
    status: TaskStatus
    progress_pct: Decimal
    actual_start: date | None
    actual_end: date | None
    acceptance_criteria: str | None
    is_milestone: bool


class DependencyIn(BaseModel):
    predecessor_id: int
    dep_type: str = Field(default="FS", pattern="^(FS|SS|FF|SF)$")


# ---------- Budget ----------
class PlannedExpenseIn(BaseModel):
    category: ExpenseCategory
    amount: Decimal = Field(gt=0)
    wbs_id: int | None = None
    description: str | None = None


class BudgetGenerateIn(BaseModel):
    planned_expenses: list[PlannedExpenseIn] = []
    contingency_pct: Decimal | None = Field(default=None, ge=0, le=1)
    contingency_amount: Decimal | None = Field(default=None, ge=0)
    management_reserve_amount: Decimal = Field(default=Decimal(0), ge=0)
    notes: str | None = None

    @model_validator(mode="after")
    def _one(self):
        if self.contingency_pct is not None and self.contingency_amount is not None:
            raise ValueError("Isi salah satu: contingency_pct atau contingency_amount")
        return self


class BudgetLineOut(ORM):
    id: int
    wbs_id: int | None
    task_id: int | None
    employee_id: int | None
    category: str
    description: str | None
    procurement_item_id: int | None = None
    qty: Decimal | None = None
    unit: str | None = None
    md: Decimal | None
    rate: Decimal | None
    amount: Decimal


class BudgetOut(ORM):
    id: int
    project_id: int
    version: int
    status: DocStatus
    total_md: Decimal
    labor_cost: Decimal
    expense_cost: Decimal
    procurement_cost: Decimal = Decimal(0)
    direct_cost: Decimal
    contingency: Decimal
    cost_baseline: Decimal
    management_reserve: Decimal
    total_budget: Decimal
    notes: str | None
    approved_by_id: int | None
    approved_at: datetime | None
    created_at: datetime


class BudgetDetailOut(BudgetOut):
    lines: list[BudgetLineOut]


class DecisionIn(BaseModel):
    reason: str | None = None


# ---------- Timesheet & Expense ----------
class TimesheetIn(BaseModel):
    project_id: int
    wbs_id: int | None = None
    task_id: int | None = None
    employee_id: int | None = None  # kosong = diri sendiri
    work_date: date
    hours: Decimal = Field(gt=0, le=24)
    description: str | None = None

    @model_validator(mode="after")
    def _target(self):
        if self.wbs_id is None and self.task_id is None:
            raise ValueError("Isi wbs_id atau task_id")
        return self


class TimesheetUpdate(BaseModel):
    work_date: date | None = None
    hours: Decimal | None = Field(default=None, gt=0, le=24)
    description: str | None = None
    task_id: int | None = None


class TimesheetOut(ORM):
    id: int
    employee_id: int
    project_id: int
    wbs_id: int
    task_id: int | None
    work_date: date
    hours: Decimal
    description: str | None
    status: DocStatus
    actual_md: Decimal
    cost_rate: Decimal | None = None
    labor_cost: Decimal | None = None
    reject_reason: str | None


class ExpenseIn(BaseModel):
    project_id: int
    wbs_id: int | None = None
    vendor_id: int | None = None
    category: ExpenseCategory
    amount: Decimal = Field(gt=0)
    expense_date: date
    description: str | None = None
    reference: str | None = None


class ExpenseOut(ORM):
    id: int
    project_id: int
    wbs_id: int | None
    vendor_id: int | None
    category: ExpenseCategory
    amount: Decimal
    expense_date: date
    description: str | None
    reference: str | None
    status: DocStatus
    reversal_of_id: int | None
    reject_reason: str | None
    posted_at: datetime | None


class PeriodIn(BaseModel):
    period: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    reason: str | None = None


class PeriodOut(ORM):
    id: int
    period: str
    locked_by_id: int | None
    locked_at: datetime


class AuditOut(ORM):
    id: int
    actor_id: int | None
    action: str
    entity: str
    entity_id: int
    old_value: str | None
    new_value: str | None
    reason: str | None
    created_at: datetime


# =====================================================================
# TAHAP 2
# =====================================================================
from .models import BaselineStatus, CRLineType, RiskStatus  # noqa: E402


class BaselineCreateIn(BaseModel):
    reason: str | None = None


class BaselineItemOut(ORM):
    id: int
    wbs_id: int | None
    task_id: int | None
    employee_id: int | None
    category: str
    description: str | None
    planned_start: date
    planned_end: date
    md: Decimal | None
    rate: Decimal | None
    amount: Decimal
    progress_method: ProgressMethod


class BaselineOut(ORM):
    id: int
    project_id: int
    version: int
    budget_id: int
    change_request_id: int | None
    status: BaselineStatus
    bac: Decimal
    start_date: date
    end_date: date
    reason: str | None
    created_at: datetime


class BaselineDetailOut(BaselineOut):
    items: list[BaselineItemOut]


class SnapshotIn(BaseModel):
    as_of: date | None = None
    method: str = "cpi"


class SnapshotOut(ORM):
    id: int
    project_id: int
    baseline_version: int
    as_of: date
    bac: Decimal
    pv: Decimal
    ev: Decimal
    ac: Decimal
    sv: Decimal
    cv: Decimal
    spi: Decimal | None
    cpi: Decimal | None
    eac: Decimal
    etc: Decimal
    vac: Decimal
    tcpi: Decimal | None
    eac_method: str
    planned_pct: Decimal
    earned_pct: Decimal
    source: str
    created_at: datetime


class RiskIn(BaseModel):
    title: str
    description: str | None = None
    category: str | None = None
    probability: int = Field(ge=1, le=5)
    impact: int = Field(ge=1, le=5)
    owner_user_id: int | None = None
    mitigation: str | None = None
    contingency_plan: str | None = None
    cost_impact: Decimal | None = Field(default=None, ge=0)
    schedule_impact_days: int | None = Field(default=None, ge=0)
    due_date: date | None = None


class RiskUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    category: str | None = None
    probability: int | None = Field(default=None, ge=1, le=5)
    impact: int | None = Field(default=None, ge=1, le=5)
    owner_user_id: int | None = None
    mitigation: str | None = None
    contingency_plan: str | None = None
    cost_impact: Decimal | None = Field(default=None, ge=0)
    schedule_impact_days: int | None = Field(default=None, ge=0)
    due_date: date | None = None
    status: RiskStatus | None = None


class RiskOut(ORM, RiskIn):
    id: int
    project_id: int
    code: str
    score: int
    level: str
    status: RiskStatus


class CRLineIn(BaseModel):
    line_type: CRLineType
    wbs_id: int | None = None          # add_task / expense
    task_id: int | None = None         # change_task
    name: str | None = None            # add_task
    employee_id: int | None = None     # add_task
    md_delta: Decimal | None = None    # add_task (>0) / change_task (±)
    planned_start: date | None = None
    planned_end: date | None = None
    category: ExpenseCategory | None = None  # expense
    amount: Decimal | None = None             # expense (±)

    @model_validator(mode="after")
    def _check(self):
        t = self.line_type
        if t == CRLineType.ADD_TASK:
            if not (self.wbs_id and self.name and self.employee_id and self.md_delta and self.md_delta > 0):
                raise ValueError("add_task wajib: wbs_id, name, employee_id, md_delta > 0")
            if self.planned_start and self.planned_end and self.planned_end < self.planned_start:
                raise ValueError("planned_end harus >= planned_start")
        elif t == CRLineType.CHANGE_TASK:
            if not (self.task_id and self.md_delta):
                raise ValueError("change_task wajib: task_id dan md_delta ≠ 0")
        elif not (self.category and self.amount):
            raise ValueError("expense wajib: category dan amount ≠ 0")
        return self


class CRIn(BaseModel):
    title: str
    description: str | None = None
    requested_by: str | None = None
    reason: str | None = None
    schedule_impact_days: int = 0
    revenue_delta: Decimal = Decimal(0)
    lines: list[CRLineIn] = []


class CRLineOut(ORM):
    id: int
    line_type: CRLineType
    wbs_id: int | None
    task_id: int | None
    name: str | None
    employee_id: int | None
    md_delta: Decimal | None
    planned_start: date | None
    planned_end: date | None
    category: str | None
    amount: Decimal | None
    rate: Decimal | None
    cost: Decimal


class CROut(ORM):
    id: int
    project_id: int
    code: str
    title: str
    description: str | None
    requested_by: str | None
    reason: str | None
    status: DocStatus
    schedule_impact_days: int
    revenue_delta: Decimal
    md_impact: Decimal
    cost_impact: Decimal
    decided_by_id: int | None
    decided_at: datetime | None
    decision_reason: str | None
    new_baseline_id: int | None
    created_at: datetime


class CRDetailOut(CROut):
    lines: list[CRLineOut]
    impact: dict | None = None


# =====================================================================
# TAHAP 3
# =====================================================================
from .models import InvoiceStatus, MilestoneStatus, QuoteStatus, TaxKind  # noqa: E402


class FinanceSettingIn(BaseModel):
    is_pkp: bool | None = None
    default_vat_code: str | None = None
    default_wht_code: str | None = None
    overhead_pct: Decimal | None = Field(default=None, ge=0, lt=1)
    quote_mgmt_threshold: Decimal | None = Field(default=None, ge=0)
    price_rounding: Decimal | None = Field(default=None, ge=0)
    payment_term_days: int | None = Field(default=None, ge=0, le=365)
    evm_bac_includes_contingency: bool | None = None
    opening_cash: Decimal | None = None


class FinanceSettingOut(ORM):
    is_pkp: bool
    default_vat_code: str | None
    default_wht_code: str | None
    overhead_pct: Decimal
    quote_mgmt_threshold: Decimal
    price_rounding: Decimal
    payment_term_days: int
    evm_bac_includes_contingency: bool
    opening_cash: Decimal


class TaxRateIn(BaseModel):
    code: str = Field(pattern=r"^[A-Z0-9_]{2,30}$")
    name: str
    kind: TaxKind
    rate: Decimal = Field(ge=0, lt=1)
    dpp_num: int = Field(default=1, ge=1)
    dpp_den: int = Field(default=1, ge=1)
    effective_date: date
    notes: str | None = None

    @model_validator(mode="after")
    def _dpp(self):
        if self.dpp_num > self.dpp_den:
            raise ValueError("dpp_num tidak boleh > dpp_den")
        return self


class TaxRateOut(ORM, TaxRateIn):
    id: int
    is_active: bool


class PriceCalcIn(BaseModel):
    total_cost: Decimal = Field(gt=0)
    target_margin: Decimal = Field(ge=0, lt=1)
    discount: Decimal = Field(default=Decimal(0), ge=0)
    vat_code: str | None = None
    wht_code: str | None = None
    rounding: Decimal | None = Field(default=None, ge=0)
    on: date | None = None


class TermIn(BaseModel):
    name: str
    pct: Decimal = Field(gt=0, le=1)
    offset_days: int | None = Field(default=None, ge=0)


class QuotationIn(BaseModel):
    project_id: int
    budget_id: int | None = None           # default: budget approved terbaru
    title: str
    scope: str | None = None
    deliverables: str | None = None
    timeline: str | None = None
    assumptions: str | None = None
    exclusions: str | None = None
    valid_until: date | None = None
    target_margin: Decimal | None = Field(default=None, ge=0, lt=1)  # default: target project
    procurement_margin: Decimal | None = Field(default=None, ge=0, lt=1)  # default = target_margin
    discount: Decimal = Field(default=Decimal(0), ge=0)
    price_override: Decimal | None = Field(default=None, gt=0)       # harga manual (sebelum diskon)
    include_management_reserve: bool = False
    vat_code: str | None = None             # default dari setting (PKP)
    wht_code: str | None = None
    terms: list[TermIn] = []

    @model_validator(mode="after")
    def _terms(self):
        if self.terms and abs(sum(t.pct for t in self.terms) - 1) > Decimal("0.0001"):
            raise ValueError("Total persentase termin pembayaran harus 100%")
        return self


class TermOut(ORM):
    seq: int
    name: str
    pct: Decimal
    amount: Decimal
    offset_days: int | None


class QuotationOut(ORM):
    id: int
    project_id: int
    customer_id: int | None
    budget_id: int
    code: str
    version: int
    title: str
    scope: str | None
    deliverables: str | None
    timeline: str | None
    assumptions: str | None
    exclusions: str | None
    valid_until: date | None
    direct_cost: Decimal
    contingency: Decimal
    overhead: Decimal
    management_reserve: Decimal
    total_cost: Decimal
    target_margin: Decimal
    list_price: Decimal
    discount: Decimal
    subtotal: Decimal
    margin: Decimal
    vat_code: str | None
    vat_rate: Decimal
    vat_amount: Decimal
    total: Decimal
    wht_code: str | None
    wht_amount: Decimal
    status: QuoteStatus
    requires_management: bool
    decision_reason: str | None
    created_at: datetime
    procurement_margin: Decimal | None = None
    terms: list[TermOut]
    lines: list["QuotationLineOut"] = []


class ContractIn(BaseModel):
    quotation_id: int
    contract_no: str
    signed_date: date
    payment_term_days: int | None = Field(default=None, ge=0, le=365)
    sla: str | None = None
    warranty_months: int | None = Field(default=None, ge=0)
    change_mechanism: str | None = "Perubahan scope melalui Change Request tertulis yang disetujui kedua pihak"


class ContractOut(ORM):
    id: int
    project_id: int
    quotation_id: int | None
    contract_no: str
    signed_date: date
    contract_value: Decimal
    vat_code: str | None
    wht_code: str | None
    payment_term_days: int
    sla: str | None
    warranty_months: int | None
    change_mechanism: str | None


class MilestoneIn(BaseModel):
    name: str
    amount: Decimal | None = Field(default=None, gt=0)
    planned_date: date | None = None
    task_id: int | None = None
    change_request_id: int | None = None   # tagihan change order (amount default = revenue CR)


class MilestoneUpdate(BaseModel):
    planned_date: date | None = None
    task_id: int | None = None


class MilestoneOut(ORM):
    id: int
    contract_id: int
    seq: int
    name: str
    pct: Decimal | None
    amount: Decimal
    planned_date: date | None
    task_id: int | None
    change_request_id: int | None
    status: MilestoneStatus


class InvoiceFromMilestoneIn(BaseModel):
    milestone_id: int
    description: str | None = None
    force: bool = False   # tagih walau task syarat belum selesai (tercatat di audit)


class InvoiceIssueIn(BaseModel):
    issue_date: date | None = None
    due_days: int | None = Field(default=None, ge=0, le=365)


class InvoiceOut(ORM):
    id: int
    project_id: int
    contract_id: int | None
    milestone_id: int | None
    invoice_no: str | None
    description: str
    issue_date: date | None
    due_date: date | None
    subtotal: Decimal
    vat_code: str | None
    vat_rate: Decimal
    dpp: Decimal
    vat_amount: Decimal
    total: Decimal
    wht_code: str | None
    wht_amount: Decimal
    status: InvoiceStatus
    status_reason: str | None


class InvoiceViewOut(InvoiceOut):
    cash_received: Decimal
    settled: Decimal
    outstanding: Decimal
    display_status: str
    days_past_due: int | None


class PaymentIn(BaseModel):
    invoice_id: int
    payment_date: date
    amount: Decimal = Field(gt=0)
    wht_amount: Decimal = Field(default=Decimal(0), ge=0)
    method: str | None = "transfer"
    reference: str | None = None


class PaymentOut(ORM):
    id: int
    invoice_id: int
    payment_date: date
    amount: Decimal
    wht_amount: Decimal
    method: str | None
    reference: str | None
    reversal_of_id: int | None
    created_at: datetime


# =====================================================================
# TAHAP 3b: Katalog & BOQ
# =====================================================================
from .models import ProcCategory, ProcStatus  # noqa: E402


class CatalogIn(BaseModel):
    code: str
    name: str
    category: ProcCategory
    unit: str = "unit"
    unit_cost: Decimal = Field(ge=0)
    vendor_id: int | None = None
    lead_time_days: int = Field(default=0, ge=0)
    spec: str | None = None


class CatalogUpdate(BaseModel):
    name: str | None = None
    unit: str | None = None
    unit_cost: Decimal | None = Field(default=None, ge=0)
    vendor_id: int | None = None
    lead_time_days: int | None = Field(default=None, ge=0)
    spec: str | None = None
    is_active: bool | None = None


class CatalogOut(ORM, CatalogIn):
    id: int
    is_active: bool


class ProcItemIn(BaseModel):
    wbs_id: int
    catalog_item_id: int | None = None   # isi → nama/kategori/satuan/harga/vendor/lead time otomatis
    name: str | None = None
    category: ProcCategory | None = None
    spec: str | None = None
    qty: Decimal = Field(gt=0)
    unit: str | None = None
    unit_cost: Decimal | None = Field(default=None, ge=0)
    vendor_id: int | None = None
    lead_time_days: int | None = Field(default=None, ge=0)
    need_by_date: date | None = None
    task_id: int | None = None
    notes: str | None = None


class ProcItemUpdate(BaseModel):
    wbs_id: int | None = None
    name: str | None = None
    spec: str | None = None
    qty: Decimal | None = Field(default=None, gt=0)
    unit: str | None = None
    unit_cost: Decimal | None = Field(default=None, ge=0)
    vendor_id: int | None = None
    lead_time_days: int | None = Field(default=None, ge=0)
    need_by_date: date | None = None
    notes: str | None = None


class ProcItemOut(ORM):
    id: int
    project_id: int
    wbs_id: int
    task_id: int | None
    catalog_item_id: int | None
    name: str
    category: ProcCategory
    spec: str | None
    qty: Decimal
    unit: str
    unit_cost: Decimal
    total_cost: Decimal = Decimal(0)
    vendor_id: int | None
    lead_time_days: int
    need_by_date: date | None
    order_by_date: date | None = None
    status: ProcStatus
    po_number: str | None
    order_date: date | None
    actual_unit_cost: Decimal | None
    received_date: date | None
    expense_id: int | None
    notes: str | None


class ProcOrderIn(BaseModel):
    po_number: str
    order_date: date
    vendor_id: int | None = None
    actual_unit_cost: Decimal | None = Field(default=None, ge=0)


class ProcReceiveIn(BaseModel):
    received_date: date


class QuotationLineOut(ORM):
    seq: int
    wbs_id: int | None
    wbs_code: str | None
    wbs_name: str | None
    kind: str
    description: str
    qty: Decimal
    unit: str
    unit_cost: Decimal
    cost: Decimal
    unit_price: Decimal
    amount: Decimal


QuotationOut.model_rebuild()
