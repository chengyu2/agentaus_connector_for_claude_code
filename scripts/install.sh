#!/usr/bin/env bash
# Install the Agentaus bridge for Claude Code.
#
#   curl -fsSL https://raw.githubusercontent.com/chengyu2/agentaus_connector_for_claude_code/main/scripts/install.sh | bash
#
# or, from a clone:  ./scripts/install.sh
#
# This script only does what has to happen before Python can take over: find Python 3.11+,
# fetch the bridge (when piped from curl), create the virtualenv and install the
# installer's own packages. Then it hands over to `python -m agentaus_bridge setup`, which
# lets you pick components and confirms each step. Options are passed through, e.g.
#
#   curl -fsSL .../install.sh | bash -s -- --preset full
#   ./scripts/install.sh --yes --preset minimal --settings none      (no questions)
#   ./scripts/install.sh --dry-run                                   (change nothing)
#
# Environment:
#   AGENTAUS_BRIDGE_HOME  where a piped install goes      (default ~/.agentaus-bridge)
#   AGENTAUS_BRIDGE_REPO  the git repository to install   (default the GitHub repo)
#   AGENTAUS_BRIDGE_REF   branch or tag to install        (default main)
#   PYTHON                the Python to use               (default: the newest 3.11+ found)
#   INSTALL_DOCUMENT_READERS=1  same as --with office,ocr  (kept for older instructions)
#
# The whole script is one function called on its last line, so a download cut short
# runs nothing.

