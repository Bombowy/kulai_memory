# Future Android fact review contract

Design only. TASK 5A.3 adds no public routes, Android permission grants or main
fact storage. The local demo capability is never serialized or sent to Android.

## Identity and authorization

The host derives the reviewer identity from its authenticated security session
and checks an active device binding on every request. A client-supplied reviewer,
account name, role, `admin` flag or local principal object never grants authority.
The host validates that the session owns the requested fact, source Memory,
relation and both relation endpoints. Cross-account UUID knowledge grants no
access. Queries must apply ownership predicates before pagination and counts.

Separate scopes:

| Scope | Allowed operations |
| --- | --- |
| `facts:read` | Bounded candidates, current evidence, versions, relations, audit belonging to the account |
| `facts:review` | Explicit approve/reject/revoke of a fact or relation; also requires `facts:read` |

Pairing does not implicitly grant review permission. Scope grants require an
explicit authorized account action. Token expiry, logout, pairing revocation,
device revocation and scope removal deny subsequent commands. Recheck authority
at transaction entry, including idempotent receipt reads. Never use an offline
Android approval queue that can commit after its authority has expired.

## Commands and idempotency

A future command carries only a target ID, action, displayed lock version and
stable request UUID. The server supplies reviewer/time and validates the intent.
The request UUID is bound to authenticated account/device/reviewer, target,
action and observed version. A changed intent under one UUID is an error.
Concurrent commands resolve through expected versions and atomic transactions.
Unknown COMMIT outcome is resolved by an authorized receipt lookup using the
same UUID; an explicit retry uses that same UUID. No automatic fresh UUID retry.
Relation review remains separate from approving either endpoint.

Semantic source verification runs without a checked-out transaction. Before
commit, recheck ownership, session authority, source revision/hash/mutation epoch,
exact evidence and status under the source/candidate locks. The UI cannot bypass
this by sending its displayed text. Model output never constitutes approval.

## Responses, privacy and lifecycle

Return bounded pages with opaque cursors and separate typed fact/plan/preference/
hypothesis labels. Exact provenance offsets use Unicode code points, half-open
ranges over the unchanged source; Android maps those to UTF-16 without fuzzy
quote matching. Supply timestamps with timezone and a source-validity indicator.

EDIT/ARCHIVE/RESTORE invalidate old approvals; DELETE must remove all derived
quotes, claims and touching relation rationales. Retain only minimal technical
audit where justified. Invalid source data must not appear in retrieval, receipts
or error payloads. Both source text and selected panels are sensitive caches:
clear them on invalidation, read failure, logout, background/lock and revocation.
Production needs an authenticated invalidation channel or a documented bounded
lease; a SQL filter alone does not erase content already held on the device.
Do not persist source/evidence in notifications, telemetry, crash reports,
clipboard synchronization or unencrypted local caches.

Audit actor/account/device, server time, target opaque ID, action, expected/result
version and request UUID. Do not log full text, quotes, source hashes or arbitrary
decision notes after source deletion. Audit reads are authorized and paginated.

## Required gates before enabling Android review

- Durable canonical lifecycle epoch and transactional source invalidation.
- Main migration/backup/retention review separate from Desktop demo.
- Server-side scope/ownership enforcement for reads, commands and receipts.
- Device/session revocation tests during inference and immediately before commit.
- Cross-account IDOR, principal spoofing, replay, concurrent decisions, lost ACK
  and deleted-source cache tests.
- TLS and existing transport security review; no endpoint until these gates have
  a separately reviewed implementation.
