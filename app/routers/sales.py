"""Pricing (PRD §11), Quotation & Contract (PRD §12), billing milestone."""
from datetime import date, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import audit
from ..database import get_db
from ..models import (WBS, BillingMilestone, Budget, ChangeRequest, Contract, DocStatus, MilestoneStatus, Quotation,
                      QuotationLine, QuotationTerm, QuoteStatus, Role, Task, TaxKind, TaxRate, User)
from ..schemas import (ContractIn, ContractOut, DecisionIn, FinanceSettingIn, FinanceSettingOut, MilestoneIn,
                       MilestoneOut, MilestoneUpdate, PriceCalcIn, QuotationIn, QuotationOut, TaxRateIn, TaxRateOut,
                       TermIn)
from ..security import get_current_user, load_project, require_roles
from ..services import estimate as est
from ..services import pricing
from ..services.costing import money

router = APIRouter(prefix="/api", tags=["Pricing, Quotation & Contract"])
SELLERS = (Role.ADMIN, Role.MANAGEMENT, Role.SALES, Role.PM)
FINANCE_ADMIN = (Role.ADMIN, Role.FINANCE)
DEFAULT_TERMS = [("Kickoff", "0.30"), ("Development", "0.30"), ("UAT", "0.30"), ("Handover", "0.10")]


