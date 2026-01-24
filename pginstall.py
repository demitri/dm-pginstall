#!/usr/bin/env python3
"""
PostgreSQL Source Installer

Automates building PostgreSQL and dependencies from source on Linux and macOS.
All packages are installed to /usr/local/<package>-<version> with symlinks.

Usage:
    pginstall.py [options]

Options:
    --config FILE      Use config file for version pinning
    --dry-run          Show what would be done without executing
    --component NAME   Build only specific component
    --skip-extensions  Skip q3c and pgast
    --verbose          Show all build output
"""

import argparse
import configparser
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from typing import Optional

# Constants
SRC_DIR = Path("/usr/local/src")
INSTALL_BASE = Path("/usr/local")
SCRIPT_DIR = Path(__file__).parent.resolve()

# GitHub API base
GITHUB_API = "https://api.github.com"

# Contrib extensions to build
CONTRIB_EXTENSIONS = ["citext", "cube", "earthdistance", "pg_trgm"]


def get_platform() -> str:
    """Return 'linux' or 'darwin'."""
    system = platform.system().lower()
    if system == "darwin":
        return "darwin"
    elif system == "linux":
        return "linux"
    else:
        print(f"Error: Unsupported platform: {system}", file=sys.stderr)
        sys.exit(1)


def get_cpu_count() -> int:
    """Return number of CPUs for parallel builds."""
    return os.cpu_count() or 1


def get_sanitized_env() -> dict:
    """Return environment dict with /usr/local/anaconda/bin removed from PATH."""
    env = os.environ.copy()
    path_parts = env.get("PATH", "").split(":")
    sanitized_path = ":".join(
        p for p in path_parts if "/usr/local/anaconda" not in p and "/anaconda" not in p
    )
    env["PATH"] = sanitized_path
    return env


def find_llvm_config() -> Optional[str]:
    """Locate llvm-config binary, return path or None."""
    # Common locations to check
    candidates = [
        "/usr/bin/llvm-config",
        "/usr/local/opt/llvm/bin/llvm-config",  # macOS Homebrew
    ]

    # Linux: check /usr/lib/llvm-*/bin/llvm-config
    if get_platform() == "linux":
        llvm_dirs = sorted(Path("/usr/lib").glob("llvm-*/bin/llvm-config"), reverse=True)
        candidates.extend(str(p) for p in llvm_dirs)

    for candidate in candidates:
        if Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate

    # Try finding in PATH
    result = shutil.which("llvm-config")
    if result:
        return result

    return None


def detect_package_manager() -> Optional[str]:
    """Detect the system package manager on Linux."""
    if shutil.which("apt-get"):
        return "apt"
    elif shutil.which("dnf"):
        return "dnf"
    elif shutil.which("yum"):
        return "yum"
    elif shutil.which("pacman"):
        return "pacman"
    return None


def get_llvm_install_command() -> Optional[str]:
    """Return the command to install LLVM dev packages for PostgreSQL JIT."""
    pkg_mgr = detect_package_manager()
    if pkg_mgr == "apt":
        return "sudo apt install llvm-dev clang"
    elif pkg_mgr == "dnf":
        return "sudo dnf install llvm-devel clang"
    elif pkg_mgr == "yum":
        return "sudo yum install llvm-devel clang"
    elif pkg_mgr == "pacman":
        return "sudo pacman -S llvm clang"
    return None


def check_existing(install_path: Path) -> bool:
    """Return True if installation already exists."""
    return install_path.is_dir()


def create_symlink(target: Path, link_name: Path, dry_run: bool = False) -> None:
    """Create or update versioned symlink using sudo."""
    if dry_run:
        print(f"  Would create symlink: {link_name} -> {target}")
        return

    # Use sudo ln -sf to create/update symlink in /usr/local
    try:
        subprocess.run(
            ["sudo", "ln", "-sf", str(target), str(link_name)],
            check=True,
            capture_output=True,
            text=True,
        )
        print(f"  Created symlink: {link_name} -> {target}")
    except subprocess.CalledProcessError as e:
        print(f"Error creating symlink: {e.stderr}", file=sys.stderr)
        sys.exit(1)


