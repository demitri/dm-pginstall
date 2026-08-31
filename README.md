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
| OpenSSL | Cryptographic library (required by pgcrypto) | `/usr/local/openssl-x.y.z` |
| LLVM | LLVM + clang for JIT (only with `--build-llvm`) | `/usr/local/llvm-x.y.z` |
| PostgreSQL | PostgreSQL database server | `/usr/local/postgresql-x.y` |
| readline | GNU Readline (macOS only) | `/usr/local/readline-x.y` |

### PostgreSQL Extensions

| Extension | Description | Source |
|-----------|-------------|--------|
| citext | Case-insensitive text type | PostgreSQL contrib |
| cube | Multi-dimensional cube data type | PostgreSQL contrib |
| earthdistance | Great circle distance calculations | PostgreSQL contrib |
| ltree | Hierarchical tree-like data type | PostgreSQL contrib |
| pgcrypto | Cryptographic functions | PostgreSQL contrib |
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

To build a private LLVM instead (`--build-llvm`), which is immune to distro LLVM
upgrades:
```bash
sudo apt install cmake ninja-build
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

`pkg-config` is also required. Install from source (https://pkg-config.freedesktop.org/releases/) or via Homebrew (`brew install pkg-config`).

For building the Starlink AST library, `gfortran` is required. Download and install the appropriate version for your macOS from:

https://github.com/fxcoudert/gfortran-for-macOS/releases

> **Note**: If using `--exclude-ast`, gfortran can be omitted.

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
  --with-llvm         Enable LLVM/JIT support
  --build-llvm        Build LLVM from source rather than using the system LLVM
  --verbose           Show all build output
  --completions SHELL Output shell completion script (bash or zsh)
  -h, --help          Show help message
```

**Components:** `readline`, `openssl`, `icu`, `llvm`, `postgresql`, `contrib`, `q3c`, `ast`, `pgast`

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

# Build a private LLVM so distro upgrades can never break JIT
./pginstall.py --build-llvm
```

### Building a private LLVM

PostgreSQL built with `--with-llvm` links `llvmjit.so` against a specific
versioned LLVM runtime. When that runtime is the *system* LLVM, the package
manager has no record of the dependency, so a distribution upgrade can retire it
and break JIT — silently, since the module is loaded lazily and only queries
above `jit_above_cost` fail.

`--build-llvm` removes the failure mode instead of guarding against it. LLVM and
clang are built from source into `/usr/local/llvm-<version>`, PostgreSQL is
linked against that, and an rpath is baked in so `llvmjit.so` finds it at
runtime. Nothing the package manager does can touch it.

```bash
# Build LLVM, then PostgreSQL against it (implies --with-llvm)
./pginstall.py --build-llvm

# See exactly what would happen first
./pginstall.py --build-llvm --dry-run

# Build only LLVM
./pginstall.py --build-llvm --component llvm
```

Once built, `/usr/local/llvm/bin/llvm-config` is preferred over any system LLVM
automatically, so later PostgreSQL rebuilds keep using it with no extra flags.

> **This is a long build.** Expect roughly 30–90 minutes and several GB under
> `/usr/local/src`. It needs `cmake` (and uses `ninja` if present, which is
> substantially faster). Only the host target is built, and link jobs are capped
> independently of compile jobs, since linking LLVM needs several GB per job and
> one link per core will exhaust memory on most machines.

clang is built alongside LLVM and passed to `configure` as `CLANG=`. PostgreSQL
uses clang to emit the bitcode that `llvmjit.so` consumes, and the two must come
from the same LLVM — letting `configure` find an unrelated clang on `PATH` risks
a version mismatch between the bitcode and the runtime that reads it.

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

### pgstatus.py

Discovers and manages PostgreSQL instances on Linux and macOS. Supports systemd services, Homebrew services, and pg_ctl-managed instances.

```bash
# List all instances (default command)
./pgstatus.py

# List with expanded details and commands
./pgstatus.py list --expand

# List as JSON
./pgstatus.py list --json

# Show detailed info for an instance
./pgstatus.py info main

# Start/stop/restart an instance
./pgstatus.py start main
./pgstatus.py stop main
./pgstatus.py restart main

