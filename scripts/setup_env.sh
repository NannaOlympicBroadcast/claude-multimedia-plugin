#!/usr/bin/env bash
# Install / upgrade everything the claude-multimedia plugin needs, then run the doctor.
# Fails loudly on anything it cannot install (no silent fallbacks).
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-python3}"
FAILED=()

have() { command -v "$1" >/dev/null 2>&1; }
SUDO=""
if [ "$(id -u 2>/dev/null || echo 0)" != "0" ] && have sudo; then SUDO="sudo"; fi

install_pkg() {  # $1 = binary to check, $2.. = package names
  local bin="$1"; shift
  if have "$bin"; then echo "[ok] $bin: $(command -v "$bin")"; return 0; fi
  echo "[..] installing $* ..."
  if have apt-get; then
    $SUDO apt-get install -y "$@" >/dev/null 2>&1 || { $SUDO apt-get update >/dev/null 2>&1 && $SUDO apt-get install -y "$@" >/dev/null 2>&1; }
  elif have dnf; then $SUDO dnf install -y "$@" >/dev/null 2>&1
  elif have yum; then $SUDO yum install -y "$@" >/dev/null 2>&1
  elif have pacman; then $SUDO pacman -S --noconfirm "$@" >/dev/null 2>&1
  elif have apk; then $SUDO apk add "$@" >/dev/null 2>&1
  elif have brew; then brew install "$@" >/dev/null 2>&1
  elif have choco; then choco install -y "$@" >/dev/null 2>&1
  elif have winget; then for p in "$@"; do winget install -e --id "$p" >/dev/null 2>&1; done
  fi
  if have "$bin"; then echo "[ok] $bin installed"; else echo "[FAIL] could not install $bin ($*)"; FAILED+=("$bin"); fi
}

install_pkg aria2c aria2
install_pkg ffmpeg ffmpeg
have ffprobe || FAILED+=("ffprobe")

PIP_PKGS=("yt-dlp[default]" deno edge-tts librosa soundfile numpy requests matplotlib)
echo "[..] pip install -U ${PIP_PKGS[*]}"
if ! "$PY" -m pip install -q -U "${PIP_PKGS[@]}" 2>/tmp/mm_pip.err; then
  "$PY" -m pip install -q -U --user "${PIP_PKGS[@]}" 2>>/tmp/mm_pip.err \
    || "$PY" -m pip install -q -U --break-system-packages "${PIP_PKGS[@]}" 2>>/tmp/mm_pip.err \
    || { echo "[FAIL] pip install failed:"; tail -20 /tmp/mm_pip.err; FAILED+=("pip"); }
fi

mkdir -p "$HOME/.config/claude-multimedia"
if [ ! -f "$HOME/.config/claude-multimedia/config.env" ]; then
  cp "$HERE/../config.example.env" "$HOME/.config/claude-multimedia/config.env"
  chmod 600 "$HOME/.config/claude-multimedia/config.env"
  echo "[ok] created ~/.config/claude-multimedia/config.env (fill in your API keys)"
fi

echo
"$PY" "$HERE/doctor.py"
if [ ${#FAILED[@]} -gt 0 ]; then
  echo
  echo "以下依赖安装失败，请手动安装后重试: ${FAILED[*]}"
  exit 2
fi
