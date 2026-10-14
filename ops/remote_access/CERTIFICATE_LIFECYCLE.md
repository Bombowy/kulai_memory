# Certificate lifecycle — operator procedure, not executed production setup

Keep the encrypted private CA offline, independently backed up and outside Git,
VPS and Windows runtime. Use separate ingress/serverAuth and gateway/clientAuth
leaf keys. Public Android HTTPS uses platform trust, never this private CA.

After separate authorization, use a NEW protected directory, umask 077 or restricted
Windows ACL, supported OpenSSL, unique serials and operator-selected identities.
These are templates; no production keys were generated:

```sh
openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -aes-256-cbc -out ca.key
openssl req -new -x509 -sha256 -days 1825 -key ca.key -out ca.pem -subj '/CN=<PRIVATE_CA_NAME>' -addext 'basicConstraints=critical,CA:TRUE,pathlen:0' -addext 'keyUsage=critical,keyCertSign,cRLSign'
openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out ingress.key
openssl req -new -sha256 -key ingress.key -out ingress.csr -subj '/CN=<INGRESS_DNS_SAN>'
openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out gateway.key
openssl req -new -sha256 -key gateway.key -out gateway.csr -subj '/CN=<GATEWAY_DNS_SAN>'
```

Review CSRs on the offline signer. Ingress extension file:

```ini
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature
extendedKeyUsage=serverAuth
subjectAltName=DNS:<INGRESS_DNS_SAN>
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid,issuer
```

Gateway extension file: same constraints/key usage, `extendedKeyUsage=clientAuth`
and exact `subjectAltName=DNS:<GATEWAY_DNS_SAN>`. No wildcard or CN-only identity.

```sh
openssl x509 -req -in ingress.csr -CA ca.pem -CAkey ca.key -CAcreateserial -days 90 -sha256 -extfile ingress.ext -out ingress.pem
openssl x509 -req -in gateway.csr -CA ca.pem -CAkey ca.key -CAserial ca.srl -days 90 -sha256 -extfile gateway.ext -out gateway.pem
openssl verify -x509_strict -CAfile ca.pem -purpose sslserver -verify_hostname <INGRESS_DNS_SAN> ingress.pem
openssl verify -x509_strict -CAfile ca.pem -purpose sslclient -verify_hostname <GATEWAY_DNS_SAN> gateway.pem
openssl x509 -in ingress.pem -checkend 604800 -noout
openssl x509 -in gateway.pem -checkend 604800 -noout
```

Check exact SAN using the supplied checker, CA expiry, EKU and cert/key public-key
hash equality locally; never print private keys. The CA must outlive its leaves.
[OpenSSL verification reference](https://docs.openssl.org/3.5/man1/openssl-verify/).
Keep protected serial/issuance records; never reuse keys or serials.

VPS receives gateway leaf/key and ingress trust CA only. Windows receives ingress
leaf/key and gateway trust CA. Windows ACL: service account/SYSTEM/Administrators
only; remove broad inheritance after approval. Ubuntu key: root-owned 0600 or an
explicitly reviewed service owner, parent 0700. Verify Nginx master's actual access.
Runtime leaves are unencrypted for unattended TLS load; restrictive OS protection
and encrypted backups are mandatory. No real key belongs in candidates/APK/reports.

Alert at 30/14/7 days for chain and leaf. Rotate into NEW protected paths, verify
chain/SAN/EKU/expiry/key match/ACL and complete candidate `nginx -t`. After approval
change only KulAI references and reload Nginx without changing other sites. Ingress
trust/key changes require controlled restart with existing matching recovery gate;
never bootstrap a gate. Deliberately update optional `gateway_sha256` pin. Verify
fresh successful handshakes and rejected removed identities before removing old
files. Keep overlap short and explicit.

Public ACME: confirm DNS/account ownership and renewal/unit conflicts. Choose a
challenge that does not take over another project's port 80. Review a hook that
checks fullchain/hostname and `nginx -t` before reload and alerts on failure. Test
staging renewal before authorized activation; staging CA never enters release APK.
No hook/service is installed by this task.

Compromise: immediately close KulAI routing and stop bridge/ingress. This stack
does NOT claim online CRL/OCSP enforcement. Replace a compromised gateway key plus
pin or expected identity/trusted CA and restart to terminate existing connections.
A CRL file alone is not enforcement. If CA is compromised, replace trust on both
ends, remove old CA completely and issue fresh leaves. Record serial revocation
offline; separately authorize session recovery/re-pairing if tokens were exposed.

Failed, uncompromised rotation: remain closed, verify previous pair is valid and
uncompromised, restore only reviewed KulAI references, repeat syntax/chain tests
then controlled reload/restart. Never disable verification, accept any hostname,
use an expired certificate or roll back the recovery journal.
