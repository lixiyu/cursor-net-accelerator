"""分域解析的启停生命周期：启动劫持、退出释放。

重点：进程退出后必须把 /etc/resolver/<domain> 改回「只走系统 DNS」，
否则加速器不在了，cursor.sh 反而谁都解析不了。

跑法：.venv/bin/python -m unittest discover -s tests -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cursor_net_accelerator import resolver_macos as rm
from cursor_net_accelerator.config import Settings


class ResolverLifecycleTest(unittest.TestCase):
    def setUp(self):
        self._orig_dir = rm.RESOLVER_DIR
        self._tmp = tempfile.TemporaryDirectory()
        rm.RESOLVER_DIR = Path(self._tmp.name)
        self.settings = Settings.from_env()
        self.path = rm.resolver_file_path(self.settings.resolver_domain)

    def tearDown(self):
        rm.RESOLVER_DIR = self._orig_dir
        self._tmp.cleanup()

    def test_active_content_points_to_local_dns_with_fallback(self):
        body = rm.resolver_contents(self.settings)
        lines = [ln for ln in body.splitlines() if ln.startswith("nameserver")]
        self.assertEqual(lines[0], f"nameserver {self.settings.dns_bind}.{self.settings.dns_port}")
        self.assertGreaterEqual(len(lines), 2, "必须带一条系统 DNS 兜底，防止进程挂了就断网")

    def test_neutral_content_has_no_local_dns(self):
        body = rm.neutral_contents()
        self.assertNotIn(f"127.0.0.1.{self.settings.dns_port}", body)
        self.assertIn("nameserver", body)

    def test_activate_then_neutralize(self):
        self.assertTrue(rm.activate(self.settings))
        st = rm.status(self.settings)
        self.assertTrue(st["active"], "启动后应处于劫持状态")
        self.assertFalse(st["neutralized"])

        self.assertTrue(rm.neutralize(self.settings))
        st = rm.status(self.settings)
        self.assertFalse(st["active"], "退出后不应再指向本机")
        self.assertTrue(st["neutralized"])
        self.assertEqual(self.path.read_text(encoding="utf-8"), rm.neutral_contents())

    def test_neutralize_is_ok_when_file_missing(self):
        self.assertTrue(rm.neutralize(self.settings))

    def test_activate_returns_false_when_not_writable(self):
        # 模拟 /etc/resolver 仍是 root 的情况：目录不可写
        rm.RESOLVER_DIR.chmod(0o500)
        try:
            self.assertFalse(rm.activate(self.settings))
        finally:
            rm.RESOLVER_DIR.chmod(0o700)

    def test_status_reports_installed_and_mismatch(self):
        self.assertFalse(rm.status(self.settings)["installed"])
        self.path.write_text("nameserver 1.1.1.1\n", encoding="utf-8")
        st = rm.status(self.settings)
        self.assertTrue(st["installed"])
        self.assertFalse(st["active"])
        self.assertFalse(st["neutralized"])


if __name__ == "__main__":
    unittest.main()
