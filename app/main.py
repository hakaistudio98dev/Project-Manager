from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse

from .config import settings
from .database import Base, engine
from .routers import actuals, auth, billing, budgets, changes, dashboard, evm, master, procurement, projects, sales

app = FastAPI(
    title="PM Automation & Project Financial Control System",
    version="0.3.0-v1",
    description="Tahap 1 (MVP 1): Auth/RBAC, Project, WBS, Resource, Costing, Budget, Timesheet, Expense.\n\n"
                "Tahap 2 (MVP 2): Baseline, EVM, Forecast, Risk, Change Request, Dashboard.\n\n"
                "Tahap 3 (V1): Pricing & Tax, Quotation, Contract, Billing, Invoice, Payment, AR, Cashflow, Profitability.",
)
app.add_middleware(CORSMiddleware, allow_origins=settings.CORS_ORIGINS,
                   allow_methods=["*"], allow_headers=["*"])

# Dev: auto-create tabel. Production: gunakan Alembic migration.
Base.metadata.create_all(bind=engine)

for r in (auth.router, master.router, projects.router, budgets.router, actuals.router,
          evm.router, changes.router, dashboard.router, sales.router, billing.router, procurement.router):
    app.include_router(r)

STATIC = Path(__file__).parent / "static"


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/dashboard")


@app.get("/dashboard", include_in_schema=False)
def dashboard_page():
    return FileResponse(STATIC / "dashboard.html")


@app.get("/health", tags=["System"])
def health():
    return {"status": "ok", "phase": "V1"}
