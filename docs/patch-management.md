# Patch Management

## Monthly cycle

| Day (from Patch Tuesday) | Ring | Scope |
|---|---|---|
| 0 | Review | Read the release notes and known issues; update `baseline.json` minimum builds |
| +1 | Ring 0 | IT test servers and IT workstations |
| +3 | Ring 1 | Non-critical servers (APP, PRN), 10% of workstations |
| +7 | Ring 2 | Remaining workstations; critical servers in their maintenance window |
| +14 | Deadline | **Critical tier** (DC, FS, SQL) |
| +30 | Deadline | **Standard tier** |

Workstations update through Windows Update for Business or Intune update rings. Servers update through WSUS or Azure Update
Manager in maintenance windows, one domain controller at a time, and SQL failover-cluster nodes one by one.

## Compliance rules (`patch_report.py`)

| State | Rule |
|---|---|
| COMPLIANT | Build.UBR ≥ the cycle's minimum for that OS (a later cumulative update counts) |
| IN GRACE | Below minimum, deadline not reached |
| NON-COMPLIANT | Below minimum, deadline passed |
| UNKNOWN | Inventory failed (WinRM, firewall, offline). **Counts as not compliant** |
| UNSUPPORTED | OS not in the baseline: end of support, so it can't be patched (migration or exception needed) |

Flags: **reboot pending**, which means the update isn't active until the computer reboots, and **uptime > 35 days**, which means last month's cycle was probably missed.

Why build numbers and not KB lists: cumulative updates replace each other, so "is KB X installed" gives false negatives
when a later update is present. The OS build plus UBR (update build revision) is one number that only goes up.

## Exceptions

An exception needs an owner, a reason (vendor support, application compatibility), compensating controls (isolation,
extra monitoring) and an end date of 90 days maximum. It is recorded in the ticketing system, and the computer stays visible in the report.

## Reporting

Monthly report to the infrastructure lead: compliance % by tier, the action list (non-compliant first, critical tier first),
the unreachable list, and unsupported OS with a migration plan.
