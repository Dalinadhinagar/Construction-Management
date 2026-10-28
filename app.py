"""Flask application for a role-based construction procurement workflow."""

import hmac
import logging
import os
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps
from pathlib import Path

import mysql.connector
from dotenv import load_dotenv
from flask import (
    Flask,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

from database import db_cursor


load_dotenv(Path(__file__).resolve().with_name(".env"))

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "0") == "1",
)
app.permanent_session_lifetime = timedelta(minutes=30)
app.logger.setLevel(logging.INFO)

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
VALID_ROLES = {"Admin", "Supplier", "Purchaser"}
MAX_ORDER_TOTAL = Decimal("999999999999.99")
LOGIN_ATTEMPT_LIMIT = 5
LOGIN_ATTEMPT_WINDOW_SECONDS = 15 * 60
_login_attempts = defaultdict(deque)
_login_attempts_lock = threading.Lock()


def login_is_rate_limited(remote_addr):
    now = time.monotonic()
    with _login_attempts_lock:
        attempts = _login_attempts[remote_addr]
        while attempts and now - attempts[0] >= LOGIN_ATTEMPT_WINDOW_SECONDS:
            attempts.popleft()
        return len(attempts) >= LOGIN_ATTEMPT_LIMIT


def record_login_failure(remote_addr):
    now = time.monotonic()
    with _login_attempts_lock:
        attempts = _login_attempts[remote_addr]
        while attempts and now - attempts[0] >= LOGIN_ATTEMPT_WINDOW_SECONDS:
            attempts.popleft()
        attempts.append(now)


def clear_login_failures(remote_addr):
    with _login_attempts_lock:
        _login_attempts.pop(remote_addr, None)


def csrf_token():
    """Return a per-session token used to protect all state-changing forms."""
    token = session.get("_csrf_token")
    if token is None:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def protect_post_requests():
    """Reject form submissions without a matching CSRF token."""
    if request.method == "POST":
        supplied = request.form.get("_csrf_token", "")
        expected = session.get("_csrf_token", "")
        if not supplied or not expected or not hmac.compare_digest(supplied, expected):
            abort(400, description="The form expired or could not be verified. Please retry.")


def role_required(*allowed_roles):
    """Guard a route by login state and the user's assigned role."""

    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if "user_id" not in session:
                flash("Please log in to continue.", "warning")
                return redirect(url_for("login"))
            with db_cursor() as cursor:
                cursor.execute(
                    "SELECT user_id, name, role, is_active FROM users WHERE user_id = %s",
                    (session["user_id"],),
                )
                user = cursor.fetchone()
            if not user or not user["is_active"]:
                session.clear()
                flash("Your account is unavailable. Please contact an administrator.", "danger")
                return redirect(url_for("login"))
            if user["role"] not in VALID_ROLES:
                session.clear()
                flash(
                    "This account role is no longer available. Contact an administrator.",
                    "danger",
                )
                return redirect(url_for("login"))
            session["name"] = user["name"]
            session["role"] = user["role"]
            if allowed_roles and user["role"] not in allowed_roles:
                abort(403)
            g.user_id = user["user_id"]
            return view(*args, **kwargs)

        return wrapped

    return decorator


def dashboard_endpoint(role=None):
    """Choose the dashboard corresponding to a role, or send anonymous users home."""
    selected_role = role or session.get("role")
    endpoints = {
        "Admin": "admin_dashboard",
        "Supplier": "supplier_dashboard",
        "Purchaser": "purchaser_dashboard",
    }
    if selected_role not in endpoints:
        return url_for("index")
    return url_for(endpoints[selected_role])


def valid_email(value):
    return bool(EMAIL_PATTERN.fullmatch(value or ""))


def positive_int(value, field_name):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be a whole number.")
    if parsed <= 0:
        raise ValueError(f"{field_name} must be greater than zero.")
    if parsed > 2147483647:
        raise ValueError(f"{field_name} is outside the supported range.")
    return parsed


def positive_price(value, field_name="Price"):
    try:
        parsed = Decimal(value)
    except (InvalidOperation, TypeError):
        raise ValueError(f"{field_name} must be a valid amount.")
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"{field_name} must be greater than zero.")
    try:
        parsed = parsed.quantize(Decimal("0.01"))
    except InvalidOperation:
        raise ValueError(f"{field_name} is outside the supported range.")
    if parsed <= 0 or parsed > Decimal("9999999999.99"):
        raise ValueError(f"{field_name} must be between 0.01 and 9,999,999,999.99.")
    return parsed


def nonnegative_int(value, field_name, maximum=2147483647):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be a whole number.")
    if parsed < 0:
        raise ValueError(f"{field_name} cannot be negative.")
    if parsed > maximum:
        raise ValueError(f"{field_name} cannot exceed {maximum:,}.")
    return parsed


def supplier_offers_for_lines(cursor, lines, source_supplier_id=None):
    """Return active supplier offers that can fulfill each line in a request group."""
    cursor.execute(
        "SELECT m.material_id, m.supplier_id, u.name AS supplier_name, "
        "m.material_name, m.unit, m.price, m.stock_quantity "
        "FROM materials m JOIN users u ON u.user_id = m.supplier_id "
        "WHERE u.role = 'Supplier' AND u.is_active = TRUE AND m.is_active = TRUE "
        "ORDER BY u.name, m.price, m.material_id"
    )
    offers_by_product = defaultdict(list)
    for offer in cursor.fetchall():
        product_key = (
            offer["material_name"].strip().casefold(),
            offer["unit"].strip().casefold(),
        )
        offers_by_product[product_key].append(offer)

    supplier_candidates = {}
    for line in lines:
        product_key = (
            line["material_name"].strip().casefold(),
            line["unit"].strip().casefold(),
        )
        by_supplier = defaultdict(list)
        for offer in offers_by_product.get(product_key, []):
            if offer["stock_quantity"] >= line["quantity"]:
                by_supplier[offer["supplier_id"]].append(offer)
        for supplier_id in tuple(supplier_candidates) + tuple(by_supplier):
            if supplier_id not in by_supplier:
                supplier_candidates.pop(supplier_id, None)
        for supplier_id, supplier_offers in by_supplier.items():
            candidate = supplier_candidates.setdefault(
                supplier_id,
                {
                    "supplier_id": supplier_id,
                    "supplier_name": supplier_offers[0]["supplier_name"],
                    "total": Decimal("0.00"),
                    "offers": {},
                },
            )
            matching_offer = min(
                supplier_offers,
                key=lambda offer: (offer["price"], offer["material_id"]),
            )
            preferred_supplier_id = line.get("source_supplier_id", source_supplier_id)
            if supplier_id == preferred_supplier_id:
                original_offer = next(
                    (
                        offer
                        for offer in supplier_offers
                        if offer["material_id"] == line["material_id"]
                    ),
                    None,
                )
                if original_offer:
                    matching_offer = original_offer
                    line_price = line["unit_price"]
                else:
                    line_price = matching_offer["price"]
            else:
                line_price = matching_offer["price"]
            candidate["offers"][line["request_item_id"]] = {
                "material_id": matching_offer["material_id"],
                "material_name": matching_offer["material_name"],
                "unit": matching_offer["unit"],
                "unit_price": Decimal(str(line_price)),
            }
            candidate["total"] += Decimal(str(line_price)) * line["quantity"]

    expected_count = len(lines)
    return sorted(
        (
            candidate
            for candidate in supplier_candidates.values()
            if len(candidate["offers"]) == expected_count
        ),
        key=lambda candidate: candidate["supplier_name"].casefold(),
    )


def form_text(field, label, required=True, maximum=255):
    value = request.form.get(field, "").strip()
    if required and not value:
        raise ValueError(f"{label} is required.")
    if len(value) > maximum:
        raise ValueError(f"{label} must be no more than {maximum} characters.")
    return value


def notify_validation_error(error):
    flash(str(error), "danger")


def get_dashboard_url():
    return dashboard_endpoint()


@app.errorhandler(mysql.connector.Error)
def handle_database_error(error):
    """Log database failures and return a clear service-unavailable response."""
    app.logger.exception("Database operation failed: %s", error)
    return (
        render_template(
            "error.html",
            code=503,
            message="The database is temporarily unavailable. Check the MySQL configuration and try again.",
        ),
        503,
    )


