#!/usr/bin/env bash
# Installs dependencies for the MTGA Collection Exporter GUI on macOS / Linux.
set -e

if ! command -v python3 >/dev/null 2>&1; then
    echo "Error: python3 not found." >&2
    echo "Install Python 3 first:" >&2
    echo "  macOS (Homebrew): brew install python-tk" >&2
    echo "  Linux (Debian/Ubuntu): sudo apt install python3 python3-tk python3-pip" >&2
    exit 1
fi

if ! python3 -c "import tkinter" >/dev/null 2>&1; then
    echo "Warning: tkinter is not available for your Python install." >&2
    echo "  macOS (Homebrew): brew install python-tk" >&2
    echo "  Linux (Debian/Ubuntu): sudo apt install python3-tk" >&2
    echo "Continuing with the requests install anyway." >&2
fi

echo "Installing Python dependencies (requests)..."
python3 -m pip install --user requests

echo
echo "Done. Launch the GUI with:"
echo "  python3 mtga_export_gui.py"
echo
echo "Note: MTG Arena does not run natively on macOS/Linux, so the live memory"
echo "scanner (mtg.py) is Windows-only. Run the scanner on a Windows machine to"
echo "produce mtga_collection.json, then use this GUI on any platform to filter"
echo "by color and re-export."
