"""Live-MySQL test for request assignment, supplier fulfillment, and purchaser tracking."""

import secrets
import unittest
import uuid
from datetime import date, timedelta

import app
from database import db_cursor
from werkzeug.security import generate_password_hash


class ProcurementWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.app.config.update(TESTING=True, SECRET_KEY="procurement-workflow-test")
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT user_id FROM users "
                "WHERE role = 'Admin' AND is_active = TRUE ORDER BY user_id LIMIT 1"
            )
            cls.admin_id = cursor.fetchone()["user_id"]

    def test_request_assignment_supplier_fulfillment_and_purchaser_tracking(self):
        suffix = uuid.uuid4().hex[:12]
        supplier_email = f"workflow-supplier-{suffix}@example.test"
        purchaser_email = f"workflow-purchaser-{suffix}@example.test"
        ids = {}
        clients = {}
        tokens = {}

        with db_cursor() as cursor:
            cursor.execute(
                "INSERT INTO users (name, email, password_hash, role) "
                "VALUES (%s, %s, %s, 'Supplier')",
                ("Workflow Supplier", supplier_email, generate_password_hash("Workflow123")),
            )
            ids["supplier"] = cursor.lastrowid
            cursor.execute(
                "INSERT INTO users (name, email, password_hash, role) "
                "VALUES (%s, %s, %s, 'Supplier')",
                (
                    "Workflow Alternate Supplier",
                    f"workflow-alternate-{suffix}@example.test",
                    generate_password_hash("Workflow123"),
                ),
            )
            ids["alternate_supplier"] = cursor.lastrowid
            cursor.execute(
                "INSERT INTO users (name, email, password_hash, role) "
                "VALUES (%s, %s, %s, 'Purchaser')",
                ("Workflow Purchaser", purchaser_email, generate_password_hash("Workflow123")),
            )
            ids["purchaser"] = cursor.lastrowid
            cursor.execute(
                "INSERT INTO materials "
                "(supplier_id, material_name, unit, price, stock_quantity) "
                "VALUES (%s, %s, 'bag', 5.00, 10)",
                (ids["supplier"], f"Workflow Cement {suffix}"),
            )
            ids["material"] = cursor.lastrowid
            cursor.execute(
                "INSERT INTO materials "
                "(supplier_id, material_name, unit, price, stock_quantity) "
                "VALUES (%s, %s, 'bag', 7.25, 20)",
                (ids["alternate_supplier"], f"Workflow Cement {suffix}"),
            )
            ids["alternate_material"] = cursor.lastrowid
            cursor.execute(
                "INSERT INTO purchase_requests (purchaser_id, required_date, status) "
                "VALUES (%s, %s, 'Approved')",
                (ids["purchaser"], date.today() + timedelta(days=10)),
            )
            ids["request"] = cursor.lastrowid
            cursor.execute(
                "INSERT INTO request_items "
                "(request_id, material_id, material_name_snapshot, unit_snapshot, "
                "supplier_id, quantity, unit_price) "
                "VALUES (%s, %s, %s, 'bag', %s, 3, 5.00)",
                (
                    ids["request"],
                    ids["material"],
                    f"Workflow Cement {suffix}",
                    ids["supplier"],
                ),
            )
            ids["request_item"] = cursor.lastrowid

        try:
            for role, user_id in (
                ("Admin", self.admin_id),
                ("Supplier", ids["supplier"]),
                ("Purchaser", ids["purchaser"]),
            ):
                clients[role] = app.app.test_client()
                tokens[role] = secrets.token_urlsafe(24)
                with clients[role].session_transaction() as state:
                    state["user_id"] = user_id
                    state["role"] = role
                    state["name"] = f"Workflow {role}"
                    state["_csrf_token"] = tokens[role]
            clients["Alternate Supplier"] = app.app.test_client()
            tokens["Alternate Supplier"] = secrets.token_urlsafe(24)
            with clients["Alternate Supplier"].session_transaction() as state:
                state["user_id"] = ids["alternate_supplier"]
                state["role"] = "Supplier"
                state["name"] = "Workflow Alternate Supplier"
                state["_csrf_token"] = tokens["Alternate Supplier"]

            offer_page = clients["Purchaser"].get("/purchaser/requests/new")
            self.assertEqual(offer_page.status_code, 200)
            self.assertIn(b"Workflow Supplier", offer_page.data)
            self.assertIn(b"Workflow Alternate Supplier", offer_page.data)
            created_request = clients["Purchaser"].post(
                "/purchaser/requests/new",
                data={
                    "_csrf_token": tokens["Purchaser"],
                    "required_date": (date.today() + timedelta(days=5)).isoformat(),
                    "material_id": str(ids["alternate_material"]),
                    "quantity": "2",
                    "price": "0.01",
                    "total_amount": "0.02",
                },
            )
            self.assertEqual(created_request.status_code, 302)
            with db_cursor() as cursor:
                cursor.execute(
                    "SELECT request_id FROM purchase_requests "
                    "WHERE purchaser_id = %s ORDER BY request_id DESC LIMIT 1",
                    (ids["purchaser"],),
                )
                ids["created_request"] = cursor.fetchone()["request_id"]
                cursor.execute(
                    "SELECT supplier_id, material_id, quantity, unit_price "
                    "FROM request_items WHERE request_id = %s",
                    (ids["created_request"],),
                )
                selected_line = cursor.fetchone()
            self.assertEqual(selected_line["supplier_id"], ids["alternate_supplier"])
            self.assertEqual(selected_line["material_id"], ids["alternate_material"])
            self.assertEqual(selected_line["quantity"], 2)
            self.assertEqual(float(selected_line["unit_price"]), 7.25)

            assigned = clients["Admin"].post(
                f"/admin/requests/{ids['request']}/assign",
                data={
                    "_csrf_token": tokens["Admin"],
                    "supplier_id": str(ids["supplier"]),
                    "request_item_id": str(ids["request_item"]),
                    "delivery_address": "42 Test Avenue, Site A",
                },
            )
            self.assertEqual(assigned.status_code, 302)
            with db_cursor() as cursor:
                cursor.execute(
                    "SELECT order_id, total_amount, order_status FROM orders "
                    "WHERE request_id = %s AND supplier_id = %s",
                    (ids["request"], ids["supplier"]),
                )
                order = cursor.fetchone()
            self.assertIsNotNone(order)
            ids["order"] = order["order_id"]
            self.assertEqual(float(order["total_amount"]), 15.0)
            self.assertEqual(order["order_status"], "Confirmed")

            reassigned = clients["Admin"].post(
                f"/admin/orders/{order['order_id']}/reassign",
                data={
                    "_csrf_token": tokens["Admin"],
                    "supplier_id": ids["alternate_supplier"],
                },
            )
            self.assertEqual(reassigned.status_code, 302)
            with db_cursor() as cursor:
                cursor.execute(
                    "SELECT supplier_id, total_amount, order_status FROM orders "
                    "WHERE order_id = %s",
                    (order["order_id"],),
                )
                reassigned_order = cursor.fetchone()
                cursor.execute(
                    "SELECT material_id, material_name_snapshot, unit_snapshot, "
                    "quantity, unit_price FROM order_items WHERE order_id = %s",
                    (order["order_id"],),
                )
                line_snapshot = cursor.fetchone()
                cursor.execute(
                    "SELECT COUNT(*) AS history_count FROM order_assignment_history "
                    "WHERE order_id = %s",
                    (order["order_id"],),
                )
                self.assertEqual(cursor.fetchone()["history_count"], 1)
            self.assertEqual(reassigned_order["supplier_id"], ids["alternate_supplier"])
            self.assertEqual(float(reassigned_order["total_amount"]), 21.75)
            self.assertEqual(line_snapshot["material_id"], ids["alternate_material"])
            self.assertEqual(line_snapshot["material_name_snapshot"], f"Workflow Cement {suffix}")
            self.assertEqual(float(line_snapshot["unit_price"]), 7.25)

            accepted = clients["Supplier"].post(
                f"/supplier/orders/{ids['order']}/decision",
                data={
                    "_csrf_token": tokens["Supplier"],
                    "decision": "accept",
                },
            )
            self.assertEqual(accepted.status_code, 302)
            with clients["Supplier"].session_transaction() as state:
                self.assertTrue(state.get("_flashes"))
            accepted = clients["Alternate Supplier"].post(
                f"/supplier/orders/{ids['order']}/decision",
                data={
                    "_csrf_token": tokens["Alternate Supplier"],
                    "decision": "accept",
                },
            )
            self.assertEqual(accepted.status_code, 302)
            with db_cursor() as cursor:
                cursor.execute(
                    "SELECT stock_quantity FROM materials WHERE material_id = %s",
                    (ids["alternate_material"],),
                )
                self.assertEqual(cursor.fetchone()["stock_quantity"], 17)
                cursor.execute(
                    "SELECT order_status FROM orders WHERE order_id = %s",
                    (ids["order"],),
                )
                self.assertEqual(cursor.fetchone()["order_status"], "Processing")

            for status, expected_delivery in (
                ("Dispatched", "In Transit"),
                ("Delivered", "Delivered"),
            ):
                response = clients["Alternate Supplier"].post(
                    f"/supplier/orders/{ids['order']}/status",
                    data={
                        "_csrf_token": tokens["Alternate Supplier"],
                        "order_status": status,
                    },
                )
                self.assertEqual(response.status_code, 302)
                with clients["Alternate Supplier"].session_transaction() as session_state:
                    flashes = list(session_state.get("_flashes", []))
                with db_cursor() as cursor:
                    cursor.execute(
                        "SELECT o.order_status, d.delivery_status FROM orders o "
                        "JOIN deliveries d ON d.order_id = o.order_id WHERE o.order_id = %s",
                        (ids["order"],),
                    )
                    state = cursor.fetchone()
                self.assertEqual(state["order_status"], status, f"Flashes: {flashes!r}")
                self.assertEqual(state["delivery_status"], expected_delivery)

            history = clients["Purchaser"].get("/purchaser/history")
            self.assertEqual(history.status_code, 200)
            self.assertIn(f"#{ids['order']}".encode(), history.data)
        finally:
            with db_cursor() as cursor:
                if ids.get("order"):
                    cursor.execute(
                        "DELETE FROM order_assignment_history_items WHERE "
                        "assignment_history_id IN (SELECT assignment_history_id "
                        "FROM order_assignment_history WHERE order_id = %s)",
                        (ids["order"],),
                    )
                    cursor.execute(
                        "DELETE FROM order_assignment_history WHERE order_id = %s",
                        (ids["order"],),
                    )
                    cursor.execute("DELETE FROM deliveries WHERE order_id = %s", (ids["order"],))
                    cursor.execute("DELETE FROM orders WHERE order_id = %s", (ids["order"],))
                if ids.get("request"):
                    cursor.execute(
                        "DELETE FROM request_items WHERE request_id = %s",
                        (ids["request"],),
                    )
                    cursor.execute(
                        "DELETE FROM purchase_requests WHERE request_id = %s",
                        (ids["request"],),
                    )
                if ids.get("created_request"):
                    cursor.execute(
                        "DELETE FROM request_items WHERE request_id = %s",
                        (ids["created_request"],),
                    )
                    cursor.execute(
                        "DELETE FROM purchase_requests WHERE request_id = %s",
                        (ids["created_request"],),
                    )
                for key in ("material", "alternate_material"):
                    if ids.get(key):
                        cursor.execute("DELETE FROM materials WHERE material_id = %s", (ids[key],))
                for key in ("supplier", "alternate_supplier", "purchaser"):
                    if ids.get(key):
                        cursor.execute("DELETE FROM users WHERE user_id = %s", (ids[key],))


if __name__ == "__main__":
    unittest.main()
