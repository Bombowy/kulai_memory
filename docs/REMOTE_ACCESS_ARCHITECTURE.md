# Remote access architecture — TASK 4D.3A / plan 4D.3B

This is a reviewed deployment plan, not an installed gateway. The present backend
still binds numeric loopback, rejects proxy headers/browser Origin and permits
pairing/refresh only in SECURE through its validated local transport. No DNS, public
listener, firewall rule, tunnel or certificate was installed by TASK 4D.3A.

## 1. Architecture

```mermaid
flowchart LR
    C[Android / Desktop / AI client] -->|HTTPS / WSS + per-device access| V[VPS TLS gateway]
    V -->|Private encrypted tunnel + authenticated upstream| E[Local verified ingress]
    E -->|Verified ingress adapter| B[Loopback KulAI Memory / FastAPI]
    B --> P[(Private PostgreSQL / pgvector / security)]
    B --> G[Private GPU: Whisper / BGE-M3 / Qwen]
    A[Local administrator CLI] --> P
```

The VPS can run a small gateway without GPU. Canonical data and inference remain
on the local computer. Each device has its own session and rotating secrets. No
shared client bearer, multi-user database isolation or public model API is proposed.

## 2. Trust boundaries

Internet requests, client IPs and all supplied proxy headers are untrusted. The VPS
terminates public TLS and applies admission/resource limits; the backend still
authenticates credentials and enforces approved actions. A compromised VPS can see
application traffic at TLS termination, including credentials and Memory; this is an
explicit trust boundary, not end-to-end encryption from phone to GPU.

The private tunnel authenticates peers cryptographically. Upstream ingress must
also authenticate the gateway, validate the effective origin and fail closed. Local
administrator CLI/database access has provisioning authority and is never an HTTP
admin endpoint. PostgreSQL administrators and a compromised Android OS remain
outside the bearer protocol's protection. Keystore storage does not prove device
attestation or prevent use of a stolen bearer while valid.

## 3. Domain and certificates

Choose one canonical origin, for example `https://memory.example.invalid` (placeholder,
not a configured domain). Use a publicly trusted, automatically renewed server
certificate with the exact hostname. Maintain expiry alarms and renewal drills.
Android release requires HTTPS, derives WSS from that origin and keeps normal
hostname/certificate verification. Changing scheme/host/port requires explicit
configuration and pairing; it must not silently transfer an existing credential.

Redirects must not forward pairing/refresh proofs. Do not log request Authorization,
request/response bodies or query strings. The current client refuses redirects and
secret-bearing URL configuration. No production key or real token belongs in docs.

## 4. Tunnel choice

| Option | Benefits | Operational boundary |
| --- | --- | --- |
| WireGuard | Small peer-key based encrypted UDP tunnel, explicit routes | Manage peer keys, NAT reachability, routing and key revocation yourself |
| Tailscale | WireGuard transport, NAT traversal and relay fallback | Additional control-plane dependency and metadata/ACL management |
| Restricted reverse SSH forward | Outbound connection from local PC can work behind NAT | Pin SSH host keys, restrict forwarding/account privileges, monitor reconnect and avoid accidental public bind |

