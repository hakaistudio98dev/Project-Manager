"""Tahap 2: Baseline, EVM, Forecast, Risk, Change Request, Dashboard.

Skenario angka bulat (rate Rp1.000.000/MD, BAC Rp20 jt):
  Task A 1–10 Jan 2027 10 MD, Task B 11–20 Jan 2027 10 MD.
  As of 10 Jan: PV = 10 jt; A progress 80% → EV = 8 jt; timesheet 9 MD → AC = 9 jt.
  CPI = 0,8889, SPI = 0,8
  EAC(cpi) = 22,5 jt | EAC(cpi_spi) = 9 + 12/(0,8889×0,8) = 25,875 jt
  EAC(budget_rate) = 21 jt | EAC(bottom_up) = 9 + (2 + 10) = 21 jt | TCPI = 12/11 = 1,0909
"""
from datetime import date, timedelta
from decimal import Decimal as D

from app.services import evm as E
from app.models import ProgressMethod as PM_, TaskStatus as TS
from tests.conftest import login

PW = "rahasia123"


def test_pure_functions():
    assert E.planned_fraction(date(2027, 1, 1), date(2027, 1, 10), date(2027, 1, 5)) == D("0.5")
    assert E.earned_fraction(PM_.FIFTY_FIFTY, TS.IN_PROGRESS, D(10)) == D("0.5")
    assert E.earned_fraction(PM_.ZERO_HUNDRED, TS.IN_PROGRESS, D(90)) == 0
    assert E.health(D("0.96"), D("0.91")) == "amber"
    f = E.forecast(D(100), D(40), D(50), D("0.8"), D("0.8"), D(70))
    assert f["cpi"]["eac"] == D("125.00") and f["bottom_up"]["eac"] == D("120.00")


def _setup(client):
    r = client.post("/api/auth/bootstrap", json={"org_name": "YTEX", "admin_email": "admin@ytex.id",
                                                  "admin_name": "Admin", "admin_password": PW})
    assert r.status_code in (201, 409)
    admin = login(client, "admin@ytex.id", PW)
    for email, role in [("pm2@ytex.id", "pm"), ("fin2@ytex.id", "finance"), ("dir2@ytex.id", "management")]:
        client.post("/api/users", headers=admin, json={"email": email, "full_name": role, "password": PW, "role": role})
    return admin, login(client, "pm2@ytex.id", PW), login(client, "fin2@ytex.id", PW), login(client, "dir2@ytex.id", PW)


