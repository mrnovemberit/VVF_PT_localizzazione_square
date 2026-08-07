# Carica le variabili d'ambiente da .env nella sessione PowerShell corrente.
# Uso (dalla cartella del progetto): .\load_env.ps1

$envFile = Join-Path $PSScriptRoot ".env"

if (-not (Test-Path $envFile)) {
    Write-Host "File .env non trovato in $envFile" -ForegroundColor Red
    exit 1
}

Get-Content $envFile | ForEach-Object {
    if ($_ -match '^\s*([^#=][^=]*)=(.*)$') {
        $name = $matches[1].Trim()
        $value = $matches[2].Trim()
        [System.Environment]::SetEnvironmentVariable($name, $value, "Process")
    }
}

Write-Host "Variabili .env caricate nella sessione corrente." -ForegroundColor Green