main() {
  set -euo pipefail

  local repo="${AGENTAUS_BRIDGE_REPO:-https://github.com/chengyu2/agentaus_connector_for_claude_code.git}"
  local ref="${AGENTAUS_BRIDGE_REF:-main}"
  local home="${AGENTAUS_BRIDGE_HOME:-$HOME/.agentaus-bridge}"
  local caller_cwd="$PWD"

  if [[ -t 1 ]]; then
    bold=$'\033[1m'; dim=$'\033[2m'; red=$'\033[31m'; green=$'\033[32m'; reset=$'\033[0m'
  else
    bold=""; dim=""; red=""; green=""; reset=""
  fi
  say()  { printf '%s\n' "  $*"; }
  ok()   { printf '%s\n' "  ${green}✓${reset} $*"; }
  die()  { printf '%s\n' "  ${red}✗${reset} $*" >&2; exit 1; }

  printf '\n%s\n' "${bold}Agentaus bridge - bootstrap${reset}"

  # --- where the bridge lives ---------------------------------------------------------
  # Run from a clone: install in place. Piped from curl: fetch into $home.
  local root=""
  local self="${BASH_SOURCE[0]:-}"
  if [[ -n "$self" && -f "$self" ]]; then
    local candidate
    candidate="$(cd "$(dirname "$self")/.." && pwd)"
    if [[ -f "$candidate/agentaus_bridge/__main__.py" ]]; then
      root="$candidate"
    fi
  fi

  # Without git: download the archive into a temporary folder first, so a download that
  # dies halfway leaves nothing behind, then copy it over the install (which keeps .venv,
  # .env and .install-state.json - they are not in the archive).
  download_archive() {
    command -v curl >/dev/null 2>&1 || die "Neither git nor curl is installed."
    local tarball="${repo%.git}/archive/${ref}.tar.gz" tmp
    say "Downloading $tarball (git is not installed, so 'agentaus-bridge update' cannot pull)"
    tmp="$(mktemp -d)"
    if ! curl -fsSL "$tarball" | tar -xz -C "$tmp" --strip-components=1; then
      rm -rf "$tmp"; die "download failed"
    fi
    mkdir -p "$root"
    cp -R "$tmp"/. "$root"/
    rm -rf "$tmp"
  }

  if [[ -z "$root" ]]; then
    root="$home"
    if [[ -d "$root/.git" ]]; then
      say "Updating the existing install in $root"
      git -C "$root" fetch --quiet origin "$ref" || die "git fetch failed"
      git -C "$root" checkout --quiet "$ref" 2>/dev/null || git -C "$root" checkout --quiet -B "$ref" "origin/$ref"
      if ! git -C "$root" merge --quiet --ff-only "origin/$ref" 2>/dev/null; then
        printf '%s\n' "  ${red}!${reset} Could not fast-forward $root to origin/$ref (local changes?); installing the version already there." >&2
      fi
    elif [[ -f "$root/agentaus_bridge/__main__.py" ]]; then
      say "Updating the existing install in $root"
      download_archive
    elif [[ -e "$root" && -n "$(ls -A "$root" 2>/dev/null)" ]]; then
      die "$root exists and is not a bridge install. Move it, or set AGENTAUS_BRIDGE_HOME."
    elif command -v git >/dev/null 2>&1; then
      say "Downloading the bridge into $root"
      git clone --quiet --branch "$ref" "$repo" "$root" || die "git clone of $repo failed"
    else
      download_archive
    fi
    ok "Bridge source in $root"
  fi
  cd "$root"

  # --- Python 3.11+ -------------------------------------------------------------------
  local python=""
  local candidates=()
  [[ -n "${PYTHON:-}" ]] && candidates+=("$PYTHON")
  candidates+=(python3.14 python3.13 python3.12 python3.11 python3)
  for c in "${candidates[@]}"; do
    if command -v "$c" >/dev/null 2>&1 \
        && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      python="$(command -v "$c")"
      break
    fi
  done
  if [[ -z "$python" ]]; then
    local found=""
    command -v python3 >/dev/null 2>&1 && found=" (found $(python3 -V 2>&1))"
    say "Python 3.11 or newer is needed${found}. Install it with:"
    case "$(uname)" in
      Darwin) say "  brew install python      (the python3 from Apple's command line tools is too old)";;
      *)
        if command -v apt-get >/dev/null 2>&1; then say "  sudo apt-get install python3 python3-venv"
        elif command -v dnf >/dev/null 2>&1; then say "  sudo dnf install python3"
        elif command -v pacman >/dev/null 2>&1; then say "  sudo pacman -S python"
        else say "  your package manager, or https://www.python.org/downloads/"; fi;;
    esac
    die "then run this installer again (or point PYTHON= at a 3.11+ interpreter)."
  fi
  ok "Python $("$python" -c 'import platform; print(platform.python_version())') ($python)"

  # --- virtualenv and the installer's packages ---------------------------------------
  # A venv is reused only if it runs, is 3.11+ and has pip: a failed `python -m venv`
  # (no ensurepip) leaves a python behind with no pip in it.
  if [[ ! -x .venv/bin/python ]] \
      || ! .venv/bin/python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null \
      || ! .venv/bin/python -m pip --version >/dev/null 2>&1; then
    say "Creating a virtualenv in $root/.venv"
    rm -rf .venv
    local err
    err="$(mktemp)"
    # Python prints the "ensurepip is not available" advice on stdout, not stderr.
    if ! "$python" -m venv .venv >"$err" 2>&1; then
      rm -rf .venv
      if grep -q ensurepip "$err" 2>/dev/null; then
        rm -f "$err"
        local minor
        minor="$("$python" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
        die "Python's venv module is missing. Install it with: sudo apt-get install python${minor}-venv"
      fi
      cat "$err" >&2
      rm -f "$err"
      die "Could not create the virtualenv."
    fi
    rm -f "$err"
  fi
  say "Installing the installer's packages ${dim}(core bridge, questionary, rich)${reset}"
  .venv/bin/python -m pip install --quiet --disable-pip-version-check --upgrade pip \
    || die "pip could not upgrade itself (see above)."
  .venv/bin/python -m pip install --quiet --disable-pip-version-check \
    -r requirements/core.txt -r requirements/setup.txt || die "pip install failed (see above)."
  ok "Ready"

  # --- hand over to the guided setup -------------------------------------------------
  # ${a[@]+"${a[@]}"}: macOS's bash 3.2 calls an empty array unbound under set -u.
  local args=()
  args=(${@+"$@"})
  if [[ "${INSTALL_DOCUMENT_READERS:-0}" == "1" ]]; then
    args+=(--with office,ocr)   # --with adds up, so the user's own --with is kept
  fi
  export AGENTAUS_INSTALL_CWD="$caller_cwd"

  # Piped from curl, stdin is the script itself: the questions must read the terminal.
  if [[ ! -t 0 ]]; then
    if { : </dev/tty; } 2>/dev/null; then
      exec .venv/bin/python -m agentaus_bridge setup ${args[@]+"${args[@]}"} </dev/tty
    fi
  fi
  exec .venv/bin/python -m agentaus_bridge setup ${args[@]+"${args[@]}"}
}

main "$@"
