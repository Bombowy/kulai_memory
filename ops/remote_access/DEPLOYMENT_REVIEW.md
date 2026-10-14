# Shared VPS review and controlled startup

`Deployment(shared_ip=True)` renders only a KulAI vhost and its namespaced
maps/zones. It does not claim `default_server`, change nginx.conf, install anything,
or overwrite a candidate directory. The example now selects shared mode. Dedicated
mode remains available only after verifying exclusive address/port ownership.

Run read-only `ubuntu/preflight.sh` and `windows/inventory.ps1` locally on the
respective hosts after separate authorization. No SSH is embedded. Public IP must
come from the provider's inventory; do not assume a local address proves NAT/public
ownership. Confirm DNS A/AAAA ownership, ACME account/cert ownership, existing sites,
listen options, TLS policy, routes and unit names. Locally review full firewall
rules and Nginx includes in a protected terminal. Never paste raw `nginx -T`, `.env`,
private keys, service command lines or logs into a report.

The existing default vhost must remain owned by its project/operator. It must
already have compatible TLS1.2/1.3 policy on this listener. Nginx selects some TLS
settings before the SNI vhost; a KulAI server-level directive cannot independently
repair an unsafe default. See [NGINX SSL directives](https://nginx.org/en/docs/http/ngx_http_ssl_module.html).
If no explicit existing default exists, options conflict, or its policy needs a
change, deployment is BLOCKED for an operator decision. The package never takes it
over. An existing reject-handshake default is preferred. An existing other-project
default may serve unknown names, but must never route them into KulAI; KulAI still
requires exact SNI and Host. Client HTTPS certificate/hostname validation remains
mandatory.

The conservative inventory lexer detects candidate-domain, wildcard/regex domain,
listener, reserved `$kulai_*`, `kulai_safe`, rate/connection-zone and default ownership
conflicts. Regex/wildcard ambiguity requires manual review. It does not replace
`nginx -t` against the complete combined candidate. PROXY protocol, QUIC, bind,
reuseport or incompatible listener options are blocked for explicit integration
review. No include rewrite or default-vhost workaround is performed. Other sites
and their files remain unchanged.

Prepare review candidates in a **new** directory with `package.py`. Do not include
these files into the running Nginx until separately approved. Fill
`inventory.example.json` from timestamped, locally reviewed facts; null means not
verified. Supply existing expanded Nginx inventory from protected local storage,
not from Git. This file can contain private configuration and must not be exported.

```text
<BACKEND_PYTHON> -m ops.remote_access.validator --config <CONFIG_JSON> --inventory <INVENTORY_JSON> --existing-nginx <PRIVATE_EXPANDED_CONFIG> --candidates <NEW_CANDIDATE_DIR> --certificates <PUBLIC_CERT_PATHS_JSON> --openssl <OPENSSL_EXECUTABLE>
```

`--backend-read-only --gate <ABS_GATE>` replaces backend attestations with actual
local read-only catalog/fingerprint evidence. It does not close/open a gate. Public
certificate specs contain `public`, `ingress`, `gateway`, each with `certificate`
and `ca` paths; never include key material. Verification uses exact DNS SAN, EKU,
validity (minimum seven days), and OpenSSL chain validation. Private key protection
and cert/key match must be verified on their owning machines; mark absent evidence
null. Firewall checks are operator evidence, not claims that this process queried
a remote VPS. The validator is read-only and returns:

- READY FOR REVIEW: supplied evidence passed; **not** production GO.
- BLOCKED: a conflict, unsafe configuration or invalid evidence was found.
- NOT VERIFIED: one or more required observations are missing.

Review routing on the actual combined config using an isolated Nginx instance.
The integration test `test_nginx_shared_ip.py` uses real TLS, other-project/default
vhosts, Host/SNI mismatch, unknown SNI, HTTPS/WSS and restart on one loopback IP:port.
It does not emulate or certify a two-host WireGuard path.

## Monitoring and restart

Feed aggregate observations to `python -m ops.remote_access.monitor --snapshot
<LOCAL_METRICS_JSON>`. Accepted fields are documented by `evaluate`: booleans for
Windows/bridge/upstream/HTTPS/WSS, handshake age, certificate days, gate state,
snapshot age, counts for 401/403/429/503 and uncertain refresh/session errors.
Only check IDs/statuses are emitted; unknown input is never HEALTHY. Collect from
safe counters, `wg show all latest-handshakes` and unauthenticated negative probes,
not Memory bodies or credential-bearing URLs. Configure alerts before enabling
traffic. Monitor journal size below 16 MiB separately.

Proposed normal boot order: dedicated tunnel -> verified SECURE runtime with
independent matching OPEN gate -> bridge validation/start -> existing Nginx vhost.
Do not install these services now. A future service must run with least privilege,
manual enablement, bounded restart backoff and no gate initialize/recover/release
step. After restore or gate failure it must stop and alert, never regenerate state.
Sleep/offline Windows produces safe upstream 503; resume reconnects the tunnel,
not client writes. Do not add automatic NOTE/refresh replay.

## Authorized TASK 4D.3C sequence

Each infrastructure/data-changing step needs separate explicit operator approval.

1. Record main read-only doctor/full fingerprints and protected backup with actual
   restore verification. Confirm independently persisted CLOSED recovery gate.
2. Separately approve the existing guarded security v3 migration on main, if absent;
   no migration is run by this task. Recheck heads and canonical fingerprints.
3. Provision approved domain/DNS/ACME ownership and protected separate mTLS identities.
4. Install dedicated WireGuard only after route/port inventory. Test real two-host
   handshake, peer /32 routes and denial of every non-ingress private service.
5. Apply minimal reviewed Windows/VPS firewall rules and verify exact ownership;
   start verified loopback ingress and explicit-interface transparent bridge.
6. In a new candidate path test complete shared Nginx config, other projects, SNI,
   mTLS failures, allowlisted endpoints, refresh uncertainty and unavailable backend.
7. After required recovery/release approval, enable only the KulAI vhost, obtain
   final public certificate tests, and approve physical Android pairing.
8. Test NOTE/ASK/MEMORY/TTS over LTE/5G and network changes, long audio/disconnect,
   revocation, redacted logs and after-snapshot integrity. No uncertain-write replay.
9. Declare production GO only on real two-host WireGuard, Ubuntu Nginx, public TLS
   and physical Internet phone results. Local Windows Nginx is not this evidence.

## Immediate rollback

Stop/disable only KulAI public routing, bridge and remote runtime. Leave other
vhosts and services intact. Preserve gate and aggregate audit evidence. Use the
existing bridge rollback dry-run and review exact rule ownership before approved
Apply. Remove only the dedicated tunnel after confirming it serves no other project.
Do not change canonical DB or downgrade security. Never restore older credentials
to roll back application code. A code version without this gate must remain remote
disabled. Any DB restore goes through RECOVERY.md with new pairing, not an automatic
reopen. Restore known-safe candidate certificates only if uncompromised and valid;
otherwise keep access closed.