@app.route("/")
def index():
    if "user_id" in session:
        if session.get("role") not in VALID_ROLES:
            session.clear()
            flash(
                "This account role is no longer available. Contact an administrator.",
                "danger",
            )
            return redirect(url_for("login"))
        return redirect(get_dashboard_url())
    return render_template("index.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    return handle_login()


@app.route("/login/admin", methods=["GET", "POST"])
def admin_login():
    return handle_login(expected_role="Admin")


@app.route("/login/purchaser", methods=["GET", "POST"])
def purchaser_login():
    return handle_login(expected_role="Purchaser")


@app.route("/login/supplier", methods=["GET", "POST"])
def supplier_login():
    return handle_login(expected_role="Supplier")


def handle_login(expected_role=None):
    if request.method == "POST":
        remote_addr = request.remote_addr or "unknown"
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if login_is_rate_limited(remote_addr):
            flash("Too many sign-in attempts. Please wait 15 minutes and try again.", "danger")
        elif not email or not password:
            record_login_failure(remote_addr)
            flash("Email and password are required.", "danger")
        elif not valid_email(email):
            record_login_failure(remote_addr)
            flash("Enter a valid email address.", "danger")
        else:
            with db_cursor() as cursor:
                cursor.execute(
                    "SELECT user_id, name, email, password_hash, role, is_active "
                    "FROM users WHERE email = %s",
                    (email,),
                )
                user = cursor.fetchone()
            authenticated = bool(user and user["is_active"]) and check_password_hash(
                user["password_hash"], password
            )
            if authenticated and expected_role and user["role"] != expected_role:
                record_login_failure(remote_addr)
                flash(f"This sign-in page is for {expected_role} accounts only.", "danger")
            elif authenticated and user["role"] not in VALID_ROLES:
                record_login_failure(remote_addr)
                flash(
                    "This account role is no longer available. Contact an administrator.",
                    "danger",
                )
            elif authenticated:
                clear_login_failures(remote_addr)
                session.clear()
                session["user_id"] = user["user_id"]
                session["name"] = user["name"]
                session["role"] = user["role"]
                session.permanent = True
                flash(f"Welcome back, {user['name']}!", "success")
                return redirect(get_dashboard_url())
            else:
                record_login_failure(remote_addr)
                flash("The email or password is incorrect.", "danger")
    return render_template("login.html", expected_role=expected_role)


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        try:
            name = form_text("name", "Name", maximum=120)
            email = form_text("email", "Email", maximum=254).lower()
            password = request.form.get("password", "")
            confirm = request.form.get("confirm_password", "")
            phone = form_text("phone", "Phone", required=False, maximum=30)
            role = request.form.get("role", "")
            if not valid_email(email):
                raise ValueError("Enter a valid email address.")
            if len(password) < 8:
                raise ValueError("Password must contain at least 8 characters.")
            if password != confirm:
                raise ValueError("The password confirmation does not match.")
            if role not in {"Supplier", "Purchaser"}:
                raise ValueError("Choose Supplier or Purchaser as your account type.")
            with db_cursor() as cursor:
                cursor.execute("SELECT user_id FROM users WHERE email = %s", (email,))
                if cursor.fetchone():
                    raise ValueError("An account with that email already exists.")
                cursor.execute(
                    "INSERT INTO users (name, email, password_hash, role, phone) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (name, email, generate_password_hash(password), role, phone or None),
                )
            flash("Your account is ready. Please log in.", "success")
            return redirect(url_for("login"))
        except ValueError as error:
            notify_validation_error(error)
    return render_template("register.html")


@app.route("/logout", methods=["POST"])
@role_required("Admin", "Supplier", "Purchaser")
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("index"))


@app.route("/dashboard")
@role_required("Admin", "Supplier", "Purchaser")
def dashboard():
    return redirect(get_dashboard_url())


@app.route("/admin")
@role_required("Admin")
def admin_dashboard():
    with db_cursor() as cursor:
        cursor.execute("SELECT COUNT(*) AS total FROM users")
        user_count = cursor.fetchone()["total"]
        cursor.execute("SELECT COUNT(*) AS total FROM materials")
        material_count = cursor.fetchone()["total"]
        cursor.execute(
            "SELECT COUNT(*) AS total FROM purchase_requests WHERE status = 'Pending'"
        )
        pending_count = cursor.fetchone()["total"]
        cursor.execute(
            "SELECT COUNT(*) AS total, COALESCE(SUM(total_amount), 0) AS value "
            "FROM orders WHERE order_status <> 'Cancelled'"
        )
        order_summary = cursor.fetchone()
        cursor.execute(
            "SELECT o.order_id, o.request_id, o.supplier_id, o.order_status, o.total_amount, o.order_date, "
            "p.name AS purchaser_name, s.name AS supplier_name "
            "FROM orders o JOIN purchase_requests r ON r.request_id = o.request_id "
            "JOIN users p ON p.user_id = r.purchaser_id "
            "JOIN users s ON s.user_id = o.supplier_id "
            "ORDER BY o.order_date DESC LIMIT 8"
        )
        recent_orders = cursor.fetchall()
    return render_template(
        "admin_dashboard.html",
        user_count=user_count,
        material_count=material_count,
        pending_count=pending_count,
        order_summary=order_summary,
        recent_orders=recent_orders,
    )


