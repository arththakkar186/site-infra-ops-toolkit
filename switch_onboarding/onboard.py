"""Access switch onboarding: validate the request against the site fabric, render both sides of the config,
produce the change plan, and verify the result from show-command output.

    python3 -m switch_onboarding.onboard plan      --request switch_onboarding/samples/new-switch.json --out out/onboarding
    python3 -m switch_onboarding.onboard postcheck --request switch_onboarding/samples/new-switch.json \
            --show switch_onboarding/samples/postcheck-good.txt

Secrets (RADIUS/TACACS keys, SNMP auth/priv keys, local admin hash) are never in the repo: the rendered config
contains placeholders such as {{RADIUS_KEY}} that the deployment tool fills from the vault.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOSTNAME = re.compile(r"^(?P<site>[A-Z0-9]+)-AS-(?P<floor>\d{1,2}F|B\d)-(?P<n>\d{2})$")


def load(p) -> dict:
    return json.loads(Path(p).read_text())


def expand(rng: str) -> list[str]:
    """'1/0/1-4' -> ['1/0/1', '1/0/2', '1/0/3', '1/0/4']"""
    prefix, _, span = rng.rpartition("/")
    lo, _, hi = span.partition("-")
    return [f"{prefix}/{i}" for i in range(int(lo), int(hi or lo) + 1)]


# --------------------------------------------------------------------------------------------- validation

def validate(req: dict, fab: dict) -> list[str]:
    e: list[str] = []
    m = HOSTNAME.match(req["hostname"])
    if not m:
        e.append(f"hostname '{req['hostname']}' does not follow SITE-AS-<floor>-NN")
    elif m["site"] != fab["site"]:
        e.append(f"hostname site {m['site']} is not {fab['site']}")
    model = fab["models"].get(req["model"])
    if not model:
        return e + [f"model {req['model']} is not a supported access model"]

    mg = fab["management"]
    net = ipaddress.ip_network(mg["subnet"])
    ip = ipaddress.ip_address(req["mgmt_ip"])
    if ip not in net or ip in (net.network_address, net.broadcast_address):
        e.append(f"management IP {ip} is not a host address in {net}")
    if str(ip) in mg["reserved"] or str(ip) in mg["in_use"]:
        e.append(f"management IP {ip} is reserved or already in use")

    used_pc = {pc for d in fab["distribution"].values() if isinstance(d, dict) for pc in d["used_port_channels"]}
    if req["port_channel"] in used_pc:
        e.append(f"port-channel {req['port_channel']} is already used on the distribution pair")

    peers = set()
    for u in req["uplinks"]:
        d = fab["distribution"].get(u["peer"])
        if not isinstance(d, dict):
            e.append(f"uplink peer {u['peer']} is not a distribution switch")
            continue
        if u["peer_port"] not in d["free_ports"]:
            e.append(f"{u['peer']} {u['peer_port']} is not a free port")
        if u["local_member"] > req["stack_members"] or u["local_port"] > model["uplinks"]:
            e.append(f"local uplink {u['local_member']}/1/{u['local_port']} does not exist on this stack")
        peers.add(u["peer"])
    if len(peers) < 2:
        e.append("uplinks must land on both distribution switches (no single point of failure)")
    if len({u["local_member"] for u in req["uplinks"]}) < min(2, req["stack_members"]):
        e.append("uplinks must use different stack members")

    seen: dict[str, str] = {}
    for p in req["ports"]:
        prof = fab["port_profiles"].get(p["profile"])
        if not prof:
            e.append(f"unknown port profile {p['profile']}")
            continue
        for port in expand(p["range"]):
            member, _, num = port.split("/")
            if int(member) > req["stack_members"] or int(num) > model["access_ports"]:
                e.append(f"port {port} does not exist on a {req['stack_members']}-member {req['model']} stack")
            if port in seen:
                e.append(f"port {port} assigned twice ({seen[port]} and {p['profile']})")
            seen[port] = p["profile"]
        for v in (prof.get("access_vlan"), prof.get("voice_vlan"), prof.get("native_vlan"), *prof.get("allowed", [])):
            if v is not None and str(v) not in fab["vlans"]:
                e.append(f"profile {p['profile']} uses VLAN {v}, which is not in the fabric")
    expected = req["stack_members"] * model["access_ports"]
    if len(seen) != expected:
        e.append(f"{len(seen)} of {expected} access ports have a profile; every port needs one (use 'unused')")
    return e


# --------------------------------------------------------------------------------------------- rendering

def trunk_vlans(fab: dict) -> str:
    return ",".join(sorted((v for v in fab["vlans"] if v != "999"), key=int))


def render_access(req: dict, fab: dict) -> str:
    s, mg, model = fab["services"], fab["management"], fab["models"][req["model"]]
    L = [f"! {req['hostname']}  {req['model']} x{req['stack_members']}  {req['location']}  change {req['change']}",
         f"hostname {req['hostname']}", f"ip domain name {fab['domain']}", "no ip domain lookup",
         "service timestamps log datetime msec localtime show-timezone", "service password-encryption",
         "username netadmin privilege 15 secret 9 {{LOCAL_ADMIN_HASH}}", "!", "aaa new-model",
         f"aaa group server tacacs+ TACACS", *[f" server name TACACS-{i}" for i in range(1, len(s["tacacs"]) + 1)],
         f"aaa group server radius {s['aaa_group']}", *[f" server name RADIUS-{i}" for i in range(1, len(s["radius"]) + 1)],
         "aaa authentication login default group TACACS local", "aaa authorization exec default group TACACS local",
         "aaa accounting commands 15 default start-stop group TACACS",
         f"aaa authentication dot1x default group {s['aaa_group']}", f"aaa authorization network default group {s['aaa_group']}",
         f"aaa accounting dot1x default start-stop group {s['aaa_group']}", "dot1x system-auth-control", "!"]
    for i, ip in enumerate(s["tacacs"], 1):
        L += [f"tacacs server TACACS-{i}", f" address ipv4 {ip}", " key {{TACACS_KEY}}"]
    for i, ip in enumerate(s["radius"], 1):
        L += [f"radius server RADIUS-{i}", f" address ipv4 {ip} auth-port 1812 acct-port 1813", " key {{RADIUS_KEY}}"]
    L += ["!", *[f"ntp server {n}" for n in s["ntp"]], f"logging host {s['syslog']}", "logging trap informational",
          f"snmp-server group {s['snmp_group']} v3 priv read NETMON-VIEW access SNMP-ACL",
          f"snmp-server user {s['snmp_user']} {s['snmp_group']} v3 auth sha {{{{SNMP_AUTH}}}} priv aes 128 {{{{SNMP_PRIV}}}}",
          "snmp-server view NETMON-VIEW iso included", "!",
          "spanning-tree mode rapid-pvst", "spanning-tree portfast bpduguard default", "spanning-tree extend system-id",
          "udld enable", "errdisable recovery cause bpduguard", "errdisable recovery interval 300",
          f"ip dhcp snooping vlan {','.join(map(str, s['dhcp_snooping_vlans']))}", "no ip dhcp snooping information option",
          "ip dhcp snooping", f"ip arp inspection vlan {','.join(map(str, s['dhcp_snooping_vlans']))}", "!"]
    for vid, name in sorted(fab["vlans"].items(), key=lambda x: int(x[0])):
        L += [f"vlan {vid}", f" name {name}"]
    L += ["!", f"interface Vlan{mg['vlan']}", " description MGMT", f" ip address {req['mgmt_ip']} {ipaddress.ip_network(mg['subnet']).netmask}",
          " no shutdown", f"ip default-gateway {mg['gateway']}", "!",
          f"interface Port-channel{req['port_channel']}", " description UPLINK-DS1-DS2", " switchport mode trunk",
          f" switchport trunk allowed vlan {trunk_vlans(fab)}", " ip dhcp snooping trust", " ip arp inspection trust", "!"]
    for u in req["uplinks"]:
        port = model["uplink_prefix"].format(member=u["local_member"]) + str(u["local_port"])
        L += [f"interface {port}", f" description UPLINK {u['peer']} {u['peer_port']}", " switchport mode trunk",
              f" switchport trunk allowed vlan {trunk_vlans(fab)}", f" channel-group {req['port_channel']} mode active",
              " udld port aggressive", " ip dhcp snooping trust", " ip arp inspection trust", " no shutdown", "!"]
    for p in req["ports"]:
        prof = fab["port_profiles"][p["profile"]]
        member, mid, rng = p["range"].split("/")
        L.append(f"interface range GigabitEthernet{member}/{mid}/{rng.replace('-', ' - ')}")
        L.append(f" description {p['profile'].upper()}")
        if prof.get("trunk"):
            L += [" switchport mode trunk", f" switchport trunk native vlan {prof['native_vlan']}",
                  f" switchport trunk allowed vlan {','.join(map(str, prof['allowed']))}", " spanning-tree portfast trunk"]
        else:
            L += [" switchport mode access", f" switchport access vlan {prof['access_vlan']}"]
            if prof.get("voice_vlan"):
                L.append(f" switchport voice vlan {prof['voice_vlan']}")
            if prof.get("portfast"):
                L.append(" spanning-tree portfast")
            if prof.get("dot1x"):
                L += [" authentication host-mode multi-auth", " authentication order dot1x mab", " authentication priority dot1x mab",
                      " authentication port-control auto", " mab", " dot1x pae authenticator"]
            if not prof.get("shutdown"):
                L.append(" ip dhcp snooping limit rate 20")
        L.append(" shutdown" if prof.get("shutdown") else " no shutdown")
        L.append("!")
    L += ["ip ssh version 2", "ip access-list standard SNMP-ACL", " permit 10.10.5.0 0.0.0.255",
          "ip access-list standard VTY-ACL", " permit 10.10.5.0 0.0.0.255", " permit 10.20.99.0 0.0.0.255",
          "line vty 0 15", " access-class VTY-ACL in", " transport input ssh", " exec-timeout 10 0",
          "line con 0", " exec-timeout 10 0", "!", "banner login ^Authorised access only. Activity is logged.^", "end"]
    return "\n".join(L) + "\n"


def render_distribution(req: dict, fab: dict) -> dict[str, str]:
    out = {}
    for u in req["uplinks"]:
        out.setdefault(u["peer"], [f"! {u['peer']}: add {req['hostname']} (change {req['change']})",
                                   f"interface Port-channel{req['port_channel']}", f" description DOWNLINK {req['hostname']}",
                                   " switchport mode trunk", f" switchport trunk allowed vlan {trunk_vlans(fab)}",
                                   " spanning-tree guard root", "!"])
        out[u["peer"]] += [f"interface {u['peer_port']}", f" description DOWNLINK {req['hostname']} member {u['local_member']}",
                           " switchport mode trunk", f" switchport trunk allowed vlan {trunk_vlans(fab)}",
                           f" channel-group {req['port_channel']} mode active", " udld port aggressive", " no shutdown", "!"]
    return {k: "\n".join(v) + "\n" for k, v in out.items()}


def plan(req: dict, fab: dict) -> str:
    fam = fab["models"][req["model"]]["family"]
    return f"""# Change plan: onboard {req['hostname']} ({req['change']})