# Preview commands without executing
./pgstatus.py stop main --dry-run
```

Commands:
- `list` - List all PostgreSQL instances (default)
- `info <instance>` - Show detailed info about an instance
- `start <instance>` - Start an instance
- `stop <instance>` - Stop an instance
- `restart <instance>` - Restart an instance

Options:
- `--json` - Output as JSON (for list, info)
- `--expand`, `-e` - Show expanded details including commands (for list)
- `--dry-run` - Show what would be done (for start/stop/restart)
- `--completions` - Output shell completion script

Example output:
```
Instance  Status   Port   Version  Data Directory
--------  ------   ----   -------  --------------
main      running  5432   17.2     /usr/local/postgresql/data/main
dev       stopped  5433   17.2     /usr/local/postgresql/data/dev
```

### pgjitguard.py (Linux only)

Protects a JIT-enabled build against system LLVM upgrades.

PostgreSQL built with `--with-llvm` produces `llvmjit.so`, which links against a
specific versioned LLVM runtime (e.g. `libLLVM.so.20.1`). The package manager has
no record of that dependency, so an ordinary system upgrade can retire the
runtime. The failure is easy to miss: `llvmjit.so` is loaded lazily, so the
server starts cleanly, `pg_isready` succeeds, and cheap queries work — only
queries costing more than `jit_above_cost` fail.

The tool reads `llvmjit.so`'s own `DT_NEEDED` entries, resolves them with `ldd`,
and asks `dpkg` which packages own them. The dependency set is always derived
from the built module, never hand-maintained.

```bash
# What does the JIT module depend on, and is anything protecting it?
./pgjitguard.py status

# Verify JIT still works, by running a query that forces JIT compilation
./pgjitguard.py check --live

# Declare the dependency to apt
sudo ./pgjitguard.py protect

# Run the check automatically after every apt transaction
sudo ./pgjitguard.py install-hook

# ... and remove it again
sudo ./pgjitguard.py uninstall-hook
```

`status` compares what is actually enforced against what `llvmjit.so` needs
*now*, rather than just checking that some protection exists. After a rebuild
against a different LLVM it reports the old runtime as still protected and the
new one as exposed, which is the state that would otherwise go unnoticed until
the next upgrade. `check` warns about the same drift without failing.

`protect` generates a small `.deb` whose `Depends:` are the packages owning
`llvmjit.so`'s dependencies, and installs it. apt then models the dependency
properly: removal is refused, an upgrade that would break it warns first,
`autoremove` can never reap the runtime, and security updates still apply
normally.

> An earlier version also offered an `apt-mark hold` method. It was removed:
> holding a package pins it at a fixed version, so it blocks that package's
> security updates. Keeping known vulnerabilities on the system to protect a
> JIT module is not a good trade, and the generated package achieves the same
> protection without it.

`pginstall.py` offers to run `protect` after any JIT-enabled build.

**After rebuilding PostgreSQL against a newer LLVM**, re-run `sudo ./pgjitguard.py
protect`. It re-derives the dependency set from the rebuilt module, protects the
new runtime, and releases the old one — `apt autoremove` then reclaims it. There
is nothing to unpin by hand.

Options:
- `--pg-config PATH` - Use a specific `pg_config` (default: `/usr/local/postgresql/bin`, then PATH)
- `--live` - `check` also runs a query that forces JIT compilation
- `--hook` - `check` warns loudly but always exits 0 (used by the apt hook)
- `--dry-run` - Show what would be done without executing

`protect` refuses to run against a module whose dependencies are already
unresolved — doing so would record the wrong set. Rebuild first, then protect.

If no dpkg package owns the LLVM runtime — the case after
[`--build-llvm`](#building-a-private-llvm) — every command reports that there is
nothing to protect, because no apt operation can remove it.

> **Scope**: this guards one installation at a time — the one `--pg-config`
> names, defaulting to the `/usr/local/postgresql` symlink. The generated
> package and the manifests use fixed names, so protecting a second
> installation replaces the first rather than adding to it. With side-by-side
> PostgreSQL versions, protect the one whose JIT you rely on, or build them
> against the same LLVM.

Run `./test_pgjitguard.py` to exercise the parsing, drift-detection, and
manifest logic. The tests use recorded fixtures and need no dpkg, apt, or
running PostgreSQL.

### create_pg_service.py (Linux only)

Creates systemd services to run PostgreSQL instances. Uses template units to support multiple instances running simultaneously. This script:
- Creates a `postgres` system user if it doesn't exist
- Initializes the database cluster for each instance
- Creates a systemd template unit (`postgresql@.service`)
- Creates per-instance configuration in `/etc/postgresql/<instance>/`
- Enables and starts the instance

```bash
# Create a "main" instance (prompts for settings)
sudo ./create_pg_service.py main

# Create a "dev" instance on a different port
sudo ./create_pg_service.py dev --port 5433

