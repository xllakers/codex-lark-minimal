#!/usr/bin/env bash
set -euo pipefail

# Usage: ./install.sh [--no-setup]
NO_SETUP=0
for arg in "$@"; do
  case "$arg" in
    --no-setup) NO_SETUP=1 ;;
    *) ;;
  esac
done

ROOT="$(cd "$(dirname "$0")" && pwd)"
INSTALL_HOME="${FEISHU_CODEX_HOME:-$HOME/.codex/bridges/codex-lark-minimal}"
VENV="$INSTALL_HOME/.venv"
CONFIG="$INSTALL_HOME/config.env"

mkdir -p "$INSTALL_HOME/state/jobs" "$INSTALL_HOME/logs"

if [ ! -d "$VENV" ]; then
  python3 -m venv "$VENV"
fi

"$VENV/bin/python" -m pip install --upgrade pip >/dev/null
"$VENV/bin/python" -m pip install -e "$ROOT"

if [ ! -f "$CONFIG" ]; then
  cp "$ROOT/config.env.example" "$CONFIG"
  chmod 600 "$CONFIG"
  echo "Created config: $CONFIG"
else
  chmod 600 "$CONFIG"
  echo "Config already exists: $CONFIG"
fi

cat > "$INSTALL_HOME/run.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
INSTALL_HOME="$INSTALL_HOME"
CONFIG="\${FEISHU_CODEX_CONFIG:-\$INSTALL_HOME/config.env}"
export FEISHU_CODEX_CONFIG="\$CONFIG"
exec "\$INSTALL_HOME/.venv/bin/codex-lark" --config "\$CONFIG" "\$@"
EOF
chmod +x "$INSTALL_HOME/run.sh"

# Opportunistic PATH symlink. Only if ~/.local/bin exists AND is already on
# PATH — we don't mutate shell rc files.
SYMLINK_OK=0
LOCAL_BIN="$HOME/.local/bin"
if [ -d "$LOCAL_BIN" ] && case ":$PATH:" in *":$LOCAL_BIN:"*) true;; *) false;; esac; then
  ln -sf "$INSTALL_HOME/run.sh" "$LOCAL_BIN/codex-lark"
  echo "Linked: $LOCAL_BIN/codex-lark -> $INSTALL_HOME/run.sh"
  SYMLINK_OK=1
fi

echo "Installed codex-lark-minimal to $INSTALL_HOME"

if [ "$SYMLINK_OK" -eq 0 ]; then
  echo ""
  echo "To run codex-lark from anywhere, add this alias to your shell rc:"
  echo "  alias codex-lark='$INSTALL_HOME/run.sh'"
fi

if [ "$NO_SETUP" -eq 1 ]; then
  echo ""
  echo "Skipped setup wizard. Edit $CONFIG manually, then:"
  echo "  $INSTALL_HOME/run.sh doctor"
  echo "  $INSTALL_HOME/run.sh daemon"
  exit 0
fi

# Run wizard. It auto-detects non-interactive shells and exits cleanly.
"$INSTALL_HOME/run.sh" setup
