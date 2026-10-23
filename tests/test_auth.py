import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient

from backend import auth
from backend.project_shutdown import router as project_router


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = auth.AuthStore(Path(self.temp.name) / "auth.db")
        self.patch = patch.object(auth, "store", self.store)
        self.patch.start()
        app = FastAPI()
        app.add_middleware(auth.AuthMiddleware)
        app.include_router(auth.router)

        app.include_router(project_router)

        @app.get("/api/private")
        def private():
            return {"ok": True}

        @app.websocket("/api/private/ws")
        async def socket(ws: WebSocket):
            await ws.accept()
            await ws.send_text("ok")
            await ws.close()

        self.client = TestClient(app)
        self.credentials = {"username": "admin", "password": "test-password-123", "remember": True}

    def tearDown(self):
        self.client.close()
        self.patch.stop()
        self.temp.cleanup()

    def test_setup_login_logout_and_cookie(self):
        self.assertEqual(self.client.get("/api/private").status_code, 401)
        response = self.client.post("/api/auth/setup", json=self.credentials)
        self.assertEqual(response.status_code, 200)
        cookie = response.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=strict", cookie)
        self.assertIn("max-age=2592000", cookie)
        self.assertEqual(self.client.get("/api/private").status_code, 200)
        token = self.client.cookies.get(auth.COOKIE)
        self.assertEqual(self.client.post("/api/auth/setup", json=self.credentials).status_code, 409)
        self.assertEqual(self.client.post("/api/auth/logout").status_code, 200)
        self.assertFalse(self.store.valid(token))
        self.assertEqual(self.client.get("/api/private").status_code, 401)
        self.assertEqual(self.client.post("/api/auth/login", json=self.credentials).status_code, 200)

    def test_launcher_health_check_does_not_require_login(self):
        response = self.client.get("/api/system/project-status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"running": True})
        self.assertEqual(self.client.get("/api/private").status_code, 401)
        self.assertEqual(self.client.post("/api/system/close-project").status_code, 401)

    def test_cross_origin_setup_and_authenticated_mutation_rejected(self):
        headers = {"Origin": "https://evil.example"}
        self.assertEqual(self.client.post("/api/auth/setup", json=self.credentials, headers=headers).status_code, 403)
        self.client.post("/api/auth/setup", json=self.credentials)
        self.assertEqual(self.client.post("/api/auth/logout", headers=headers).status_code, 403)
        self.assertTrue(self.client.get("/api/auth/status").json()["authenticated"])

    def test_bad_password_throttling(self):
        self.client.post("/api/auth/setup", json=self.credentials)
        bad = {**self.credentials, "password": "wrong-password-123"}
        for _ in range(5):
            self.assertEqual(self.client.post("/api/auth/login", json=bad).status_code, 401)
        self.assertEqual(self.client.post("/api/auth/login", json=self.credentials).status_code, 429)

    def test_expiry_and_persistence(self):
        self.store.setup(auth.Credentials(**self.credentials))
        token, _ = self.store.issue(False)
        self.assertTrue(auth.AuthStore(self.store.path).valid(token))
        with patch("backend.auth.time.time", return_value=999999999999):
            self.assertFalse(self.store.valid(token))

    def test_websocket_requires_session(self):
        from starlette.websockets import WebSocketDisconnect
        with self.assertRaises(WebSocketDisconnect) as caught:
            with self.client.websocket_connect("/api/private/ws"):
                pass
        self.assertEqual(caught.exception.code, 4401)
        self.client.post("/api/auth/setup", json=self.credentials)
        with self.client.websocket_connect("/api/private/ws", headers={"Origin": "http://testserver"}) as ws:
            self.assertEqual(ws.receive_text(), "ok")

    def test_weak_password_and_remote_setup_rejected(self):
        self.assertEqual(self.client.post("/api/auth/setup", json={**self.credentials, "password": "short"}).status_code, 422)
        remote = TestClient(self.client.app, client=("192.0.2.1", 5000))
        self.assertEqual(remote.post("/api/auth/setup", json=self.credentials).status_code, 403)
        remote.close()

    def test_six_character_password_can_setup_and_login(self):
        credentials = {**self.credentials, "password": "abc123"}
        self.assertEqual(self.client.post("/api/auth/setup", json=credentials).status_code, 200)
        self.client.post("/api/auth/logout")
        self.assertEqual(self.client.post("/api/auth/login", json=credentials).status_code, 200)


if __name__ == "__main__":
    unittest.main()
