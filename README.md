# PostgreSQL Source Installer

A Python-based installer that automates building PostgreSQL and its dependencies from source on Linux and macOS.

## Why Build from Source?

- **Latest versions**: Get the newest PostgreSQL, ICU, and extensions without waiting for package managers
- **Consistent paths**: All components install to `/usr/local/<package>-<version>` with predictable symlinks
- **Full control**: Pin specific versions or always use the latest
- **JIT support**: Automatically enables LLVM JIT compilation when available
- **Astronomy extensions**: Includes q3c and pgast for spatial/WCS queries

## Quick Start

```bash
# Install prerequisites (Debian/Ubuntu)
sudo apt install build-essential bison flex libreadline-dev zlib1g-dev patchelf git

# Create source directory (one-time setup)
sudo mkdir -p /usr/local/src && sudo chown $(whoami) /usr/local/src

# Preview what will be installed
./pginstall.py --dry-run

# Install everything
./pginstall.py
```

## What Gets Installed

### Core Components

| Component | Description | Install Path |
|-----------|-------------|--------------|
| ICU | International Components for Unicode | `/usr/local/icu-x.y` |
| PostgreSQL | PostgreSQL database server | `/usr/local/postgresql-x.y` |
| readline | GNU Readline (macOS only) | `/usr/local/readline-x.y` |

### PostgreSQL Extensions