@app.route("/admin/users", methods=["GET", "POST"])
@role_required("Admin")
def admin_users():
    if request.method == "POST":
        try:
            name = form_text("name", "Name", maximum=120)
            email = form_text("email", "Email", maximum=254).lower()
            password = request.form.get("password", "")
            phone = form_text("phone", "Phone", required=False, maximum=30)
            role = request.form.get("role", "")
            if not valid_email(email):
                raise ValueError("Enter a valid email address.")
            if len(password) < 8:
                raise ValueError("Password must contain at least 8 characters.")
            if role not in VALID_ROLES:
                raise ValueError("Choose a valid user role.")
            with db_cursor() as cursor:
                cursor.execute("SELECT user_id FROM users WHERE email = %s", (email,))
                if cursor.fetchone():
                    raise ValueError("An account with that email already exists.")
                cursor.execute(
                    "INSERT INTO users (name, email, password_hash, role, phone) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (name, email, generate_password_hash(password), role, phone or None),
                )
            flash("User account created.", "success")
            return redirect(url_for("admin_users"))
        except ValueError as error:
            notify_validation_error(error)
    search = request.args.get("q", "").strip()
    role_filter = request.args.get("role", "").strip()
    if role_filter not in VALID_ROLES:
        role_filter = ""
    sort_columns = {
        "name": "name",
        "email": "email",
        "role": "role",
        "created": "created_at",
    }
    sort_by = request.args.get("sort", "created")
    if sort_by not in sort_columns:
        sort_by = "created"
    sort_direction = request.args.get("direction", "desc").lower()
    if sort_direction not in {"asc", "desc"}:
        sort_direction = "desc"
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1
    page_size = 25
    with db_cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) AS total FROM users "
            "WHERE (%s = '' OR role = %s) "
            "AND (%s = '' OR name LIKE %s OR email LIKE %s)",
            (role_filter, role_filter, search, f"%{search}%", f"%{search}%"),
        )
        total_users = cursor.fetchone()["total"]
        page_count = max(1, (total_users + page_size - 1) // page_size)
        page = min(page, page_count)
        cursor.execute(
            "SELECT user_id, name, email, role, phone, is_active, created_at FROM users "
            "WHERE (%s = '' OR role = %s) "
            "AND (%s = '' OR name LIKE %s OR email LIKE %s) "
            f"ORDER BY {sort_columns[sort_by]} {sort_direction}, user_id ASC "
            "LIMIT %s OFFSET %s",
            (
                role_filter,
                role_filter,
                search,
                f"%{search}%",
                f"%{search}%",
                page_size,
                (page - 1) * page_size,
            ),
        )
        users = cursor.fetchall()
        cursor.execute(
            "SELECT user_id, name, email FROM users "
            "WHERE is_active = TRUE AND role IN ('Admin', 'Supplier', 'Purchaser') "
            "ORDER BY name"
        )
        active_users = cursor.fetchall()
    return render_template(
        "admin_users.html",
        users=users,
        active_users=active_users,
        search=search,
        role_filter=role_filter,
        sort_by=sort_by,
        sort_direction=sort_direction,
        page=page,
        page_count=page_count,
        total_users=total_users,
    )


@app.route("/admin/users/<int:user_id>/edit", methods=["GET", "POST"])
@role_required("Admin")
def admin_edit_user(user_id):
    if request.method == "POST":
        try:
            name = form_text("name", "Name", maximum=120)
            email = form_text("email", "Email", maximum=254).lower()
            phone = form_text("phone", "Phone", required=False, maximum=30)
            role = request.form.get("role", "")
            if not valid_email(email):
                raise ValueError("Enter a valid email address.")
            if role not in VALID_ROLES:
                raise ValueError("Choose a valid user role.")
            with db_cursor() as cursor:
                cursor.execute(
                    "SELECT user_id FROM users "
                    "WHERE role = 'Admin' AND is_active = TRUE "
                    "ORDER BY user_id FOR UPDATE"
                )
                active_admins = cursor.fetchall()
                cursor.execute(
                    "SELECT role, is_active FROM users WHERE user_id = %s FOR UPDATE",
                    (user_id,),
                )
                target = cursor.fetchone()
                if not target:
                    raise ValueError("User account not found.")
                if target["role"] != role:
                    if target["role"] == "Supplier":
                        cursor.execute(
                            "SELECT (SELECT COUNT(*) FROM materials WHERE supplier_id = %s) "
                            "+ (SELECT COUNT(*) FROM orders WHERE supplier_id = %s) "
                            "AS reference_count",
                            (user_id, user_id),
                        )
                        if cursor.fetchone()["reference_count"]:
                            raise ValueError(
                                "A supplier with catalog or order history cannot be assigned "
                                "a different role."
                            )
                    elif target["role"] == "Purchaser":
                        cursor.execute(
                            "SELECT COUNT(*) AS reference_count FROM purchase_requests "
                            "WHERE purchaser_id = %s",
                            (user_id,),
                        )
                        if cursor.fetchone()["reference_count"]:
                            raise ValueError(
                                "A purchaser with request history cannot be assigned "
                                "a different role."
                            )
                cursor.execute(
                    "SELECT user_id FROM users WHERE email = %s AND user_id <> %s",
                    (email, user_id),
                )
                if cursor.fetchone():
                    raise ValueError("Another account already uses that email.")
                if (
                    target["role"] == "Admin"
                    and target["is_active"]
                    and role != "Admin"
                ):
                    if len(active_admins) <= 1:
                        raise ValueError("The last active administrator cannot be demoted.")
                cursor.execute(
                    "UPDATE users SET name = %s, email = %s, phone = %s, role = %s "
                    "WHERE user_id = %s",
                    (name, email, phone or None, role, user_id),
                )
            flash("User account updated.", "success")
            return redirect(url_for("admin_users"))
        except ValueError as error:
            notify_validation_error(error)
    with db_cursor() as cursor:
        cursor.execute(
            "SELECT user_id, name, email, role, phone, is_active "
            "FROM users WHERE user_id = %s",
            (user_id,),
        )
        user = cursor.fetchone()
    if not user:
        abort(404)
    return render_template("admin_edit_user.html", user=user)


@app.route("/admin/users/password", methods=["POST"])
@role_required("Admin")
def admin_reset_password():
    try:
        user_id = positive_int(request.form.get("user_id", ""), "User")
    except ValueError as error:
        notify_validation_error(error)
        return redirect(url_for("admin_users"))
    new_password = request.form.get("new_password", "")
    if len(new_password) < 8:
        flash("The new password must contain at least 8 characters.", "danger")
        return redirect(url_for("admin_users"))
    with db_cursor() as cursor:
        cursor.execute(
            "UPDATE users SET password_hash = %s WHERE user_id = %s AND is_active = TRUE",
            (generate_password_hash(new_password), user_id),
        )
        changed = cursor.rowcount
    if changed:
        flash("Password reset. Give the new password to the account owner securely.", "success")
    else:
        flash("Active user account not found.", "warning")
    return redirect(url_for("admin_users"))


@app.route("/admin/suppliers")
@role_required("Admin")
def admin_suppliers():
    with db_cursor() as cursor:
        cursor.execute(
            "SELECT u.user_id, u.name, u.email, u.phone, u.is_active, u.created_at, "
            "(SELECT COUNT(*) FROM materials m WHERE m.supplier_id = u.user_id) "
            "AS material_count, "
            "(SELECT COUNT(*) FROM orders o WHERE o.supplier_id = u.user_id) AS order_count, "
            "(SELECT COALESCE(SUM(o.total_amount), 0) FROM orders o "
            "WHERE o.supplier_id = u.user_id AND o.order_status <> 'Cancelled') AS order_value "
            "FROM users u WHERE u.role = 'Supplier' ORDER BY u.name"
        )
        suppliers = cursor.fetchall()
    return render_template("admin_suppliers.html", suppliers=suppliers)


@app.route("/admin/purchasers")
@role_required("Admin")
def admin_purchasers():
    with db_cursor() as cursor:
        cursor.execute(
            "SELECT u.user_id, u.name, u.email, u.phone, u.is_active, u.created_at, "
            "COUNT(DISTINCT r.request_id) AS request_count, "
            "COUNT(DISTINCT o.order_id) AS order_count "
            "FROM users u LEFT JOIN purchase_requests r ON r.purchaser_id = u.user_id "
            "LEFT JOIN orders o ON o.request_id = r.request_id "
            "WHERE u.role = 'Purchaser' "
            "GROUP BY u.user_id, u.name, u.email, u.phone, u.is_active, u.created_at "
            "ORDER BY u.name"
        )
        purchasers = cursor.fetchall()
    return render_template("admin_purchasers.html", purchasers=purchasers)


@app.route("/admin/users/<int:user_id>/toggle", methods=["POST"])
@role_required("Admin")
def admin_toggle_user(user_id):
    if user_id == g.user_id:
        flash("You cannot disable your own account.", "danger")
        return redirect(url_for("admin_users"))
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT user_id FROM users "
                "WHERE role = 'Admin' AND is_active = TRUE "
                "ORDER BY user_id FOR UPDATE"
            )
            active_admins = cursor.fetchall()
            cursor.execute(
                "SELECT role, is_active FROM users WHERE user_id = %s FOR UPDATE",
                (user_id,),
            )
            target = cursor.fetchone()
            if not target:
                raise ValueError("User account not found.")
            if target["role"] == "Admin" and target["is_active"]:
                if len(active_admins) <= 1:
                    raise ValueError("The last active administrator cannot be disabled.")
            if target["role"] == "Supplier" and target["is_active"]:
                cursor.execute(
                    "SELECT COUNT(*) AS active_orders FROM orders "
                    "WHERE supplier_id = %s "
                    "AND order_status IN ('Confirmed', 'Processing', 'Dispatched')",
                    (user_id,),
                )
                if cursor.fetchone()["active_orders"]:
                    raise ValueError(
                        "A supplier with active orders cannot be disabled. "
                        "Complete or reassign the orders first."
                    )
            new_status = not target["is_active"]
            cursor.execute(
                "UPDATE users SET is_active = %s WHERE user_id = %s",
                (new_status, user_id),
            )
        flash(
            "User account enabled." if new_status else "User account disabled.",
            "success",
        )
    except ValueError as error:
        notify_validation_error(error)
    return redirect(url_for("admin_users"))


@app.route("/account/password", methods=["GET", "POST"])
@role_required("Admin", "Supplier", "Purchaser")
def change_password():
    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")
        if not current_password or not new_password or not confirm_password:
            flash("Complete all password fields.", "danger")
        elif len(new_password) < 8:
            flash("The new password must contain at least 8 characters.", "danger")
        elif new_password != confirm_password:
            flash("The new password confirmation does not match.", "danger")
        else:
            with db_cursor() as cursor:
                cursor.execute(
                    "SELECT password_hash FROM users WHERE user_id = %s AND is_active = TRUE",
                    (g.user_id,),
                )
                current_user = cursor.fetchone()
                if not current_user or not check_password_hash(
                    current_user["password_hash"], current_password
                ):
                    flash("The current password is incorrect.", "danger")
                else:
                    cursor.execute(
                        "UPDATE users SET password_hash = %s WHERE user_id = %s",
                        (generate_password_hash(new_password), g.user_id),
                    )
                    session.clear()
                    flash("Password changed. Please sign in with your new password.", "success")
                    return redirect(url_for("login"))
    return render_template("change_password.html")


@app.route("/admin/materials", methods=["GET", "POST"])
@role_required("Admin")
def admin_materials():
    with db_cursor() as cursor:
        cursor.execute(
            "SELECT user_id, name FROM users "
            "WHERE role = 'Supplier' AND is_active = TRUE ORDER BY name"
        )
        suppliers = cursor.fetchall()
    if request.method == "POST":
        try:
            name = form_text("material_name", "Material name", maximum=150)
            description = form_text("description", "Description", required=False, maximum=2000)
            unit = form_text("unit", "Unit", maximum=40)
            price = positive_price(request.form.get("price", ""))
            stock = nonnegative_int(request.form.get("stock_quantity", ""), "Stock")
            supplier_id = positive_int(request.form.get("supplier_id", ""), "Supplier")
            with db_cursor() as cursor:
                cursor.execute(
                    "SELECT user_id FROM users "
                    "WHERE user_id = %s AND role = 'Supplier' AND is_active = TRUE",
                    (supplier_id,),
                )
                if not cursor.fetchone():
                    raise ValueError("Choose an active supplier.")
                cursor.execute(
                    "INSERT INTO materials "
                    "(supplier_id, material_name, description, unit, price, stock_quantity) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (supplier_id, name, description or None, unit, price, stock),
                )
            flash("Material listing created.", "success")
            return redirect(url_for("admin_materials"))
        except ValueError as error:
            notify_validation_error(error)
    try:
        supplier_filter = max(0, int(request.args.get("supplier_id", "0")))
    except ValueError:
        supplier_filter = 0
    with db_cursor() as cursor:
        cursor.execute(
            "SELECT m.material_id, m.material_name, m.unit, m.price, "
            "m.stock_quantity, m.is_active, m.description, m.supplier_id, "
            "u.name AS supplier_name "
            "FROM materials m JOIN users u ON u.user_id = m.supplier_id "
            "WHERE (%s = 0 OR m.supplier_id = %s) "
            "ORDER BY m.material_name, u.name",
            (supplier_filter, supplier_filter),
        )
        materials = cursor.fetchall()
    return render_template("admin_materials.html", materials=materials, suppliers=suppliers)


