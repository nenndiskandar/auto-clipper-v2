#!/usr/bin/env bash
# Start Auto Clipper Web UI (Linux/macOS)
cd "$(dirname "$0")/webjs" || exit 1
exec node server.js