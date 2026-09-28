"""Estimasi dinamis per WBS + harga penawaran terperinci.

Satu engine dipakai oleh:
  • GET /projects/{id}/estimate   → live dari task (MD × rate) + BOQ (qty × harga beli)
  • POST /quotations               → dari budget approved (snapshot)
sehingga angka estimasi dan penawaran selalu konsisten.

Langkah perhitungan
  1. Direct cost per baris: jasa = MD × cost/MD, barang = qty × harga beli.
  2. Beban (load) = (direct + contingency + overhead + MR) / direct → dibagi proporsional ke tiap baris.
  3. Dua pool margin: jasa (target margin) & pengadaan (procurement margin, default = target margin).
       harga pool = biaya ter-load pool / (1 − margin pool)
  4. Total dibulatkan ke atas (price_rounding) → selisih pembulatan dibagi proporsional.
  5. Harga satuan barang dibulatkan ke rupiah; sisa selisih diserap baris jasa terbesar
     → Σ rincian = harga penawaran (sebelum diskon), tepat.
"""
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import WBS, ProcStatus, ProcurementItem, Project, Task
from . import costing
from .costing import money
from .pricing import margin as margin_of
from .pricing import round_up

ONE = Decimal(1)


@dataclass
class Line:
    kind: str                 # service | procurement | expense
    wbs_id: int | None
    description: str
    qty: Decimal
    unit: str
    unit_cost: Decimal
    cost: Decimal
    ref: dict = field(default_factory=dict)
    unit_price: Decimal = Decimal(0)
    amount: Decimal = Decimal(0)


def price_lines(lines: list[Line], contingency: Decimal, overhead_pct: Decimal, target_margin: Decimal,
                procurement_margin: Decimal | None, rounding: Decimal, management_reserve: Decimal = Decimal(0),
                price_override: Decimal | None = None) -> dict:
    for m in (target_margin, procurement_margin):
        if m is not None and not (0 <= m < 1):
            raise HTTPException(422, "Margin harus 0 ≤ m < 1")
    direct = sum((l.cost for l in lines), Decimal(0))
    if direct <= 0:
        raise HTTPException(422, "Belum ada biaya untuk dihitung (isi MD task atau item BOQ)")
    p_margin = procurement_margin if procurement_margin is not None else target_margin
    overhead = money(direct * overhead_pct)
    total_cost = money(direct + contingency + overhead + management_reserve)
    load = total_cost / direct

    def pool_margin(l):
        return p_margin if l.kind == "procurement" else target_margin

    raw = {id(l): l.cost * load / (ONE - pool_margin(l)) for l in lines}
    raw_total = sum(raw.values(), Decimal(0))
    list_price = money(price_override) if price_override else round_up(raw_total, rounding)
    k = list_price / raw_total

    allocated = Decimal(0)
    for l in lines:
        if l.kind == "procurement" and l.qty > 0:
            l.unit_price = (raw[id(l)] * k / l.qty).quantize(ONE, ROUND_HALF_UP)
            l.amount = money(l.unit_price * l.qty)
        else:
            l.amount = money(raw[id(l)] * k)
            l.unit_price = money(l.amount / l.qty) if l.qty else l.amount
        allocated += l.amount
    diff = list_price - allocated
    if diff:
        service = [l for l in lines if l.kind != "procurement"]
        target = max(service or lines, key=lambda x: x.amount)
        target.amount += diff
        target.unit_price = money(target.amount / target.qty) if target.qty else target.amount

    svc_cost = sum((l.cost for l in lines if l.kind != "procurement"), Decimal(0))
    proc_cost = direct - svc_cost
    svc_price = sum((l.amount for l in lines if l.kind != "procurement"), Decimal(0))
    return {
        "direct_cost": money(direct), "service_cost": money(svc_cost), "procurement_cost": money(proc_cost),
        "contingency": money(contingency), "overhead": overhead, "management_reserve": money(management_reserve),
        "total_cost": total_cost, "target_margin": target_margin, "procurement_margin": p_margin,
        "list_price": list_price, "service_price": money(svc_price), "procurement_price": money(list_price - svc_price),
        "margin": margin_of(list_price, total_cost), "lines": lines,
    }


def live_lines(db: Session, p: Project) -> tuple[list[Line], list[str]]:
    """Baris biaya live: task (MD × cost rate owner pada planned_start) + BOQ aktif."""
    lines, warnings = [], []
    for t in db.scalars(select(Task).where(Task.project_id == p.id).order_by(Task.id)).all():
        if t.estimate_md <= 0:
            continue
        on = t.planned_start or p.start_date
        rate = costing.rate_effective_on(db, t.owner_employee_id, on) if (t.owner_employee_id and on) else None
        if not rate:
            warnings.append(f"Task '{t.name}' belum punya owner/rate/tanggal — tidak masuk estimasi")
            continue
        lines.append(Line("service", t.wbs_id, t.name, t.estimate_md, "MD", rate.cost_per_md,
                          costing.labor_cost(t.estimate_md, rate.cost_per_md), {"task_id": t.id}))
    for it in db.scalars(select(ProcurementItem).where(ProcurementItem.project_id == p.id,
                                                       ProcurementItem.status != ProcStatus.CANCELLED)
                         .order_by(ProcurementItem.id)).all():
        uc = it.actual_unit_cost if it.actual_unit_cost is not None else it.unit_cost
        lines.append(Line("procurement", it.wbs_id, it.name, it.qty, it.unit, uc, money(it.qty * uc),
                          {"procurement_item_id": it.id, "category": it.category.value, "status": it.status.value}))
    return lines, warnings


def wbs_tree(db: Session, project_id: int, lines: list[Line]) -> list[dict]:
    """Roll-up biaya & harga per WBS ke parent-nya (dinamis)."""
    nodes = {w.id: w for w in db.scalars(select(WBS).where(WBS.project_id == project_id))}
    agg = defaultdict(lambda: defaultdict(Decimal))
    own = defaultdict(list)
    for l in lines:
        own[l.wbs_id].append(l)
        cur = l.wbs_id
        while cur is not None and cur in nodes:
            a = agg[cur]
            a["service_cost"] += l.cost if l.kind != "procurement" else 0
            a["procurement_cost"] += l.cost if l.kind == "procurement" else 0
            a["md"] += l.qty if l.kind == "service" else 0
            a["price"] += l.amount
            cur = nodes[cur].parent_id
    children = defaultdict(list)
    for n in nodes.values():
        children[n.parent_id].append(n)

    def key(n):
        return [x.zfill(6) for x in n.code.split(".")]

    def build(n):
        a = agg[n.id]
        return {"wbs_id": n.id, "code": n.code, "name": n.name, "md": a["md"],
                "service_cost": money(a["service_cost"]), "procurement_cost": money(a["procurement_cost"]),
                "direct_cost": money(a["service_cost"] + a["procurement_cost"]), "price": money(a["price"]),
                "lines": [line_dict(l) for l in own[n.id]],
                "children": [build(c) for c in sorted(children[n.id], key=key)]}

    return [build(n) for n in sorted(children[None], key=key)]


def line_dict(l: Line) -> dict:
    return {"kind": l.kind, "wbs_id": l.wbs_id, "description": l.description, "qty": l.qty, "unit": l.unit,
            "unit_cost": l.unit_cost, "cost": l.cost, "unit_price": l.unit_price, "amount": l.amount, **l.ref}