| Extension | Description | Source |
|-----------|-------------|--------|
| citext | Case-insensitive text type | PostgreSQL contrib |
| cube | Multi-dimensional cube data type | PostgreSQL contrib |
| earthdistance | Great circle distance calculations | PostgreSQL contrib |
| pg_trgm | Trigram text similarity | PostgreSQL contrib |
| q3c | Spatial indexing for astronomy | [segasai/q3c](https://github.com/segasai/q3c) |
| AST | Starlink AST library (WCS handling) | [Starlink/ast](https://github.com/Starlink/ast) |
| pgast | Starlink AST PostgreSQL extension | [demitri/pgast](https://github.com/demitri/pgast) |
| pgvector | Vector similarity search (separate script) | [pgvector/pgvector](https://github.com/pgvector/pgvector) |

## Prerequisites

### Linux (Debian/Ubuntu)

```bash
sudo apt install build-essential bison flex libreadline-dev zlib1g-dev patchelf git gfortran
```

For JIT support (optional but recommended):
```bash
sudo apt install llvm-dev clang
```

> **Note**: `gfortran` is required for building the Starlink AST library. If using `--exclude-ast`, it can be omitted.

### Linux (Fedora/RHEL)

```bash
sudo dnf install gcc make bison flex readline-devel zlib-devel patchelf git gcc-gfortran
```

For JIT support:
```bash
sudo dnf install llvm-devel clang
```

### macOS

```bash
xcode-select --install
```

## Usage

### pginstall.py

```
./pginstall.py [options]

Options:
  --config FILE       Config file for version pinning (INI format)
  --dry-run           Show what would be done without executing
  --component NAME    Build only specific component
  --skip-extensions   Skip q3c, ast, and pgast
  --exclude-ast       Exclude Starlink AST library and pgast extension
  --verbose           Show all build output
  --completions SHELL Output shell completion script (bash or zsh)
  -h, --help          Show help message
```

**Components:** `readline`, `icu`, `postgresql`, `contrib`, `q3c`, `ast`, `pgast`

#### Examples

```bash
# Preview what will be installed (recommended first step)
./pginstall.py --dry-run

# Install everything with auto-detected latest versions
./pginstall.py

# Install with verbose output
./pginstall.py --verbose

# Install only PostgreSQL (assumes ICU already installed)
./pginstall.py --component postgresql

# Install without astronomy extensions
./pginstall.py --exclude-ast

# Use a config file for version pinning
./pginstall.py --config pginstall.conf
```

### install_pgvector.py

pgvector is installed separately since it may be added to an existing PostgreSQL installation.

```bash
# Preview what will be installed
./install_pgvector.py --dry-run

# Install latest pgvector
./install_pgvector.py

# Install specific version
./install_pgvector.py --version 0.8.0

# Use a different PostgreSQL installation
./install_pgvector.py --pg-config /opt/postgresql/bin/pg_config
```

### create_pg_service.py (Linux only)

Creates a systemd service to run PostgreSQL as a system service. This script:
- Creates a `postgres` system user if it doesn't exist
- Initializes the database cluster
- Creates and enables a systemd service
- Starts PostgreSQL

```bash
# Interactive mode (prompts for settings)
sudo ./create_pg_service.py

# Specify all options
sudo ./create_pg_service.py --pgdata /usr/local/postgresql/data \
                            --logfile /var/log/postgresql/postgresql.log \
                            --port 5432

# Use a custom service name (for multiple instances)
sudo ./create_pg_service.py --service-name postgresql-dev --port 5433
```

Options:
- `--pgdata` - Data directory (default: `/usr/local/postgresql/data`)
- `--logfile` - Log file location (default: `/var/log/postgresql/postgresql.log`)
- `--port` - Port number (default: `5432`)
- `--user` - User to run as (default: `postgres`, created if needed)
- `--service-name` - Systemd service name (default: `postgresql`)

## Configuration File

Pin specific versions instead of auto-detecting latest:

```ini
# pginstall.conf
[versions]
postgresql = 17.2
icu = 76.1
readline = 8.2
q3c = 2.0.1
ast = 9.3.0
pgast = main
```

Copy `pginstall.conf.example` to `pginstall.conf` and modify as needed. Any version not specified will be auto-detected from upstream sources.

## Build Order

Components are built in dependency order:

```
1. readline (macOS only)
   └── Required by PostgreSQL for command-line editing

2. ICU
   └── Required by PostgreSQL for Unicode collation
   └── Automatically fixes library rpaths after install

3. PostgreSQL
   └── Links against ICU and readline
   └── Enables JIT if LLVM is detected

4. Contrib Extensions (from PostgreSQL source)
   ├── citext
   ├── cube
   ├── earthdistance
   └── pg_trgm

5. External Extensions
   ├── q3c
   ├── Starlink AST library
   └── pgast (requires AST)
```

## Post-Installation

### Add PostgreSQL to PATH

```bash
export PATH=/usr/local/postgresql/bin:$PATH
```

Add this to your `~/.bashrc` or `~/.zshrc` for persistence.

### Initialize Database

#### Option 1: Systemd Service (Recommended for Linux)

Use the included script to set up PostgreSQL as a system service:

```bash
sudo ./create_pg_service.py
```

This creates a `postgres` user, initializes the database, and starts PostgreSQL as a systemd service that starts automatically on boot.

#### Option 2: Manual Setup

```bash
# Create data directory
sudo mkdir -p /usr/local/postgresql/data
sudo chown $(whoami) /usr/local/postgresql/data

# Initialize database cluster
/usr/local/postgresql/bin/initdb -D /usr/local/postgresql/data

# Start PostgreSQL
/usr/local/postgresql/bin/pg_ctl -D /usr/local/postgresql/data -l logfile start
```

### Verify Installation

Run the included test script:

```bash
./test_install.sh
```

Or verify manually:

```bash
# Check PostgreSQL version
/usr/local/postgresql/bin/psql --version

# Check ICU is linked (Linux)
ldd /usr/local/postgresql/bin/postgres | grep icu

# Check ICU is linked (macOS)
otool -L /usr/local/postgresql/bin/postgres | grep icu
```

### Test Extensions

```sql
-- Connect to PostgreSQL
psql -d postgres

-- Test contrib extensions
CREATE EXTENSION citext;
CREATE EXTENSION cube;
CREATE EXTENSION earthdistance;
CREATE EXTENSION pg_trgm;

-- Test external extensions
CREATE EXTENSION q3c;
CREATE EXTENSION pgast;

-- Test pgvector (if installed separately)
CREATE EXTENSION vector;
```

## Shell Autocompletion

Both scripts support tab completion for bash and zsh.

### Bash

```bash
# Add to ~/.bashrc
eval "$(./pginstall.py --completions bash)"
eval "$(./install_pgvector.py --completions bash)"
```

### Zsh

```bash
# Add to ~/.zshrc
eval "$(./pginstall.py --completions zsh)"
eval "$(./install_pgvector.py --completions zsh)"
```

## Platform-Specific Notes

### Linux

- Readline is installed via system package manager (not built from source)
- Requires `patchelf` (system version, not anaconda) for rpath fixes
- LLVM/JIT: Auto-detected in `/usr/bin/llvm-config` or `/usr/lib/llvm-*/bin/llvm-config`

### macOS

- Readline is built from source (GNU version, not Apple's libedit)
- Uses `--with-bonjour` for PostgreSQL
- LLVM/JIT: Auto-detected in `/usr/local/opt/llvm/bin/llvm-config` (Homebrew)
- No Homebrew dependencies required

### Both Platforms

- PATH is sanitized during builds (removes `/usr/local/anaconda/bin` to avoid conflicts)
- Sources are downloaded and built in `/usr/local/src` (owned by your user)
- `sudo` is only used for `make install` steps

## Troubleshooting

### Source Directory Does Not Exist

```
Checking source directory (/usr/local/src)...
  Source directory does not exist: /usr/local/src

  To create it, run:
    sudo mkdir -p /usr/local/src && sudo chown $(whoami) /usr/local/src
```

### Missing Prerequisites

```
Checking prerequisites...
  Missing tools: make, gcc

To install missing prerequisites:
  sudo apt install build-essential
```

### LLVM Not Found

```
LLVM not found, building without JIT support
```

This is informational, not an error. PostgreSQL works fine without JIT. To enable JIT:

```bash
# Debian/Ubuntu
sudo apt install llvm-dev clang

# Fedora/RHEL
sudo dnf install llvm-devel clang
```

### patchelf Not Found (with sudo)

If you see `sudo: patchelf: command not found`, install the system patchelf:

```bash
sudo apt install patchelf  # Debian/Ubuntu
sudo dnf install patchelf  # Fedora/RHEL
```

The anaconda version of patchelf won't work with sudo.

## Version Detection

The installer auto-detects latest versions from:

| Component | Source |
|-----------|--------|
| ICU | GitHub API: unicode-org/icu releases |
| PostgreSQL | PostgreSQL FTP directory listing |
| readline | GNU FTP directory listing |
| q3c | GitHub API: segasai/q3c releases |
| AST | GitHub API: Starlink/ast releases |
| pgast | GitHub API: demitri/pgast releases |
| pgvector | GitHub API: pgvector/pgvector tags |

## Files

| File | Description |
|------|-------------|
| `pginstall.py` | Main installer script |
| `install_pgvector.py` | Separate pgvector installer |
| `create_pg_service.py` | Systemd service setup (Linux only) |
| `add_rpaths_to_dylibs.py` | Rpath fixer for shared libraries |
| `test_install.sh` | Post-installation verification script |
| `pginstall.conf.example` | Example configuration file |

## License

This project is provided as-is for building PostgreSQL from source. PostgreSQL, ICU, and all extensions are subject to their respective licenses.