Proposed first deployment: WireGuard with the local computer initiating its peer
connection, minimal AllowedIPs and no forwarding to PostgreSQL/model ports. A NAT
mapping may require bounded PersistentKeepalive; choose it based on measured NAT
behavior. [WireGuard protocol](https://www.wireguard.com/protocol/),
[WireGuard quick start](https://www.wireguard.com/quickstart/).

If UDP/NAT constraints make this impractical, evaluate Tailscale ACLs and its direct
versus relay behavior before selecting it. Relay transport remains encrypted but
adds a dependency/latency boundary. [Tailscale encryption](https://tailscale.com/docs/concepts/tailscale-encryption),
[connection types](https://tailscale.com/docs/reference/connection-types).
These alternatives are not installed here; the final choice requires 4D.3B evidence.

## 5. Host, Origin and proxy identity

An ingress policy must separately validate the TLS hostname, HTTP Host, expected
public origin, and cryptographically authenticated gateway identity. Unknown Host
must fail before forwarding. Native Android/Desktop clients omit browser Origin;
future browser clients need an explicit allowlist and CSRF/CORS assessment.

The present middleware accepts only a loopback Host/listener with matching ports.
Simply putting nginx in front does not enable remote mode. 4D.3B must add and test
an explicit verified-ingress adapter without weakening LOCAL_DEV or creating a
loopback-header bypass. Client IP may help rate limiting/diagnostics, never identity.

## 6. Forwarded headers

The gateway must strip all client-supplied Forwarded/X-Forwarded-* fields and any
ingress attestation field, then generate a minimal consistent set. The backend may
consume these values only after independently authenticating ingress; malformed,
duplicated or inconsistent Host/proto data must fail closed. An IP allowlist alone
is insufficient. Keep Uvicorn proxy-header parsing disabled until the new verified
adapter supplies a well-defined trusted scope. Current requests with proxy headers
remain denied, including on loopback.

## 7. Authenticated upstream

Prefer mTLS from the VPS gateway to a private local ingress: a private CA, distinct
client/server certificates, exact SAN verification, certificate rotation and explicit
revocation. Configure verification in both directions; nginx upstream client keys
are gateway secrets with restricted storage. [NGINX upstream TLS verification](https://docs.nginx.com/nginx/admin-guide/security-controls/securing-http-traffic-upstream).

Select an ASGI/TLS ingress adapter that exposes the **verified** peer certificate
identity from its own TLS stack. The app must not derive that identity from arbitrary
headers. If a separate local TLS terminator is used, its authenticated transfer to
ASGI needs a separately reviewed cryptographic attestation or equivalent authenticated
channel; a plain `X-Verified` header plus loopback is not acceptable. WireGuard peer
authentication is useful transport protection, but does not itself make arbitrary
HTTP metadata trusted. 4D.3B must resolve and test this adapter before any public access.

## 8. REST, WSS and streaming

Forward REST without response caching; pairing/refresh responses are `no-store`.
Do not buffer streaming audio into an unbounded request, and preserve WebSocket
upgrade semantics/backpressure/cancellation. Nginx requires explicit handling of
Upgrade/Connection and appropriate idle timeouts. [NGINX WebSocket proxying](https://nginx.org/en/docs/http/websocket.html).

Separate connect timeout, DB/auth timeout, idle WS timeout and allowed audio-session
duration. Test long NOTE/ASK recordings, inference latency and transport cancellation
under the actual gateway; keep Voice Note v1 unchanged. Establish bounded frame/body
sizes and maximum simultaneous sessions. Backend currently reauthorizes each WS
frame with one coherent security SELECT plus read-only transaction setup; no
authorization cache delays revocation. Measure over the private tunnel before
considering a bounded cache with a documented revocation deadline.

Rotation revokes the predecessor access. Existing WS sessions using it close on
their next frame and clients must reconnect with the published replacement. Do not
replay a NOTE or writing operation to reconnect. Preserve ingestion IDs/idempotency.
Android defers proactive renewal during active voice only while access remains valid;
test that boundary under the gateway. Expiry/revocation still ends the predecessor's
access and never authorizes write replay.

## 9. Firewall, limits and diagnostics

The future public surface is TLS gateway traffic only; tunnel administration and
gateway SSH require separately limited access. Local firewall rules should permit
only the selected private ingress path. PostgreSQL, Ollama/Whisper/BGE and ordinary
backend listeners must stay private. No such firewall changes are made by this task.

Use ingress body/frame/concurrency limits and separate pairing/refresh abuse controls,
with per-device limits only after authentication. The current durable global 120/min
security capability limiter is an application safety bound, not a public DDoS defense.
Monitor status categories, bounded timing/count metrics, pool exhaustion and tunnel
health. Redact proofs/tokens, private text/audio, complete URLs and SQL parameters in
every component, including gateway diagnostics and tracing. Monitoring must not
become a route around Memory authorization.

## 10. Local computer unavailable

The gateway returns a fixed unavailable response/503 without private config and
without queuing writes. Android locks private functions when access/renewal cannot
be verified, uses bounded reconnect backoff, and never falls back to LOCAL_DEV.
Access is at most one hour, refresh 30 days and absolute session 90 days. Connectivity
returning within the remaining refresh/absolute lifetime can resume a session; longer
outages require pairing. An ambiguous refresh response requires pairing even when
the server later returns. Do not automatically retry a possibly applied refresh or
a Memory write. No offline outbox is implemented here.

## 11. VPS/tunnel recovery

Keep reproducible gateway configuration and separately protected TLS/tunnel keys.
Test restarting gateway/tunnel/local PC independently. Restore the same verified
public origin; a different origin requires deliberate reconfiguration/pairing. On
gateway/key compromise rotate ingress/tunnel secrets, review client credentials,
revoke affected devices/sessions and investigate redacted audit events before
reopening traffic. A failed gateway upgrade must fail closed; a rollback cannot
restore an expired/revoked credential through cached authorization.

## 12. PostgreSQL backup and session recovery

Security v3 snapshots include all session fields and refresh digests/consumption
history in addition to devices, access tokens, pairing and rate state. Verified
backup requires actual pg_restore to an owned temporary DB and complete canonical
and security fingerprint equality. Canonical-only and v1/v2 archives remain valid
historical forms with explicit absence, not fabricated session records.

Restoring a past DB can rewind rotation/revocation and re-enable a stolen old secret.
Before serving a recovered database: keep ingress closed; verify archive and restore;
run strict doctor; review restore time; **revoke all restored active sessions and
associated access credentials (and legacy access credentials affected by rewind)**
using a separately approved, transactional administration procedure. Restore must
not silently resume phone login. Require pairing again, then reopen only after
synthetic expiry/replay/revoke checks. This recovery revocation procedure is a
required 4D.3B operational deliverable; 4D.3A never executes it on main.

Security upgrade on main also remains separately authorized: stop all writers/admin
commands -> strict doctor -> full backup -> actual restore verification -> archive
and source revalidation -> guarded additive upgrade -> doctor/full fingerprints.
Canonical head stays `kulai_memory_0004`; security head becomes `kulai_security_0003`.
Existing v1/v2 row fingerprints must match; new tables start empty. No startup DDL,
destructive downgrade or automatic production migration is supported.

## 13. TASK 4D.3B steps and acceptance gates

1. Review 4D.3A code/test results and approve separately any main schema installation.
2. Choose VPS/domain and tunnel after measuring NAT/UDP constraints; threat-model the
   trust in TLS termination, key storage, gateway compromise and recovery.
3. Implement/test verified mTLS ingress and Host/Origin/Forwarded policy on isolated
   loopback test servers; prove spoofed headers/certificates cannot bypass auth.
4. Prepare reviewable gateway/tunnel/firewall/DNS/certificate configurations and
   secret provisioning instructions; obtain deployment authorization before applying.
5. Add gateway rate/frame/concurrency limits, TLS renewal alarms, bounded monitoring,
   private-only model/database listeners and tested deny-by-default routing.
6. Test REST and long WSS/audio, backpressure, cancellation, provider ownership,
   rotation during a voice session, outages and reconnect without write replay.
7. Implement/test the approved backup-recovery credential revocation procedure and
   perform a full synthetic backup/restore/tunnel/VPS recovery drill.
8. Only then deploy the authorized gateway/tunnel, run remote physical Android
   NOTE/ASK/MEMORY/TTS against separately approved data/providers, and verify main
   fingerprints, redaction and revocation. Do not treat synthetic smoke as real RAG.
9. Document rollback/recovery and credential/key rotation; Desktop/AI clients may
   adopt the same per-device protocol in separate client work. MCP, accounts, QR,
   discovery and Memory Intelligence remain separate tasks.
