#!/bin/bash
# One-click installer: Python packages in a private environment, a Desktop Pet app on the
# Desktop with the pet icon, then starts the pet. Safe to run again at any time.

fail() {
    echo
    echo "  $1"
    echo
    read -r -p "  Press Return to close this window. " _
    exit 1
}

cd "$(dirname "$0")/.." || fail "Could not open the Desktop Pet folder."
[ -f desktop_cat.py ] || fail "desktop_cat.py was not found. Keep this install folder inside the Desktop Pet folder."

DATA_DIR="$HOME/Library/Application Support/DesktopCat"
VENV="$DATA_DIR/venv"
CHECK='import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'

find_python() {
    # /usr/bin/python3 is left out: it is too old, and without the Xcode tools it opens a dialog.
    for candidate in python3.13 python3.12 python3.11 python3.10 \
        /opt/homebrew/bin/python3 /usr/local/bin/python3 \
        /Library/Frameworks/Python.framework/Versions/Current/bin/python3; do
        if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c "$CHECK" >/dev/null 2>&1; then
            echo "$candidate"
            return 0
        fi
    done
    return 1
}

echo
echo "  Installing Desktop Pet"
echo "  ======================"
echo

if [ ! -x "$VENV/bin/python" ]; then
    echo "[1/4] Looking for Python 3.10 or newer..."
    PYTHON="$(find_python)"
    if [ -z "$PYTHON" ] && command -v brew >/dev/null 2>&1; then
        echo "      Python not found. Installing it now..."
        brew install python || fail "Python could not be installed automatically."
        PYTHON="$(find_python)"
    fi
    if [ -z "$PYTHON" ]; then
        open "https://www.python.org/downloads/macos/"
        fail "Python 3.10 or newer is needed. Install it from the page that just opened, then run this file again."
    fi
    echo "[2/4] Creating a private Python environment..."
    mkdir -p "$DATA_DIR" || fail "Could not create $DATA_DIR."
    "$PYTHON" -m venv "$VENV" || fail "Could not create the Python environment."
else
    echo "[1/4] Python environment already there."
    echo "[2/4] Skipped."
fi

echo "[3/4] Installing required packages (this can take a minute)..."
"$VENV/bin/python" -m pip install --quiet --disable-pip-version-check -r requirements.txt \
    || fail "Could not install the packages. Check your internet connection and run this file again."

echo "[4/4] Creating the Desktop app and starting the pet..."
"$VENV/bin/python" desktop_cat.py --install-shortcut || fail "Could not create the Desktop app."
open "$HOME/Desktop/Desktop Pet.app" || fail "Could not start the app."

echo
echo "  Done! Look for your pet above your Dock."
echo "  Next time, open \"Desktop Pet\" on your Desktop."
echo
sleep 5
osascript -e 'tell application "Terminal" to close front window' >/dev/null 2>&1 &
exit 0
