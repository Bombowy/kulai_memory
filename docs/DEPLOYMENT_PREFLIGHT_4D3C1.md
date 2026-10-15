# TASK 4D.3C.1 — Windows facts and authorized deployment sequence

This is an operator handoff for the existing remote-access implementation. It
authorizes no VPS, firewall, DNS, certificate, service or main-DB operation.
Measured performance and final status are in `wynik_4d3c1.txt`. Do not treat this
document or an unfilled candidate as production approval.

Follow-up measurements, the Uvicorn burst-queue fix, journal review budgets and
the concrete redacted VPS collector are in `REMOTE_PERFORMANCE_HANDOFF_4D3C2A.md`.
PostgreSQL binding remains a separately approved operation described in
`POSTGRES_LOOPBACK_CHANGE.md`; no production change was made by the preflights.

The completed benchmark is also preserved as
`docs/SECURITY_GATE_BENCHMARK_4D3C1.json`. It ran 600.011 seconds of paced PCM at
the existing Android 6400-byte/200ms cadence, with real mTLS WSS and 1000 synthetic
sessions, one Memory save and 3003 live auth lookups. This is a single-client
loopback result. Per-frame auth p95 was 172.622ms; Python used 59.93% of one core.
Full fingerprints dominate the measured auth cost. Multi-client load, old/large
journal behavior and the real tunnel remain deployment checks; no auth cache or
revocation delay was introduced to improve these results.

## Observed Windows host (2026-10-14)

- Windows 11 Home, build 26200, x64; 16 logical CPUs, approximately 31.84 GiB RAM.
- Backend SDK Python 3.14.5, pinned Uvicorn 0.54.0; PostgreSQL service 18.6,
  automatic start. pg_dump/pg_restore binaries are also 18.6.
- Ethernet is Up, IPv4 192.168.1.20/24, default gateway 192.168.1.1, Public profile.
  Only Ethernet, disconnected Bluetooth and loopback were observed. No active VPN,
  WireGuard service/driver or standard installation files were found.
- Current routes include 192.168.1.0/24, 169.254.0.0/16 and loopback. Candidate
  10.203.71.1/32 and 10.203.71.2/32 do not conflict with the current Windows table;
  they are **not** confirmed production addresses. VPS and future VPN routes remain
  unverified. Default route 0/0 is not a conflicting LAN prefix to import into WG.
- TCP 8000 and 8443 were free before synthetic tests; FastAPI health was unavailable.
  No production remote launcher process or KulAI autostart service/task was found.
  Temporary test listeners use random numeric loopback ports only.
- Ollama is reachable at 127.0.0.1:11434, version 0.40.0, with a logon Startup link.
  Configured models are BGE-M3, Qwen3.5:9b and Whisper large-v3. No real inference
  or model-content inspection is performed by this preflight.
- PostgreSQL listens on 0.0.0.0:5432 and [::]:5432 (`listen_addresses='*'`). Current
  redacted HBA permits only loopback, with SCRAM. This is not proof of public
  reachability, nor does it satisfy the package's all-services-loopback check.
  Review binding and effective firewall isolation before installing any tunnel.
- Effective ActiveStore firewall: all profiles enabled, inbound Block, outbound
  Allow. No KulAI-Memory-Bridge-v1 rules exist. Detailed port-filter enumeration
  returned access denied; unrelated broad allow rules have not been verified.
  Persistent profile values are NotConfigured; do not confuse these with effective
  ActiveStore Block. The dry-run bridge script now checks complete ActiveStore
  profiles, rather than falsely rejecting inherited defaults. Allow/missing-profile
  cases remain fail-closed; regression tests use mocked OS inventory, never Apply.
  No firewall was changed.
- Balanced plan, S3/hibernate/hybrid available. AC sleep timeout is 0 (never);
  DC is 600 seconds. Sleep, outage, reboot and logoff were not forced.
- Main has canonical `kulai_memory_0004`, no security schema. Config defaults to
  SECURE but is not explicitly set; `dev.py` can explicitly choose LOCAL_DEV.
  Actual managed main security startup was tested read-only: ready=false,
  remote_ready=false. No AI runtime/listener was started by this check.

## Recovery gate readiness

`C:/ProgramData/KulAI-Memory/recovery` does not exist and KULAI_RECOVERY_GATE is
unset. C: is fixed NTFS and has enough space; restrictive NTFS ACLs are technically
available and temporary test-gate ACL verification passes. Production provisioning
was not attempted. ProgramData's broad parent permissions must not be inherited
into a production gate/key folder. Use the final service identity with only its
SID, SYSTEM and Administrators admitted by the existing checker.

