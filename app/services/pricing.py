"""Pricing, Margin & Tax (PRD §11).

  Total Project Cost = Direct Cost + Contingency + Overhead (+ Management Reserve bila dipilih)
  Selling Price Before Tax = Total Project Cost / (1 − Target Margin)   → dibulatkan ke atas
  Margin = (Subtotal − Total Project Cost) / Subtotal
  PPN     = Subtotal × (dpp_num/dpp_den) × tarif          (dipungut, BUKAN revenue perusahaan)
  PPh     = Subtotal × tarif                        (dipotong customer, mengurangi kas diterima)
Tarif pajak diambil dari master TaxRate per tanggal (effective date), tidak di-hardcode.
"""
from datetime import date
from decimal import ROUND_CEILING, Decimal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import FinanceSetting, TaxKind, TaxRate
from .costing import money


def settings_for(db: Session, org_id: int, create: bool = True) -> FinanceSetting:
    """Setting organisasi. create=False → objek default sementara (tidak disimpan) bila belum ada."""
    s = db.scalar(select(FinanceSetting).where(FinanceSetting.org_id == org_id))
    if not s:
        s = FinanceSetting(org_id=org_id, is_pkp=True, overhead_pct=Decimal("0.10"),
                           quote_mgmt_threshold=Decimal("500000000"), price_rounding=Decimal("1000000"),
                           payment_term_days=30, evm_bac_includes_contingency=True, opening_cash=Decimal(0))
        if create:
            db.add(s)
            db.flush()
    return s


def tax_on(db: Session, org_id: int, code: str | None, on: date, kind: TaxKind) -> TaxRate | None:
    if not code:
        return None
    t = db.scalar(select(TaxRate).where(TaxRate.org_id == org_id, TaxRate.code == code,
                                        TaxRate.effective_date <= on, TaxRate.is_active.is_(True))
                  .order_by(TaxRate.effective_date.desc()).limit(1))
    if not t:
        raise HTTPException(422, f"Kode pajak '{code}' tidak ditemukan / belum berlaku per {on}")
    if t.kind != kind:
        raise HTTPException(422, f"Kode pajak '{code}' bukan jenis {kind.value}")
    return t


def round_up(x: Decimal, unit: Decimal) -> Decimal:
    if not unit or unit <= 0:
        return money(x)
    return money((Decimal(x) / unit).to_integral_value(ROUND_CEILING) * unit)


def selling_price(total_cost: Decimal, target_margin: Decimal, rounding: Decimal) -> Decimal:
    if not (0 <= target_margin < 1):
        raise HTTPException(422, "target_margin harus 0 ≤ m < 1")
    return round_up(Decimal(total_cost) / (1 - Decimal(target_margin)), rounding)


def margin(revenue: Decimal, cost: Decimal) -> Decimal | None:
    return ((Decimal(revenue) - Decimal(cost)) / Decimal(revenue)).quantize(Decimal("0.0001")) if revenue else None


def vat_amounts(subtotal: Decimal, tax: TaxRate | None) -> tuple[Decimal, Decimal]:
    """(DPP, PPN)"""
    if not tax:
        return money(subtotal), Decimal("0.00")
    dpp = money(Decimal(subtotal) * tax.dpp_num / tax.dpp_den)
    return dpp, money(dpp * tax.rate)


def wht_amount(subtotal: Decimal, tax: TaxRate | None) -> Decimal:
    return money(Decimal(subtotal) * tax.rate) if tax else Decimal("0.00")
