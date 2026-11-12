<#
  Legt "virtuelle Drucker" (weitere Instanzen desselben Druckers) mit eigenen
  Standardeinstellungen an. Der Agent wählt pro Preset nur noch die Instanz.

  ALS ADMINISTRATOR AUSFÜHREN:
    .\setup-instances.ps1 -BasePrinter "HP OfficeJet Pro 7740 series"

  Papierformat, Farbe und Duplex werden hier gesetzt. Treiberspezifisches
  (Papierfach, Druckqualität, Papiertyp) danach EINMAL je Instanz im
  Treiber-Dialog einstellen – das Skript öffnet ihn auf Wunsch (-OpenDialogs).
#>
param(
  [Parameter(Mandatory = $true)][string]$BasePrinter,
  [switch]$OpenDialogs
)
$ErrorActionPreference = "Stop"

# Name, Papierformat, Farbe, Duplex (OneSided | TwoSidedLongEdge | TwoSidedShortEdge)
# Namen müssen zu den [[route]]-Einträgen in config.toml passen.
$instances = @(
  @{ Name = "HP7740_A4_sw_duplex"; PaperSize = "A4"; Color = $false; Duplex = "TwoSidedLongEdge" },
  @{ Name = "HP7740_A4_farbe";     PaperSize = "A4"; Color = $true;  Duplex = "OneSided" },
  @{ Name = "HP7740_A3_farbe";     PaperSize = "A3"; Color = $true;  Duplex = "OneSided" }
)

$base = Get-Printer -Name $BasePrinter
Write-Host "Basis: $($base.Name)  Treiber: $($base.DriverName)  Anschluss: $($base.PortName)"

foreach ($i in $instances) {
  $existing = Get-Printer -Name $i.Name -ErrorAction SilentlyContinue
  if ($existing) {
    Write-Host "  = $($i.Name) existiert bereits – Einstellungen werden aktualisiert"
  } else {
    Add-Printer -Name $i.Name -DriverName $base.DriverName -PortName $base.PortName
    Write-Host "  + $($i.Name) angelegt"
  }
  Set-PrintConfiguration -PrinterName $i.Name -PaperSize $i.PaperSize -Color $i.Color -DuplexingMode $i.Duplex
  $c = Get-PrintConfiguration -PrinterName $i.Name
  Write-Host ("    -> {0}, Farbe={1}, {2}" -f $c.PaperSize, $c.Color, $c.DuplexingMode)
  if ($OpenDialogs) {
    Write-Host "    Treiber-Dialog: Fach/Qualität einstellen, mit OK speichern ..."
    Start-Process rundll32.exe -ArgumentList "printui.dll,PrintUIEntry /e /n `"$($i.Name)`"" -Wait
  }
}

Write-Host "`nFertig. Kontrolle: Get-Printer | Where-Object Name -like 'HP7740_*'"
Write-Host "Treiberoptionen je Instanz prüfen: .\show-printer-options.ps1 -PrinterName HP7740_A3_farbe"
