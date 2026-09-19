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
    --with-llvm        Enable LLVM/JIT support (required on macOS, auto on Linux)
    --build-llvm       Build LLVM from source rather than using the system one
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

# gfortran releases page for macOS
GFORTRAN_MACOS_RELEASES = "https://github.com/fxcoudert/gfortran-for-macOS/releases"

# Contrib extensions to build
CONTRIB_EXTENSIONS = ["citext", "cube", "earthdistance", "ltree", "pgcrypto", "pg_trgm"]


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
    """Return environment dict with PATH cleaned up for builds.

    - Removes anaconda paths (can cause conflicts)
    - Adds /usr/local/pkg-config/bin if it exists (macOS source install)
    """
    env = os.environ.copy()
    path_parts = env.get("PATH", "").split(":")

    # Remove anaconda paths
    sanitized_parts = [
        p for p in path_parts if "/usr/local/anaconda" not in p and "/anaconda" not in p
    ]

    # Add pkg-config path if it exists and not already in PATH
    pkg_config_bin = "/usr/local/pkg-config/bin"
    if Path(pkg_config_bin).is_dir() and pkg_config_bin not in sanitized_parts:
        sanitized_parts.insert(0, pkg_config_bin)

    env["PATH"] = ":".join(sanitized_parts)
    return env


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


def get_extension_build_env() -> dict:
    """Get environment for building PostgreSQL extensions."""
    env = get_sanitized_env()

    if get_platform() == "darwin":
        sdk_path = find_macos_sdk()
        if sdk_path:
            env["SDKROOT"] = sdk_path

    return env


def fix_stale_isysroot(flags: str, sdk_path: str) -> str:
    """Replace stale -isysroot in a flags string with the current SDK path.

    Args:
        flags: A compiler/linker flags string (e.g., from pg_config)
        sdk_path: The current SDK path to use

    Returns:
        The flags string with -isysroot pointing to the current SDK.
    """
    if "-isysroot" not in flags:
        return flags

    # Remove any existing -isysroot and its argument
    parts = flags.split()
    filtered = []
    skip_next = False
    for part in parts:
        if skip_next:
            skip_next = False
            continue
        if part == "-isysroot":
            skip_next = True
            continue
        if part.startswith("-isysroot"):
            # Handle -isysroot/path (no space) - unlikely but handle it
            continue
        filtered.append(part)

    # Add the correct -isysroot
    filtered.append("-isysroot")
    filtered.append(sdk_path)
    return " ".join(filtered)


def get_fixed_pg_config_flags(pg_config: Path, flag_type: str) -> Optional[str]:
    """Get pg_config flags with stale -isysroot replaced by current SDK path.

    On macOS, PostgreSQL embeds the SDK path used at compile time into pg_config.
    After an Xcode update, this path may no longer exist. This function returns
    corrected flags with the current SDK path.

    Args:
        pg_config: Path to pg_config binary
        flag_type: One of "cppflags" or "ldflags"

    Returns:
        Fixed flags string, or None if no fix is needed (Linux or no -isysroot).
    """
    if get_platform() != "darwin":
        return None

    sdk_path = find_macos_sdk()
    if not sdk_path:
        return None

    try:
        result = subprocess.run(
            [str(pg_config), f"--{flag_type}"],
            capture_output=True,
            text=True,
            check=True,
        )
        flags = result.stdout.strip()

        if "-isysroot" not in flags:
            return None

        return fix_stale_isysroot(flags, sdk_path)

    except subprocess.CalledProcessError:
        return None


def get_extension_make_args(pg_config: Path) -> list[str]:
    """Get make arguments for building PostgreSQL extensions.

    On macOS, includes fixed CPPFLAGS and LDFLAGS if the SDK path in pg_config
    is stale (can happen after Xcode updates).
    """
    args = ["make", f"PG_CONFIG={pg_config}"]

    # Fix CPPFLAGS (for compilation)
    fixed_cppflags = get_fixed_pg_config_flags(pg_config, "cppflags")
    if fixed_cppflags:
        args.append(f"CPPFLAGS={fixed_cppflags}")

    # Fix LDFLAGS (for linking)
    fixed_ldflags = get_fixed_pg_config_flags(pg_config, "ldflags")
    if fixed_ldflags:
        args.append(f"LDFLAGS={fixed_ldflags}")

    return args


def find_llvm_config() -> Optional[str]:
    """Locate llvm-config binary, return path or None.

    On Linux, /usr/bin/llvm-config belongs to the unversioned llvm-dev
    metapackage and follows whatever LLVM the distribution currently treats as
    default. Building against it bakes today's default into llvmjit.so, so a
    later default bump can retire that runtime and break JIT silently — the
    module loads lazily, so only queries above jit_above_cost fail. Prefer an
    explicitly versioned toolchain, which moves only when we rebuild.
    """
    # A private LLVM built by --build-llvm wins over anything the system
    # provides: it is the only one no package manager can retire.
    #
    # Resolved to its versioned target, never left as the /usr/local/llvm
    # alias. PostgreSQL bakes this path into its rpath, so an aliased rpath
    # would break an existing build the moment the symlink is repointed at a
    # newer LLVM -- the same "the runtime moved underneath us" failure this
    # whole feature exists to prevent, just self-inflicted.
    # The alias first when present, then any versioned private install. Without
    # the second pass, '--build-llvm --no-alias' would build a private LLVM that
    # a later ordinary rebuild could never find, silently falling back to the
    # system toolchain this feature exists to avoid.
    # Completeness matters here, not just the presence of llvm-config: a
    # half-installed newer tree would otherwise outrank a working older one and
    # PostgreSQL would fail later, missing clang or libLLVM.
    candidates: list[str] = []
    alias = INSTALL_BASE / "llvm" / "bin" / "llvm-config"
    if alias.is_file() and llvm_install_is_complete(alias.parent.parent.resolve()):
        candidates.append(str(alias.resolve()))

    private_versioned: list[tuple[tuple[int, ...], str]] = []
    for path in INSTALL_BASE.glob("llvm-*/bin/llvm-config"):
        match = re.match(r"^llvm-(\d+(?:\.\d+)*)$", path.parent.parent.name)
        if match:
            key = tuple(int(part) for part in match.group(1).split("."))
            if not llvm_install_is_complete(path.parent.parent):
                print(f"  Ignoring incomplete LLVM install: {path.parent.parent}")
                continue
            # Resolved like the alias branch: this path is baked into
            # PostgreSQL's rpath, so it must be concrete.
            private_versioned.append((key, str(path.resolve())))
    private_versioned.sort(key=lambda item: item[0], reverse=True)
    for _, path in private_versioned:
        if path not in candidates:
            candidates.append(path)

    if get_platform() == "linux":
        # Versioned toolchains first, highest version wins. Compare as version
        # tuples, not text: a lexical sort ranks llvm-9 above llvm-21, and
        # handles dotted names (llvm-6.0) alongside plain ones (llvm-21).
        versioned: list[tuple[tuple[int, ...], str]] = []
        for libdir in ("/usr/lib", "/usr/lib64"):
            for path in Path(libdir).glob("llvm-*/bin/llvm-config"):
                match = re.match(r"^llvm-(\d+(?:\.\d+)*)$", path.parent.parent.name)
                if match:
                    version = tuple(int(p) for p in match.group(1).split("."))
                    versioned.append((version, str(path)))
        versioned.sort(key=lambda item: item[0], reverse=True)
        candidates.extend(path for _, path in versioned)
        # Unversioned metapackage symlink only as a fallback.
        candidates.append("/usr/bin/llvm-config")
    else:
        candidates.extend([
            "/usr/bin/llvm-config",
            "/opt/homebrew/opt/llvm/bin/llvm-config",  # macOS Homebrew (Apple Silicon)
            "/usr/local/opt/llvm/bin/llvm-config",  # macOS Homebrew (Intel)
        ])

    for candidate in candidates:
        if Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate

    # Try finding in PATH
    result = shutil.which("llvm-config")
    if result:
        return result

    return None