# Specify all options
sudo ./create_pg_service.py test --pgdata /data/test \
                                 --logfile /var/log/postgresql/test.log \
                                 --port 5434

# List all configured instances
./create_pg_service.py --list
```

Options:
- `instance` - Instance name (required, e.g., 'main', 'dev', 'test')
- `--pgdata` - Data directory (default: `/usr/local/postgresql/data/<instance>`)
- `--logfile` - Log file location (default: `/var/log/postgresql/<instance>.log`)
- `--port` - Port number (default: `5432`)
- `--list` - List all configured instances (no sudo required)

Managing instances:
```bash
sudo systemctl start postgresql@main     # Start instance
sudo systemctl stop postgresql@main      # Stop instance
sudo systemctl restart postgresql@main   # Restart instance
sudo systemctl status postgresql@main    # Check status
journalctl -u postgresql@main            # View logs
```

### create_pg_service_macos.py (macOS only)

Creates launchd services to run PostgreSQL instances on macOS. Similar to the Linux version but uses launchd instead of systemd. This script:
- Creates a `postgres` system user if it doesn't exist (or uses your specified user)
- Optionally adds your user to the `postgres` group for file access
- Initializes the database cluster for each instance
- Configures memory settings based on your system resources
- Creates a launchd plist in `/Library/LaunchDaemons/`
- Creates per-instance configuration in `/usr/local/etc/postgresql/<instance>/`
- Loads and starts the instance

```bash
# Create a "main" instance (prompts for settings)
sudo ./create_pg_service_macos.py main

# Create a "dev" instance on a different port
sudo ./create_pg_service_macos.py dev --port 5433

# Run as your own user (convenient for development)
sudo ./create_pg_service_macos.py dev --user $(whoami) --port 5433

# Specify all options including memory profile
sudo ./create_pg_service_macos.py test --pgdata /data/test \
                                       --logfile /usr/local/var/log/postgresql/test.log \
                                       --port 5434 \
                                       --user postgres \
                                       --memory medium

# List all configured instances
./create_pg_service_macos.py --list
```

Options:
- `instance` - Instance name (required, e.g., 'main', 'dev', 'test')
- `--pgdata` - Data directory (default: `/usr/local/postgresql/data/<instance>`)
- `--logfile` - Log file location (default: `/usr/local/var/log/postgresql/<instance>.log`)
- `--port` - Port number (default: `5432`)
- `--user` - User to run PostgreSQL as (default: `postgres`)
- `--memory` - Memory profile: `lite`, `medium`, `max`, or `skip` (default: prompt)
- `--list` - List all configured instances (no sudo required)

Memory profiles (percentage of system RAM for shared_buffers):
- `lite` - 10% of RAM, good for development or shared systems
- `medium` - 25% of RAM, balanced for dedicated development machines
- `max` - 40% of RAM, for dedicated database servers
- `skip` - Use PostgreSQL defaults (can configure later)

Managing instances:
```bash
sudo launchctl kickstart system/com.postgresql.main       # Start instance
sudo launchctl kill SIGTERM system/com.postgresql.main    # Stop instance
sudo launchctl kickstart -k system/com.postgresql.main    # Restart instance
sudo launchctl print system/com.postgresql.main           # Check status
tail -f /usr/local/var/log/postgresql/main.log            # View logs
```

To permanently remove an instance:
```bash
sudo launchctl bootout system/com.postgresql.main
sudo rm /Library/LaunchDaemons/com.postgresql.main.plist
sudo rm -r /usr/local/etc/postgresql/main
```

## Configuration File

Pin specific versions instead of auto-detecting latest:

```ini
# pginstall.conf
[versions]
postgresql = 17.2
openssl = 3.6.1
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

2. OpenSSL
   └── Required by PostgreSQL for pgcrypto and SSL support

3. ICU
   └── Required by PostgreSQL for Unicode collation
   └── Automatically fixes library rpaths after install

4. PostgreSQL
   └── Links against ICU, OpenSSL, and readline
   └── Enables JIT if LLVM is detected

5. Contrib Extensions (from PostgreSQL source)
   ├── citext
   ├── cube
   ├── earthdistance
   ├── ltree
   ├── pgcrypto
   └── pg_trgm

6. External Extensions
   ├── q3c
   ├── Starlink AST library
   └── pgast (requires AST)
