"""Tahap 3b: katalog, BOQ per WBS, estimasi dinamis, penawaran terperinci, realisasi pengadaan.

Jasa: Backend 20 MD + DevOps 10 MD @Rp1 jt = 30 jt
BOQ : 8 kamera @2,5 jt = 20 jt · 1 NVR 15 jt · 8 lisensi @0,5 jt = 4 jt  (WBS Deployment)
      subkon instalasi 6 jt (WBS DevOps)                                  → pengadaan 45 jt
Direct 75 jt · contingency 10% 7,5 jt · overhead 10% 7,5 jt → total cost 90 jt (load 1,2)
Margin jasa 30%, margin pengadaan 15%: 36/0,7 + 54/0,85 = 114,96 jt → dibulatkan 115 jt
"""
from decimal import Decimal as D

from tests.conftest import login

PW = "rahasia123"
N = lambda x: D(str(x))  # noqa: E731


def test_procurement_flow(client):
    r = client.post("/api/auth/bootstrap", json={"org_name": "YTEX", "admin_email": "admin@ytex.id",
                                                  "admin_name": "Admin", "admin_password": PW})
    assert r.status_code in (201, 409)
    admin = login(client, "admin@ytex.id", PW)
    for email, role in [("pm4@ytex.id", "pm"), ("fin4@ytex.id", "finance"), ("sales4@ytex.id", "sales")]:
        client.post("/api/users", headers=admin, json={"email": email, "full_name": role, "password": PW, "role": role})
    pm, fin, sales = (login(client, f"{x}4@ytex.id", PW) for x in ("pm", "fin", "sales"))
    client.patch("/api/finance/settings", headers=fin, json={"overhead_pct": "0.10", "price_rounding": 1_000_000,
                                                              "quote_mgmt_threshold": 500_000_000})
    if not any(t["code"] == "PPN12" for t in client.get("/api/taxes", headers=fin).json()):
        client.post("/api/taxes", headers=fin, json={"code": "PPN12", "name": "PPN", "kind": "vat", "rate": "0.12",
                                                      "dpp_num": 11, "dpp_den": 12, "effective_date": "2025-01-01"})
        client.patch("/api/finance/settings", headers=fin, json={"default_vat_code": "PPN12"})

    vendor = client.post("/api/vendors", headers=fin, json={"name": "PT Distributor CCTV"}).json()["id"]
    cat = {}
    for code, name, category, unit, cost, lead in [
            ("CAM-4MP", "IP Camera 4MP Bullet", "hardware", "unit", 2_500_000, 14),
            ("NVR-32", "NVR 32 channel", "hardware", "unit", 15_000_000, 21),
            ("LIC-VMS", "Lisensi VMS per channel", "software_license", "channel", 500_000, 3)]:
        r = client.post("/api/catalog", headers=pm, json={"code": code, "name": name, "category": category,
                        "unit": unit, "unit_cost": cost, "vendor_id": vendor, "lead_time_days": lead})
        assert r.status_code == 201, r.text
        cat[code] = r.json()["id"]
    assert client.post("/api/catalog", headers=pm, json={"code": "CAM-4MP", "name": "x", "category": "hardware",
                                                         "unit_cost": 1}).status_code == 409

    emp = client.post("/api/employees", headers=fin, json={"name": "Gilang", "job_role": "Backend"}).json()["id"]
    client.post(f"/api/employees/{emp}/rates", headers=fin, json={"effective_date": "2026-01-01", "salary": 18_000_000,
                                                                 "working_days": 20, "productive_capacity": "0.9"})
    pid = client.post("/api/projects", headers=pm, json={"code": "PRC-T", "name": "Integrasi CCTV", "status": "active",
                      "start_date": "2026-05-01", "end_date": "2026-05-31", "target_margin": "0.30"}).json()["id"]
    wbs = {w["name"]: w["id"] for w in client.post(f"/api/projects/{pid}/wbs/template", headers=pm).json()}
    client.post(f"/api/projects/{pid}/members", headers=pm, json={"employee_id": emp, "allocation_pct": 10})
    for w, md in [("Backend", 20), ("DevOps", 10)]:
        client.post(f"/api/projects/{pid}/tasks", headers=pm, json={"wbs_id": wbs[w], "name": f"Task {w}",
                    "owner_employee_id": emp, "planned_start": "2026-05-01", "planned_end": "2026-05-20", "estimate_md": md})

    # --- BOQ: dari katalog (otomatis isi harga/satuan/vendor/lead time) & manual ---
    cam = client.post(f"/api/projects/{pid}/procurements", headers=pm, json={
        "wbs_id": wbs["Deployment"], "catalog_item_id": cat["CAM-4MP"], "qty": 8, "need_by_date": "2026-05-20"}).json()
    assert N(cam["unit_cost"]) == 2_500_000 and cam["unit"] == "unit" and N(cam["total_cost"]) == 20_000_000
    assert cam["order_by_date"] == "2026-05-06"
    nvr = client.post(f"/api/projects/{pid}/procurements", headers=pm, json={
        "wbs_id": wbs["Deployment"], "catalog_item_id": cat["NVR-32"], "qty": 1, "need_by_date": "2026-05-20"}).json()
    lic = client.post(f"/api/projects/{pid}/procurements", headers=pm, json={
        "wbs_id": wbs["Deployment"], "catalog_item_id": cat["LIC-VMS"], "qty": 8}).json()
    sub = client.post(f"/api/projects/{pid}/procurements", headers=pm, json={
        "wbs_id": wbs["DevOps"], "name": "Subkon instalasi kabel", "category": "subcontract", "qty": 1,
        "unit": "lot", "unit_cost": 6_000_000}).json()
    assert client.post(f"/api/projects/{pid}/procurements", headers=pm, json={
        "wbs_id": wbs["DevOps"], "qty": 1}).status_code == 422

    # --- Estimasi dinamis ---
    e = client.get(f"/api/projects/{pid}/estimate?procurement_margin=0.15", headers=pm).json()
    assert N(e["direct_cost"]) == 75_000_000 and N(e["procurement_cost"]) == 45_000_000
    assert N(e["total_cost"]) == 90_000_000 and N(e["list_price"]) == 115_000_000
    assert N(e["margin"]) == D("0.2174")
    dep = next(n for n in e["tree"] if n["name"] == "Deployment")
    dev = next(n for n in e["tree"] if n["name"] == "DevOps")
    assert N(dep["direct_cost"]) == 39_000_000 and N(dev["direct_cost"]) == 16_000_000
    all_lines = [l for n in e["tree"] for l in n["lines"]]
    assert sum(N(l["amount"]) for l in all_lines) == 115_000_000          # Σ rincian = harga
    for l in all_lines:
        if l["kind"] == "procurement":
            assert N(l["unit_price"]) == N(l["unit_price"]).to_integral_value()   # harga satuan bulat
            assert N(l["amount"]) == N(l["unit_price"]) * N(l["qty"])
    same = client.get(f"/api/projects/{pid}/estimate", headers=pm).json()   # margin sama 30%
    assert N(same["list_price"]) == 129_000_000                              # 90 / 0,7 = 128,57 → 129

    # ubah qty kamera → WBS & harga terhitung ulang otomatis
    client.patch(f"/api/procurements/{cam['id']}", headers=pm, json={"qty": 10})
    e2 = client.get(f"/api/projects/{pid}/estimate?procurement_margin=0.15", headers=pm).json()
    assert N(next(n for n in e2["tree"] if n["name"] == "Deployment")["direct_cost"]) == 44_000_000
    assert N(e2["total_cost"]) == 96_000_000 and N(e2["list_price"]) == 123_000_000

    # refresh harga dari katalog
    client.patch(f"/api/catalog/{cat['LIC-VMS']}", headers=pm, json={"unit_cost": 550_000})
    rf = client.post(f"/api/projects/{pid}/procurements/refresh-prices", headers=pm).json()
    assert rf["updated"] == 1
    client.patch(f"/api/procurements/{lic['id']}", headers=pm, json={"unit_cost": 500_000})

    # --- Budget otomatis menarik BOQ ---
    b = client.post(f"/api/projects/{pid}/budgets/generate", headers=pm, json={"contingency_pct": "0.10"}).json()
    assert N(b["procurement_cost"]) == 50_000_000 and N(b["direct_cost"]) == 80_000_000
    assert sum(1 for l in b["lines"] if l["procurement_item_id"]) == 4
    client.post(f"/api/budgets/{b['id']}/submit", headers=pm)
    client.post(f"/api/budgets/{b['id']}/approve", headers=fin, json={})

    # --- Penawaran terperinci ---
    q = client.post("/api/quotations", headers=sales, json={"project_id": pid, "title": "Integrasi CCTV",
                    "procurement_margin": "0.15"})
    assert q.status_code == 201, q.text
    q = q.json()
    assert N(q["total_cost"]) == 96_000_000 and N(q["list_price"]) == 123_000_000
    assert N(q["list_price"]) == N(e2["list_price"])                       # estimasi = penawaran
    assert sum(N(l["amount"]) for l in q["lines"]) == 123_000_000
    kinds = {(l["kind"], l["description"]) for l in q["lines"]}
    assert ("service", "Jasa Backend") in kinds and ("procurement", "IP Camera 4MP Bullet") in kinds
    assert q["requires_management"] is False and N(q["procurement_margin"]) == D("0.15")

    # --- Realisasi: PO → terima → expense → approve → actual cost ---
    o = client.post(f"/api/procurements/{cam['id']}/order", headers=pm, json={
        "po_number": "PO-001", "order_date": "2026-05-05", "actual_unit_cost": 2_400_000}).json()
    assert o["status"] == "ordered"
    assert client.patch(f"/api/procurements/{cam['id']}", headers=pm, json={"qty": 1}).status_code == 409
    rc = client.post(f"/api/procurements/{cam['id']}/receive", headers=pm, json={"received_date": "2026-05-12"}).json()
    assert rc["status"] == "received" and rc["expense_id"]
    ex = client.post(f"/api/expenses/{rc['expense_id']}/approve", headers=fin, json={}).json()
    assert ex["status"] == "posted" and N(ex["amount"]) == 24_000_000 and ex["category"] == "hardware"
    cs = client.get(f"/api/projects/{pid}/cost-summary", headers=pm).json()
    assert N(cs["actual_expense"]) == 24_000_000

    # --- Alert batas pesan (NVR: dibutuhkan 20 Mei, lead time 21 hari) ---
    dash = client.get("/api/dashboard", headers=pm).json()
    assert any(a["rule"] == "procurement_order" and "NVR" in a["message"] for a in dash["alerts"])

    # --- Hapus vs batal ---
    assert client.delete(f"/api/procurements/{sub['id']}", headers=pm).status_code == 204
    items = {i["id"]: i for i in client.get(f"/api/projects/{pid}/procurements", headers=pm).json()}
    assert items[sub["id"]]["status"] == "cancelled"          # sudah dipakai budget → dibatalkan, bukan dihapus
    extra = client.post(f"/api/projects/{pid}/procurements", headers=pm, json={
        "wbs_id": wbs["UAT"], "name": "Konsumsi UAT", "category": "other", "qty": 1, "unit_cost": 500_000}).json()
    client.delete(f"/api/procurements/{extra['id']}", headers=pm)
    assert extra["id"] not in {i["id"] for i in client.get(f"/api/projects/{pid}/procurements", headers=pm).json()}
