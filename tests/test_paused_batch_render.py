import asyncio
import unittest
from unittest.mock import patch
from fastapi import HTTPException
from backend import app as module

class PausedRenderTests(unittest.TestCase):
    def test_paused_ready_batch_can_start_render(self):
        state={'status':'paused','items':[{'status':'confirmed'}]}
        with patch.object(module,'_batch_or_404',return_value=state), patch.object(module.batch_worker,'_prep_remaining',return_value=False), patch.object(module.batch_store,'update'), patch.object(module,'_resume_batch',return_value={'status':'running'}) as resume:
            self.assertEqual(asyncio.run(module.start_batch_render('batch'))['status'],'running')
            resume.assert_called_once()

    def test_pause_does_not_bypass_unfinished_materials(self):
        with patch.object(module,'_batch_or_404',return_value={'status':'paused','items':[{'status':'confirmed'}]}), patch.object(module.batch_worker,'_prep_remaining',return_value=True), patch.object(module,'_resume_batch') as resume:
            with self.assertRaises(HTTPException):
                asyncio.run(module.start_batch_render('batch'))
            resume.assert_not_called()
