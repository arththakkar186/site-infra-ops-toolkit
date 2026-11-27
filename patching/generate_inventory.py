"""Fictional patch inventory in the exact CSV layout Get-PatchCompliance.ps1 writes.

    python3 -m patching.generate_inventory --out data/synthetic/patch_inventory.csv
"""
from __future__ import annotations

import argparse
import csv
import random
from datetime import datetime, timedelta
from pathlib import Path

AS_OF = datetime(2026, 11, 1, 6, 0)
FLEET = [  # (role, count, OS caption, build major, current UBR, previous-cycle UBR)
    ("DC", 4, "Microsoft Windows Server 2022 Datacenter", "20348", 4300, 4170),
    ("FS", 6, "Microsoft Windows Server 2022 Standard", "20348", 4300, 4170),
    ("SQL", 5, "Microsoft Windows Server 2019 Standard", "17763", 7900, 7792),
    ("APP", 14, "Microsoft Windows Server 2025 Standard", "26100", 6900, 6584),
    ("PRN", 3, "Microsoft Windows Server 2019 Standard", "17763", 7900, 7792),
    ("WS", 60, "Microsoft Windows 11 Enterprise", "26100", 6900, 6584),
    ("WS", 20, "Microsoft Windows 11 Enterprise", "22631", 6100, 5909),
]


def generate(out: Path, seed: int = 5) -> None:
    rnd = random.Random(seed)
    rows, n = [], 0
    for role, count, caption, major, cur, prev in FLEET:
        for _ in range(count):
            n += 1
            name = f"SITE1-{role}-{n:03d}"
            roll = rnd.random()
            if roll < 0.04:
                rows.append({"ComputerName": name, "Status": "Unreachable", "OS": "", "Build": "", "LastBootUtc": "",
                             "InstalledKBs": "", "LastKBDate": "", "PendingReboot": ""})
                continue
            patched = roll > 0.16
            ubr = cur + rnd.choice([0, 0, 0, 33]) if patched else prev
            pending = (not patched and rnd.random() < 0.5) or (patched and rnd.random() < 0.05)
            boot = AS_OF - timedelta(days=rnd.randint(1, 18) if patched else rnd.randint(19, 60), hours=rnd.randint(0, 23))
            kb_date = AS_OF - timedelta(days=rnd.randint(5, 18) if patched else rnd.randint(30, 50))
            rows.append({"ComputerName": name, "Status": "OK", "OS": caption, "Build": f"{major}.{ubr}",
                         "LastBootUtc": boot.strftime("%Y-%m-%dT%H:%M:%SZ"),
                         "InstalledKBs": ";".join(f"KB50{rnd.randint(10000, 99999)}" for _ in range(3)),
                         "LastKBDate": kb_date.strftime("%Y-%m-%d"), "PendingReboot": str(pending)})
    rows.append({"ComputerName": "SITE1-APP-900", "Status": "OK", "OS": "Microsoft Windows Server 2016 Standard",
                 "Build": "14393.7428", "LastBootUtc": "2026-09-01T03:00:00Z", "InstalledKBs": "KB5066836",
                 "LastKBDate": "2026-08-20", "PendingReboot": "False"})
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/synthetic/patch_inventory.csv")
    ap.add_argument("--seed", type=int, default=5)
    a = ap.parse_args()
    generate(Path(a.out), a.seed)
    print(f"wrote {a.out}")