The only visible normal data volume is C:, which also holds PostgreSQL data at
`C:/Program Files/PostgreSQL/18/data`. A journal outside that directory is excluded
from logical pg_dump, but independence from whole-volume/whole-host snapshots is
**not verified**. Backup-related system/cloud tasks do not establish their exclusion
policy. Obtain an operator-confirmed independent persistent location/backup policy,
or require the documented manual new-CLOSED/full-recovery procedure after any
whole-host restore. Never restore an old OPEN seal with a database backup.

No production initialize/recover/release was run. All future SECURE runtimes and
pairing administration must use the same absolute gate path; mismatch closes
remote access. No service/autostart should create/release a gate. Normal boot may
start remote components only after matching OPEN checkpoint verification. The
current host does not automatically start these remote components.

## VPS diagnostic commands — not executed without exact-host SSH authorization

Confirm host/IP, SSH user/port and the independently verified host-key fingerprint.
Use existing verified known_hosts, BatchMode and StrictHostKeyChecking=yes. Never
accept a new/changed key automatically, upload keys, or guess which server to use.
Run the existing `ops/remote_access/ubuntu/preflight.sh` in the operator's terminal.
The following supplementary commands read state only; missing privileges remain
NOT VERIFIED, rather than triggering sudo/password collection or installation:

```sh
cat /etc/os-release
uname -sr
nginx -v
nginx -V
openssl version
ip -brief address
ip -4 route show
ip -6 route show
ss -lntup
ls -l /etc/nginx/sites-enabled
systemctl is-active nginx
systemctl list-unit-files 'wg-quick@*' '*nginx*' '*certbot*' '*acme*' '*kulai*' --no-pager
systemctl list-units --type=service --state=running --no-legend --no-pager --plain | awk '{print $1,$2,$3,$4}'
systemctl list-timers --all '*certbot*' '*acme*' --no-pager
command -v wg
wg --version
wg show interfaces
sudo -n wg show all latest-handshakes
sudo -n wg show all transfer
sudo -n ufw status verbose
sudo -n nft list tables
```

The operator must privately inspect `sudo -n nft list ruleset`,
`sudo -n iptables -S`, `sudo -n ip6tables -S`, and expanded `sudo -n nginx -T`.
Do not paste/store their complete content in this repository or report. Return only
domain/listen/default-owner, reserved map/zone collisions, effective TLS policy,
peer/port firewall conclusions and unresolved items. Do not request `systemctl cat`,
environment dumps, private logs, `wg showconf`, `wg ... dump` or private-key content.
Confirm whether an existing configuration backup is complete and restorable; listing
backup paths is not backup verification. Do not create/replace that backup here.

Public addresses require provider-console confirmation; local NIC addresses alone
do not prove public/NAT ownership. For the operator-confirmed domain only:

```sh
dig +short A '<CONFIRMED_DOMAIN>'
dig +short AAAA '<CONFIRMED_DOMAIN>'
openssl x509 -in '<CONFIRMED_PUBLIC_CERT_PATH>' -noout -dates -issuer -ext subjectAltName -fingerprint -sha256
```

Do not execute placeholders. Inspect existing ACME ownership/renewal locally without
reading account private keys. Do not run global reload, apt install, wg-quick, DNS
updates, firewall Apply, cert issuance or service changes during inventory.

## Candidate and validator handoff

The external operator directory recorded in the report contains deployment.json,
inventory.json and observations.json. Unknown real domain/IP/SAN/cert ownership is
null, not a fabricated production value. Therefore the existing package renderer
and validator cannot produce approved production candidates yet: BLOCKED.
Do not substitute test addresses to manufacture a success result.

After collecting exact observations, fill and freeze that external configuration:

```text
<BACKEND_PYTHON> -m ops.remote_access.package --config <EXTERNAL_DEPLOYMENT_JSON> --write-candidates <NEW_EMPTY_CANDIDATE_DIRECTORY>
<BACKEND_PYTHON> -m ops.remote_access.package --config <EXTERNAL_DEPLOYMENT_JSON> --validate-candidates <CANDIDATE_DIRECTORY>
<BACKEND_PYTHON> -m ops.remote_access.validator --config <EXTERNAL_DEPLOYMENT_JSON> --inventory <EXTERNAL_INVENTORY_JSON> --existing-nginx <PROTECTED_EXPANDED_CONFIG> --candidates <CANDIDATE_DIRECTORY> --certificates <PUBLIC_CERT_PATHS_JSON> --openssl <OPENSSL_EXE>
```

Rendering writes only a new external candidate directory, not /etc/nginx. Expanded
config stays protected outside Git; public certificate specs contain paths to certs
and CA chains, never keys. Complete combined Ubuntu `nginx -t` remains mandatory.
No real expanded Nginx config has been verified by this task without SSH approval.

