"""Cashflow & Billing (PRD §17) dan Profitability (PRD §18)."""
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (BillingMilestone, Contract, DocStatus, Expense, Invoice, InvoiceStatus, MilestoneStatus,
                      Payment, Project, Quotation, QuoteStatus, TaxKind, Timesheet)
from . import change, evm
from .costing import money
from .pricing import margin, settings_for, tax_on, vat_amounts, wht_amount

OPEN = (InvoiceStatus.ISSUED, InvoiceStatus.PARTIALLY_PAID, InvoiceStatus.DISPUTED)
ZERO = Decimal(0)


def ym(d: date) -> str:
    return d.strftime("%Y-%m")


def settled(db: Session, invoice_id: int) -> tuple[Decimal, Decimal]:
    """(kas diterima, total pelunasan = kas + PPh dipotong)."""
    cash, wht = db.execute(select(func.coalesce(func.sum(Payment.amount), 0),
                                  func.coalesce(func.sum(Payment.wht_amount), 0))
                           .where(Payment.invoice_id == invoice_id)).one()
    return money(cash), money(Decimal(cash) + Decimal(wht))


def invoice_view(db: Session, inv: Invoice, today: date) -> dict:
    cash, paid = settled(db, inv.id)
    outstanding = money(inv.total - paid) if inv.status in OPEN else ZERO
    display = inv.status.value
    days_past_due = None
    if inv.status in (InvoiceStatus.ISSUED, InvoiceStatus.PARTIALLY_PAID) and inv.due_date:
        days_past_due = (today - inv.due_date).days
        if days_past_due > 0:
            display = "overdue"
        elif days_past_due >= -7:
            display = "due"   # jatuh tempo ≤ 7 hari
    return {"cash_received": cash, "settled": paid, "outstanding": outstanding,
            "display_status": display, "days_past_due": days_past_due}


def ar_aging(db: Session, org_id: int, today: date, project_id: int | None = None) -> dict:
    q = select(Invoice).where(Invoice.org_id == org_id, Invoice.status.in_(OPEN))
    if project_id:
        q = q.where(Invoice.project_id == project_id)
    buckets = {"current": ZERO, "1_30": ZERO, "31_60": ZERO, "61_90": ZERO, "over_90": ZERO}
    rows = []
    for inv in db.scalars(q.order_by(Invoice.due_date)).all():
        v = invoice_view(db, inv, today)
        if v["outstanding"] <= 0:
            continue
        dpd = v["days_past_due"] or 0
        key = "current" if dpd <= 0 else "1_30" if dpd <= 30 else "31_60" if dpd <= 60 else "61_90" if dpd <= 90 else "over_90"
        buckets[key] += v["outstanding"]
        rows.append({"invoice_id": inv.id, "invoice_no": inv.invoice_no, "project_id": inv.project_id,
                     "due_date": inv.due_date, "total": inv.total, "outstanding": v["outstanding"],
                     "days_past_due": dpd, "bucket": key, "status": v["display_status"]})
    total = sum(buckets.values(), ZERO)
    return {"as_of": today, "total_outstanding": money(total),
            "overdue": money(total - buckets["current"]),
            "buckets": {k: money(v) for k, v in buckets.items()}, "invoices": rows}


def revenue(db: Session, p: Project) -> Decimal:
    """Revenue = Contract Revenue + Approved Change Orders (sebelum pajak)."""
    return money(p.contract_value + change.approved_cr_revenue(db, p.id))


def _month_iter(a: date, b: date):
    cur = a.replace(day=1)
    while cur <= b:
        yield ym(cur)
        cur = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)


