<#
  Zeigt Drucker, Anschluss, Standardeinstellungen und ALLE treiberspezifischen
  Optionen (PrintCapabilities) an – Grundlage für das Preset-/Instanz-Mapping.

  Aufruf:
    .\show-printer-options.ps1                       # listet alle Drucker
    .\show-printer-options.ps1 -PrinterName "HP OfficeJet Pro 7740 series"
    .\show-printer-options.ps1 -PrinterName "..." -OutFile optionen.txt
#>
param(
  [string]$PrinterName,
  [string]$OutFile
)
$ErrorActionPreference = "Stop"

if (-not $PrinterName) {
  Get-Printer | Format-Table -AutoSize Name, DriverName, PortName, Shared
  Write-Host "`nAufruf: .\show-printer-options.ps1 -PrinterName '<Name aus der Liste>'"
  exit 0
}

$out = New-Object System.Collections.Generic.List[string]
function Say($s) { $out.Add([string]$s); Write-Host $s }

$p = Get-Printer -Name $PrinterName
Say "=== Drucker ==="
Say ("Name:      " + $p.Name)
Say ("Treiber:   " + $p.DriverName)
Say ("Anschluss: " + $p.PortName)
try {
  $port = Get-PrinterPort -Name $p.PortName
  if ($port.PrinterHostAddress) { Say ("IP:        " + $port.PrinterHostAddress + "  -> IPP-Test: python druckagent.py --check-ipp ipp://" + $port.PrinterHostAddress + "/ipp/print") }
} catch {}

Say "`n=== Aktuelle Standardeinstellungen (Set-PrintConfiguration) ==="
$c = Get-PrintConfiguration -PrinterName $PrinterName
Say ("Papierformat: " + $c.PaperSize)
Say ("Farbe:        " + $c.Color)
Say ("Duplex:       " + $c.DuplexingMode)
Say ("Sortieren:    " + $c.Collate)

Say "`n=== Treiberspezifische Eigenschaften (Get-PrinterProperty) ==="
Get-PrinterProperty -PrinterName $PrinterName | ForEach-Object { Say ("{0,-45} {1}" -f $_.PropertyName, $_.Value) }

Say "`n=== Alle Features und Optionen aus PrintCapabilities ==="
Add-Type -AssemblyName System.Printing
$server = New-Object System.Printing.LocalPrintServer
$queue = $server.GetPrintQueue($PrinterName)
$stream = $queue.GetPrintCapabilitiesAsXml()
$xml = New-Object System.Xml.XmlDocument
$xml.Load($stream)
$ns = New-Object System.Xml.XmlNamespaceManager($xml.NameTable)
$ns.AddNamespace("psf", "http://schemas.microsoft.com/windows/2003/08/printing/printschemaframework")

function DisplayName($node) {
  $d = $node.SelectSingleNode("psf:Property[contains(@name,'DisplayName')]/psf:Value", $ns)
  if ($d) { return " (" + $d.InnerText + ")" } else { return "" }
}

foreach ($f in $xml.SelectNodes("//psf:Feature", $ns)) {
  Say ("`n[" + $f.GetAttribute("name") + "]" + (DisplayName $f))
  foreach ($o in $f.SelectNodes("psf:Option", $ns)) {
    $name = $o.GetAttribute("name")
    if (-not $name) { $name = "(ohne Namen)" }
    Say ("   - " + $name + (DisplayName $o))
  }
}

Say "`nHinweise:"
Say " * Papierfächer: Feature 'psk:JobInputBin' bzw. 'psk:PageInputBin'. SumatraPDF akzeptiert bin=<Nummer> oder den Fachnamen."
Say " * Qualität:     'psk:PageOutputQuality' (Draft/Normal/High) – nur per Instanz einstellbar."
Say " * Medientyp:    'psk:PageMediaType' (Normalpapier, Fotopapier ...) – nur per Instanz einstellbar."
Say " * Treiber-Dialog direkt öffnen: rundll32 printui.dll,PrintUIEntry /e /n `"$PrinterName`""

if ($OutFile) { $out | Set-Content -Encoding UTF8 $OutFile; Write-Host "`nGespeichert in $OutFile" }
