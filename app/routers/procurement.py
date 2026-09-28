"""Katalog item, BOQ / procurement per WBS, estimasi dinamis, dan realisasi pengadaan.

Realisasi: planned → ordered (PO) → received → Expense 'submitted' otomatis
→ approval Finance → posted → masuk Actual Cost (EVM, cost summary, cashflow).
"""
from datetime import date, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..database import get_db
from ..models import (PROC_TO_EXPENSE, WBS, BudgetLine, CatalogItem, DocStatus, Expense, ExpenseCategory,
                      ProcStatus, ProcurementItem, Role, Task, TaxKind, User, Vendor)
from ..schemas import (CatalogIn, CatalogOut, CatalogUpdate, DecisionIn, ProcItemIn, ProcItemOut, ProcItemUpdate,
                       ProcOrderIn, ProcReceiveIn)
from ..security import get_current_user, load_project, require_roles
from ..services import estimate as est
from ..services import evm, pricing
from ..services.costing import money
from .actuals import _assert_open

router = APIRouter(prefix="/api", tags=["Katalog, BOQ & Estimasi"])
CATALOG_EDIT = (Role.ADMIN, Role.MANAGEMENT, Role.FINANCE, Role.PM)
VIEW = (Role.ADMIN, Role.MANAGEMENT, Role.FINANCE, Role.PM, Role.SALES)


# ---------------- Katalog ----------------
def _vendor_ok(db, user, vendor_id):
    if vendor_id:
        v = db.get(Vendor, vendor_id)
        if not v or v.org_id != user.org_id or v.is_deleted:
            raise HTTPException(422, "vendor_id tidak valid")


@router.post("/catalog", response_model=CatalogOut, status_code=201)
def create_catalog(body: CatalogIn, db: Session = Depends(get_db), user: User = Depends(require_roles(*CATALOG_EDIT))):
    if db.scalar(select(CatalogItem).where(CatalogItem.org_id == user.org_id, CatalogItem.code == body.code)):
        raise HTTPException(409, "Kode item sudah ada")
    _vendor_ok(db, user, body.vendor_id)
    c = CatalogItem(org_id=user.org_id, **body.model_dump())
    db.add(c)
    db.flush()
    audit.log(db, user, "create", "catalog_item", c.id, new=body.model_dump())
    db.commit()
    return c


@router.get("/catalog", response_model=list[CatalogOut])
def list_catalog(q: str | None = None, category: str | None = None, db: Session = Depends(get_db),
                 user: User = Depends(require_roles(*VIEW))):
    stmt = select(CatalogItem).where(CatalogItem.org_id == user.org_id, CatalogItem.is_active.is_(True))
    if q:
        stmt = stmt.where((CatalogItem.name.ilike(f"%{q}%")) | (CatalogItem.code.ilike(f"%{q}%")))
    if category:
        stmt = stmt.where(CatalogItem.category == category)
    return db.scalars(stmt.order_by(CatalogItem.category, CatalogItem.name)).all()


@router.patch("/catalog/{cid}", response_model=CatalogOut)
def update_catalog(cid: int, body: CatalogUpdate, db: Session = Depends(get_db),
                   user: User = Depends(require_roles(*CATALOG_EDIT))):
    c = db.get(CatalogItem, cid)
    if not c or c.org_id != user.org_id:
        raise HTTPException(404, "Item katalog tidak ditemukan")
    data = body.model_dump(exclude_unset=True)
    _vendor_ok(db, user, data.get("vendor_id"))
    old = audit.snapshot(c)
    for k, v in data.items():
        setattr(c, k, v)
    audit.log(db, user, "update", "catalog_item", c.id, old=old, new=audit.snapshot(c))
    db.commit()
    return c


# ---------------- BOQ ----------------
def _out(it: ProcurementItem) -> ProcItemOut:
    o = ProcItemOut.model_validate(it)
    o.total_cost = money(it.qty * (it.actual_unit_cost if it.actual_unit_cost is not None else it.unit_cost))
    if it.need_by_date:
        o.order_by_date = it.need_by_date - timedelta(days=it.lead_time_days or 0)
    return o


def _item(db, user, iid, manage=True) -> ProcurementItem:
    it = db.get(ProcurementItem, iid)
    if not it:
        raise HTTPException(404, "Item BOQ tidak ditemukan")
    load_project(db, user, it.project_id, manage=manage)
    return it


def _wbs_ok(db, project_id, wbs_id):
    w = db.get(WBS, wbs_id)
    if not w or w.project_id != project_id:
        raise HTTPException(422, "wbs_id tidak valid")


