#Requires -Version 5.1
<#
.SYNOPSIS
  Instalador de GRAFENO para Windows.
.DESCRIPTION
  Instala GRAFENO con pipx (entorno aislado). Requiere Python 3.11+.
  Nota: este script es ASCII puro a proposito, para que Windows PowerShell 5.1
  lo lea correctamente sin importar la codificacion de la consola.
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\install.ps1
#>
$ErrorActionPreference = 'Stop'
$MinVersion = [version]'3.11'
$RepoRoot = $PSScriptRoot

function Write-Info { param([string]$Msg) Write-Host "[i] $Msg" -ForegroundColor Cyan }
function Write-Ok   { param([string]$Msg) Write-Host "[+] $Msg" -ForegroundColor Green }
function Write-Warn { param([string]$Msg) Write-Host "[!] $Msg" -ForegroundColor Yellow }
function Write-Err  { param([string]$Msg) Write-Host "[x] $Msg" -ForegroundColor Red }

# --- 1. Python >= 3.11 ------------------------------------------------------
$script:PyExe = $null
$script:PyArgs = @()

function Test-Python {
    param([string]$Exe, [string[]]$ExeArgs = @())
    try {
        $out = & $Exe @ExeArgs -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) {
            return ([version]$out.Trim()) -ge $MinVersion
        }
    } catch { }
    return $false
}

foreach ($c in @(
    @{ Exe = 'py';      Args = @('-3') },
    @{ Exe = 'python';  Args = @() },
    @{ Exe = 'python3'; Args = @() }
)) {
    if ((Get-Command $c.Exe -ErrorAction SilentlyContinue) -and (Test-Python $c.Exe $c.Args)) {
        $script:PyExe = $c.Exe
        $script:PyArgs = $c.Args
        break
    }
}

if (-not $PyExe) {
    Write-Err "No se encontro Python $MinVersion o superior."
    Write-Info "Instalalo con:  winget install -e --id Python.Python.3.12"
    Write-Info "o desde https://www.python.org/downloads/ (marca 'Add python.exe to PATH')."
    exit 1
}
$PyArgsStr = ($PyArgs -join ' ')
Write-Ok "Python: $(& $PyExe @PyArgs --version 2>&1)"

# --- 2. pipx ----------------------------------------------------------------
if (-not (Get-Command pipx -ErrorAction SilentlyContinue)) {
    Write-Info "pipx no esta instalado; instalando en el entorno de usuario..."
    & $PyExe @PyArgs -m pip install --user pipx
    if ($LASTEXITCODE -ne 0) {
        Write-Err "No se pudo instalar pipx."
        Write-Info "Prueba manualmente:  $PyExe -m pip install --user pipx"
        exit 1
    }
    & $PyExe @PyArgs -m pipx ensurepath | Out-Null
}

function Invoke-Pipx {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$A)
    if (Get-Command pipx -ErrorAction SilentlyContinue) {
        & pipx @A
    } else {
        & $PyExe @PyArgs -m pipx @A
    }
}

# --- 3. Instalar GRAFENO ----------------------------------------------------
Write-Info "Instalando GRAFENO desde $RepoRoot ..."
Push-Location $RepoRoot
try {
    Invoke-Pipx install --force .
    if ($LASTEXITCODE -ne 0) { throw "pipx install fallo con codigo $LASTEXITCODE" }
    Invoke-Pipx ensurepath | Out-Null
} finally {
    Pop-Location
}

$binDir = $null
try { $binDir = (& $PyExe @PyArgs -m pipx environment --value PIPX_BIN_DIR 2>$null) } catch { }
if (-not $binDir) { $binDir = Join-Path $env:USERPROFILE '.local\bin' }
$binDir = $binDir.Trim()
$exe = Join-Path $binDir 'grafeno.exe'

if (Test-Path $exe) {
    Write-Ok "GRAFENO instalado: $exe"
} else {
    Write-Err "La instalacion termino pero no se encontro $exe"
    exit 1
}

$resolved = $null
try { $resolved = (Get-Command grafeno -ErrorAction SilentlyContinue).Source } catch { }
if (-not $resolved) {
    Write-Warn "'grafeno' aun no esta en el PATH de esta sesion."
    Write-Info "Abre una terminal nueva o ejecuta:"
    Write-Info "  `$env:Path = `"$binDir;`$env:Path`""
} elseif ($resolved -ne $exe) {
    Write-Warn "'grafeno' resuelve a otra ruta con mas prioridad en el PATH:"
    Write-Warn "  $resolved"
    Write-Warn "Es una copia antigua que tapa la instalacion nueva ($exe)."
    Write-Info "Quitala con:  $PyExe $PyArgsStr -m pip uninstall grafeno -y"
    Write-Info "o borra ese ejecutable; despues 'grafeno' resolvera $exe."
}

# --- 4. CLIs de agentes (dependencias en tiempo de ejecucion) ---------------
$missing = @()
$found = @()
foreach ($cli in 'opencode', 'kimi', 'codex', 'claude') {
    if (Get-Command $cli -ErrorAction SilentlyContinue) { $found += $cli } else { $missing += $cli }
}
if ($missing.Count -gt 0) {
    Write-Warn "CLIs de agentes no encontrados: $($missing -join ', ')"
}
if ($found.Count -eq 0) {
    Write-Warn "No se encontro NINGUN CLI de agente soportado (opencode, kimi, codex, claude)."
    Write-Warn "GRAFENO se ha instalado, pero NO podra ejecutar ninguna tarea hasta que instales alguno."
    Write-Info "Instala al menos uno: https://opencode.ai - https://moonshotai.github.io/kimi-code/ - https://github.com/openai/codex - https://docs.anthropic.com/en/docs/claude-code"
} else {
    Write-Ok "CLIs de agentes detectados: $($found -join ', ')"
}

Write-Ok "Listo. Ejecuta: grafeno"
