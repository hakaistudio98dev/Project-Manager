"""Data demo Tahap 1 + 2 + 3 — project "VMS Development" yang sedang berjalan.

Tanggal dibuat relatif terhadap hari ini, jadi dashboard selalu menampilkan
project di tengah jalan (±3 bulan berjalan, 3 bulan tersisa) dengan CPI/SPI ~0,9.

Jalankan di database kosong:  python seed.py
Login (password: demo12345):
  admin@demo.id | pm@demo.id | finance@demo.id | mgmt@demo.id | sales@demo.id | dev@demo.id
"""
from datetime import date, timedelta
from decimal import Decimal
from decimal import Decimal as D

from fastapi.testclient import TestClient

from app.main import app

PW = "demo12345"
c = TestClient(app)
TODAY = date.today()
T0 = TODAY - timedelta(days=86)          # project mulai ±3 bulan lalu


def d(offset: int) -> date:
    return T0 + timedelta(days=offset)


def ok(r):
    assert r.status_code in (200, 201), r.text
    return r.json()


def login(email):
    return {"Authorization": "Bearer " + ok(c.post("/api/auth/login", json={"email": email, "password": PW}))["access_token"]}


def weekdays(a: date, b: date):
    x = a
    while x <= b:
        if x.weekday() < 5:
            yield x
        x += timedelta(days=1)


print("Membuat organisasi & user…")
ok(c.post("/api/auth/bootstrap", json={"org_name": "YTEX Corp", "admin_email": "admin@demo.id",
                                       "admin_name": "Administrator", "admin_password": PW}))
A = login("admin@demo.id")
uid = {}
for email, name, role in [("pm@demo.id", "Project Manager", "pm"), ("finance@demo.id", "Finance", "finance"),
                          ("mgmt@demo.id", "Direktur", "management"), ("sales@demo.id", "Account Manager", "sales"),
                          ("dev@demo.id", "Budi (Backend)", "staff")]:
    uid[role] = ok(c.post("/api/users", headers=A, json={"email": email, "full_name": name, "password": PW, "role": role}))["id"]
PM, FIN, MG, DEV = login("pm@demo.id"), login("finance@demo.id"), login("mgmt@demo.id"), login("dev@demo.id")
SALES = login("sales@demo.id")

print("Master pajak & setting keuangan…")
ok(c.post("/api/taxes", headers=FIN, json={"code": "PPN12", "name": "PPN 12% (DPP nilai lain 11/12)", "kind": "vat",
                                           "rate": "0.12", "dpp_num": 11, "dpp_den": 12, "effective_date": "2025-01-01"}))
ok(c.post("/api/taxes", headers=FIN, json={"code": "PPH23", "name": "PPh 23 jasa teknik", "kind": "withholding",
                                           "rate": "0.02", "effective_date": "2009-01-01"}))
ok(c.patch("/api/finance/settings", headers=FIN, json={
    "is_pkp": True, "default_vat_code": "PPN12", "default_wht_code": "PPH23", "overhead_pct": "0.10",
    "price_rounding": 1_000_000, "quote_mgmt_threshold": 250_000_000, "payment_term_days": 30, "opening_cash": 0}))

# Rate per role — tabel PRD §9 (20 hari × 0,9 = 18 productive MD)
emp = {}
for name, role, loaded, user in [("Sari", "UI/UX", 10_000_000, None), ("Dimas", "Frontend", 12_500_000, None),
                                 ("Budi", "Backend", 15_000_000, uid["staff"]), ("Rizky", "DevOps", 15_000_000, None),
                                 ("Nina", "QA", 9_000_000, None)]:
    emp[role] = ok(c.post("/api/employees", headers=FIN, json={"name": name, "job_role": role, "user_id": user}))["id"]
    ok(c.post(f"/api/employees/{emp[role]}/rates", headers=FIN, json={
        "effective_date": str(T0 - timedelta(days=365)), "salary": loaded, "working_days": 20, "productive_capacity": "0.9"}))

print("Membuat project, WBS, task…")
cust = ok(c.post("/api/customers", headers=PM, json={"name": "PT Klien Demo"}))["id"]
pid = ok(c.post("/api/projects", headers=PM, json={
    "code": "VMS-DEV", "name": "VMS Development", "customer_id": cust, "contract_value": 250_000_000,
    "start_date": str(d(0)), "end_date": str(d(182)), "status": "active", "target_margin": "0.30", "tax_rate": "0.11"}))["id"]
