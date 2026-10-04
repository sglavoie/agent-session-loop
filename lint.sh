#!/bin/bash
################################################################################
# Shell Script Linter
#
# Runs ShellCheck on all shell scripts in the repository.
# Finds scripts by .sh extension and shebang detection for extensionless files.
#
# Usage:
#   ./lint.sh
################################################################################

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

info() { echo -e "${BLUE}i $1${NC}"; }
pass() { echo -e "${GREEN}ok $1${NC}"; }
fail() { echo -e "${RED}FAIL $1${NC}"; }

# Check for shellcheck
if ! command -v shellcheck &> /dev/null; then
    fail "shellcheck is not installed"
    echo "Install: brew install shellcheck  (macOS) or apt install shellcheck (Debian/Ubuntu)"
    exit 1
fi

# Collect shell scripts
scripts=()

is_git_ignored() {
    local path="$1"
    local relative="${path#"$SCRIPT_DIR"/}"

    git -C "$SCRIPT_DIR" check-ignore -q -- "$relative" 2>/dev/null
}

# 1. Find all .sh files (excluding .git/ and Git-ignored runtime files)
while IFS= read -r -d '' f; do
    is_git_ignored "$f" && continue
    scripts+=("$f")
done < <(find "$SCRIPT_DIR" -path "$SCRIPT_DIR/.git" -prune -o -name '*.sh' -type f -print0)

# 2. Find extensionless files with a bash/sh shebang (same exclusions)
while IFS= read -r -d '' f; do
    is_git_ignored "$f" && continue
    # Skip files that already have an extension
    [[ "$(basename "$f")" == *.* ]] && continue
    # Check for a shell shebang: `#!/usr/bin/env bash` or any absolute path to
    # sh/bash, including Homebrew's (`#!/opt/homebrew/bin/bash`)
    if head -1 "$f" 2>/dev/null | grep -qE '^#!\s*(/usr/bin/env\s+|/\S*/)(ba)?sh(\s|$)'; then
        scripts+=("$f")
    fi
done < <(find "$SCRIPT_DIR" -path "$SCRIPT_DIR/.git" -prune -o -type f -perm -100 -print0)

if [[ ${#scripts[@]} -eq 0 ]]; then
    fail "No shell scripts found"
    exit 1
fi

info "Found ${#scripts[@]} shell script(s):"
for s in "${scripts[@]}"; do
    echo "  ${s#"$SCRIPT_DIR"/}"
done
echo ""

# Run shellcheck
info "Running shellcheck..."
echo ""

if shellcheck "${scripts[@]}"; then
    echo ""
    pass "All ${#scripts[@]} script(s) passed shellcheck"
else
    echo ""
    fail "ShellCheck found issues"
    exit 1
fi
