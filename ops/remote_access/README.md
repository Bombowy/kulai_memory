# Remote gateway deployment candidates — TASK 4D.3B.2

This directory contains working configuration generation and isolated Nginx tests,
not an installed VPS. Production is **NO-GO** until a separately authorized two-host
WireGuard test, Ubuntu Nginx validation, recovery drill and physical phone Internet
smoke succeed. No migration, firewall/service/DNS installation or real key generation
is performed by the test pipeline.

## Transport and trust

```text
public HTTPS/WSS -> Nginx public TLS termination
  -> TLS with verified ingress SAN + Nginx gateway client certificate
  -> dedicated WireGuard IPv4 pair (/32 peer routes)
  -> Windows co-hosted opaque TCP bridge on its WireGuard address only
  -> owned 127.0.0.1:8443 mTLS Uvicorn listener
  -> scope-bound admission + original bearer/scopes -> private FastAPI/DB/models
```

The bridge does not parse or terminate TLS and never reads Authorization or Host.
OpenSSL inside Uvicorn still receives Nginx's actual client certificate. A compromised
gateway sees public traffic and tokens; it is a trusted transport operator, not an
authorization authority. WireGuard/IP allowlists do not replace mTLS or bearer.
No gateway private key/test CA belongs in an Android APK.

`netsh interface portproxy` was considered. Its persistent destination can outlive
ingress, and it lacks the per-connection resource and lifecycle policy used here.
The selected Python bridge runs in the same process as its verified ingress. It
accepts only an actual socket of that server, only while its managed SECURE runtime
is ready, and never permits a standalone arbitrary target such as LOCAL_DEV:8000.
Ingress failure stops forwarding; coordinated shutdown closes bridge sockets first.
The listener requires a real, Up WireGuard/Wintun adapter, an exact local /32 and an
exact VPS /32 route. The gateway peer IP is a filter, not proof of authentication.

Bridge limits: 32 concurrent connections; 64 KiB asyncio reader/write buffers;
16 KiB copies with drain/backpressure; 5-second connect; 240-second idle;
900-second total; bounded two-second reverse drain/half-close on EOF. Faults and
stop cancel both pumps and abort sockets. The adapter is revalidated every five
seconds; a failed check closes ingress/bridge instead of falling back.
No plaintext fallback, queuing writes, automatic retry or autostart exists.

An oversized WS message always disconnects without a write. Through Nginx the
client may observe an abnormal close (1006) if upstream rejects while Nginx is
still forwarding the frame; delivery of the ingress 1009 close reason is not
guaranteed. Clients must not interpret this as a safe-to-retry NOTE.

## Candidate generation and validation

Copy `deployment.example.json` to protected operator storage outside Git, fill every
placeholder with inventory values and choose a private pair absent from all LAN/VPN
routes. Examples in tests are synthetic, never assumed to be your deployment.

```powershell
.venv\Scripts\python.exe ops/remote_access/package.py --config <operator-config.json>
.venv\Scripts\python.exe ops/remote_access/package.py --config <operator-config.json> --write-candidates <new-empty-directory>
.venv\Scripts\python.exe ops/remote_access/package.py --config <operator-config.json> --validate-candidates <candidate-directory>
```

Default is dry-run. Writing creates an exclusive **new** directory with three
candidate files; it never copies them into system paths. Validation compares the
entire result to the reviewed template, rejecting `proxy_ssl_verify off`, plaintext
upstream, retries, pass-through headers, changed Host/SNI and other hand edits.
This proves candidate policy provenance, not OS/firewall/certificate readiness.
Operator-supplied real WireGuard private keys must be provisioned only afterwards
into protected system files, never candidates/reports/source control.

## VPS inventory, DNS and Nginx