@app.route("/admin/materials/<int:material_id>/edit", methods=["GET", "POST"])
@role_required("Admin")
def admin_edit_material(material_id):
    with db_cursor() as cursor:
        cursor.execute(
            "SELECT user_id, name FROM users "
            "WHERE role = 'Supplier' AND is_active = TRUE ORDER BY name"
        )
        suppliers = cursor.fetchall()
    if request.method == "POST":
        try:
            name = form_text("material_name", "Material name", maximum=150)
            description = form_text("description", "Description", required=False, maximum=2000)
            unit = form_text("unit", "Unit", maximum=40)
            price = positive_price(request.form.get("price", ""))
            stock = nonnegative_int(request.form.get("stock_quantity", ""), "Stock")
            supplier_id = positive_int(request.form.get("supplier_id", ""), "Supplier")
            with db_cursor() as cursor:
                cursor.execute(
                    "SELECT user_id FROM users "
                    "WHERE user_id = %s AND role = 'Supplier' AND is_active = TRUE",
                    (supplier_id,),
                )
                if not cursor.fetchone():
                    raise ValueError("Choose an active supplier.")
                cursor.execute(
                    "SELECT supplier_id FROM materials "
                    "WHERE material_id = %s FOR UPDATE",
                    (material_id,),
                )
                current_material = cursor.fetchone()
                if not current_material:
                    raise ValueError("Material listing not found.")
                if current_material["supplier_id"] != supplier_id:
                    cursor.execute(
                        "SELECT COUNT(*) AS reference_count FROM request_items "
                        "WHERE material_id = %s",
                        (material_id,),
                    )
                    if cursor.fetchone()["reference_count"]:
                        raise ValueError(
                            "Supplier ownership cannot be changed after this material "
                            "appears in purchase history."
                        )
                cursor.execute(
                    "UPDATE materials SET supplier_id = %s, material_name = %s, "
                    "description = %s, unit = %s, price = %s, stock_quantity = %s "
                    "WHERE material_id = %s",
                    (
                        supplier_id,
                        name,
                        description or None,
                        unit,
                        price,
                        stock,
                        material_id,
                    ),
                )
            flash("Material listing updated.", "success")
            return redirect(url_for("admin_materials"))
        except ValueError as error:
            notify_validation_error(error)
    with db_cursor() as cursor:
        cursor.execute(
            "SELECT material_id, supplier_id, material_name, description, unit, "
            "price, stock_quantity FROM materials WHERE material_id = %s",
            (material_id,),
        )
        material = cursor.fetchone()
    if not material:
        abort(404)
    return render_template(
        "admin_edit_material.html", material=material, suppliers=suppliers
    )


@app.route("/admin/materials/<int:material_id>/delete", methods=["POST"])
@role_required("Admin")
def admin_delete_material(material_id):
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT material_id FROM materials WHERE material_id = %s FOR UPDATE",
                (material_id,),
            )
            if not cursor.fetchone():
                raise ValueError("Material listing not found.")
            cursor.execute(
                "SELECT COUNT(*) AS reference_count FROM request_items "
                "WHERE material_id = %s",
                (material_id,),
            )
            if cursor.fetchone()["reference_count"]:
                cursor.execute(
                    "UPDATE materials SET is_active = FALSE WHERE material_id = %s",
                    (material_id,),
                )
                flash(
                    "This listing is part of purchase history, so it was archived instead of deleted.",
                    "success",
                )
            else:
                cursor.execute(
                    "DELETE FROM materials WHERE material_id = %s", (material_id,)
                )
                flash("Material listing deleted.", "success")
    except ValueError as error:
        notify_validation_error(error)
    except mysql.connector.IntegrityError:
        app.logger.info("Admin could not delete material %s due to a new reference.", material_id)
        flash("This material is now referenced by a transaction and cannot be deleted.", "danger")
    return redirect(url_for("admin_materials"))


@app.route("/admin/materials/<int:material_id>/restore", methods=["POST"])
@role_required("Admin")
def admin_restore_material(material_id):
    with db_cursor() as cursor:
        cursor.execute(
            "UPDATE materials SET is_active = TRUE "
            "WHERE material_id = %s AND is_active = FALSE",
            (material_id,),
        )
        restored = cursor.rowcount
    if restored:
        flash("Material listing restored.", "success")
    else:
        flash("Archived material listing not found.", "warning")
    return redirect(url_for("admin_materials"))


@app.route("/admin/requests")
@role_required("Admin")
def admin_requests():
    try:
        purchaser_filter = max(0, int(request.args.get("purchaser_id", "0")))
    except ValueError:
        purchaser_filter = 0
    try:
        supplier_filter = max(0, int(request.args.get("supplier_id", "0")))
    except ValueError:
        supplier_filter = 0
    with db_cursor() as cursor:
        cursor.execute(
            "SELECT r.request_id, r.purchaser_id, r.request_date, r.required_date, r.status, "
            "u.name AS purchaser_name, u.email AS purchaser_email, "
            "COUNT(ri.request_item_id) AS item_count, "
            "COALESCE(SUM(ri.quantity * ri.unit_price), 0) AS request_total "
            "FROM purchase_requests r JOIN users u ON u.user_id = r.purchaser_id "
            "LEFT JOIN request_items ri ON ri.request_id = r.request_id "
            "WHERE (%s = 0 OR r.purchaser_id = %s) "
            "GROUP BY r.request_id, r.request_date, r.required_date, r.status, "
            "u.name, u.email ORDER BY r.request_date DESC",
            (purchaser_filter, purchaser_filter),
        )
        requests = cursor.fetchall()
        cursor.execute(
            "SELECT r.request_id, r.purchaser_id, r.required_date, "
            "p.name AS purchaser_name, ri.request_item_id, ri.material_id, "
            "ri.quantity, ri.unit_price, ri.supplier_id AS source_supplier_id, "
            "ri.material_name_snapshot AS material_name, ri.unit_snapshot AS unit "
            "FROM purchase_requests r "
            "JOIN users p ON p.user_id = r.purchaser_id "
            "JOIN request_items ri ON ri.request_id = r.request_id "
            "JOIN materials m ON m.material_id = ri.material_id "
            "LEFT JOIN order_items oi ON oi.request_item_id = ri.request_item_id "
            "WHERE r.status = 'Approved' AND oi.order_item_id IS NULL "
            "AND (%s = 0 OR r.purchaser_id = %s) "
            "ORDER BY r.required_date, r.request_id, ri.request_item_id",
            (purchaser_filter, purchaser_filter),
        )
        pending_lines = cursor.fetchall()
        cursor.execute(
            "SELECT o.order_id, o.request_id, o.supplier_id, o.order_status, "
            "o.total_amount, o.order_date, r.status AS request_status, "
            "r.required_date, p.name AS purchaser_name, s.name AS supplier_name, "
            "d.delivery_address, d.delivery_status, "
            "COALESCE(SUM(oi.quantity * oi.unit_price), 0) AS snapshot_total, "
            "GROUP_CONCAT(CONCAT(oi.quantity, ' ', oi.unit_snapshot, ' ', "
            "oi.material_name_snapshot) ORDER BY oi.material_name_snapshot "
            "SEPARATOR ', ') AS items_summary "
            "FROM orders o "
            "JOIN purchase_requests r ON r.request_id = o.request_id "
            "JOIN users p ON p.user_id = r.purchaser_id "
            "JOIN users s ON s.user_id = o.supplier_id "
            "LEFT JOIN deliveries d ON d.order_id = o.order_id "
            "LEFT JOIN order_items oi ON oi.order_id = o.order_id "
            "WHERE (%s = 0 OR r.purchaser_id = %s) "
            "AND (%s = 0 OR o.supplier_id = %s) "
            "GROUP BY o.order_id, o.request_id, o.supplier_id, o.order_status, "
            "o.total_amount, o.order_date, r.status, r.required_date, "
            "p.name, s.name, d.delivery_address, d.delivery_status "
            "ORDER BY o.order_date DESC",
            (purchaser_filter, purchaser_filter, supplier_filter, supplier_filter),
        )
        orders = cursor.fetchall()
        cursor.execute(
            "SELECT user_id, name FROM users "
            "WHERE role = 'Supplier' AND is_active = TRUE ORDER BY name"
        )
        suppliers = cursor.fetchall()

        grouped_lines = {}
        for line in pending_lines:
            group_key = (line["request_id"], line["source_supplier_id"])
            grouped_lines.setdefault(group_key, []).append(line)
        assignments = []
        for (request_id, source_supplier_id), lines in grouped_lines.items():
            candidates = supplier_offers_for_lines(
                cursor, lines, source_supplier_id=source_supplier_id
            )
            if supplier_filter:
                candidates = [
                    candidate for candidate in candidates
                    if candidate["supplier_id"] == supplier_filter
                ]
            selected = next(
                (
                    candidate for candidate in candidates
                    if candidate["supplier_id"] == source_supplier_id
                ),
                candidates[0] if candidates else None,
            )
            first = lines[0]
            assignments.append(
                {
                    "request_id": request_id,
                    "required_date": first["required_date"],
                    "purchaser_name": first["purchaser_name"],
                    "purchaser_id": first["purchaser_id"],
                    "source_supplier_id": source_supplier_id,
                    "item_count": len(lines),
                    "items_summary": ", ".join(
                        f'{line["quantity"]} {line["unit"]} {line["material_name"]}'
                        for line in lines
                    ),
                    "lines": lines,
                    "suppliers": candidates,
                    "selected_supplier_id": (
                        selected["supplier_id"] if selected else None
                    ),
                    "supplier_total": selected["total"] if selected else None,
                }
            )
        assignments.sort(key=lambda row: (row["required_date"], row["request_id"]))

        order_lines_by_id = defaultdict(list)
        cursor.execute(
            "SELECT oi.order_id, oi.request_item_id, oi.material_id, "
            "oi.material_name_snapshot AS material_name, oi.unit_snapshot AS unit, "
            "oi.quantity, oi.unit_price "
            "FROM order_items oi "
            "JOIN orders o ON o.order_id = oi.order_id "
            "WHERE (%s = 0 OR o.order_id IN "
            "(SELECT o2.order_id FROM orders o2 JOIN purchase_requests r2 "
            "ON r2.request_id = o2.request_id WHERE r2.purchaser_id = %s)) "
            "AND (%s = 0 OR o.supplier_id = %s) "
            "ORDER BY oi.order_id, oi.order_item_id",
            (purchaser_filter, purchaser_filter, supplier_filter, supplier_filter),
        )
        for line in cursor.fetchall():
            order_lines_by_id[line["order_id"]].append(line)
        for order in orders:
            lines = order_lines_by_id.get(order["order_id"], [])
            candidates = (
                supplier_offers_for_lines(
                    cursor, lines, source_supplier_id=order["supplier_id"]
                )
                if lines
                else []
            )
            candidates = [
                candidate
                for candidate in candidates
                if candidate["supplier_id"] != order["supplier_id"]
            ]
            order["suppliers"] = candidates
            order["snapshot_mismatch"] = (
                Decimal(str(order["total_amount"]))
                != Decimal(str(order["snapshot_total"]))
            )
    return render_template(
        "admin_requests.html",
        requests=requests,
        assignments=assignments,
        orders=orders,
        suppliers=suppliers,
    )


