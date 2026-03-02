#!/bin/bash
#
# Post-installation test script for pginstall.py
# Verifies PostgreSQL and extensions are correctly installed
#

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

PASS="${GREEN}PASS${NC}"
FAIL="${RED}FAIL${NC}"
WARN="${YELLOW}WARN${NC}"

PG_BASE="/usr/local/postgresql"
PG_BIN="${PG_BASE}/bin"
TEST_DB="pginstall_test_$$"
ERRORS=0

echo "=============================================="
echo "PostgreSQL Installation Test"
echo "=============================================="
echo

# ----------------------------------------------
# Check binaries exist
# ----------------------------------------------
echo "Checking PostgreSQL binaries..."

for bin in psql postgres initdb pg_ctl pg_config; do
    if [[ -x "${PG_BIN}/${bin}" ]]; then
        echo -e "  ${bin}: ${PASS}"
    else
        echo -e "  ${bin}: ${FAIL} (not found or not executable)"
        ((ERRORS++))
    fi
done
echo

# ----------------------------------------------
# Check PostgreSQL version
# ----------------------------------------------
echo "PostgreSQL version:"
${PG_BIN}/psql --version
echo

# ----------------------------------------------
# Check ICU linkage
# ----------------------------------------------
echo "Checking ICU library linkage..."

if [[ "$(uname)" == "Darwin" ]]; then
    ICU_LIBS=$(otool -L ${PG_BIN}/postgres 2>/dev/null | grep -i icu || true)
else
    ICU_LIBS=$(ldd ${PG_BIN}/postgres 2>/dev/null | grep -i icu || true)
fi

if [[ -n "$ICU_LIBS" ]]; then
    echo -e "  ICU linked: ${PASS}"
    echo "$ICU_LIBS" | sed 's/^/    /'
else
    echo -e "  ICU linked: ${FAIL}"
    ((ERRORS++))
fi
echo

# ----------------------------------------------
# Check ICU library rpaths (Linux only)
# ----------------------------------------------
if [[ "$(uname)" == "Linux" ]]; then
    echo "Checking ICU library rpaths..."
    ICU_LIB="/usr/local/icu/lib"
    if [[ -d "$ICU_LIB" ]]; then
        MISSING_RPATH=0
        for lib in ${ICU_LIB}/libicu*.so.*; do
            if [[ -f "$lib" && ! -L "$lib" ]]; then
                RPATH=$(patchelf --print-rpath "$lib" 2>/dev/null || echo "")
                if [[ "$RPATH" != *"/usr/local/icu/lib"* ]]; then
                    echo -e "  $(basename $lib): ${WARN} (missing rpath)"
                    ((MISSING_RPATH++))
                fi
            fi
        done
        if [[ $MISSING_RPATH -eq 0 ]]; then
            echo -e "  All ICU libraries have correct rpath: ${PASS}"
        fi
    else
        echo -e "  ICU lib directory not found: ${WARN}"
    fi
    echo
fi

# ----------------------------------------------
# Check if PostgreSQL is running
# ----------------------------------------------
echo "Checking PostgreSQL server status..."

PG_DATA="${PG_BASE}/data"
PG_RUNNING=false

if [[ -d "$PG_DATA" ]]; then
    if ${PG_BIN}/pg_ctl status -D "$PG_DATA" >/dev/null 2>&1; then
        echo -e "  Server running: ${PASS}"
        PG_RUNNING=true
    else
        echo -e "  Server running: ${WARN} (not running)"
        echo "  To start: ${PG_BIN}/pg_ctl -D ${PG_DATA} -l logfile start"
    fi
else
    echo -e "  Data directory: ${WARN} (not initialized)"
    echo "  To initialize:"
    echo "    sudo mkdir -p ${PG_DATA}"
    echo "    sudo chown \$(whoami) ${PG_DATA}"
    echo "    ${PG_BIN}/initdb -D ${PG_DATA}"
fi
echo

# Exit early if server not running
if [[ "$PG_RUNNING" != "true" ]]; then
    echo "=============================================="
    echo "Server not running - skipping database tests"
    echo "=============================================="
    if [[ $ERRORS -gt 0 ]]; then
        echo -e "\n${RED}Tests completed with ${ERRORS} error(s)${NC}"
        exit 1
    fi
    exit 0
