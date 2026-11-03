import tempfile
import io
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient

from backend import auth
from backend import internal_auth
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

        @app.post("/api/private/mutate")
        def mutate():
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

    def test_internal_token_bypasses_login_only_from_loopback(self):
        # 2026-10-23 实测回归：批量流水线自调用没有 Cookie，被 401 成「请先登录」。
        header = {internal_auth.HEADER: internal_auth.TOKEN}
        self.assertEqual(self.client.get("/api/private").status_code, 401)
        self.assertEqual(self.client.get("/api/private", headers=header).status_code, 200)
        self.assertEqual(self.client.post("/api/private/mutate", headers=header).status_code, 200)
        self.assertEqual(self.client.get("/api/private", headers={internal_auth.HEADER: "wrong"}).status_code, 401)
        self.assertEqual(self.client.get("/api/private", headers={internal_auth.HEADER: ""}).status_code, 401)
        remote = TestClient(self.client.app, client=("192.0.2.1", 5000))
        self.assertEqual(remote.get("/api/private", headers=header).status_code, 401)
        remote.close()

    def test_internal_token_is_shared_with_local_scripts(self):
        self.assertGreaterEqual(len(internal_auth.TOKEN), 32)
        self.assertTrue(Path(internal_auth.TOKEN_PATH).exists())
        self.assertEqual(Path(internal_auth.TOKEN_PATH).read_text(encoding="utf-8").strip(), internal_auth.TOKEN)
        self.assertEqual(internal_auth.headers(), {internal_auth.HEADER: internal_auth.TOKEN})

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

    def test_profile_is_private_and_persists_after_new_store(self):
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)
        self.client.post("/api/auth/setup", json=self.credentials)
        response = self.client.patch("/api/auth/me", json={"username": "creator", "displayName": "创作人", "bio": "唱歌与舞蹈"})
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["displayName"], "创作人")
        self.assertNotIn("digest", result)
        self.assertNotIn("salt", result)
        self.assertEqual(auth.AuthStore(self.store.path).profile()["bio"], "唱歌与舞蹈")
        self.client.post("/api/auth/logout")
        self.assertEqual(self.client.post("/api/auth/login", json=self.credentials).status_code, 401)
        self.assertEqual(self.client.post("/api/auth/login", json={**self.credentials, "username": "creator"}).status_code, 200)

    def test_password_rotation_requires_current_and_revokes_previous_sessions(self):
        self.client.post("/api/auth/setup", json=self.credentials)
        old_token = self.client.cookies.get(auth.COOKIE)
        other_token, _ = self.store.issue(True)
        bad = self.client.post("/api/auth/password", json={"currentPassword": "wrong-password", "newPassword": "new-secret"})
        self.assertEqual(bad.status_code, 401)
        self.assertTrue(self.store.valid(old_token))
        response = self.client.post("/api/auth/password", json={"currentPassword": self.credentials["password"], "newPassword": "new-secret"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.store.valid(old_token))
        self.assertFalse(self.store.valid(other_token))
        self.assertTrue(self.client.get("/api/auth/status").json()["authenticated"])
        self.assertEqual(self.client.get("/api/auth/me").json()["sessionCount"], 1)
        self.assertEqual(self.client.post("/api/auth/login", json={**self.credentials, "password": "new-secret"}).status_code, 200)

    def test_avatar_normalization_removal_and_invalid_upload(self):
        from PIL import Image
        self.client.post("/api/auth/setup", json=self.credentials)
        image = io.BytesIO()
        Image.new("RGB", (12, 8), "purple").save(image, "JPEG")
        response = self.client.post("/api/auth/avatar", files={"file": ("avatar.jpg", image.getvalue(), "image/jpeg")})
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.json()["avatarUrl"])
        avatar = self.client.get("/api/auth/avatar")
        self.assertEqual(avatar.headers["content-type"], "image/png")
        self.assertEqual(Image.open(io.BytesIO(avatar.content)).size, (256, 256))
        self.assertEqual(self.client.post("/api/auth/avatar", files={"file": ("bad.svg", b"<svg/>", "image/svg+xml")}).status_code, 422)
        self.assertEqual(self.client.post("/api/auth/avatar", files={"file": ("big.png", b"0" * (2 * 1024 * 1024 + 1), "image/png")}).status_code, 413)
        self.assertIsNone(self.client.delete("/api/auth/avatar").json()["avatarUrl"])
        self.assertEqual(self.client.get("/api/auth/avatar").status_code, 404)

    def test_logout_all_invalidates_every_session(self):
        self.client.post("/api/auth/setup", json=self.credentials)
        other, _ = self.store.issue(True)
        self.assertEqual(self.client.post("/api/auth/logout-all").status_code, 200)
        self.assertFalse(self.store.valid(other))
        self.assertFalse(self.client.get("/api/auth/status").json()["authenticated"])

    def test_existing_account_schema_migrates_without_changing_credentials(self):
        salt = "ab" * 16
        digest = self.store.digest(self.credentials["password"], salt)
        connection = sqlite3.connect(self.store.path)
        connection.execute("CREATE TABLE owner (id INTEGER PRIMARY KEY, username TEXT NOT NULL, salt TEXT NOT NULL, digest TEXT NOT NULL)")
        connection.execute("INSERT INTO owner VALUES (1, ?, ?, ?)", ("admin", salt, digest))
        connection.commit()
        connection.close()
        result = self.store.profile()
        self.assertEqual(result["username"], "admin")
        self.assertIsNone(result["avatarUrl"])
        self.assertEqual(self.client.post("/api/auth/login", json=self.credentials).status_code, 200)


