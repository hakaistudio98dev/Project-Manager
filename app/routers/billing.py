"""Contract → Billing Milestone → Invoice → Payment → AR Reconciliation → Cashflow → Profitability."""
from datetime import date, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import audit
from ..database import get_db
from ..models import (BillingMilestone, Contract, Invoice, InvoiceStatus, MilestoneStatus, Payment, Project,
                      ProjectStatus, Role, Task, TaskStatus, TaxKind, User)
from ..schemas import (DecisionIn, InvoiceFromMilestoneIn, InvoiceIssueIn, InvoiceOut, InvoiceViewOut, PaymentIn,
                       PaymentOut)
from ..security import get_current_user, load_project, require_roles
from ..services import finance, pricing
from ..services.costing import money
from .actuals import _assert_open

router = APIRouter(prefix="/api", tags=["Billing, Cashflow & Profitability"])
FIN = (Role.ADMIN, Role.FINANCE)
VIEW = (Role.ADMIN, Role.MANAGEMENT, Role.FINANCE, Role.PM)


def _invoice(db, user, iid) -> Invoice:
    inv = db.get(Invoice, iid)
    if not inv or inv.org_id != user.org_id:
        raise HTTPException(404, "Invoice not found")
    load_project(db, user, inv.project_id)
    return inv


def _view(db, inv) -> InvoiceViewOut:
    return InvoiceViewOut(**InvoiceOut.model_validate(inv).model_dump(), **finance.invoice_view(db, inv, date.today()))


def _price(db, inv: Invoice, contract: Contract, on: date):
    """Hitung pajak memakai tarif yang berlaku pada tanggal invoice."""
    vt = pricing.tax_on(db, inv.org_id, contract.vat_code, on, TaxKind.VAT)
    wt = pricing.tax_on(db, inv.org_id, contract.wht_code, on, TaxKind.WITHHOLDING)
    inv.vat_code, inv.vat_rate = contract.vat_code, vt.rate if vt else Decimal(0)
    inv.dpp, inv.vat_amount = pricing.vat_amounts(inv.subtotal, vt)
    inv.total = inv.subtotal + inv.vat_amount
    inv.wht_code, inv.wht_rate = contract.wht_code, wt.rate if wt else Decimal(0)
    inv.wht_amount = pricing.wht_amount(inv.subtotal, wt)


def _refresh_status(db, inv: Invoice):
    if inv.status in (InvoiceStatus.CANCELLED, InvoiceStatus.DRAFT, InvoiceStatus.DISPUTED):
        return
    _, paid = finance.settled(db, inv.id)
    inv.status = InvoiceStatus.PAID if paid >= inv.total else \
        InvoiceStatus.PARTIALLY_PAID if paid > 0 else InvoiceStatus.ISSUED


# ---------------- Invoice ----------------
@router.post("/invoices/from-milestone", response_model=InvoiceViewOut, status_code=201)
def invoice_from_milestone(body: InvoiceFromMilestoneIn, response: Response, db: Session = Depends(get_db),
                           user: User = Depends(require_roles(*FIN)),
                           idempotency_key: str | None = Header(default=None)):
    if idempotency_key:
        dup = db.scalar(select(Invoice).where(Invoice.org_id == user.org_id, Invoice.idempotency_key == idempotency_key))
        if dup:
            response.status_code = 200
            return _view(db, dup)
    m = db.get(BillingMilestone, body.milestone_id)
    if not m:
        raise HTTPException(404, "Milestone not found")
    p = load_project(db, user, m.project_id)
    if m.status != MilestoneStatus.PLANNED or db.scalar(select(Invoice).where(Invoice.milestone_id == m.id)):
        raise HTTPException(409, "Milestone sudah ditagih / sudah punya invoice draft")
    warning = None
    if m.task_id:
        t = db.get(Task, m.task_id)
        if t and t.status != TaskStatus.DONE:
            if not body.force:
                raise HTTPException(422, f"Syarat tagih belum terpenuhi: task '{t.name}' belum selesai")
            warning = f"Ditagih sebelum task '{t.name}' selesai"
    c = db.get(Contract, m.contract_id)
    inv = Invoice(org_id=user.org_id, project_id=p.id, contract_id=c.id, milestone_id=m.id,
                  description=body.description or f"{p.name} — Termin {m.seq}: {m.name}", subtotal=m.amount,
                  dpp=m.amount, total=m.amount, idempotency_key=idempotency_key, created_by_id=user.id)
    _price(db, inv, c, date.today())
    db.add(inv)
    db.flush()
    audit.log(db, user, "create", "invoice", inv.id, new={"milestone": m.id, "subtotal": inv.subtotal}, reason=warning)
    db.commit()
    return _view(db, inv)