Run `ubuntu/preflight.sh` locally on the VPS after separate authorization for access.
It reads OS, listeners, route/service state and public handshake counters only.
Do not upload raw `nginx -T`, `wg showconf`, `.env`, private keys or full logs.
Inspect existing nginx include order, default vhosts and firewall policies privately.
Record Ubuntu/kernel/nginx/OpenSSL versions, public/local IP, UDP reachability,
occupied 80/443/8443/51820 ports, DNS ownership and renewal mechanism.

The generated Nginx file is an `http {}` include with one named vhost, unique maps/
zones and a rejecting default vhost on a **dedicated explicit IP:port**. It must not
be loaded twice. It does not open port 80 or redirect capability POSTs. If other
projects already own the default for that address, STOP: use an approved dedicated
IP or review integration with the existing rejecting default; never overwrite it.

Prepare DNS and a publicly trusted SAN certificate using separately approved actions.
Provision distinct ingress-server and gateway-client certificates from a protected
private CA, correct serverAuth/clientAuth EKUs, exact configured DNS SANs and key
permissions. Separate issuing roots may be used; `check-certificates.ps1` accepts
a bundle containing both roots. The ingress client-CA store should trust only the
dedicated gateway issuer; pinning the gateway leaf adds a narrower identity check.
An upstream SNI is the private ingress SAN, independent of the public Host. It does
not require public DNS resolution: proxy destination is an explicit WireGuard IP.

Validate certificates offline (explicit trusted OpenSSL executable) and run Ubuntu
`nginx -t` before any reload. Install only the named KulAI include after separately
approved review; no script here rewrites `/etc/nginx/nginx.conf` or other sites.

Nginx verifies public Host and SNI, validates raw route/method, denies browser Origin,
and sends a **positive header allowlist**. All client Forwarded/X-Forwarded*/X-Real-IP/
X-Verified*/arbitrary headers are stripped; none are regenerated. They cannot create
authentication. A legitimate bearer with irrelevant spoofed headers stays authorized
after stripping. Unknown/admin/debug/model/DB routes are denied. No CORS policy exists.

Limits include 64 KiB HTTP body, 8 KiB header buffer budget, per-IP request/capability
rate limits and four connections; ingress retains stricter 4 KiB capability bodies,
256 KiB binary/16 KiB text messages, 600-second/19.2 MB PCM and 900-second WS lifetime.
Proxy idle read/send 240 s are reset by streaming/pings, not a ten-minute total cap.
Raw access logging is replaced with fixed status/bytes/timing fields. Error logging
is disabled to a null device because Nginx can echo URLs/headers in error diagnostics.
Use synthetic probing and aggregated status counters for failure alerts. No cache,
buffering to disk, redirect, upstream retry or uncertain-result `request_applied:false`.

## WireGuard and firewall (not installed by this task)

Use one dedicated `kulai-memory` interface and new independent keys generated with
the official `wg genkey`/`wg pubkey` tools **only under future authorization**. Never
print private keys or use `wg show all dump` in shared diagnostics.

Ubuntu address `<VPS_WG_IP>/32`, peer AllowedIPs `<WINDOWS_WG_IP>/32`; no endpoint on
Ubuntu. Windows address `<WINDOWS_WG_IP>/32`, peer AllowedIPs `<VPS_WG_IP>/32`, public
VPS endpoint/UDP port, PersistentKeepalive=25 for outbound NAT. No default route,
home subnet, IP forwarding, NAT/masquerade, PostUp or SaveConfig. Check both hosts'
LAN/VPN/cloud routes before choosing the pair. UDP-blocking networks need a separate
reviewed tunnel alternative; no HTTP/LOCAL_DEV downgrade is permitted.

`ubuntu/wireguard-firewall.nft.in` is an additive candidate table for the dedicated
tunnel: only VPS -> Windows ingress TCP output; established replies; no incoming
new Windows connections to VPS services; no forwarded/home-network traffic. It
does not flush or replace existing policies. Review chain priorities, UFW/nft coexistence
and UDP/public HTTPS/SSH allowlists. Run `nft --check -f <candidate>` under authorized
operator execution; adding any real rules requires separate approval. PostgreSQL/
Ollama remain loopback-bound and are not Nginx destinations.