@app.route("/admin/requests/<int:request_id>/decision", methods=["POST"])
@role_required("Admin")
def admin_request_decision(request_id):
    decision = request.form.get("decision")
    if decision not in {"Approved", "Rejected"}:
        flash("Choose a valid request decision.", "danger")
        return redirect(url_for("admin_requests"))
    with db_cursor() as cursor:
        cursor.execute(
            "UPDATE purchase_requests SET status = %s "
            "WHERE request_id = %s AND status = 'Pending'",
            (decision, request_id),
        )
        changed = cursor.rowcount
    if changed:
        flash(f"Request #{request_id} {decision.lower()}.", "success")
    else:
        flash("That request was not found or has already been reviewed.", "warning")
    return redirect(url_for("admin_requests"))


@app.route("/admin/requests/<int:request_id>/assign", methods=["POST"])
@app.route("/admin/requests/<int:request_id>/assign/<int:supplier_id>", methods=["POST"])
@role_required("Admin")
def admin_assign_order(request_id, supplier_id=None):
    address = request.form.get("delivery_address", "").strip()
    if not address or len(address) > 500:
        flash("Enter a delivery address (up to 500 characters).", "danger")
        return redirect(url_for("admin_requests"))
    try:
        supplier_id = positive_int(
            request.form.get("supplier_id", supplier_id), "Supplier"
        )
        selected_ids = [
            positive_int(value, "Request item")
            for value in request.form.getlist("request_item_id")
        ]
        if len(selected_ids) != len(set(selected_ids)):
            raise ValueError("A request item was selected more than once.")
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT r.request_id FROM purchase_requests r "
                "WHERE r.request_id = %s AND r.status = 'Approved' FOR UPDATE",
                (request_id,),
            )
            if not cursor.fetchone():
                raise ValueError("Only approved requests can be assigned.")
            cursor.execute(
                "SELECT ri.request_item_id, ri.material_id, ri.quantity, ri.unit_price, "
                "ri.material_name_snapshot AS material_name, "
                "ri.unit_snapshot AS unit, ri.supplier_id AS source_supplier_id "
                "FROM request_items ri JOIN materials m ON m.material_id = ri.material_id "
                "LEFT JOIN order_items oi ON oi.request_item_id = ri.request_item_id "
                "WHERE ri.request_id = %s AND oi.order_item_id IS NULL "
                "ORDER BY ri.request_item_id FOR UPDATE",
                (request_id,),
            )
            lines = cursor.fetchall()
            if selected_ids:
                available_by_id = {line["request_item_id"]: line for line in lines}
                if any(item_id not in available_by_id for item_id in selected_ids):
                    raise ValueError(
                        "One or more selected items are no longer available for assignment."
                    )
                lines = [available_by_id[item_id] for item_id in selected_ids]
            else:
                lines = [
                    line
                    for line in lines
                    if line["source_supplier_id"] == supplier_id
                ]
            if not lines:
                raise ValueError("All request items have already been assigned.")
            candidates = supplier_offers_for_lines(
                cursor, lines
            )
            supplier_choice = next(
                (candidate for candidate in candidates if candidate["supplier_id"] == supplier_id),
                None,
            )
            if not supplier_choice:
                raise ValueError(
                    "This active supplier does not offer every selected material "
                    "with enough available stock."
                )
            if supplier_choice["total"] > MAX_ORDER_TOTAL:
                raise ValueError("This supplier-specific order exceeds the supported total.")
            cursor.execute(
                "SELECT order_id FROM orders WHERE request_id = %s AND supplier_id = %s",
                (request_id, supplier_id),
            )
            existing_order = cursor.fetchone()
            if existing_order:
                raise ValueError(
                    "An order already exists for that supplier and request. "
                    "Use Reassign instead."
                )
            supplier_total = supplier_choice["total"].quantize(Decimal("0.01"))
            cursor.execute(
                "INSERT INTO orders (request_id, supplier_id, total_amount) "
                "VALUES (%s, %s, %s)",
                (request_id, supplier_id, supplier_total),
            )
            order_id = cursor.lastrowid
            for line in lines:
                selected_offer = supplier_choice["offers"][line["request_item_id"]]
                cursor.execute(
                    "INSERT INTO order_items "
                    "(order_id, request_item_id, material_id, material_name_snapshot, "
                    "unit_snapshot, quantity, unit_price) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (
                        order_id,
                        line["request_item_id"],
                        selected_offer["material_id"],
                        selected_offer["material_name"],
                        selected_offer["unit"],
                        line["quantity"],
                        selected_offer["unit_price"],
                    ),
                )
            cursor.execute(
                "INSERT INTO deliveries (order_id, delivery_address) VALUES (%s, %s)",
                (order_id, address),
            )
    except ValueError as error:
        notify_validation_error(error)
    else:
        flash(f"Order #{order_id} assigned and delivery record created.", "success")
    return redirect(url_for("admin_requests"))