| | |
|---|---|
| Model | {req['model']} x {req['stack_members']} (target IOS XE {fab['target_software'][fam]}) |
| Location | {req['location']} |
| Management | VLAN {fab['management']['vlan']}, {req['mgmt_ip']} |
| Uplinks | Po{req['port_channel']} (LACP) to {', '.join(f"{u['peer']} {u['peer_port']}" for u in req['uplinks'])} |

## Before the window
1. Stack cabled and powered on the bench; `show switch` shows {req['stack_members']} members, ready.
2. Software at target: `show version` (install mode, `install add file ... activate commit` if not).
3. Load `{req['hostname']}.cfg` with the vault placeholders filled; `show running-config | include {{{{` returns nothing.
4. Add the IPAM record ({req['mgmt_ip']}) and the monitoring/NetBox device, status "planned".

## In the window
1. Pre-checks on both distribution switches: `show etherchannel summary`, `show spanning-tree root`, `show interfaces status | include {req['uplinks'][0]['peer_port'].split('Ethernet')[1]}`.
2. Apply the distribution config (Po{req['port_channel']} and member ports).
3. Rack, cable uplinks, power on.
4. Post-checks (`postcheck` subcommand): Po{req['port_channel']} bundled with both members, trunk allows {trunk_vlans(fab)},
   spanning-tree root is the distribution switch, NTP synchronised, TACACS login works.
