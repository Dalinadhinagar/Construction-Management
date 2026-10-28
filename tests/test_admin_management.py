"""Live-MySQL regression tests for Admin management workflows."""

import secrets
import unittest
import uuid

import app
from database import db_cursor
from werkzeug.security import generate_password_hash


class AdminManagementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.app.config.update(TESTING=True, SECRET_KEY="admin-management-test")
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT user_id, name FROM users "
                "WHERE role = 'Admin' AND is_active = TRUE ORDER BY user_id LIMIT 1"
            )
            cls.admin = cursor.fetchone()
            if not cls.admin:
                raise RuntimeError("Admin management tests require an active Admin account.")
            cursor.execute(
                "SELECT m.material_id, m.is_active FROM request_items ri "
                "JOIN materials m ON m.material_id = ri.material_id "
                "ORDER BY ri.request_item_id LIMIT 1"
            )
            referenced = cursor.fetchone()
            cls.referenced_material_id = referenced["material_id"] if referenced else None
            cls.referenced_material_active = (
                referenced["is_active"] if referenced else None
            )
            cursor.execute(
                "SELECT user_id FROM users "
                "WHERE role = 'Supplier' AND is_active = TRUE ORDER BY user_id LIMIT 1"
            )
            cls.supplier = cursor.fetchone()
            if not cls.supplier:
                raise RuntimeError("Admin management tests require an active Supplier account.")

    def setUp(self):
        self.client = app.app.test_client()
        self.token = secrets.token_urlsafe(24)
        self.authenticate_as_admin()

    def authenticate_as_admin(self):
        with self.client.session_transaction() as state:
            state["user_id"] = self.admin["user_id"]
            state["name"] = self.admin["name"]
            state["role"] = "Admin"
            state["_csrf_token"] = self.token

    def csrf_form(self, **fields):
        return {"_csrf_token": self.token, **fields}

    def test_admin_pages_render_against_live_schema(self):
        for path in (
            "/admin/users",
            "/admin/users?q=test&role=Supplier&sort=role&direction=asc",
            "/admin/suppliers",
            "/admin/purchasers",
            "/admin/materials",
            "/admin/requests",
            "/admin/requests?purchaser_id=1",
            "/admin/requests?supplier_id=1",
            "/admin/reports",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_supplier_is_forbidden_from_admin_management_pages(self):
        client = app.app.test_client()
        with client.session_transaction() as state:
            state["user_id"] = self.supplier["user_id"]
            state["role"] = "Supplier"
            state["name"] = "Supplier test"
            state["_csrf_token"] = self.token
        for path in (
            "/admin",
            "/admin/users",
            "/admin/suppliers",
            "/admin/purchasers",
            "/admin/materials",
            "/admin/requests",
            "/admin/reports",
        ):
            with self.subTest(path=path):
                self.assertEqual(client.get(path).status_code, 403)

    def test_admin_can_create_edit_and_delete_material(self):
        material_name = f"Admin test listing {uuid.uuid4().hex[:8]}"
        response = self.client.post(
            "/admin/materials",
            data=self.csrf_form(
                supplier_id=str(self.supplier["user_id"]),
                material_name=material_name,
                description="Created by integration test",
                unit="piece",
                price="3.25",
                stock_quantity="14",
            ),
        )
        self.assertEqual(response.status_code, 302)
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT material_id FROM materials WHERE material_name = %s",
                (material_name,),
            )
            material = cursor.fetchone()
        self.assertIsNotNone(material)
        material_id = material["material_id"]
        try:
            response = self.client.post(
                f"/admin/materials/{material_id}/edit",
                data=self.csrf_form(
                    supplier_id=str(self.supplier["user_id"]),
                    material_name=f"{material_name} edited",
                    description="Updated by integration test",
                    unit="ton",
                    price="4.50",
                    stock_quantity="20",
                ),
            )
            self.assertEqual(response.status_code, 302)
            with db_cursor() as cursor:
                cursor.execute(
                    "SELECT material_name, unit, price, stock_quantity "
                    "FROM materials WHERE material_id = %s",
                    (material_id,),
                )
                updated = cursor.fetchone()
            self.assertEqual(updated["material_name"], f"{material_name} edited")
            self.assertEqual(updated["unit"], "ton")
            self.assertEqual(float(updated["price"]), 4.5)
            self.assertEqual(updated["stock_quantity"], 20)
            response = self.client.post(
                f"/admin/materials/{material_id}/delete",
                data=self.csrf_form(),
            )
            self.assertEqual(response.status_code, 302)
            with db_cursor() as cursor:
                cursor.execute(
                    "SELECT material_id FROM materials WHERE material_id = %s",
                    (material_id,),
                )
                self.assertIsNone(cursor.fetchone())
        finally:
            with db_cursor() as cursor:
                cursor.execute("DELETE FROM materials WHERE material_id = %s", (material_id,))

    def test_admin_archives_material_in_purchase_history(self):
        if self.referenced_material_id is None:
            self.skipTest("No purchase history material exists in this database.")
        response = self.client.post(
            f"/admin/materials/{self.referenced_material_id}/delete",
            data=self.csrf_form(),
        )
        self.assertEqual(response.status_code, 302)
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT material_id, is_active FROM materials WHERE material_id = %s",
                (self.referenced_material_id,),
            )
            archived = cursor.fetchone()
            self.assertIsNotNone(archived)
            self.assertFalse(archived["is_active"])
            cursor.execute(
                "UPDATE materials SET is_active = %s WHERE material_id = %s",
                (
                    self.referenced_material_active,
                    self.referenced_material_id,
                ),
            )
        with self.client.session_transaction() as state:
            self.assertTrue(state.get("_flashes"))

    def test_admin_can_edit_user_without_exposing_password_hash(self):
        email = f"admin-test-{uuid.uuid4().hex[:10]}@example.test"
        with db_cursor() as cursor:
            cursor.execute(
                "INSERT INTO users (name, email, password_hash, role, phone) "
                "VALUES (%s, %s, %s, 'Purchaser', %s)",
                ("Temporary Admin Test", email, generate_password_hash("BeforeTest123"), "555-0199"),
            )
            user_id = cursor.lastrowid
        try:
            page = self.client.get("/admin/users")
            self.assertEqual(page.status_code, 200)
            self.assertNotIn(b"password_hash", page.data)
            response = self.client.post(
                f"/admin/users/{user_id}/edit",
                data=self.csrf_form(
                    name="Updated Test Purchaser",
                    email=email,
                    phone="555-0123",
                    role="Purchaser",
                ),
            )
            self.assertEqual(response.status_code, 302)
            with db_cursor() as cursor:
                cursor.execute(
                    "SELECT name, email, role, phone FROM users WHERE user_id = %s",
                    (user_id,),
                )
                updated = cursor.fetchone()
            self.assertEqual(updated["name"], "Updated Test Purchaser")
            self.assertEqual(updated["role"], "Purchaser")
            self.assertEqual(updated["phone"], "555-0123")
        finally:
            with db_cursor() as cursor:
                cursor.execute("DELETE FROM users WHERE user_id = %s", (user_id,))

    def test_admin_cannot_create_a_removed_legacy_role(self):
        email = f"removed-role-{uuid.uuid4().hex[:10]}@example.test"
        response = self.client.post(
            "/admin/users",
            data=self.csrf_form(
                name="Unsupported Role Test",
                email=email,
                phone="",
                password="TemporaryPass123",
                role="User",
            ),
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Choose a valid user role", response.data)
        with db_cursor() as cursor:
            cursor.execute("SELECT user_id FROM users WHERE email = %s", (email,))
            self.assertIsNone(cursor.fetchone())


if __name__ == "__main__":
    unittest.main()
