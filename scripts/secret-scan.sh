#!/usr/bin/env bash
# Fallback secret scan. Narrow by design: it catches the specific things this
# project must never commit, rather than pretending to be a general scanner.
#
# Real risks here (implementation-plan.md §2.5 "Token handling"):
#   - the pporlock bearer token
#   - mitmproxy CA private keys
#   - fixture or real TLS private keys
set -uo pipefail

MODE="${1:-detect}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ "$MODE" = "protect" ]; then
    FILES=$(git diff --cached --name-only --diff-filter=ACM)
else
    FILES=$(git ls-files)
fi

[ -z "$FILES" ] && { echo "secret-scan: nothing to scan"; exit 0; }

FAIL=0
while IFS= read -r f; do
    [ -f "$f" ] || continue
    case "$f" in
        *.png|*.jpg|*.gif|*.woff2|*.ico|*/node_modules/*) continue ;;
        scripts/secret-scan.sh|.gitleaks.toml) continue ;;
    esac

    if grep -qE -- '-----BEGIN [A-Z ]*PRIVATE KEY-----' "$f" 2>/dev/null; then
        echo "SECRET: private key material in $f"; FAIL=1
    fi
    if grep -qiE '(aws_secret_access_key|AKIA[0-9A-Z]{16})' "$f" 2>/dev/null; then
        echo "SECRET: AWS credential in $f"; FAIL=1
    fi
    if grep -qE '(sk-ant-|ghp_[A-Za-z0-9]{36}|xox[baprs]-)' "$f" 2>/dev/null; then
        echo "SECRET: API token in $f"; FAIL=1
    fi
    if grep -qiE '^[[:space:]]*(pporlock_token|bearer_token)[[:space:]]*[=:][[:space:]]*["'"'"']?[A-Za-z0-9_-]{16,}' "$f" 2>/dev/null; then
        echo "SECRET: hard-coded pporlock token in $f"; FAIL=1
    fi
done <<< "$FILES"

case "$FILES" in
    *".pporlock/token"*|*".mitmproxy/"*)
        echo "SECRET: attempt to commit local pporlock or mitmproxy state"; FAIL=1 ;;
esac

if [ "$FAIL" -ne 0 ]; then
    echo "secret-scan: FAILED"
    exit 1
fi
echo "secret-scan: clean ($(echo "$FILES" | wc -l | tr -d ' ') files)"
