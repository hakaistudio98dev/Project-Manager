# PM Automation & Project Financial Control System

Implementasi bertahap dari PRD v1.0. **Status: Tahap 1, 2, 3 (V1) dan 3b (BOQ & penawaran terperinci) selesai.**

## Roadmap Tahap

| Tahap | Scope (PRD §27) | Status |
|---|---|---|
| **1 — MVP 1** | Auth/RBAC, Customer, Project, WBS, Task, Resource, Employee Cost, MD, Budget, Timesheet, Expense, Audit | ✅ Selesai |
| **2 — MVP 2** | Baseline, EVM (PV/EV/AC/SV/CV/SPI/CPI/TCPI), Forecast EAC/ETC/VAC 4 metode, snapshot & kurva S, Risk register, Change Request + what-if, Dashboard web | ✅ Selesai |
| 3 — V1 ⏳ | Quotation (pricing, margin, tax configurable), Contract, Billing milestone, Invoice, Payment, Cashflow, Profitability | |
| 4 — V1.5 ⏳ | Automation rules engine (Redis + BullMQ/worker), alert, scheduled reports, integrasi | |
| 5 — V2 | AI PM Assistant, advanced forecasting, portfolio analytics, client portal | |
| Frontend | Dashboard web ringan sudah ada di `/dashboard`; migrasi ke Next.js dijadwalkan setelah modul inti lengkap | |

## Menjalankan

```bash
pip install -r requirements.txt
python seed.py                       # opsional: data demo "VMS Development"
uvicorn app.main:app --reload
# Dashboard: http://localhost:8000        (login pm@demo.id / mgmt@demo.id)
# API docs : http://localhost:8000/docs
```

Dengan PostgreSQL: `docker compose up --build`, lalu `docker compose exec api python seed.py`.

Test: `pytest -q`

Login demo (password `demo12345`): `admin@demo.id`, `pm@demo.id`, `finance@demo.id`, `mgmt@demo.id`, `sales@demo.id`, `dev@demo.id`.
Tanpa seed: panggil `POST /api/auth/bootstrap` sekali untuk membuat organisasi + admin pertama.

## Struktur

```
app/
  main.py            FastAPI app
  models.py          Data model (ERD PRD §23)
  schemas.py         Validasi request/response
  security.py        JWT, password hashing, RBAC, akses per project
  audit.py           Audit trail
  services/
    costing.py       Man-Day engine (PRD §9, §13) — fungsi murni, Decimal
    allocation.py    Kapasitas & alokasi resource lintas project
  routers/
    auth.py          bootstrap, login, users
    master.py        customer, vendor, employee, cost rate, capacity
    projects.py      project, member, WBS (template 10 fase), task, dependency, resource summary
    budgets.py       generate budget dari task, versioning, submit/approve/reject
    actuals.py       timesheet, expense (+reversal), period lock, cost summary, audit log
tests/               unit test formula + end-to-end flow
```

## Rumus yang diimplementasikan

- Loaded Monthly Cost = Salary + Benefits + Equipment + Software + Other
- Productive MD = Working Days × Productive Capacity
- Cost Rate/MD = Loaded Monthly Cost / Productive MD  (Rp15 jt / 18 = Rp833.333,33)
- Labor Cost = MD × Cost Rate
- Direct Cost = Labor + Planned Expense; Cost Baseline = Direct + Contingency; Budget = Baseline + Management Reserve
- Actual MD = Hours / Standard Hours per Day (default 8, per organisasi)
- Actual Cost (AC) = Σ labor timesheet approved + Σ expense posted

## Aturan governance yang sudah berlaku

- Cost rate berversi `effective_date`; rate lama tidak pernah diubah. Timesheet memakai rate yang berlaku di tanggal kerja dan menyimpan snapshot-nya.
- Budget berversi; approve versi baru → versi lama `superseded`. Pembuat budget tidak boleh meng-approve (segregation of duties).
- Timesheet: draft → submitted → approved/rejected; tidak boleh approve milik sendiri; maks 24 jam/hari.
- Expense: draft → submitted → posted. Koreksi hanya via **reversal** (amount negatif), transaksi asli tidak diubah/dihapus.
- Period lock (YYYY-MM): timesheet/expense di periode tertutup ditolak (HTTP 423). Unlock hanya admin + alasan.
- Gaji/detail rate hanya untuk admin/management/finance; PM hanya melihat cost/MD; staff tidak melihat nilai kontrak maupun biaya.
- Master data soft delete; semua perubahan kritis tercatat di `audit_logs` (old/new value, actor, alasan).

