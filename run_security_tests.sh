#!/usr/bin/env bash
# =============================================================================
# Security Test Runner
# Usage: bash run_security_tests.sh
# Run from the project root directory.
# =============================================================================

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

PASS=0
FAIL=0
SKIP=0

section() { echo -e "\n${BLUE}${BOLD}══════════════════════════════════════${NC}"; echo -e "${BLUE}${BOLD}  $1${NC}"; echo -e "${BLUE}${BOLD}══════════════════════════════════════${NC}"; }
ok()      { echo -e "  ${GREEN}✔ $1${NC}"; ((PASS++)) || true; }
fail()    { echo -e "  ${RED}✘ $1${NC}"; ((FAIL++)) || true; }
warn()    { echo -e "  ${YELLOW}⚠ $1${NC}"; ((SKIP++)) || true; }

# ── 1. Dependency check ───────────────────────────────────────────────────────
section "1. Checking test dependencies"
for pkg in pytest pytest_asyncio bandit; do
    if python3 -c "import $pkg" 2>/dev/null; then
        ok "$pkg available"
    else
        fail "$pkg NOT installed — run: pip install $pkg pytest-asyncio"
    fi
done

if python3 -c "import httpx" 2>/dev/null; then
    ok "httpx available (dashboard tests enabled)"
else
    warn "httpx not installed — dashboard auth tests will be skipped"
    warn "Install with: pip install httpx"
fi

# ── 2. Static analysis with Bandit ───────────────────────────────────────────
section "2. Static Analysis (Bandit)"
BANDIT_OUT=$(python3 -m bandit -r . \
    --exclude ./.venv,./dashboard/templates,./tests \
    -ll -q 2>&1 || true)

HIGH=$(echo "$BANDIT_OUT" | grep -c "Severity: High" || true)
MED=$(echo "$BANDIT_OUT"  | grep -c "Severity: Medium" || true)
LOW=$(echo "$BANDIT_OUT"  | grep -c "Severity: Low" || true)

echo "  High: $HIGH   Medium: $MED   Low: $LOW"

if [ "$HIGH" -gt 0 ]; then
    fail "HIGH severity issues found — fix these immediately"
    echo "$BANDIT_OUT" | grep -A5 "Severity: High" | sed 's/^/    /'
else
    ok "No HIGH severity issues"
fi

if [ "$MED" -gt 0 ]; then
    warn "$MED MEDIUM severity issue(s) — review manually"
    echo "$BANDIT_OUT" | grep -A3 "Issue\|Location" | grep -v "^--$" | sed 's/^/    /'
else
    ok "No MEDIUM severity issues"
fi

# ── 3. Dependency vulnerability scan ─────────────────────────────────────────
section "3. Dependency Vulnerability Scan (pip-audit)"
if python3 -m pip_audit --version &>/dev/null 2>&1; then
    AUDIT_OUT=$(python3 -m pip_audit -r requirements.txt 2>&1 || true)
    VULN_COUNT=$(echo "$AUDIT_OUT" | grep -c "PYSEC\|CVE" || true)
    if [ "$VULN_COUNT" -gt 0 ]; then
        fail "$VULN_COUNT known vulnerability/vulnerabilities in dependencies"
        echo "$AUDIT_OUT" | grep "PYSEC\|CVE" | sed 's/^/    /'
    else
        ok "No known vulnerabilities in dependencies"
    fi
else
    warn "pip-audit not installed — skipping CVE scan"
    warn "Install: pip install pip-audit"
fi

# ── 4. Plaintext secret scan ──────────────────────────────────────────────────
section "4. Hardcoded Secret Scan"
TOKEN_PATTERN='[A-Za-z0-9_-]{24,}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}'
FOUND_TOKENS=$(grep -rn --include="*.py" -E "$TOKEN_PATTERN" . \
    --exclude-dir=.venv --exclude-dir=__pycache__ --exclude-dir=tests 2>/dev/null || true)
if [ -n "$FOUND_TOKENS" ]; then
    fail "Possible hardcoded Discord token(s) found in source:"
    echo "$FOUND_TOKENS" | sed 's/^/    /'
else
    ok "No hardcoded tokens detected in .py files"
fi