class InternalCallHeaderTests(unittest.IsolatedAsyncioTestCase):
    """批量流水线的自调用必须带上内部令牌，否则加登录后会 401（2026-10-23 事故）。"""

    class FakeResponse:
        status_code = 200

    def _fake_client(self, captured):
        test = self

        class FakeClient:
            def __init__(self, **kwargs):
                captured["headers"] = kwargs.get("headers")

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info):
                return False

            async def get(self, *args, **kwargs):
                return test.FakeResponse()

            async def post(self, *args, **kwargs):
                return test.FakeResponse()

        return FakeClient

    async def test_batch_worker_cancel_sends_internal_header(self):
        from backend import batch_worker

        captured = {}
        with patch.object(batch_worker.httpx, "AsyncClient", self._fake_client(captured)):
            await batch_worker._cancel_child_job_quietly("child", "batch", "item")
        self.assertEqual(captured["headers"].get(internal_auth.HEADER), internal_auth.TOKEN)

    async def test_image_workbench_wake_batch_sends_internal_header(self):
        from backend import image_workbench

        captured = {}
        with patch.object(image_workbench.httpx, "AsyncClient", self._fake_client(captured)):
            await image_workbench._post_batch_start("batch-id")
        self.assertEqual(captured["headers"].get(internal_auth.HEADER), internal_auth.TOKEN)

    def test_image_upload_sends_internal_header(self):
        from backend import chatgpt_image

        captured = {}
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
            handle.write(b"png")
            path = handle.name
        try:
            with patch("httpx.post", lambda url, **kwargs: captured.update(kwargs) or self.FakeResponse()):
                chatgpt_image._upload_item_image("batch", "item", path)
        finally:
            Path(path).unlink(missing_ok=True)
        self.assertEqual(captured["headers"].get(internal_auth.HEADER), internal_auth.TOKEN)

    def test_automation_client_sends_internal_header_for_loopback(self):
        from automation import h3_client

        local = h3_client.H3Client("http://127.0.0.1:15111")
        try:
            self.assertEqual(local._http.headers.get(internal_auth.HEADER), internal_auth.TOKEN)
        finally:
            local._http.close()
        remote = h3_client.H3Client("http://192.0.2.1:15111")
        try:
            self.assertNotIn(internal_auth.HEADER, remote._http.headers)
        finally:
            remote._http.close()


if __name__ == "__main__":
    unittest.main()