Use shared_ip=true only if the confirmed existing explicit default, TLS policy,
domain/listen ownership, IPv4/IPv6 routing and namespace allow it. Preserve every
other project's default/vhost and files. Otherwise BLOCKED for operator review;
do not buy an additional IP or alter a default as an automatic workaround.
Check A and AAAA routes independently: the Android origin must never terminate at
another application's wildcard certificate/vhost. Unknown IPv6 is not safe evidence.

## TASK 4D.3C.2 — separate approval gates, not executed commands

Every numbered operation below requires its own explicit approval before real
infrastructure/data writes. Stop and retain the previous safe state on any failure.

1. **Freeze observations and config.** Approve exact VPS/domain, /32 pair, UDP port,
   service identities, existing files/default ownership and external rollback paths.
   Confirm no changed observations; do not mutate existing services in preflight.
2. **Main backup.** Stop approved writers/clients, run strict read-only doctor/full
   snapshot, then approved `scripts/db_backup.py --output <NEW_EXTERNAL_DUMP>` and
   `scripts/db_restore_smoke.py <DUMP>`. Restore only into capability-owned DB, never
   main. Preserve all canonical/vector/tombstone/security fingerprints.
3. **Protected gate.** Approve final independent storage, owner and restrictive ACL;
   provision and verify as the final service identity. Only then approve
   `scripts/security_recovery.py initialize --gate <ABS_GATE> --apply
   --confirm-database <EXACT_DB>`. It must remain CLOSED. No remote listener yet.
4. **Security v3 migration.** Separately approve
   `scripts/security_schema.py upgrade --confirm-main-security-schema
   --backup-output <NEW_EXTERNAL_PRE_MIGRATION_DUMP>`. Existing guarded workflow
   performs actual restore verification before transactional DDL. Re-run doctor and
   full fingerprints. Canonical head must stay v4; security must become v3.
   A pre-migration archive without security v3 cannot serve as recovery v3 evidence.
   Create a separately approved **post-migration** verified full backup/manifest
   with `security_recovery.py backup` before first controlled recovery/release.
5. **PKI.** Approve offline encrypted private CA, distinct ingress serverAuth and
   gateway clientAuth keys, exact configured DNS SANs, protected key paths/ACL,
   expiry alarms and rotation. Use the existing CERTIFICATE_LIFECYCLE.md procedure.
   No test CA in Android release. No claimed online CRL enforcement; compromised
   identity/CA requires trust replacement and terminating old connections.
6. **WireGuard.** Approve installation and exact new dedicated interface on both
   hosts only after route inventory. Windows initiates outbound to confirmed VPS;
   peer AllowedIPs are exact /32, keepalive25, no home-LAN or 0/0 routing.
7. **Firewall.** Approve exact new owned Windows and VPS rules after privileged
   read-only review. Windows bridge TCP8443 only on WG IP from VPS /32; deny other
   sources and private services. VPS UDP only on confirmed endpoint; peer traffic
   must not reach DB/Ollama/other applications. Do not overwrite unrelated rules.
   Inspect bridge.ps1 Setup dry-run before separate Apply confirmation.
8. **Real handshake.** Test two-host WG handshake, routes, stop/restart, NAT timeout
   and denial of all non-ingress services. Local loopback tests are insufficient.
9. **SECURE ingress and bridge.** Separately approve first controlled recovery and
   manual release against the post-migration verified backup, revoking recovered
   credentials. All writers use the same gate. Start the existing manual combined
   launcher with confirmed configuration; listener remains numeric loopback and
   bridge only the actual verified WG address. No autostart installation here.
10. **Nginx integration.** Approve only new KulAI candidate references/files. Test
    complete shared config/default/SNI/Host/mTLS/TLS/route/header policy and other
    projects in isolation. Never claim ownership of a default implicitly.
11. **Negative tests before public opening.** Wrong/missing gateway cert, wrong
    server CA/SAN, no bearer, revoked token, bad Host/Origin, LOCAL_DEV/no schema/
    closed gate, unknown/admin routes, unavailable backend and uncertain refresh.
    Verify no retries, no private logs and no route to other services.
12. **Public activation.** Independently approve DNS A/AAAA, public ACME/certificate,
    only the selected vhost's enablement/reload and public HTTPS/WSS access. Check
    effects on every existing project. Roll back immediately on mismatch.
13. **Android.** Approve new device pairing, then physical Internet LTE/5G and
    Wi-Fi/LTE NOTE/ASK/MEMORY/TTS, restart and disconnect smoke. No uninstall/data
    clear, TLS exception, origin credential forwarding or uncertain write replay.
