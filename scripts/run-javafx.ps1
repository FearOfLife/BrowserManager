param(
    [string]$JavaFxHome = $env:JAVAFX_HOME,
    [string]$Python = "python",
    [int]$Port = 8765,
    [switch]$Detached
)

$Root = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($JavaFxHome)) {
    $Candidates = @(
        "C:\javafx-sdk-26",
        "C:\javafx-sdk-26.0.0",
        "C:\javafx-sdk-26.0.1",
        "C:\javafx-sdk",
        "C:\Program Files\JavaFX",
        "C:\Program Files\openjfx"
    )
    foreach ($Candidate in $Candidates) {
        if (Test-Path (Join-Path $Candidate "lib")) {
            $JavaFxHome = $Candidate
            break
        }
    }
}

if ([string]::IsNullOrWhiteSpace($JavaFxHome) -or -not (Test-Path (Join-Path $JavaFxHome "lib"))) {
    Write-Error "JavaFX SDK не найден. Укажите -JavaFxHome или переменную JAVAFX_HOME на папку JavaFX SDK."
    exit 1
}

$BuildDir = Join-Path $Root "build\javafx-client"
if (-not (Test-Path $BuildDir)) {
    New-Item -ItemType Directory -Path $BuildDir | Out-Null
}

$Sources = Get-ChildItem (Join-Path $Root "javafx-client\src\main\java") -Filter *.java -Recurse | Select-Object -ExpandProperty FullName
javac --module-path (Join-Path $JavaFxHome "lib") --add-modules javafx.controls -encoding UTF-8 -d $BuildDir $Sources
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

$JavaArgs = @(
    "--module-path", (Join-Path $JavaFxHome "lib"),
    "--add-modules", "javafx.controls",
    "-Dbrowser.manager.root=$Root",
    "-Dbrowser.manager.python=$Python",
    "-Dbrowser.manager.port=$Port",
    "-cp", $BuildDir,
    "browsermanager.BrowserManagerFx"
)

if ($Detached) {
    $JavaExe = (Get-Command javaw -ErrorAction SilentlyContinue)
    if ($null -eq $JavaExe) {
        $JavaExe = Get-Command java -ErrorAction Stop
    }
    $Process = Start-Process -FilePath $JavaExe.Source -ArgumentList $JavaArgs -WorkingDirectory $Root -PassThru
    Write-Output "BrowserManager JavaFX started: $($Process.Id)"
} else {
    java @JavaArgs
}