## Catatan untuk production

- Ganti `JWT_SECRET`; gunakan Alembic migration (saat ini `create_all` untuk dev).
- Tambahkan refresh token, MFA role sensitif, dan rate limiting (PRD §25) — dijadwalkan di tahap berikutnya.

## Tahap 2 — EVM, Forecast, Risk & Change Request

**Alur:** Budget approved → `POST /api/projects/{id}/baseline` (bekukan baseline v1) → EVM live & snapshot → perubahan scope hanya lewat Change Request → approve CR membuat baseline v2 (v1 superseded).

| Endpoint | Fungsi |
|---|---|
| `POST /api/projects/{id}/baseline` | Bekukan baseline dari budget approved (sekali; berikutnya via CR) |
| `GET /api/projects/{id}/baseline(s)` | Baseline aktif + riwayat versi |
| `GET /api/projects/{id}/evm?as_of=&method=` | EVM live + breakdown per WBS (roll-up) |
| `GET /api/projects/{id}/forecast` | EAC/ETC/VAC semua metode berdampingan |
| `POST /api/projects/{id}/evm/snapshots` | Simpan snapshot (metode & input tercatat) |
| `GET /api/projects/{id}/evm/curve` | Data kurva S PV/EV/AC + tren CPI/SPI |
| `POST /api/evm/snapshots/run-all` | Snapshot semua project aktif (untuk cron) |
| `/api/projects/{id}/risks`, `PATCH /api/risks/{id}` | Risk register (skor P×I, level otomatis) |
| `POST /api/projects/{id}/change-requests/preview` | What-if: dampak MD, biaya, jadwal, BAC, EAC, margin — tanpa menyimpan |
| `POST /api/projects/{id}/change-requests` → `/submit` → `/approve` | CR resmi; approve = baseline versi baru |
| `GET /api/dashboard` | Kartu project, alert aturan PRD §20, portfolio (management/finance) |

**Rumus EVM:** PV = Planned% × BAC, EV = Earned% × BAC, AC = labor approved + expense posted.
Planned% linear per hari antara planned start–end tiap item baseline. Earned% memakai progress method
yang dibekukan di baseline (0/100, 50/50, milestone, physical %, weighted deliverable).

**Metode forecast:** `cpi` (BAC/CPI), `cpi_spi` (AC + (BAC−EV)/(CPI×SPI)), `budget_rate` (AC + BAC − EV),
`bottom_up` (AC + sisa MD × rate + sisa expense). Snapshot menyimpan metode, formula, dan input.

**Catatan desain:** default sesuai PRD, BAC = cost baseline *termasuk contingency*. Mulai Tahap 3 bisa diubah
lewat `PATCH /api/finance/settings {"evm_bac_includes_contingency": false}` agar contingency dipegang terpisah
(praktik PMI) sehingga overrun terlihat lebih dini di CPI.

## Tahap 3 — Pricing, Quotation, Contract, Billing, Cashflow, Profitability

**Alur:** Budget approved → Quotation (harga = total cost / (1 − target margin)) → Finance approve → Management
approve (jika di atas threshold atau margin < target) → kirim → diterima → Contract (termin tagihan otomatis)
→ Invoice per termin → Payment → AR → Cashflow → Profitability.

