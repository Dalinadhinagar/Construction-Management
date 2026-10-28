# SiteLedger Construction Management System

A Flask and MySQL application for managing construction material catalogs, purchase requests, supplier orders, and deliveries.

## Requirements

- Python 3.10+
- MySQL 8.0+

## Setup

1. Create the database and load the schema/demo data using MySQL Workbench, or from PowerShell:

   ```powershell
   Get-Content -Raw .\database\construction_management.sql | mysql -u root -p
   ```

2. Create and activate a virtual environment, then install dependencies:

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

3. Create a local environment file from the template:

   ```powershell
   Copy-Item .\.env.example .\.env
   notepad .\.env
   ```

   Set `MYSQL_PASSWORD` to the password for your local MySQL `root` account, and replace `SECRET_KEY` with a long random value. `.env` is ignored by Git so local credentials are not committed.

4. Start the development server:

   ```powershell
   python .\app.py
   ```

   Open `http://127.0.0.1:5000`. The app reads `.env` automatically; restart Flask after changing it.

## Demo accounts

All seeded users use the password `Construction@123`. Existing projects must apply the Stage 2 migration before using User accounts.

| Role | Email |
|---|---|
| Admin | `admin@construction.local` |
| Supplier | `supplier1@construction.local` |
| Supplier | `supplier2@construction.local` |
| Purchaser | `purchaser1@construction.local` |
| Purchaser | `purchaser2@construction.local` |

Public registration is limited to Supplier and Purchaser accounts. Administrators can create Admin, Supplier, and Purchaser accounts from the user management page. Admins can disable or re-enable accounts; the last active administrator cannot be disabled.

The Admin workspace provides searchable, filterable, sortable, paginated account management; account editing, password resets, and enable/disable controls; dedicated Supplier and Purchaser directories; and create/edit/delete controls for material listings. Accounts with existing request, order, or catalog history are disabled/archived rather than physically removed. Materials referenced by request history cannot be deleted, and their supplier ownership cannot be transferred.

Supplier and Purchaser directory entries link to their associated catalog or request/order history. Supplier reassignment is available only while an order is awaiting a response or has been rejected; accepted orders cannot be reassigned because acceptance has already reserved stock.

## Login routes

| Route | Access |
|---|---|
| `/login` | General login; redirects users to their role dashboard |
| `/login/admin` | Admin-only login |
| `/login/purchaser` | Purchaser-only login |
| `/login/supplier` | Supplier-only login |
| `/register` | Supplier or Purchaser self-registration |
| `/account/password` | Authenticated users; current password required |
| `/logout` | Authenticated users; POST with CSRF token |
| `/dashboard`, `/admin`, `/supplier`, `/purchaser` | Role-protected dashboards |

Sessions use HTTP-only, SameSite=Lax cookies with a 30-minute lifetime. Set `COOKIE_SECURE=1` when serving over HTTPS; leave it `0` for local HTTP development. Role access is checked against the current active account in MySQL on every protected request. Wrong-role requests return HTTP 403; unauthenticated requests are redirected to sign in. Login attempts are limited to five failures per client IP in a 15-minute window (in-memory, per running Flask process).

## Updating an existing database

The Stage 2 migration adds account status without dropping existing tables or accounts. Later additive migrations add supplier-specific order-line snapshots, reassignment history, the supplier chosen on each request item, and safe material archival. Back up the database before applying only the migrations that have not already been applied:

```powershell
Get-Content -Raw .\database\migrations\002_stage2_auth.sql | mysql -u root -p
Get-Content -Raw .\database\migrations\003_order_item_snapshots.sql | mysql -u root -p
Get-Content -Raw .\database\migrations\004_order_item_display_snapshots.sql | mysql -u root -p
Get-Content -Raw .\database\migrations\005_request_supplier_snapshots.sql | mysql -u root -p
Get-Content -Raw .\database\migrations\006_material_archival.sql | mysql -u root -p
Get-Content -Raw .\database\migrations\007_request_item_display_snapshots.sql | mysql -u root -p
```

Run each migration once and in order; skip any migration already applied to that database. Only use `database/construction_management.sql` to create a fresh development database; that script drops and recreates the application tables. Never run it to update an existing database.

## Authentication tests

With the configured MySQL server running and the schema migration applied:

```powershell
python -m unittest discover -s tests -v
```

To run the fast login-route regression tests without MySQL:

```powershell
python -m unittest tests.test_login_routes -v
```

Supplier material edit/delete integration tests use the configured MySQL database and clean up their temporary material records:

```powershell
python -m unittest tests.test_supplier_materials -v
```

Admin catalog/account and end-to-end procurement workflow tests also use the configured MySQL database and remove their temporary records:

```powershell
python -m unittest tests.test_admin_management tests.test_procurement_workflow -v
```

Restart Flask after changing Python files or `.env` values so the running server loads the updates.

## Workflow

1. Purchasers compare supplier offers and submit a multi-item request with a required date.
2. An administrator approves or rejects the request.
3. For an approved request, the administrator selects an active supplier offer for each unassigned request item group, verifies stock, and supplies the delivery address. Each supplier order stores its own material, quantity, unit, and agreed-price snapshot.
4. The supplier accepts or rejects each assigned order. Stock is checked and decremented transactionally when an order is accepted.
5. The supplier advances an accepted order through Processing, Dispatched, and Delivered; purchasers and administrators can track delivery status.

Purchasers compare supplier offers and select an offer for each material. The server validates the active supplier, material, and available stock, then stores the selected supplier, material name, unit, and price from MySQL rather than trusting submitted prices. Request estimates include every selected request line; supplier-order totals include only that order's saved lines. Reassignment before acceptance records the prior supplier and line prices in assignment history.

Suppliers can edit listings they own from **My materials**. Delete permanently removes an unused listing; when request history references a listing, it is archived instead, preserving the transaction and allowing administrators or its supplier to restore it. Archived materials are excluded from new requests. Stock updates remain separate from price and material-detail edits.

Supplier fulfillment updates the corresponding delivery record in the same database transaction. Stock is decremented from the order's saved material lines when an order is accepted, and the status flow only allows Processing → Dispatched → Delivered.

The existing database retains any legacy `User`-role records to preserve prior data, but the application no longer exposes or authenticates that role.

The database script contains demonstration data. It drops and recreates the application tables when rerun; back up any data before running it again.