@app.route("/admin/orders/<int:order_id>/reassign", methods=["POST"])
@role_required("Admin")
def admin_reassign_order(order_id):
    try:
        supplier_id = positive_int(request.form.get("supplier_id", ""), "Supplier")
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT order_id, request_id, supplier_id, order_status, total_amount "
                "FROM orders "
                "WHERE order_id = %s FOR UPDATE",
                (order_id,),
            )
            order = cursor.fetchone()
            if not order:
                raise ValueError("Order not found.")
            if order["order_status"] not in {"Confirmed", "Cancelled"}:
                raise ValueError(
                    "Only unaccepted or rejected orders can be reassigned."
                )
            cursor.execute(
                "SELECT request_id FROM purchase_requests "
                "WHERE request_id = %s AND status = 'Approved'",
                (order["request_id"],),
            )
            if not cursor.fetchone():
                raise ValueError("The related request is not approved.")
            cursor.execute(
                "SELECT oi.request_item_id, oi.material_id, "
                "oi.material_name_snapshot AS material_name, "
                "oi.unit_snapshot AS unit, oi.quantity, oi.unit_price "
                "FROM order_items oi "
                "WHERE oi.order_id = %s ORDER BY oi.order_item_id FOR UPDATE",
                (order_id,),
            )
            lines = cursor.fetchall()
            if not lines:
                raise ValueError("This order has no saved item details and cannot be reassigned safely.")
            candidates = supplier_offers_for_lines(cursor, lines)
            supplier_choice = next(
                (candidate for candidate in candidates if candidate["supplier_id"] == supplier_id),
                None,
            )
            if not supplier_choice:
                raise ValueError(
                    "Choose an active supplier offering every order item with enough stock."
                )
            if supplier_choice["total"] > MAX_ORDER_TOTAL:
                raise ValueError("The reassigned order exceeds the supported total.")
            if supplier_id == order["supplier_id"]:
                raise ValueError("Choose a different supplier to reassign this order.")
            cursor.execute(
                "SELECT order_id FROM orders "
                "WHERE request_id = %s AND supplier_id = %s AND order_id <> %s",
                (order["request_id"], supplier_id, order_id),
            )
            if cursor.fetchone():
                raise ValueError(
                    "A separate order already exists for that supplier and request."
                )
            new_total = supplier_choice["total"].quantize(Decimal("0.01"))
            cursor.execute(
                "INSERT INTO order_assignment_history "
                "(order_id, previous_supplier_id, new_supplier_id, previous_total, "
                "new_total, previous_status, changed_by) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    order_id,
                    order["supplier_id"],
                    supplier_id,
                    order["total_amount"],
                    new_total,
                    order["order_status"],
                    g.user_id,
                ),
            )
            history_id = cursor.lastrowid
            for line in lines:
                new_offer = supplier_choice["offers"][line["request_item_id"]]
                cursor.execute(
                    "INSERT INTO order_assignment_history_items "
                    "(assignment_history_id, previous_material_name, previous_unit, "
                    "quantity, previous_unit_price, new_material_name, new_unit, "
                    "new_unit_price) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        history_id,
                        line["material_name"],
                        line["unit"],
                        line["quantity"],
                        line["unit_price"],
                        new_offer["material_name"],
                        new_offer["unit"],
                        new_offer["unit_price"],
                    ),
                )
                cursor.execute(
                    "UPDATE order_items SET material_id = %s, "
                    "material_name_snapshot = %s, unit_snapshot = %s, unit_price = %s "
                    "WHERE request_item_id = %s AND order_id = %s",
                    (
                        new_offer["material_id"],
                        new_offer["material_name"],
                        new_offer["unit"],
                        new_offer["unit_price"],
                        line["request_item_id"],
                        order_id,
                    ),
                )
            cursor.execute(
                "UPDATE orders SET supplier_id = %s, total_amount = %s, "
                "order_status = 'Confirmed' WHERE order_id = %s",
                (supplier_id, new_total, order_id),
            )
            cursor.execute(
                "UPDATE deliveries SET delivery_status = 'Pending', "
                "dispatch_date = NULL, delivery_date = NULL WHERE order_id = %s",
                (order_id,),
            )
        flash(f"Order #{order_id} reassigned and returned to supplier confirmation.", "success")
    except ValueError as error:
        notify_validation_error(error)
    return redirect(url_for("admin_requests"))


@app.route("/admin/reports")
@role_required("Admin")
def admin_reports():
    with db_cursor() as cursor:
        cursor.execute(
            "SELECT status, COUNT(*) AS request_count FROM purchase_requests "
            "GROUP BY status ORDER BY status"
        )
        request_report = cursor.fetchall()
        cursor.execute(
            "SELECT order_status, COUNT(*) AS order_count, "
            "COALESCE(SUM(total_amount), 0) AS total_value "
            "FROM orders GROUP BY order_status ORDER BY order_status"
        )
        order_report = cursor.fetchall()
        cursor.execute(
            "SELECT u.name AS supplier_name, COUNT(o.order_id) AS order_count, "
            "COALESCE(SUM(o.total_amount), 0) AS sales_total "
            "FROM users u LEFT JOIN orders o ON o.supplier_id = u.user_id "
            "WHERE u.role = 'Supplier' GROUP BY u.user_id, u.name "
            "ORDER BY sales_total DESC"
        )
        supplier_report = cursor.fetchall()
        cursor.execute(
            "SELECT m.material_name, COUNT(ri.request_item_id) AS line_count, "
            "COALESCE(SUM(ri.quantity), 0) AS units_requested "
            "FROM materials m LEFT JOIN request_items ri ON ri.material_id = m.material_id "
            "GROUP BY m.material_id, m.material_name ORDER BY units_requested DESC"
        )
        material_report = cursor.fetchall()
    return render_template(
        "admin_reports.html",
        request_report=request_report,
        order_report=order_report,
        supplier_report=supplier_report,
        material_report=material_report,
    )


@app.route("/supplier")
@role_required("Supplier")
def supplier_dashboard():
    with db_cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) AS total FROM materials WHERE supplier_id = %s",
            (g.user_id,),
        )
        material_count = cursor.fetchone()["total"]
        cursor.execute(
            "SELECT COUNT(*) AS total FROM orders WHERE supplier_id = %s "
            "AND order_status NOT IN ('Delivered', 'Cancelled')",
            (g.user_id,),
        )
        active_orders = cursor.fetchone()["total"]
        cursor.execute(
            "SELECT COUNT(*) AS total FROM orders WHERE supplier_id = %s "
            "AND order_status = 'Confirmed'",
            (g.user_id,),
        )
        awaiting_response = cursor.fetchone()["total"]
        cursor.execute(
            "SELECT o.order_id, o.order_status, o.total_amount, o.order_date, "
            "r.required_date, p.name AS purchaser_name, "
            "(SELECT GROUP_CONCAT(CONCAT(oi.quantity, ' ', oi.unit_snapshot, ' ', "
            "oi.material_name_snapshot) ORDER BY oi.material_name_snapshot SEPARATOR ', ') "
            "FROM order_items oi WHERE oi.order_id = o.order_id) AS items_summary "
            "FROM orders o JOIN purchase_requests r ON r.request_id = o.request_id "
            "JOIN users p ON p.user_id = r.purchaser_id "
            "WHERE o.supplier_id = %s ORDER BY o.order_date DESC LIMIT 8",
            (g.user_id,),
        )
        recent_orders = cursor.fetchall()
    return render_template(
        "supplier_dashboard.html",
        material_count=material_count,
        active_orders=active_orders,
        awaiting_response=awaiting_response,
        recent_orders=recent_orders,
    )


@app.route("/supplier/materials", methods=["GET", "POST"])
@role_required("Supplier")
def supplier_materials():
    if request.method == "POST":
        try:
            name = form_text("material_name", "Material name", maximum=150)
            description = form_text("description", "Description", required=False, maximum=2000)
            unit = form_text("unit", "Unit", maximum=40)
            price = positive_price(request.form.get("price", ""))
            stock = nonnegative_int(request.form.get("stock_quantity", ""), "Stock")
            with db_cursor() as cursor:
                cursor.execute(
                    "INSERT INTO materials "
                    "(supplier_id, material_name, description, unit, price, stock_quantity) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (g.user_id, name, description or None, unit, price, stock),
                )
            flash("Material added to your catalog.", "success")
            return redirect(url_for("supplier_materials"))
        except (ValueError, InvalidOperation) as error:
            notify_validation_error(error)
    with db_cursor() as cursor:
        cursor.execute(
            "SELECT material_id, material_name, description, unit, price, "
            "stock_quantity, is_active FROM materials "
            "WHERE supplier_id = %s ORDER BY material_name",
            (g.user_id,),
        )
        materials = cursor.fetchall()
    return render_template("supplier_materials.html", materials=materials)


@app.route("/supplier/materials/<int:material_id>/stock", methods=["POST"])
@role_required("Supplier")
def supplier_update_stock(material_id):
    try:
        stock = nonnegative_int(request.form.get("stock_quantity", ""), "Stock")
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT is_active FROM materials "
                "WHERE material_id = %s AND supplier_id = %s FOR UPDATE",
                (material_id, g.user_id),
            )
            material = cursor.fetchone()
            if not material:
                raise ValueError("Material not found in your catalog.")
            if not material["is_active"]:
                raise ValueError("Archived listings cannot receive stock updates.")
            cursor.execute(
                "UPDATE materials SET stock_quantity = %s "
                "WHERE material_id = %s AND supplier_id = %s AND is_active = TRUE",
                (stock, material_id, g.user_id),
            )
        flash("Stock quantity updated.", "success")
    except ValueError as error:
        notify_validation_error(error)
    return redirect(url_for("supplier_materials"))


