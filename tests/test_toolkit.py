from __future__ import annotations

import copy
import csv
import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backup import restore_verify as rv  # noqa: E402
from patching import generate_inventory, patch_report  # noqa: E402
from switch_onboarding import onboard  # noqa: E402


class Backup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.src, self.dst = t / "src", t / "dst"
        for i in range(20):
            p = self.src / f"d{i % 3}" / f"f{i}.bin"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(bytes([i]) * (100 + i))
        self.m = rv.build_manifest(self.src, "t")
        shutil.copytree(self.src, self.dst)

    def tearDown(self):
        self.tmp.cleanup()

    def test_clean_restore_passes(self):
        r = rv.verify(self.m, self.dst)
        self.assertTrue(r["passed"])
        self.assertEqual(r["verified"], 20)

    def test_detects_missing_corrupt_resized_and_extra(self):
        (self.dst / "d0/f0.bin").unlink()
        p = self.dst / "d1/f1.bin"
        b = bytearray(p.read_bytes()); b[5] ^= 1; p.write_bytes(bytes(b))     # same size, different content
        (self.dst / "d2/f2.bin").write_bytes(b"short")
        (self.dst / "d2/new.txt").write_text("not in backup")
        r = rv.verify(self.m, self.dst)
        self.assertEqual((r["missing"], r["hash_mismatch"], r["size_mismatch"], r["extra"]),
                         (["d0/f0.bin"], ["d1/f1.bin"], ["d2/f2.bin"], ["d2/new.txt"]))
        self.assertFalse(r["passed"])

    def test_sample_always_includes_changed_files(self):
        prev = copy.deepcopy(self.m)
        prev["files"]["d1/f4.bin"]["sha256"] = "0" * 64
        del prev["files"]["d2/f5.bin"]
        s = rv.choose_sample(self.m, 10, seed=1, previous=prev)
        self.assertIn("d1/f4.bin", s)
        self.assertIn("d2/f5.bin", s)
        self.assertGreaterEqual(len(s), 2)

    def test_rpo_and_rto(self):
        r = rv.verify(self.m, self.dst)
        created = datetime.strptime(self.m["created_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        ok = rv.assess(self.m, r, 600, rto_minutes=240, rpo_hours=24, now=created + timedelta(hours=2))
        self.assertEqual(ok["overall"], "PASS")
        late = rv.assess(self.m, r, 600, rto_minutes=240, rpo_hours=24, now=created + timedelta(hours=30))
        slow = rv.assess(self.m, r, 5 * 3600, rto_minutes=240, rpo_hours=24, now=created)
        self.assertEqual((late["overall"], late["rpo_met"]), ("FAIL", False))
        self.assertEqual((slow["overall"], slow["rto_met"]), ("FAIL", False))

    def test_demo_catches_injected_faults(self):
        out = Path(self.tmp.name) / "ev"
        self.assertEqual(rv.main(["demo", "--out", str(out)]), 0)
        self.assertTrue(list(out.glob("*.md")))


class Patching(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = json.loads((ROOT / "patching/baseline.json").read_text())

    def row(self, **kw):
        r = {"ComputerName": "SITE1-DC-001", "Status": "OK", "OS": "Microsoft Windows Server 2022 Datacenter",
             "Build": "20348.4300", "LastBootUtc": "2026-10-25T03:00:00Z", "InstalledKBs": "", "LastKBDate": "", "PendingReboot": "False"}
        r.update(kw)
        return r

    def state(self, as_of="2026-11-01", **kw):
        return patch_report.evaluate([self.row(**kw)], self.base, datetime.strptime(as_of, "%Y-%m-%d"))[0]

    def test_build_compare_is_numeric(self):
        self.assertGreater(patch_report.build_tuple("20348.10000"), patch_report.build_tuple("20348.4300"))

    def test_states(self):
        self.assertEqual(self.state()["state"], "COMPLIANT")
        self.assertEqual(self.state(Build="20348.4400")["state"], "COMPLIANT")
        self.assertEqual(self.state(Build="20348.4170")["state"], "NON-COMPLIANT")             # critical, 14 d passed
        self.assertEqual(self.state(Build="20348.4170", as_of="2026-10-20")["state"], "IN GRACE")
        self.assertEqual(self.state(ComputerName="SITE1-WS-001", OS="Microsoft Windows 11 Enterprise",
                                    Build="26100.6584")["state"], "IN GRACE")                  # standard, 30 d
        self.assertEqual(self.state(Status="Unreachable")["state"], "UNKNOWN")
        self.assertEqual(self.state(OS="Microsoft Windows Server 2016 Standard", Build="14393.7428")["state"], "UNSUPPORTED")

    def test_flags_reboot_and_uptime(self):
        r = self.state(PendingReboot="True", LastBootUtc="2026-09-01T00:00:00Z")
        self.assertIn("reboot pending", r["notes"])
        self.assertIn("up 61 days", r["notes"])

    def test_unknown_counts_against_compliance(self):
        rows = [self.row(), self.row(ComputerName="SITE1-FS-002", Status="Unreachable")]
        s = patch_report.summarise(patch_report.evaluate(rows, self.base, datetime(2026, 11, 1)))
        self.assertEqual(s["compliance_pct"], 50.0)

    def test_committed_inventory_matches_generator(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "inv.csv"
            generate_inventory.generate(p)
            self.assertEqual(p.read_text(), (ROOT / "data/synthetic/patch_inventory.csv").read_text())


class Onboarding(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fab = onboard.load(ROOT / "switch_onboarding/fabric.json")
        cls.req = onboard.load(ROOT / "switch_onboarding/samples/new-switch.json")

    def errors(self, mutate):
        r = copy.deepcopy(self.req)
        mutate(r)
        return "\n".join(onboard.validate(r, self.fab))

    def test_sample_request_is_valid(self):
        self.assertEqual(onboard.validate(self.req, self.fab), [])

    def test_rejects_bad_requests(self):
        cases = [
            (lambda r: r.update(hostname="switch-3"), "does not follow"),
            (lambda r: r.update(mgmt_ip="10.20.99.12"), "already in use"),
            (lambda r: r.update(mgmt_ip="10.20.98.20"), "not a host address"),
            (lambda r: r.update(mgmt_ip="10.20.99.255"), "not a host address"),
            (lambda r: r.update(port_channel=12), "already used"),
            (lambda r: r["uplinks"][1].update(peer="SITE1-DS1", peer_port="TenGigabitEthernet1/1/10"), "both distribution"),
            (lambda r: r["uplinks"][0].update(peer_port="TenGigabitEthernet1/1/1"), "not a free port"),
            (lambda r: r["ports"].pop(), "every port needs one"),
            (lambda r: r["ports"].append({"range": "1/0/1-2", "profile": "user"}), "assigned twice"),
            (lambda r: r["ports"].append({"range": "3/0/1", "profile": "user"}), "does not exist"),
            (lambda r: r.update(model="C2960X"), "not a supported"),
        ]
        for mutate, expect in cases:
            with self.subTest(expect=expect):
                self.assertIn(expect, self.errors(mutate))

    def test_rendered_config(self):
        cfg = onboard.render_access(self.req, self.fab)
        self.assertIn("hostname SITE1-AS-3F-01", cfg)
        self.assertIn("channel-group 16 mode active", cfg)
        self.assertIn("ip address 10.20.99.16 255.255.255.0", cfg)
        self.assertIn("interface range GigabitEthernet2/0/47 - 48\n description UNUSED\n switchport mode access\n switchport access vlan 999", cfg)
        self.assertIn("{{RADIUS_KEY}}", cfg)
        for secret_like in ("key 7 ", "secret 5 ", "password "):
            self.assertNotIn(secret_like, cfg, "no real secrets in rendered config")
        self.assertEqual(cfg.count("dot1x pae authenticator"), 2)       # two user ranges

    def test_distribution_config_per_peer(self):
        d = onboard.render_distribution(self.req, self.fab)
        self.assertEqual(set(d), {"SITE1-DS1", "SITE1-DS2"})
        self.assertIn("spanning-tree guard root", d["SITE1-DS1"])
        self.assertIn("interface TenGigabitEthernet1/1/9", d["SITE1-DS2"])

    def test_postcheck_good_and_bad(self):
        good = onboard.postcheck(self.req, self.fab, (ROOT / "switch_onboarding/samples/postcheck-good.txt").read_text())
        bad = onboard.postcheck(self.req, self.fab, (ROOT / "switch_onboarding/samples/postcheck-bad.txt").read_text())
        self.assertTrue(all(ok for _, ok, _ in good), good)
        failed = {n for n, ok, _ in bad if not ok}
        self.assertEqual(len(failed), 5)
        self.assertIn("this switch is NOT spanning-tree root", failed)

    def test_expand(self):
        self.assertEqual(onboard.expand("1/0/3-5"), ["1/0/3", "1/0/4", "1/0/5"])
        self.assertEqual(onboard.expand("2/0/7"), ["2/0/7"])


if __name__ == "__main__":
    unittest.main()