5. Test one user port (802.1X), one printer port, one AP port.

## Rollback
Shut Po{req['port_channel']} on both distribution switches and remove the config added in step 2. The rest of the site is unaffected,
because the change only adds a new port-channel and new member ports.
"""


# --------------------------------------------------------------------------------------------- post-checks

def postcheck(req: dict, fab: dict, show: str) -> list[tuple[str, bool, str]]:
    r: list[tuple[str, bool, str]] = []
    po = req["port_channel"]
    m = re.search(rf"^{po}\s+Po{po}\((\w+)\)\s+LACP\s+(.+)$", show, re.M)
    bundled = re.findall(r"(\S+)\(P\)", m[2]) if m else []
    r.append((f"Po{po} up and in use (SU)", bool(m and m[1] == "SU"), m[1] if m else "not found"))
    r.append((f"Po{po} has {len(req['uplinks'])} bundled members", len(bundled) == len(req["uplinks"]), ", ".join(bundled) or "none"))

    section = re.search(r"Vlans allowed on trunk\n(.*?)(?:\n\s*\n|\Z)", show, re.S)
    t = re.search(rf"^Po{po}\s+(\S+)\s*$", section[1], re.M) if section else None
    allowed = t[1] if t else ""
    r.append(("trunk allows the fabric VLANs", allowed == trunk_vlans(fab), allowed or "not found"))

    root = re.search(r"This bridge is the root", show)
    r.append(("this switch is NOT spanning-tree root", root is None, "local root!" if root else "root is upstream"))

    ntp = re.search(r"Clock is (synchronized|unsynchronized)", show)
    r.append(("NTP synchronised", bool(ntp and ntp[1] == "synchronized"), ntp[1] if ntp else "not found"))

    fam = fab["models"][req["model"]]["family"]
    ver = re.search(r"Cisco IOS XE Software, Version (\S+)", show)
    r.append((f"software {fab['target_software'][fam]}", bool(ver and ver[1] == fab["target_software"][fam]), ver[1] if ver else "not found"))
    return r


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="onboard")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("validate", "plan", "postcheck"):
        s = sub.add_parser(name)
        s.add_argument("--request", required=True)
        s.add_argument("--fabric", default=str(HERE / "fabric.json"))
        if name == "plan":
            s.add_argument("--out", default="out/onboarding")
        if name == "postcheck":
            s.add_argument("--show", required=True, help="text file with show command output")
    a = ap.parse_args(argv)
    req, fab = load(a.request), load(a.fabric)

    if a.cmd == "postcheck":
        results = postcheck(req, fab, Path(a.show).read_text())
        for name, ok, detail in results:
            print(f"{'PASS' if ok else 'FAIL'}  {name:42} {detail}")
        return 0 if all(ok for _, ok, _ in results) else 1

    errors = validate(req, fab)
    for x in errors:
        print(f"INVALID  {x}")
    if errors:
        return 1
    if a.cmd == "validate":
        print(f"{req['hostname']}: request is valid against the {fab['site']} fabric")
        return 0
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{req['hostname']}.cfg").write_text(render_access(req, fab))
    for peer, cfg in render_distribution(req, fab).items():
        (out / f"{peer}_add_{req['hostname']}.cfg").write_text(cfg)
    (out / f"{req['hostname']}_change_plan.md").write_text(plan(req, fab))
    print(f"{req['hostname']}: valid; wrote config, distribution changes and change plan to {out}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
