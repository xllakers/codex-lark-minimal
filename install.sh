#!/usr/bin/env bash
set -euo pipefail

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

echo "Installed codex-lark-minimal to $INSTALL_HOME"
echo ""
echo "Next:"
echo "  1. Edit $CONFIG"
echo "  2. Run $INSTALL_HOME/run.sh doctor"
echo "  3. Run $INSTALL_HOME/run.sh daemon"
