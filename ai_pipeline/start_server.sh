#!/bin/bash
# ThoxForge AI Pipeline Server Startup Script
# ===========================================
# Starts the Python AI pipeline server and optionally preloads the model.
#
# Usage:
#   ./start_server.sh              # Start with defaults (TRELLIS, port 7861)
#   ./start_server.sh --preload     # Preload model on startup
#   ./start_server.sh --backend triposr   # Use TripoSR backend
#   ./start_server.sh --help        # Show all options

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Check Python
PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" &>/dev/null; then
    echo "ERROR: Python 3 not found. Install Python 3.10 or 3.11."
    exit 1
fi

echo "╔══════════════════════════════════════════════════════════╗"
echo "║          ThoxForge AI Pipeline Server                    ║"
echo "║          Photo → Manifold 3D-Printable Mesh             ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""
echo "Python: $($PYTHON --version)"
echo "Directory: $SCRIPT_DIR"
echo ""

# Check for virtual environment
if [ -d ".venv" ]; then
    echo "Activating virtual environment..."
    source .venv/bin/activate 2>/dev/null || source .venv/Scripts/activate 2>/dev/null
    PYTHON=python
fi

# Check dependencies
check_deps() {
    $PYTHON -c "import flask, trimesh, numpy, PIL" 2>/dev/null && return 0
    echo "Missing dependencies. Installing..."
    $PYTHON -m pip install -r requirements.txt
}

check_deps

# Check for TRELLIS
if [ ! -d "TRELLIS" ]; then
    echo "TRELLIS not found. Clone it?"
    read -p "Clone Microsoft/TRELLIS? (y/n) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        git clone https://github.com/microsoft/TRELLIS.git
        echo "Installing TRELLIS dependencies..."
        cd TRELLIS
        pip install -r requirements.txt
        pip install extensions/nvdiffrast
        pip install extensions/diff-gaussian-rasterization
        pip install extensions/simple-knn
        cd ..
        echo "TRELLIS installed."
    else
        echo "WARNING: TRELLIS not available. Only TripoSR backend will work."
    fi
fi

# Check for TripoSR
if [ ! -d "TripoSR" ]; then
    echo "TripoSR not found. Clone it?"
    read -p "Clone TripoSR? (y/n) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        git clone https://github.com/VAST-AI-Research/TripoSR.git
        cd TripoSR
        pip install -r requirements.txt
        cd ..
        echo "TripoSR installed."
    fi
fi

# Start server
echo ""
echo "Starting ThoxForge AI Pipeline Server..."
echo "  API: http://127.0.0.1:7861"
echo "  Health: http://127.0.0.1:7861/health"
echo ""

exec $PYTHON server.py "$@"