"""Patch compliance against the cycle baseline.

    python3 -m patching.patch_report --inventory data/synthetic/patch_inventory.csv --as-of 2026-11-01 --out out/patching

Rules, per computer:
  * Unreachable           -> UNKNOWN (an unreachable computer counts as not compliant until proven otherwise)
  * OS not in baseline    -> UNSUPPORTED (end-of-life or unknown build family)
  * build >= minimum      -> COMPLIANT    (a pending reboot is flagged, because the update isn't active until reboot)
  * build <  minimum      -> IN GRACE while as_of <= release + grace days for the tier, else NON-COMPLIANT
  * uptime > max_uptime   -> flagged (the last cycle was probably not applied)
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent


def build_tuple(b: str) -> tuple[int, int]:
    major, _, ubr = b.partition(".")
    return int(major), int(ubr or 0)


def os_family(caption: str, build: str) -> str | None:
    cap, major = caption.replace("Microsoft ", ""), build.split(".")[0]
    if "Server 2019" in cap:
        return "Windows Server 2019"
    if "Server 2022" in cap:
        return "Windows Server 2022"
    if "Server 2025" in cap:
        return "Windows Server 2025"
    if "Windows 11" in cap:
        return {"22631": "Windows 11 23H2", "26100": "Windows 11 24H2"}.get(major)
    return None


def tier(name: str, tiers: dict) -> str:
    role = name.split("-")[1] if "-" in name else ""
    return next((t for t, roles in tiers.items() if role in roles), "standard")


def evaluate(rows: list[dict], baseline: dict, as_of: datetime) -> list[dict]:
    release = datetime.strptime(baseline["release_date"], "%Y-%m-%d")
    out = []
    for r in rows:
        t = tier(r["ComputerName"], baseline["tiers"])
        deadline = release + timedelta(days=baseline["grace_days"][t])
        res = {"computer": r["ComputerName"], "tier": t, "os": r["OS"], "build": r["Build"], "required": "",
               "state": "", "pending_reboot": r["PendingReboot"] == "True", "uptime_days": "", "notes": ""}
        if r["Status"] != "OK":
            res.update(state="UNKNOWN", notes="unreachable: check WinRM / firewall / decommissioned")
            out.append(res)
            continue
        fam = os_family(r["OS"], r["Build"])
        up = (as_of - datetime.strptime(r["LastBootUtc"], "%Y-%m-%dT%H:%M:%SZ")).days
        res["uptime_days"] = up
        if not fam or fam not in baseline["minimum_build"]:
            res.update(state="UNSUPPORTED", notes="OS not in baseline (end of support?)")
            out.append(res)
            continue
        need = baseline["minimum_build"][fam]
        res["required"] = need
        if build_tuple(r["Build"]) >= build_tuple(need):
            res["state"] = "COMPLIANT"
        elif as_of <= deadline:
            res.update(state="IN GRACE", notes=f"deadline {deadline:%Y-%m-%d}")
        else:
            res.update(state="NON-COMPLIANT", notes=f"deadline {deadline:%Y-%m-%d} passed")
        if res["pending_reboot"]:
            res["notes"] = (res["notes"] + "; " if res["notes"] else "") + "reboot pending"
        if up > baseline["max_uptime_days"]:
            res["notes"] = (res["notes"] + "; " if res["notes"] else "") + f"up {up} days"
        out.append(res)
    return out


def summarise(results: list[dict]) -> dict:
    by_state = Counter(r["state"] for r in results)
    by_tier = {t: Counter(r["state"] for r in results if r["tier"] == t) for t in sorted({r["tier"] for r in results})}
    measured = [r for r in results if r["state"] != "UNKNOWN"]
    good = sum(1 for r in results if r["state"] in ("COMPLIANT", "IN GRACE"))
    return {"computers": len(results), "by_state": dict(by_state),
            "compliance_pct": round(100 * good / len(results), 1) if results else 0.0,
            "reachable_pct": round(100 * len(measured) / len(results), 1) if results else 0.0,
            "pending_reboot": sum(r["pending_reboot"] for r in results),
            "by_tier": {t: dict(c) for t, c in by_tier.items()}}


def render(s: dict, results: list[dict], baseline: dict, as_of: datetime) -> str:
    L = [f"# Patch compliance: cycle {baseline['cycle']} (as of {as_of:%Y-%m-%d})", "",
         f"Computers: **{s['computers']}** · compliant or in grace: **{s['compliance_pct']}%** · reachable: {s['reachable_pct']}% · "
         f"pending reboot: {s['pending_reboot']}", "",
         "Unreachable computers count against compliance: a computer that can't be inventoried can't be shown to be patched.", "",
         "| State | Computers |", "|---|---:|"] + [f"| {k} | {v} |" for k, v in sorted(s["by_state"].items())]
    L += ["", "| Tier | Grace | " + " | ".join(sorted(s["by_state"])) + " |", "|---|---:|" + "---:|" * len(s["by_state"])]
    for t, c in s["by_tier"].items():
        L.append(f"| {t} | {baseline['grace_days'][t]} d | " + " | ".join(str(c.get(k, 0)) for k in sorted(s["by_state"])) + " |")
    L += ["", "## Action list", "", "| Computer | Tier | OS | Build | Required | State | Notes |", "|---|---|---|---|---|---|---|"]
    order = {"NON-COMPLIANT": 0, "UNKNOWN": 1, "UNSUPPORTED": 2, "IN GRACE": 3, "COMPLIANT": 4}
    for r in sorted(results, key=lambda r: (order[r["state"]], r["tier"] != "critical", r["computer"])):
        if r["state"] == "COMPLIANT" and not r["notes"]:
            continue
        L.append(f"| {r['computer']} | {r['tier']} | {r['os']} | {r['build']} | {r['required']} | {r['state']} | {r['notes']} |")
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inventory", required=True)
    ap.add_argument("--baseline", default=str(HERE / "baseline.json"))
    ap.add_argument("--as-of", default=datetime.utcnow().strftime("%Y-%m-%d"))
    ap.add_argument("--out", default="out/patching")
    a = ap.parse_args(argv)
    baseline = json.loads(Path(a.baseline).read_text())
    as_of = datetime.strptime(a.as_of, "%Y-%m-%d")
    with open(a.inventory, newline="") as fh:
        rows = list(csv.DictReader(fh))
    results = evaluate(rows, baseline, as_of)
    s = summarise(results)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    with (out / "patch_compliance.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(results[0].keys()))
        w.writeheader()
        w.writerows(results)
    (out / "patch_report.md").write_text(render(s, results, baseline, as_of))
    print(f"{s['computers']} computers, {s['compliance_pct']}% compliant or in grace; {s['by_state']}. Report: {out}/patch_report.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
