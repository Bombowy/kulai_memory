param(
    [Parameter(Mandatory=$true)][string]$OpenSsl,
    [Parameter(Mandatory=$true)][string]$Ca,
    [Parameter(Mandatory=$true)][string]$IngressCertificate,
    [Parameter(Mandatory=$true)][string]$IngressIdentity,
    [Parameter(Mandatory=$true)][string]$GatewayCertificate,
    [Parameter(Mandatory=$true)][string]$GatewayIdentity,
    [ValidateRange(1,2592000)][int]$MinimumValiditySeconds = 604800
)
$ErrorActionPreference='Stop'
# Read-only offline validation; no key generation, imports, trust-store writes.
& $OpenSsl verify -x509_strict -CAfile $Ca -purpose sslserver -verify_hostname $IngressIdentity $IngressCertificate
if ($LASTEXITCODE -ne 0) { throw 'Ingress chain/purpose/identity validation failed' }
& $OpenSsl verify -x509_strict -CAfile $Ca -purpose sslclient $GatewayCertificate
if ($LASTEXITCODE -ne 0) { throw 'Gateway chain/purpose validation failed' }
# Backend requires exact DNS SAN, not OpenSSL hostname wildcard/CN fallback.
foreach ($entry in @(@($IngressCertificate,$IngressIdentity),@($GatewayCertificate,$GatewayIdentity))) {
    $san = & $OpenSsl x509 -in $entry[0] -noout -ext subjectAltName
    if ($LASTEXITCODE -ne 0) { throw 'Cannot read SAN' }
    $identities = (($san | Select-Object -Skip 1) -join ' ') -split ',\s*' | ForEach-Object { $_.Trim() }
    if (('DNS:'+$entry[1]) -notin $identities) { throw 'Exact DNS SAN missing' }
    & $OpenSsl x509 -in $entry[0] -checkend $MinimumValiditySeconds -noout
    if ($LASTEXITCODE -ne 0) { throw 'Certificate renewal required within configured margin' }
}
& $OpenSsl x509 -in $GatewayCertificate -noout -fingerprint -sha256
if ($LASTEXITCODE -ne 0) { throw 'Fingerprint check failed' }
