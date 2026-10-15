# PostgreSQL network restriction: reviewed procedure, not applied

This document authorizes no configuration edit, restart or firewall change.
The instance is shared: obtain approval from every application owner first.
The current listener is `0.0.0.0:5432` and `[::]:5432`. Loopback-only HBA does not
remove the network listener. Default inbound Block does not rule out explicit
Allow rules. Detailed ActiveStore filter access requires an administrator.

Run `ops/remote_access/windows/postgres-preflight.ps1` without Apply; it reports
socket owners and effective profiles. It never elevates. `NOT VERIFIED` filter
access must be resolved by a separately authorized administrator read, not by
assuming no matching rule exists. Review both IPv4 and IPv6, all profiles,
Any/program/service rules, remote address/interface scope and future WG exposure.

## Read-only evidence before any change

In a protected local PostgreSQL session, without printing connection credentials:

```sql
BEGIN READ ONLY;
SHOW server_version;
SHOW listen_addresses;
SHOW port;
SHOW config_file;
SHOW hba_file;
SELECT coalesce(client_addr::text, 'local') AS client_address,
       application_name, state, count(*)
FROM pg_stat_activity
WHERE backend_type='client backend'
GROUP BY client_addr, application_name, state;
SELECT type, address, netmask, auth_method, error FROM pg_hba_file_rules;
ROLLBACK;
```

Keep database names, usernames, application names and paths local. Export only
redacted counts and whether non-loopback clients or HBA entries exist. An idle
snapshot cannot exclude scheduled LAN jobs. Ask all instance owners to identify
their connection endpoints, overnight jobs and monitoring. Do not declare that
LAN access is unnecessary until they confirm it. If an approved LAN consumer
exists, stop this procedure and review a narrowly scoped alternative separately.

## Separately approved maintenance operation

1. Freeze approved endpoints and current config paths, include files, ACLs and
   hashes. Inspect `pg_settings.sourcefile`, `sourceline`, `pending_restart` for
   `listen_addresses`; check for an override in `postgresql.auto.conf` or command
   line settings. Do not use `ALTER SYSTEM` as an unreviewed override.
2. Complete and verify a backup with a real restore into a capability-owned DB
   using existing backup tooling. Never test restore on main. This network change
   does not install security schema or invalidate credentials.
3. Obtain a restart maintenance window from all owners. Stop/drain their writers
   through their normal controls. Close remote ingress/bridge if one has later
   been installed. Retain local console access throughout; SSH is unnecessary.
4. Save the exact previous config and its ACL in protected storage outside Git,
   then edit only the effective `listen_addresses` setting to
   `'127.0.0.1,::1'`. Keep the port and HBA unchanged. Verify the setting is not
   overridden by another include or a service argument.
5. Read `pg_file_settings` for parsing errors and the effective proposed
   `listen_addresses`. Do not dump all configuration values. Resolve any parse
   error before restart. This setting needs a PostgreSQL restart; reload alone
   does not implement the change.
6. With explicit approval for this operation, restart the identified service
   (`postgresql-x64-18` was observed), using the normal Windows service manager.
   Every existing connection is interrupted. No automatic restart is performed
   by the provided preflight script.
7. Confirm Running state, both loopback listeners only, fresh successful IPv4 and
   IPv6 local connections, doctor, application health and unchanged canonical
   fingerprints. Check the host LAN/WG address cannot accept TCP/5432. HBA and
   Windows Firewall remain unchanged by this operation.
8. Resume each application only after its owner verifies reconnect and jobs.
   Record rollback readiness before installing any VPN interface.

## Immediate rollback (separate operator approval)

Keep a local administrative console open. If startup, IPv6 binding or an approved
client fails, stop/drain affected applications, restore the exact saved effective
configuration with its ACL, inspect parse errors and restart the same service.
Confirm the original listeners and local connections before resuming clients.
Do not broaden HBA or firewall, reset passwords, restore the database, or apply
security migrations to repair a listener issue. Compare Memory/vector/tombstone
fingerprints again. Do not retry uncertain application writes automatically.

IPv6 policy needs explicit review even if the current Ethernet has only link
local IPv6: a future VPN/interface can change reachability. A loopback-only IPv4
setting combined with an IPv6 wildcard is not an acceptable result.