wbs = {w["name"]: w["id"] for w in ok(c.post(f"/api/projects/{pid}/wbs/template", headers=PM))}
for role in emp:
    ok(c.post(f"/api/projects/{pid}/members", headers=PM, json={"employee_id": emp[role]}))

# (wbs, nama, role, mulai, selesai, MD, progress hari ini, milestone)
PLAN = [
    ("Requirement", "Workshop & SRS", "UI/UX", 0, 13, 8, 100, False),
    ("UI/UX", "Wireframe & UI kit", "UI/UX", 14, 48, 25, 100, False),
    ("DevOps", "CI/CD & server setup", "DevOps", 5, 45, 15, 100, False),
    ("Backend", "Camera & stream API", "Backend", 14, 91, 45, 72, False),
    ("Frontend", "Dashboard VMS", "Frontend", 46, 136, 50, 40, False),
    ("Backend", "AI event pipeline", "Backend", 92, 152, 35, 0, False),
    ("QA", "System & regression test", "QA", 110, 165, 20, 0, False),
    ("UAT", "UAT bersama klien", "QA", 160, 172, 6, 0, True),
    ("Deployment", "Go-live", "DevOps", 170, 178, 6, 0, True),
]
tasks = []
for w, name, role, s, e, md, prog, ms in PLAN:
    t = ok(c.post(f"/api/projects/{pid}/tasks", headers=PM, json={
        "wbs_id": wbs[w], "name": name, "owner_employee_id": emp[role], "planned_start": str(d(s)),
        "planned_end": str(d(e)), "estimate_md": md, "is_milestone": ms}))
    tasks.append((t, role, s, e, md, prog))

print("Katalog & BOQ pengadaan…")
vendor = ok(c.post("/api/vendors", headers=FIN, json={"name": "PT Distributor CCTV Nusantara"}))["id"]
vendor2 = ok(c.post("/api/vendors", headers=FIN, json={"name": "CV Instalasi Jaringan"}))["id"]
cat = {}
for code, name, category, unit, cost, lead, v in [
        ("CAM-IP-4MP", "IP Camera 4MP Bullet IR 30m", "hardware", "unit", 2_500_000, 14, vendor),
        ("NVR-64", "NVR 64 channel 8 HDD bay", "hardware", "unit", 32_000_000, 21, vendor),
        ("SRV-GPU", "Server AI (GPU 24GB, 64GB RAM)", "hardware", "unit", 65_000_000, 30, vendor),
        ("LIC-VMS-CH", "Lisensi VMS per channel", "software_license", "channel", 450_000, 3, vendor),
        ("HDD-8TB", "HDD surveillance 8TB", "hardware", "unit", 3_200_000, 7, vendor)]:
    cat[code] = ok(c.post("/api/catalog", headers=PM, json={"code": code, "name": name, "category": category, "unit": unit,
                                                           "unit_cost": cost, "lead_time_days": lead, "vendor_id": v}))["id"]
boq = {}
for key, w, code, qty, need in [("cam", "Deployment", "CAM-IP-4MP", 24, 110), ("nvr", "Deployment", "NVR-64", 1, 100),
                                ("hdd", "Deployment", "HDD-8TB", 8, 105), ("gpu", "DevOps", "SRV-GPU", 1, 80),
                                ("lic", "Deployment", "LIC-VMS-CH", 24, 165)]:
    boq[key] = ok(c.post(f"/api/projects/{pid}/procurements", headers=PM, json={
        "wbs_id": wbs[w], "catalog_item_id": cat[code], "qty": qty, "need_by_date": str(d(need))}))["id"]
boq["sub"] = ok(c.post(f"/api/projects/{pid}/procurements", headers=PM, json={
    "wbs_id": wbs["Deployment"], "name": "Subkon instalasi & kabel 24 titik", "category": "subcontract", "qty": 1,
    "unit": "lot", "unit_cost": 18_000_000, "vendor_id": vendor2, "lead_time_days": 7, "need_by_date": str(d(120))}))["id"]

print("Budget & baseline…")
b = ok(c.post(f"/api/projects/{pid}/budgets/generate", headers=PM, json={
    "planned_expenses": [{"category": "cloud", "amount": 12_000_000, "wbs_id": wbs["DevOps"], "description": "Cloud storage & backup"},
                         {"category": "api", "amount": 4_000_000, "wbs_id": wbs["Backend"], "description": "Lisensi SDK kamera"}],
    "contingency_pct": "0.10", "management_reserve_amount": 10_000_000, "notes": "Estimasi awal"}))
