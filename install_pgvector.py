#!/usr/bin/env python3
"""
pgvector Extension Installer

Installs the pgvector extension for PostgreSQL from source.
This is a separate script because pgvector may be added to an existing
PostgreSQL installation independently of the main install process.

Usage:
    install_pgvector.py [options]

Options:
    --pg-config PATH   Path to pg_config (default: /usr/local/postgresql/bin/pg_config)
    --version VER      Specific version to install (default: latest)
    --dry-run          Show what would be done
    --verbose          Show all build output
"""

import argparse
import json
import os
import platform
import re
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from typing import Optional

# Constants
SRC_DIR = Path("/usr/local/src")
GITHUB_API = "https://api.github.com"
DEFAULT_PG_CONFIG = Path("/usr/local/postgresql/bin/pg_config")


def get_platform() -> str:
    """Return 'linux' or 'darwin'."""
    return platform.system().lower()


def find_macos_sdk() -> Optional[str]:
    """Find the current macOS SDK path. Returns None on Linux or if not found."""
    if get_platform() != "darwin":
        return None

    try:
        result = subprocess.run(
            ["xcrun", "--show-sdk-path"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def get_sanitized_env() -> dict:
    """Return environment dict with problematic paths removed and macOS SDK set."""
    env = os.environ.copy()
    path_parts = env.get("PATH", "").split(":")
    # Remove paths that contain non-Apple gcc which is incompatible with
    # clang-specific compiler flags used by PostgreSQL on macOS
    sanitized_path = ":".join(
        p for p in path_parts
        if "/usr/local/anaconda" not in p
        and "/anaconda" not in p
        and "/opt/homebrew/Cellar/gcc" not in p
        and "/usr/local/Cellar/gcc" not in p
        and "/usr/local/gfortran" not in p  # gfortran bundle includes incompatible gcc
    )
    env["PATH"] = sanitized_path

    # On macOS, set SDKROOT to the current SDK path
    # This fixes issues when Xcode has been updated since PostgreSQL was built
    if get_platform() == "darwin":
        sdk_path = find_macos_sdk()
        if sdk_path:
            env["SDKROOT"] = sdk_path
        # Ensure we use Apple's clang, not any other gcc
        # pgvector's Makefile uses $(CC) which defaults to the CC env var
        env["CC"] = "/usr/bin/clang"

    return env


def get_cpu_count() -> int:
    """Return number of CPUs for parallel builds."""
    return os.cpu_count() or 1


def run_build_cmd(
    cmd: list[str],
    cwd: Path,
    env: dict = None,
    dry_run: bool = False,
    verbose: bool = False,
    description: str = "",
) -> None:
    """Execute build command with error handling."""
    if env is None:
        env = get_sanitized_env()

    cmd_str = " ".join(cmd)
    if description:
        print(f"  {description}")
    if verbose or dry_run:
        print(f"  Running: {cmd_str}")
        print(f"  In: {cwd}")

    if dry_run:
        return

    try:
        if verbose:
            subprocess.run(cmd, cwd=cwd, env=env, check=True)
        else:
            subprocess.run(
                cmd, cwd=cwd, env=env, check=True, capture_output=True, text=True
            )
    except subprocess.CalledProcessError as e:
        print(f"Error running command: {cmd_str}", file=sys.stderr)
        print(f"Exit code: {e.returncode}", file=sys.stderr)
        if e.stdout:
            print(f"stdout: {e.stdout}", file=sys.stderr)
        if e.stderr:
            print(f"stderr: {e.stderr}", file=sys.stderr)
        sys.exit(1)


def download_file(url: str, dest: Path, dry_run: bool = False) -> None:
    """Download a file from URL to destination."""
    print(f"  Downloading: {url}")
    if dry_run:
        print(f"  Would save to: {dest}")
        return

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "pginstall/1.0"})
        with urllib.request.urlopen(req, timeout=60) as response:
            with open(dest, "wb") as f:
                f.write(response.read())
    except Exception as e:
        print(f"Error downloading {url}: {e}", file=sys.stderr)
        sys.exit(1)


