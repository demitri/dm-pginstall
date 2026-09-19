#!/usr/bin/env python3
"""
PostgreSQL JIT Dependency Guard

A source-built PostgreSQL configured with --with-llvm produces llvmjit.so, which
links against a specific versioned LLVM runtime (e.g. libLLVM.so.20.1). The
package manager has no record of that dependency, so a routine system upgrade
that retires the old LLVM will silently break JIT: the module is loaded lazily,
so the server starts cleanly and only queries above jit_above_cost fail.

This tool closes that gap. It reads llvmjit.so's own DT_NEEDED entries, asks
dpkg which packages own them, and generates a small .deb whose Depends are those
packages. apt then models the dependency properly: removal is refused, an
upgrade that would break it warns first, autoremove can never reap the runtime,
and security updates still apply normally.

The dependency set is derived from the binary, never hand-written. Rebuilt
PostgreSQL against a newer LLVM? Re-run "protect". The new runtime is protected
and the old one is released, so "apt autoremove" reclaims it.

The stronger option is not to depend on the system LLVM at all: "pginstall.py
--build-llvm" builds a private LLVM under /usr/local and links PostgreSQL
against it, putting the runtime outside apt's reach entirely. This tool then
reports that there is nothing left to protect.

Scope: --pg-config names which installation to inspect or check, defaulting to
/usr/local/postgresql. The generated package has a fixed name and is therefore
system-wide, while PostgreSQL installations are per-version: "protect" declares
the union of what every at-risk installation needs, so protecting one does not
unprotect another. An installation that cannot be inspected is reported and
treated as still needing protection.

Usage:
    pgjitguard.py                    # Show JIT dependency status
    pgjitguard.py status             # Show JIT dependency status
    pgjitguard.py check              # Exit non-zero if JIT is broken
    pgjitguard.py protect            # Declare the dependency to apt
    pgjitguard.py unprotect          # Remove the protection
    pgjitguard.py install-hook       # Run "check" after every apt transaction
    pgjitguard.py uninstall-hook     # Remove the apt hook

Options:
    --pg-config PATH   Use a specific pg_config (default: /usr/local/postgresql/bin,
                       then PATH)
    --live             "check" also runs a query that forces JIT compilation
    --hook             "check" warns loudly but always exits 0 (for apt)
    --quiet            Suppress output when everything is fine
    --dry-run          Show what would be done without executing
"""

import argparse
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Constants - match the project conventions
INSTALL_BASE = Path("/usr/local")
PG_BASE = INSTALL_BASE / "postgresql"
PG_BIN = PG_BASE / "bin"

# Generated dependency package
PIN_PACKAGE = "postgresql-jit-llvm-pin"
PIN_MANIFEST = Path("/usr/share/pgjitguard/pin-manifest.txt")

# apt integration
APT_HOOK_FILE = Path("/etc/apt/apt.conf.d/99-pgjitguard")
INSTALLED_SCRIPT = Path("/usr/local/sbin/pgjitguard")

# Exit codes
EXIT_OK = 0
EXIT_BROKEN = 1
EXIT_ERROR = 2




def run(cmd: list[str], check: bool = True) -> tuple[int, str, str]:
    """Run a command, returning (returncode, stdout, stderr)."""
    result = subprocess.run(cmd, capture_output=True, text=True)
    if check and result.returncode != 0:
        print(f"Error: command failed: {' '.join(cmd)}", file=sys.stderr)
        if result.stderr.strip():
            print(f"  {result.stderr.strip()}", file=sys.stderr)
        sys.exit(EXIT_ERROR)
    return result.returncode, result.stdout, result.stderr


def require_tool(name: str, provided_by: str) -> str:
    """Return the path to a required tool, or exit explaining what provides it."""
    path = shutil.which(name)
    if not path:
        print(f"Error: '{name}' is required but not installed.", file=sys.stderr)
        print(f"  It is provided by the '{provided_by}' package.", file=sys.stderr)
        sys.exit(EXIT_ERROR)
    return path


def require_linux() -> None:
    """Exit unless running on Linux."""
    if platform.system().lower() != "linux":
        print("Error: pgjitguard inspects ELF shared libraries and is Linux-only.",
              file=sys.stderr)
        print("  On macOS the JIT module links against a Homebrew or system LLVM;",
              file=sys.stderr)
        print("  there is no package manager hook to install here.", file=sys.stderr)
        sys.exit(EXIT_ERROR)


def require_dpkg() -> None:
    """Exit unless this is a dpkg-based system."""
    if not shutil.which("dpkg"):
        print("Error: this command requires a dpkg-based system (Debian/Ubuntu).",
              file=sys.stderr)
        print("  On rpm-based systems the equivalents are a meta-package with the",
              file=sys.stderr)
        print("  same Requires, or 'dnf versionlock' on the LLVM runtime.",
              file=sys.stderr)
        sys.exit(EXIT_ERROR)


