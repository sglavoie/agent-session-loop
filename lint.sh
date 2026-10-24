#!/bin/bash
################################################################################
# Shell Script Linter
#
# Runs ShellCheck on all shell scripts in the repository.
# Finds Git-visible scripts by .sh extension, or by shebang for extensionless
# executables.
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

# Collect shell scripts: tracked and untracked-but-not-ignored files, so
# ignored runtime trees (node_modules, caches) are never walked.
scripts=()
while IFS= read -r -d '' rel; do
    f="$SCRIPT_DIR/$rel"
    [[ -f "$f" ]] || continue
    if [[ "$rel" == *.sh ]]; then
        scripts+=("$f")
        continue
    fi
    # Extensionless executables with a shell shebang: `#!/usr/bin/env bash` or
    # any absolute path to sh/bash, including Homebrew's.
    [[ "$(basename "$rel")" == *.* ]] && continue
    [[ -x "$f" ]] || continue
    if head -1 "$f" 2>/dev/null | grep -qE '^#!\s*(/usr/bin/env\s+|/\S*/)(ba)?sh(\s|$)'; then
        scripts+=("$f")
    fi
done < <(git -C "$SCRIPT_DIR" ls-files -z --cached --others --exclude-standard)

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
