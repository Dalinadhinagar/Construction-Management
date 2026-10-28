"""Live-MySQL integration tests for Stage 2 authentication."""

import re
import unittest
import uuid

from werkzeug.security import generate_password_hash

import app
from database import db_cursor


PASSWORD = "Construction@123"
ROLE_LOGINS = {
    "Admin": ("/login/admin", "admin@construction.local", "/admin"),
    "Purchaser": ("/login/purchaser", "purchaser1@construction.local", "/purchaser"),
    "Supplier": ("/login/supplier", "supplier1@construction.local", "/supplier"),
}


class AuthenticationIntegrationTests(unittest.TestCase):
    """Exercise all login pages and session permissions against configured MySQL."""

    @classmethod
    def setUpClass(cls):
        app.app.config.update(TESTING=True, SECRET_KEY="authentication-test-secret")

    def setUp(self):
        self.client = app.app.test_client()
        self.temporary_email = f"auth-test-{uuid.uuid4().hex}@example.test"
        self.temporary_user_id = None

    def tearDown(self):
        if self.temporary_user_id:
            with db_cursor() as cursor:
                cursor.execute(
                    "DELETE FROM users WHERE user_id = %s",
                    (self.temporary_user_id,),
                )

    def csrf_token(self, client, path):
        response = client.get(path)
        self.assertEqual(response.status_code, 200)
        match = re.search(
            rb'name="_csrf_token" value="([^"]+)"',
            response.data,
        )
        self.assertIsNotNone(match)
        return match.group(1).decode()

    def sign_in(self, client, role, email=None, password=PASSWORD):
        path, default_email, _dashboard = ROLE_LOGINS[role]
        response = client.post(
            path,
            data={
                "_csrf_token": self.csrf_token(client, path),
                "email": email or default_email,
                "password": password,
            },
            follow_redirects=False,
        )
        return response

    def create_temporary_account(self, role="Supplier", password=PASSWORD):
        with db_cursor() as cursor:
            cursor.execute(
                "INSERT INTO users (name, email, password_hash, role, is_active) "
                "VALUES (%s, %s, %s, %s, TRUE)",
                (
                    "Authentication Test Account",
                    self.temporary_email,
                    generate_password_hash(password),
                    role,
                ),
            )
            self.temporary_user_id = cursor.lastrowid

    def test_each_role_can_use_its_own_login_page(self):
        for role, (path, email, dashboard) in ROLE_LOGINS.items():
            with self.subTest(role=role):
                client = app.app.test_client()
                response = self.sign_in(client, role, email)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.headers["Location"].endswith(dashboard))
                self.assertEqual(client.get(dashboard).status_code, 200)

    def test_login_pages_reject_credentials_for_other_roles(self):
        wrong_account_for_page = {
            "Admin": "supplier1@construction.local",
            "Purchaser": "admin@construction.local",
            "Supplier": "purchaser1@construction.local",
        }
        for role, (path, _email, _dashboard) in ROLE_LOGINS.items():
            with self.subTest(role=role):
                client = app.app.test_client()
                response = client.post(
                    path,
                    data={
                        "_csrf_token": self.csrf_token(client, path),
                        "email": wrong_account_for_page[role],
                        "password": PASSWORD,
                    },
                )
                self.assertEqual(response.status_code, 200)
                self.assertIn(b"accounts only", response.data)
                with client.session_transaction() as state:
                    self.assertNotIn("user_id", state)

    def test_login_attempts_are_rate_limited(self):
        client = app.app.test_client()
        client.environ_base["REMOTE_ADDR"] = "203.0.113.55"
        for _attempt in range(app.LOGIN_ATTEMPT_LIMIT):
            response = client.post(
                "/login/admin",
                data={
                    "_csrf_token": self.csrf_token(client, "/login/admin"),
                    "email": "admin@construction.local",
                    "password": "incorrect-password",
                },
            )
            self.assertEqual(response.status_code, 200)
        blocked = client.post(
            "/login/admin",
            data={
                "_csrf_token": self.csrf_token(client, "/login/admin"),
                "email": "admin@construction.local",
                "password": PASSWORD,
            },
        )
        self.assertEqual(blocked.status_code, 200)
        self.assertIn(b"Too many sign-in attempts", blocked.data)

    def test_unauthenticated_redirect_and_cross_role_forbidden(self):
        response = self.client.get("/admin")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/login"))

        self.sign_in(self.client, "Supplier")
        forbidden = self.client.get("/admin")
        self.assertEqual(forbidden.status_code, 403)

    def test_logout_clears_session(self):
        self.sign_in(self.client, "Admin")
        response = self.client.post(
            "/logout",
            data={"_csrf_token": self.csrf_token(self.client, "/admin")},
        )
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as state:
            self.assertNotIn("user_id", state)
        self.assertEqual(self.client.get("/admin").status_code, 302)

    def test_admin_can_create_supplier_and_change_password(self):
        self.sign_in(self.client, "Admin")
        response = self.client.post(
            "/admin/users",
            data={
                "_csrf_token": self.csrf_token(self.client, "/admin/users"),
                "name": "Authentication Test Account",
                "email": self.temporary_email,
                "password": PASSWORD,
                "role": "Supplier",
            },
        )
        self.assertEqual(response.status_code, 302)
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT user_id FROM users WHERE email = %s",
                (self.temporary_email,),
            )
            account = cursor.fetchone()
        self.assertIsNotNone(account)
        self.temporary_user_id = account["user_id"]

        client = app.app.test_client()
        login = self.sign_in(client, "Supplier", self.temporary_email)
        self.assertEqual(login.status_code, 302)
        response = client.post(
            "/account/password",
            data={
                "_csrf_token": self.csrf_token(client, "/account/password"),
                "current_password": "wrong-current-password",
                "new_password": "NewConstruction@456",
                "confirm_password": "NewConstruction@456",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"current password is incorrect", response.data)
        response = client.post(
            "/account/password",
            data={
                "_csrf_token": self.csrf_token(client, "/account/password"),
                "current_password": PASSWORD,
                "new_password": "NewConstruction@456",
                "confirm_password": "NewConstruction@456",
            },
        )
        self.assertEqual(response.status_code, 302)
        with client.session_transaction() as state:
            self.assertNotIn("user_id", state)
        self.assertEqual(
            self.sign_in(client, "Supplier", self.temporary_email, PASSWORD).status_code,
            200,
        )
        self.assertEqual(
            self.sign_in(client, "Supplier", self.temporary_email, "NewConstruction@456").status_code,
            302,
        )

    def test_disabled_accounts_cannot_sign_in(self):
        self.create_temporary_account()
        client = app.app.test_client()
        self.assertEqual(
            self.sign_in(client, "Supplier", self.temporary_email).status_code,
            302,
        )
        with db_cursor() as cursor:
            cursor.execute(
                "UPDATE users SET is_active = FALSE WHERE user_id = %s",
                (self.temporary_user_id,),
            )
        revoked = client.get("/supplier")
        self.assertEqual(revoked.status_code, 302)
        with client.session_transaction() as state:
            self.assertNotIn("user_id", state)
        disabled_client = app.app.test_client()
        response = self.sign_in(disabled_client, "Supplier", self.temporary_email)
        self.assertEqual(response.status_code, 200)
        with disabled_client.session_transaction() as state:
            self.assertNotIn("user_id", state)

    def test_homepage_links_only_to_supported_role_login_pages(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        for path in ("/login/admin", "/login/purchaser", "/login/supplier"):
            with self.subTest(path=path):
                self.assertIn(path.encode(), response.data)
        self.assertNotIn(b"/login/user", response.data)

    def test_legacy_user_accounts_are_preserved_but_cannot_sign_in(self):
        response = self.client.get("/login/user")
        self.assertEqual(response.status_code, 404)
        self.create_temporary_account(role="User")
        response = self.client.post(
            "/login",
            data={
                "_csrf_token": self.csrf_token(self.client, "/login"),
                "email": self.temporary_email,
                "password": PASSWORD,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"role is no longer available", response.data)
        with self.client.session_transaction() as state:
            self.assertNotIn("user_id", state)
        self.assertEqual(self.client.get("/user").status_code, 404)

    def test_sessions_expire_and_are_http_only(self):
        self.sign_in(self.client, "Admin")
        cookie = self.client.get_cookie(app.app.config["SESSION_COOKIE_NAME"])
        self.assertTrue(cookie.http_only)
        self.assertEqual(cookie.same_site.lower(), "lax")
        self.assertEqual(
            int(app.app.permanent_session_lifetime.total_seconds()),
            30 * 60,
        )


if __name__ == "__main__":
    unittest.main()