14. **Performance and recovery.** Repeat real-tunnel paced 10-minute PCM, multi-client
    load, revocation/refresh uncertainty, certificate rotation and approved recovery
    drill. Verify independent gate rollback policy and journal capacity/alerts.
15. **GO/NO-GO.** Compare main after-snapshot and all real test evidence. Only explicit
    final approval may declare GO; unresolved missing data or a failed check blocks it.

## Immediate rollback

Close only KulAI routing and stop its bridge/runtime; preserve the gate and audit.
Remove only confirmed owned rule/tunnel/vhost changes after their own rollback
approval. Keep other projects running. Do not downgrade schema, edit canonical data,
delete audit/replay rows, replay writes/refresh, or restore an old OPEN journal.
Any DB restore requires closed ingress, verified full restore, controlled credential
recovery, manual release and fresh approved pairing. A code rollback without gate
support must keep remote access disabled.

## Bootstrap firewall sequencing and concrete operation templates

These are **not executed** commands. Replace every placeholder from frozen real
inventory; each changing action requires its own approval and saved ownership
receipt. Do not install/upgrade an existing Nginx package used by other projects.

Before approval, read package availability with `apt-cache policy wireguard-tools`
on the confirmed Ubuntu. A later approved binary installation may use
`sudo apt-get install wireguard-tools`. On Windows verify the selected official
installer with `Get-AuthenticodeSignature -FilePath <VERIFIED_INSTALLER>` before
the separately approved install. No PATH change is needed: use absolute paths.
Tunnel activation is a separate operation from binary installation.

The bridge verifier requires an Up WG adapter, while the host already has unrelated
wildcard listeners. Do not bring up a new tunnel and assume existing broad firewall
allows are safe. First approve a temporary deployment hold on the **confirmed** WG
local address before first activation, then approve adapter activation, validate it,
install final narrow rules and remove the hold only after verifying final isolation:

```powershell
# Templates ONLY, after per-operation approval and exact ownership/conflict checks.
New-NetFirewallRule -Name 'KulAI-Memory-Deployment-Hold-v1' -Direction Inbound -Action Block -Protocol Any -LocalAddress <WINDOWS_WG_IP> -RemoteAddress Any -Profile Any
& '<WIREGUARD_EXE>' /installtunnelservice '<PROTECTED_DEDICATED_WG_CONF>'
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File ops/remote_access/windows/inspect-interface.ps1 -InterfaceAlias <WG_ALIAS> -Address <WINDOWS_WG_IP> -PeerAddress <VPS_WG_IP>
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File ops/remote_access/windows/bridge.ps1 -Action Setup -InterfaceAlias <WG_ALIAS> -Address <WINDOWS_WG_IP> -PeerAddress <VPS_WG_IP>
# Setup above is dry-run. Apply needs its own explicit approval/confirmation.
```

The hold affects inner traffic to the future WG address, not the outer WireGuard
UDP handshake. Do not remove it until final policy is complete. Existing bridge.ps1
owns only its TCP8443 rules; it does **not** isolate every other host service by
itself. Separately review explicit deny of all other TCP ports and inner UDP/other
protocols on that exact WG address/interface, even for the VPS peer, so unrelated
broad application allows cannot expose port5432/5678/etc. Additive port-complement
rules and their rollback receipts must be reviewed after privileged firewall
inventory; no such rules were generated/applied by C1. Never flush existing policy.

After final Windows/VPS rule approval, test a real two-host handshake with
`wg show <DEDICATED_INTERFACE> latest-handshakes` and negative TCP/service probes.
For an approved Ubuntu restart use only
`sudo systemctl restart wg-quick@<DEDICATED_INTERFACE>`, never another project's
tunnel. For approved disable use its corresponding stop. Windows uninstall of only
the dedicated tunnel service is `wireguard.exe /uninstalltunnelservice <TUNNEL_NAME>`.
These actions require their own approval and are not diagnostics.

The existing main upgrade command already creates/retains a verified pre-upgrade
backup. After migration, a separate approved post-upgrade recovery-evidence command
is `scripts/security_recovery.py backup --gate <ABS_GATE> --output <NEW_POST_V3_DUMP>
--manifest <NEW_POST_V3_MANIFEST> --apply --confirm-database <EXACT_DB>`. The backup
command creates only an external archive plus owned restore verification. Later
`recover` and `release` each require a separate explicit approval; neither starts
ingress. Use only the existing combined remote_bridge.py launcher after all guards
pass. Before any authorized Nginx reload, validate the entire combined candidate,
preserve existing vhost backups and obtain explicit activation approval.
