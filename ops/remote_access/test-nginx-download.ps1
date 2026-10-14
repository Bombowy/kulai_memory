param(
    [Parameter(Mandatory=$true)][string]$Gpg,
    [switch]$Download
)
$ErrorActionPreference='Stop'
# Optional test-only portable binary. Default is plan-only, not installation.
$version='1.30.5'
$sha256='E5AFE28B6A50BEC92C478BFE1A4D3758206B80FB77159277BC5C4E88955C2A35'
$signer='D6786CE303D9A9022998DC6CC8464D549AF75C0A'
if (-not $Download) { Write-Output ('DRY RUN: official Nginx '+$version+' ZIP + detached PGP signature; isolated temporary directory only'); return }
$root=Join-Path $env:TEMP ('kulai-owned-nginx-'+[guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $root | Out-Null
Set-Content -LiteralPath (Join-Path $root '.kulai-test-marker') -Value 'kulai-owned-test-download'
foreach ($item in @(@('https://nginx.org/download/nginx-1.30.5.zip','nginx.zip'),@('https://nginx.org/download/nginx-1.30.5.zip.asc','nginx.zip.asc'),@('https://nginx.org/keys/pluknet.key','pluknet.key'))) {
    Invoke-WebRequest -UseBasicParsing -Uri $item[0] -OutFile (Join-Path $root $item[1])
}
if ((Get-FileHash -LiteralPath (Join-Path $root 'nginx.zip') -Algorithm SHA256).Hash -ne $sha256) { throw 'Archive digest mismatch; no execution' }
$keyring=Join-Path $root 'keyring'
New-Item -ItemType Directory -Path $keyring | Out-Null
function Gpg-Path([string]$Value) {
    # Git for Windows GPG expects MSYS paths. Native GPG accepts Windows paths.
    if ($Gpg -match '[\\/]Git[\\/]usr[\\/]bin[\\/]') { return '/'+$Value.Substring(0,1).ToLower()+$Value.Substring(2).Replace('\','/') }
    return $Value
}
$ring=Gpg-Path $keyring
$publicRing=Gpg-Path (Join-Path $root 'public.gpg')
& $Gpg --batch --yes --dearmor --output $publicRing (Gpg-Path (Join-Path $root 'pluknet.key'))
if ($LASTEXITCODE -ne 0) { throw 'Cannot decode public signing key; no execution' }
$status=& $Gpg --homedir $ring --batch --no-autostart --no-default-keyring --keyring $publicRing --status-fd 1 --verify (Gpg-Path (Join-Path $root 'nginx.zip.asc')) (Gpg-Path (Join-Path $root 'nginx.zip'))
if ($LASTEXITCODE -ne 0 -or -not ($status -match ('^\[GNUPG:\] VALIDSIG '+$signer+' '))) { throw 'Signature verification failed; no execution' }
Expand-Archive -LiteralPath (Join-Path $root 'nginx.zip') -DestinationPath $root
Write-Output ('KULAI_TEST_NGINX='+ (Join-Path $root ('nginx-'+$version+'\nginx.exe')))
Write-Output ('OWNED_TEMP_DIRECTORY='+$root)
Write-Output 'No global install/service/start. Remove only this marked directory after owned process shutdown.'
