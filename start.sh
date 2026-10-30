#!/usr/bin/env bash

# AI CRM System - Launcher (Linux/macOS)
# Auto-installs Python if not found

echo ""
echo "  ============================================"
echo "      AI CRM System - Quick Start Launcher"
echo "  ============================================"
echo ""

# 1. Check Python - auto install if not found
echo "[1/5] Checking Python environment..."
PYTHON=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        $cmd --version &>/dev/null && { PYTHON="$cmd"; break; }
    fi
done

if [ -z "$PYTHON" ]; then
    echo "[WARN] Python not found. Starting automatic installation..."
    echo ""

    # Detect OS
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        OS_ID=$ID
    elif [ "$(uname)" = "Darwin" ]; then
        OS_ID="macos"
    else
        OS_ID="unknown"
    fi

    case "$OS_ID" in
        ubuntu|debian)
            echo "Detected: $OS_ID"
            echo "Installing Python via apt..."
            sudo apt-get update -qq && sudo apt-get install -y -qq python3 python3-pip python3-venv
            ;;
        centos|rhel|fedora|rocky|alma)
            echo "Detected: $OS_ID"
            echo "Installing Python via yum/dnf..."
            if command -v dnf &>/dev/null; then
                sudo dnf install -y -q python3 python3-pip
            else
                sudo yum install -y -q python3 python3-pip
            fi
            ;;
        macos)
            echo "Detected: macOS"
            if command -v brew &>/dev/null; then
                echo "Installing Python via Homebrew..."
                brew install python
            else
                echo "Installing Homebrew first..."
                /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
                brew install python
            fi
            ;;
        *)
            echo "[ERROR] Unsupported OS. Please install Python 3.9+ manually."
            echo "Download: https://www.python.org/downloads/"
            exit 1
            ;;
    esac

    # Re-check after installation
    for cmd in python3 python; do
        if command -v "$cmd" &>/dev/null; then
            $cmd --version &>/dev/null && { PYTHON="$cmd"; break; }
        fi
    done

    if [ -z "$PYTHON" ]; then
        echo "[ERROR] Python installation failed."
        echo "Please install Python 3.9+ manually."
        exit 1
    fi
    echo "[OK] Python installed successfully"
fi

$PYTHON --version
echo ""

# 2. Enter backend directory
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/backend"

# 3. Check .env config file
echo "[2/5] Checking config file..."
if [ ! -f .env ]; then
    if [ -f .env.example ]; then
        cp .env.example .env
        echo "[INFO] Created .env from .env.example template"
        echo "[IMPORTANT] Please edit backend/.env and fill in your DeepSeek API Key"
        echo ""
        echo "Content to modify:"
        echo "  DEEPSEEK_API_KEY = your DeepSeek API key"
        echo ""
        echo "Please edit backend/.env manually, then re-run this script."
        exit 1
    else
        echo "[ERROR] .env.example template not found"
        exit 1
    fi
else
    echo "[OK] Config file exists"
fi
echo ""

# 4. Install dependencies
echo "[3/5] Installing Python dependencies..."
$PYTHON -m pip install --upgrade pip -q 2>/dev/null || true
$PYTHON -m pip install -r requirements.txt || {
    echo "[WARN] Retrying with --no-cache-dir..."
    $PYTHON -m pip install -r requirements.txt --no-cache-dir
}
echo "[OK] Dependencies installed"
echo ""

# 5. Start service
echo "[4/5] Starting service..."
echo ""
echo "--------------------------------------------"
echo "  Service starting, please wait..."
echo "  URL: http://127.0.0.1:8000"
echo "  Press Ctrl+C to stop"
echo "--------------------------------------------"
echo ""

# Start service
$PYTHON app.py &
SERVER_PID=$!

# Wait for service to be ready
echo "[5/5] Waiting for service to start..."
for i in $(seq 1 15); do
    sleep 1
    if curl -s http://127.0.0.1:8000/api/health > /dev/null 2>&1; then
        echo "[OK] Service is ready!"
        break
    fi
done

# Open browser
if command -v open &>/dev/null; then
    open http://127.0.0.1:8000
elif command -v xdg-open &>/dev/null; then
    xdg-open http://127.0.0.1:8000
fi

echo "Browser opened. Please login with your email to start."
echo ""

# Wait for service process
wait $SERVER_PID