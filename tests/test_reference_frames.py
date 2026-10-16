from pathlib import Path
from unittest.mock import patch
import io
import unittest
from PIL import Image, ImageDraw, ImageFilter
from backend import batch_worker

class LocalReferenceFrameTests(unittest.TestCase):
    def payload(self, blur_left=False, blur_right=False, busy_background=False):
        image = Image.new('RGB', (320, 560), '#888888')
        draw = ImageDraw.Draw(image)
        if busy_background:
            for y in range(0, 560, 4):
                draw.line((0,y,24,y), fill='white',width=2)
                draw.line((296,y,319,y), fill='white',width=2)
        for x in (50, 190):
            for y in range(175, 410, 12):
                draw.rectangle((x,y,x+60,y+5), fill='white')
        for enabled, box in ((blur_left,(26,157,154,459)),(blur_right,(166,157,294,459))):
            if enabled:
                image.paste(image.crop(box).filter(ImageFilter.GaussianBlur(7)),box)
        result=io.BytesIO(); image.save(result,format='PNG');return result.getvalue()

    def test_either_blurred_side_loses_even_with_detailed_background(self):
        clear=batch_worker._frame_detail_score(self.payload())
        for left,right in ((True,False),(False,True),(True,True)):
            self.assertGreater(clear,batch_worker._frame_detail_score(self.payload(left,right,True)))

    def test_sampling_covers_wider_video_and_nearby_moments(self):
        stamps=[]
        with patch.object(batch_worker,'_video_duration_seconds',return_value=10), patch.object(batch_worker,'_extract_frame_bytes',side_effect=lambda source,stamp,**kw: stamps.append(stamp) or b'frame'), patch.object(batch_worker,'_frame_detail_score',return_value=1):
            result=batch_worker._scene_frame_candidates(Path('unused.mp4'),0.38)
        self.assertEqual(len(result),12)
        self.assertLess(min(stamps),1)
        self.assertGreater(max(stamps),8)
        self.assertTrue(any(0 < abs(stamp-3.8) < .16 for stamp in stamps))
