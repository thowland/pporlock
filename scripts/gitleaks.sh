#!/usr/bin/env bash
# gitleaks wrapper. Uses the installed binary if present, otherwise Docker is
# unavailable to us by design, so we fall back to a built-in scan and say so
# loudly rather than silently passing.
set -euo pipefail

MODE="${1:-detect}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if command -v gitleaks >/dev/null 2>&1; then
    case "$MODE" in
        protect) exec gitleaks protect --staged --redact --config "$ROOT/.gitleaks.toml" ;;
        *)       exec gitleaks detect --redact --no-banner --config "$ROOT/.gitleaks.toml" ;;
    esac
fi

echo "WARNING: gitleaks not installed — running the built-in fallback scan."
echo "         Install it for full coverage:  brew install gitleaks"
exec "$ROOT/scripts/secret-scan.sh" "$MODE"
