[CmdletBinding(SupportsShouldProcess=$true, ConfirmImpact='High')]
param(
    [ValidateSet('Setup','Verify','Rollback')][string]$Action = 'Verify',
    [Parameter(Mandatory=$true)][string]$InterfaceAlias,
    [Parameter(Mandatory=$true)][string]$Address,
    [Parameter(Mandatory=$true)][string]$PeerAddress,
    [ValidateRange(1024,65535)][int]$Port = 8443,
    [switch]$Apply
)
$ErrorActionPreference = 'Stop'
# Only the two narrowly scoped rules below are owned. No portproxy/service writes.
$rulePrefix = 'KulAI-Memory-Bridge-v1'
$description = 'KulAI Memory bridge v1 managed rule; verify exact filters before removal'
function Other-IPv4Ranges([string]$Peer) {
    $bytes = ([System.Net.IPAddress]::Parse($Peer)).GetAddressBytes()
    [array]::Reverse($bytes)
    $number = [BitConverter]::ToUInt32($bytes,0)
    $before = [BitConverter]::GetBytes([uint32]($number - 1))
    $after = [BitConverter]::GetBytes([uint32]($number + 1))
    [array]::Reverse($before); [array]::Reverse($after)
    @(('0.0.0.0-' + ([System.Net.IPAddress]::new($before)).ToString()), (([System.Net.IPAddress]::new($after)).ToString() + '-255.255.255.255'))
}
$interfaceCheck = & (Join-Path $PSScriptRoot 'inspect-interface.ps1') -InterfaceAlias $InterfaceAlias -Address $Address -PeerAddress $PeerAddress | ConvertFrom-Json
if (-not $interfaceCheck.verified) { throw 'Interface validation failed' }
if ($Address -notmatch '^(10\.|172\.(1[6-9]|2[0-9]|3[01])\.|192\.168\.)' -or $PeerAddress -notmatch '^(10\.|172\.(1[6-9]|2[0-9]|3[01])\.|192\.168\.)') { throw 'Private IPv4 addresses required' }
if (@(Get-NetFirewallProfile | Where-Object { -not $_.Enabled -or $_.DefaultInboundAction -ne 'Block' }).Count -gt 0) { throw 'Enabled default-deny firewall profiles required; no automatic repair' }
$ranges = @(Other-IPv4Ranges $PeerAddress)
$existing = @{}
foreach ($kind in @('Allow','Block')) {
    $name = $rulePrefix + '-' + $kind
    $rule = Get-NetFirewallRule -PolicyStore PersistentStore -Name $name -ErrorAction SilentlyContinue
    if ($rule) {
        $ports = $rule | Get-NetFirewallPortFilter
        $ips = $rule | Get-NetFirewallAddressFilter
        $interfaces = $rule | Get-NetFirewallInterfaceFilter
        $remote = if ($kind -eq 'Allow') { @($PeerAddress) } else { $ranges }
        if ($rule.Description -ne $description -or $rule.Direction -ne 'Inbound' -or $rule.Action -ne $kind -or
            $ports.Protocol -notin @('TCP',6) -or [string]$ports.LocalPort -ne [string]$Port -or
            [string]$ips.LocalAddress -ne $Address -or [string]$interfaces.InterfaceAlias -ne $InterfaceAlias -or
            (@(Compare-Object @($ips.RemoteAddress) $remote).Count -ne 0)) { throw 'Existing rule ownership/filter conflict; manual review required' }
        $existing[$kind] = $rule
        if ($Action -eq 'Setup' -and -not $rule.Enabled) { throw 'Owned rule disabled; operator review required' }
    }
}
$netshRules = & "$env:SystemRoot\System32\netsh.exe" interface portproxy show v4tov4
if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect existing portproxy rules' }
if ($netshRules -match ([regex]::Escape($Address) + '\s+' + $Port + '\s')) { throw 'Existing portproxy conflicts with owned bridge' }
$listeners = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue | Where-Object { $_.LocalAddress -in @($Address,'0.0.0.0','::') })
if ($Action -eq 'Setup' -and $listeners.Count -gt 0) { throw 'Stop existing listener before firewall setup' }
if ($Action -eq 'Verify' -and ($existing.Count -ne 2 -or @($existing.Values | Where-Object Enabled -ne 'True').Count -gt 0)) { throw 'Owned firewall rules missing or disabled' }
if ($Action -eq 'Rollback' -and $listeners.Count -gt 0) { throw 'Stop bridge process before rollback' }
Write-Output ('Validated ' + $Action + '; Apply=' + $Apply + '; listener/service configuration unchanged')
if (-not $Apply -or $Action -eq 'Verify') { return }
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Explicit elevated operator execution required' }
if ($Action -eq 'Setup' -and $PSCmdlet.ShouldProcess($InterfaceAlias,'Install narrowly scoped KulAI firewall rules')) {
    # Block other sources first; it takes precedence over unrelated broad allow rules.
    foreach ($kind in @('Block','Allow')) {
        if (-not $existing.ContainsKey($kind)) {
            $remote = if ($kind -eq 'Allow') { @($PeerAddress) } else { $ranges }
            New-NetFirewallRule -PolicyStore PersistentStore -Name ($rulePrefix + '-' + $kind) -DisplayName ($rulePrefix + '-' + $kind) -Description $description -Direction Inbound -Action $kind -Enabled True -Profile Any -Protocol TCP -LocalAddress $Address -LocalPort $Port -RemoteAddress $remote -InterfaceAlias $InterfaceAlias | Out-Null
        }
    }
}
if ($Action -eq 'Rollback' -and $PSCmdlet.ShouldProcess($InterfaceAlias,'Remove exactly verified KulAI firewall rules')) {
    foreach ($kind in @('Allow','Block')) { if ($existing.ContainsKey($kind)) { $existing[$kind] | Remove-NetFirewallRule } }
}
