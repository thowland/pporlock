#!/usr/bin/env bash
# Installs the repository git hooks. Idempotent.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
git -C "$ROOT" config core.hooksPath .githooks
chmod +x "$ROOT"/.githooks/* 2>/dev/null || true
echo "hooks installed (core.hooksPath = .githooks)"