@app.route("/supplier/materials/<int:material_id>/edit", methods=["GET", "POST"])
@role_required("Supplier")
def supplier_edit_material(material_id):
    if request.method == "POST":
        try:
            name = form_text("material_name", "Material name", maximum=150)
            description = form_text("description", "Description", required=False, maximum=2000)
            unit = form_text("unit", "Unit", maximum=40)
            price = positive_price(request.form.get("price", ""))
            stock = nonnegative_int(request.form.get("stock_quantity", ""), "Stock")
            with db_cursor() as cursor:
                cursor.execute(
                    "SELECT material_id FROM materials "
                    "WHERE material_id = %s AND supplier_id = %s FOR UPDATE",
                    (material_id, g.user_id),
                )
                if not cursor.fetchone():
                    flash("Material not found in your catalog.", "warning")
                    return redirect(url_for("supplier_materials"))
                cursor.execute(
                    "UPDATE materials SET material_name = %s, description = %s, "
                    "unit = %s, price = %s, stock_quantity = %s "
                    "WHERE material_id = %s AND supplier_id = %s",
                    (
                        name,
                        description or None,
                        unit,
                        price,
                        stock,
                        material_id,
                        g.user_id,
                    ),
                )
            flash("Material listing updated.", "success")
            return redirect(url_for("supplier_materials"))
        except ValueError as error:
            notify_validation_error(error)
    with db_cursor() as cursor:
        cursor.execute(
            "SELECT material_id, material_name, description, unit, price, stock_quantity "
            "FROM materials WHERE material_id = %s AND supplier_id = %s",
            (material_id, g.user_id),
        )
        material = cursor.fetchone()
    if not material:
        abort(404)
    return render_template("supplier_edit_material.html", material=material)


@app.route("/supplier/materials/<int:material_id>/delete", methods=["POST"])
@role_required("Supplier")
def supplier_delete_material(material_id):
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT material_id FROM materials "
                "WHERE material_id = %s AND supplier_id = %s FOR UPDATE",
                (material_id, g.user_id),
            )
            if not cursor.fetchone():
                flash("Material not found in your catalog.", "warning")
                return redirect(url_for("supplier_materials"))
            cursor.execute(
                "SELECT COUNT(*) AS reference_count FROM request_items "
                "WHERE material_id = %s",
                (material_id,),
            )
            if cursor.fetchone()["reference_count"]:
                cursor.execute(
                    "UPDATE materials SET is_active = FALSE "
                    "WHERE material_id = %s AND supplier_id = %s",
                    (material_id, g.user_id),
                )
                flash(
                    "This listing is used in purchase history, so it was archived instead of deleted.",
                    "success",
                )
            else:
                cursor.execute(
                    "DELETE FROM materials WHERE material_id = %s AND supplier_id = %s",
                    (material_id, g.user_id),
                )
                deleted = cursor.rowcount
                if deleted:
                    flash("Material listing deleted.", "success")
                else:
                    flash("Material not found in your catalog.", "warning")
    except mysql.connector.IntegrityError:
        app.logger.info(
            "Supplier %s could not delete material %s because it gained a reference.",
            g.user_id,
            material_id,
        )
        flash(
            "This material cannot be deleted because it is referenced by existing records.",
            "danger",
        )
    return redirect(url_for("supplier_materials"))


@app.route("/supplier/materials/<int:material_id>/restore", methods=["POST"])
@role_required("Supplier")
def supplier_restore_material(material_id):
    with db_cursor() as cursor:
        cursor.execute(
            "UPDATE materials SET is_active = TRUE "
            "WHERE material_id = %s AND supplier_id = %s AND is_active = FALSE",
            (material_id, g.user_id),
        )
        restored = cursor.rowcount
    if restored:
        flash("Material listing restored and available for new requests.", "success")
    else:
        flash("Archived listing not found in your catalog.", "warning")
    return redirect(url_for("supplier_materials"))


@app.route("/supplier/orders")
@role_required("Supplier")
def supplier_orders():
    with db_cursor() as cursor:
        cursor.execute(
            "SELECT o.order_id, o.order_status, o.total_amount, o.order_date, "
            "r.required_date, r.request_id, p.name AS purchaser_name, "
            "d.delivery_address, d.delivery_status, "
            "GROUP_CONCAT(CONCAT(oi.quantity, ' ', oi.unit_snapshot, ' ', "
            "oi.material_name_snapshot) ORDER BY oi.material_name_snapshot "
            "SEPARATOR ', ') AS items_summary "
            "FROM orders o JOIN purchase_requests r ON r.request_id = o.request_id "
            "JOIN users p ON p.user_id = r.purchaser_id "
            "LEFT JOIN deliveries d ON d.order_id = o.order_id "
            "LEFT JOIN order_items oi ON oi.order_id = o.order_id "
            "WHERE o.supplier_id = %s "
            "GROUP BY o.order_id, o.order_status, o.total_amount, o.order_date, "
            "r.required_date, r.request_id, p.name, d.delivery_address, d.delivery_status "
            "ORDER BY o.order_date DESC",
            (g.user_id,),
        )
        orders = cursor.fetchall()
    return render_template("supplier_orders.html", orders=orders)


@app.route("/supplier/orders/<int:order_id>/decision", methods=["POST"])
@role_required("Supplier")
def supplier_order_decision(order_id):
    decision = request.form.get("decision")
    if decision not in {"accept", "reject"}:
        flash("Choose accept or reject.", "danger")
        return redirect(url_for("supplier_orders"))
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT order_id, request_id FROM orders "
                "WHERE order_id = %s AND supplier_id = %s "
                "AND order_status = 'Confirmed' FOR UPDATE",
                (order_id, g.user_id),
            )
            order = cursor.fetchone()
            if not order:
                raise ValueError("This order is not awaiting your response.")
            if decision == "reject":
                cursor.execute(
                    "UPDATE orders SET order_status = 'Cancelled' WHERE order_id = %s",
                    (order_id,),
                )
                flash(f"Order #{order_id} rejected.", "success")
            else:
                cursor.execute(
                    "SELECT oi.material_id, m.stock_quantity, oi.quantity "
                    "FROM order_items oi JOIN materials m ON m.material_id = oi.material_id "
                    "WHERE oi.order_id = %s FOR UPDATE",
                    (order_id,),
                )
                lines = cursor.fetchall()
                if not lines:
                    raise ValueError("The order contains no materials assigned to you.")
                insufficient = [line for line in lines if line["stock_quantity"] < line["quantity"]]
                if insufficient:
                    raise ValueError(
                        "There is not enough stock to accept this order. Update your stock first."
                    )
                for line in lines:
                    cursor.execute(
                        "UPDATE materials SET stock_quantity = stock_quantity - %s "
                        "WHERE material_id = %s AND stock_quantity >= %s",
                        (line["quantity"], line["material_id"], line["quantity"]),
                    )
                    if cursor.rowcount != 1:
                        raise ValueError("Stock changed during acceptance. Please retry.")
                cursor.execute(
                    "UPDATE orders SET order_status = 'Processing' WHERE order_id = %s",
                    (order_id,),
                )
                flash(f"Order #{order_id} accepted and moved to processing.", "success")
    except ValueError as error:
        notify_validation_error(error)
    return redirect(url_for("supplier_orders"))


@app.route("/supplier/orders/<int:order_id>/status", methods=["POST"])
@role_required("Supplier")
def supplier_update_order_status(order_id):
    next_status = request.form.get("order_status")
    transitions = {"Dispatched", "Delivered"}
    if next_status not in transitions:
        flash("Choose a valid fulfillment status.", "danger")
        return redirect(url_for("supplier_orders"))
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT order_status FROM orders "
                "WHERE order_id = %s AND supplier_id = %s FOR UPDATE",
                (order_id, g.user_id),
            )
            order = cursor.fetchone()
            allowed = {
                "Processing": {"Dispatched"},
                "Dispatched": {"Delivered"},
            }
            if not order or next_status not in allowed.get(order["order_status"], set()):
                raise ValueError("That status change is not allowed for this order.")
            cursor.execute(
                "UPDATE orders SET order_status = %s WHERE order_id = %s",
                (next_status, order_id),
            )
            if next_status == "Dispatched":
                cursor.execute(
                    "UPDATE deliveries SET delivery_status = 'In Transit', "
                    "dispatch_date = CURDATE() WHERE order_id = %s",
                    (order_id,),
                )
            else:
                cursor.execute(
                    "UPDATE deliveries SET delivery_status = 'Delivered', "
                    "delivery_date = CURDATE() WHERE order_id = %s",
                    (order_id,),
                )
        flash(f"Order #{order_id} updated to {next_status}.", "success")
    except ValueError as error:
        notify_validation_error(error)
    return redirect(url_for("supplier_orders"))