def run_build_cmd(
    cmd: list[str],
    cwd: Path,
    env: Optional[dict] = None,
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

    # Directory should already exist from ensure_src_dir, but create if needed
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

            # Use filter="tar" for full compatibility
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


# =============================================================================
# Version Detection Functions
# =============================================================================


def get_latest_icu_version() -> str:
    """Query GitHub API for latest ICU release version."""
    print("  Detecting latest ICU version...")
    releases = github_api_get("/repos/unicode-org/icu/releases")
    for release in releases:
        tag = release.get("tag_name", "")
        # ICU tags are like "release-76-1"
        match = re.match(r"release-(\d+)-(\d+)", tag)
        if match:
            version = f"{match.group(1)}.{match.group(2)}"
            print(f"  Latest ICU version: {version}")
            return version
    print("Error: Could not determine latest ICU version", file=sys.stderr)
    sys.exit(1)


def get_latest_postgresql_version() -> str:
    """Parse PostgreSQL FTP listing for latest version."""
    print("  Detecting latest PostgreSQL version...")
    url = "https://ftp.postgresql.org/pub/source/"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "pginstall/1.0"})
        with urllib.request.urlopen(req, timeout=30) as response:
            html = response.read().decode()
        # Find version directories like v17.2
        versions = re.findall(r'href="v(\d+\.\d+)/"', html)
        if versions:
            # Sort by version number (major.minor)
            versions.sort(key=lambda v: tuple(map(int, v.split("."))), reverse=True)
            version = versions[0]
            print(f"  Latest PostgreSQL version: {version}")
            return version
    except Exception as e:
        print(f"Error fetching PostgreSQL versions: {e}", file=sys.stderr)
    print("Error: Could not determine latest PostgreSQL version", file=sys.stderr)
    sys.exit(1)


def get_latest_readline_version() -> str:
    """Parse GNU FTP for latest readline version (macOS only)."""
    print("  Detecting latest readline version...")
    url = "https://ftp.gnu.org/gnu/readline/"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "pginstall/1.0"})
        with urllib.request.urlopen(req, timeout=30) as response:
            html = response.read().decode()
        # Find versions like readline-8.2.tar.gz
        versions = re.findall(r'href="readline-(\d+\.\d+)\.tar\.gz"', html)
        if versions:
            versions.sort(key=lambda v: tuple(map(int, v.split("."))), reverse=True)
            version = versions[0]
            print(f"  Latest readline version: {version}")
            return version
    except Exception as e:
        print(f"Error fetching readline versions: {e}", file=sys.stderr)
    print("Error: Could not determine latest readline version", file=sys.stderr)
    sys.exit(1)


def get_latest_q3c_version() -> str:
    """Query GitHub API for latest q3c release."""
    print("  Detecting latest q3c version...")
    releases = github_api_get("/repos/segasai/q3c/releases")
    for release in releases:
        tag = release.get("tag_name", "")
        # q3c tags are like "v2.0.1" or "2.0.1"
        match = re.match(r"v?(\d+\.\d+\.\d+)", tag)
        if match:
            version = match.group(1)
            print(f"  Latest q3c version: {version}")
            return version
    print("Error: Could not determine latest q3c version", file=sys.stderr)
    sys.exit(1)


def get_latest_pgast_version() -> str:
    """Query GitHub API for latest pgast release (or use main branch)."""
    print("  Detecting latest pgast version...")
    try:
        releases = github_api_get("/repos/demitri/pgast/releases")
        for release in releases:
            tag = release.get("tag_name", "")
            match = re.match(r"v?(\d+\.\d+\.?\d*)", tag)
            if match:
                version = match.group(1)
                print(f"  Latest pgast version: {version}")
                return version
    except Exception:
        pass
    # Fall back to main branch
    print("  Using pgast main branch")
    return "main"


def get_latest_ast_version() -> str:
    """Query GitHub API for latest Starlink AST release."""
    print("  Detecting latest Starlink AST version...")
    try:
        releases = github_api_get("/repos/Starlink/ast/releases")
        for release in releases:
            # Skip prereleases
            if release.get("prerelease", False):
                continue
            tag = release.get("tag_name", "")
            match = re.match(r"v?(\d+\.\d+\.?\d*)", tag)
            if match:
                version = match.group(1)
                print(f"  Latest AST version: {version}")
                return version
    except Exception as e:
        print(f"  Warning: Could not detect AST version: {e}", file=sys.stderr)
    # Fall back to known stable version
    print("  Using AST version 9.3.0")
    return "9.3.0"


# =============================================================================
# Config File Support
# =============================================================================


def load_versions(config_path: Optional[Path], exclude_ast: bool = False) -> dict:
    """Load versions from config file, falling back to auto-detect."""
    versions = {}

    # Auto-detect all versions first
    plat = get_platform()

    print("\nDetecting versions...")
    if plat == "darwin":
        versions["readline"] = get_latest_readline_version()
    versions["icu"] = get_latest_icu_version()
    versions["postgresql"] = get_latest_postgresql_version()
    versions["q3c"] = get_latest_q3c_version()
    if not exclude_ast:
        versions["ast"] = get_latest_ast_version()
        versions["pgast"] = get_latest_pgast_version()

    # Override with config file if provided
    if config_path and config_path.exists():
        print(f"\nLoading config from: {config_path}")
        config = configparser.ConfigParser()
        config.read(config_path)
        if "versions" in config:
            for key, value in config["versions"].items():
                if key in versions or key == "readline":
                    print(f"  Overriding {key}: {value}")
                    versions[key] = value

    return versions


# =============================================================================
# Build Functions
# =============================================================================