def require_root(action: str) -> None:
    """Exit unless running as root."""
    if os.geteuid() != 0:
        print(f"Error: {action} requires root.", file=sys.stderr)
        print(f"  Re-run with: sudo {' '.join(sys.argv)}", file=sys.stderr)
        sys.exit(EXIT_ERROR)


def find_pg_config(explicit: Optional[str] = None) -> Path:
    """Locate pg_config, preferring an explicit path then the project convention."""
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            print(f"Error: pg_config not found at {path}", file=sys.stderr)
            sys.exit(EXIT_ERROR)
        return path.resolve()

    candidate = PG_BIN / "pg_config"
    if candidate.is_file():
        return candidate.resolve()

    found = shutil.which("pg_config")
    if found:
        return Path(found).resolve()

    print("Error: pg_config not found.", file=sys.stderr)
    print(f"  Looked in {PG_BIN} and on PATH. Use --pg-config to name it.",
          file=sys.stderr)
    sys.exit(EXIT_ERROR)


def get_pg_setting(pg_config: Path, flag: str) -> str:
    """Query a single pg_config setting."""
    _, out, _ = run([str(pg_config), flag])
    return out.strip()


def find_llvmjit(pg_config: Path) -> Optional[Path]:
    """Return the path to llvmjit.so, or None if PostgreSQL was built without JIT."""
    pkglibdir = Path(get_pg_setting(pg_config, "--pkglibdir"))
    module = pkglibdir / "llvmjit.so"
    return module if module.is_file() else None


def direct_needed(module: Path) -> list[str]:
    """Return the DT_NEEDED sonames recorded directly in a shared object."""
    readelf = require_tool("readelf", "binutils")
    _, out, _ = run([readelf, "-d", str(module)])
    # Lines look like:  0x0001 (NEEDED)  Shared library: [libLLVM.so.20.1]
    return re.findall(r"\(NEEDED\)\s+Shared library:\s+\[([^\]]+)\]", out)


def resolve_deps(module: Path) -> dict[str, Optional[str]]:
    """Map every soname ldd reports for a module to its resolved path, or None."""
    ldd = require_tool("ldd", "libc-bin")
    returncode, out, err = run([ldd, str(module)], check=False)
    if returncode != 0 and not out.strip():
        print(f"Error: ldd could not inspect {module}", file=sys.stderr)
        if err.strip():
            print(f"  {err.strip()}", file=sys.stderr)
        sys.exit(EXIT_ERROR)

    resolved: dict[str, Optional[str]] = {}
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        if "=>" in line:
            soname, _, target = line.partition("=>")
            soname = soname.strip()
            target = target.strip()
            if target.startswith("not found"):
                resolved[soname] = None
            else:
                # "/path/to/lib.so (0x00007f...)" or bare "(0x00007f...)"
                path = target.split(" (")[0].strip()
                resolved[soname] = path or None
        else:
            # vdso / loader lines: "linux-vdso.so.1 (0x00007ffc...)"
            soname = line.split(" (")[0].strip()
            if soname.startswith("/"):
                resolved[Path(soname).name] = soname
    return resolved


def dpkg_owner(path: str) -> Optional[str]:
    """Return the package owning a file, or None if dpkg does not track it."""
    if not shutil.which("dpkg"):
        return None
    real = str(Path(path).resolve())
    for candidate in (real, path):
        returncode, out, _ = run(["dpkg", "-S", candidate], check=False)
        if returncode == 0 and out.strip():
            # "libllvm21:amd64: /usr/lib/x86_64-linux-gnu/libLLVM.so.21.1"
            package = out.splitlines()[0].split(":")[0].strip()
            if package:
                return package
    return None


