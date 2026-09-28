"""Tahap 3: pricing & pajak, quotation berjenjang, kontrak, invoice, payment, AR, cashflow, profitability.

Angka (rate Rp1 jt/MD):
  Labor 80 MD = 80 jt · contingency 10% = 8 jt · overhead 10% × direct = 8 jt → total cost 96 jt
  Harga = 96 / (1 − 0,30) = 137,14 jt → dibulatkan 138 jt · diskon 3 jt → subtotal 135 jt
  Margin = 39/135 = 28,89% < target 30% → wajib approval management
  PPN 12% × DPP 11/12 = 11% efektif · PPh 23 2%
  Termin 1 (30%) = 40,5 jt → DPP 37,125 jt, PPN 4,455 jt, total 44,955 jt, PPh 0,81 jt
"""
from decimal import Decimal as D

from tests.conftest import login

PW = "rahasia123"


def N(x):
    return D(str(x))


def test_phase3_flow(client):
    r = client.post("/api/auth/bootstrap", json={"org_name": "YTEX", "admin_email": "admin@ytex.id",
                                                  "admin_name": "Admin", "admin_password": PW})
    assert r.status_code in (201, 409)
    admin = login(client, "admin@ytex.id", PW)
    for email, role in [("pm3@ytex.id", "pm"), ("fin3@ytex.id", "finance"), ("dir3@ytex.id", "management"),
                        ("sales3@ytex.id", "sales")]:
        client.post("/api/users", headers=admin, json={"email": email, "full_name": role, "password": PW, "role": role})
    pm, fin, mgmt, sales = (login(client, f"{x}3@ytex.id", PW) for x in ("pm", "fin", "dir", "sales"))

    # --- Master pajak (effective date) & setting ---
    for body in [{"code": "PPN12", "name": "PPN 12% DPP nilai lain", "kind": "vat", "rate": "0.12",
                  "dpp_num": 11, "dpp_den": 12, "effective_date": "2025-01-01"},
                 {"code": "PPH23", "name": "PPh 23 jasa", "kind": "withholding", "rate": "0.02",
                  "effective_date": "2020-01-01"},
                 {"code": "PPNTEST", "name": "uji tanggal", "kind": "vat", "rate": "0.10", "effective_date": "2020-01-01"},
                 {"code": "PPNTEST", "name": "uji tanggal", "kind": "vat", "rate": "0.12", "effective_date": "2026-07-01"}]:
        assert client.post("/api/taxes", headers=fin, json=body).status_code == 201
    assert client.post("/api/taxes", headers=pm, json={"code": "X1", "name": "x", "kind": "vat", "rate": "0.1",
                                                       "effective_date": "2020-01-01"}).status_code == 403
    s = client.patch("/api/finance/settings", headers=fin, json={
        "default_vat_code": "PPN12", "default_wht_code": "PPH23", "overhead_pct": "0.10",
        "price_rounding": 1_000_000, "quote_mgmt_threshold": 500_000_000}).json()
    assert s["default_vat_code"] == "PPN12"

    # --- Kalkulator harga: contoh PRD (cost 350 jt, margin 30% → 500 jt) ---
    c = client.post("/api/pricing/calculate", headers=sales, json={
        "total_cost": 350_000_000, "target_margin": "0.30", "vat_code": "PPN12", "wht_code": "PPH23"}).json()
    assert N(c["subtotal"]) == 500_000_000 and N(c["vat"]) == 55_000_000 and N(c["customer_total"]) == 555_000_000
    assert N(c["wht"]) == 10_000_000 and N(c["margin"]) == D("0.3")
    t1 = client.post("/api/pricing/calculate", headers=sales, json={"total_cost": 70, "target_margin": 0.3,
                     "vat_code": "PPNTEST", "rounding": 0, "on": "2026-06-30"}).json()
    t2 = client.post("/api/pricing/calculate", headers=sales, json={"total_cost": 70, "target_margin": 0.3,
                     "vat_code": "PPNTEST", "rounding": 0, "on": "2026-07-01"}).json()
    assert N(t1["vat"]) == 10 and N(t2["vat"]) == 12   # tarif mengikuti effective date

    # --- Project + budget ---
    emp = client.post("/api/employees", headers=fin, json={"name": "Fajar", "job_role": "Backend"}).json()["id"]
    client.post(f"/api/employees/{emp}/rates", headers=fin, json={"effective_date": "2026-01-01",
                "salary": 18_000_000, "working_days": 20, "productive_capacity": "0.9"})
    cust = client.post("/api/customers", headers=sales, json={"name": "PT Pembeli"}).json()["id"]
    pid = client.post("/api/projects", headers=pm, json={
        "code": "FIN-T", "name": "Finance Test", "customer_id": cust, "status": "active",
        "start_date": "2026-06-01", "end_date": "2026-06-30", "target_margin": "0.30"}).json()["id"]
    wbs = client.post(f"/api/projects/{pid}/wbs", headers=pm, json={"name": "Backend"}).json()["id"]
    client.post(f"/api/projects/{pid}/members", headers=pm, json={"employee_id": emp, "allocation_pct": 10})
    ta = client.post(f"/api/projects/{pid}/tasks", headers=pm, json={"wbs_id": wbs, "name": "A", "owner_employee_id": emp,
                     "planned_start": "2026-06-01", "planned_end": "2026-06-15", "estimate_md": 50}).json()["id"]
    client.post(f"/api/projects/{pid}/tasks", headers=pm, json={"wbs_id": wbs, "name": "B", "owner_employee_id": emp,
                "planned_start": "2026-06-16", "planned_end": "2026-06-30", "estimate_md": 30})
    b = client.post(f"/api/projects/{pid}/budgets/generate", headers=pm, json={"contingency_pct": "0.10"}).json()
    client.post(f"/api/budgets/{b['id']}/submit", headers=pm)
    client.post(f"/api/budgets/{b['id']}/approve", headers=fin, json={})

    # --- Quotation ---
    q = client.post("/api/quotations", headers=sales, json={"project_id": pid, "title": "Sistem X", "discount": 3_000_000})
    assert q.status_code == 201, q.text
    q = q.json()
    assert N(q["total_cost"]) == 96_000_000 and N(q["overhead"]) == 8_000_000
    assert N(q["list_price"]) == 138_000_000 and N(q["subtotal"]) == 135_000_000
    assert N(q["margin"]) == D("0.2889") and q["requires_management"] is True
    assert N(q["vat_amount"]) == 14_850_000 and N(q["total"]) == 149_850_000 and N(q["wht_amount"]) == 2_700_000
    assert [N(t["amount"]) for t in q["terms"]] == [40_500_000, 40_500_000, 40_500_000, 13_500_000]
    qid = q["id"]
    client.post(f"/api/quotations/{qid}/submit", headers=sales)
    assert client.post(f"/api/quotations/{qid}/approve", headers=mgmt, json={}).status_code == 403  # finance dulu
    assert client.post(f"/api/quotations/{qid}/approve", headers=fin, json={}).json()["status"] == "pending_management"
    assert client.post(f"/api/quotations/{qid}/approve", headers=fin, json={}).status_code == 403
    assert client.post(f"/api/quotations/{qid}/approve", headers=mgmt, json={"reason": "Diskon strategis"}).json()["status"] == "approved"
    client.post(f"/api/quotations/{qid}/send", headers=sales)
    assert client.post(f"/api/quotations/{qid}/accept", headers=sales).json()["status"] == "accepted"
    assert client.post("/api/quotations", headers=sales, json={"project_id": pid, "title": "lagi"}).status_code == 409

    # --- Kontrak ---
    ct = client.post("/api/contracts", headers=sales, json={"quotation_id": qid, "contract_no": "KTR/001",
                     "signed_date": "2026-05-28"})
    assert ct.status_code == 201, ct.text
    proj = client.get(f"/api/projects/{pid}", headers=pm).json()
    assert N(proj["contract_value"]) == 135_000_000 and N(proj["tax_rate"]) == D("0.11")
    ms = client.get(f"/api/projects/{pid}/milestones", headers=fin).json()
    assert len(ms) == 4 and sum(N(m["amount"]) for m in ms) == 135_000_000
    client.patch(f"/api/milestones/{ms[1]['id']}", headers=pm, json={"task_id": ta})

    # --- Invoice termin 1 & pembayaran ---
    inv = client.post("/api/invoices/from-milestone", headers={**fin, "Idempotency-Key": "inv-1"},
                      json={"milestone_id": ms[0]["id"]})
    assert inv.status_code == 201, inv.text
    dup = client.post("/api/invoices/from-milestone", headers={**fin, "Idempotency-Key": "inv-1"},
                      json={"milestone_id": ms[0]["id"]})
    assert dup.status_code == 200 and dup.json()["id"] == inv.json()["id"]
    inv = client.post(f"/api/invoices/{inv.json()['id']}/issue", headers=fin,
                      json={"issue_date": "2026-06-05", "due_days": 30}).json()
    assert inv["invoice_no"] == "INV/2026/06/0001" and inv["due_date"] == "2026-07-05"
    assert N(inv["dpp"]) == 37_125_000 and N(inv["vat_amount"]) == 4_455_000 and N(inv["total"]) == 44_955_000
    assert N(inv["wht_amount"]) == 810_000
    pay = {"invoice_id": inv["id"], "payment_date": "2026-06-20", "amount": 44_145_000, "wht_amount": 810_000}
    p1 = client.post("/api/payments", headers={**fin, "Idempotency-Key": "pay-1"}, json=pay)
    assert p1.status_code == 201
    p2 = client.post("/api/payments", headers={**fin, "Idempotency-Key": "pay-1"}, json=pay)
    assert p2.status_code == 200 and p2.json()["id"] == p1.json()["id"]      # retry aman
    assert client.get(f"/api/invoices/{inv['id']}", headers=fin).json()["invoice"]["status"] == "paid"
    assert client.post("/api/payments", headers=fin, json={**pay, "amount": 1}).status_code == 409  # sudah lunas

    # --- Invoice termin 2: syarat task ---
    assert client.post("/api/invoices/from-milestone", headers=fin, json={"milestone_id": ms[1]["id"]}).status_code == 422
    inv2 = client.post("/api/invoices/from-milestone", headers=fin, json={"milestone_id": ms[1]["id"], "force": True}).json()
    inv2 = client.post(f"/api/invoices/{inv2['id']}/issue", headers=fin,
                       json={"issue_date": "2026-07-01", "due_days": 10}).json()
    assert inv2["display_status"] == "overdue"
    assert client.post("/api/payments", headers=fin, json={"invoice_id": inv2["id"], "payment_date": "2026-07-05",
                       "amount": 50_000_000}).status_code == 422                                  # overpayment
    part = client.post("/api/payments", headers=fin, json={"invoice_id": inv2["id"], "payment_date": "2026-07-05",
                       "amount": 10_000_000}).json()
    assert client.get(f"/api/invoices/{inv2['id']}", headers=fin).json()["invoice"]["status"] == "partially_paid"

    aging = client.get(f"/api/ar/aging?project_id={pid}", headers=fin).json()
    assert N(aging["total_outstanding"]) == 34_955_000 and N(aging["overdue"]) == 34_955_000

    dash = client.get("/api/dashboard", headers=fin).json()
    assert any(a["rule"] == "invoice_overdue" and a["project_code"] == "FIN-T" for a in dash["alerts"])
    assert N(dash["portfolio"]["ar_outstanding"]) >= 34_955_000

    # --- Cashflow ---
    t = client.post("/api/timesheets", headers=pm, json={"project_id": pid, "task_id": ta, "employee_id": emp,
                    "work_date": "2026-06-10", "hours": 8})
    client.post(f"/api/timesheets/{t.json()['id']}/submit", headers=pm)
    client.post(f"/api/timesheets/{t.json()['id']}/approve", headers=pm, json={})
    cf = client.get(f"/api/projects/{pid}/cashflow", headers=fin).json()
    jun = next(m for m in cf["months"] if m["month"] == "2026-06")
    assert N(jun["cash_in"]) == 44_145_000 and N(jun["vat_in_cash_in"]) == 4_455_000
    assert N(jun["cash_out_labor"]) == 1_000_000
    assert N(cf["actual_cash_in"]) == 54_145_000

    # --- Baseline + profitability 4 lensa ---
    client.post(f"/api/projects/{pid}/baseline", headers=pm, json={})
    pr = client.get(f"/api/projects/{pid}/profitability", headers=fin).json()
    assert N(pr["planned"]["revenue"]) == 135_000_000 and N(pr["planned"]["total_cost"]) == 96_000_000
    assert N(pr["planned"]["margin"]) == D("0.2889") and pr["planned"]["below_target"] is True
    assert N(pr["baseline"]["total_cost"]) == 96_000_000   # BAC 88 jt + overhead 8 jt
    assert N(pr["actual"]["invoiced_revenue"]) == 81_000_000 and N(pr["actual"]["cash_collected"]) == 54_145_000
    assert pr["forecast"] is not None
    rep = client.get("/api/reports/profitability", headers=mgmt).json()
    assert any(x["project_code"] == "FIN-T" for x in rep["projects"])
    assert client.get("/api/reports/profitability", headers=pm).status_code == 403

    # --- Reversal & cancel ---
    client.post(f"/api/payments/{part['id']}/reverse", headers=fin, json={"reason": "salah alokasi"})
    assert client.get(f"/api/invoices/{inv2['id']}", headers=fin).json()["invoice"]["status"] == "issued"
    r = client.post(f"/api/invoices/{inv2['id']}/cancel", headers=fin, json={"reason": "Salah termin"}).json()
    assert r["status"] == "cancelled"
    ms = client.get(f"/api/projects/{pid}/milestones", headers=fin).json()
    assert ms[1]["status"] == "planned"   # milestone bisa ditagih ulang

    # --- Opsi EVM: contingency terpisah ---
    e1 = client.get(f"/api/projects/{pid}/evm", headers=pm).json()
    client.patch("/api/finance/settings", headers=fin, json={"evm_bac_includes_contingency": False})
    e2 = client.get(f"/api/projects/{pid}/evm", headers=pm).json()
    assert N(e1["bac"]) == 88_000_000 and N(e2["bac"]) == 80_000_000
    client.patch("/api/finance/settings", headers=fin, json={"evm_bac_includes_contingency": True})