def build_readline(version: str, dry_run: bool = False, verbose: bool = False) -> None:
    """Build readline from source (macOS only)."""
    install_path = INSTALL_BASE / f"readline-{version}"
    symlink_path = INSTALL_BASE / "readline"

    print(f"\n{'=' * 60}")
    print(f"Building readline {version}")
    print(f"{'=' * 60}")

    if check_existing(install_path):
        print(f"  Already installed: {install_path}")
        create_symlink(install_path, symlink_path, dry_run)
        return

    # Download and extract
    url = f"https://ftp.gnu.org/gnu/readline/readline-{version}.tar.gz"
    src_path = download_and_extract(url, SRC_DIR, dry_run)

    if not dry_run:
        # Configure
        run_build_cmd(
            ["./configure", f"--prefix={install_path}"],
            cwd=src_path,
            dry_run=dry_run,
            verbose=verbose,
            description="Configuring readline...",
        )

        # Build
        run_build_cmd(
            ["make", f"-j{get_cpu_count()}"],
            cwd=src_path,
            dry_run=dry_run,
            verbose=verbose,
            description="Building readline...",
        )

        # Install
        run_build_cmd(
            ["sudo", "make", "install"],
            cwd=src_path,
            dry_run=dry_run,
            verbose=verbose,
            description="Installing readline...",
        )
    else:
        print("  Would configure, build, and install readline")

    # Create symlink
    create_symlink(install_path, symlink_path, dry_run)
    print(f"  readline {version} installed successfully")