class JitState:
    """The resolved JIT dependency picture for one PostgreSQL installation."""

    def __init__(self, pg_config: Path):
        self.pg_config = pg_config
        self.pg_version = get_pg_setting(pg_config, "--version")
        self.module = find_llvmjit(pg_config)
        self.needed: list[str] = []
        self.resolved: dict[str, Optional[str]] = {}
        self.missing: list[str] = []
        self.packages: list[str] = []
        self.llvm_packages: list[str] = []
        self.untracked: list[str] = []

        if not self.module:
            return

        self.needed = direct_needed(self.module)
        self.resolved = resolve_deps(self.module)

        for soname in self.needed:
            path = self.resolved.get(soname)
            if path is None:
                self.missing.append(soname)
                continue
            owner = dpkg_owner(path)
            if owner:
                if owner not in self.packages:
                    self.packages.append(owner)
                # The LLVM runtime is the only dependency here that is actually
                # at risk: its package name carries the version, so a successor
                # arrives as a separate package and the old one is retired.
                # libc6 and friends upgrade in place and are never withdrawn.
                if "LLVM" in soname and owner not in self.llvm_packages:
                    self.llvm_packages.append(owner)
            else:
                self.untracked.append(f"{soname} -> {path}")

        self.packages.sort()
        self.llvm_packages.sort()

    @property
    def has_jit(self) -> bool:
        return self.module is not None

    @property
    def is_broken(self) -> bool:
        return bool(self.missing)

    @property
    def is_at_risk(self) -> bool:
        """True if an apt operation could remove the LLVM this module needs.

        False when no dpkg package owns the LLVM runtime -- the case after
        'pginstall.py --build-llvm', where LLVM lives under /usr/local and apt
        has no say over it. That is the desired end state, not a failure.
        """
        return bool(self.llvm_packages)

    @property
    def llvm_provider(self) -> str:
        """Where the LLVM runtime actually comes from, for reporting."""
        paths = [self.resolved.get(s) for s in self.llvm_sonames]
        paths = [p for p in paths if p]
        return ", ".join(paths) if paths else "unknown"

    @property
    def llvm_sonames(self) -> list[str]:
        return [s for s in self.needed if "LLVM" in s]


# --------------------------------------------------------------------------
# The generated package that declares the dependency
# --------------------------------------------------------------------------


def installed_pin_version() -> Optional[str]:
    """Return the installed dependency package's version, or None."""
    if not shutil.which("dpkg-query"):
        return None
    returncode, out, _ = run(
        ["dpkg-query", "-W", "-f=${Status}|${Version}", PIN_PACKAGE], check=False
    )
    if returncode != 0 or "|" not in out:
        return None
    status, _, version = out.partition("|")
    if "install ok installed" not in status:
        return None
    return version.strip() or None


def installed_pin_depends() -> list[str]:
    """Return the package names the installed pin currently depends on.

    Read from dpkg rather than from our own manifest: the manifest describes
    what we last wrote, while this describes what apt is actually enforcing.
    """
    if not shutil.which("dpkg-query"):
        return []
    returncode, out, _ = run(
        ["dpkg-query", "-W", f"-f=${{Depends}}", PIN_PACKAGE], check=False
    )
    if returncode != 0 or not out.strip():
        return []
    packages = []
    for entry in out.split(","):
        # "libllvm21 (>= 1:21~)" or "libc6 | libc6-udeb" -> bare package name
        name = entry.split("|")[0].strip().split(" ")[0].strip()
        if name:
            packages.append(name)
    return packages


def next_pin_version() -> str:
    """Return a version that always sorts above the installed one.

    Upgrading in place rather than removing and reinstalling keeps the
    protection atomic: there is never a window in which the LLVM runtime sits
    unprotected and an autoremove could take it.
    """
    current = installed_pin_version()
    if current:
        match = re.match(r"^1\.(\d+)$", current)
        if match:
            return f"1.{int(match.group(1)) + 1}"
    return "1.1"


def build_pin_package(state: JitState, version: str, workdir: Path,
                      packages: list[str],
                      contributors: Optional[list[tuple[Path, list[str]]]] = None,
                      uninspectable: Optional[list[Path]] = None) -> Path:
    """Build the dependency .deb from the module's real dependencies."""
    dpkg_deb = require_tool("dpkg-deb", "dpkg")

    root = workdir / "pkgroot"
    debian = root / "DEBIAN"
    debian.mkdir(parents=True)

    share = root / PIN_MANIFEST.parent.relative_to("/")
    share.mkdir(parents=True)

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    manifest = [
        "# Generated by pgjitguard.py -- do not edit.",
        f"# Regenerate with: {remediation_command(state)}",
        f"generated:   {stamp}",
        f"pg_config:   {state.pg_config}",
        f"postgresql:  {state.pg_version}",
        f"module:      {state.module}",
        f"depends:     {', '.join(packages)}",
        "",
        "# Installations this package protects. Its Depends are the union of",
        "# their requirements, since one system-wide package name has to cover",
        "# every side-by-side PostgreSQL version.",
    ]
    for path, packages_for in (contributors or []):
        manifest.append(f"  {path}  ->  {', '.join(packages_for)}")
    for path in (uninspectable or []):
        manifest.append(f"  {path}  ->  (not inspectable; prior Depends kept)")
    manifest += [
        "",
        "# Direct dependencies of llvmjit.so in the installation that was named:",
    ]
    for soname in state.needed:
        path = state.resolved.get(soname) or "NOT FOUND"
        owner = dpkg_owner(path) if path != "NOT FOUND" else None
        manifest.append(f"  {soname} -> {path}" + (f"  [{owner}]" if owner else ""))
    (share / PIN_MANIFEST.name).write_text("\n".join(manifest) + "\n")

    llvm = ", ".join(state.llvm_sonames) or "none"
    control = f"""Package: {PIN_PACKAGE}
Version: {version}
Architecture: all
Maintainer: pgjitguard <root@localhost>
Section: misc
Priority: optional
Depends: {', '.join(packages)}
Description: Pin runtime libraries needed by a source-built PostgreSQL JIT
 PostgreSQL built from source with --with-llvm links its JIT module against a
 specific versioned LLVM runtime ({llvm}). dpkg has no
 record of that dependency, so an upgrade can retire the runtime and break JIT
 for every query above jit_above_cost while the server still starts cleanly.
 .
 This package exists only to declare that dependency, so apt refuses to remove
 the runtime and warns before an upgrade would. It was generated from the
 dependencies of {state.module}
 and is regenerated by "pgjitguard protect" after PostgreSQL is rebuilt.
"""
    (debian / "control").write_text(control)

    deb = workdir / f"{PIN_PACKAGE}_{version}_all.deb"
    run([dpkg_deb, "--root-owner-group", "--build", str(root), str(deb)])
    return deb


