# Design Decisions

| Tool | Decision | Alternative | Reason |
|---|---|---|---|
| Backup | Manifest at backup time, compare after restore | Trust the backup product's verification | Independent of the product, and it proves the data matches what was protected, not just that the job's own check passed |
| Backup | Random sample + every changed file | Full restore every time | Full restores of large shares take too long monthly. Changed files are where new problems appear, and random coverage catches the rest over time |
| Backup | RPO and RTO in the same record | Separate DR test | One record answers "can we recover, how much do we lose, how long does it take" |
| Backup | Exit code 2 on FAIL | Always exit 0 | A scheduler or pipeline can alert on a failed test without parsing text |
| Patching | Compare build.UBR | Check a KB list | Cumulative updates supersede each other, and a build number never goes down |
| Patching | Unreachable = not compliant | Skip unreachable hosts | Hosts that can't be inventoried are often exactly the ones being missed |
| Patching | Tier deadlines from naming convention | Per-host config | No list to maintain; the role is in the name (`SITE1-DC-001`) |
| Patching | CIM over WinRM, read-only | Agent | Works with built-in Windows remoting; no install, no change to the host |
| Onboarding | Site fabric as JSON | Spreadsheet / wiki | Machine-checkable; the same file validates, renders and records what is in use |
| Onboarding | Validate before render | Render and review | Catches IP clashes and port conflicts before anyone touches a device |
| Onboarding | Render both sides | Access side only | Most onboarding mistakes are on the distribution side (wrong port, wrong port-channel) |
| Onboarding | Post-checks parse show output | Manual eyeballing | Same checks every time, a recorded result, and one obvious place to add checks |
| Onboarding | Vault placeholders | Encrypted secrets in Git | Nothing secret in the repository, even encrypted |
| All | Python standard library | pandas, Jinja2, Netmiko | Runs on a locked-down jump host with no package installs. Netmiko or Nornir can feed `postcheck` when available |