# Check .env for live token
if [ -f ".env" ]; then
    TOKEN_IN_ENV=$(grep -E "^DISCORD_TOKEN\s*=\s*[A-Za-z0-9_.-]{20,}" .env || true)
    if [ -n "$TOKEN_IN_ENV" ]; then
        fail ".env still contains a live DISCORD_TOKEN value — remove it after running setup_token.py"
    else
        ok "DISCORD_TOKEN not set to a live value in .env"
    fi
fi

# Check token.enc exists
if [ -f "token.enc" ]; then
    ok "token.enc exists (encrypted token present)"
else
    warn "token.enc not found — run setup_token.py to encrypt your token"
fi

# ── 5. .gitignore hygiene ─────────────────────────────────────────────────────
section "5. .gitignore Hygiene"
GITIGNORE=".gitignore"
check_gitignore() {
    local pattern="$1" label="$2"
    if grep -q "$pattern" "$GITIGNORE" 2>/dev/null; then
        ok "$label is in .gitignore"
    else
        fail "$label NOT in .gitignore — it may be committed to git!"
    fi
}
check_gitignore ".env"        ".env"
check_gitignore "token.enc"   "token.enc"
check_gitignore "\.db\|data/" "Database files (.db)"
check_gitignore ".venv\|venv" "Virtual environment"

# ── 6. Database path check ────────────────────────────────────────────────────
section "6. Database Path Security"
DB_PATH_RAW=$(python3 -c "import config; print(config.DB_PATH)" 2>/dev/null || true)
if echo "$DB_PATH_RAW" | grep -qE "^/|^[A-Z]:"; then
    ok "DB_PATH is absolute: $DB_PATH_RAW"
else
    fail "DB_PATH is relative ('$DB_PATH_RAW') — use an absolute path (see Prompt 8)"
fi

# ── 7. Run pytest test suite ──────────────────────────────────────────────────
section "7. Running pytest Test Suite"

export PYTHONDONTWRITEBYTECODE=1

run_test() {
    local label="$1" file="$2"
    if python3 -m pytest "$file" -v --tb=short -q 2>&1; then
        ok "$label — all tests passed"
    else
        fail "$label — some tests FAILED (see output above)"
    fi
}

echo ""
echo -e "${BOLD}  → SQL Injection Tests${NC}"
python3 -m pytest tests/test_sql_injection.py -v --tb=short 2>&1
echo ""

echo -e "${BOLD}  → Input Validation Tests${NC}"
python3 -m pytest tests/test_input_validation.py -v --tb=short 2>&1
echo ""

echo -e "${BOLD}  → Token Security Tests${NC}"
python3 -m pytest tests/test_token_security.py -v --tb=short 2>&1
echo ""

echo -e "${BOLD}  → Spam & Detection Logic Tests${NC}"
python3 -m pytest tests/test_spam_detection.py -v --tb=short 2>&1
echo ""

if python3 -c "import utils.link_scanner" 2>/dev/null; then
    echo -e "${BOLD}  → Link Scanner Tests${NC}"
    python3 -m pytest tests/test_link_scanner.py -v --tb=short 2>&1
    echo ""
else
    warn "Link scanner not implemented yet — skipping test_link_scanner.py"
fi

if python3 -c "import httpx" 2>/dev/null && python3 -c "import dashboard.app" 2>/dev/null; then
    echo -e "${BOLD}  → Dashboard Auth Tests${NC}"
    python3 -m pytest tests/test_dashboard_auth.py -v --tb=short 2>&1
    echo ""
else
    warn "Dashboard/httpx not available — skipping test_dashboard_auth.py"
fi

# ── 8. Summary ────────────────────────────────────────────────────────────────
section "Summary"
echo -e "  Passed checks : ${GREEN}${BOLD}$PASS${NC}"
echo -e "  Failed checks : ${RED}${BOLD}$FAIL${NC}"
echo -e "  Skipped/Warn  : ${YELLOW}${BOLD}$SKIP${NC}"
echo ""

if [ "$FAIL" -gt 0 ]; then
    echo -e "${RED}${BOLD}  ✘ SECURITY ISSUES FOUND — do not ship until resolved.${NC}"
    exit 1
else
    echo -e "${GREEN}${BOLD}  ✔ All checks passed.${NC}"
    exit 0
fi