ok(c.post(f"/api/budgets/{b['id']}/submit", headers=PM))
ok(c.post(f"/api/budgets/{b['id']}/approve", headers=FIN, json={"reason": "Sesuai estimasi"}))

print("Quotation → approval berjenjang → kontrak…")
q = ok(c.post("/api/quotations", headers=SALES, json={
    "project_id": pid, "title": "Pengembangan & implementasi VMS berbasis AI", "discount": 5_000_000,
    "procurement_margin": "0.15",
    "scope": "Stream kamera multi-vendor, dashboard VMS, AI event pipeline", "valid_until": str(TODAY + timedelta(days=30)),
    "terms": [{"name": "Kickoff", "pct": "0.30", "offset_days": 0}, {"name": "Development", "pct": "0.30", "offset_days": 60},
              {"name": "UAT", "pct": "0.30", "offset_days": 165}, {"name": "Handover", "pct": "0.10", "offset_days": 182}]}))
ok(c.post(f"/api/quotations/{q['id']}/submit", headers=SALES))
q = ok(c.post(f"/api/quotations/{q['id']}/approve", headers=FIN, json={"reason": "Margin & pajak sesuai"}))
if q["status"] == "pending_management":
    ok(c.post(f"/api/quotations/{q['id']}/approve", headers=MG, json={"reason": "Nilai di atas threshold, disetujui"}))
ok(c.post(f"/api/quotations/{q['id']}/send", headers=SALES))
ok(c.post(f"/api/quotations/{q['id']}/accept", headers=SALES))
ok(c.post("/api/contracts", headers=SALES, json={"quotation_id": q["id"], "contract_no": f"KTR/{T0:%Y}/VMS/001",
                                                 "signed_date": str(d(-3)), "warranty_months": 6,
                                                 "sla": "Respon insiden P1 ≤ 4 jam"}))
print(f"  Harga {q['subtotal']} (margin {q['margin']}), total + PPN {q['total']}")

ok(c.post(f"/api/projects/{pid}/baseline", headers=PM, json={"reason": "Baseline kickoff"}))

print("Timesheet (bisa ±30 detik)…")
OVERRUN = Decimal("1.24")  # effort aktual 24% di atas rencana → CPI ≈ 0,9 (setelah contingency)
for t, role, s, e, md, prog in tasks:
    if prog == 0:
        continue
    days = list(weekdays(d(s), min(d(e), TODAY - timedelta(days=1))))
    total_h = Decimal(md) * Decimal(prog) / 100 * OVERRUN * 8
    per_day = (total_h / len(days)).quantize(Decimal("0.25"))
    for day in days:
        hdr = DEV if role == "Backend" else PM
        body = {"project_id": pid, "task_id": t["id"], "work_date": str(day), "hours": str(per_day)}
        if role != "Backend":
            body["employee_id"] = emp[role]
        ts = ok(c.post("/api/timesheets", headers=hdr, json=body))
        ok(c.post(f"/api/timesheets/{ts['id']}/submit", headers=hdr))
        ok(c.post(f"/api/timesheets/{ts['id']}/approve", headers=PM, json={}))

for amt, dd, cat, desc in [(3_000_000, 10, "cloud", "Cloud bulan 1"), (3_000_000, 40, "cloud", "Cloud bulan 2"),
                           (3_500_000, 70, "cloud", "Cloud bulan 3"), (4_000_000, 20, "api", "Lisensi SDK kamera")]:
    wid = wbs["DevOps"] if cat == "cloud" else wbs["Backend"]
    ex = ok(c.post("/api/expenses", headers=PM, json={"project_id": pid, "wbs_id": wid, "category": cat,
                                                      "amount": amt, "expense_date": str(d(dd)), "description": desc}))
    ok(c.post(f"/api/expenses/{ex['id']}/submit", headers=PM))
    ok(c.post(f"/api/expenses/{ex['id']}/approve", headers=FIN, json={}))

# Server GPU sudah dipesan & diterima → biaya masuk actual setelah approval Finance
ok(c.post(f"/api/procurements/{boq['gpu']}/order", headers=PM, json={"po_number": "PO/VMS/001", "order_date": str(d(45)),
                                                                     "actual_unit_cost": 63_500_000}))
rc = ok(c.post(f"/api/procurements/{boq['gpu']}/receive", headers=PM, json={"received_date": str(d(74))}))
ok(c.post(f"/api/expenses/{rc['expense_id']}/approve", headers=FIN, json={"reason": "Sesuai PO & BAST"}))
ok(c.post(f"/api/procurements/{boq['cam']}/order", headers=PM, json={"po_number": "PO/VMS/002", "order_date": str(d(84))}))