def build_icu(version: str, dry_run: bool = False, verbose: bool = False) -> None:
    """Build ICU from source."""
    install_path = INSTALL_BASE / f"icu-{version}"
    symlink_path = INSTALL_BASE / "icu"

    print(f"\n{'=' * 60}")
    print(f"Building ICU {version}")
    print(f"{'=' * 60}")

    if check_existing(install_path):
        print(f"  Already installed: {install_path}")
        create_symlink(install_path, symlink_path, dry_run)
        return

    # Download and extract
    # ICU download URL format: https://github.com/unicode-org/icu/releases/download/release-76-1/icu4c-76_1-src.tgz
    version_parts = version.split(".")
    tag_version = f"{version_parts[0]}-{version_parts[1]}"
    file_version = f"{version_parts[0]}_{version_parts[1]}"
    url = f"https://github.com/unicode-org/icu/releases/download/release-{tag_version}/icu4c-{file_version}-src.tgz"

    src_path = download_and_extract(url, SRC_DIR, dry_run)
    # ICU extracts to 'icu' directory, source is in 'icu/source'
    if not dry_run:
        src_path = src_path / "source"
    else:
        src_path = SRC_DIR / "icu" / "source"

    if not dry_run:
        # Configure
        run_build_cmd(
            ["./configure", f"--prefix={install_path}"],
            cwd=src_path,
            dry_run=dry_run,
            verbose=verbose,
            description="Configuring ICU...",
        )

        # Build
        run_build_cmd(
            ["make", f"-j{get_cpu_count()}"],
            cwd=src_path,
            dry_run=dry_run,
            verbose=verbose,
            description="Building ICU...",
        )

        # Install
        run_build_cmd(
            ["sudo", "make", "install"],
            cwd=src_path,
            dry_run=dry_run,
            verbose=verbose,
            description="Installing ICU...",
        )

        # Run rpath fixer
        print("  Running rpath fixer on ICU libraries...")
        rpath_script = SCRIPT_DIR / "add_rpaths_to_dylibs.py"
        lib_dir = install_path / "lib"
        symlink_lib_dir = symlink_path / "lib"  # Use symlink path for rpath
        if rpath_script.exists():
            try:
                # Generate patchelf commands (use versioned path, skip cascade since symlink doesn't exist yet)
                result = subprocess.run(
                    [
                        sys.executable,
                        str(rpath_script),
                        str(lib_dir),
                        "--rpath",
                        str(symlink_lib_dir),
                        "--no-cascade",
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                # Prepend sudo to each patchelf/install_name_tool command and run
                if result.stdout.strip():
                    # Find system patchelf path for sudo compatibility
                    patchelf_path = find_system_patchelf() or "patchelf"
                    # Add sudo to each command line (skip comments)
                    lines = result.stdout.strip().split("\n")
                    sudo_commands = []
                    for line in lines:
                        stripped = line.strip()
                        if stripped and not stripped.startswith("#"):
                            # Replace 'patchelf' with full path for sudo
                            if stripped.startswith("patchelf "):
                                stripped = patchelf_path + stripped[8:]
                            sudo_commands.append(f"sudo {stripped}")
                        else:
                            sudo_commands.append(line)
                    sudo_script = "\n".join(sudo_commands)
                    if verbose:
                        print("  Executing rpath commands:")
                        for cmd in sudo_commands:
                            if not cmd.startswith("#"):
                                print(f"    {cmd}")
                    subprocess.run(
                        ["bash"],
                        input=sudo_script,
                        text=True,
                        check=True,
                    )
                    # Verify rpaths were actually set
                    patchelf_bin = find_system_patchelf()
                    if patchelf_bin and get_platform() != "darwin":
                        verify_lib = lib_dir / "libicuuc.so.77.1"
                        if verify_lib.exists():
                            verify_result = subprocess.run(
                                [patchelf_bin, "--print-rpath", str(verify_lib)],
                                capture_output=True,
                                text=True,
                            )
                            actual_rpath = verify_result.stdout.strip()
                            expected_rpath = str(symlink_lib_dir)
                            if expected_rpath in actual_rpath:
                                print("  rpath fixes applied and verified successfully")
                            else:
                                print(
                                    f"  Warning: rpath verification failed. "
                                    f"Expected '{expected_rpath}', got '{actual_rpath}'",
                                    file=sys.stderr,
                                )
                        else:
                            print("  rpath fixes applied (could not verify)")
                    else:
                        print("  rpath fixes applied successfully")
                else:
                    print("  No rpath fixes needed")
            except subprocess.CalledProcessError as e:
                print(f"  Error: rpath fixer failed: {e}", file=sys.stderr)
                sys.exit(1)
    else:
        print("  Would configure, build, and install ICU")
        print("  Would run rpath fixer on ICU libraries")

    # Create symlink
    create_symlink(install_path, symlink_path, dry_run)
    print(f"  ICU {version} installed successfully")


def build_postgresql(
    version: str, dry_run: bool = False, verbose: bool = False
) -> None:
    """Build PostgreSQL from source."""
    install_path = INSTALL_BASE / f"postgresql-{version}"
    symlink_path = INSTALL_BASE / "postgresql"

    print(f"\n{'=' * 60}")
    print(f"Building PostgreSQL {version}")
    print(f"{'=' * 60}")

    if check_existing(install_path):
        print(f"  Already installed: {install_path}")
        create_symlink(install_path, symlink_path, dry_run)
        return

    # Download and extract
    url = f"https://ftp.postgresql.org/pub/source/v{version}/postgresql-{version}.tar.gz"
    src_path = download_and_extract(url, SRC_DIR, dry_run)

    if dry_run:
        src_path = SRC_DIR / f"postgresql-{version}"

    # Build configure command
    plat = get_platform()
    icu_path = INSTALL_BASE / "icu"

    configure_cmd = [
        "./configure",
        f"--prefix={install_path}",
        "--with-icu",
    ]

    # ICU flags
    env = get_sanitized_env()
    env["ICU_CFLAGS"] = f"-I{icu_path}/include"
    env["ICU_LIBS"] = f"-L{icu_path}/lib -licui18n -licuuc -licudata"

    # Set rpath so binaries can find ICU libraries at runtime
    if plat == "darwin":
        env["LDFLAGS"] = f"-L{icu_path}/lib -Wl,-rpath,{icu_path}/lib"
    else:
        env["LDFLAGS"] = f"-Wl,-rpath,{icu_path}/lib"

    # Platform-specific options
    if plat == "darwin":
        readline_path = INSTALL_BASE / "readline"
        configure_cmd.extend([
            "--with-bonjour",
            f"--with-libraries={readline_path}/lib",
            f"--with-includes={readline_path}/include",
        ])
    else:
        # Linux: readline should be from system packages
        pass

    # LLVM/JIT support
    llvm_config = find_llvm_config()
    if llvm_config:
        print(f"  ✅ Found LLVM: {llvm_config}")
        configure_cmd.extend([
            "--with-llvm",
            f"LLVM_CONFIG={llvm_config}",
        ])
    else:
        print("  LLVM not found, building without JIT support")

    if not dry_run:
        # Configure
        run_build_cmd(
            configure_cmd,
            cwd=src_path,
            env=env,
            dry_run=dry_run,
            verbose=verbose,
            description="Configuring PostgreSQL...",
        )

        # Build
        run_build_cmd(
            ["make", f"-j{get_cpu_count()}"],
            cwd=src_path,
            env=env,
            dry_run=dry_run,
            verbose=verbose,
            description="Building PostgreSQL...",
        )

        # Install
        run_build_cmd(
            ["sudo", "make", "install"],
            cwd=src_path,
            env=env,
            dry_run=dry_run,
            verbose=verbose,
            description="Installing PostgreSQL...",
        )
    else:
        print(f"  Would run: {' '.join(configure_cmd)}")
        print("  Would build and install PostgreSQL")

    # Create symlink
    create_symlink(install_path, symlink_path, dry_run)
    print(f"  PostgreSQL {version} installed successfully")


def build_contrib_extensions(
    pg_version: str, dry_run: bool = False, verbose: bool = False
) -> None:
    """Build contrib extensions from PostgreSQL source."""
    print(f"\n{'=' * 60}")
    print("Building contrib extensions")
    print(f"{'=' * 60}")

    pg_config = INSTALL_BASE / "postgresql" / "bin" / "pg_config"
    src_path = SRC_DIR / f"postgresql-{pg_version}" / "contrib"

    if dry_run:
        print(f"  Would build contrib extensions from: {src_path}")
        for ext in CONTRIB_EXTENSIONS:
            print(f"    - {ext}")
        return

    if not src_path.exists():
        print(f"  Error: PostgreSQL source not found at {src_path}", file=sys.stderr)
        print("  Contrib extensions must be built from PostgreSQL source", file=sys.stderr)
        return

    env = get_sanitized_env()

    for ext in CONTRIB_EXTENSIONS:
        ext_path = src_path / ext
        if not ext_path.exists():
            print(f"  Warning: Extension {ext} not found at {ext_path}", file=sys.stderr)
            continue

        print(f"  Building {ext}...")

        # Build
        run_build_cmd(
            ["make", f"PG_CONFIG={pg_config}"],
            cwd=ext_path,
            env=env,
            dry_run=dry_run,
            verbose=verbose,
        )

        # Install
        run_build_cmd(
            ["sudo", "make", "install", f"PG_CONFIG={pg_config}"],
            cwd=ext_path,
            env=env,
            dry_run=dry_run,
            verbose=verbose,
        )

        print(f"    {ext} installed successfully")


def build_q3c(version: str, dry_run: bool = False, verbose: bool = False) -> None:
    """Build q3c extension from GitHub."""
    print(f"\n{'=' * 60}")
    print(f"Building q3c {version}")
    print(f"{'=' * 60}")

    pg_config = INSTALL_BASE / "postgresql" / "bin" / "pg_config"

    # Download and extract
    url = f"https://github.com/segasai/q3c/archive/refs/tags/v{version}.tar.gz"
    src_path = download_and_extract(url, SRC_DIR, dry_run)

    if dry_run:
        src_path = SRC_DIR / f"q3c-{version}"
        print(f"  Would build q3c from: {src_path}")
        return

    env = get_sanitized_env()

    # Build
    run_build_cmd(
        ["make", f"PG_CONFIG={pg_config}"],
        cwd=src_path,
        env=env,
        dry_run=dry_run,
        verbose=verbose,
        description="Building q3c...",
    )

    # Install
    run_build_cmd(
        ["sudo", "make", "install", f"PG_CONFIG={pg_config}"],
        cwd=src_path,
        env=env,
        dry_run=dry_run,
        verbose=verbose,
        description="Installing q3c...",
    )

    print(f"  q3c {version} installed successfully")


def build_ast(version: str, dry_run: bool = False, verbose: bool = False) -> None:
    """Build Starlink AST library from source."""
    install_path = INSTALL_BASE / f"ast-{version}"
    symlink_path = INSTALL_BASE / "ast"

    print(f"\n{'=' * 60}")
    print(f"Building Starlink AST {version}")
    print(f"{'=' * 60}")

    if check_existing(install_path):
        print(f"  Already installed: {install_path}")
        create_symlink(install_path, symlink_path, dry_run)
        return

    # Download release tarball (use the pre-built release asset, not the source tarball)
    url = f"https://github.com/Starlink/ast/releases/download/v{version}/ast-{version}.tar.gz"
    src_path = download_and_extract(url, SRC_DIR, dry_run)

    if dry_run:
        src_path = SRC_DIR / f"ast-{version}"
        print(f"  Would build AST from: {src_path}")
        print(f"  Would install to: {install_path}")
        create_symlink(install_path, symlink_path, dry_run)
        return

    env = get_sanitized_env()

    # Configure
    configure_cmd = [
        "./configure",
        f"--prefix={install_path}",
    ]

    run_build_cmd(
        configure_cmd,
        cwd=src_path,
        env=env,
        dry_run=dry_run,
        verbose=verbose,
        description="Configuring AST...",
    )

    # Build
    run_build_cmd(
        ["make", f"-j{get_cpu_count()}"],
        cwd=src_path,
        env=env,
        dry_run=dry_run,
        verbose=verbose,
        description="Building AST...",
    )

    # Install
    run_build_cmd(
        ["sudo", "make", "install"],
        cwd=src_path,
        env=env,
        dry_run=dry_run,
        verbose=verbose,
        description="Installing AST...",
    )

    # Create symlink
    create_symlink(install_path, symlink_path, dry_run)
    print(f"  AST {version} installed successfully")


def build_pgast(version: str, dry_run: bool = False, verbose: bool = False) -> None:
    """Build pgast extension from GitHub. Requires Starlink AST library."""
    print(f"\n{'=' * 60}")
    print(f"Building pgast {version}")
    print(f"{'=' * 60}")

    pg_config = INSTALL_BASE / "postgresql" / "bin" / "pg_config"
    ast_path = INSTALL_BASE / "ast"
    repo_path = SRC_DIR / "pgast"

    if version == "main":
        # Clone or update from main branch
        url = "https://github.com/demitri/pgast.git"
        if dry_run:
            print(f"  Would clone/update pgast from: {url}")
        else:
            if repo_path.exists():
                print("  Updating existing pgast checkout...")
                run_build_cmd(
                    ["git", "pull"],
                    cwd=repo_path,
                    dry_run=dry_run,
                    verbose=verbose,
                )
            else:
                print("  Cloning pgast...")
                run_build_cmd(
                    ["git", "clone", url, str(repo_path)],
                    cwd=SRC_DIR,
                    dry_run=dry_run,
                    verbose=verbose,
                )
    else:
        # Download release tarball
        url = f"https://github.com/demitri/pgast/archive/refs/tags/v{version}.tar.gz"
        repo_path = download_and_extract(url, SRC_DIR, dry_run)
        if dry_run:
            repo_path = SRC_DIR / f"pgast-{version}"

    # The Makefile is in the src/ subdirectory
    src_path = repo_path / "src"

    if dry_run:
        print(f"  Would build pgast from: {src_path}")
        print(f"  Using AST library at: {ast_path}")
        return

    env = get_sanitized_env()

    # Build (pass AST path to make)
    run_build_cmd(
        ["make", f"PG_CONFIG={pg_config}", f"AST={ast_path}"],
        cwd=src_path,
        env=env,
        dry_run=dry_run,
        verbose=verbose,
        description="Building pgast...",
    )

    # Install (AST path also required for install target)
    run_build_cmd(
        ["sudo", "make", "install", f"PG_CONFIG={pg_config}", f"AST={ast_path}"],
        cwd=src_path,
        env=env,
        dry_run=dry_run,
        verbose=verbose,
        description="Installing pgast...",
    )

    print(f"  pgast {version} installed successfully")


# =============================================================================
# Main Orchestration
# =============================================================================


def generate_bash_completion() -> str:
    """Generate bash completion script."""
    script_name = Path(sys.argv[0]).name
    return f'''# Bash completion for {script_name}
# Add to ~/.bashrc: eval "$({script_name} --completions bash)"
# Or save to: /etc/bash_completion.d/{script_name}

_{script_name.replace("-", "_").replace(".", "_")}_completions() {{
    local cur prev opts components
    COMPREPLY=()
    cur="${{COMP_WORDS[COMP_CWORD]}}"
    prev="${{COMP_WORDS[COMP_CWORD-1]}}"
    opts="--config --dry-run --component --skip-extensions --exclude-ast --verbose --completions --help"
    components="readline icu postgresql contrib q3c ast pgast"

    case "${{prev}}" in
        --config)
            COMPREPLY=( $(compgen -f -- "${{cur}}") )
            return 0
            ;;
        --component)
            COMPREPLY=( $(compgen -W "${{components}}" -- "${{cur}}") )
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
# Or save to a file in your $fpath

_pginstall() {{
    local -a opts components shells
    opts=(
        '--config[Config file for version pinning]:file:_files'
        '--dry-run[Show what would be done without executing]'
        '--component[Build only specific component]:component:(readline icu postgresql contrib q3c ast pgast)'
        '--skip-extensions[Skip q3c, ast, and pgast extensions]'
        '--exclude-ast[Exclude Starlink AST library and pgast]'
        '--verbose[Show all build output]'
        '--completions[Output shell completion script]:shell:(bash zsh)'
        '--help[Show help message]'
    )
    _arguments -s $opts
}}

_pginstall "$@"
'''


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="PostgreSQL Source Installer - Build PostgreSQL and dependencies from source",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                     # Build everything with auto-detected versions
  %(prog)s --dry-run           # Show what would be done
  %(prog)s --config pginstall.conf  # Use config file for version pinning
  %(prog)s --component icu     # Build only ICU
  %(prog)s --skip-extensions   # Skip q3c, ast, and pgast
  %(prog)s --exclude-ast       # Skip Starlink AST library and pgast

Components:
  readline     GNU readline (macOS only)
  icu          ICU - International Components for Unicode
  postgresql   PostgreSQL database server
  contrib      Contrib extensions (citext, cube, earthdistance, pg_trgm)
  q3c          Q3C spatial indexing extension
  ast          Starlink AST library (required by pgast)
  pgast        pgast extension (requires ast)

Shell completions:
  %(prog)s --completions bash  # Output bash completion script
  %(prog)s --completions zsh   # Output zsh completion script

  To enable, add to your shell config:
    eval "$(%(prog)s --completions bash)"  # for bash
    eval "$(%(prog)s --completions zsh)"   # for zsh
""",
    )

    parser.add_argument(
        "--config",
        type=Path,
        help="Config file for version pinning (INI format)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without executing",
    )
    parser.add_argument(
        "--component",
        choices=["readline", "icu", "postgresql", "contrib", "q3c", "ast", "pgast"],
        help="Build only specific component",
    )
    parser.add_argument(
        "--skip-extensions",
        action="store_true",
        help="Skip q3c, ast, and pgast extensions",
    )
    parser.add_argument(
        "--exclude-ast",
        action="store_true",
        help="Exclude Starlink AST library and pgast extension",
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


def get_required_tools(exclude_ast: bool = False) -> list[str]:
    """Return list of required tools for the current platform."""
    tools = ["make", "gcc", "tar", "git", "bison", "flex"]
    plat = get_platform()

    if plat == "darwin":
        tools.append("clang")
    else:
        tools.append("patchelf")

    # Fortran compiler required for Starlink AST library
    if not exclude_ast:
        tools.append("gfortran")

    return tools


def find_system_patchelf() -> str | None:
    """Find patchelf in system paths (not anaconda). Returns full path or None."""
    # Check system paths first
    system_paths = ["/usr/bin/patchelf", "/usr/local/bin/patchelf"]
    for path in system_paths:
        if Path(path).exists():
            return path
    # Fall back to which, but exclude anaconda
    patchelf_path = shutil.which("patchelf")
    if patchelf_path and "anaconda" not in patchelf_path.lower():
        return patchelf_path
    return None


def check_missing_tools(exclude_ast: bool = False) -> list[str]:
    """Return list of missing required tools."""
    required = get_required_tools(exclude_ast=exclude_ast)
    missing = []
    for tool in required:
        if tool == "patchelf":
            # patchelf needs to be in system path for sudo to work
            if not find_system_patchelf():
                missing.append(tool)
        elif not shutil.which(tool):
            missing.append(tool)
    return missing


def check_missing_libraries() -> list[str]:
    """Return list of missing required libraries (Linux only)."""
    if get_platform() == "darwin":
        return []  # macOS builds readline from source

    missing = []

    # Check for readline development headers
    readline_paths = [
        Path("/usr/include/readline/readline.h"),
        Path("/usr/local/include/readline/readline.h"),
    ]
    if not any(p.exists() for p in readline_paths):
        missing.append("libreadline-dev")

    # Check for zlib development headers
    zlib_paths = [
        Path("/usr/include/zlib.h"),
        Path("/usr/local/include/zlib.h"),
    ]
    if not any(p.exists() for p in zlib_paths):
        missing.append("zlib1g-dev")

    return missing


def get_package_names_for_tools(tools: list[str], pkg_mgr: str) -> list[str]:
    """Map tool names to package names for a given package manager."""
    # Mapping: tool -> {pkg_mgr: package_name}
    tool_to_pkg = {
        "make": {"apt": "make", "dnf": "make", "yum": "make", "pacman": "make"},
        "gcc": {"apt": "gcc", "dnf": "gcc", "yum": "gcc", "pacman": "gcc"},
        "tar": {"apt": "tar", "dnf": "tar", "yum": "tar", "pacman": "tar"},
        "git": {"apt": "git", "dnf": "git", "yum": "git", "pacman": "git"},
        "bison": {"apt": "bison", "dnf": "bison", "yum": "bison", "pacman": "bison"},
        "flex": {"apt": "flex", "dnf": "flex", "yum": "flex", "pacman": "flex"},
        "patchelf": {"apt": "patchelf", "dnf": "patchelf", "yum": "patchelf", "pacman": "patchelf"},
        "clang": {"apt": "clang", "dnf": "clang", "yum": "clang", "pacman": "clang"},
        "gfortran": {"apt": "gfortran", "dnf": "gcc-gfortran", "yum": "gcc-gfortran", "pacman": "gcc-fortran"},
        "libreadline-dev": {"apt": "libreadline-dev", "dnf": "readline-devel", "yum": "readline-devel", "pacman": "readline"},
        "zlib1g-dev": {"apt": "zlib1g-dev", "dnf": "zlib-devel", "yum": "zlib-devel", "pacman": "zlib"},
    }

    # For apt, build-essential provides make and gcc
    if pkg_mgr == "apt":
        packages = set()
        has_build_tools = False
        for tool in tools:
            if tool in ("make", "gcc"):
                has_build_tools = True
            elif tool in tool_to_pkg:
                packages.add(tool_to_pkg[tool].get(pkg_mgr, tool))
            else:
                packages.add(tool)
        if has_build_tools:
            packages.add("build-essential")
        return sorted(packages)

    # For other package managers
    packages = set()
    for tool in tools:
        if tool in tool_to_pkg:
            packages.add(tool_to_pkg[tool].get(pkg_mgr, tool))
        else:
            packages.add(tool)
    return sorted(packages)


def get_prereq_install_command(missing_tools: list[str]) -> Optional[str]:
    """Return command to install missing prerequisites on Linux."""
    if not missing_tools:
        return None

    pkg_mgr = detect_package_manager()
    if not pkg_mgr:
        return None

    packages = get_package_names_for_tools(missing_tools, pkg_mgr)

    if pkg_mgr == "apt":
        return f"sudo apt install {' '.join(packages)}"
    elif pkg_mgr == "dnf":
        return f"sudo dnf install {' '.join(packages)}"
    elif pkg_mgr == "yum":
        return f"sudo yum install {' '.join(packages)}"
    elif pkg_mgr == "pacman":
        return f"sudo pacman -S {' '.join(packages)}"
    return None


def check_src_dir(dry_run: bool = False) -> bool:
    """Check that the source directory exists and is writable."""
    print(f"Checking source directory ({SRC_DIR})...")

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


def check_prerequisites(dry_run: bool = False, exclude_ast: bool = False) -> list[str]:
    """Check that required tools and libraries are available. Returns list of missing items."""
    print("Checking prerequisites...")

    missing_tools = check_missing_tools(exclude_ast=exclude_ast)
    missing_libs = check_missing_libraries()
    missing = missing_tools + missing_libs

    if missing:
        if dry_run:
            if missing_tools:
                print(f"  🛑 Missing tools: {', '.join(missing_tools)}")
            if missing_libs:
                print(f"  🛑 Missing libraries: {', '.join(missing_libs)}")
        else:
            if missing_tools:
                print(f"Error: Missing required tools: {', '.join(missing_tools)}", file=sys.stderr)
            if missing_libs:
                print(f"Error: Missing required libraries: {', '.join(missing_libs)}", file=sys.stderr)
            install_cmd = get_prereq_install_command(missing)
            if install_cmd:
                print(f"  Install with: {install_cmd}", file=sys.stderr)
            elif get_platform() == "darwin":
                print("  Install Xcode Command Line Tools: xcode-select --install", file=sys.stderr)
            sys.exit(1)
    else:
        print("  ✅ All prerequisites satisfied")

    return missing


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

    plat = get_platform()

    print(f"PostgreSQL Source Installer")
    print(f"Platform: {plat}")
    print(f"CPU cores: {get_cpu_count()}")

    if args.dry_run:
        print("\n*** DRY RUN MODE - No changes will be made ***")

    # Check prerequisites
    missing_tools = check_prerequisites(dry_run=args.dry_run, exclude_ast=args.exclude_ast)

    # In dry-run mode, show install commands for missing prerequisites
    if args.dry_run and missing_tools:
        install_cmd = get_prereq_install_command(missing_tools)
        if install_cmd:
            print(f"\nTo install missing prerequisites:")
            print(f"  {install_cmd}")
        elif plat == "darwin":
            print(f"\nTo install missing prerequisites:")
            print(f"  xcode-select --install")

    # Check source directory exists and is writable
    check_src_dir(dry_run=args.dry_run)

    # Load versions
    versions = load_versions(args.config, exclude_ast=args.exclude_ast)

    print("\nBuild plan:")
    if plat == "darwin":
        print(f"  readline:   {versions.get('readline', 'N/A')}")
    print(f"  ICU:        {versions['icu']}")
    print(f"  PostgreSQL: {versions['postgresql']}")
    print(f"  q3c:        {versions['q3c']}")
    if not args.exclude_ast:
        print(f"  AST:        {versions['ast']}")
        print(f"  pgast:      {versions['pgast']}")
    else:
        print(f"  AST:        (excluded)")
        print(f"  pgast:      (excluded)")

    # Build components
    if args.component:
        # Build only specified component
        if args.component == "readline":
            if plat == "darwin":
                build_readline(versions["readline"], args.dry_run, args.verbose)
            else:
                print("readline is only built from source on macOS")
        elif args.component == "icu":
            build_icu(versions["icu"], args.dry_run, args.verbose)
        elif args.component == "postgresql":
            build_postgresql(versions["postgresql"], args.dry_run, args.verbose)
        elif args.component == "contrib":
            build_contrib_extensions(versions["postgresql"], args.dry_run, args.verbose)
        elif args.component == "q3c":
            build_q3c(versions["q3c"], args.dry_run, args.verbose)
        elif args.component == "ast":
            if args.exclude_ast:
                print("AST is excluded (--exclude-ast)")
            else:
                build_ast(versions["ast"], args.dry_run, args.verbose)
        elif args.component == "pgast":
            if args.exclude_ast:
                print("pgast is excluded (--exclude-ast)")
            else:
                build_pgast(versions["pgast"], args.dry_run, args.verbose)
    else:
        # Build everything in order
        if plat == "darwin":
            build_readline(versions["readline"], args.dry_run, args.verbose)

        build_icu(versions["icu"], args.dry_run, args.verbose)
        build_postgresql(versions["postgresql"], args.dry_run, args.verbose)
        build_contrib_extensions(versions["postgresql"], args.dry_run, args.verbose)

        if not args.skip_extensions:
            build_q3c(versions["q3c"], args.dry_run, args.verbose)
            if not args.exclude_ast:
                build_ast(versions["ast"], args.dry_run, args.verbose)
                build_pgast(versions["pgast"], args.dry_run, args.verbose)

    print(f"\n{'=' * 60}")
    print("Installation complete!")
    print(f"{'=' * 60}")
    print(f"\nPostgreSQL is available at: {INSTALL_BASE / 'postgresql'}")
    print(f"Add to your PATH: export PATH={INSTALL_BASE / 'postgresql' / 'bin'}:$PATH")

    # Show LLVM install hint on Linux if not found
    if args.dry_run and plat == "linux" and not find_llvm_config():
        llvm_cmd = get_llvm_install_command()
        if llvm_cmd:
            print(f"\nTo enable JIT support, install LLVM development packages:")
            print(f"  {llvm_cmd}")


if __name__ == "__main__":
    main()
