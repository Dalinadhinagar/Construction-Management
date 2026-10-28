"""Fast route and role-login regression tests without a live MySQL dependency."""

import re
import unittest
from unittest.mock import MagicMock, patch

from werkzeug.security import generate_password_hash

import app


PASSWORD = "RouteTest@123"
ROLE_LOGINS = {
    "Admin": ("/login/admin", "admin@example.test", "/admin"),
    "Purchaser": ("/login/purchaser", "purchaser@example.test", "/purchaser"),
    "Supplier": ("/login/supplier", "supplier@example.test", "/supplier"),
}
PASSWORD_HASH = generate_password_hash(PASSWORD)


class LoginRouteTests(unittest.TestCase):
    def setUp(self):
        app.app.config.update(TESTING=True, SECRET_KEY="route-test-secret")
        self.client = app.app.test_client()

    def token_for(self, client, path):
        response = client.get(path)
        self.assertEqual(response.status_code, 200)
        match = re.search(rb'name="_csrf_token" value="([^"]+)"', response.data)
        self.assertIsNotNone(match)
        return match.group(1).decode()

    def mock_user_lookup(self, role, email):
        cursor = MagicMock()
        cursor.fetchone.return_value = {
            "user_id": 25,
            "name": "Route Test User",
            "email": email,
            "password_hash": PASSWORD_HASH,
            "role": role,
            "is_active": True,
        }
        db_context = MagicMock()
        db_context.__enter__.return_value = cursor
        db_context.__exit__.return_value = False
        return db_context

    def test_role_login_pages_are_registered_and_render_the_right_role(self):
        registered_routes = {
            rule.rule: rule for rule in app.app.url_map.iter_rules()
        }
        for role, (path, _email, _dashboard) in ROLE_LOGINS.items():
            with self.subTest(role=role):
                self.assertIn(path, registered_routes)
                self.assertTrue({"GET", "POST"} <= registered_routes[path].methods)
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                expected_label = f"{role} sign in"
                self.assertIn(expected_label.encode(), response.data)

    def test_successful_login_redirects_to_authenticated_roles_dashboard(self):
        for role, (path, email, dashboard) in ROLE_LOGINS.items():
            with self.subTest(role=role):
                client = app.app.test_client()
                with patch(
                    "app.db_cursor",
                    return_value=self.mock_user_lookup(role, email),
                ):
                    response = client.post(
                        path,
                        data={
                            "_csrf_token": self.token_for(client, path),
                            "email": email,
                            "password": PASSWORD,
                        },
                    )
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.headers["Location"].endswith(dashboard))
                with client.session_transaction() as state:
                    self.assertEqual(state["role"], role)

    def test_role_login_rejects_an_account_for_a_different_role(self):
        client = app.app.test_client()
        path, email, _dashboard = ROLE_LOGINS["Admin"]
        with patch(
            "app.db_cursor",
            return_value=self.mock_user_lookup("Supplier", "supplier@example.test"),
        ):
            response = client.post(
                path,
                data={
                    "_csrf_token": self.token_for(client, path),
                    "email": "supplier@example.test",
                    "password": PASSWORD,
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"This sign-in page is for Admin accounts only", response.data)
        with client.session_transaction() as state:
            self.assertNotIn("user_id", state)

    def test_general_login_route_is_preserved(self):
        response = self.client.get("/login")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Sign in to your workspace", response.data)

    def test_site_user_login_and_dashboard_routes_are_removed(self):
        routes = {rule.rule for rule in app.app.url_map.iter_rules()}
        self.assertNotIn("/login/user", routes)
        self.assertNotIn("/user", routes)
        response = self.client.get("/login")
        self.assertNotIn(b"/login/user", response.data)

    def test_existing_legacy_role_session_is_cleared_without_redirect_loop(self):
        with self.client.session_transaction() as state:
            state["user_id"] = 901
            state["role"] = "User"
            state["name"] = "Legacy account"
        response = self.client.get("/", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/login"))
        with self.client.session_transaction() as state:
            self.assertNotIn("user_id", state)

    def test_unauthenticated_dashboard_access_redirects_to_login(self):
        response = self.client.get("/admin")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/login"))


if __name__ == "__main__":
    unittest.main()
