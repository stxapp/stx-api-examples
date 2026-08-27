#!/bin/sh
# install.sh - set up whichever runtimes you actually have.
#
#     ./install.sh
#
# Python gets a virtualenv at python/.venv with the pinned requirements in it.
# JavaScript gets node_modules under javascript/, from the committed lockfile.
# Neither is required: the shell examples (./configure, ./verify) need only curl
# and openssl, and the JavaScript REST examples have no dependencies at all.
#
# Nothing is installed globally and nothing is installed outside this directory.
# If a runtime is missing this says so and moves on - it is not an error.

set -eu

REPO_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
cd "$REPO_DIR"

PYTHON_MIN="3.9"
NODE_MIN="20"

installed=""
skipped=""

note_installed() { installed="$installed$1\n"; }
note_skipped()   { skipped="$skipped$1\n"; }

echo "Setting up $REPO_DIR"
echo

# ---------------------------------------------------------------------------
# Python: python/.venv, from python/requirements.txt
#
# The websockets and cryptography packages are the reason this venv exists.
# Python has no Ed25519 in its standard library, so signing needs cryptography.
# ---------------------------------------------------------------------------

PYTHON=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
            PYTHON=$candidate
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    note_skipped "python  - no python >= $PYTHON_MIN on PATH; python/ examples will not run"
    echo "Python  : not found (>= $PYTHON_MIN), skipping"
else
    PYTHON_VERSION=$("$PYTHON" -c 'import platform; print(platform.python_version())')
    echo "Python  : $PYTHON $PYTHON_VERSION"

    if [ ! -d python/.venv ]; then
        echo "          creating python/.venv"
        "$PYTHON" -m venv python/.venv
    else
        echo "          python/.venv already exists, reusing it"
    fi

    echo "          installing python/requirements.txt"
    ./python/.venv/bin/python -m pip install --quiet --upgrade pip
    ./python/.venv/bin/python -m pip install --quiet -r python/requirements.txt

    note_installed "python  - python/.venv, with cryptography, requests and websockets"
fi

echo

# ---------------------------------------------------------------------------
# JavaScript: javascript/node_modules, from the committed package-lock.json
#
# Only the WebSocket examples need these (phoenix and ws). The REST and GraphQL
# examples use built-in fetch and node:crypto, so they run with nothing
# installed.
# ---------------------------------------------------------------------------

if ! command -v node >/dev/null 2>&1; then
    note_skipped "node    - no node on PATH; javascript/ examples will not run"
    echo "Node    : not found, skipping"
elif ! command -v npm >/dev/null 2>&1; then
    note_skipped "node    - node is present but npm is not; cannot install phoenix and ws"
    echo "Node    : npm not found, skipping"
else
    NODE_VERSION=$(node --version)
    NODE_MAJOR=$(printf '%s' "$NODE_VERSION" | sed 's/^v//; s/\..*//')
    echo "Node    : $NODE_VERSION"

    if [ "$NODE_MAJOR" -lt "$NODE_MIN" ]; then
        note_skipped "node    - $NODE_VERSION is older than v$NODE_MIN; these examples need built-in fetch and Ed25519"
        echo "          older than v$NODE_MIN, skipping"
    else
        echo "          npm ci in javascript/"
        # `npm ci` installs exactly the committed lockfile, which is the point of
        # committing one. It needs the lockfile to be in step with package.json.
        if ! (cd javascript && npm ci --silent); then
            echo "          npm ci failed, falling back to npm install"
            (cd javascript && npm install --silent)
        fi
        note_installed "node    - javascript/node_modules, with phoenix and ws"
    fi
fi

echo

# ---------------------------------------------------------------------------
# The shell examples, which are the fallback when neither runtime is present.
# ---------------------------------------------------------------------------

for tool in curl openssl; do
    if command -v "$tool" >/dev/null 2>&1; then
        note_installed "$tool  - already present, nothing to install"
    else
        note_skipped "$tool  - not found; ./configure and ./verify need it"
    fi
done

echo "Set up:"
if [ -n "$installed" ]; then
    printf "$installed" | sed 's/^/  /'
else
    echo "  nothing"
fi

if [ -n "$skipped" ]; then
    echo
    echo "Skipped:"
    printf "$skipped" | sed 's/^/  /'
fi

echo
echo "Next:"
echo "  ./configure     # store your key id and private key in ~/.stx/"
echo "  ./verify        # sign GET /api/v1/me and print who you are"