`inspect-interface.ps1` runs read-only in a child PowerShell with process-only
RemoteSigned policy; it never changes the machine execution policy. Organization
Group Policy remains authoritative. Operator scripts should be signed/reviewed
and run under the approved script policy, not installed with a global bypass.

Windows `windows/bridge.ps1` defaults to Verify, or Setup/Rollback validation without
`-Apply`. It checks adapter/route/portproxy/listener ownership and firewall profiles.
Setup requires stopped bridge, explicit `-Apply`, elevation and ShouldProcess
confirmation. It adds only two owned narrow rules for this interface/local IP/port:
allow the exact VPS IPv4 and block every other IPv4 (block takes precedence over
unrelated broad allow rules). Rollback requires the bridge stopped and verifies
every filter/description before removing only these rules. No services, portproxy,
global firewall settings or other-project rules are changed. Disabled/conflicting
owned rules require operator repair; do not bypass validation.

```powershell
# Validation only; requires a previously configured real tunnel.
.\ops\remote_access\windows\bridge.ps1 -Action Setup -InterfaceAlias kulai-memory -Address <WINDOWS_WG_IP> -PeerAddress <VPS_WG_IP>
.\ops\remote_access\windows\bridge.ps1 -Action Verify -InterfaceAlias kulai-memory -Address <WINDOWS_WG_IP> -PeerAddress <VPS_WG_IP>
# Explicit launcher: default validates files; --enable starts only after SECURE/schema checks.
backend\.venv\Scripts\python.exe scripts/remote_bridge.py --config <protected-ingress-bridge.json>
backend\.venv\Scripts\python.exe scripts/remote_bridge.py --config <protected-ingress-bridge.json> --enable
```

Windows WireGuard service installation/autostart and Ubuntu `wg-quick@kulai-memory`
enable/start are deferred. No automatic ingress service is installed; use a deliberate
foreground process until recovery and autostart policy are separately reviewed.

After separate approval and installation, the dedicated restart commands are
`sudo systemctl restart wg-quick@kulai-memory` on Ubuntu and, in elevated Windows
PowerShell, `Stop-Service -Name 'WireGuardTunnel$kulai-memory'` followed by
`Start-Service -Name 'WireGuardTunnel$kulai-memory'`. Verify exact service names
first; never restart all VPNs. Stop public routing and the combined ingress process
before these commands. Disable only that service/tunnel for rollback. The Windows
peer initiates/re-establishes its connection while running; after PC restart the
combined ingress still requires manual verification/start. Sleep/power-off gives
unavailability, not background public recovery.

## Isolated actual Nginx tests