# ---------------- Settings & Tax master ----------------
@router.get("/finance/settings", response_model=FinanceSettingOut)
def get_settings(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return pricing.settings_for(db, user.org_id, create=False)


@router.patch("/finance/settings", response_model=FinanceSettingOut)
def update_settings(body: FinanceSettingIn, db: Session = Depends(get_db),
                    user: User = Depends(require_roles(*FINANCE_ADMIN))):
    s = pricing.settings_for(db, user.org_id)
    old = audit.snapshot(s)
    data = body.model_dump(exclude_unset=True)
    today = date.today()
    if data.get("default_vat_code"):
        pricing.tax_on(db, user.org_id, data["default_vat_code"], today, TaxKind.VAT)
    if data.get("default_wht_code"):
        pricing.tax_on(db, user.org_id, data["default_wht_code"], today, TaxKind.WITHHOLDING)
    for k, v in data.items():
        setattr(s, k, v)
    audit.log(db, user, "update", "finance_setting", s.id, old=old, new=audit.snapshot(s))
    db.commit()
    return s


@router.post("/taxes", response_model=TaxRateOut, status_code=201)
def create_tax(body: TaxRateIn, db: Session = Depends(get_db), user: User = Depends(require_roles(*FINANCE_ADMIN))):
    """Tarif baru = baris baru dengan effective_date baru (riwayat tarif tetap tersimpan)."""
    if db.scalar(select(TaxRate).where(TaxRate.org_id == user.org_id, TaxRate.code == body.code,
                                       TaxRate.effective_date == body.effective_date)):
        raise HTTPException(409, "Tarif untuk kode & tanggal efektif ini sudah ada")
    t = TaxRate(org_id=user.org_id, **body.model_dump())
    db.add(t)
    db.flush()
    audit.log(db, user, "create", "tax_rate", t.id, new=body.model_dump())
    db.commit()
    return t


@router.get("/taxes", response_model=list[TaxRateOut])
def list_taxes(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return db.scalars(select(TaxRate).where(TaxRate.org_id == user.org_id)
                      .order_by(TaxRate.code, TaxRate.effective_date)).all()


@router.post("/pricing/calculate")
def calculate_price(body: PriceCalcIn, db: Session = Depends(get_db),
                    user: User = Depends(require_roles(*SELLERS, Role.FINANCE))):
    """Kalkulator harga stateless: cost + target margin → harga jual, pajak, margin setelah diskon."""
    s = pricing.settings_for(db, user.org_id, create=False)
    on = body.on or date.today()
    rounding = body.rounding if body.rounding is not None else s.price_rounding
    list_price = pricing.selling_price(body.total_cost, body.target_margin, rounding)
    subtotal = money(list_price - body.discount)
    if subtotal <= 0:
        raise HTTPException(422, "Diskon melebihi harga")
    vt = pricing.tax_on(db, user.org_id, body.vat_code, on, TaxKind.VAT)
    wt = pricing.tax_on(db, user.org_id, body.wht_code, on, TaxKind.WITHHOLDING)
    dpp, vat = pricing.vat_amounts(subtotal, vt)
    wht = pricing.wht_amount(subtotal, wt)
    return {"total_cost": money(body.total_cost), "target_margin": body.target_margin, "list_price": list_price,
            "discount": money(body.discount), "subtotal": subtotal, "margin": pricing.margin(subtotal, body.total_cost),
            "gross_profit": money(subtotal - body.total_cost), "dpp": dpp, "vat": vat, "customer_total": subtotal + vat,
            "wht": wht, "expected_cash": subtotal + vat - wht,
            "note": "PPN dipungut dari customer dan disetor ke negara — bukan profit perusahaan"}


# ---------------- Quotation ----------------
def _quote(db, user, qid) -> Quotation:
    q = db.get(Quotation, qid)
    if not q or q.org_id != user.org_id:
        raise HTTPException(404, "Quotation not found")
    load_project(db, user, q.project_id)
    return q


@router.post("/quotations", response_model=QuotationOut, status_code=201)
def create_quotation(body: QuotationIn, db: Session = Depends(get_db), user: User = Depends(require_roles(*SELLERS))):
    """Harga dihitung dari budget: Total Cost / (1 − target margin). Quotation ulang = versi baru."""
    p = load_project(db, user, body.project_id)
    if user.role == Role.PM:
        load_project(db, user, p.id, manage=True)
    if db.scalar(select(Quotation).where(Quotation.project_id == p.id, Quotation.status == QuoteStatus.ACCEPTED)):
        raise HTTPException(409, "Project sudah punya quotation accepted; perubahan harga melalui Change Request")
    if body.budget_id:
        b = db.get(Budget, body.budget_id)
        if not b or b.project_id != p.id:
            raise HTTPException(422, "budget_id tidak valid")
    else:
        b = db.scalar(select(Budget).where(Budget.project_id == p.id, Budget.status == DocStatus.APPROVED))
        if not b:
            raise HTTPException(422, "Belum ada budget approved sebagai dasar harga")
    s = pricing.settings_for(db, user.org_id)
    target = body.target_margin if body.target_margin is not None else p.target_margin
    if target is None:
        raise HTTPException(422, "Isi target_margin (quotation atau project)")
    today = date.today()

    # Rincian per WBS dari budget: jasa (agregat MD per WBS), item BOQ, dan biaya lain
    wbs_map = {w.id: w for w in db.scalars(select(WBS).where(WBS.project_id == p.id))}
    svc = {}
    lines = []
    for bl_ in b.lines:
        if bl_.category == "labor":
            agg = svc.setdefault(bl_.wbs_id, [Decimal(0), Decimal(0)])
            agg[0] += bl_.md or 0
            agg[1] += bl_.amount
        elif bl_.procurement_item_id:
            lines.append(est.Line("procurement", bl_.wbs_id, bl_.description, bl_.qty, bl_.unit or "unit",
                                  bl_.rate, bl_.amount))
        else:
            lines.append(est.Line("expense", bl_.wbs_id, bl_.description or bl_.category, Decimal(1), "lot",
                                  bl_.amount, bl_.amount))
    for wid, (md, amt) in svc.items():
        w = wbs_map.get(wid)
        lines.append(est.Line("service", wid, f"Jasa {w.name if w else 'proyek'}", md, "MD",
                              money(amt / md) if md else amt, amt))
    mr = b.management_reserve if body.include_management_reserve else Decimal(0)
    priced = est.price_lines(lines, b.contingency, s.overhead_pct, target, body.procurement_margin,
                             s.price_rounding, mr, body.price_override)
    overhead, total_cost, list_price = priced["overhead"], priced["total_cost"], priced["list_price"]

    def _wkey(l):
        w = wbs_map.get(l.wbs_id)
        return ([x.zfill(6) for x in w.code.split(".")] if w else ["~"], 0 if l.kind == "service" else 1)
    q_lines = []
    for i, l in enumerate(sorted(priced["lines"], key=_wkey), start=1):
        w = wbs_map.get(l.wbs_id)
        q_lines.append(QuotationLine(seq=i, wbs_id=l.wbs_id, wbs_code=w.code if w else None,
                                     wbs_name=w.name if w else None, kind=l.kind, description=l.description,
                                     qty=l.qty, unit=l.unit, unit_cost=l.unit_cost, cost=l.cost,
                                     unit_price=l.unit_price, amount=l.amount))
    subtotal = money(list_price - body.discount)
    if subtotal <= 0:
        raise HTTPException(422, "Diskon melebihi harga")

    vat_code = body.vat_code if body.vat_code is not None else (s.default_vat_code if s.is_pkp else None)
    if vat_code and not s.is_pkp:
        raise HTTPException(422, "Organisasi non-PKP tidak boleh memungut PPN")
    vt = pricing.tax_on(db, user.org_id, vat_code, today, TaxKind.VAT)
    wht_code = body.wht_code if body.wht_code is not None else s.default_wht_code
    wt = pricing.tax_on(db, user.org_id, wht_code, today, TaxKind.WITHHOLDING)
    dpp, vat = pricing.vat_amounts(subtotal, vt)
    m = pricing.margin(subtotal, total_cost)

    prev = db.scalar(select(Quotation).where(Quotation.project_id == p.id, Quotation.status.not_in(
        [QuoteStatus.LOST, QuoteStatus.SUPERSEDED])).order_by(Quotation.version.desc()))
    if prev:
        code, version = prev.code, prev.version + 1
        prev.status = QuoteStatus.SUPERSEDED
        audit.log(db, user, "supersede", "quotation", prev.id, new={"by_version": version})
    else:
        n = (db.scalar(select(func.count(func.distinct(Quotation.code))).where(Quotation.org_id == user.org_id)) or 0) + 1
        code, version = f"Q-{today.year}-{n:03d}", 1

    terms_in = body.terms or [TermIn(name=n, pct=Decimal(pc)) for n, pc in DEFAULT_TERMS]
    terms, allocated = [], Decimal(0)
    for i, t in enumerate(terms_in, start=1):
        amt = subtotal - allocated if i == len(terms_in) else money(subtotal * t.pct)
        allocated += amt
        terms.append(QuotationTerm(seq=i, name=t.name, pct=t.pct, amount=amt, offset_days=t.offset_days))

    q = Quotation(org_id=user.org_id, project_id=p.id, customer_id=p.customer_id, budget_id=b.id, code=code,
                  version=version, title=body.title, scope=body.scope, deliverables=body.deliverables,
                  timeline=body.timeline, assumptions=body.assumptions, exclusions=body.exclusions,
                  valid_until=body.valid_until or today + timedelta(days=30), direct_cost=b.direct_cost,
                  procurement_margin=body.procurement_margin, lines=q_lines,
                  contingency=b.contingency, overhead=overhead, management_reserve=mr, total_cost=total_cost,
                  target_margin=target, list_price=list_price, discount=money(body.discount), subtotal=subtotal,
                  margin=m, vat_code=vat_code, vat_rate=vt.rate if vt else 0, vat_dpp_factor=(Decimal(vt.dpp_num) / vt.dpp_den).quantize(Decimal('0.00000001')) if vt else 1,
                  vat_amount=vat, total=subtotal + vat, wht_code=wht_code, wht_rate=wt.rate if wt else 0,
                  wht_amount=pricing.wht_amount(subtotal, wt),
                  # Management wajib bila nilai di atas threshold, atau diskon/harga manual menurunkan margin
                  # di bawah margin kebijakan (blended margin jasa + pengadaan pada harga standar)
                  requires_management=bool(subtotal > s.quote_mgmt_threshold
                                           or m < min(target, priced["margin"])),
                  created_by_id=user.id, terms=terms)
    db.add(q)
    db.flush()
    audit.log(db, user, "create", "quotation", q.id,
              new={"code": code, "version": version, "subtotal": subtotal, "margin": m})
    db.commit()
    return q


@router.get("/quotations", response_model=list[QuotationOut])
def list_quotations(project_id: int, db: Session = Depends(get_db),
                    user: User = Depends(require_roles(*SELLERS, Role.FINANCE))):
    load_project(db, user, project_id)
    return db.scalars(select(Quotation).where(Quotation.project_id == project_id)
                      .order_by(Quotation.version)).all()


@router.get("/quotations/{qid}", response_model=QuotationOut)
def get_quotation(qid: int, db: Session = Depends(get_db), user: User = Depends(require_roles(*SELLERS, Role.FINANCE))):
    return _quote(db, user, qid)


def _transition(db, user, q, to, reason=None, action=None):
    old = q.status
    q.status = to
    if reason:
        q.decision_reason = reason
    audit.log(db, user, action or to.value, "quotation", q.id, old={"status": old}, new={"status": to}, reason=reason)
    db.commit()
    return q


@router.post("/quotations/{qid}/submit", response_model=QuotationOut)
def submit_quotation(qid: int, db: Session = Depends(get_db), user: User = Depends(require_roles(*SELLERS))):
    q = _quote(db, user, qid)
    if q.status != QuoteStatus.DRAFT:
        raise HTTPException(409, "Hanya quotation draft yang dapat diajukan")
    return _transition(db, user, q, QuoteStatus.PENDING_FINANCE, action="submit")


@router.post("/quotations/{qid}/approve", response_model=QuotationOut)
def approve_quotation(qid: int, body: DecisionIn, db: Session = Depends(get_db),
                      user: User = Depends(get_current_user)):
    """Approval berjenjang: Finance → Management (bila di atas threshold atau margin < target)."""
    q = _quote(db, user, qid)
    if q.created_by_id == user.id:
        raise HTTPException(403, "Segregation of duties: pembuat quotation tidak boleh meng-approve")
    if q.status == QuoteStatus.PENDING_FINANCE:
        if user.role not in FINANCE_ADMIN:
            raise HTTPException(403, "Tahap ini memerlukan approval Finance")
        q.finance_approved_by_id = user.id
        nxt = QuoteStatus.PENDING_MANAGEMENT if q.requires_management else QuoteStatus.APPROVED
        return _transition(db, user, q, nxt, body.reason, "approve_finance")
    if q.status == QuoteStatus.PENDING_MANAGEMENT:
        if user.role not in (Role.ADMIN, Role.MANAGEMENT):
            raise HTTPException(403, "Tahap ini memerlukan approval Management")
        if q.finance_approved_by_id == user.id:
            raise HTTPException(403, "Approver finance dan management harus orang berbeda")
        q.management_approved_by_id = user.id
        return _transition(db, user, q, QuoteStatus.APPROVED, body.reason, "approve_management")
    raise HTTPException(409, f"Quotation berstatus {q.status.value}, tidak menunggu approval")


@router.post("/quotations/{qid}/reject", response_model=QuotationOut)
def reject_quotation(qid: int, body: DecisionIn, db: Session = Depends(get_db),
                     user: User = Depends(require_roles(Role.ADMIN, Role.FINANCE, Role.MANAGEMENT))):
    q = _quote(db, user, qid)
    if q.status not in (QuoteStatus.PENDING_FINANCE, QuoteStatus.PENDING_MANAGEMENT):
        raise HTTPException(409, "Quotation tidak sedang menunggu approval")
    if not body.reason:
        raise HTTPException(422, "Alasan reject wajib diisi")
    return _transition(db, user, q, QuoteStatus.REJECTED, body.reason, "reject")


@router.post("/quotations/{qid}/send", response_model=QuotationOut)
def send_quotation(qid: int, db: Session = Depends(get_db), user: User = Depends(require_roles(*SELLERS))):
    q = _quote(db, user, qid)
    if q.status != QuoteStatus.APPROVED:
        raise HTTPException(409, "Hanya quotation approved yang dapat dikirim ke customer")
    return _transition(db, user, q, QuoteStatus.SENT, action="send")


@router.post("/quotations/{qid}/accept", response_model=QuotationOut)
def accept_quotation(qid: int, db: Session = Depends(get_db), user: User = Depends(require_roles(*SELLERS))):
    q = _quote(db, user, qid)
    if q.status not in (QuoteStatus.SENT, QuoteStatus.APPROVED):
        raise HTTPException(409, "Quotation harus approved/sent sebelum diterima customer")
    if q.valid_until and q.valid_until < date.today():
        raise HTTPException(422, "Quotation sudah kedaluwarsa; buat versi baru")
    return _transition(db, user, q, QuoteStatus.ACCEPTED, action="accept")


@router.post("/quotations/{qid}/lost", response_model=QuotationOut)
def lose_quotation(qid: int, body: DecisionIn, db: Session = Depends(get_db), user: User = Depends(require_roles(*SELLERS))):
    q = _quote(db, user, qid)
    if q.status in (QuoteStatus.ACCEPTED, QuoteStatus.SUPERSEDED, QuoteStatus.LOST):
        raise HTTPException(409, f"Quotation berstatus {q.status.value}")
    return _transition(db, user, q, QuoteStatus.LOST, body.reason, "lost")


# ---------------- Contract & milestones ----------------
@router.post("/contracts", response_model=ContractOut, status_code=201)
def create_contract(body: ContractIn, db: Session = Depends(get_db),
                    user: User = Depends(require_roles(Role.ADMIN, Role.MANAGEMENT, Role.SALES, Role.FINANCE))):
    q = _quote(db, user, body.quotation_id)
    if q.status != QuoteStatus.ACCEPTED:
        raise HTTPException(409, "Kontrak hanya dari quotation yang sudah accepted")
    p = load_project(db, user, q.project_id)
    if db.scalar(select(Contract).where(Contract.project_id == p.id)):
        raise HTTPException(409, "Project sudah memiliki kontrak")
    if db.scalar(select(Contract).where(Contract.org_id == user.org_id, Contract.contract_no == body.contract_no)):
        raise HTTPException(409, "Nomor kontrak sudah dipakai")
    s = pricing.settings_for(db, user.org_id)
    c = Contract(org_id=user.org_id, project_id=p.id, quotation_id=q.id, contract_no=body.contract_no,
                 signed_date=body.signed_date, contract_value=q.subtotal, vat_code=q.vat_code, wht_code=q.wht_code,
                 payment_term_days=body.payment_term_days if body.payment_term_days is not None else s.payment_term_days,
                 sla=body.sla, warranty_months=body.warranty_months, change_mechanism=body.change_mechanism,
                 created_by_id=user.id)
    db.add(c)
    db.flush()
    for t in q.terms:
        db.add(BillingMilestone(contract_id=c.id, project_id=p.id, seq=t.seq, name=t.name, pct=t.pct, amount=t.amount,
                                planned_date=(p.start_date + timedelta(days=t.offset_days))
                                if (t.offset_days is not None and p.start_date) else None))
    old = {"contract_value": p.contract_value, "tax_rate": p.tax_rate, "customer_id": p.customer_id}
    p.contract_value = q.subtotal
    p.tax_rate = (q.vat_rate * q.vat_dpp_factor).quantize(Decimal('0.0001'))  # tarif efektif
    p.customer_id = q.customer_id or p.customer_id
    audit.log(db, user, "create", "contract", c.id, new={"contract_no": c.contract_no, "value": c.contract_value})
    audit.log(db, user, "update", "project", p.id, old=old,
              new={"contract_value": p.contract_value, "tax_rate": p.tax_rate}, reason=f"Kontrak {c.contract_no}")
    db.commit()
    return c


@router.get("/projects/{pid}/contract", response_model=ContractOut)
def get_contract(pid: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    p = load_project(db, user, pid)
    if user.role == Role.STAFF:
        raise HTTPException(403, "Tidak diizinkan")
    c = db.scalar(select(Contract).where(Contract.project_id == p.id))
    if not c:
        raise HTTPException(404, "Project belum memiliki kontrak")
    return c


@router.get("/projects/{pid}/milestones", response_model=list[MilestoneOut])
def list_milestones(pid: int, db: Session = Depends(get_db),
                    user: User = Depends(require_roles(Role.ADMIN, Role.MANAGEMENT, Role.PM, Role.FINANCE, Role.SALES))):
    p = load_project(db, user, pid)
    return db.scalars(select(BillingMilestone).where(BillingMilestone.project_id == p.id)
                      .order_by(BillingMilestone.seq)).all()


@router.post("/contracts/{cid}/milestones", response_model=MilestoneOut, status_code=201)
def add_milestone(cid: int, body: MilestoneIn, db: Session = Depends(get_db),
                  user: User = Depends(require_roles(Role.ADMIN, Role.FINANCE, Role.MANAGEMENT))):
    """Tambahan termin hanya untuk change order (CR approved dengan revenue), agar total = kontrak + CR."""
    c = db.get(Contract, cid)
    if not c or c.org_id != user.org_id:
        raise HTTPException(404, "Contract not found")
    if not body.change_request_id:
        raise HTTPException(422, "Termin tambahan harus terkait Change Request yang approved")
    cr = db.get(ChangeRequest, body.change_request_id)
    if not cr or cr.project_id != c.project_id or cr.status != DocStatus.APPROVED or cr.revenue_delta <= 0:
        raise HTTPException(422, "CR harus approved, milik project ini, dan memiliki revenue")
    billed = db.scalar(select(func.coalesce(func.sum(BillingMilestone.amount), 0)).where(
        BillingMilestone.change_request_id == cr.id)) or 0
    amount = money(body.amount or (cr.revenue_delta - Decimal(billed)))
    if amount <= 0 or Decimal(billed) + amount > cr.revenue_delta:
        raise HTTPException(422, f"Sisa revenue {cr.code} yang bisa ditagih: {cr.revenue_delta - Decimal(billed)}")
    seq = (db.scalar(select(func.max(BillingMilestone.seq)).where(BillingMilestone.contract_id == c.id)) or 0) + 1
    m = BillingMilestone(contract_id=c.id, project_id=c.project_id, seq=seq, name=body.name, amount=amount,
                         planned_date=body.planned_date, task_id=body.task_id, change_request_id=cr.id)
    db.add(m)
    db.flush()
    audit.log(db, user, "create", "billing_milestone", m.id, new={"amount": amount, "cr": cr.code})
    db.commit()
    return m


@router.patch("/milestones/{mid}", response_model=MilestoneOut)
def update_milestone(mid: int, body: MilestoneUpdate, db: Session = Depends(get_db),
                     user: User = Depends(require_roles(Role.ADMIN, Role.FINANCE, Role.MANAGEMENT, Role.PM))):
    m = db.get(BillingMilestone, mid)
    if not m:
        raise HTTPException(404, "Milestone not found")
    load_project(db, user, m.project_id)
    if m.status != MilestoneStatus.PLANNED:
        raise HTTPException(409, "Milestone yang sudah ditagih tidak dapat diubah")
    data = body.model_dump(exclude_unset=True)
    if data.get("task_id"):
        t = db.get(Task, data["task_id"])
        if not t or t.project_id != m.project_id:
            raise HTTPException(422, "task_id tidak valid")
    old = audit.snapshot(m)
    for k, v in data.items():
        setattr(m, k, v)
    audit.log(db, user, "update", "billing_milestone", m.id, old=old, new=audit.snapshot(m))
    db.commit()
    return m