@router.post("/invoices/{iid}/issue", response_model=InvoiceViewOut)
def issue_invoice(iid: int, body: InvoiceIssueIn, db: Session = Depends(get_db),
                  user: User = Depends(require_roles(*FIN))):
    """Draft → Issued: nomor invoice diberikan, pajak dihitung ulang per tanggal terbit, lalu dikunci."""
    inv = _invoice(db, user, iid)
    if inv.status != InvoiceStatus.DRAFT:
        raise HTTPException(409, "Hanya invoice draft yang dapat diterbitkan")
    issue = body.issue_date or date.today()
    _assert_open(db, user.org_id, issue)
    c = db.get(Contract, inv.contract_id)
    _price(db, inv, c, issue)
    n = (db.scalar(select(func.count(Invoice.id)).where(Invoice.org_id == user.org_id,
                                                        Invoice.invoice_no.like(f"INV/{issue:%Y/%m}/%"))) or 0) + 1
    inv.invoice_no = f"INV/{issue:%Y/%m}/{n:04d}"
    inv.issue_date = issue
    inv.due_date = issue + timedelta(days=body.due_days if body.due_days is not None else c.payment_term_days)
    inv.status, inv.issued_by_id = InvoiceStatus.ISSUED, user.id
    if inv.milestone_id:
        db.get(BillingMilestone, inv.milestone_id).status = MilestoneStatus.INVOICED
    audit.log(db, user, "issue", "invoice", inv.id, new={"invoice_no": inv.invoice_no, "total": inv.total,
                                                          "vat": inv.vat_amount, "due": inv.due_date})
    db.commit()
    return _view(db, inv)


@router.post("/invoices/{iid}/cancel", response_model=InvoiceViewOut)
def cancel_invoice(iid: int, body: DecisionIn, db: Session = Depends(get_db), user: User = Depends(require_roles(*FIN))):
    inv = _invoice(db, user, iid)
    if inv.status not in (InvoiceStatus.DRAFT, InvoiceStatus.ISSUED, InvoiceStatus.DISPUTED):
        raise HTTPException(409, f"Invoice {inv.status.value} tidak dapat dibatalkan")
    if not body.reason:
        raise HTTPException(422, "Alasan pembatalan wajib diisi")
    if finance.settled(db, inv.id)[1] > 0:
        raise HTTPException(409, "Invoice sudah ada pembayaran; reverse pembayaran terlebih dahulu")
    if inv.milestone_id:  # lepaskan milestone agar bisa ditagih ulang
        db.get(BillingMilestone, inv.milestone_id).status = MilestoneStatus.PLANNED
        inv.status_reason = f"Dibatalkan: {body.reason} (milestone #{inv.milestone_id} dilepas)"
        inv.milestone_id = None
    else:
        inv.status_reason = f"Dibatalkan: {body.reason}"
    inv.status = InvoiceStatus.CANCELLED
    audit.log(db, user, "cancel", "invoice", inv.id, reason=body.reason)
    db.commit()
    return _view(db, inv)