def download_and_extract(url: str, dest_dir: Path, dry_run: bool = False) -> Path:
    """Download tarball and extract to dest_dir. Returns extracted directory path."""
    if dry_run:
        print(f"  Would download and extract: {url}")
        print(f"  To: {dest_dir}")
        # Return a plausible path for dry-run
        filename = url.split("/")[-1]
        if filename.endswith(".tar.gz"):
            dirname = filename[:-7]
        elif filename.endswith(".tgz"):
            dirname = filename[:-4]
        else:
            dirname = filename.rsplit(".", 1)[0]
        return dest_dir / dirname

    # Directory should already exist from check_src_dir, but create if needed
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        print(f"Error: Permission denied creating directory: {dest_dir}", file=sys.stderr)
        print(f"  Ensure {SRC_DIR} exists and is writable by your user", file=sys.stderr)
        sys.exit(1)

    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    try:
        download_file(url, tmp_path)

        print(f"  Extracting to: {dest_dir}")
        with tarfile.open(tmp_path, "r:*") as tar:
            # Get the top-level directory name
            members = tar.getmembers()
            if members:
                top_dir = members[0].name.split("/")[0]
            else:
                print("Error: Empty tarball", file=sys.stderr)
                sys.exit(1)

            try:
                tar.extractall(path=dest_dir, filter="tar")
            except PermissionError:
                print(f"Error: Permission denied extracting to: {dest_dir}", file=sys.stderr)
                print(f"  Ensure {SRC_DIR} is writable by your user", file=sys.stderr)
                sys.exit(1)

        return dest_dir / top_dir
    finally:
        tmp_path.unlink(missing_ok=True)


def github_api_get(endpoint: str) -> dict:
    """Make a GET request to the GitHub API."""
    url = f"{GITHUB_API}{endpoint}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "pginstall/1.0",
            "Accept": "application/vnd.github.v3+json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode())
    except Exception as e:
        print(f"Error fetching {url}: {e}", file=sys.stderr)
        sys.exit(1)


def get_latest_pgvector_version() -> str:
    """Query GitHub API for latest pgvector version (uses tags, not releases)."""
    print("  Detecting latest pgvector version...")
    tags = github_api_get("/repos/pgvector/pgvector/tags")
    for tag_info in tags:
        tag = tag_info.get("name", "")
        # pgvector tags are like "v0.8.0"
        match = re.match(r"v?(\d+\.\d+\.\d+)", tag)
        if match:
            version = match.group(1)
            print(f"  Latest pgvector version: {version}")
            return version
    print("Error: Could not determine latest pgvector version", file=sys.stderr)
    sys.exit(1)


def check_src_dir(dry_run: bool = False) -> bool:
    """Check that the source directory exists and is writable."""
    if SRC_DIR.exists():
        if os.access(SRC_DIR, os.W_OK):
            print(f"  ✅ Source directory exists and is writable")
            return True
        else:
            print(f"  🛑 No write permission: {SRC_DIR}")
            print(f"\n  To fix, run:")
            print(f"    sudo chown $(whoami) {SRC_DIR}")
            if not dry_run:
                sys.exit(1)
            return False
    else:
        print(f"  🛑 Source directory does not exist: {SRC_DIR}")
        print(f"\n  To create it, run:")
        print(f"    sudo mkdir -p {SRC_DIR} && sudo chown $(whoami) {SRC_DIR}")
        if not dry_run:
            sys.exit(1)
        return False


def verify_pg_config(pg_config: Path) -> bool:
    """Verify that pg_config exists and is executable."""
    if not pg_config.exists():
        print(f"Error: pg_config not found at {pg_config}", file=sys.stderr)
        return False
    if not os.access(pg_config, os.X_OK):
        print(f"Error: pg_config is not executable: {pg_config}", file=sys.stderr)
        return False

    # Try running it
    try:
        result = subprocess.run(
            [str(pg_config), "--version"],
            capture_output=True,
            text=True,
            check=True,
        )
        print(f"  PostgreSQL: {result.stdout.strip()}")
        return True
    except subprocess.CalledProcessError as e:
        print(f"Error running pg_config: {e}", file=sys.stderr)
        return False


def build_pgvector(
    version: str,
    pg_config: Path,
    dry_run: bool = False,
    verbose: bool = False,
) -> None:
    """Build and install pgvector extension."""
    print(f"\n{'=' * 60}")
    print(f"Building pgvector {version}")
    print(f"{'=' * 60}")

    # Download and extract
    url = f"https://github.com/pgvector/pgvector/archive/refs/tags/v{version}.tar.gz"
    src_path = download_and_extract(url, SRC_DIR, dry_run)

    if dry_run:
        src_path = SRC_DIR / f"pgvector-{version}"
        print(f"  Would build pgvector from: {src_path}")
        print(f"  Using pg_config: {pg_config}")
        return

    env = get_sanitized_env()

    # Build
    run_build_cmd(
        ["make", f"PG_CONFIG={pg_config}", f"-j{get_cpu_count()}"],
        cwd=src_path,
        env=env,
        dry_run=dry_run,
        verbose=verbose,
        description="Building pgvector...",
    )

    # Install
    run_build_cmd(
        ["sudo", "make", "install", f"PG_CONFIG={pg_config}"],
        cwd=src_path,
        env=env,
        dry_run=dry_run,
        verbose=verbose,
        description="Installing pgvector...",
    )

    print(f"  pgvector {version} installed successfully")