def apply_depends(state: JitState, dry_run: bool, packages: list[str],
                  contributors: Optional[list[tuple[Path, list[str]]]] = None,
                  uninspectable: Optional[list[Path]] = None) -> int:
    """Install a generated package declaring the JIT dependencies to protect."""
    version = next_pin_version()
    current = installed_pin_version()

    print(f"Package:     {PIN_PACKAGE} {version}"
          + (f", replacing {current}" if current else ""))

    if dry_run:
        print("\n[dry-run] Would build and install the package above.")
        return EXIT_OK

    require_root("protect")

    with tempfile.TemporaryDirectory(prefix="pgjitguard-") as tmp:
        deb = build_pin_package(state, version, Path(tmp), packages,
                                contributors, uninspectable)
        print(f"\nInstalling {deb.name} ...")
        run(["dpkg", "-i", str(deb)])

    print(f"\nProtected. apt will refuse to remove: {', '.join(packages)}")
    if current:
        print("\nThe previous dependency set has been replaced. Any LLVM runtime it")
        print("protected that nothing else needs is now reclaimable:")
        print("  sudo apt autoremove")
    return EXIT_OK


def remove_depends(dry_run: bool) -> int:
    """Remove the generated dependency package."""
    version = installed_pin_version()
    if not version:
        print(f"  {PIN_PACKAGE} is not installed.")
        return EXIT_OK

    if dry_run:
        print(f"  [dry-run] Would remove {PIN_PACKAGE} {version}.")
        return EXIT_OK

    require_root("unprotect")
    run(["dpkg", "--remove", PIN_PACKAGE])
    print(f"  Removed {PIN_PACKAGE} {version}.")
    return EXIT_OK















# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def at_risk_installations(current: JitState) -> tuple[
        list[tuple[Path, list[str]]], list[Path]]:
    """Every PostgreSQL installation whose JIT needs a dpkg-owned LLVM.

    Includes `current` when it is itself at risk. The pin package has a fixed
    name and is therefore system-wide, while installations are per-version and
    this repository supports them side by side. Declaring only the installation
    being protected would silently drop the packages a sibling depends on --
    and, on the cleanup path, would keep a pin that does not actually cover the
    sibling it is being kept for.

    Returns (contributors, uninspectable). Anything that cannot be inspected is
    reported rather than skipped: guessing permissively unprotects a working
    build, which is the failure this tool exists to prevent.
    """
    contributors: list[tuple[Path, list[str]]] = []
    uninspectable: list[Path] = []

    if current.has_jit and current.is_at_risk:
        contributors.append((current.pg_config, current.packages))

    for pg_config in sorted(INSTALL_BASE.glob("postgresql-*/bin/pg_config")):
        if pg_config.resolve() == current.pg_config.resolve():
            continue
        try:
            sibling = JitState(pg_config)
        except SystemExit:
            # JitState exits on an unreadable module; not fatal here, but not
            # ignorable either.
            uninspectable.append(pg_config)
            continue
        if sibling.has_jit and sibling.is_at_risk:
            contributors.append((pg_config, sibling.packages))

    return contributors, uninspectable


def union_pin_packages(current: JitState) -> tuple[
        list[str], list[tuple[Path, list[str]]], list[Path]]:
    """The package set one system-wide pin must declare to cover every install.

    When an installation cannot be inspected, whatever the existing pin already
    declares is carried forward. Replacing the pin with only the packages we
    could derive would drop the dependencies protecting that installation --
    the exact silent unprotection this tool exists to prevent, and the opposite
    of the conservative treatment uninspectable installs are promised.
    """
    contributors, uninspectable = at_risk_installations(current)
    packages = {pkg for _, pkgs in contributors for pkg in pkgs}
    if uninspectable:
        packages.update(installed_pin_depends())
    return sorted(packages), contributors, uninspectable