fi

# ----------------------------------------------
# Create test database
# ----------------------------------------------
echo "Creating test database '${TEST_DB}'..."
${PG_BIN}/createdb "$TEST_DB" 2>/dev/null || {
    echo -e "  Create database: ${FAIL}"
    ((ERRORS++))
    exit 1
}
echo -e "  Create database: ${PASS}"
echo

# Cleanup function
cleanup() {
    echo
    echo "Cleaning up..."
    ${PG_BIN}/dropdb "$TEST_DB" 2>/dev/null || true
    echo "  Dropped test database"
}
trap cleanup EXIT

# ----------------------------------------------
# Test extensions
# ----------------------------------------------
echo "Testing extensions..."

test_extension() {
    local ext=$1
    local test_query=$2

    if ${PG_BIN}/psql -d "$TEST_DB" -c "CREATE EXTENSION IF NOT EXISTS ${ext};" 2>/dev/null; then
        if [[ -n "$test_query" ]]; then
            if ${PG_BIN}/psql -d "$TEST_DB" -t -c "$test_query" >/dev/null 2>&1; then
                echo -e "  ${ext}: ${PASS}"
            else
                echo -e "  ${ext}: ${WARN} (loaded but query failed)"
            fi
        else
            echo -e "  ${ext}: ${PASS}"
        fi
        return 0
    else
        echo -e "  ${ext}: ${FAIL}"
        ((ERRORS++))
        return 1
    fi
}

# Contrib extensions
test_extension "citext" "SELECT 'Hello'::citext = 'HELLO'::citext;"
test_extension "cube" "SELECT cube(1,2,3);"
test_extension "earthdistance" "SELECT earth_distance(ll_to_earth(40.7, -74.0), ll_to_earth(34.0, -118.2));"
test_extension "pgcrypto" "SELECT gen_random_uuid();"
test_extension "pg_trgm" "SELECT similarity('word', 'words');"

# External extensions
test_extension "q3c" "SELECT q3c_version();"

# pgast (optional - may not be installed)
if ${PG_BIN}/psql -d "$TEST_DB" -c "SELECT * FROM pg_available_extensions WHERE name = 'pgast';" -t | grep -q pgast; then
    test_extension "pgast"
else
    echo -e "  pgast: ${YELLOW}SKIP${NC} (not installed)"
fi

# pgvector (optional - installed separately)
if ${PG_BIN}/psql -d "$TEST_DB" -c "SELECT * FROM pg_available_extensions WHERE name = 'vector';" -t | grep -q vector; then
    test_extension "vector" "SELECT '[1,2,3]'::vector;"
else
    echo -e "  vector: ${YELLOW}SKIP${NC} (not installed)"
fi
echo

# ----------------------------------------------
# Test basic operations
# ----------------------------------------------
echo "Testing basic database operations..."

# Create table with ICU collation
if ${PG_BIN}/psql -d "$TEST_DB" -c "
    CREATE TABLE test_icu (
        id serial PRIMARY KEY,
        name text COLLATE \"en-x-icu\"
    );
    INSERT INTO test_icu (name) VALUES ('apple'), ('Banana'), ('cherry');
    SELECT name FROM test_icu ORDER BY name;
" >/dev/null 2>&1; then
    echo -e "  ICU collation: ${PASS}"
else
    echo -e "  ICU collation: ${FAIL}"
    ((ERRORS++))
fi

# Test JIT (if available)
JIT_STATUS=$(${PG_BIN}/psql -d "$TEST_DB" -t -c "SHOW jit;" 2>/dev/null | tr -d ' ')
if [[ "$JIT_STATUS" == "on" ]]; then
    echo -e "  JIT enabled: ${PASS}"
else
    echo -e "  JIT enabled: ${YELLOW}SKIP${NC} (not compiled with LLVM)"
fi
echo

# ----------------------------------------------
# Summary
# ----------------------------------------------
echo "=============================================="
if [[ $ERRORS -eq 0 ]]; then
    echo -e "${GREEN}All tests passed!${NC}"
else
    echo -e "${RED}Tests completed with ${ERRORS} error(s)${NC}"
fi
echo "=============================================="

exit $ERRORS
