"""End-to-end MVP 1: setup → project → WBS → resource → budget → timesheet → expense → cost."""
from decimal import Decimal as D

from tests.conftest import login

PW = "rahasia123"


def test_end_to_end(client):
    # --- Bootstrap & users ---
    r = client.post("/api/auth/bootstrap", json={"org_name": "YTEX Corp", "admin_email": "admin@ytex.id",
                                                  "admin_name": "Admin", "admin_password": PW})
    assert r.status_code == 201, r.text
    assert client.post("/api/auth/bootstrap", json={"org_name": "X", "admin_email": "a@b.id",
                                                     "admin_name": "A", "admin_password": PW}).status_code == 409
    admin = login(client, "admin@ytex.id", PW)

    users = {}
    for email, role in [("pm@ytex.id", "pm"), ("fin@ytex.id", "finance"),
                        ("dev@ytex.id", "staff"), ("mgmt@ytex.id", "management")]:
        r = client.post("/api/users", headers=admin,
                        json={"email": email, "full_name": email.split("@")[0], "password": PW, "role": role})
        assert r.status_code == 201, r.text
        users[role] = r.json()["id"]
    pm, fin = login(client, "pm@ytex.id", PW), login(client, "fin@ytex.id", PW)
    dev, mgmt = login(client, "dev@ytex.id", PW), login(client, "mgmt@ytex.id", PW)

    # --- Employees & cost rates (finance) ---
    emp = {}
    for name, role, loaded, uid in [("Budi", "Backend", 15_000_000, users["staff"]),
                                    ("Sari", "UI/UX", 10_000_000, None)]:
        r = client.post("/api/employees", headers=fin, json={"name": name, "job_role": role, "user_id": uid})
        assert r.status_code == 201, r.text
        emp[name] = r.json()["id"]
        r = client.post(f"/api/employees/{emp[name]}/rates", headers=fin,
                        json={"effective_date": "2026-01-01", "salary": loaded,
                              "working_days": 20, "productive_capacity": "0.9"})
        assert r.status_code == 201, r.text
    assert D(r.json()["cost_per_md"]) == D("555555.56")
    # PM & staff tidak boleh melihat detail gaji
    assert client.get(f"/api/employees/{emp['Budi']}/rates", headers=pm).status_code == 403
    assert client.get(f"/api/employees/{emp['Budi']}/rates", headers=dev).status_code == 403
    cap = client.get(f"/api/employees/{emp['Budi']}/capacity?on=2026-10-01", headers=pm).json()
    assert D(cap["cost_per_md"]) == D("833333.33") and "salary" not in cap

    # --- Customer & project (PM) ---
    cust = client.post("/api/customers", headers=pm, json={"name": "PT Contoh"}).json()["id"]
    r = client.post("/api/projects", headers=pm, json={
        "code": "VMS-01", "name": "VMS Development", "customer_id": cust,
        "contract_value": 500_000_000, "start_date": "2026-10-01", "end_date": "2027-03-31",
        "status": "active", "target_margin": "0.30", "tax_rate": "0.11"})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    assert r.json()["pm_id"] == users["pm"]

    # --- WBS template + sub WBS ---
    wbs = client.post(f"/api/projects/{pid}/wbs/template", headers=pm).json()
    assert len(wbs) == 10
    backend = next(w for w in wbs if w["name"] == "Backend")
    uiux = next(w for w in wbs if w["name"] == "UI/UX")
    sub = client.post(f"/api/projects/{pid}/wbs", headers=pm,
                      json={"name": "API Service", "parent_id": backend["id"]}).json()
    assert sub["code"] == "4.1"

    # --- Members (resource allocation) ---
    r = client.post(f"/api/projects/{pid}/members", headers=pm,
                    json={"employee_id": emp["Budi"], "allocation_pct": 100})
    assert r.status_code == 201 and r.json()["over_allocated"] is False
    client.post(f"/api/projects/{pid}/members", headers=pm, json={"employee_id": emp["Sari"], "allocation_pct": 50})

    # --- Tasks ---
    t_be = client.post(f"/api/projects/{pid}/tasks", headers=pm, json={
        "wbs_id": sub["id"], "name": "Build REST API", "owner_employee_id": emp["Budi"],
        "planned_start": "2026-10-05", "planned_end": "2026-11-05", "estimate_md": 20}).json()
    t_ui = client.post(f"/api/projects/{pid}/tasks", headers=pm, json={
        "wbs_id": uiux["id"], "name": "Design screens", "owner_employee_id": emp["Sari"],
        "planned_start": "2026-10-01", "estimate_md": 9}).json()

    # --- Budget: generate → submit → approve (PRD §10) ---
    r = client.post(f"/api/projects/{pid}/budgets/generate", headers=pm, json={
        "planned_expenses": [{"category": "cloud", "amount": 6_000_000, "wbs_id": wbs[5]["id"]}],
        "contingency_pct": "0.10", "management_reserve_amount": 5_000_000})
    assert r.status_code == 201, r.text
    b = r.json()
    labor = D("20") * D("833333.33") + D("9") * D("555555.56")          # 16.666.666,60 + 5.000.000,04
    assert D(b["labor_cost"]) == labor
    assert D(b["direct_cost"]) == labor + 6_000_000
    assert D(b["contingency"]) == ((labor + 6_000_000) * D("0.10")).quantize(D("0.01"))
    assert D(b["total_budget"]) == D(b["cost_baseline"]) + 5_000_000
    assert D(b["total_md"]) == 29

    assert client.post(f"/api/budgets/{b['id']}/approve", headers=fin, json={}).status_code == 409  # belum submit
    assert client.post(f"/api/budgets/{b['id']}/submit", headers=pm).status_code == 200
    assert client.post(f"/api/budgets/{b['id']}/approve", headers=dev, json={}).status_code == 403
    r = client.post(f"/api/budgets/{b['id']}/approve", headers=fin, json={"reason": "OK"})
    assert r.json()["status"] == "approved"

    # versi 2 → approve → versi 1 superseded
    b2 = client.post(f"/api/projects/{pid}/budgets/generate", headers=pm,
                     json={"contingency_amount": 1_000_000}).json()
    assert b2["version"] == 2
    client.post(f"/api/budgets/{b2['id']}/submit", headers=pm)
    client.post(f"/api/budgets/{b2['id']}/approve", headers=mgmt, json={})
    statuses = {x["version"]: x["status"] for x in client.get(f"/api/projects/{pid}/budgets", headers=pm).json()}
    assert statuses == {1: "superseded", 2: "approved"}

    # --- Timesheet (PRD §13) ---
    r = client.post("/api/timesheets", headers=dev, json={
        "project_id": pid, "task_id": t_be["id"], "work_date": "2026-10-06", "hours": 6,
        "description": "Endpoint auth"})
    assert r.status_code == 201, r.text
    ts = r.json()
    assert D(ts["actual_md"]) == D("0.75") and ts["labor_cost"] is None  # staff tidak lihat cost
    assert client.post("/api/timesheets", headers=dev, json={
        "project_id": pid, "task_id": t_be["id"], "work_date": "2026-10-06", "hours": 19}).status_code == 422
    client.post(f"/api/timesheets/{ts['id']}/submit", headers=dev)
    assert client.post(f"/api/timesheets/{ts['id']}/approve", headers=dev, json={}).status_code == 403
    r = client.post(f"/api/timesheets/{ts['id']}/approve", headers=pm, json={})
    assert r.status_code == 200 and D(r.json()["labor_cost"]) == D("625000.00")

    # PM input timesheet untuk Sari (tanpa user login)
    r = client.post("/api/timesheets", headers=pm, json={
        "project_id": pid, "task_id": t_ui["id"], "employee_id": emp["Sari"],
        "work_date": "2026-10-02", "hours": 8})
    client.post(f"/api/timesheets/{r.json()['id']}/submit", headers=pm)
    client.post(f"/api/timesheets/{r.json()['id']}/approve", headers=admin, json={})

    # --- Expense (PRD §14) ---
    e = client.post("/api/expenses", headers=pm, json={
        "project_id": pid, "wbs_id": wbs[5]["id"], "category": "cloud", "amount": 2_000_000,
        "expense_date": "2026-10-10", "description": "VPS"}).json()
    client.post(f"/api/expenses/{e['id']}/submit", headers=pm)
    r = client.post(f"/api/expenses/{e['id']}/approve", headers=fin, json={})
    assert r.json()["status"] == "posted"
    e2 = client.post("/api/expenses", headers=pm, json={
        "project_id": pid, "category": "transport", "amount": 300_000, "expense_date": "2026-10-11"}).json()
    client.post(f"/api/expenses/{e2['id']}/submit", headers=pm)
    client.post(f"/api/expenses/{e2['id']}/approve", headers=fin, json={})
    r = client.post(f"/api/expenses/{e2['id']}/reverse", headers=fin, json={"reason": "salah input"})
    assert r.status_code == 201 and D(r.json()["amount"]) == D("-300000.00")
    assert client.post(f"/api/expenses/{e2['id']}/reverse", headers=fin,
                       json={"reason": "x"}).status_code == 409

    # --- Progress & cost summary ---
    client.patch(f"/api/tasks/{t_ui['id']}", headers=pm, json={"status": "done"})
    assert client.patch(f"/api/tasks/{t_be['id']}", headers=dev, json={"estimate_md": 1}).status_code == 403
    assert client.patch(f"/api/tasks/{t_be['id']}", headers=dev, json={"progress_pct": 10}).status_code == 200

    s = client.get(f"/api/projects/{pid}/cost-summary", headers=pm).json()
    assert s["approved_budget_version"] == 2
    assert D(s["actual_labor"]) == D("625000.00") + D("555555.56")
    assert D(s["actual_expense"]) == D("2000000.00")   # 2jt + 300rb − 300rb
    assert D(s["actual_cost"]) == D(s["actual_labor"]) + D(s["actual_expense"])
    tree = client.get(f"/api/projects/{pid}/wbs", headers=pm).json()
    be_node = next(n for n in tree if n["name"] == "Backend")
    assert D(be_node["estimate_md"]) == 20 and D(be_node["progress_pct"]) == 10

    res = client.get(f"/api/projects/{pid}/resources", headers=pm).json()
    budi = next(m for m in res["members"] if m["name"] == "Budi")
    assert D(budi["planned_md"]) == 20 and D(budi["actual_md"]) == D("0.75")

    # --- Period lock ---
    assert client.post("/api/periods/lock", headers=fin, json={"period": "2026-10"}).status_code == 201
    r = client.post("/api/timesheets", headers=dev, json={
        "project_id": pid, "task_id": t_be["id"], "work_date": "2026-10-20", "hours": 4})
    assert r.status_code == 423

    # --- RBAC & audit ---
    assert client.get(f"/api/projects/{pid}", headers=dev).json()["contract_value"] is None
    assert client.get(f"/api/projects/{pid}/cost-summary", headers=dev).status_code == 403
    logs = client.get("/api/audit-logs?entity=budget", headers=fin).json()
    assert {"approve", "supersede", "create", "submit"} <= {l["action"] for l in logs}