@router.post("/invoices/{iid}/dispute", response_model=InvoiceViewOut)
def dispute_invoice(iid: int, body: DecisionIn, db: Session = Depends(get_db),
                    user: User = Depends(require_roles(*FIN, Role.MANAGEMENT))):
    inv = _invoice(db, user, iid)
    if inv.status not in (InvoiceStatus.ISSUED, InvoiceStatus.PARTIALLY_PAID):
        raise HTTPException(409, "Hanya invoice terbit yang belum lunas yang bisa di-dispute")
    if not body.reason:
        raise HTTPException(422, "Alasan dispute wajib diisi")
    inv.status, inv.status_reason = InvoiceStatus.DISPUTED, body.reason
    audit.log(db, user, "dispute", "invoice", inv.id, reason=body.reason)
    db.commit()
    return _view(db, inv)


@router.post("/invoices/{iid}/resolve", response_model=InvoiceViewOut)
def resolve_dispute(iid: int, body: DecisionIn, db: Session = Depends(get_db),
                    user: User = Depends(require_roles(*FIN, Role.MANAGEMENT))):
    inv = _invoice(db, user, iid)
    if inv.status != InvoiceStatus.DISPUTED:
        raise HTTPException(409, "Invoice tidak dalam status disputed")
    inv.status, inv.status_reason = InvoiceStatus.ISSUED, f"Dispute selesai: {body.reason or '-'}"
    _refresh_status(db, inv)
    audit.log(db, user, "resolve_dispute", "invoice", inv.id, reason=body.reason)
    db.commit()
    return _view(db, inv)


@router.get("/invoices", response_model=list[InvoiceViewOut])
def list_invoices(project_id: int | None = None, status: str | None = None, db: Session = Depends(get_db),
                  user: User = Depends(require_roles(*VIEW))):
    q = select(Invoice).where(Invoice.org_id == user.org_id)
    if project_id:
        load_project(db, user, project_id)
        q = q.where(Invoice.project_id == project_id)
    elif user.role == Role.PM:
        raise HTTPException(422, "PM wajib mengisi project_id")
    rows = [_view(db, i) for i in db.scalars(q.order_by(Invoice.id)).all()]
    return [r for r in rows if not status or status in (r.status.value, r.display_status)]


@router.get("/invoices/{iid}")
def get_invoice(iid: int, db: Session = Depends(get_db), user: User = Depends(require_roles(*VIEW))):
    inv = _invoice(db, user, iid)
    pays = db.scalars(select(Payment).where(Payment.invoice_id == inv.id).order_by(Payment.id)).all()
    return {"invoice": _view(db, inv), "payments": [PaymentOut.model_validate(p) for p in pays]}


# ---------------- Payment ----------------
@router.post("/payments", response_model=PaymentOut, status_code=201)
def record_payment(body: PaymentIn, response: Response, db: Session = Depends(get_db),
                   user: User = Depends(require_roles(*FIN)), idempotency_key: str | None = Header(default=None)):
    """Catat pelunasan. Pelunasan = kas + PPh dipotong (bukti potong). Kirim header Idempotency-Key
    agar retry jaringan tidak mencatat pembayaran ganda."""
    if idempotency_key:
        dup = db.scalar(select(Payment).where(Payment.org_id == user.org_id, Payment.idempotency_key == idempotency_key))
        if dup:
            response.status_code = 200
            return dup
    inv = _invoice(db, user, body.invoice_id)
    if inv.status not in (InvoiceStatus.ISSUED, InvoiceStatus.PARTIALLY_PAID):
        raise HTTPException(409, f"Invoice {inv.status.value} tidak dapat menerima pembayaran")
    if inv.issue_date and body.payment_date < inv.issue_date:
        raise HTTPException(422, "Tanggal bayar sebelum tanggal invoice")
    _assert_open(db, user.org_id, body.payment_date)
    _, paid = finance.settled(db, inv.id)
    outstanding = inv.total - paid
    if body.amount + body.wht_amount > outstanding:
        raise HTTPException(422, f"Pembayaran melebihi sisa tagihan ({money(outstanding)})")
    pay = Payment(org_id=user.org_id, invoice_id=inv.id, project_id=inv.project_id, payment_date=body.payment_date,
                  amount=money(body.amount), wht_amount=money(body.wht_amount), method=body.method,
                  reference=body.reference, idempotency_key=idempotency_key, created_by_id=user.id)
    db.add(pay)
    db.flush()
    _refresh_status(db, inv)
    audit.log(db, user, "create", "payment", pay.id, new={"invoice": inv.invoice_no, "amount": pay.amount,
                                                          "wht": pay.wht_amount, "invoice_status": inv.status})
    db.commit()
    return pay


