#!/bin/bash
# create_app.sh — builds PDF Redactor.app in /Applications
# Run once after cloning: bash create_app.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP="/Applications/PDF Redactor.app"
MACOS="$APP/Contents/MacOS"
RESOURCES="$APP/Contents/Resources"

echo "=== Building PDF Redactor.app ==="

# --- App bundle skeleton ---
mkdir -p "$MACOS" "$RESOURCES"

# --- Info.plist ---
cat > "$APP/Contents/Info.plist" << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>PDF Redactor</string>
    <key>CFBundleDisplayName</key>
    <string>PDF Redactor</string>
    <key>CFBundleIdentifier</key>
    <string>com.local.pdf-redactor</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>CFBundleExecutable</key>
    <string>launch</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>LSUIElement</key>
    <false/>
    <key>LSMinimumSystemVersion</key>
    <string>11.0</string>
    <key>LSArchitecturePriority</key>
    <array>
        <string>arm64</string>
    </array>
</dict>
</plist>
EOF

# --- Icon ---
cp "$SCRIPT_DIR/AppIcon.icns" "$RESOURCES/AppIcon.icns"

# --- Detect architecture ---
ARCH=$(uname -m)
if [ "$ARCH" = "arm64" ]; then
    ARCH_PREFIX="arch -arm64"
else
    ARCH_PREFIX=""
fi

# --- Launch script ---
cat > "$MACOS/launch" << LAUNCH
#!/bin/bash
VENV="\$HOME/.venvs/pdf-redactor"
PROJECT="$SCRIPT_DIR"
PID_FILE="\$VENV/server.pid"
LOG_FILE="\$VENV/server.log"
PORT=8501

if [ -f "\$PID_FILE" ]; then
    kill "\$(cat "\$PID_FILE")" 2>/dev/null
    rm -f "\$PID_FILE"
fi
lsof -ti tcp:\$PORT | xargs kill -9 2>/dev/null
sleep 0.5

export PYTHONPATH="\$PROJECT:\$PYTHONPATH"
cd "\$HOME"
$ARCH_PREFIX "\$VENV/bin/python3" -m streamlit run "\$PROJECT/app.py" \\
    --server.port \$PORT \\
    --server.headless true \\
    --browser.gatherUsageStats false \\
    > "\$LOG_FILE" 2>&1 &

echo \$! > "\$PID_FILE"

for i in \$(seq 1 30); do
    curl -s "http://localhost:\$PORT" > /dev/null 2>&1 && break
    sleep 0.5
done

open "http://localhost:\$PORT"
LAUNCH

chmod +x "$MACOS/launch"

# --- Refresh launch services so Dock picks up icon ---
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
    -f "$APP" 2>/dev/null || true

echo ""
echo "Done! PDF Redactor.app is in /Applications."
echo "Drag it to your Dock, or double-click it from Finder."
echo ""
echo "First launch will set up the Python environment (~2 min)."
echo "For AI mode, see README.md for Ollama setup instructions."
