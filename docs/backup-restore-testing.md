# Backup and Restore Testing

## Backup design: 3-2-1-1-0

| Rule | Implementation (reference) |
|---|---|
| **3** copies | Production + local backup repository + off-site copy |
| **2** media | Local disk repository and cloud object storage |
| **1** off-site | Cloud copy job, daily |
| **1** immutable / offline | Object lock (immutability) on the cloud copy for 30 days, so ransomware with admin rights can't delete it |
| **0** errors | Verified by test restores, not by job status |

## Recovery objectives by tier

| Tier | Examples | RPO | RTO | Test frequency | Sample |
|---|---|---:|---:|---|---|
| 1 | Domain controllers, file shares with finance data, SQL | 24 h | 4 h | Monthly | 10% random + all changed since last test |
| 2 | Application servers, print | 24 h | 8 h | Quarterly | 5% + changed |
| 3 | Archive | 7 d | 48 h | Twice a year | 1% |

Domain controllers are tested by restoring to an **isolated network** and checking AD services (authoritative restore is never tested on the production network).

## Procedure

1. **At backup time**, a post-job script writes the manifest: `python3 -m backup.restore_verify manifest --source <path> --out manifests/<dataset>.json`.
2. **Restore** the latest restore point of the sample to an isolated target (separate volume or VM, no production network access). Record the start and end times.
3. **Verify**: `python3 -m backup.restore_verify verify --manifest ... --restored ... --restore-seconds <n> --rto-minutes 240 --rpo-hours 24 --sample-percent 10 --previous-manifest <last verified>`.
4. **Application check** (for databases and VMs): mount the database or boot the VM and run a basic query or login.
5. **Sign off**: the tester and a reviewer sign the Markdown record, which is filed with the quarterly DR evidence.
6. **Destroy** the restored copy.

## What "pass" means

| Check | Pass |
|---|---|
| Every sampled file restored | No `missing` |
| Same size and SHA-256 as at backup time | No `size_mismatch` / `hash_mismatch` |
| Restore point age at test time | ≤ RPO |
| Restore duration | ≤ RTO |

A hash mismatch is investigated as a possible corruption in the backup chain (storage, deduplication, or a file changed during
backup without VSS). The dataset is re-tested at 100% before the next cycle.

## Why hashes and not "job succeeded"

A successful job only shows the data was written. Silent corruption, excluded paths, permission-denied files and broken backup
chains all show up only when the data is read back and compared with what was there. The demo (`restore_verify demo`) injects
one corrupted file and one missing file to show the verifier catches both.
