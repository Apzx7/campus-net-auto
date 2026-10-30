# -*- coding: utf-8 -*-
"""connect.py 的单元测试（只用标准库）：python -m unittest discover -s tests -v"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import connect  # noqa: E402


class ConfigTests(unittest.TestCase):
    def test_load_missing_file_returns_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = connect.load_config(Path(tmp) / "nope.json")
        self.assertEqual(cfg["portal_url"], connect.DEFAULTS["portal_url"])
        self.assertTrue(cfg["headless"])

    def test_save_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            cfg = dict(connect.DEFAULTS)
            cfg["username"] = "20240001"
            cfg["_runtime"] = "should not be persisted"
            connect.save_config(cfg, path)

            self.assertNotIn("_runtime", path.read_text(encoding="utf-8"))
            loaded = connect.load_config(path)
            self.assertEqual(loaded["username"], "20240001")

    def test_broken_json_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(SystemExit):
                connect.load_config(path)


class PasswordTests(unittest.TestCase):
    def test_env_password_wins(self):
        old = os.environ.get("CAMPUS_PASSWORD")
        os.environ["CAMPUS_PASSWORD"] = "from-env"
        try:
            self.assertEqual(connect.resolve_password({"password": "plain"}), "from-env")
        finally:
            if old is None:
                os.environ.pop("CAMPUS_PASSWORD", None)
            else:
                os.environ["CAMPUS_PASSWORD"] = old

    def test_plaintext_password(self):
        os.environ.pop("CAMPUS_PASSWORD", None)
        self.assertEqual(connect.resolve_password({"password": "plain"}), "plain")

    def test_missing_password_exits(self):
        os.environ.pop("CAMPUS_PASSWORD", None)
        with self.assertRaises(SystemExit):
            connect.resolve_password({"password": "", "password_dpapi": ""})

    @unittest.skipUnless(os.name == "nt", "DPAPI 只在 Windows 上可用")
    def test_dpapi_roundtrip_and_not_plaintext(self):
        secret = "P@ssw0rd-中文-123"
        blob = connect.protect_password(secret)
        self.assertNotIn("P@ssw0rd", blob)
        self.assertEqual(connect.unprotect_password(blob), secret)


class DetectionTests(unittest.TestCase):
    def test_error_keywords(self):
        self.assertEqual(connect._has_error("用户名或密码错误"), "密码错误")
        self.assertEqual(connect._has_error("密码错误，请重试"), "密码错误")
        self.assertIsNone(connect._has_error("欢迎使用校园网"))

    def test_success_url_pattern(self):
        self.assertTrue(connect.RE_SUCCESS_URL.search("http://10.254.241.66/portal/entry/pc/finish;flowParams=undefined;from="))
        self.assertTrue(connect.RE_SUCCESS_URL.search("http://10.254.241.66/self/index"))
        # 登录页本身不是成功页
        self.assertIsNone(connect.RE_SUCCESS_URL.search("http://10.254.241.66/pc/center?service=abc"))
        self.assertIsNone(connect.RE_SUCCESS_URL.search("http://10.254.241.66/cas-sso/login?service=x"))

    def test_check_online_shape(self):
        online, detail = connect.check_online(timeout=2)
        self.assertIsInstance(online, bool)
        self.assertIsInstance(detail, str)
        self.assertTrue(detail)

    def test_unreachable_portal(self):
        # 保留地址，必须快速失败而不是卡住
        self.assertFalse(connect.portal_reachable("http://192.0.2.1/", timeout=1.5))

    def test_find_portal_entry_returns_string(self):
        # 在线时没有劫持跳转 -> 返回空串，调用方退回配置里的地址
        entry = connect.find_portal_entry("http://10.254.241.66/", timeout=2)
        self.assertIsInstance(entry, str)
        if entry:
            self.assertTrue(entry.startswith("http"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
