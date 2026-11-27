#Requires -Version 5.1
<#
.SYNOPSIS
    Read-only patch inventory of Windows servers and workstations over CIM/WinRM: OS build, installed updates,
    last boot, pending reboot. Output feeds patching/patch_report.py.

.DESCRIPTION
    For each computer:
      Win32_OperatingSystem          caption, version, build (+ UBR from the registry), last boot
      Win32_QuickFixEngineering      installed KBs and install dates
      Registry (StdRegProv)          pending reboot: CBS RebootPending, WindowsUpdate RebootRequired,
                                     Session Manager PendingFileRenameOperations
    Unreachable computers are reported with Status = Unreachable instead of being skipped silently.

.EXAMPLE
    ./patching/Get-PatchCompliance.ps1 -ComputerName (Get-Content servers.txt) -OutFile ./data/live/patch_inventory.csv
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string[]]$ComputerName,
    [Parameter(Mandatory)][string]$OutFile,
    [pscredential]$Credential
)

$HKLM = [uint32]2147483650

function Test-RegKey($Session, [string]$Path) {
    $r = Invoke-CimMethod -CimSession $Session -Namespace root/default -ClassName StdRegProv -MethodName EnumKey `
        -Arguments @{ hDefKey = $HKLM; sSubKeyName = $Path }
    $r.ReturnValue -eq 0
}

function Get-RegValue($Session, [string]$Path, [string]$Name, [string]$Method = 'GetDWORDValue') {
    $r = Invoke-CimMethod -CimSession $Session -Namespace root/default -ClassName StdRegProv -MethodName $Method `
        -Arguments @{ hDefKey = $HKLM; sSubKeyName = $Path; sValueName = $Name }
    if ($r.ReturnValue -eq 0) { if ($Method -eq 'GetMultiStringValue') { $r.sValue } else { $r.uValue } }
}

$results = foreach ($c in $ComputerName) {
    $params = @{ ComputerName = $c; ErrorAction = 'Stop'; OperationTimeoutSec = 30 }
    if ($Credential) { $params.Credential = $Credential }
    try {
        $s = New-CimSession @params
        $os = Get-CimInstance -CimSession $s -ClassName Win32_OperatingSystem
        $ubr = Get-RegValue $s 'SOFTWARE\Microsoft\Windows NT\CurrentVersion' 'UBR'
        $kbs = Get-CimInstance -CimSession $s -ClassName Win32_QuickFixEngineering |
            Sort-Object { $_.InstalledOn } -Descending
        $pending = (Test-RegKey $s 'SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending') -or
            (Test-RegKey $s 'SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired') -or
            [bool](Get-RegValue $s 'SYSTEM\CurrentControlSet\Control\Session Manager' 'PendingFileRenameOperations' 'GetMultiStringValue')
        [pscustomobject]@{
            ComputerName   = $c
            Status         = 'OK'
            OS             = $os.Caption
            Build          = "$($os.BuildNumber).$ubr"
            LastBootUtc    = $os.LastBootUpTime.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
            InstalledKBs   = ($kbs.HotFixID | Select-Object -Unique) -join ';'
            LastKBDate     = if ($kbs) { ([datetime]$kbs[0].InstalledOn).ToString('yyyy-MM-dd') } else { '' }
            PendingReboot  = $pending
        }
        Remove-CimSession $s
    }
    catch {
        [pscustomobject]@{ ComputerName = $c; Status = 'Unreachable'; OS = ''; Build = ''; LastBootUtc = ''
            InstalledKBs = ''; LastKBDate = ''; PendingReboot = '' }
        Write-Warning "$c : $($_.Exception.Message)"
    }
}

New-Item -ItemType Directory -Force -Path (Split-Path $OutFile) | Out-Null
$results | Export-Csv -Path $OutFile -NoTypeInformation
Write-Output "Inventoried $(@($results | Where-Object Status -eq 'OK').Count) of $($ComputerName.Count) computers -> $OutFile"
