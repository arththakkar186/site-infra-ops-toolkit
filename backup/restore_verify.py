"""Prove backups can be restored: test-restore a sample and verify it against the backup-time manifest.

A backup job reporting "Success" only shows that data was written. A test restore shows the data can be
recovered intact and within the recovery time objective. This tool:

  1. builds a manifest at backup time      (path, size, mtime, SHA-256 for every file)
  2. picks a restore sample                 (all files, or a random % plus every file changed since the last test)
  3. verifies the restored copy             (missing, extra, size mismatch, hash mismatch)
  4. checks RPO and RTO                     (age of the restore point; how long the restore took)
  5. writes evidence                        (JSON + Markdown, timestamped, for the quarterly DR review)

    python3 -m backup.restore_verify manifest --source /data/finance --out manifests/finance.json
    python3 -m backup.restore_verify verify   --manifest manifests/finance.json --restored /restore/finance \
            --restore-seconds 1260 --rto-minutes 240 --rpo-hours 24 --out evidence/
    python3 -m backup.restore_verify demo      # self-contained end-to-end run on generated files
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import shutil
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

CHUNK = 1024 * 1024


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def build_manifest(source: Path, label: str = "") -> dict:
    files = {}
    for p in sorted(source.rglob("*")):
        if p.is_file() and not p.is_symlink():
            st = p.stat()
            files[p.relative_to(source).as_posix()] = {"size": st.st_size, "mtime": int(st.st_mtime), "sha256": sha256(p)}
    return {"label": label or source.name, "source": str(source), "created_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "file_count": len(files), "total_bytes": sum(f["size"] for f in files.values()), "files": files}


def choose_sample(manifest: dict, percent: float, seed: int, previous: dict | None = None) -> list[str]:
    """Random sample plus every file that is new or changed since the previous verified manifest."""
    names = sorted(manifest["files"])
    if percent >= 100:
        return names
    rnd = random.Random(seed)
    k = max(1, round(len(names) * percent / 100))
    sample = set(rnd.sample(names, k))
    if previous:
        old = previous["files"]
        sample |= {n for n in names if n not in old or old[n]["sha256"] != manifest["files"][n]["sha256"]}
    return sorted(sample)


def verify(manifest: dict, restored: Path, sample: list[str] | None = None) -> dict:
    expected = manifest["files"]
    names = sample if sample is not None else sorted(expected)
    missing, size_mismatch, hash_mismatch, ok = [], [], [], 0
    for n in names:
        p = restored / n
        if not p.is_file():
            missing.append(n)
            continue
        if p.stat().st_size != expected[n]["size"]:
            size_mismatch.append(n)
            continue
        if sha256(p) != expected[n]["sha256"]:
            hash_mismatch.append(n)
            continue
        ok += 1
    extra = []
    if sample is None:
        present = {p.relative_to(restored).as_posix() for p in restored.rglob("*") if p.is_file()}
        extra = sorted(present - set(expected))
    return {"checked": len(names), "verified": ok, "missing": missing, "size_mismatch": size_mismatch,
            "hash_mismatch": hash_mismatch, "extra": extra,
            "passed": not (missing or size_mismatch or hash_mismatch)}


def assess(manifest: dict, result: dict, restore_seconds: float, rto_minutes: float, rpo_hours: float,
           now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    point = datetime.strptime(manifest["created_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    age_h = (now - point).total_seconds() / 3600
    rto_ok = restore_seconds / 60 <= rto_minutes
    rpo_ok = age_h <= rpo_hours
    return {**result, "label": manifest["label"], "restore_point_utc": manifest["created_utc"],
            "restore_point_age_hours": round(age_h, 2), "rpo_hours": rpo_hours, "rpo_met": rpo_ok,
            "restore_minutes": round(restore_seconds / 60, 1), "rto_minutes": rto_minutes, "rto_met": rto_ok,
            "overall": "PASS" if result["passed"] and rto_ok and rpo_ok else "FAIL",
            "tested_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ")}


def write_evidence(a: dict, out: Path) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    stem = f"restore-test_{a['label']}_{a['tested_utc'][:10]}"
    (out / f"{stem}.json").write_text(json.dumps(a, indent=2) + "\n")
    lines = [f"# Test restore: {a['label']} ({a['tested_utc']})", "",
             f"**Result: {a['overall']}**", "", "| Check | Value | Target | Met |", "|---|---|---|---|",
             f"| Files verified | {a['verified']} / {a['checked']} | all | {'yes' if a['passed'] else 'NO'} |",
             f"| Restore point age | {a['restore_point_age_hours']} h | ≤ {a['rpo_hours']} h (RPO) | {'yes' if a['rpo_met'] else 'NO'} |",
             f"| Restore duration | {a['restore_minutes']} min | ≤ {a['rto_minutes']} min (RTO) | {'yes' if a['rto_met'] else 'NO'} |", ""]
    for k in ("missing", "size_mismatch", "hash_mismatch", "extra"):
        if a[k]:
            lines += [f"## {k.replace('_', ' ').title()} ({len(a[k])})", ""] + [f"- `{n}`" for n in a[k][:50]] + [""]
    lines += ["Tested by: ____________  Reviewed by: ____________", ""]
    p = out / f"{stem}.md"
    p.write_text("\n".join(lines))
    return p


def demo(out: Path) -> dict:
    """Generate files, back them up to a tar archive, restore, corrupt one file, verify."""
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        src, restored = t / "share", t / "restore"
        rnd = random.Random(5)
        for d in ("finance/2026", "hr/policies", "ops/runbooks"):
            (src / d).mkdir(parents=True)
            for i in range(40):
                (src / d / f"doc{i:03d}.txt").write_bytes(os.urandom(rnd.randint(200, 20000)))
        manifest = build_manifest(src, "demo-share")
        with tarfile.open(t / "backup.tar.gz", "w:gz") as tf:
            tf.add(src, arcname=".")
        started = time.monotonic()
        restored.mkdir()
        with tarfile.open(t / "backup.tar.gz") as tf:
            tf.extractall(restored)
        seconds = time.monotonic() - started
        victim = restored / "finance/2026/doc007.txt"           # simulate silent corruption in the backup chain
        data = bytearray(victim.read_bytes())
        data[0] ^= 0xFF
        victim.write_bytes(bytes(data))
        (restored / "ops/runbooks/doc039.txt").unlink()          # simulate a file missing from the backup
        result = verify(manifest, restored)
        a = assess(manifest, result, seconds, rto_minutes=240, rpo_hours=24)
        write_evidence(a, out)
        shutil.rmtree(restored)
        return a


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="restore_verify")
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("manifest")
    m.add_argument("--source", required=True)
    m.add_argument("--out", required=True)
    m.add_argument("--label", default="")
    v = sub.add_parser("verify")
    v.add_argument("--manifest", required=True)
    v.add_argument("--restored", required=True)
    v.add_argument("--sample-percent", type=float, default=100)
    v.add_argument("--previous-manifest")
    v.add_argument("--seed", type=int, default=int(time.time()) // 86400)
    v.add_argument("--restore-seconds", type=float, required=True)
    v.add_argument("--rto-minutes", type=float, default=240)
    v.add_argument("--rpo-hours", type=float, default=24)
    v.add_argument("--out", default="evidence")
    d = sub.add_parser("demo")
    d.add_argument("--out", default="out/evidence")
    a = ap.parse_args(argv)

    if a.cmd == "manifest":
        mf = build_manifest(Path(a.source), a.label)
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(mf, indent=1) + "\n")
        print(f"{mf['file_count']} files, {mf['total_bytes']} bytes -> {a.out}")
        return 0
    if a.cmd == "verify":
        mf = json.loads(Path(a.manifest).read_text())
        prev = json.loads(Path(a.previous_manifest).read_text()) if a.previous_manifest else None
        sample = None if a.sample_percent >= 100 else choose_sample(mf, a.sample_percent, a.seed, prev)
        res = assess(mf, verify(mf, Path(a.restored), sample), a.restore_seconds, a.rto_minutes, a.rpo_hours)
    else:
        res = demo(Path(a.out))
        caught = res["missing"] == ["ops/runbooks/doc039.txt"] and res["hash_mismatch"] == ["finance/2026/doc007.txt"]
        print(f"demo injected 1 corrupted and 1 missing file; detected exactly those: {caught}")
        path = write_evidence(res, Path(a.out))
        print(f"{res['overall']} (expected FAIL). Evidence: {path}")
        return 0 if caught and res["overall"] == "FAIL" else 1
    path = write_evidence(res, Path(a.out))
    print(f"{res['overall']}: {res['verified']}/{res['checked']} verified, missing {len(res['missing'])}, "
          f"hash mismatch {len(res['hash_mismatch'])}, RPO met {res['rpo_met']}, RTO met {res['rto_met']}. Evidence: {path}")
    return 0 if res["overall"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
