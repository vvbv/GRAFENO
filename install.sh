#!/usr/bin/env bash
# Instalador de GRAFENO para Linux y macOS.
# Instala GRAFENO con pipx (entorno aislado). Requiere Python 3.11+.
# Uso: ./install.sh
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
MIN_VERSION="3.11"

info() { printf '\033[1;34m[i]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[✓]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; }

OS="$(uname -s)"
case "$OS" in
  Linux)  PKG_HINT="sudo apt install pipx   # Debian/Ubuntu · Fedora: sudo dnf install pipx · Arch: sudo pacman -S python-pipx" ;;
  Darwin) PKG_HINT="brew install pipx" ;;
  *)      PKG_HINT="https://pipx.pypa.io/stable/installation/" ;;
esac

# --- 1. Python >= 3.11 ------------------------------------------------------
PY=""
for cmd in python3 python; do
  if command -v "$cmd" >/dev/null 2>&1 && \
     "$cmd" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    PY="$cmd"
    break
  fi
done

if [ -z "$PY" ]; then
  err "No se encontró Python $MIN_VERSION o superior."
  case "$OS" in
    Linux)  info "Instálalo con el gestor de paquetes de tu distro, p.ej.: sudo apt install python3" ;;
    Darwin) info "Instálalo con: brew install python@3.13   (o desde https://www.python.org)" ;;
  esac
  exit 1
fi
ok "Python: $("$PY" --version 2>&1)"

# --- 2. pipx ----------------------------------------------------------------
if ! command -v pipx >/dev/null 2>&1; then
  info "pipx no está instalado; intentando instalarlo en el entorno de usuario..."
  if "$PY" -m pip install --user pipx >/dev/null 2>&1; then
    "$PY" -m pipx ensurepath >/dev/null 2>&1 || true
    hash -r
  fi
fi

# PIPX_CMD queda como variable; se usa sin comillas para permitir "python3 -m pipx".
if command -v pipx >/dev/null 2>&1; then
  PIPX_CMD="pipx"
elif "$PY" -m pipx --version >/dev/null 2>&1; then
  PIPX_CMD="$PY -m pipx"
else
  err "No se pudo instalar pipx automáticamente."
  info "Instálalo manualmente y vuelve a ejecutar este script:"
  info "  $PKG_HINT"
  exit 1
fi
ok "pipx: $($PIPX_CMD --version 2>/dev/null || echo instalado)"

# --- 3. Instalar GRAFENO ----------------------------------------------------
info "Instalando GRAFENO desde $REPO_ROOT ..."
cd "$REPO_ROOT"
$PIPX_CMD install --force .
$PIPX_CMD ensurepath >/dev/null 2>&1 || true
hash -r

BIN_DIR="$($PIPX_CMD environment --value PIPX_BIN_DIR 2>/dev/null || true)"
[ -n "${BIN_DIR:-}" ] || BIN_DIR="$HOME/.local/bin"

if [ -x "$BIN_DIR/grafeno" ]; then
  ok "GRAFENO instalado: $BIN_DIR/grafeno"
else
  err "La instalación terminó pero no se encontró el ejecutable $BIN_DIR/grafeno"
  exit 1
fi

if ! command -v grafeno >/dev/null 2>&1; then
  warn "'grafeno' aún no está en el PATH de esta sesión."
  info "Abre una terminal nueva o ejecuta:  export PATH=\"$BIN_DIR:\$PATH\""
fi

# --- 4. CLIs de agentes (dependencias en tiempo de ejecución) ---------------
MISSING=""
FOUND=""
for cli in opencode kimi codex claude; do
  if command -v "$cli" >/dev/null 2>&1; then
    FOUND="$FOUND $cli"
  else
    MISSING="$MISSING $cli"
  fi
done
if [ -n "$MISSING" ]; then
  warn "CLIs de agentes no encontrados:$MISSING"
fi
if [ -z "$FOUND" ]; then
  warn "No se encontró NINGÚN CLI de agente soportado (opencode, kimi, codex, claude)."
  warn "GRAFENO se ha instalado, pero NO podrá ejecutar ninguna tarea hasta que instales alguno."
  info "Instala al menos uno: https://opencode.ai · https://moonshotai.github.io/kimi-code/ · https://github.com/openai/codex · https://docs.anthropic.com/en/docs/claude-code"
else
  ok "CLIs de agentes detectados:$FOUND"
fi

ok "Listo. Ejecuta: grafeno"
