"""Live-MySQL tests for supplier-owned material edit and delete actions."""

import secrets
import unittest
import uuid

import app
from database import db_cursor


class SupplierMaterialTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.app.config.update(TESTING=True, SECRET_KEY="supplier-material-test")
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT user_id FROM users WHERE role = 'Supplier' AND is_active = TRUE "
                "ORDER BY user_id LIMIT 1"
            )
            supplier = cursor.fetchone()
            if not supplier:
                raise RuntimeError("Supplier material tests need an active Supplier account.")
            cls.supplier_id = supplier["user_id"]
            cursor.execute(
                "SELECT m.material_id, m.supplier_id, m.is_active FROM materials m "
                "JOIN request_items ri ON ri.material_id = m.material_id LIMIT 1"
            )
            cls.referenced_material = cursor.fetchone()

    def setUp(self):
        self.client = app.app.test_client()
        self.material_name = f"Temporary test material {uuid.uuid4().hex[:8]}"
        with db_cursor() as cursor:
            cursor.execute(
                "INSERT INTO materials "
                "(supplier_id, material_name, description, unit, price, stock_quantity) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    self.supplier_id,
                    self.material_name,
                    "Temporary integration test material",
                    "piece",
                    5.25,
                    17,
                ),
            )
            self.material_id = cursor.lastrowid
        self.authenticate_as(self.supplier_id)

    def tearDown(self):
        if getattr(self, "material_id", None):
            with db_cursor() as cursor:
                cursor.execute(
                    "DELETE FROM materials WHERE material_id = %s",
                    (self.material_id,),
                )

    def authenticate_as(self, user_id):
        with self.client.session_transaction() as state:
            state["user_id"] = user_id
            state["role"] = "Supplier"
            state["name"] = "Supplier Test"
            state["_csrf_token"] = secrets.token_urlsafe(32)

    def csrf_token(self):
        with self.client.session_transaction() as state:
            return state["_csrf_token"]

    def test_supplier_can_edit_owned_material(self):
        page = self.client.get(f"/supplier/materials/{self.material_id}/edit")
        self.assertEqual(page.status_code, 200)
        self.assertIn(self.material_name.encode(), page.data)

        response = self.client.post(
            f"/supplier/materials/{self.material_id}/edit",
            data={
                "_csrf_token": self.csrf_token(),
                "material_name": "Updated test aggregate",
                "description": "Updated through edit route",
                "unit": "ton",
                "price": "9.50",
                "stock_quantity": "23",
            },
        )
        self.assertEqual(response.status_code, 302)
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT material_name, description, unit, price, stock_quantity "
                "FROM materials WHERE material_id = %s AND supplier_id = %s",
                (self.material_id, self.supplier_id),
            )
            updated = cursor.fetchone()
        self.assertEqual(updated["material_name"], "Updated test aggregate")
        self.assertEqual(updated["description"], "Updated through edit route")
        self.assertEqual(updated["unit"], "ton")
        self.assertEqual(float(updated["price"]), 9.5)
        self.assertEqual(updated["stock_quantity"], 23)

    def test_supplier_can_delete_unreferenced_owned_material(self):
        response = self.client.post(
            f"/supplier/materials/{self.material_id}/delete",
            data={"_csrf_token": self.csrf_token()},
        )
        self.assertEqual(response.status_code, 302)
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT material_id FROM materials WHERE material_id = %s",
                (self.material_id,),
            )
            self.assertIsNone(cursor.fetchone())
        self.material_id = None

    def test_delete_archives_material_in_purchase_history(self):
        if not self.referenced_material:
            self.skipTest("No purchase-requested material exists in this database.")
        if self.referenced_material["supplier_id"] != self.supplier_id:
            self.authenticate_as(self.referenced_material["supplier_id"])
        response = self.client.post(
            f"/supplier/materials/{self.referenced_material['material_id']}/delete",
            data={"_csrf_token": self.csrf_token()},
        )
        self.assertEqual(response.status_code, 302)
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT material_id, is_active FROM materials WHERE material_id = %s",
                (self.referenced_material["material_id"],),
            )
            archived = cursor.fetchone()
            self.assertIsNotNone(archived)
            self.assertFalse(archived["is_active"])
        restored = self.client.post(
            f"/supplier/materials/{self.referenced_material['material_id']}/restore",
            data={"_csrf_token": self.csrf_token()},
        )
        self.assertEqual(restored.status_code, 302)
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT is_active FROM materials WHERE material_id = %s",
                (self.referenced_material["material_id"],),
            )
            self.assertTrue(cursor.fetchone()["is_active"])
            cursor.execute(
                "UPDATE materials SET is_active = %s WHERE material_id = %s",
                (
                    self.referenced_material["is_active"],
                    self.referenced_material["material_id"],
                ),
            )
        with self.client.session_transaction() as state:
            self.assertTrue(state.get("_flashes"))

    def test_supplier_cannot_edit_another_suppliers_material(self):
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT user_id FROM users WHERE role = 'Supplier' AND user_id <> %s "
                "ORDER BY user_id LIMIT 1",
                (self.supplier_id,),
            )
            other_supplier = cursor.fetchone()
        if not other_supplier:
            self.skipTest("A second supplier account is needed for the ownership test.")
        self.authenticate_as(other_supplier["user_id"])
        response = self.client.post(
            f"/supplier/materials/{self.material_id}/edit",
            data={
                "_csrf_token": self.csrf_token(),
                "material_name": "Unauthorized edit",
                "description": "",
                "unit": "piece",
                "price": "1.00",
                "stock_quantity": "1",
            },
        )
        self.assertEqual(response.status_code, 302)
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT material_name, supplier_id FROM materials WHERE material_id = %s",
                (self.material_id,),
            )
            unchanged = cursor.fetchone()
        self.assertEqual(unchanged["material_name"], self.material_name)
        self.assertEqual(unchanged["supplier_id"], self.supplier_id)


if __name__ == "__main__":
    unittest.main()