Use a reviewed local binary with `KULAI_TEST_NGINX` (Windows portable or Linux native).
The test fixture runs `nginx -t` and a dedicated foreground process with temporary
prefix, PID, config, PKI, random **loopback-only** ports, synthetic inference and
marker-owned PostgreSQL. It stops only its own prefix/PID, not all nginx processes.
No system install/reload or port/firewall change is performed. For reproducible
Windows provisioning see `test-nginx-download.ps1`; it only downloads/extracts into
a new marked temporary directory after official detached-signature verification.
Remove that directory after tests, after confirming no owned process remains.
The reviewed Windows 1.30.5 archive SHA-256 is
`e5afe28b6a50bec92c478bfe1a4d3758206b80fb77159277bc5c4e88955c2a35`;
the official detached signature fingerprint is
`D6786CE303D9A9022998DC6CC8464D549AF75C0A`, from the
[official downloads](https://nginx.org/en/download.html) and
[maintainer keys](https://nginx.org/en/pgp_keys.html). No unsigned executable is run.

```powershell
$env:KULAI_RUN_POSTGRES_INTEGRATION='1'
$env:KULAI_TEST_NGINX='<reviewed-nginx-executable>'
backend\.venv\Scripts\python.exe -m pytest backend/tests/test_remote_gateway_config.py backend/tests/integration/test_tcp_bridge.py backend/tests/integration/test_nginx_gateway.py -q
```

Missing Nginx is a pytest SKIP with an explicit NOT RUN reason, never a synthetic
PASS. Tests without Nginx still exercise real endpoint TLS through the byte bridge.
The loopback bridge is **not** a two-host WireGuard test; 19.2 MB PCM is accelerated
volume, not ten minutes of wall-clock endurance. Android unit/TLS/retry tests do not
prove physical Wi-Fi/LTE connectivity. Linux-specific nft/systemd and real Windows
firewall/interface application remain NOT RUN until deployment authorization.

## Restart, monitoring, rotation and rollback

1. Stop public routing first on any uncertain restore/revocation or key compromise.
2. On Windows stop the combined process (Ctrl+C). It closes bridge before security/AI.
   PC sleep/power loss yields fixed 503, no write queue. Keep client credentials during
   short unavailability; ambiguous refresh is never replayed and requires pairing.
3. Inspect only `wg show <interface> latest-handshakes/transfer`, explicit routes,
   adapter status and loopback ingress listener. Use alarms for stale handshake,
   503/429 spikes, certificate expiry and owned process exit; never full request URLs.
4. Restart tunnel only after key/route/firewall verification, then explicitly start
   SECURE ingress+bridge after strict doctor and canonical/security heads. LOCAL_DEV
   or missing schema cannot listen through this launcher.
5. Validate HTTPS/WSS, anonymous denial, bad cert/Host, admin routes and revocation
   on synthetic owned DB before reopening public routing. Pair real phone separately.
6. For rotation close ingress/public routing, provision fresh protected keys/certs,
   update exact peer key/SAN/leaf pin on both hosts, validate and do negative tests.
   Replace public TLS cert via existing ACME renewal with tested reload; no shared key
   reuse. Check NAT handshake after each WireGuard peer replacement. Remove old keys.
7. Rollback: close only KulAI public vhost using approved include change + nginx -t;
   stop combined process; validated Windows rollback for owned rules; stop only the
   dedicated tunnel; remove only the dedicated nft table after ownership review.
   Restore unrelated sites' state unchanged. Never downgrade security DB or keys.

After database recovery keep ingress closed. Verify backup and a real owned restore,
canonical/vector/tombstone/security fingerprints, then separately approve transactional
revocation of restored sessions/access credentials and new pairing if rollback could
rewind revocations. There is no recovery DDL/revocation tool in this package.

## Operator checklist for the next task

- [ ] Real VPS inventory and safe dedicated vhost/default integration; no overwrite.
- [ ] Public domain/DNS/SAN/ACME ownership and certificates/rotation/storage verified.
- [ ] Explicit private /32 pair free of LAN/VPN conflicts, NAT/UDP reachability measured.
- [ ] Authorized two-host WireGuard handshake, stop/restart and deny-other-services test.
- [ ] Authorized Windows narrow firewall rules and adapter validation on real tunnel.
- [ ] Main backup + actual restore, guarded security migration and recovery revocation plan.
- [ ] Ubuntu Nginx -t and full negative TLS/HTTP/WSS tests before public opening.
- [ ] Physical Android Wi-Fi -> LTE -> Wi-Fi, NOTE/ASK/MEMORY/TTS and interruption smoke.
- [ ] Ten-minute paced PCM/backpressure and inference-latency test, redacted monitoring.
- [ ] Immediate rollback exercised; explicit separate approval for each infrastructure change.

Protocol references: [Nginx upstream TLS/header/retry directives](https://nginx.org/en/docs/http/ngx_http_proxy_module.html),
[WebSocket proxying](https://nginx.org/en/docs/http/websocket.html),
[WireGuard NAT keepalive](https://www.wireguard.com/quickstart/).