def remediation_command(state: JitState, action: str = "protect") -> str:
    """Render the command that fixes things, naming the installation explicitly.

    Every "re-run this" message must carry --pg-config. Telling the user to run
    a bare 'pgjitguard protect' sends them back to the default symlink, which is
    the exact class of bug this tool guards against.

    The path is absolute and the arguments quoted so the suggestion can be
    pasted as-is: the documented invocation is './pgjitguard.py', and 'sudo
    pgjitguard.py' would not resolve, since sudo's PATH excludes the current
    directory.
    """
    return "sudo " + shlex.join(
        [str(Path(sys.argv[0]).resolve()), "--pg-config", str(state.pg_config),
         action]
    )




def protection_gaps(state: JitState) -> tuple[list[str], list[str]]:
    """Compare what is actually enforced against what the module needs now.

    Returns (what_is_protecting, gaps). The mere presence of the pin package is
    NOT protection: after a rebuild against a newer LLVM it still guards the
    *previous* runtime, so the dependency set must be compared, not merely
    found. This is the drift the tool exists to catch.
    """
    protected_by: list[str] = []
    gaps: list[str] = []

    required, _, _ = union_pin_packages(state)
    if not required:
        # Nothing anywhere depends on a dpkg-owned LLVM -- the private-LLVM end
        # state. An installed pin is then redundant rather than protective, so
        # claim neither protection nor a gap; cmd_status reports it as obsolete.
        return protected_by, gaps

    if installed_pin_version():
        depends = installed_pin_depends()
        uncovered = [p for p in required if p not in depends]
        if uncovered:
            gaps.append(f"{PIN_PACKAGE} does not depend on: {', '.join(uncovered)}")
        else:
            protected_by.append("dependency package")

    return protected_by, gaps




def cmd_status(args: argparse.Namespace) -> int:
    """Show the JIT dependency picture and what is protecting it."""
    require_linux()
    pg_config = find_pg_config(args.pg_config)
    state = JitState(pg_config)

    print(f"PostgreSQL:  {state.pg_version}")
    print(f"pg_config:   {pg_config}")

    if not state.has_jit:
        print("JIT module:  not built (PostgreSQL was configured without --with-llvm)")
        print("\nNothing to protect: no llvmjit.so means no hidden LLVM dependency.")
        return EXIT_OK

    print(f"JIT module:  {state.module}")
    print("\nDirect dependencies:")
    for soname in state.needed:
        path = state.resolved.get(soname)
        if path is None:
            print(f"  {soname}  ->  *** NOT FOUND ***")
            continue
        owner = dpkg_owner(path)
        suffix = f"  [{owner}]" if owner else "  [not tracked by dpkg]"
        print(f"  {soname}  ->  {path}{suffix}")

    print("\nProtection:")

    version = installed_pin_version()
    if version:
        print(f"  dependency package: {PIN_PACKAGE} {version} (installed)")
        depends = installed_pin_depends()
        print(f"    depends on:       {', '.join(depends) if depends else '(none)'}")
    else:
        print("  dependency package: not installed")

    print(f"  apt hook:           "
          f"{'installed' if APT_HOOK_FILE.is_file() else 'not installed'}"
          f" ({APT_HOOK_FILE})")

    protected_by, gaps = protection_gaps(state)

    if protected_by:
        print(f"\n  Protected by: {', '.join(protected_by)}")
    if gaps:
        print("\n  *** Protection does not cover what llvmjit.so needs now ***")
        for gap in gaps:
            print(f"    {gap}")
        print("\n  This is what a rebuild against a different LLVM looks like: the")
        print("  old runtime is still protected while the new one is exposed.")
        print(f"  Re-run: {remediation_command(state)}")
    elif not state.is_at_risk:
        print("\n  Nothing to protect: no dpkg package owns the LLVM runtime")
        print(f"  ({state.llvm_provider}), so an apt upgrade cannot remove it.")
        if installed_pin_version():
            required, contributors, uninspectable = union_pin_packages(state)
            if contributors or uninspectable:
                print(f"\n  {PIN_PACKAGE} is installed and still needed by other")
                print("  installations:")
                for path, packages in contributors:
                    print(f"    {path}  ({', '.join(packages)})")
                for path in uninspectable:
                    print(f"    {path}  (could not inspect)")
                missing = [p for p in required if p not in installed_pin_depends()]
                if missing:
                    print(f"\n  ...but it does not cover: {', '.join(missing)}")
                    print(f"  Re-run: {remediation_command(state)}")
            else:
                print(f"\n  {PIN_PACKAGE} is OBSOLETE: it still depends on the LLVM")
                print("  this build no longer uses, which stops apt reclaiming it.")
                print(f"  Remove it with: {remediation_command(state, 'unprotect')}")
    else:
        print("\n  Nothing is protecting these packages. An LLVM upgrade can still")
        print(f"  break JIT. Run: {remediation_command(state)}")

    if state.is_broken:
        print(f"\nJIT is BROKEN: {len(state.missing)} dependency/dependencies unresolved.")
        return EXIT_BROKEN

    print("\nJIT dependencies all resolve.")
    return EXIT_OK