@router.post("/projects/{pid}/procurements", response_model=ProcItemOut, status_code=201)
def add_item(pid: int, body: ProcItemIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    p = load_project(db, user, pid, manage=True)
    _wbs_ok(db, p.id, body.wbs_id)
    data = body.model_dump()
    if body.catalog_item_id:
        c = db.get(CatalogItem, body.catalog_item_id)
        if not c or c.org_id != user.org_id or not c.is_active:
            raise HTTPException(422, "catalog_item_id tidak valid")
        for k in ("name", "category", "spec", "unit", "unit_cost", "vendor_id", "lead_time_days"):
            if data.get(k) is None:
                data[k] = getattr(c, k)
    if not data.get("name") or data.get("category") is None or data.get("unit_cost") is None:
        raise HTTPException(422, "Isi catalog_item_id, atau name + category + unit_cost")
    if body.task_id:
        t = db.get(Task, body.task_id)
        if not t or t.project_id != p.id:
            raise HTTPException(422, "task_id tidak valid")
    _vendor_ok(db, user, data.get("vendor_id"))
    data["unit"] = data.get("unit") or "unit"
    data["lead_time_days"] = data.get("lead_time_days") or 0
    it = ProcurementItem(project_id=p.id, created_by_id=user.id, **data)
    db.add(it)
    db.flush()
    audit.log(db, user, "create", "procurement_item", it.id, new=audit.snapshot(it))
    db.commit()
    return _out(it)


@router.get("/projects/{pid}/procurements", response_model=list[ProcItemOut])
def list_items(pid: int, db: Session = Depends(get_db), user: User = Depends(require_roles(*VIEW))):
    p = load_project(db, user, pid)
    return [_out(i) for i in db.scalars(select(ProcurementItem).where(ProcurementItem.project_id == p.id)
                                        .order_by(ProcurementItem.wbs_id, ProcurementItem.id)).all()]


@router.patch("/procurements/{iid}", response_model=ProcItemOut)
def update_item(iid: int, body: ProcItemUpdate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    it = _item(db, user, iid)
    if it.status != ProcStatus.PLANNED:
        raise HTTPException(409, "Item yang sudah dipesan tidak dapat diubah")
    data = body.model_dump(exclude_unset=True)
    if "wbs_id" in data:
        _wbs_ok(db, it.project_id, data["wbs_id"])
    _vendor_ok(db, user, data.get("vendor_id"))
    old = audit.snapshot(it)
    for k, v in data.items():
        setattr(it, k, v)
    audit.log(db, user, "update", "procurement_item", it.id, old=old, new=audit.snapshot(it))
    db.commit()
    return _out(it)


@router.delete("/procurements/{iid}", status_code=204)
def delete_item(iid: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Item planned yang belum dipakai budget dihapus; yang sudah masuk budget dibatalkan (jejak tetap ada)."""
    it = _item(db, user, iid)
    if it.status != ProcStatus.PLANNED:
        raise HTTPException(409, "Item yang sudah dipesan tidak dapat dihapus")
    used = db.scalar(select(BudgetLine).where(BudgetLine.procurement_item_id == it.id))
    audit.log(db, user, "cancel" if used else "delete", "procurement_item", it.id, old=audit.snapshot(it))
    if used:
        it.status = ProcStatus.CANCELLED
    else:
        db.delete(it)
    db.commit()


@router.post("/projects/{pid}/procurements/refresh-prices")
def refresh_prices(pid: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Samakan harga item planned dengan harga katalog terbaru."""
    p = load_project(db, user, pid, manage=True)
    changed = []
    for it in db.scalars(select(ProcurementItem).where(ProcurementItem.project_id == p.id,
                                                       ProcurementItem.status == ProcStatus.PLANNED,
                                                       ProcurementItem.catalog_item_id.is_not(None))).all():
        c = db.get(CatalogItem, it.catalog_item_id)
        if c and c.unit_cost != it.unit_cost:
            changed.append({"item_id": it.id, "name": it.name, "old": it.unit_cost, "new": c.unit_cost})
            it.unit_cost = c.unit_cost
    if changed:
        audit.log(db, user, "refresh_prices", "project", p.id, new={"items": changed})
    db.commit()
    return {"updated": len(changed), "items": changed}


@router.post("/procurements/{iid}/order", response_model=ProcItemOut)
def order_item(iid: int, body: ProcOrderIn, db: Session = Depends(get_db),
               user: User = Depends(require_roles(Role.ADMIN, Role.FINANCE, Role.PM, Role.MANAGEMENT))):
    it = _item(db, user, iid, manage=False)
    if it.status != ProcStatus.PLANNED:
        raise HTTPException(409, "Hanya item planned yang dapat dipesan")
    _vendor_ok(db, user, body.vendor_id)
    old = audit.snapshot(it)
    it.status, it.po_number, it.order_date = ProcStatus.ORDERED, body.po_number, body.order_date
    it.vendor_id = body.vendor_id or it.vendor_id
    it.actual_unit_cost = body.actual_unit_cost if body.actual_unit_cost is not None else it.unit_cost
    audit.log(db, user, "order", "procurement_item", it.id, old=old, new=audit.snapshot(it))
    db.commit()
    return _out(it)


@router.post("/procurements/{iid}/receive", response_model=ProcItemOut)
def receive_item(iid: int, body: ProcReceiveIn, db: Session = Depends(get_db),
                 user: User = Depends(require_roles(Role.ADMIN, Role.FINANCE, Role.PM, Role.MANAGEMENT))):
    """Barang diterima → expense 'submitted' dibuat otomatis untuk di-approve Finance."""
    it = _item(db, user, iid, manage=False)
    if it.status != ProcStatus.ORDERED:
        raise HTTPException(409, "Hanya item ordered yang dapat diterima")
    _assert_open(db, user.org_id, body.received_date)
    amount = money(it.qty * it.actual_unit_cost)
    e = Expense(org_id=user.org_id, project_id=it.project_id, wbs_id=it.wbs_id, vendor_id=it.vendor_id,
                category=ExpenseCategory(PROC_TO_EXPENSE[it.category.value]), amount=amount,
                expense_date=body.received_date, description=f"{it.name} × {it.qty.normalize()} {it.unit}",
                reference=it.po_number, status=DocStatus.SUBMITTED, created_by_id=user.id)
    db.add(e)
    db.flush()
    it.status, it.received_date, it.expense_id = ProcStatus.RECEIVED, body.received_date, e.id
    audit.log(db, user, "receive", "procurement_item", it.id, new={"expense_id": e.id, "amount": amount})
    db.commit()
    return _out(it)


# ---------------- Estimasi dinamis ----------------
@router.get("/projects/{pid}/estimate")
def live_estimate(pid: int, target_margin: Decimal | None = None, procurement_margin: Decimal | None = None,
                  contingency_pct: Decimal = Decimal("0.10"), discount: Decimal = Decimal(0),
                  db: Session = Depends(get_db), user: User = Depends(require_roles(*VIEW))):
    """Estimasi live per WBS: jasa (MD × rate) + BOQ (qty × harga), plus simulasi harga penawaran.
    Berubah otomatis setiap MD task, qty, atau harga item diubah."""
    p = load_project(db, user, pid)
    s = pricing.settings_for(db, user.org_id, create=False)
    tm = target_margin if target_margin is not None else (p.target_margin if p.target_margin is not None else Decimal("0.30"))
    lines, warnings = est.live_lines(db, p)
    if not lines:
        return {"project_id": p.id, "empty": True, "warnings": warnings or ["Belum ada task ber-MD atau item BOQ"],
                "tree": est.wbs_tree(db, p.id, [])}
    direct = sum((l.cost for l in lines), Decimal(0))
    r = est.price_lines(lines, money(direct * contingency_pct), s.overhead_pct, tm, procurement_margin, s.price_rounding)
    subtotal = money(r["list_price"] - discount)
    vt = pricing.tax_on(db, user.org_id, s.default_vat_code if s.is_pkp else None, date.today(), TaxKind.VAT)
    dpp, vat = pricing.vat_amounts(subtotal, vt)
    bl = evm.active_baseline(db, p.id)
    out = {k: v for k, v in r.items() if k != "lines"}
    out.update({"project_id": p.id, "empty": False, "contingency_pct": contingency_pct,
                "overhead_pct": s.overhead_pct, "discount": money(discount), "subtotal": subtotal,
                "margin_after_discount": pricing.margin(subtotal, r["total_cost"]), "vat": vat,
                "customer_total": subtotal + vat, "total_md": sum((l.qty for l in lines if l.kind == "service"), Decimal(0)),
                "tree": est.wbs_tree(db, p.id, lines), "warnings": warnings,
                "baseline_note": (f"Baseline v{bl.version} aktif (BAC {bl.bac}). Perubahan estimasi tidak mengubah "
                                  "baseline — ajukan Change Request.") if bl else None})
    return out
