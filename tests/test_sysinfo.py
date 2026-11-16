"""`panel/sysinfo.leases()` 的设备合并行为测试（v0.1.116 / P3-39）。

背景：设备列表原来只有两个数据源——dnsmasq 租约 + 静态绑定。手工配静态 IP 的
设备（mesh 主节点、NAS、打印机）**从不走 DHCP**，于是它们只出现在概览的
ARP 计数里，设备列表永远看不到，两个数字对不上（用户实测：概览 12 台、列表 11 条）。

本测试喂假的租约文件和假的 `/proc/net/arp`，断言合并结果，不读真机。
"""
import os
import shutil
import sys
import tempfile
import unittest

import support

sys.path.insert(0, os.path.join(support.ROOT, "panel"))
import sysinfo  # noqa: E402

ARP_HEAD = ("IP address       HW type     Flags       HW address            "
            "Mask     Device\n")


def arp_line(ip, mac, flags="0x2", dev="br-lan"):
    return "%s   0x1         %s         %s     *        %s\n" % (ip, flags, mac, dev)


class Base(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp(prefix="dert-sysinfo-")
        self._old = (sysinfo._lease_file, sysinfo._arp_file, sysinfo._notes_file)
        sysinfo._lease_file = os.path.join(self.td, "leases")
        sysinfo._arp_file = os.path.join(self.td, "arp")
        sysinfo._notes_file = os.path.join(self.td, "notes.json")
        self.write_leases("")
        self.write_arp()

    def tearDown(self):
        sysinfo._lease_file, sysinfo._arp_file, sysinfo._notes_file = self._old
        shutil.rmtree(self.td, ignore_errors=True)

    def write_leases(self, text):
        with open(sysinfo._lease_file, "w") as f:
            f.write(text)

    def write_arp(self, *lines):
        with open(sysinfo._arp_file, "w") as f:
            f.write(ARP_HEAD + "".join(lines))

    def by_mac(self, rows):
        return {r["mac"]: r for r in rows}


class StaticIpDeviceTest(Base):
    """只在 ARP 里出现、不走 DHCP 的设备。"""

    def test_static_ip_device_is_listed(self):
        """mesh 主 AP 这类静态 IP 设备必须进列表，并带 no_dhcp 标记。"""
        self.write_arp(arp_line("192.168.10.128", "8c:de:f9:32:fb:48"))
        rows = sysinfo.leases([])
        self.assertEqual(len(rows), 1, rows)
        r = rows[0]
        self.assertEqual(r["mac"], "8C:DE:F9:32:FB:48")   # 统一大写
        self.assertEqual(r["ip"], "192.168.10.128")        # IP 来自 ARP
        self.assertTrue(r["no_dhcp"])
        self.assertTrue(r["online"])
        self.assertFalse(r["static"])
        self.assertIsNone(r["name"])
        self.assertEqual(r["lease_text"], "")

    def test_incomplete_arp_entry_ignored(self):
        """flags 0x0 = ARP 未解析完整，不能当在线设备。"""
        self.write_arp(arp_line("192.168.10.150", "aa:bb:cc:dd:ee:01", flags="0x0"))
        self.assertEqual(sysinfo.leases([]), [])

    def test_non_br_lan_interface_ignored(self):
        """WAN 侧接口的 ARP 条目不算内网设备（B-05：与 priv/status.py 判据一致）。"""
        self.write_arp(arp_line("219.142.78.1", "aa:bb:cc:dd:ee:02", dev="enp4s0"))
        self.assertEqual(sysinfo.leases([]), [])

    def test_zero_mac_ignored(self):
        self.write_arp(arp_line("192.168.10.151", "00:00:00:00:00:00"))
        self.assertEqual(sysinfo.leases([]), [])

    def test_offline_static_ip_device_not_listed(self):
        """ARP 里没有的设备不会被凭空造出来（合并只认 ARP 在线条目）。"""
        self.write_arp()
        self.write_leases("1793600000 00:11:22:33:44:55 192.168.10.10 nas *\n")
        rows = sysinfo.leases([])
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["online"])