def test_phase2_flow(client):
    admin, pm, fin, mgmt = _setup(client)
    emp = client.post("/api/employees", headers=fin, json={"name": "Eko", "job_role": "Backend"}).json()["id"]
    client.post(f"/api/employees/{emp}/rates", headers=fin, json={"effective_date": "2026-01-01",
                "salary": 18_000_000, "working_days": 20, "productive_capacity": "0.9"})
    pid = client.post("/api/projects", headers=pm, json={
        "code": "EVM-T", "name": "EVM Test", "contract_value": 30_000_000, "status": "active",
        "start_date": "2027-01-01", "end_date": "2027-01-31", "target_margin": "0.30"}).json()["id"]
    wbs = client.post(f"/api/projects/{pid}/wbs", headers=pm, json={"name": "Backend"}).json()["id"]
    client.post(f"/api/projects/{pid}/members", headers=pm, json={"employee_id": emp})
    ta = client.post(f"/api/projects/{pid}/tasks", headers=pm, json={"wbs_id": wbs, "name": "A",
                     "owner_employee_id": emp, "planned_start": "2027-01-01", "planned_end": "2027-01-10",
                     "estimate_md": 10}).json()["id"]
    client.post(f"/api/projects/{pid}/tasks", headers=pm, json={"wbs_id": wbs, "name": "B",
                "owner_employee_id": emp, "planned_start": "2027-01-11", "planned_end": "2027-01-20",
                "estimate_md": 10})

    # CR & EVM belum bisa sebelum baseline
    assert client.get(f"/api/projects/{pid}/evm", headers=pm).status_code == 409
    assert client.post(f"/api/projects/{pid}/baseline", headers=pm, json={}).status_code == 422  # belum ada budget

    b = client.post(f"/api/projects/{pid}/budgets/generate", headers=pm, json={}).json()
    client.post(f"/api/budgets/{b['id']}/submit", headers=pm)
    client.post(f"/api/budgets/{b['id']}/approve", headers=fin, json={})
    r = client.post(f"/api/projects/{pid}/baseline", headers=pm, json={"reason": "Kickoff"})
    assert r.status_code == 201, r.text
    assert D(r.json()["bac"]) == D("20000000") and len(r.json()["items"]) == 2
    # governance: baseline kedua tidak boleh langsung
    assert client.post(f"/api/projects/{pid}/baseline", headers=pm, json={}).status_code == 409

    # Actuals: 9 hari × 8 jam; progress A 80%
    for d in range(1, 10):
        t = client.post("/api/timesheets", headers=pm, json={"project_id": pid, "task_id": ta, "employee_id": emp,
                        "work_date": f"2027-01-{d:02d}", "hours": 8}).json()
        client.post(f"/api/timesheets/{t['id']}/submit", headers=pm)
        client.post(f"/api/timesheets/{t['id']}/approve", headers=pm, json={})
    client.patch(f"/api/tasks/{ta}", headers=pm, json={"status": "in_progress", "progress_pct": 80})

    e = client.get(f"/api/projects/{pid}/evm?as_of=2027-01-10", headers=pm).json()
    assert D(str(e["pv"])) == D("10000000") and D(str(e["ev"])) == D("8000000") and D(str(e["ac"])) == D("9000000")
    assert D(str(e["cpi"])) == D("0.8889") and D(str(e["spi"])) == D("0.8") and e["health"] == "red"
    assert D(str(e["tcpi"])) == D("1.0909")
    fc = client.get(f"/api/projects/{pid}/forecast?as_of=2027-01-10", headers=pm).json()["methods"]
    assert D(str(fc["cpi"]["eac"])) == D("22500000")
    assert D(str(fc["cpi_spi"]["eac"])) == D("25875000")
    assert D(str(fc["budget_rate"]["eac"])) == D("21000000")
    assert D(str(fc["bottom_up"]["eac"])) == D("21000000")
    assert client.get(f"/api/projects/{pid}/evm?method=xyz", headers=pm).status_code == 422

    # Snapshot tersimpan dengan metode
    s = client.post(f"/api/projects/{pid}/evm/snapshots", headers=pm,
                    json={"as_of": "2027-01-10", "method": "cpi_spi"}).json()
    assert s["eac_method"] == "cpi_spi" and D(s["eac"]) == D("25875000")
    curve = client.get(f"/api/projects/{pid}/evm/curve", headers=pm).json()
    pt = next(p for p in curve["points"] if p["date"] == "2027-01-10")
    assert pt["ev"] == 8000000 and pt["pv"] == 10000000

    # Risk
    r = client.post(f"/api/projects/{pid}/risks", headers=pm, json={"title": "API kamera vendor berubah",
                    "probability": 4, "impact": 4, "mitigation": "Abstraction layer"}).json()
    assert r["code"] == "R-001" and r["score"] == 16 and r["level"] == "critical"
    r = client.patch(f"/api/risks/{r['id']}", headers=pm, json={"probability": 2}).json()
    assert r["score"] == 8 and r["level"] == "medium"

    # Change Request: +5 MD, +5 hari, +8 jt revenue
    body = {"title": "Fitur export laporan", "requested_by": "Klien", "schedule_impact_days": 5,
            "revenue_delta": 8_000_000, "lines": [{"line_type": "add_task", "wbs_id": wbs, "name": "Export",
            "employee_id": emp, "md_delta": 5, "planned_start": "2027-01-21", "planned_end": "2027-01-25"}]}
    imp = client.post(f"/api/projects/{pid}/change-requests/preview", headers=pm, json=body).json()
    assert D(str(imp["cost_impact"])) == D("5000000") and D(str(imp["baseline"]["bac_after"])) == D("25000000")
    assert imp["schedule"]["end_date_after"] == "2027-02-05"
    assert D(str(imp["margin"]["planned_before"])) == D("0.3333")
    assert D(str(imp["margin"]["planned_after"])) == D("0.3421")   # (38-25)/38
    assert client.get(f"/api/projects/{pid}/change-requests", headers=pm).json() == []  # preview tidak menyimpan

    cr = client.post(f"/api/projects/{pid}/change-requests", headers=pm, json=body).json()
    assert cr["code"] == "CR-001" and D(str(cr["impact"]["md_impact"])) == 5
    client.post(f"/api/change-requests/{cr['id']}/submit", headers=pm)
    assert client.post(f"/api/change-requests/{cr['id']}/approve", headers=fin, json={}).status_code == 403
    dash = client.get("/api/dashboard", headers=mgmt).json()
    card = next(c for c in dash["projects"] if c["code"] == "EVM-T")
    assert card["pending_change_requests"] == 1
    assert any(a["rule"] == "cr_pending" for a in dash["alerts"])

    r = client.post(f"/api/change-requests/{cr['id']}/approve", headers=mgmt, json={"reason": "Disetujui klien"})
    assert r.status_code == 200, r.text
    bls = client.get(f"/api/projects/{pid}/baselines", headers=pm).json()
    assert [(x["version"], x["status"]) for x in bls] == [(1, "superseded"), (2, "active")]
    assert D(bls[1]["bac"]) == D("25000000") and bls[1]["end_date"] == "2027-02-05"
    assert client.get(f"/api/projects/{pid}", headers=pm).json()["end_date"] == "2027-02-05"
    assert any(t["name"] == "Export" for t in client.get(f"/api/projects/{pid}/tasks", headers=pm).json())

    # Dashboard PM: alert CPI/SPI dari data live (hari ini > tanggal project → PV penuh)
    dash = client.get("/api/dashboard", headers=pm).json()
    assert "portfolio" not in dash
    card = next(c for c in dash["projects"] if c["code"] == "EVM-T")
    assert card["has_baseline"] and D(str(card["revenue"])) == D("38000000")
    assert "portfolio" in client.get("/api/dashboard", headers=fin).json()

    # Halaman dashboard web
    assert client.get("/dashboard").status_code == 200