def live_jit_check(args: argparse.Namespace, pg_config: Path) -> Optional[str]:
    """Force a JIT compilation through psql. Returns an error string, or None."""
    # Derive psql from the resolved pg_config's own bindir, not PATH: a caller
    # who names --pg-config to check one installation must not have the live
    # query silently run against a different one that happens to be on PATH.
    psql = pg_config.parent / "psql"
    if not psql.is_file():
        return f"psql not found next to {pg_config}; cannot run the live check"

    # Zeroing the cost thresholds is what forces the provider to load; the
    # default jit_above_cost is exactly why this breakage stays hidden.
    sql = (
        "SET jit = on; "
        "SET jit_above_cost = 0; "
        "SET jit_inline_above_cost = 0; "
        "SET jit_optimize_above_cost = 0; "
        "SELECT count(*) FROM generate_series(1, 1000);"
    )
    # -w: a password prompt would otherwise block on inherited stdin, and this
    # runs from --hook-adjacent, unattended contexts as often as interactively.
    cmd = [str(psql), "-XAtqw", "-v", "ON_ERROR_STOP=1"]
    if args.dbname:
        cmd += ["-d", args.dbname]
    cmd += ["-c", sql]

    returncode, _, err = run(cmd, check=False)
    if returncode != 0:
        return err.strip() or f"psql exited {returncode}"
    return None


