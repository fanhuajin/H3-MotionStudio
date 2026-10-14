import unittest, tempfile, json
from pathlib import Path
from unittest.mock import patch, AsyncMock
import httpx
from backend import batch_image as b

class YeeloTests(unittest.IsolatedAsyncioTestCase):
    async def test_poll_success_without_resubmitting(self):
        responses = [httpx.Response(200,json={'status':'processing'}),httpx.Response(200,json={'status':'completed','data':[{'url':'https://example.com/image.png'}]})]
        calls=[]
        def handler(request):
            calls.append(request)
            return responses.pop(0)
        transport=httpx.MockTransport(handler)
        real_client=httpx.AsyncClient
        with tempfile.TemporaryDirectory() as directory, patch.object(b,'_yeelo',return_value=True), patch.object(b,'base_url',return_value='https://img.yeelo.fun/v1'), patch.object(b,'api_key',return_value='test-key'), patch.object(b.asyncio,'sleep',new_callable=AsyncMock), patch.object(b.httpx,'AsyncClient',side_effect=lambda **kwargs:real_client(transport=transport)):
            target=Path(directory)/'image.png'
            result=await b._await_image_task(httpx.Response(200,json={'task_id':'task/1'}),target)
            self.assertEqual(result.json()['data'][0]['url'],'https://example.com/image.png')
            self.assertTrue(all(r.method=='GET' for r in calls))
            self.assertEqual(len(calls),2)
            self.assertEqual(json.loads(target.with_name('image.png.relay-task.json').read_text())['task_id'],'task/1')
    async def test_failed_task_keeps_checkpoint(self):
        real_client=httpx.AsyncClient
        transport=httpx.MockTransport(lambda request:httpx.Response(200,json={'status':'failed','error':'rejected'}))
        with tempfile.TemporaryDirectory() as directory, patch.object(b,'_yeelo',return_value=True), patch.object(b.asyncio,'sleep',new_callable=AsyncMock), patch.object(b.httpx,'AsyncClient',side_effect=lambda **kwargs:real_client(transport=transport)):
            target=Path(directory)/'image.png'
            with self.assertRaisesRegex(RuntimeError,'未重新提交'):
                await b._await_image_task(httpx.Response(200,json={'task_id':'failed-task'}),target)
            self.assertTrue(target.with_name('image.png.relay-task.json').exists())
    def test_provider_options(self):
        with patch.object(b,'_yeelo',return_value=True):
            data=b._request_options({'input_fidelity':'high','quality':'high','n':'1','size':'1024x768'})
        self.assertNotIn('input_fidelity',data)
        self.assertEqual(data['async'],'true')
        self.assertEqual(data['quality'],'auto')
        self.assertEqual(data['n'],'1')
    def test_4k_sizes_skip_local_upscale(self):
        with patch.object(b, '_yeelo', return_value=True), patch.object(b, 'env_value', side_effect=lambda name, default='': '4k' if name == 'H3_BATCH_IMAGE_RESOLUTION' else default):
            self.assertEqual(b.image_sizes(), {'4:3': '3264x2448', '9:16': '2160x3840', '3:4': '2448x3264'})
            for ratio in b.image_sizes():
                self.assertFalse(b.needs_local_upscale(ratio))

    async def test_sync_legacy_response(self):
        response=httpx.Response(200,json={'data':[{'b64_json':'eA=='}]})
        with patch.object(b,'_yeelo',return_value=False):
            self.assertIs(await b._await_image_task(response,Path('unused.png')),response)

if __name__=='__main__': unittest.main()