| Endpoint | Fungsi |
|---|---|
| `GET/PATCH /api/finance/settings` | PKP, kode PPN/PPh default, overhead %, threshold approval, pembulatan harga, termin, opsi EVM contingency, saldo awal kas |
| `POST/GET /api/taxes` | Master tarif pajak ber-effective date; DPP sebagai pecahan (mis. 11/12) |
| `POST /api/pricing/calculate` | Kalkulator harga stateless (harga, PPN, PPh, kas diterima, margin) |
| `POST /api/quotations` → `/submit` → `/approve` (×1–2) → `/send` → `/accept` | Quotation berversi, approval berjenjang, segregation of duties |
| `POST /api/contracts` | Kontrak dari quotation accepted; update nilai kontrak project + buat termin |
| `POST /api/contracts/{id}/milestones` | Termin tambahan khusus change order (CR approved ber-revenue) |
| `POST /api/invoices/from-milestone` → `/issue` | Invoice draft → terbit (nomor `INV/YYYY/MM/NNNN`, pajak dihitung per tanggal terbit) |
| `/cancel`, `/dispute`, `/resolve` | Status invoice: issued, due, overdue, partially paid, paid, cancelled, disputed |
| `POST /api/payments`, `/payments/{id}/reverse` | Pelunasan = kas + PPh dipotong; reversal, bukan hapus |
| `GET /api/ar/aging` | Piutang per bucket (current, 1–30, 31–60, 61–90, >90) |
| `GET /api/projects/{id}/cashflow` | Kas masuk/keluar aktual per bulan + forecast (termin, piutang, ETC) + kumulatif |
| `GET /api/projects/{id}/profitability` | Rencana · baseline · aktual (POC) · forecast |
| `GET /api/reports/profitability` | Laporan portfolio |

Header `Idempotency-Key` didukung pada pembuatan invoice dan pembayaran, sehingga retry jaringan tidak
membuat transaksi ganda. Transaksi pada periode yang sudah dikunci ditolak (HTTP 423).

**Rumus pajak:** PPN = Subtotal × (dpp_num/dpp_den) × tarif; PPh = Subtotal × tarif. PPN yang diterima tidak
dihitung sebagai revenue/profit (ditampilkan terpisah di cashflow). Tarif mengikuti tanggal transaksi.

**Profitabilitas:** Revenue = nilai kontrak + revenue CR approved. Total cost = biaya project + overhead
(overhead_pct × direct cost). Lensa aktual memakai revenue diakui = % penyelesaian (EV/BAC) × revenue.

> Konfigurasi pajak di seed (PPN 12% DPP 11/12, PPh 23 2%) hanya contoh — sesuaikan dengan ketentuan
> perpajakan yang berlaku dan konsultasikan dengan konsultan pajak Anda.

## Tahap 3b — Katalog, BOQ per WBS, estimasi dinamis & penawaran terperinci

Biaya per WBS = **jasa** (MD task × cost/MD) + **pengadaan** (qty × harga beli item BOQ), di-roll-up otomatis ke
parent WBS. Setiap perubahan MD, qty, atau harga langsung mengubah total WBS, total cost, dan harga penawaran.

| Endpoint | Fungsi |
|---|---|
| `POST/GET/PATCH /api/catalog` | Master item: harga beli acuan, satuan, vendor, lead time |
| `POST/GET /api/projects/{id}/procurements` | BOQ per WBS (dari katalog → harga/satuan/vendor terisi otomatis, atau manual) |
| `PATCH/DELETE /api/procurements/{id}` | Ubah qty/harga (item planned); hapus — atau batal bila sudah dipakai budget |
| `POST /api/projects/{id}/procurements/refresh-prices` | Samakan harga item planned dengan katalog terbaru |
| `GET /api/projects/{id}/estimate?target_margin=&procurement_margin=&contingency_pct=&discount=` | Estimasi live: tree WBS + rincian + simulasi harga |
| `POST /api/procurements/{id}/order` → `/receive` | PO → barang diterima → expense otomatis (menunggu approval Finance) → actual cost |

**Harga penawaran terperinci:** biaya tiap baris dibebani contingency + overhead secara proporsional, lalu dibagi
(1 − margin). Margin jasa dan margin pengadaan bisa berbeda (`procurement_margin`, mis. 15% untuk perangkat).
Total dibulatkan ke atas; harga satuan barang dibulatkan ke rupiah; selisih pembulatan diserap baris jasa
terbesar, sehingga **jumlah rincian = harga penawaran** tepat. Estimasi dan quotation memakai engine yang sama.

Budget (`/budgets/generate`) otomatis menarik semua item BOQ; quotation menyimpan rincian per WBS
(`lines`) yang bisa dicetak dari dashboard (tab Keuangan → Cetak). Dashboard memberi alert bila item belum
dipesan padahal batas pesan (tanggal dibutuhkan − lead time) sudah dekat atau terlewat.

Approval management wajib bila nilai di atas threshold, atau diskon/harga manual menurunkan margin di bawah
margin kebijakan (blended jasa + pengadaan pada harga standar).
#   P r o j e c t - M a n a g e r  
 