import unittest
from types import SimpleNamespace
from browser_page import read_browser_url


def control(kind,name,value,hidden=False):
    return SimpleNamespace(ControlTypeName=kind,Name=name,IsOffscreen=hidden,AutomationId="",
                           GetValuePattern=lambda:SimpleNamespace(Value=value))


class BrowserPageTests(unittest.TestCase):
    def read(self,controls):
        # Only the read-only traversal API is available: focus/keys/clipboard use would fail.
        uia=SimpleNamespace(WalkControl=lambda window,maxDepth:[(c,0) for c in controls])
        return read_browser_url(uia,object(),"创作")

    def test_visible_document_over_address(self):
        self.assertEqual(self.read([
            control("DocumentControl","创作平台","https://creator.example/upload"),
            control("EditControl","地址和搜索栏","https://other.example")]),"https://creator.example/upload")

    def test_hidden_document_not_used(self):
        self.assertEqual(self.read([
            control("DocumentControl","创作平台","https://hidden.example",True),
            control("EditControl","地址和搜索栏","creator.example/upload")]),"https://creator.example/upload")

    def test_missing_or_conflicting_address_stops(self):
        with self.assertRaises(RuntimeError):
            self.read([])
        with self.assertRaises(RuntimeError):
            self.read([control("EditControl","地址","https://a.example"),control("EditControl","地址","https://b.example")])


if __name__=="__main__":
    unittest.main()
