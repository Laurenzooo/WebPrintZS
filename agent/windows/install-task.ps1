<#
  Richtet den Druck-Agenten als Aufgabe in der Windows-Aufgabenplanung ein:
  Start beim Hochfahren (ohne Anmeldung), Neustart bei Absturz, keine Laufzeitbegrenzung.

  ALS ADMINISTRATOR AUSFÜHREN, im Ordner mit druckagent.py:
    .\windows\install-task.ps1                 # läuft unter einem Benutzerkonto (fragt nach Passwort) – empfohlen
    .\windows\install-task.ps1 -AsSystem       # läuft als SYSTEM (kein Passwort nötig)
    .\windows\install-task.ps1 -Uninstall
#>
param(
  [string]$AgentDir = (Split-Path -Parent $PSScriptRoot),
  [string]$TaskName = "WebDruckAgent",
  [switch]$AsSystem,
  [switch]$Uninstall
)
$ErrorActionPreference = "Stop"

if ($Uninstall) {
  Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
  Write-Host "Aufgabe $TaskName entfernt."
  exit 0
}

$script = Join-Path $AgentDir "druckagent.py"
$config = Join-Path $AgentDir "config.toml"
if (-not (Test-Path $script)) { throw "druckagent.py nicht gefunden in $AgentDir" }
if (-not (Test-Path $config)) { throw "config.toml fehlt – bitte config.example.toml kopieren und anpassen." }

# pythonw.exe = ohne Konsolenfenster. Python muss "für alle Benutzer" installiert sein.
$pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $pythonw) {
  $py = (Get-Command py.exe -ErrorAction SilentlyContinue).Source
  if ($py) { $pythonw = (& $py -3 -c "import sys,os;print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))").Trim() }
}
if (-not $pythonw -or -not (Test-Path $pythonw)) { throw "pythonw.exe nicht gefunden. Python 3.11+ von python.org installieren (Haken: 'Add to PATH', 'for all users')." }
Write-Host "Python: $pythonw"

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$script`" --config `"$config`"" -WorkingDirectory $AgentDir
$trigger = New-ScheduledTaskTrigger -AtStartup
$trigger.Delay = "PT30S"   # 30 s warten, bis Netzwerk/Spooler bereit sind
$settings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
  -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
  -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew

if ($AsSystem) {
  $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
} else {
  $cred = Get-Credential -Message "Windows-Konto, unter dem der Agent laufen soll (Drucker muss dort sichtbar sein)"
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -User $cred.UserName -Password $cred.GetNetworkCredential().Password -RunLevel Limited -Force | Out-Null
}

Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 3
$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host "Aufgabe '$TaskName' eingerichtet. Letztes Ergebnis: $($info.LastTaskResult) (267009 = läuft)"
Write-Host "Logdatei siehe log_file in config.toml."