def cashflow(db: Session, p: Project, today: date) -> dict:
    """Actual per bulan (s.d. hari ini) + forecast bulan-bulan berikutnya.
    Cash-in = pembayaran customer (termasuk PPN yang nanti disetor — ditampilkan terpisah).
    Cash-out = labor approved (biaya gaji) + expense posted. Overhead tidak dihitung sebagai kas project."""
    s = settings_for(db, p.org_id, create=False)
    cin, vat_in, lab, exp = defaultdict(Decimal), defaultdict(Decimal), defaultdict(Decimal), defaultdict(Decimal)
    for pay, inv in db.execute(select(Payment, Invoice).join(Invoice, Invoice.id == Payment.invoice_id)
                               .where(Payment.project_id == p.id, Payment.payment_date <= today)).all():
        cin[ym(pay.payment_date)] += pay.amount
        if inv.total:
            vat_in[ym(pay.payment_date)] += (pay.amount + pay.wht_amount) * inv.vat_amount / inv.total
    for d, c in db.execute(select(Timesheet.work_date, Timesheet.labor_cost).where(
            Timesheet.project_id == p.id, Timesheet.status == DocStatus.APPROVED, Timesheet.work_date <= today)).all():
        lab[ym(d)] += c
    for d, a in db.execute(select(Expense.expense_date, Expense.amount).where(
            Expense.project_id == p.id, Expense.status == DocStatus.POSTED, Expense.expense_date <= today)).all():
        exp[ym(d)] += a

    # ---- Forecast ----
    f_in, f_out = defaultdict(Decimal), defaultdict(Decimal)
    this_m = ym(today)
    for inv in db.scalars(select(Invoice).where(Invoice.project_id == p.id, Invoice.status.in_(
            (InvoiceStatus.ISSUED, InvoiceStatus.PARTIALLY_PAID)))).all():
        v = invoice_view(db, inv, today)
        if v["outstanding"] > 0:
            net = v["outstanding"] * (inv.total - inv.wht_amount) / inv.total if inv.total else v["outstanding"]
            due = inv.due_date or today
            f_in[max(ym(due), this_m)] += net   # piutang overdue diasumsikan cair bulan ini
    for ms in db.scalars(select(BillingMilestone).where(BillingMilestone.project_id == p.id,
                                                        BillingMilestone.status == MilestoneStatus.PLANNED)).all():
        ct = db.get(Contract, ms.contract_id)
        when = (ms.planned_date or p.end_date or today) + timedelta(days=ct.payment_term_days if ct else 30)
        vt = tax_on(db, p.org_id, ct.vat_code if ct else None, when, TaxKind.VAT)
        wt = tax_on(db, p.org_id, ct.wht_code if ct else None, when, TaxKind.WITHHOLDING)
        _, vat = vat_amounts(ms.amount, vt)
        f_in[max(ym(when), this_m)] += ms.amount + vat - wht_amount(ms.amount, wt)

    r = evm.compute(db, p, today)
    bl = evm.active_baseline(db, p.id)
    if r and bl and r.selected["etc"] > 0:
        end = max(bl.end_date, today)
        months = [m for m in _month_iter(today, end)]
        incr, prev = {}, evm.pv_at(bl, today, r.bac)
        for m in months:
            y, mo = map(int, m.split("-"))
            last = (date(y, mo, 28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
            cur = evm.pv_at(bl, min(last, end), r.bac)
            incr[m] = max(cur - prev, ZERO)
            prev = cur
        tot = sum(incr.values(), ZERO)
        for m in months:
            share = incr[m] / tot if tot else (Decimal(1) / len(months))
            f_out[m] += r.selected["etc"] * share

    start = p.start_date or today
    keys = sorted(set(cin) | set(lab) | set(exp) | set(f_in) | set(f_out) | set(_month_iter(start, today)))
    cum = s.opening_cash or ZERO
    series = []
    for m in keys:
        actual = m <= this_m
        i = cin[m] if actual else ZERO
        o = lab[m] + exp[m] if actual else ZERO
        fi = f_in[m] if m >= this_m else ZERO
        fo = f_out[m] if m >= this_m else ZERO
        net = i - o + fi - fo
        cum += net
        series.append({"month": m, "kind": "actual" if m < this_m else "current" if m == this_m else "forecast",
                       "cash_in": money(i), "vat_in_cash_in": money(vat_in[m]) if actual else ZERO,
                       "cash_out_labor": money(lab[m]) if actual else ZERO,
                       "cash_out_expense": money(exp[m]) if actual else ZERO,
                       "forecast_cash_in": money(fi), "forecast_cash_out": money(fo),
                       "net": money(net), "cumulative": money(cum)})
    act_in = money(sum(cin.values(), ZERO))
    act_out = money(sum(lab.values(), ZERO) + sum(exp.values(), ZERO))
    lowest = min(series, key=lambda x: x["cumulative"]) if series else None
    return {"project_id": p.id, "opening_cash": money(s.opening_cash or ZERO), "as_of": today,
            "actual_cash_in": act_in, "actual_cash_out": act_out,
            "cash_position": money((s.opening_cash or ZERO) + act_in - act_out),
            "lowest_cumulative": lowest, "months": series,
            "notes": "Cash-in termasuk PPN yang wajib disetor (kolom vat_in_cash_in); bukan profit."}


def profitability(db: Session, p: Project, today: date) -> dict:
    """Empat lensa: planned (quotation) · baseline · actual s.d. hari ini (POC) · forecast (EAC)."""
    s = settings_for(db, p.org_id, create=False)
    oh = s.overhead_pct or ZERO
    rev = revenue(db, p)
    target = p.target_margin

    def lens(revenue_, direct, overhead, extra=None):
        total = money(direct + overhead)
        gp = money(revenue_ - total)
        d = {"revenue": money(revenue_), "cost_excl_overhead": money(direct), "overhead": money(overhead),
             "total_cost": total, "gross_profit": gp, "margin": margin(revenue_, total),
             "below_target": bool(target is not None and revenue_ and margin(revenue_, total) < target)}
        if extra:
            d.update(extra)
        return d

    out = {"project_id": p.id, "project_code": p.code, "as_of": today, "target_margin": target,
           "overhead_pct": oh, "revenue_contract": money(p.contract_value),
           "revenue_change_orders": money(rev - p.contract_value)}

    q = db.scalar(select(Quotation).where(Quotation.project_id == p.id, Quotation.status == QuoteStatus.ACCEPTED)
                  .order_by(Quotation.id.desc()))
    out["planned"] = lens(q.subtotal, q.total_cost - q.overhead, q.overhead,
                          {"source": f"Quotation {q.code} v{q.version}"}) if q else None

    bl = evm.active_baseline(db, p.id)
    r = evm.compute(db, p, today) if bl else None
    if bl:
        direct = sum((i.amount for i in bl.items), ZERO)
        out["baseline"] = lens(rev, bl.bac, direct * oh, {"source": f"Baseline v{bl.version}"})
    else:
        out["baseline"] = None

    inv_total = db.scalar(select(func.coalesce(func.sum(Invoice.subtotal), 0)).where(
        Invoice.project_id == p.id, Invoice.status.in_(OPEN + (InvoiceStatus.PAID,)))) or 0
    cash_in = db.scalar(select(func.coalesce(func.sum(Payment.amount), 0)).where(Payment.project_id == p.id)) or 0
    ac = evm.ac_at(db, p.id, today)
    earned_rev = rev * r.earned_pct if r else ZERO
    out["actual"] = lens(earned_rev, ac, ac * oh, {
        "source": "Revenue diakui = % selesai (EV/BAC) × revenue; cost = AC s.d. hari ini",
        "earned_pct": r.earned_pct if r else None, "invoiced_revenue": money(Decimal(inv_total)),
        "cash_collected": money(Decimal(cash_in))})
    out["forecast"] = lens(rev, r.selected["eac"], r.selected["eac"] * oh,
                           {"source": f"EAC metode {r.method}"}) if r else None
    return out
