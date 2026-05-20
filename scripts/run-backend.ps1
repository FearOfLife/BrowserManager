param(
    [int]$Port = 8765,
    [string]$HostAddress = "127.0.0.1"
)

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
python backend_server.py --host $HostAddress --port $Port