class MergePriorityTest(Base):
    """三个来源重叠时的优先级。"""

    def test_lease_and_arp_same_mac_is_one_row(self):
        """既有租约又在 ARP 里：只有一条，no_dhcp 必须为 False。"""
        self.write_leases("1793600000 00:11:22:33:44:55 192.168.10.10 nas *\n")
        self.write_arp(arp_line("192.168.10.10", "00:11:22:33:44:55"))
        rows = sysinfo.leases([])
        self.assertEqual(len(rows), 1, rows)
        r = rows[0]
        self.assertFalse(r["no_dhcp"])
        self.assertTrue(r["online"])
        self.assertEqual(r["name"], "nas")

    def test_static_binding_wins_over_arp_ip(self):
        """静态绑定的 IP 优先（绑定改了地址时以绑定为准，沿用原逻辑）。"""
        self.write_arp(arp_line("192.168.10.99", "00:11:22:33:44:55"))
        rows = sysinfo.leases([{"name": "nas", "ip": "192.168.10.10",
                                "mac": "00:11:22:33:44:55"}])
        r = rows[0]
        self.assertEqual(r["ip"], "192.168.10.10")
        self.assertTrue(r["static"])
        self.assertFalse(r["no_dhcp"])

    def test_static_binding_without_lease_or_arp(self):
        """离线静态绑定仍要进列表（回归：不能被新逻辑吞掉）。"""
        rows = sysinfo.leases([{"name": "printer", "ip": "192.168.10.20",
                                "mac": "00:11:22:33:44:66"}])
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["static"])
        self.assertFalse(rows[0]["online"])
        self.assertFalse(rows[0]["no_dhcp"])

    def test_all_sources_merge(self):
        """租约 + 静态绑定 + ARP 静态 IP 三种来源同时存在。"""
        self.write_leases("1793600000 00:11:22:33:44:55 192.168.10.10 nas *\n")
        self.write_arp(
            arp_line("192.168.10.10", "00:11:22:33:44:55"),      # 有租约
            arp_line("192.168.10.128", "8c:de:f9:32:fb:48"),     # 只有 ARP
        )
        rows = sysinfo.leases([{"name": "printer", "ip": "192.168.10.20",
                                "mac": "00:11:22:33:44:66"}])
        self.assertEqual(len(rows), 3, rows)
        m = self.by_mac(rows)
        self.assertFalse(m["00:11:22:33:44:55"]["no_dhcp"])
        self.assertTrue(m["8C:DE:F9:32:FB:48"]["no_dhcp"])
        self.assertTrue(m["00:11:22:33:44:66"]["static"])


class FieldContractTest(Base):
    """字段契约：前端与第三方消费方不该靠猜。"""

    def test_no_dhcp_always_present(self):
        self.write_leases("1793600000 00:11:22:33:44:55 192.168.10.10 nas *\n")
        self.write_arp(arp_line("192.168.10.128", "8c:de:f9:32:fb:48"))
        for r in sysinfo.leases([]):
            self.assertIn("no_dhcp", r, r)
            self.assertIn("online", r, r)
            self.assertIn("note", r, r)

    def test_broken_lease_line_skipped(self):
        self.write_leases("broken-line 00:11:22:33:44:56 192.168.10.11 x *\n"
                          "0 00:11:22:33:44:57 192.168.10.12 infinite *\n")
        rows = sysinfo.leases([])
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["lease_text"], "永久")

    def test_missing_files_do_not_raise(self):
        os.remove(sysinfo._lease_file)
        os.remove(sysinfo._arp_file)
        self.assertEqual(sysinfo.leases([]), [])


class ArpHelpersTest(Base):
    """arp_online() / arp_online_macs() 的返回契约。"""

    def test_map_and_set_agree(self):
        self.write_arp(
            arp_line("192.168.10.128", "8c:de:f9:32:fb:48"),
            arp_line("192.168.10.156", "8c:de:f9:32:fb:66"),
        )
        m = sysinfo.arp_online()
        self.assertEqual(m, {"8C:DE:F9:32:FB:48": "192.168.10.128",
                             "8C:DE:F9:32:FB:66": "192.168.10.156"})
        self.assertEqual(sysinfo.arp_online_macs(), set(m))

    def test_header_only_file(self):
        self.write_arp()
        self.assertEqual(sysinfo.arp_online(), {})
        self.assertEqual(sysinfo.arp_online_macs(), set())

    def test_empty_file_does_not_raise(self):
        with open(sysinfo._arp_file, "w"):
            pass
        self.assertEqual(sysinfo.arp_online(), {})


if __name__ == "__main__":
    unittest.main()