def cmd_check(args: argparse.Namespace) -> int:
    """Verify the JIT module can still resolve its dependencies."""
    require_linux()
    pg_config = find_pg_config(args.pg_config)
    state = JitState(pg_config)

    if not state.has_jit:
        if not args.quiet:
            print("OK: PostgreSQL has no JIT module; nothing to check.")
        return EXIT_OK

    problems = [f"unresolved dependency: {soname}" for soname in state.missing]

    if args.live and not problems:
        error = live_jit_check(args, pg_config)
        if error:
            problems.append(f"live JIT query failed: {error}")

    if problems:
        # Loud by design: this is the failure the tool exists to surface, and it
        # is otherwise invisible until an expensive query hits it in production.
        print("", file=sys.stderr)
        print("*** WARNING: PostgreSQL JIT is broken ***", file=sys.stderr)
        print(f"  module: {state.module}", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        print("", file=sys.stderr)
        print("  Queries costing more than jit_above_cost will fail. The server",
              file=sys.stderr)
        print("  itself starts normally, so this will not show up in a health",
              file=sys.stderr)
        print("  check that only connects.", file=sys.stderr)
        print("", file=sys.stderr)
        print("  Fix: rebuild PostgreSQL against the installed LLVM, then re-protect:",
              file=sys.stderr)
        print("    pginstall.py --component postgresql --with-llvm", file=sys.stderr)
        print(f"    {remediation_command(state)}", file=sys.stderr)
        print("", file=sys.stderr)
        # In hook mode a broken JIT must not fail the apt transaction already in
        # progress; the warning above is the deliverable, not the exit code.
        return EXIT_OK if args.hook else EXIT_BROKEN

    # JIT works right now, but protection that guards a superseded runtime will
    # let the next upgrade break it. Warn regardless of --quiet: this is the
    # one window in which the problem is cheap to fix.
    _, gaps = protection_gaps(state)
    if gaps:
        print("", file=sys.stderr)
        print("WARNING: PostgreSQL JIT works, but its protection is out of date:",
              file=sys.stderr)
        for gap in gaps:
            print(f"  {gap}", file=sys.stderr)
        print(f"  Re-run: {remediation_command(state)}", file=sys.stderr)
        print("", file=sys.stderr)

    if not args.quiet:
        checked = "dependencies resolve"
        if args.live:
            checked += " and a JIT-compiled query succeeded"
        print(f"OK: PostgreSQL JIT {checked}.")
    return EXIT_OK


def cmd_protect(args: argparse.Namespace) -> int:
    """Enforce the JIT module's hidden dependency against apt."""
    require_linux()
    require_dpkg()
    pg_config = find_pg_config(args.pg_config)
    state = JitState(pg_config)

    if not state.has_jit:
        print("Error: PostgreSQL has no llvmjit.so; there is no dependency to protect.",
              file=sys.stderr)
        print("  Rebuild with --with-llvm first, or leave JIT disabled.", file=sys.stderr)
        return EXIT_ERROR

    if state.is_broken:
        print("Error: refusing to protect a broken JIT module.", file=sys.stderr)
        for soname in state.missing:
            print(f"  unresolved: {soname}", file=sys.stderr)
        print("\n  Doing so would record the wrong dependency set. Rebuild PostgreSQL",
              file=sys.stderr)
        print("  against the installed LLVM first, then re-run protect.", file=sys.stderr)
        return EXIT_ERROR

    if not state.is_at_risk:
        # Not an error: this is what a private LLVM looks like. apt does not own
        # the runtime, so no apt operation can retire it, and a pin declaring
        # libc6 would protect nothing that was ever in danger.
        print(f"JIT module:  {state.module}")
        print(f"LLVM:        {state.llvm_provider}")
        print("\nNothing to protect: no dpkg package owns the LLVM runtime, so an")
        print("apt upgrade cannot remove it. This is the outcome that")
        print("'pginstall.py --build-llvm' is for.")

        # A pin from before the switch still depends on the old system LLVM,
        # which keeps apt from ever reclaiming it. Leaving it installed would
        # quietly pin a runtime nothing uses any more.
        required, contributors, uninspectable = union_pin_packages(state)

        if contributors:
            # Other installations still need protecting. Rebuild the pin around
            # exactly what they need: keeping the old one is not enough, since
            # it may name a runtime none of them actually uses.
            print("\nOther PostgreSQL installations still link a dpkg-owned LLVM:")
            for path, packages in contributors:
                print(f"  {path}  ({', '.join(packages)})")
            for path in uninspectable:
                print(f"  {path}  (could not inspect; keeping its existing"
                      " dependencies)")
            print("\nRebuilding the pin to cover them:")
            print(f"Depends on:  {', '.join(required)}")
            return apply_depends(state, args.dry_run, required,
                             contributors, uninspectable)

        if uninspectable:
            print("\nLeaving the pin alone: these installations could not be")
            print("inspected, and removing protection they might need is worse")
            print("than keeping a pin that is merely redundant.")
            for path in uninspectable:
                print(f"  {path}")
            return EXIT_OK

        if installed_pin_version():
            print(f"\n{PIN_PACKAGE} is installed and now obsolete: it depends on")
            print("an LLVM no installation uses any more.")
            result = remove_depends(args.dry_run)
            if result != EXIT_OK:
                return result
            if not args.dry_run:
                print("\nThe old LLVM is now reclaimable:\n  sudo apt autoremove")
        return EXIT_OK

    print(f"JIT module:  {state.module}")
    print(f"Depends on:  {', '.join(state.packages)}")
    if state.untracked:
        print("Not protected (no owning package):")
        for entry in state.untracked:
            print(f"  {entry}")

    required, contributors, uninspectable = union_pin_packages(state)
    if len(contributors) > 1:
        # One system-wide package name, several installations: it must declare
        # all of them or protecting this one would unprotect the others.
        print("\nAlso covering other installations that need a system LLVM:")
        for path, packages in contributors:
            if path.resolve() != state.pg_config.resolve():
                print(f"  {path}  ({', '.join(packages)})")
        print(f"Combined:    {', '.join(required)}")
    for path in uninspectable:
        print(f"  WARNING: could not inspect {path}; carrying its existing"
              " pin dependencies forward unchanged")

    print()
    return apply_depends(state, args.dry_run, required,
                             contributors, uninspectable)


def cmd_unprotect(args: argparse.Namespace) -> int:
    """Remove protection, returning the runtime to normal apt policy."""
    require_linux()
    require_dpkg()

    print("Removing protection:")
    remove_depends(args.dry_run)

    if not args.dry_run:
        print("\nThe LLVM runtime is no longer protected. It becomes autoremovable")
        print("once nothing else depends on it, which will break JIT silently.")
    return EXIT_OK


APT_HOOK_TEMPLATE = """// Installed by pgjitguard.py -- do not edit.
//
// PostgreSQL built from source with --with-llvm loads llvmjit.so lazily, so a
// removed or replaced LLVM runtime does not surface until a query crosses
// jit_above_cost. This runs the dependency check after every apt/dpkg
// transaction, which is the only event that can cause the breakage.
//
// The check warns but never fails, so it cannot wedge an apt run in progress.
// --pg-config and --quiet are top-level options and must precede the
// subcommand; argparse rejects them after it.
DPkg::Post-Invoke {{ "{script} --pg-config '{pg_config}' --quiet check --hook || true"; }};
"""


def cmd_install_hook(args: argparse.Namespace) -> int:
    """Install an apt hook that runs the check after every transaction."""
    require_linux()
    require_dpkg()

    source = Path(__file__).resolve()
    # Resolve pg_config now and bake it into the hook. Left implicit, the hook
    # would re-resolve at apt time against /usr/local/postgresql or root's PATH,
    # which need not be the installation the caller asked about.
    pg_config = find_pg_config(args.pg_config)
    if "'" in str(pg_config) or '"' in str(pg_config):
        print(f"Error: refusing to embed a quoted path in apt.conf: {pg_config}",
              file=sys.stderr)
        print("  Move the installation somewhere without quotes in its path.",
              file=sys.stderr)
        return EXIT_ERROR

    hook = APT_HOOK_TEMPLATE.format(script=INSTALLED_SCRIPT, pg_config=pg_config)

    print(f"Script:    {source}  ->  {INSTALLED_SCRIPT}")
    print(f"pg_config: {pg_config}")
    print(f"apt hook:  {APT_HOOK_FILE}")
    print()
    print(hook)

    if args.dry_run:
        print("[dry-run] Would copy the script and write the hook above.")
        return EXIT_OK

    require_root("install-hook")

    # Copy rather than pointing the hook at the repo: an apt hook referencing a
    # path that can be moved or deleted turns every future apt run into noise.
    INSTALLED_SCRIPT.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, INSTALLED_SCRIPT)
    INSTALLED_SCRIPT.chmod(0o755)

    APT_HOOK_FILE.write_text(hook)
    APT_HOOK_FILE.chmod(0o644)

    print("Installed. Every apt transaction now checks the JIT module.")
    return EXIT_OK