def generate_bash_completion() -> str:
    """Generate bash completion script."""
    script_name = Path(sys.argv[0]).name
    return f'''# Bash completion for {script_name}
# Add to ~/.bashrc: eval "$({script_name} --completions bash)"

_{script_name.replace("-", "_").replace(".", "_")}_completions() {{
    local cur prev opts
    COMPREPLY=()
    cur="${{COMP_WORDS[COMP_CWORD]}}"
    prev="${{COMP_WORDS[COMP_CWORD-1]}}"
    opts="--pg-config --version --dry-run --verbose --completions --help"

    case "${{prev}}" in
        --pg-config)
            COMPREPLY=( $(compgen -f -- "${{cur}}") )
            return 0
            ;;
        --version)
            return 0
            ;;
        --completions)
            COMPREPLY=( $(compgen -W "bash zsh" -- "${{cur}}") )
            return 0
            ;;
    esac

    if [[ "${{cur}}" == -* ]]; then
        COMPREPLY=( $(compgen -W "${{opts}}" -- "${{cur}}") )
        return 0
    fi
}}

complete -F _{script_name.replace("-", "_").replace(".", "_")}_completions {script_name}
'''


def generate_zsh_completion() -> str:
    """Generate zsh completion script."""
    script_name = Path(sys.argv[0]).name
    return f'''#compdef {script_name}
# Zsh completion for {script_name}
# Add to ~/.zshrc: eval "$({script_name} --completions zsh)"

_install_pgvector() {{
    local -a opts
    opts=(
        '--pg-config[Path to pg_config]:file:_files'
        '--version[Specific version to install]:version:'
        '--dry-run[Show what would be done without executing]'
        '--verbose[Show all build output]'
        '--completions[Output shell completion script]:shell:(bash zsh)'
        '--help[Show help message]'
    )
    _arguments -s $opts
}}

_install_pgvector "$@"
'''


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="pgvector Extension Installer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                          # Install latest pgvector
  %(prog)s --version 0.8.0          # Install specific version
  %(prog)s --dry-run                # Show what would be done
  %(prog)s --pg-config /path/to/pg_config  # Use custom PostgreSQL

Shell completions:
  %(prog)s --completions bash       # Output bash completion script
  %(prog)s --completions zsh        # Output zsh completion script

After installation, create the extension in your database:
  CREATE EXTENSION vector;
""",
    )

    parser.add_argument(
        "--pg-config",
        type=Path,
        default=DEFAULT_PG_CONFIG,
        help=f"Path to pg_config (default: {DEFAULT_PG_CONFIG})",
    )
    parser.add_argument(
        "--version",
        type=str,
        help="Specific version to install (default: latest)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without executing",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show all build output",
    )
    parser.add_argument(
        "--completions",
        choices=["bash", "zsh"],
        metavar="SHELL",
        help="Output shell completion script (bash or zsh)",
    )

    return parser.parse_args()


def main() -> None:
    """Main entry point."""
    args = parse_args()

    # Handle completions flag early
    if args.completions:
        if args.completions == "bash":
            print(generate_bash_completion())
        elif args.completions == "zsh":
            print(generate_zsh_completion())
        sys.exit(0)

    print("pgvector Extension Installer")

    if args.dry_run:
        print("\n*** DRY RUN MODE - No changes will be made ***")

    # Check source directory exists and is writable
    print(f"Checking source directory ({SRC_DIR})...")
    check_src_dir(dry_run=args.dry_run)

    # Verify pg_config
    print(f"\nUsing pg_config: {args.pg_config}")
    if not args.dry_run:
        if not verify_pg_config(args.pg_config):
            sys.exit(1)

    # Get version
    if args.version:
        version = args.version
        print(f"  Requested version: {version}")
    else:
        version = get_latest_pgvector_version()

    # Build and install
    build_pgvector(version, args.pg_config, args.dry_run, args.verbose)

    print(f"\n{'=' * 60}")
    print("Installation complete!")
    print(f"{'=' * 60}")
    print("\nTo use pgvector, run in your PostgreSQL database:")
    print("  CREATE EXTENSION vector;")


if __name__ == "__main__":
    main()
