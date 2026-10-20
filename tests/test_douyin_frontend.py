import unittest

import httpx

from backend.app import app, dist_dir


class DouyinFrontendTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless((dist_dir / "index.html").is_file(), "需要先构建前端")
    async def test_direct_request_returns_frontend_without_cache(self):
        # 不启动应用生命周期，避免启动任务队列或下载服务。
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            for path in ("/douyin", "/douyin/"):
                response = await client.get(path, follow_redirects=True)
                self.assertEqual(response.status_code, 200)
                self.assertIn("text/html", response.headers["content-type"])
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertEqual(response.content, (dist_dir / "index.html").read_bytes())