def cmd_uninstall_hook(args: argparse.Namespace) -> int:
    """Remove the apt hook and the copied script."""
    require_linux()

    present = [p for p in (APT_HOOK_FILE, INSTALLED_SCRIPT) if p.is_file()]
    if not present:
        print("No apt hook or installed script found; nothing to remove.")
        return EXIT_OK

    for path in present:
        print(f"  {'[dry-run] would remove' if args.dry_run else 'removing'}: {path}")

    if args.dry_run:
        return EXIT_OK

    require_root("uninstall-hook")
    for path in present:
        path.unlink()

    print("\nRemoved. apt transactions no longer check the JIT module.")
    print("Any protection applied by 'protect' is unaffected; remove it with")
    # Absolute path: a bare basename would not resolve under sudo, whose PATH
    # excludes the current directory (see remediation_command()).
    print(f"'sudo {Path(sys.argv[0]).resolve()} unprotect' if that is what you want.")
    return EXIT_OK


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pgjitguard.py",
        description="Protect a source-built PostgreSQL JIT module from LLVM upgrades.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                          # what does llvmjit.so need, and is it safe?
  %(prog)s check --live             # verify by running a JIT-compiled query
  sudo %(prog)s protect             # declare the dependency to apt
  sudo %(prog)s install-hook        # check automatically after every apt run

After rebuilding PostgreSQL against a newer LLVM, re-run "sudo %(prog)s protect".
It re-derives the dependency set from the rebuilt module, protects the new
runtime, and releases the old one for "apt autoremove" to reclaim.
""",
    )
    parser.add_argument("--pg-config", metavar="PATH",
                        help="Path to pg_config (default: /usr/local/postgresql/bin, then PATH)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be done without executing")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress output when everything is fine")

    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("status", help="Show JIT dependency status (default)")

    check = subparsers.add_parser("check", help="Exit non-zero if JIT is broken")
    check.add_argument("--live", action="store_true",
                       help="Also run a query that forces JIT compilation")
    check.add_argument("--dbname", "-d", metavar="DB",
                       help="Database to use for --live (default: psql's default)")
    check.add_argument("--hook", action="store_true",
                       help="Warn loudly but always exit 0 (for use in the apt hook)")

    subparsers.add_parser("protect", help="Declare the dependency to apt")
    subparsers.add_parser("unprotect", help="Remove the protection")

    subparsers.add_parser("install-hook",
                          help="Run the check after every apt transaction")
    subparsers.add_parser("uninstall-hook",
                          help="Remove the apt hook and the copied script")

    args = parser.parse_args()
    if not args.command:
        args.command = "status"
    # Defaults for options that exist only on some subparsers.
    for option in ("live", "dbname", "hook"):
        if not hasattr(args, option):
            setattr(args, option, None)
    return args


def main() -> int:
    args = parse_args()
    handlers = {
        "status": cmd_status,
        "check": cmd_check,
        "protect": cmd_protect,
        "unprotect": cmd_unprotect,
        "install-hook": cmd_install_hook,
        "uninstall-hook": cmd_uninstall_hook,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