print("Snapshot EVM historis…")
for cp in [d(30), d(60), TODAY]:
    for t, role, s, e, md, prog in tasks:
        if prog == 0:
            continue
        span_to_today = max((min(d(e), TODAY) - d(s)).days, 1)
        frac = max(0, min((cp - d(s)).days / span_to_today, 1))
        p_now = round(prog * frac)
        status = "done" if p_now >= 100 else ("in_progress" if p_now > 0 else "todo")
        ok(c.patch(f"/api/tasks/{t['id']}", headers=PM, json={"progress_pct": p_now, "status": status}))
    ok(c.post(f"/api/projects/{pid}/evm/snapshots", headers=PM, json={"as_of": str(cp)}))

print("Risiko & change request…")
for r in [{"title": "Vendor kamera mengubah API stream", "probability": 3, "impact": 4, "category": "Teknis",
           "mitigation": "Buat abstraction layer adapter per vendor"},
          {"title": "Model AI butuh GPU lebih besar dari rencana", "probability": 3, "impact": 3, "category": "Biaya",
           "mitigation": "Benchmark model ringan (INT8) sebelum pengadaan"},
          {"title": "Klien terlambat menyediakan data rekaman untuk training", "probability": 4, "impact": 4,
           "category": "Jadwal", "mitigation": "Pakai dataset publik sementara; eskalasi di weekly meeting"}]:
    ok(c.post(f"/api/projects/{pid}/risks", headers=PM, json=r))

cr1 = ok(c.post(f"/api/projects/{pid}/change-requests", headers=PM, json={
    "title": "Fitur pencarian wajah (face search)", "requested_by": "PT Klien Demo", "reason": "Permintaan tambahan klien",
    "schedule_impact_days": 7, "revenue_delta": 30_000_000,
    "lines": [{"line_type": "add_task", "wbs_id": wbs["Backend"], "name": "Face search service",
               "employee_id": emp["Backend"], "md_delta": 15, "planned_start": str(d(150)), "planned_end": str(d(175))}]}))
ok(c.post(f"/api/change-requests/{cr1['id']}/submit", headers=PM))
ok(c.post(f"/api/change-requests/{cr1['id']}/approve", headers=MG, json={"reason": "Disetujui, masuk addendum"}))

cr2 = ok(c.post(f"/api/projects/{pid}/change-requests", headers=PM, json={
    "title": "Tambahan 20 MD Backend untuk integrasi NVR lama", "requested_by": "PT Klien Demo",
    "schedule_impact_days": 14, "revenue_delta": 0,
    "lines": [{"line_type": "add_task", "wbs_id": wbs["Backend"], "name": "Integrasi NVR lama",
               "employee_id": emp["Backend"], "md_delta": 20}]}))
ok(c.post(f"/api/change-requests/{cr2['id']}/submit", headers=PM))

# Termin change order dari CR-001 (+ revenue 30 jt)
ct = ok(c.get(f"/api/projects/{pid}/contract", headers=FIN))
ok(c.post(f"/api/contracts/{ct['id']}/milestones", headers=FIN, json={
    "name": "Change order: face search", "change_request_id": cr1["id"], "planned_date": str(d(176))}))

print("Invoice & pembayaran…")
ms = ok(c.get(f"/api/projects/{pid}/milestones", headers=FIN))
inv1 = ok(c.post("/api/invoices/from-milestone", headers=FIN, json={"milestone_id": ms[0]["id"]}))
inv1 = ok(c.post(f"/api/invoices/{inv1['id']}/issue", headers=FIN, json={"issue_date": str(d(0))}))
ok(c.post("/api/payments", headers=FIN, json={"invoice_id": inv1["id"], "payment_date": str(d(26)),
                                              "amount": str(D(inv1["total"]) - D(inv1["wht_amount"])),
                                              "wht_amount": inv1["wht_amount"], "reference": "TRF-BCA-0001"}))
inv2 = ok(c.post("/api/invoices/from-milestone", headers=FIN, json={"milestone_id": ms[1]["id"]}))
ok(c.post(f"/api/invoices/{inv2['id']}/issue", headers=FIN, json={"issue_date": str(d(62))}))  # jatuh tempo ±6 hari lagi

e = ok(c.get(f"/api/projects/{pid}/evm", headers=PM))
print(f"\nSelesai. BAC {e['bac']} | CPI {e['cpi']} | SPI {e['spi']} | EAC {e['eac']}")
print("Buka http://localhost:8000 → login pm@demo.id atau mgmt@demo.id (password demo12345)")
