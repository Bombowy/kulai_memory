param([string]$Scenario,[string]$BridgeScript)
$ErrorActionPreference='Stop'
# Unit-test functions shadow OS inventory. Administrative writes always throw.
function Get-NetAdapter { param($Name) [pscustomobject]@{ Status='Up'; InterfaceDescription=$(if ($Scenario -eq 'wrong_adapter') {'Ethernet'} else {'WireGuard Tunnel'}); ifIndex=123 } }
function Get-NetIPAddress { [pscustomobject]@{ IPAddress='10.203.71.2'; PrefixLength=32 } }
function Get-NetRoute { [pscustomobject]@{ DestinationPrefix='10.203.71.1/32' } }
function Get-NetFirewallProfile { [pscustomobject]@{ Enabled=$true; DefaultInboundAction='Block' } }
function Get-NetTCPConnection { if ($Scenario -eq 'conflict') { [pscustomobject]@{LocalAddress='0.0.0.0'} } }
function Get-NetFirewallRule {
    param($Name,$PolicyStore)
    if ($Scenario -eq 'setup') { return }
    [pscustomobject]@{ Name=$Name; Description='KulAI Memory bridge v1 managed rule; verify exact filters before removal';
      Direction='Inbound'; Action=$(if($Name.EndsWith('Allow')) {'Allow'} else {'Block'}); Enabled=$true }
}
function Get-NetFirewallPortFilter { [CmdletBinding()]param([Parameter(ValueFromPipeline=$true)]$Rule) process { [pscustomobject]@{Protocol='TCP';LocalPort=8443} } }
function Get-NetFirewallInterfaceFilter { [CmdletBinding()]param([Parameter(ValueFromPipeline=$true)]$Rule) process { [pscustomobject]@{InterfaceAlias='kulai-memory'} } }
function Get-NetFirewallAddressFilter {
    [CmdletBinding()]param([Parameter(ValueFromPipeline=$true)]$Rule)
    process {
        $remote=if($Rule.Name.EndsWith('Allow')) { if($Scenario -eq 'wrong_filter') {'10.203.71.99'} else {'10.203.71.1'} } else { @('0.0.0.0-10.203.71.0','10.203.71.2-255.255.255.255') }
        [pscustomobject]@{LocalAddress='10.203.71.2';RemoteAddress=$remote}
    }
}
function New-NetFirewallRule { throw 'Administrative mutation attempted in unit test' }
function Remove-NetFirewallRule { throw 'Administrative mutation attempted in unit test' }
$action=if($Scenario -in @('setup','conflict')) {'Setup'} elseif($Scenario -eq 'rollback') {'Rollback'} else {'Verify'}
try {
    & $BridgeScript -Action $action -InterfaceAlias kulai-memory -Address 10.203.71.2 -PeerAddress 10.203.71.1 | Out-Null
    Write-Output 'accepted'
} catch {
    Write-Output 'refused'
}