```

## Upgrading PostgreSQL

When installing a new PostgreSQL version, the script:

1. **Detects existing installations** in `/usr/local/postgresql-*`
2. **Shows what the current symlink points to** (e.g., `/usr/local/postgresql` → `postgresql-17.5`)
3. **Prompts whether to update the symlink** to the new version
4. **Never deletes existing installations** - you must remove old versions manually if desired

This allows you to have multiple PostgreSQL versions installed side-by-side and switch between them by updating the symlink.

To manually switch versions:
```bash
sudo ln -sfn /usr/local/postgresql-17.5 /usr/local/postgresql
```

To remove an old installation:
```bash
sudo rm -rf /usr/local/postgresql-17.5
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
sudo ./create_pg_service.py main
```

This creates a `postgres` user, initializes the database, and starts a PostgreSQL instance named "main" as a systemd service that starts automatically on boot. You can create additional instances (e.g., `dev`, `test`) on different ports.

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
CREATE EXTENSION ltree;
CREATE EXTENSION pgcrypto;
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
eval "$(./pgstatus.py --completions)"
```

### Zsh

```bash
# Add to ~/.zshrc
eval "$(./pginstall.py --completions zsh)"
eval "$(./install_pgvector.py --completions zsh)"
eval "$(./pgstatus.py --completions)"
```

## Platform-Specific Notes

### Linux

- Readline is installed via system package manager (not built from source)
- Requires `patchelf` (system version, not anaconda) for rpath fixes
- LLVM/JIT: Auto-detected in `/usr/bin/llvm-config` or `/usr/lib/llvm-*/bin/llvm-config`

### macOS

- Readline is built from source (GNU version, not Apple's libedit)
- Uses `--with-bonjour` for PostgreSQL
- LLVM/JIT: Auto-detected in `/opt/homebrew/opt/llvm/bin/llvm-config` (Apple Silicon) or `/usr/local/opt/llvm/bin/llvm-config` (Intel)
- Automatically sets `SDKROOT` for gfortran compatibility across macOS versions
- No Homebrew dependencies required (but Homebrew paths are supported)

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

### Expensive Queries Fail After a System Upgrade

```
ERROR:  could not load library "/usr/local/postgresql/lib/llvmjit.so":
        libLLVM.so.20.1: cannot open shared object file: No such file or directory
```

A system upgrade replaced the LLVM that `llvmjit.so` was built against. Because
the JIT module is loaded lazily, the server starts normally and cheap queries
still succeed — only queries above `jit_above_cost` fail, which can go unnoticed
for a long time.

Confirm and fix:

```bash
# Confirm: shows exactly which dependency no longer resolves
./pgjitguard.py status

# Fix: rebuild the JIT module against the currently installed LLVM
./pginstall.py --component postgresql --with-llvm

# Then stop it happening again
sudo ./pgjitguard.py protect
```

See [pgjitguard.py](#pgjitguardpy-linux-only) for how the protection works. As an
immediate workaround, `jit = off` in `postgresql.conf` avoids the failure without
a rebuild — and is worth benchmarking regardless, since JIT is not a win for
every workload.

### patchelf Not Found (with sudo)

If you see `sudo: patchelf: command not found`, install the system patchelf:

```bash
sudo apt install patchelf  # Debian/Ubuntu
sudo dnf install patchelf  # Fedora/RHEL
```

The anaconda version of patchelf won't work with sudo.

### Stale SDK Path After Xcode Update (macOS)

After updating Xcode, you may see errors like:
```
clang: warning: no such sysroot directory: '/Applications/Xcode.app/.../MacOSX15.4.sdk'
ld: library 'z' not found
```

This happens because PostgreSQL records the SDK path at compile time. The installer automatically detects and fixes stale SDK paths when building extensions. If you encounter this error with an existing PostgreSQL installation, you may need to rebuild PostgreSQL or manually set `SDKROOT`:

```bash
export SDKROOT=$(xcrun --show-sdk-path)
```

## Version Detection

The installer auto-detects latest versions from:

| Component | Source |
|-----------|--------|
| ICU | GitHub API: unicode-org/icu releases |
| OpenSSL | GitHub API: openssl/openssl releases |
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
| `create_pg_service_macos.py` | Launchd service setup (macOS only) |
| `pgstatus.py` | Instance manager (list, info, start/stop/restart) |
| `pgjitguard.py` | Protects the JIT module's LLVM dependency from system upgrades (Linux) |
| `add_rpaths_to_dylibs.py` | Rpath fixer for shared libraries |
| `test_install.sh` | Post-installation verification script |
| `test_pgjitguard.py` | Fixture-based tests for `pgjitguard.py` (no apt required) |
| `pginstall.conf.example` | Example configuration file |

## License

This project is provided as-is for building PostgreSQL from source. PostgreSQL, ICU, and all extensions are subject to their respective licenses.