@app.route("/purchaser")
@role_required("Purchaser")
def purchaser_dashboard():
    with db_cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) AS total FROM purchase_requests WHERE purchaser_id = %s",
            (g.user_id,),
        )
        request_count = cursor.fetchone()["total"]
        cursor.execute(
            "SELECT COUNT(*) AS total FROM purchase_requests "
            "WHERE purchaser_id = %s AND status = 'Pending'",
            (g.user_id,),
        )
        pending_count = cursor.fetchone()["total"]
        cursor.execute(
            "SELECT COUNT(*) AS total FROM orders o "
            "JOIN purchase_requests r ON r.request_id = o.request_id "
            "WHERE r.purchaser_id = %s AND o.order_status NOT IN ('Delivered', 'Cancelled')",
            (g.user_id,),
        )
        active_orders = cursor.fetchone()["total"]
        cursor.execute(
            "SELECT r.request_id, r.request_date, r.required_date, r.status, "
            "COUNT(ri.request_item_id) AS item_count, "
            "COALESCE(SUM(ri.quantity * ri.unit_price), 0) AS request_total "
            "FROM purchase_requests r LEFT JOIN request_items ri ON ri.request_id = r.request_id "
            "WHERE r.purchaser_id = %s "
            "GROUP BY r.request_id, r.request_date, r.required_date, r.status "
            "ORDER BY r.request_date DESC LIMIT 8",
            (g.user_id,),
        )
        recent_requests = cursor.fetchall()
    return render_template(
        "purchaser_dashboard.html",
        request_count=request_count,
        pending_count=pending_count,
        active_orders=active_orders,
        recent_requests=recent_requests,
    )


@app.route("/purchaser/materials")
@role_required("Purchaser")
def purchaser_materials():
    search = request.args.get("q", "").strip()
    with db_cursor() as cursor:
        cursor.execute(
            "SELECT m.material_id, m.material_name, m.description, m.unit, m.price, "
            "m.stock_quantity, u.name AS supplier_name, u.is_active AS supplier_active "
            "FROM materials m JOIN users u ON u.user_id = m.supplier_id "
            "WHERE u.role = 'Supplier' AND m.is_active = TRUE AND "
            "(%s = '' OR m.material_name LIKE %s OR u.name LIKE %s) "
            "ORDER BY m.material_name, m.unit, u.name, m.price",
            (search, f"%{search}%", f"%{search}%"),
        )
        materials = cursor.fetchall()
    return render_template("purchaser_materials.html", materials=materials, search=search)


@app.route("/purchaser/requests/new", methods=["GET", "POST"])
@role_required("Purchaser")
def purchaser_new_request():
    if request.method == "POST":
        try:
            required_date = date.fromisoformat(request.form.get("required_date", ""))
            if required_date < date.today():
                raise ValueError("Required date cannot be in the past.")
            material_ids = request.form.getlist("material_id")
            quantities = request.form.getlist("quantity")
            if not material_ids or len(material_ids) != len(quantities):
                raise ValueError("Add at least one material and quantity.")
            parsed_items = {}
            for material_id, quantity_value in zip(material_ids, quantities):
                material_number = positive_int(material_id, "Material")
                quantity = positive_int(quantity_value, "Quantity")
                if material_number in parsed_items:
                    raise ValueError("A material can only appear once in a request.")
                parsed_items[material_number] = quantity
            with db_cursor() as cursor:
                item_rows = []
                for material_id, quantity in parsed_items.items():
                    cursor.execute(
                        "SELECT m.material_id, m.supplier_id, m.material_name, m.unit, "
                        "m.price, m.stock_quantity "
                        "FROM materials m JOIN users u ON u.user_id = m.supplier_id "
                        "WHERE m.material_id = %s AND u.role = 'Supplier' "
                        "AND u.is_active = TRUE AND m.is_active = TRUE FOR UPDATE",
                        (material_id,),
                    )
                    material = cursor.fetchone()
                    if not material:
                        raise ValueError("One of the selected materials is unavailable.")
                    if quantity > material["stock_quantity"]:
                        raise ValueError(
                            f"Requested quantity exceeds available stock for material "
                            f"#{material_id}."
                        )
                    item_rows.append(
                        (
                            material_id,
                            material["supplier_id"],
                            material["material_name"],
                            material["unit"],
                            quantity,
                            material["price"],
                        )
                    )
                cursor.execute(
                    "INSERT INTO purchase_requests (purchaser_id, required_date) "
                    "VALUES (%s, %s)",
                    (g.user_id, required_date),
                )
                request_id = cursor.lastrowid
                cursor.executemany(
                    "INSERT INTO request_items "
                    "(request_id, material_id, material_name_snapshot, unit_snapshot, "
                    "supplier_id, quantity, unit_price) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    [
                        (
                            request_id,
                            material_id,
                            material_name,
                            unit,
                            supplier_id,
                            quantity,
                            price,
                        )
                        for material_id, supplier_id, material_name, unit, quantity, price
                        in item_rows
                    ],
                )
            flash(f"Purchase request #{request_id} submitted for review.", "success")
            return redirect(url_for("purchaser_history"))
        except (ValueError, TypeError) as error:
            notify_validation_error(error)
    with db_cursor() as cursor:
        cursor.execute(
            "SELECT m.material_id, m.material_name, m.unit, m.price, "
            "m.stock_quantity, u.name AS supplier_name, "
            "u.user_id AS supplier_id, u.is_active AS supplier_active "
            "FROM materials m JOIN users u ON u.user_id = m.supplier_id "
            "WHERE u.role = 'Supplier' AND m.is_active = TRUE "
            "ORDER BY m.material_name, m.unit, u.name, m.price"
        )
        offers = cursor.fetchall()
    grouped_offers = {}
    for offer in offers:
        key = (offer["material_name"].strip().casefold(), offer["unit"].strip().casefold())
        group = grouped_offers.setdefault(
            key,
            {
                "material_name": offer["material_name"],
                "unit": offer["unit"],
                "offers": [],
                "available_offer_count": 0,
            },
        )
        group["offers"].append(offer)
        if offer["supplier_active"] and offer["stock_quantity"] > 0:
            group["available_offer_count"] += 1
    offer_groups = sorted(
        grouped_offers.values(),
        key=lambda group: (group["material_name"].casefold(), group["unit"].casefold()),
    )
    return render_template(
        "purchaser_new_request.html",
        materials=offer_groups,
        today=date.today().isoformat(),
    )


@app.route("/purchaser/history")
@role_required("Purchaser")
def purchaser_history():
    with db_cursor() as cursor:
        cursor.execute(
            "SELECT r.request_id, r.request_date, r.required_date, r.status, "
            "COUNT(DISTINCT ri.request_item_id) AS item_count, "
            "COALESCE(SUM(ri.quantity * ri.unit_price), 0) AS request_total, "
            "GROUP_CONCAT(CONCAT(ri.quantity, ' ', ri.unit_snapshot, ' ', "
            "ri.material_name_snapshot, "
            "' · ', supplier.name) ORDER BY ri.material_name_snapshot SEPARATOR ', ') "
            "AS request_items_summary "
            "FROM purchase_requests r "
            "LEFT JOIN request_items ri ON ri.request_id = r.request_id "
            "LEFT JOIN users supplier ON supplier.user_id = ri.supplier_id "
            "WHERE r.purchaser_id = %s "
            "GROUP BY r.request_id, r.request_date, r.required_date, r.status "
            "ORDER BY r.request_date DESC",
            (g.user_id,),
        )
        requests = cursor.fetchall()
        cursor.execute(
            "SELECT o.order_id, o.request_id, o.order_status, o.total_amount, "
            "o.order_date, s.name AS supplier_name, d.delivery_address, "
            "d.dispatch_date, d.delivery_date, d.delivery_status, "
            "GROUP_CONCAT(CONCAT(oi.quantity, ' ', oi.unit_snapshot, ' ', "
            "oi.material_name_snapshot) ORDER BY oi.material_name_snapshot "
            "SEPARATOR ', ') AS items_summary "
            "FROM orders o JOIN purchase_requests r ON r.request_id = o.request_id "
            "JOIN users s ON s.user_id = o.supplier_id "
            "LEFT JOIN deliveries d ON d.order_id = o.order_id "
            "LEFT JOIN order_items oi ON oi.order_id = o.order_id "
            "WHERE r.purchaser_id = %s "
            "GROUP BY o.order_id, o.request_id, o.order_status, o.total_amount, "
            "o.order_date, s.name, d.delivery_address, d.dispatch_date, "
            "d.delivery_date, d.delivery_status ORDER BY o.order_date DESC",
            (g.user_id,),
        )
        orders = cursor.fetchall()
    return render_template("purchaser_history.html", requests=requests, orders=orders)


@app.errorhandler(400)
def bad_request(error):
    return render_template("error.html", code=400, message=error.description), 400


@app.errorhandler(403)
def forbidden(_error):
    return render_template("error.html", code=403, message="You do not have access to this page."), 403


@app.errorhandler(404)
def not_found(_error):
    return render_template("error.html", code=404, message="The page you requested could not be found."), 404


if __name__ == "__main__":
    app.run(
        host=os.getenv("FLASK_HOST", "127.0.0.1"),
        port=int(os.getenv("FLASK_PORT", "5000")),
        debug=os.getenv("FLASK_DEBUG", "0") == "1",
    )