def get_llvm_version(llvm_config: str) -> Optional[str]:
    """Return the version reported by an llvm-config, or None if it fails."""
    try:
        result = subprocess.run(
            [llvm_config, "--version"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def get_pg_config_setting(pg_config: Path, flag: str) -> Optional[str]:
    """Return a single pg_config setting, or None if it could not be queried."""
    try:
        result = subprocess.run(
            [str(pg_config), flag], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


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


def offer_jit_protection(dry_run: bool, pg_config: Path,
                         private_llvm: bool = False) -> None:
    """After a JIT-enabled build, offer to make the LLVM dependency visible to apt.

    llvmjit.so links against a specific versioned LLVM runtime that the package
    manager has no record of. Left undeclared, a routine upgrade can retire that
    runtime: the server still starts and cheap queries still work, so the
    breakage only surfaces when a query crosses jit_above_cost. pgjitguard.py
    derives the dependency from the built module and enforces it.

    pg_config must be the concrete versioned path of the build we just made.
    pgjitguard defaults to the /usr/local/postgresql symlink, which --no-alias
    (or a declined symlink update) can leave pointing at an older installation —
    protecting that one would report success while leaving this build exposed.

    private_llvm says the build links a privately built LLVM. There is then
    nothing to protect, but a pin from before the switch may still be installed
    and still depending on the old system runtime, which stops apt reclaiming
    it. That cleanup must not depend on someone answering a prompt.
    """
    if get_platform() != "linux":
        return

    print(f"\n{'=' * 60}")
    print("JIT dependency protection")
    print(f"{'=' * 60}")
    if private_llvm:
        print("\n  PostgreSQL was built against a private LLVM under"
              f" {INSTALL_BASE},")
        print("  which no package manager owns. Nothing can remove it, so there")
        print("  is nothing to protect.")
    else:
        print("\n  PostgreSQL was built with JIT support. llvmjit.so links against a")
        print("  specific LLVM runtime that the package manager has no record of, so")
        print("  a later LLVM upgrade can remove it. The server would still start and")
        print("  cheap queries would still work; only queries above jit_above_cost")
        print("  would fail.")

    guard = SCRIPT_DIR / "pgjitguard.py"
    if not guard.is_file():
        print(f"\n  pgjitguard.py was not found next to this script ({SCRIPT_DIR}).")
        print("  Restore it to protect the dependency automatically.")
        return

    # Name the build explicitly rather than letting pgjitguard fall back to the
    # /usr/local/postgresql symlink, which may point at a different version.
    protect_cmd = ["sudo", str(guard), "--pg-config", str(pg_config), "protect"]
    protect_hint = f"sudo {guard} --pg-config {pg_config} protect"

    if private_llvm and detect_package_manager() != "apt":
        # Nothing to protect and no dpkg to clean up. Saying anything about
        # versionlock here would contradict the banner above.
        print("\n  Nothing further to do.")
        return

    if detect_package_manager() != "apt":
        # The enforcement mechanisms are dpkg-specific; say so rather than
        # leaving the impression that nothing needs doing.
        print("\n  Automatic protection requires a dpkg-based system. On this")
        print("  distribution, prevent the LLVM runtime from being removed using")
        print("  your package manager's equivalent (e.g. 'dnf versionlock').")
        print(f"\n  To inspect the dependency at any time:")
        print(f"    {guard} --pg-config {pg_config} status")
        return

    # Ahead of the existence check: during a dry run of a new version the
    # versioned path legitimately does not exist yet, and the point of a dry
    # run is to show the plan rather than report the absence.
    if private_llvm:
        # Nothing to protect, so nothing to ask. Run the guard anyway: it is a
        # no-op unless a pin survives from before the switch, in which case it
        # removes the package still pinning the old system LLVM.
        print("\n  Checking for a pin left over from before the switch:")
        if dry_run:
            print(f"\n  [dry-run] Would run: {protect_hint}")
            return
        if not pg_config.is_file():
            print(f"\n  Expected pg_config at {pg_config}, but it is not there.")
            print(f"  Skipping; run this once it exists:\n    {protect_hint}")
            return
        result = subprocess.run(protect_cmd)
        if result.returncode != 0:
            print(f"\n  pgjitguard exited {result.returncode}. To retry:",
                  file=sys.stderr)
            print(f"    {protect_hint}", file=sys.stderr)
        return

    if dry_run:
        print(f"\n  [dry-run] Would offer to run: {protect_hint}")
        return

    if not pg_config.is_file():
        print(f"\n  Expected pg_config at {pg_config}, but it is not there.")
        print("  Skipping; run this once the installation is in place:")
        print(f"    {protect_hint}")
        return

    if not sys.stdin.isatty():
        print("\n  Not running on a terminal, so skipping the prompt. To protect")
        print(f"  the dependency, run:\n    {protect_hint}")
        return

    if not prompt_yes_no("\n  Protect this dependency now?", default=True):
        print(f"\n  Skipped. To do it later, run:\n    {protect_hint}")
        return

    # pgjitguard prompts for the enforcement method itself.
    result = subprocess.run(protect_cmd)
    if result.returncode != 0:
        print(f"\n  pgjitguard exited {result.returncode}; the dependency is NOT",
              file=sys.stderr)
        print(f"  protected. To retry:\n    {protect_hint}", file=sys.stderr)
        return

    print(f"\n  Re-run this after any future PostgreSQL rebuild:\n    {protect_hint}")


def check_existing(install_path: Path) -> bool:
    """Return True if installation already exists."""
    return install_path.is_dir()


def get_pg_configure_flags(install_path: Path) -> Optional[str]:
    """Get the configure flags used to build an existing PostgreSQL installation.

    Returns the configure string from pg_config, or None if pg_config does not
    exist at the expected path (a normal condition: an incomplete or foreign
    install directory). A pg_config that exists but fails to run is a genuine
    error and is reported to stderr rather than swallowed, since the caller
    otherwise cannot tell "confirmed nothing missing" from "could not check".
    """
    pg_config = install_path / "bin" / "pg_config"
    if not pg_config.exists():
        return None
    try:
        result = subprocess.run(
            [str(pg_config), "--configure"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        print(f"  WARNING: {pg_config} --configure exited {result.returncode}; "
              f"cannot verify existing build's configure flags.", file=sys.stderr)
        if result.stderr.strip():
            print(f"    {result.stderr.strip()}", file=sys.stderr)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"  WARNING: could not run {pg_config} --configure: {e}",
              file=sys.stderr)
    return None


def check_pg_needs_rebuild(install_path: Path,
                            required_flags: list[str]) -> Optional[list[str]]:
    """Check if an existing PostgreSQL installation is missing required configure flags.

    Returns a list of missing flags (empty if all are present), or None if the
    existing build's configure flags could not be determined at all -- the
    caller must not treat that as "nothing missing".
    """
    configure_str = get_pg_configure_flags(install_path)
    if configure_str is None:
        return None
    missing = [flag for flag in required_flags if flag not in configure_str]
    return missing


def find_existing_postgresql_installations() -> list[tuple[str, Path]]:
    """Find existing PostgreSQL installations in INSTALL_BASE.

    Returns:
        List of (version, path) tuples for each found installation,
        sorted by version (newest first).
    """
    installations = []
    for path in INSTALL_BASE.glob("postgresql-*"):
        if path.is_dir() and not path.is_symlink():
            # Extract version from directory name
            version = path.name.replace("postgresql-", "")
            installations.append((version, path))

    # Sort by version (newest first) using simple string comparison
    # This works for PostgreSQL versions like "17.5", "18.1"
    installations.sort(key=lambda x: [int(p) for p in x[0].split(".")], reverse=True)
    return installations


def get_symlink_target(symlink_path: Path) -> Optional[Path]:
    """Get the target of a symlink, or None if it doesn't exist or isn't a symlink."""
    if symlink_path.is_symlink():
        target = symlink_path.resolve()
        return target
    return None


def prompt_yes_no(question: str, default: bool = True) -> bool:
    """Prompt user for yes/no answer.

    Args:
        question: The question to ask
        default: Default answer if user just presses Enter

    Returns:
        True for yes, False for no
    """
    if default:
        prompt = f"{question} [Y/n]: "
    else:
        prompt = f"{question} [y/N]: "

    while True:
        response = input(prompt).strip().lower()
        if response == "":
            return default
        if response in ("y", "yes"):
            return True
        if response in ("n", "no"):
            return False
        print("Please answer 'y' or 'n'")


def create_symlink(target: Path, link_name: Path, dry_run: bool = False) -> None:
    """Create or update versioned symlink using sudo."""
    if dry_run:
        print(f"  Would create symlink: {link_name} -> {target}")
        return

    # Use sudo ln -sfn to create/update symlink in /usr/local
    # The -n flag is crucial: without it, if link_name is an existing symlink
    # to a directory, ln would create the new link INSIDE that directory
    # instead of replacing the symlink itself.
    try:
        subprocess.run(
            ["sudo", "ln", "-sfn", str(target), str(link_name)],
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


def get_latest_openssl_version() -> str:
    """Query GitHub API for latest OpenSSL 3.x release version."""
    print("  Detecting latest OpenSSL version...")
    releases = github_api_get("/repos/openssl/openssl/releases")
    for release in releases:
        if release.get("prerelease", False):
            continue
        tag = release.get("tag_name", "")
        # OpenSSL tags are like "openssl-3.6.1"
        match = re.match(r"openssl-(\d+\.\d+\.\d+)", tag)
        if match:
            version = match.group(1)
            major = int(version.split(".")[0])
            if major >= 3:
                print(f"  Latest OpenSSL version: {version}")
                return version
    print("Error: Could not determine latest OpenSSL version", file=sys.stderr)
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


def get_latest_llvm_version() -> str:
    """Query GitHub API for the latest stable LLVM release."""
    print("  Detecting latest LLVM version...")
    releases = github_api_get("/repos/llvm/llvm-project/releases")
    for release in releases:
        if release.get("prerelease"):
            continue
        tag = release.get("tag_name", "")
        # LLVM tags look like "llvmorg-21.1.0"; skip -rc and other suffixes.
        match = re.match(r"^llvmorg-(\d+\.\d+\.\d+)$", tag)
        if match:
            version = match.group(1)
            print(f"  Latest LLVM version: {version}")
            return version
    print("Error: Could not determine latest LLVM version", file=sys.stderr)
    sys.exit(1)


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


# Which versions each component actually needs resolved. contrib is built from
# the PostgreSQL source tree, so it needs that version too.
COMPONENT_VERSIONS = {
    "readline": ["readline"],
    "openssl": ["openssl"],
    "icu": ["icu"],
    "llvm": ["llvm"],
    "postgresql": ["postgresql"],
    "contrib": ["postgresql"],
    "q3c": ["q3c"],
    "ast": ["ast"],
    "pgast": ["pgast"],
}


def load_versions(config_path: Optional[Path], exclude_ast: bool = False,
                  build_llvm: bool = False, skip_extensions: bool = False,
                  component: Optional[str] = None) -> dict:
    """Resolve component versions, preferring the config file over discovery.

    The config is read first and upstream is queried only for what it does not
    pin. Detecting every version up front and then overriding it meant a fully
    pinned configuration still failed offline, or when GitHub rate-limited the
    request -- pinning exists precisely to avoid depending on that.
    """
    plat = get_platform()

    detectors = {
        "openssl": get_latest_openssl_version,
        "icu": get_latest_icu_version,
        "postgresql": get_latest_postgresql_version,
        "q3c": get_latest_q3c_version,
    }
    if plat == "darwin":
        detectors["readline"] = get_latest_readline_version
    if build_llvm:
        detectors["llvm"] = get_latest_llvm_version
    if not exclude_ast:
        detectors["ast"] = get_latest_ast_version
        detectors["pgast"] = get_latest_pgast_version

    # A full run with --skip-extensions never builds q3c/ast/pgast, so it must
    # not depend on their upstreams being reachable either. An explicit
    # '--component q3c' (etc.) still wins over --skip-extensions below, same
    # as the dispatch in main() that actually builds it, so this is scoped to
    # the component-less "build everything" case only.
    if skip_extensions and component is None:
        for key in ("q3c", "ast", "pgast"):
            detectors.pop(key, None)

    # Building one component must not depend on unrelated upstreams being
    # reachable: '--component llvm' should not fail because GitHub rate-limited
    # a q3c release query.
    if component in COMPONENT_VERSIONS:
        wanted = set(COMPONENT_VERSIONS[component])
        # --build-llvm links PostgreSQL against the private toolchain, so a
        # PostgreSQL-only run still has to resolve the LLVM version in order to
        # locate it. Trimming that away crashed the run outright.
        if build_llvm and component in ("postgresql", "llvm"):
            wanted.add("llvm")
        detectors = {k: v for k, v in detectors.items() if k in wanted}

    pinned: dict[str, str] = {}
    if config_path and config_path.exists():
        print(f"\nLoading config from: {config_path}")
        config = configparser.ConfigParser()
        config.read(config_path)
        if "versions" in config:
            for key, value in config["versions"].items():
                if key in detectors:
                    pinned[key] = value
                else:
                    # Never silently drop a pin: an ignored key means the user
                    # thinks they pinned something they did not.
                    print(f"  Ignoring '{key} = {value}': not built in this run")

    versions: dict[str, str] = {}
    to_detect = [key for key in detectors if key not in pinned]
    if to_detect:
        print("\nDetecting versions...")
    for key, detect in detectors.items():
        if key in pinned:
            print(f"  {key}: {pinned[key]} (pinned)")
            versions[key] = pinned[key]
        else:
            versions[key] = detect()

    return versions


# =============================================================================
# Build Functions
# =============================================================================


def build_readline(version: str, dry_run: bool = False, verbose: bool = False, no_alias: bool = False) -> None:
    """Build readline from source (macOS only)."""
    install_path = INSTALL_BASE / f"readline-{version}"
    symlink_path = INSTALL_BASE / "readline"

    print(f"\n{'=' * 60}")
    print(f"Building readline {version}")
    print(f"{'=' * 60}")

    if check_existing(install_path):
        print(f"  Already installed: {install_path}")
        if not no_alias:
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
    if not no_alias:
        create_symlink(install_path, symlink_path, dry_run)
    print(f"  readline {version} installed successfully")


def build_icu(version: str, dry_run: bool = False, verbose: bool = False, no_alias: bool = False) -> None:
    """Build ICU from source."""
    install_path = INSTALL_BASE / f"icu-{version}"
    symlink_path = INSTALL_BASE / "icu"

    print(f"\n{'=' * 60}")
    print(f"Building ICU {version}")
    print(f"{'=' * 60}")

    if check_existing(install_path):
        print(f"  Already installed: {install_path}")
        if not no_alias:
            create_symlink(install_path, symlink_path, dry_run)
        return

    # Download and extract
    # ICU download URL format: https://github.com/unicode-org/icu/releases/download/release-76-1/icu4c-76_1-src.tgz
    version_parts = version.split(".")
    tag_version = f"{version_parts[0]}-{version_parts[1]}"
    file_version = f"{version_parts[0]}_{version_parts[1]}"
    url = f"https://github.com/unicode-org/icu/releases/download/release-{tag_version}/icu4c-{file_version}-src.tgz"

    # ICU always extracts to 'icu/' regardless of version, so stale build
    # artifacts from a previous version would cause symbol mismatches.
    # Remove the old source directory before extracting.
    icu_src_dir = SRC_DIR / "icu"
    if icu_src_dir.exists() and not dry_run:
        print(f"  Removing old ICU source directory: {icu_src_dir}")
        shutil.rmtree(icu_src_dir)

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
    if not no_alias:
        create_symlink(install_path, symlink_path, dry_run)
    print(f"  ICU {version} installed successfully")


def build_openssl(version: str, dry_run: bool = False, verbose: bool = False, no_alias: bool = False) -> None:
    """Build OpenSSL from source."""
    install_path = INSTALL_BASE / f"openssl-{version}"
    symlink_path = INSTALL_BASE / "openssl"

    print(f"\n{'=' * 60}")
    print(f"Building OpenSSL {version}")
    print(f"{'=' * 60}")

    if check_existing(install_path):
        print(f"  Already installed: {install_path}")
        if not no_alias:
            create_symlink(install_path, symlink_path, dry_run)
        return

    # Download and extract
    url = f"https://github.com/openssl/openssl/releases/download/openssl-{version}/openssl-{version}.tar.gz"
    src_path = download_and_extract(url, SRC_DIR, dry_run)

    if not dry_run:
        # OpenSSL uses ./config instead of ./configure
        run_build_cmd(
            [
                "./config",
                f"--prefix={install_path}",
                f"--openssldir={install_path}",
            ],
            cwd=src_path,
            dry_run=dry_run,
            verbose=verbose,
            description="Configuring OpenSSL...",
        )

        # Build
        run_build_cmd(
            ["make", f"-j{get_cpu_count()}"],
            cwd=src_path,
            dry_run=dry_run,
            verbose=verbose,
            description="Building OpenSSL...",
        )

        # Install
        run_build_cmd(
            ["sudo", "make", "install"],
            cwd=src_path,
            dry_run=dry_run,
            verbose=verbose,
            description="Installing OpenSSL...",
        )
    else:
        print("  Would configure, build, and install OpenSSL")

    # Create symlink
    if not no_alias:
        create_symlink(install_path, symlink_path, dry_run)
    print(f"  OpenSSL {version} installed successfully")


def find_existing_private_llvm_config() -> Optional[str]:
    """Return the on-disk private LLVM's llvm-config, or None if only a system
    LLVM (or nothing at all) is discoverable.

    Used by '--component postgresql --build-llvm', which links against
    whatever private LLVM already exists rather than building one -- it must
    not accept a system toolchain as a substitute, which is the dependency
    --build-llvm exists to avoid.
    """
    discovered = find_llvm_config()
    if discovered and str(Path(discovered).resolve()).startswith(str(INSTALL_BASE)):
        return discovered
    return None


def private_llvm_config(version: str) -> Path:
    """Path to the llvm-config of a --build-llvm toolchain.

    Deliberately the versioned path rather than the /usr/local/llvm symlink:
    --no-alias skips creating that symlink, and discovery would then miss the
    LLVM we just built and quietly settle for a system one -- the very
    dependency --build-llvm exists to avoid.

    Resolved like find_llvm_config()'s results: the LLVM_CONFIG= value this
    feeds into required_configure_flags (see build_postgresql) is compared
    against an existing build's recorded configure string, and an unresolved
    vs. resolved mismatch there would falsely detect a toolchain change.
    """
    return (INSTALL_BASE / f"llvm-{version}" / "bin" / "llvm-config").resolve()


def find_llvm_runtime_libs(libdir: Path) -> list[Path]:
    """Return the shared LLVM runtime library files llvmjit.so needs.

    A glob match alone is not enough: it also matches directories and dangling
    symlinks from an interrupted install. libLLVM-C is the C API wrapper, not
    the library PostgreSQL links, so it does not count either.
    """
    candidates = list(libdir.glob("libLLVM*.so*")) + list(libdir.glob("libLLVM*.dylib"))
    return [c for c in candidates
            if not c.name.startswith("libLLVM-C") and c.is_file()]


def llvm_install_is_complete(install_path: Path) -> bool:
    """Return True only if an LLVM install has the pieces PostgreSQL needs.

    The presence of the directory proves nothing: a 'cmake --install' that dies
    partway leaves one behind. Trusting it would make every retry a no-op and
    could alias a broken tree into /usr/local/llvm.
    """
    # Executable, not merely present: an interrupted install can leave a
    # non-executable stub that PostgreSQL cannot actually run.
    for binary in ("llvm-config", "clang"):
        path = install_path / "bin" / binary
        if not (path.is_file() and os.access(path, os.X_OK)):
            return False
    # llvmjit.so links the shared runtime; a static-only tree is unusable here.
    return bool(find_llvm_runtime_libs(install_path / "lib"))


def build_llvm(
    version: str, dry_run: bool = False, verbose: bool = False, no_alias: bool = False
) -> None:
    """Build LLVM and clang from source.

    A source-built PostgreSQL linked against the *system* LLVM carries a
    dependency the package manager knows nothing about, so a distribution
    upgrade that retires that LLVM breaks JIT silently. Building LLVM under
    /usr/local puts the runtime outside the package manager's reach entirely,
    which removes the failure mode rather than guarding against it.

    clang is built alongside: PostgreSQL needs it to emit the bitcode that
    llvmjit.so consumes, and it must match the LLVM it was built against.
    """
    install_path = INSTALL_BASE / f"llvm-{version}"
    symlink_path = INSTALL_BASE / "llvm"

    print(f"\n{'=' * 60}")
    print(f"Building LLVM {version}")
    print(f"{'=' * 60}")

    if check_existing(install_path):
        if llvm_install_is_complete(install_path):
            print(f"  Already installed: {install_path}")
            if not no_alias:
                create_symlink(install_path, symlink_path, dry_run)
            return
        # Loud, and rebuild: silently aliasing a half-installed LLVM would
        # surface much later as a confusing PostgreSQL build failure.
        print(f"  Found an incomplete LLVM installation at {install_path}")
        print("  (missing llvm-config, clang, or the shared LLVM runtime)")
        print("  Rebuilding it.")

    # cmake is not needed by any other component, so it is not in the
    # documented prerequisites. Say so plainly rather than failing mid-build.
    if not dry_run and not shutil.which("cmake"):
        print("\nError: building LLVM requires cmake, which was not found.",
              file=sys.stderr)
        install_cmd = {
            "apt": "sudo apt install cmake ninja-build g++",
            "dnf": "sudo dnf install cmake ninja-build gcc-c++",
            "yum": "sudo yum install cmake ninja-build gcc-c++",
            "pacman": "sudo pacman -S cmake ninja gcc",
        }.get(detect_package_manager())
        if install_cmd:
            print(f"  Install it with:\n    {install_cmd}", file=sys.stderr)
        print("\n  Or omit --build-llvm to link against the system LLVM instead.",
              file=sys.stderr)
        sys.exit(1)

    # Ninja is required, not merely faster: LLVM_PARALLEL_LINK_JOBS below is a
    # Ninja-only feature. Under Unix Makefiles it is silently ignored, so -j
    # would start one link per core -- several GB each -- and exhaust memory on
    # most machines. Degrading to Makefiles would quietly remove the guard.
    if not dry_run and not shutil.which("ninja"):
        print("\nError: building LLVM requires ninja.", file=sys.stderr)
        print("  It caps parallel link jobs, which Unix Makefiles cannot do;"
              " without it", file=sys.stderr)
        print("  the link step will likely exhaust memory.", file=sys.stderr)
        install_cmd = {
            "apt": "sudo apt install ninja-build",
            "dnf": "sudo dnf install ninja-build",
            "yum": "sudo yum install ninja-build",
            "pacman": "sudo pacman -S ninja",
        }.get(detect_package_manager())
        if install_cmd:
            print(f"  Install it with:\n    {install_cmd}", file=sys.stderr)
        sys.exit(1)
    generator = "Ninja"

    cpu_count = get_cpu_count()
    # Linking LLVM is memory-hungry: several GB per link job. Running one link
    # per core is a reliable way to OOM a machine that compiles fine.
    link_jobs = max(1, cpu_count // 4)

    print(f"\n  This is a large build: expect roughly 30-90 minutes and several")
    print(f"  GB of disk under {SRC_DIR}.")
    print(f"\n  Note: building LLVM {version}. PostgreSQL usually trails new")
    print("  LLVM majors by a release or two, and this defaults to the newest")
    print("  stable release. If the build or JIT misbehaves, pin a known-good")
    print("  version in the config file:")
    print("    [versions]\n    llvm = 20.1.8")
    print(f"  Compile jobs: {cpu_count}, link jobs: {link_jobs} (linking LLVM"
          " needs several GB each)")

    url = (f"https://github.com/llvm/llvm-project/releases/download/"
           f"llvmorg-{version}/llvm-project-{version}.src.tar.xz")
    src_path = download_and_extract(url, SRC_DIR, dry_run)
    if dry_run:
        src_path = SRC_DIR / f"llvm-project-{version}.src"

    build_dir = src_path / "build"
    cmake_cmd = [
        "cmake",
        "-S", str(src_path / "llvm"),
        "-B", str(build_dir),
        "-G", generator,
        f"-DCMAKE_INSTALL_PREFIX={install_path}",
        # Fix the libdir at "lib" rather than leaving it to GNUInstallDirs,
        # which defaults to "lib64" on 64-bit Fedora/RHEL/SUSE -- everywhere
        # else in this file that looks for libLLVM.so assumes "lib".
        "-DCMAKE_INSTALL_LIBDIR=lib",
        "-DCMAKE_BUILD_TYPE=Release",
        # PostgreSQL shells out to clang to compile bitcode for the JIT.
        "-DLLVM_ENABLE_PROJECTS=clang",
        # llvmjit.so links libLLVM.so; without the shared library it would have
        # to link every static component instead.
        "-DLLVM_BUILD_LLVM_DYLIB=ON",
        "-DLLVM_LINK_LLVM_DYLIB=ON",
        # The JIT only ever targets the host, so building every backend would
        # multiply the build time for nothing.
        "-DLLVM_TARGETS_TO_BUILD=Native",
        "-DLLVM_ENABLE_RTTI=ON",
        "-DLLVM_INCLUDE_TESTS=OFF",
        "-DLLVM_INCLUDE_BENCHMARKS=OFF",
        "-DLLVM_INCLUDE_EXAMPLES=OFF",
        f"-DLLVM_PARALLEL_LINK_JOBS={link_jobs}",
    ]

    run_build_cmd(
        cmake_cmd, cwd=src_path, dry_run=dry_run, verbose=verbose,
        description="Configuring LLVM...",
    )
    run_build_cmd(
        ["cmake", "--build", str(build_dir), f"-j{cpu_count}"],
        cwd=src_path, dry_run=dry_run, verbose=verbose,
        description="Building LLVM (this takes a while)...",
    )
    run_build_cmd(
        ["sudo", "cmake", "--install", str(build_dir)],
        cwd=src_path, dry_run=dry_run, verbose=verbose,
        description="Installing LLVM...",
    )

    # Validate before publishing the alias. Pointing /usr/local/llvm at a tree
    # we are about to reject would leave the rejected build as the system's
    # default LLVM.
    if not dry_run and not llvm_install_is_complete(install_path):
        print(f"Error: LLVM install finished but {install_path} is missing",
              file=sys.stderr)
        print("  llvm-config, clang, or the shared LLVM runtime.", file=sys.stderr)
        print(f"  Leaving {symlink_path} untouched.", file=sys.stderr)
        sys.exit(1)

    if not no_alias:
        create_symlink(install_path, symlink_path, dry_run)

    if not dry_run:
        print(f"\n  LLVM installed: {install_path}")
        print("  PostgreSQL will use it automatically on the next build.")


def embed_private_llvm_runtime(pkglibdir: Path, llvm_libdir: Path, verbose: bool) -> None:
    """Copy the private LLVM's shared runtime into pkglibdir and rpath to it.

    llvmjit.so is built with an rpath pointing at the private LLVM tree
    (see build_postgresql), which works but leaves this PostgreSQL install
    dependent on that tree continuing to exist. If it is later deleted --
    reasonable cleanup once /usr/local/llvm has moved on to a newer version --
    JIT breaks the same way a retired system LLVM does: silently, only on a
    query that crosses jit_above_cost. Copying the runtime in and rpathing to
    '$ORIGIN' makes this PostgreSQL install carry its own copy, so nothing
    outside it can break JIT by disappearing.

    Linux only: patchelf's rpath rewriting is ELF-specific. On other
    platforms llvmjit.so keeps the absolute rpath set at build time, so this
    is a hardening step, not something later steps depend on.
    """
    if get_platform() == "darwin":
        return

    llvmjit = pkglibdir / "llvmjit.so"
    if not llvmjit.is_file():
        print(f"  WARNING: {llvmjit} not found; cannot embed the LLVM runtime.",
              file=sys.stderr)
        return

    runtime_libs = find_llvm_runtime_libs(llvm_libdir)
    if not runtime_libs:
        print(f"  WARNING: no LLVM runtime library found in {llvm_libdir}; "
              f"cannot embed it.", file=sys.stderr)
        return

    patchelf = find_system_patchelf()
    if not patchelf:
        print("  WARNING: patchelf not found; leaving llvmjit.so rpathed to "
              f"{llvm_libdir}.", file=sys.stderr)
        return

    print(f"  Embedding LLVM runtime into {pkglibdir} (self-contained install)...")
    for lib in runtime_libs:
        run_build_cmd(["sudo", "cp", "-P", str(lib), str(pkglibdir / lib.name)],
                       cwd=pkglibdir, verbose=verbose,
                       description=f"    Copying {lib.name}")
    run_build_cmd([patchelf, "--set-rpath", "$ORIGIN", str(llvmjit)],
                  cwd=pkglibdir, verbose=verbose,
                  description="    Setting llvmjit.so rpath to $ORIGIN")
    if not verbose:
        print(f"    Copied {len(runtime_libs)} file(s); rpath set to $ORIGIN")


def build_postgresql(
    version: str, dry_run: bool = False, verbose: bool = False,
    with_llvm: bool = False, no_alias: bool = False,
    llvm_config: Optional[str] = None,
) -> None:
    """Build PostgreSQL from source."""
    install_path = INSTALL_BASE / f"postgresql-{version}"
    symlink_path = INSTALL_BASE / "postgresql"

    # Resolved up front: the rebuild check below compares the concrete
    # LLVM_CONFIG, so it has to be known before that decision is made.
    if with_llvm and llvm_config is None:
        llvm_config = find_llvm_config()

    print(f"\n{'=' * 60}")
    print(f"Building PostgreSQL {version}")
    print(f"{'=' * 60}")

    # Check for existing installations
    existing_installations = find_existing_postgresql_installations()
    current_symlink_target = get_symlink_target(symlink_path)
    update_symlink = not no_alias  # Default to updating symlink unless --no-alias

    if existing_installations:
        # Check if there are OTHER versions installed (not the one we're installing)
        other_versions = [(v, p) for v, p in existing_installations if v != version]

        if other_versions:
            print(f"\n  Existing PostgreSQL installations found:")
            for v, p in existing_installations:
                marker = " (current symlink target)" if current_symlink_target == p else ""
                print(f"    - {v}: {p}{marker}")

            if current_symlink_target and current_symlink_target != install_path:
                current_version = current_symlink_target.name.replace("postgresql-", "")
                print(f"\n  The symlink '{symlink_path}' currently points to version {current_version}.")
                print(f"  This script will NOT delete existing installations.")

                if not dry_run:
                    update_symlink = prompt_yes_no(
                        f"\n  Update symlink to point to new version {version}?",
                        default=True
                    )
                    if not update_symlink:
                        print(f"  Will install {version} but leave symlink pointing to {current_version}")
                else:
                    print(f"  Would ask whether to update symlink to version {version}")

    # Check if existing installation needs rebuild due to missing configure flags
    required_configure_flags = ["--with-icu", "--with-openssl"]
    if with_llvm:
        required_configure_flags.append("--with-llvm")
        # Identity, not just presence. An existing build may already carry
        # --with-llvm while pointing at a different LLVM -- the system one, or
        # an older private one. Requiring the concrete LLVM_CONFIG forces a
        # rebuild when the toolchain changes; without it, --build-llvm can
        # spend an hour building LLVM and then leave PostgreSQL linked to the
        # old runtime.
        if llvm_config:
            required_configure_flags.append(f"LLVM_CONFIG={llvm_config}")
    needs_rebuild = False

    if check_existing(install_path):
        missing_flags = check_pg_needs_rebuild(install_path, required_configure_flags)
        if missing_flags is None:
            print(f"  Already installed: {install_path}")
            print(f"  WARNING: could not verify the existing build's configure"
                  f" flags (see above); cannot confirm "
                  f"{', '.join(required_configure_flags)} are all present.")
            if not dry_run:
                needs_rebuild = prompt_yes_no(
                    f"  Rebuild PostgreSQL {version} to be sure?",
                    default=False,
                )
            else:
                needs_rebuild = False
                print(f"  Cannot verify configure flags in dry-run; "
                      f"would ask whether to rebuild")
            if not needs_rebuild:
                print(f"  Skipping rebuild")
                if update_symlink:
                    create_symlink(install_path, symlink_path, dry_run)
                return
        elif missing_flags:
            print(f"  Already installed: {install_path}")
            print(f"  WARNING: Existing build is missing: {', '.join(missing_flags)}")
            if not dry_run:
                needs_rebuild = prompt_yes_no(
                    f"  Rebuild PostgreSQL {version} with {', '.join(missing_flags)}?",
                    default=True,
                )
            else:
                needs_rebuild = True
                print(f"  Would rebuild PostgreSQL {version} with {', '.join(missing_flags)}")
            if not needs_rebuild:
                print(f"  Skipping rebuild")
                if update_symlink:
                    create_symlink(install_path, symlink_path, dry_run)
                return
        else:
            print(f"  Already installed: {install_path}")
            if update_symlink:
                create_symlink(install_path, symlink_path, dry_run)
            else:
                print(f"  Symlink not updated (still points to {current_symlink_target})")
            return

    # Download and extract (or reuse existing source for rebuild)
    src_path = SRC_DIR / f"postgresql-{version}"
    if needs_rebuild and src_path.is_dir():
        print(f"  Reusing existing source tree: {src_path}")
    else:
        url = f"https://ftp.postgresql.org/pub/source/v{version}/postgresql-{version}.tar.gz"
        src_path = download_and_extract(url, SRC_DIR, dry_run)

    if dry_run:
        src_path = SRC_DIR / f"postgresql-{version}"

    # Build configure command
    plat = get_platform()
    icu_path = INSTALL_BASE / "icu"
    openssl_path = INSTALL_BASE / "openssl"

    # Determine OpenSSL lib directory (3.x uses lib64 on some platforms)
    openssl_lib_dir = openssl_path / "lib64"
    if not openssl_lib_dir.exists():
        openssl_lib_dir = openssl_path / "lib"
    print(f"  OpenSSL path: {openssl_path} (lib: {openssl_lib_dir.name})")

    configure_cmd = [
        "./configure",
        f"--prefix={install_path}",
        "--with-icu",
        "--with-openssl",
    ]

    # ICU flags
    env = get_sanitized_env()
    env["ICU_CFLAGS"] = f"-I{icu_path}/include"
    env["ICU_LIBS"] = f"-L{icu_path}/lib -licui18n -licuuc -licudata"

    # Set rpath so binaries can find ICU and OpenSSL libraries at runtime
    if plat == "darwin":
        env["LDFLAGS"] = (
            f"-L{icu_path}/lib -Wl,-rpath,{icu_path}/lib "
            f"-L{openssl_lib_dir} -Wl,-rpath,{openssl_lib_dir}"
        )
    else:
        env["LDFLAGS"] = (
            f"-Wl,-rpath,{icu_path}/lib "
            f"-Wl,-rpath,{openssl_lib_dir}"
        )

    # Platform-specific options
    if plat == "darwin":
        readline_path = INSTALL_BASE / "readline"
        configure_cmd.extend([
            "--with-bonjour",
            f"--with-libraries={readline_path}/lib:{openssl_lib_dir}",
            f"--with-includes={readline_path}/include:{openssl_path}/include",
        ])
    else:
        # Linux: readline from system packages; add OpenSSL paths
        configure_cmd.extend([
            f"--with-libraries={openssl_lib_dir}",
            f"--with-includes={openssl_path}/include",
        ])

    # LLVM/JIT support (resolved by caller)
    private_llvm_libdir: Optional[Path] = None  # set below when private, for embedding after install
    if with_llvm:
        if llvm_config:
            # Record the exact toolchain in the build output: llvmjit.so will
            # link against this LLVM's runtime, and that dependency is invisible
            # to the package manager afterwards.
            llvm_version = get_llvm_version(llvm_config)
            version_note = f", version {llvm_version}" if llvm_version else ""
            print(f"  LLVM/JIT: enabled ({llvm_config}{version_note})")
            configure_cmd.extend([
                "--with-llvm",
                f"LLVM_CONFIG={llvm_config}",
            ])

            # Use the clang that ships beside this llvm-config. PostgreSQL
            # compiles bitcode with clang and loads it with libLLVM; letting
            # configure pick an unrelated clang off PATH risks a version
            # mismatch between the bitcode and the runtime that reads it.
            llvm_bin = Path(llvm_config).parent
            clang_path = llvm_bin / "clang"
            # In a dry run the clang beside a not-yet-built private LLVM cannot
            # exist yet, but the real run will use it -- so show the same
            # configure line the real run would produce.
            pending = (dry_run and str(clang_path).startswith(str(INSTALL_BASE)))
            if clang_path.is_file() or pending:
                suffix = " (after build)" if pending and not clang_path.is_file() else ""
                print(f"  clang:    {clang_path}{suffix}")
                configure_cmd.append(f"CLANG={clang_path}")
            else:
                print(f"  WARNING: no clang beside {llvm_config}; configure will",
                      file=sys.stderr)
                print("  search PATH, which may find a different LLVM version.",
                      file=sys.stderr)

            # A privately built LLVM is not on the default loader path, so
            # llvmjit.so needs an rpath to find libLLVM.so at runtime. This is
            # what keeps the JIT working no matter what the distro does.
            llvm_libdir = Path(llvm_config).parent.parent / "lib"
            if str(llvm_libdir).startswith(str(INSTALL_BASE)):
                print(f"  LLVM rpath: {llvm_libdir}")
                env["LDFLAGS"] = env.get("LDFLAGS", "") + f" -Wl,-rpath,{llvm_libdir}"
                private_llvm_libdir = llvm_libdir
        else:
            # Caller should have validated this, but guard anyway
            print("  WARNING: --with-llvm requested but LLVM not found, skipping")
    else:
        print("  LLVM/JIT: disabled")

    if not dry_run:
        # Clean stale objects when rebuilding with different configure flags
        if needs_rebuild:
            run_build_cmd(
                ["make", "clean"],
                cwd=src_path,
                env=env,
                dry_run=dry_run,
                verbose=verbose,
                description="Cleaning previous build...",
            )

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

        if private_llvm_libdir:
            pkglibdir_str = get_pg_config_setting(
                install_path / "bin" / "pg_config", "--pkglibdir")
            if pkglibdir_str:
                embed_private_llvm_runtime(Path(pkglibdir_str), private_llvm_libdir, verbose)
            else:
                print(f"  WARNING: could not query pkglibdir from "
                      f"{install_path}/bin/pg_config; leaving llvmjit.so rpathed "
                      f"to {private_llvm_libdir}.", file=sys.stderr)
    else:
        if needs_rebuild:
            print("  Would run: make clean")
        print(f"  Would run: {' '.join(configure_cmd)}")
        print("  Would build and install PostgreSQL")
        if private_llvm_libdir:
            print("  Would embed the private LLVM runtime into pkglibdir "
                  "(self-contained install)")

    # Create/update symlink based on user preference
    if update_symlink:
        create_symlink(install_path, symlink_path, dry_run)
    else:
        print(f"  Symlink not updated (still points to {current_symlink_target})")

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

    env = get_extension_build_env()

    for ext in CONTRIB_EXTENSIONS:
        ext_path = src_path / ext
        if not ext_path.exists():
            print(f"  Warning: Extension {ext} not found at {ext_path}", file=sys.stderr)
            continue

        print(f"  Building {ext}...")

        # Build
        make_args = get_extension_make_args(pg_config)
        run_build_cmd(
            make_args,
            cwd=ext_path,
            env=env,
            dry_run=dry_run,
            verbose=verbose,
        )

        # Install
        install_args = ["sudo"] + make_args[:]  # Copy the list
        install_args.insert(2, "install")  # Insert after "make" and "PG_CONFIG=..."
        run_build_cmd(
            install_args,
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

    env = get_extension_build_env()
    make_args = get_extension_make_args(pg_config)

    # Build
    run_build_cmd(
        make_args,
        cwd=src_path,
        env=env,
        dry_run=dry_run,
        verbose=verbose,
        description="Building q3c...",
    )

    # Install
    install_args = ["sudo"] + make_args[:]
    install_args.insert(2, "install")
    run_build_cmd(
        install_args,
        cwd=src_path,
        env=env,
        dry_run=dry_run,
        verbose=verbose,
        description="Installing q3c...",
    )

    print(f"  q3c {version} installed successfully")


def build_ast(version: str, dry_run: bool = False, verbose: bool = False, no_alias: bool = False) -> None:
    """Build Starlink AST library from source."""
    install_path = INSTALL_BASE / f"ast-{version}"
    symlink_path = INSTALL_BASE / "ast"

    print(f"\n{'=' * 60}")
    print(f"Building Starlink AST {version}")
    print(f"{'=' * 60}")

    if check_existing(install_path):
        print(f"  Already installed: {install_path}")
        if not no_alias:
            create_symlink(install_path, symlink_path, dry_run)
        return

    # Download release tarball (use the pre-built release asset, not the source tarball)
    url = f"https://github.com/Starlink/ast/releases/download/v{version}/ast-{version}.tar.gz"
    src_path = download_and_extract(url, SRC_DIR, dry_run)

    if dry_run:
        src_path = SRC_DIR / f"ast-{version}"
        print(f"  Would build AST from: {src_path}")
        print(f"  Would install to: {install_path}")
        if not no_alias:
            create_symlink(install_path, symlink_path, dry_run)
        return

    env = get_sanitized_env()

    # On macOS, set SDKROOT so gfortran can find system libraries
    # This is needed when gfortran was built for an older macOS version
    if get_platform() == "darwin":
        sdk_path = find_macos_sdk()
        if sdk_path:
            env["SDKROOT"] = sdk_path

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
    if not no_alias:
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

    env = get_extension_build_env()
    make_args = get_extension_make_args(pg_config)
    make_args.append(f"AST={ast_path}")

    # Build (pass AST path to make)
    run_build_cmd(
        make_args,
        cwd=src_path,
        env=env,
        dry_run=dry_run,
        verbose=verbose,
        description="Building pgast...",
    )

    # Install (AST path also required for install target)
    install_args = ["sudo"] + make_args[:]
    install_args.insert(2, "install")
    run_build_cmd(
        install_args,
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
    opts="--config --dry-run --component --skip-extensions --exclude-ast --with-llvm --build-llvm --no-alias --verbose --completions --help"
    components="readline openssl icu llvm postgresql contrib q3c ast pgast"

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
        '--component[Build only specific component]:component:(readline openssl icu llvm postgresql contrib q3c ast pgast)'
        '--skip-extensions[Skip q3c, ast, and pgast extensions]'
        '--exclude-ast[Exclude Starlink AST library and pgast]'
        '--with-llvm[Enable LLVM/JIT support (opt-in on macOS, auto on Linux)]'
        '--build-llvm[Build LLVM from source instead of using the system LLVM]'
        '--no-alias[Skip creating/updating symlinks in /usr/local]'
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
  %(prog)s --with-llvm         # Enable LLVM/JIT support (required on macOS)

Components:
  readline     GNU readline (macOS only)
  openssl      OpenSSL cryptographic library
  icu          ICU - International Components for Unicode
  llvm         LLVM + clang (only with --build-llvm)
  postgresql   PostgreSQL database server
  contrib      Contrib extensions (citext, cube, earthdistance, ltree, pgcrypto, pg_trgm)
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
        choices=["readline", "openssl", "icu", "llvm", "postgresql", "contrib", "q3c", "ast", "pgast"],
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
        "--with-llvm",
        action="store_true",
        help="Enable LLVM/JIT support (opt-in on macOS, auto-detected on Linux)",
    )
    parser.add_argument(
        "--build-llvm",
        action="store_true",
        help="Build LLVM from source into /usr/local instead of using the "
             "system LLVM (implies --with-llvm; slow, but immune to distro "
             "LLVM upgrades)",
    )
    parser.add_argument(
        "--no-alias",
        action="store_true",
        help="Skip creating/updating symlinks in /usr/local",
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


# What each component actually needs, beyond the always-required basics.
# Only components with a genuinely smaller footprint are listed; anything
# absent falls back to the full set, which is the safe direction -- an
# over-strict pre-flight check is better than a build that dies halfway.
COMPONENT_TOOLS = {
    "llvm": ["make", "gcc", "tar", "cmake", "ninja", "c++"],
}


def get_required_tools(exclude_ast: bool = False, build_llvm: bool = False,
                       component: Optional[str] = None) -> list[str]:
    """Return list of required tools for the current platform."""
    plat = get_platform()

    # Building only LLVM needs none of PostgreSQL's bison/flex/patchelf, nor
    # gfortran for AST. Checking for them would block a valid run.
    if component in COMPONENT_TOOLS:
        return list(COMPONENT_TOOLS[component])

    tools = ["make", "gcc", "tar", "git", "bison", "flex"]

    # LLVM is C++ and needs its own toolchain. The documented Linux
    # prerequisites install a C compiler only, so a clean setup would otherwise
    # fail partway into the cmake configure step.
    if build_llvm:
        tools.extend(["cmake", "ninja", "c++"])

    if plat == "darwin":
        tools.append("clang")
        tools.append("pkg-config")
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


def find_pkg_config() -> Optional[str]:
    """Find pkg-config binary. Returns path or None."""
    # Check common source install location first
    source_path = "/usr/local/pkg-config/bin/pkg-config"
    if Path(source_path).is_file() and os.access(source_path, os.X_OK):
        return source_path

    # Check Homebrew paths
    homebrew_paths = [
        "/opt/homebrew/bin/pkg-config",  # Apple Silicon
        "/usr/local/bin/pkg-config",  # Intel
    ]
    for path in homebrew_paths:
        if Path(path).is_file() and os.access(path, os.X_OK):
            return path

    # Fall back to PATH
    return shutil.which("pkg-config")


def check_missing_tools(exclude_ast: bool = False, build_llvm: bool = False,
                        component: Optional[str] = None) -> list[str]:
    """Return list of missing required tools."""
    required = get_required_tools(exclude_ast=exclude_ast, build_llvm=build_llvm,
                                  component=component)
    missing = []
    for tool in required:
        if tool == "patchelf":
            # patchelf needs to be in system path for sudo to work
            if not find_system_patchelf():
                missing.append(tool)
        elif tool == "pkg-config":
            if not find_pkg_config():
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
        "cmake": {"apt": "cmake", "dnf": "cmake", "yum": "cmake", "pacman": "cmake"},
        "ninja": {"apt": "ninja-build", "dnf": "ninja-build", "yum": "ninja-build", "pacman": "ninja"},
        # LLVM is C++; the base prerequisites install a C compiler only.
        "c++": {"apt": "g++", "dnf": "gcc-c++", "yum": "gcc-c++", "pacman": "gcc"},
        "libreadline-dev": {"apt": "libreadline-dev", "dnf": "readline-devel", "yum": "readline-devel", "pacman": "readline"},
        "zlib1g-dev": {"apt": "zlib1g-dev", "dnf": "zlib-devel", "yum": "zlib-devel", "pacman": "zlib"},
    }

    # For apt, build-essential provides make and gcc
    if pkg_mgr == "apt":
        packages = set()
        has_build_tools = False
        for tool in tools:
            if tool in ("make", "gcc", "c++"):
                # build-essential provides all three on Debian/Ubuntu.
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


def check_prerequisites(dry_run: bool = False, exclude_ast: bool = False,
                        build_llvm: bool = False,
                        component: Optional[str] = None) -> list[str]:
    """Check that required tools and libraries are available. Returns list of missing items."""
    print("Checking prerequisites...")

    missing_tools = check_missing_tools(exclude_ast=exclude_ast,
                                        build_llvm=build_llvm,
                                        component=component)
    # readline/zlib headers are PostgreSQL's, not LLVM's.
    missing_libs = [] if component in COMPONENT_TOOLS else check_missing_libraries()
    missing = missing_tools + missing_libs

    if missing:
        plat = get_platform()
        gfortran_missing_macos = plat == "darwin" and "gfortran" in missing_tools

        if dry_run:
            if missing_tools:
                print(f"  🛑 Missing tools: {', '.join(missing_tools)}")
            if missing_libs:
                print(f"  🛑 Missing libraries: {', '.join(missing_libs)}")
            if gfortran_missing_macos:
                print(f"\n  gfortran is required for building the Starlink AST library.")
                print(f"  Download the installer for your macOS version from:")
                print(f"    {GFORTRAN_MACOS_RELEASES}")
        else:
            if missing_tools:
                print(f"Error: Missing required tools: {', '.join(missing_tools)}", file=sys.stderr)
            if missing_libs:
                print(f"Error: Missing required libraries: {', '.join(missing_libs)}", file=sys.stderr)

            # Special handling for macOS
            if plat == "darwin":
                # Check for pkg-config
                if "pkg-config" in missing_tools:
                    print(f"\n  pkg-config is required. Install from source:", file=sys.stderr)
                    print(f"    https://pkg-config.freedesktop.org/releases/", file=sys.stderr)
                    print(f"    (or via Homebrew: brew install pkg-config)", file=sys.stderr)

                # Check for gfortran
                if gfortran_missing_macos:
                    print(f"\n  gfortran is required for building the Starlink AST library.", file=sys.stderr)
                    print(f"  Download the installer for your macOS version from:", file=sys.stderr)
                    print(f"    {GFORTRAN_MACOS_RELEASES}", file=sys.stderr)

                # Check for other tools that come with Xcode
                other_missing = [t for t in missing_tools if t not in ("pkg-config", "gfortran")]
                if other_missing:
                    print(f"\n  Install Xcode Command Line Tools: xcode-select --install", file=sys.stderr)

                sys.exit(1)

            install_cmd = get_prereq_install_command(missing)
            if install_cmd:
                print(f"  Install with: {install_cmd}", file=sys.stderr)
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

    # Validated here, before check_prerequisites(): --component llvm demands
    # cmake/ninja/c++, so catching this after that check would surface a
    # confusing "missing tools" error instead of the one-line fix.
    if args.component == "llvm" and not args.build_llvm:
        print("--component llvm requires --build-llvm", file=sys.stderr)
        sys.exit(1)

    plat = get_platform()

    print(f"PostgreSQL Source Installer")
    print(f"Platform: {plat}")
    print(f"CPU cores: {get_cpu_count()}")

    if args.dry_run:
        print("\n*** DRY RUN MODE - No changes will be made ***")

    # Check prerequisites
    # Only demand cmake/ninja/c++ when this run will actually build LLVM.
    # '--component postgresql --build-llvm' links against an existing private
    # LLVM and needs none of them.
    will_build_llvm = args.build_llvm and args.component in (None, "llvm")
    missing_tools = check_prerequisites(dry_run=args.dry_run,
                                        exclude_ast=args.exclude_ast,
                                        build_llvm=will_build_llvm,
                                        component=args.component)

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
    versions = load_versions(args.config, exclude_ast=args.exclude_ast,
                             build_llvm=args.build_llvm,
                             skip_extensions=args.skip_extensions,
                             component=args.component)

    print("\nBuild plan:")
    labels = [
        ("readline", "readline"), ("openssl", "OpenSSL"), ("icu", "ICU"),
        ("llvm", "LLVM"), ("postgresql", "PostgreSQL"), ("q3c", "q3c"),
        ("ast", "AST"), ("pgast", "pgast"),
    ]
    # '--component postgresql --build-llvm' links against a private LLVM that
    # must already exist; only a run that includes the llvm component builds it.
    for key, label in labels:
        if key in versions:
            note = ""
            if key == "llvm":
                note = (" (built from source)" if will_build_llvm
                        else " (private toolchain, must already exist)")
            print(f"  {label + ':':<12}{versions[key]}{note}")
    if args.exclude_ast and args.component is None:
        print(f"  {'AST:':<12}(excluded)")
        print(f"  {'pgast:':<12}(excluded)")

    # --build-llvm is a stronger form of --with-llvm: there is no reason to
    # build LLVM and then not link against it.
    if args.build_llvm:
        args.with_llvm = True

    # Build LLVM before resolving the toolchain, so find_llvm_config() below
    # sees the private build rather than whatever the system happens to offer.
    if will_build_llvm:
        build_llvm(versions["llvm"], args.dry_run, args.verbose,
                   no_alias=args.no_alias)

    # Resolve LLVM/JIT support (only when building PostgreSQL)
    building_pg = (args.component is None or args.component == "postgresql")
    use_llvm = False
    llvm_config = None
    if building_pg and args.build_llvm and will_build_llvm:
        # Address the private build by its versioned path. --no-alias skips the
        # /usr/local/llvm symlink, and find_llvm_config() would then miss the
        # LLVM we just built and silently settle for a system one.
        private = private_llvm_config(versions["llvm"])
        if private.is_file() or args.dry_run:
            llvm_config = str(private)
            use_llvm = True
        else:
            print(f"\n  --build-llvm was given but {private} does not exist.",
                  file=sys.stderr)
            print("  Refusing to fall back to a system LLVM, which is the"
                  " dependency --build-llvm exists to avoid.", file=sys.stderr)
            sys.exit(1)
    elif building_pg and args.build_llvm:
        # '--component postgresql --build-llvm' links against whatever private
        # LLVM already exists on disk. It must not require versions["llvm"] --
        # the network-detected *latest* upstream release -- to be the one
        # installed: this run never builds LLVM, so once upstream ships a
        # newer release than what was actually built, that exact version
        # requirement would fail every time despite a perfectly good private
        # build being on disk.
        discovered = find_existing_private_llvm_config()
        if discovered:
            llvm_config = discovered
            use_llvm = True
        else:
            print(f"\n  --component postgresql --build-llvm requires an existing"
                  f" private LLVM under {INSTALL_BASE}, but none was found.",
                  file=sys.stderr)
            print("  Build one first with: pginstall.py --component llvm --build-llvm",
                  file=sys.stderr)
            if not args.dry_run:
                sys.exit(1)
    elif building_pg:
        llvm_config = find_llvm_config()
        if plat == "darwin":
            # macOS: opt-in only via --with-llvm flag
            if args.with_llvm:
                if llvm_config:
                    use_llvm = True
                else:
                    print("\n  --with-llvm specified but LLVM was not found.", file=sys.stderr)
                    print("  Build one with --build-llvm, or install LLVM yourself.",
                          file=sys.stderr)
                    if not args.dry_run:
                        sys.exit(1)
        else:
            # Linux: auto-detect, prompt if not found
            if llvm_config:
                use_llvm = True
            elif args.with_llvm:
                # Explicitly requested but not found
                install_cmd = get_llvm_install_command()
                if install_cmd:
                    print(f"\n  --with-llvm specified but LLVM development packages are not installed.")
                    print(f"  Run the following command, then re-run this script:")
                    print(f"    {install_cmd}")
                else:
                    print(f"\n  --with-llvm specified but LLVM not found and package manager not detected.",
                          file=sys.stderr)
                if not args.dry_run:
                    sys.exit(1)
            else:
                # Not found, not explicitly requested — prompt on Linux
                install_cmd = get_llvm_install_command()
                if install_cmd:
                    if not args.dry_run:
                        want_llvm = prompt_yes_no(
                            "\n  LLVM not found. Install LLVM development packages for JIT support?",
                            default=True,
                        )
                        if want_llvm:
                            print(f"\n  Run the following command, then re-run this script:")
                            print(f"    {install_cmd}")
                            sys.exit(0)
                        # User said no, continue without LLVM
                    else:
                        print(f"  LLVM not found. To install: {install_cmd}")

    # Build components
    na = args.no_alias
    if args.component:
        # Build only specified component
        if args.component == "readline":
            if plat == "darwin":
                build_readline(versions["readline"], args.dry_run, args.verbose, no_alias=na)
            else:
                print("readline is only built from source on macOS")
        elif args.component == "openssl":
            build_openssl(versions["openssl"], args.dry_run, args.verbose, no_alias=na)
        elif args.component == "icu":
            build_icu(versions["icu"], args.dry_run, args.verbose, no_alias=na)
        elif args.component == "llvm":
            pass  # Validated at startup; already built above.
        elif args.component == "postgresql":
            build_postgresql(versions["postgresql"], args.dry_run, args.verbose,
                             with_llvm=use_llvm, no_alias=na,
                             llvm_config=llvm_config if use_llvm else None)
        elif args.component == "contrib":
            build_contrib_extensions(versions["postgresql"], args.dry_run, args.verbose)
        elif args.component == "q3c":
            build_q3c(versions["q3c"], args.dry_run, args.verbose)
        elif args.component == "ast":
            if args.exclude_ast:
                print("AST is excluded (--exclude-ast)")
            else:
                build_ast(versions["ast"], args.dry_run, args.verbose, no_alias=na)
        elif args.component == "pgast":
            if args.exclude_ast:
                print("pgast is excluded (--exclude-ast)")
            else:
                build_pgast(versions["pgast"], args.dry_run, args.verbose)
    else:
        # Build everything in order
        if plat == "darwin":
            build_readline(versions["readline"], args.dry_run, args.verbose, no_alias=na)

        build_openssl(versions["openssl"], args.dry_run, args.verbose, no_alias=na)
        build_icu(versions["icu"], args.dry_run, args.verbose, no_alias=na)
        build_postgresql(versions["postgresql"], args.dry_run, args.verbose,
                             with_llvm=use_llvm, no_alias=na,
                             llvm_config=llvm_config if use_llvm else None)
        build_contrib_extensions(versions["postgresql"], args.dry_run, args.verbose)

        if not args.skip_extensions:
            build_q3c(versions["q3c"], args.dry_run, args.verbose)
            if not args.exclude_ast:
                build_ast(versions["ast"], args.dry_run, args.verbose, no_alias=na)
                build_pgast(versions["pgast"], args.dry_run, args.verbose)

    print(f"\n{'=' * 60}")
    print("Installation complete!")
    print(f"{'=' * 60}")
    if building_pg:
        print(f"\nPostgreSQL is available at: {INSTALL_BASE / 'postgresql'}")
        print(f"Add to your PATH: export PATH={INSTALL_BASE / 'postgresql' / 'bin'}:$PATH")

    if building_pg and use_llvm:
        # The versioned path, not the symlink: --no-alias may have left the
        # symlink pointing at a different installation than the one just built.
        pg_config = (
            INSTALL_BASE / f"postgresql-{versions['postgresql']}" / "bin" / "pg_config"
        )
        # A private LLVM lives under INSTALL_BASE; a system one does not.
        private_llvm = bool(llvm_config) and str(llvm_config).startswith(
            str(INSTALL_BASE))
        offer_jit_protection(args.dry_run, pg_config, private_llvm=private_llvm)


if __name__ == "__main__":
    main()