@router.post("/payments/{pay_id}/reverse", response_model=PaymentOut, status_code=201)
def reverse_payment(pay_id: int, body: DecisionIn, db: Session = Depends(get_db), user: User = Depends(require_roles(*FIN))):
    pay = db.get(Payment, pay_id)
    if not pay or pay.org_id != user.org_id:
        raise HTTPException(404, "Payment not found")
    if pay.reversal_of_id or db.scalar(select(Payment).where(Payment.reversal_of_id == pay.id)):
        raise HTTPException(409, "Pembayaran ini reversal atau sudah di-reverse")
    if not body.reason:
        raise HTTPException(422, "Alasan reversal wajib diisi")
    today = date.today()
    _assert_open(db, user.org_id, today)
    r = Payment(org_id=pay.org_id, invoice_id=pay.invoice_id, project_id=pay.project_id, payment_date=today,
                amount=-pay.amount, wht_amount=-pay.wht_amount, method=pay.method,
                reference=f"REVERSAL #{pay.id}: {body.reason}", reversal_of_id=pay.id, created_by_id=user.id)
    db.add(r)
    db.flush()
    inv = db.get(Invoice, pay.invoice_id)
    _refresh_status(db, inv)
    audit.log(db, user, "reverse", "payment", pay.id, new={"reversal_id": r.id}, reason=body.reason)
    db.commit()
    return r


# ---------------- AR, Cashflow, Profitability ----------------
@router.get("/ar/aging")
def ar_aging(project_id: int | None = None, db: Session = Depends(get_db), user: User = Depends(require_roles(*VIEW))):
    if project_id:
        load_project(db, user, project_id)
    elif user.role == Role.PM:
        raise HTTPException(422, "PM wajib mengisi project_id")
    return finance.ar_aging(db, user.org_id, date.today(), project_id)


@router.get("/projects/{pid}/cashflow")
def project_cashflow(pid: int, db: Session = Depends(get_db), user: User = Depends(require_roles(*VIEW))):
    return finance.cashflow(db, load_project(db, user, pid), date.today())


@router.get("/projects/{pid}/profitability")
def project_profitability(pid: int, db: Session = Depends(get_db), user: User = Depends(require_roles(*VIEW))):
    return finance.profitability(db, load_project(db, user, pid), date.today())


@router.get("/reports/profitability")
def profitability_report(db: Session = Depends(get_db),
                         user: User = Depends(require_roles(Role.ADMIN, Role.MANAGEMENT, Role.FINANCE))):
    """Laporan portfolio: revenue, cost, gross profit, margin per lensa untuk semua project aktif."""
    today = date.today()
    rows, tot = [], {k: {"revenue": Decimal(0), "total_cost": Decimal(0)} for k in ("baseline", "actual", "forecast")}
    for p in db.scalars(select(Project).where(Project.org_id == user.org_id, Project.is_deleted.is_(False),
                                              Project.status != ProjectStatus.CANCELLED).order_by(Project.code)).all():
        pr = finance.profitability(db, p, today)
        rows.append(pr)
        for k in tot:
            if pr.get(k):
                tot[k]["revenue"] += pr[k]["revenue"]
                tot[k]["total_cost"] += pr[k]["total_cost"]
    totals = {k: {"revenue": money(v["revenue"]), "total_cost": money(v["total_cost"]),
                  "gross_profit": money(v["revenue"] - v["total_cost"]),
                  "margin": pricing.margin(v["revenue"], v["total_cost"])} for k, v in tot.items()}
    return {"as_of": today, "projects": rows, "totals": totals}
