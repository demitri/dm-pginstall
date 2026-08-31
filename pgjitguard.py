#!/usr/bin/env python3
"""
PostgreSQL JIT Dependency Guard

A source-built PostgreSQL configured with --with-llvm produces llvmjit.so, which
links against a specific versioned LLVM runtime (e.g. libLLVM.so.20.1). The
package manager has no record of that dependency, so a routine system upgrade
that retires the old LLVM will silently break JIT: the module is loaded lazily,
so the server starts cleanly and only queries above jit_above_cost fail.

This tool closes that gap. It reads llvmjit.so's own DT_NEEDED entries, asks
dpkg which packages own them, and then enforces that dependency by one of two
methods:

  depends  Generate a small .deb whose Depends are those packages. apt models
           the dependency properly: removal is refused, upgrades warn, and
           autoremove can never reap the runtime.
  hold     Mark those packages manual and held with apt-mark. Nothing is
           generated, but a hold also blocks their security updates.

Either way the dependency set is derived from the binary, never hand-written.
Rebuilt PostgreSQL against a newer LLVM? Re-run "protect". The new runtime is
protected and the old one is released, so "apt autoremove" reclaims it.

Scope: this guards one installation at a time -- the one --pg-config names,
defaulting to /usr/local/postgresql. The generated package and the manifests use
fixed names, so protecting a second installation replaces the first rather than
adding to it. With side-by-side PostgreSQL versions, protect the one whose JIT
you rely on, or build them against the same LLVM.

Usage:
    pgjitguard.py                    # Show JIT dependency status
    pgjitguard.py status             # Show JIT dependency status
    pgjitguard.py check              # Exit non-zero if JIT is broken
    pgjitguard.py protect            # Enforce the dependency (asks how)
    pgjitguard.py unprotect          # Remove the protection
    pgjitguard.py install-hook       # Run "check" after every apt transaction
    pgjitguard.py uninstall-hook     # Remove the apt hook

Options:
    --pg-config PATH   Use a specific pg_config (default: search PATH)
    --method NAME      depends | hold (default: ask)
    --live             "check" also runs a query that forces JIT compilation
    --hook             "check" warns loudly but always exits 0 (for apt)
    --quiet            Suppress output when everything is fine
    --dry-run          Show what would be done without executing
"""

import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

# Constants - match the project conventions
PG_BASE = Path("/usr/local/postgresql")
PG_BIN = PG_BASE / "bin"

# Generated dependency package (Method.DEPENDS)
PIN_PACKAGE = "postgresql-jit-llvm-pin"
PIN_MANIFEST = Path("/usr/share/pgjitguard/pin-manifest.txt")

# Record of what we told apt-mark to hold (Method.HOLD)
HOLD_MANIFEST = Path("/var/lib/pgjitguard/held-packages.txt")

# apt integration
APT_HOOK_FILE = Path("/etc/apt/apt.conf.d/99-pgjitguard")
INSTALLED_SCRIPT = Path("/usr/local/sbin/pgjitguard")

# Exit codes
EXIT_OK = 0
EXIT_BROKEN = 1
EXIT_ERROR = 2


class Method(Enum):
    """How the JIT module's hidden dependency is enforced against apt."""

    DEPENDS = "depends"  # generate a .deb that Depends on the runtime packages
    HOLD = "hold"        # apt-mark manual + hold on the runtime packages

    def __str__(self) -> str:
        return self.value


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
            else:
                self.untracked.append(f"{soname} -> {path}")

        self.packages.sort()

    @property
    def has_jit(self) -> bool:
        return self.module is not None

    @property
    def is_broken(self) -> bool:
        return bool(self.missing)

    @property
    def llvm_sonames(self) -> list[str]:
        return [s for s in self.needed if "LLVM" in s]


# --------------------------------------------------------------------------
# Method.DEPENDS - a generated package that declares the dependency
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


def build_pin_package(state: JitState, version: str, workdir: Path) -> Path:
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
        "# Regenerate with: sudo pgjitguard protect --method depends",
        f"generated:   {stamp}",
        f"pg_config:   {state.pg_config}",
        f"postgresql:  {state.pg_version}",
        f"module:      {state.module}",
        f"depends:     {', '.join(state.packages)}",
        "",
        "# Direct dependencies of llvmjit.so at the time of pinning:",
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
Depends: {', '.join(state.packages)}
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


def apply_depends(state: JitState, dry_run: bool) -> int:
    """Install a generated package declaring the JIT module's dependencies."""
    version = next_pin_version()
    current = installed_pin_version()

    print(f"Method:      dependency package ({PIN_PACKAGE} {version})"
          + (f", replacing {current}" if current else ""))

    if dry_run:
        print("\n[dry-run] Would build and install the package above.")
        return EXIT_OK

    require_root("protect --method depends")

    with tempfile.TemporaryDirectory(prefix="pgjitguard-") as tmp:
        deb = build_pin_package(state, version, Path(tmp))
        print(f"\nInstalling {deb.name} ...")
        run(["dpkg", "-i", str(deb)])

    print(f"\nProtected. apt will refuse to remove: {', '.join(state.packages)}")
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
# Method.HOLD - apt-mark manual + hold on the runtime packages
# --------------------------------------------------------------------------


def read_hold_manifest() -> dict[str, bool]:
    """Return {package: was_auto_installed_before_we_touched_it}.

    The auto/manual flag is recorded so releasing a hold can restore apt's
    original state. Marking a package manual without remembering that it used
    to be automatic would leave it permanently un-autoremovable.
    """
    if not HOLD_MANIFEST.is_file():
        return {}
    recorded: dict[str, bool] = {}
    for line in HOLD_MANIFEST.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, flag = line.partition("\t")
        recorded[name.strip()] = flag.strip() == "auto"
    return recorded


def write_hold_manifest(was_auto: dict[str, bool], state: JitState) -> None:
    """Record which packages we hold and how apt had them marked before."""
    HOLD_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        "# Generated by pgjitguard.py -- do not edit.",
        "# Packages held on behalf of the PostgreSQL JIT module.",
        "# Each line is: <package>\\t<auto|manual>, recording how apt had the",
        "# package marked before pgjitguard marked it manual.",
        f"# generated: {stamp}",
        f"# module:    {state.module}",
        "",
    ]
    for package in sorted(was_auto):
        lines.append(f"{package}\t{'auto' if was_auto[package] else 'manual'}")
    HOLD_MANIFEST.write_text("\n".join(lines) + "\n")


def currently_held() -> list[str]:
    """Return every package apt currently has on hold."""
    if not shutil.which("apt-mark"):
        return []
    returncode, out, _ = run(["apt-mark", "showhold"], check=False)
    if returncode != 0:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def currently_auto() -> set[str]:
    """Return every package apt considers automatically installed."""
    if not shutil.which("apt-mark"):
        return set()
    returncode, out, _ = run(["apt-mark", "showauto"], check=False)
    if returncode != 0:
        return set()
    return {line.strip() for line in out.splitlines() if line.strip()}


def release_hold_packages(packages: dict[str, bool]) -> None:
    """Unhold packages and restore the auto/manual marking apt had before."""
    if not packages:
        return
    run(["apt-mark", "unhold"] + sorted(packages))
    # Restoring 'auto' is what actually lets apt autoremove reclaim a runtime we
    # no longer need; unholding alone would leave it manual and pinned forever.
    restore_auto = sorted(name for name, was_auto in packages.items() if was_auto)
    if restore_auto:
        run(["apt-mark", "auto"] + restore_auto)


def apply_hold(state: JitState, dry_run: bool) -> int:
    """Mark the runtime packages manual and held."""
    require_tool("apt-mark", "apt")

    previous = read_hold_manifest()
    stale = {p: was_auto for p, was_auto in previous.items()
             if p not in state.packages}

    print("Method:      apt-mark hold")
    print(f"  hold:      {', '.join(state.packages)}")
    if stale:
        print(f"  release:   {', '.join(sorted(stale))}  (no longer needed by llvmjit.so)")

    if dry_run:
        print("\n[dry-run] Would apply the apt-mark changes above.")
        return EXIT_OK

    require_root("protect --method hold")

    # Capture apt's current marking before changing it, so unprotect can put it
    # back exactly as it was.
    auto_now = currently_auto()
    was_auto = {p: (p in auto_now) for p in state.packages}
    # Carry forward the original marking for anything already recorded: what it
    # was before we first touched it is the state worth restoring, not the
    # 'manual' we ourselves imposed on the previous run.
    for package, original in previous.items():
        if package in was_auto:
            was_auto[package] = original

    # manual: stops autoremove reaping it once the distro default moves on.
    # hold:   stops an upgrade or removal replacing it out from under us.
    run(["apt-mark", "manual"] + state.packages)
    run(["apt-mark", "hold"] + state.packages)
    if stale:
        release_hold_packages(stale)
    write_hold_manifest(was_auto, state)

    print(f"\nProtected. apt will not remove or upgrade: {', '.join(state.packages)}")
    if stale:
        print(f"Released: {', '.join(sorted(stale))}")
        print("Anything no longer needed is now reclaimable:")
        print("  sudo apt autoremove")
    print("\nNote: a hold also blocks security updates for these packages. Review")
    print("them when you next rebuild PostgreSQL.")
    return EXIT_OK


def remove_hold(dry_run: bool) -> int:
    """Release every hold this tool applied, restoring apt's original marking."""
    held = read_hold_manifest()
    if not held:
        print("  No apt-mark holds recorded by pgjitguard.")
        return EXIT_OK

    if dry_run:
        print(f"  [dry-run] Would unhold: {', '.join(sorted(held))}")
        restore = sorted(p for p, was_auto in held.items() if was_auto)
        if restore:
            print(f"  [dry-run] Would restore to auto: {', '.join(restore)}")
        return EXIT_OK

    require_root("unprotect")
    require_tool("apt-mark", "apt")
    release_hold_packages(held)
    HOLD_MANIFEST.unlink(missing_ok=True)
    print(f"  Released holds: {', '.join(sorted(held))}")
    restored = sorted(p for p, was_auto in held.items() if was_auto)
    if restored:
        print(f"  Restored to automatically-installed: {', '.join(restored)}")
    return EXIT_OK


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def has_protection(method: Method) -> bool:
    """Return True if the given enforcement method is currently in place."""
    if method is Method.DEPENDS:
        return installed_pin_version() is not None
    return bool(read_hold_manifest())


def protection_gaps(state: JitState) -> tuple[list[str], list[str]]:
    """Compare what is actually enforced against what the module needs now.

    Returns (methods_fully_protecting, gaps). Presence of a pin or a hold
    manifest is NOT protection: after a rebuild against a newer LLVM they still
    guard the *previous* runtime, so the dependency set must be compared, not
    merely found. This is the drift the tool exists to catch.
    """
    protected_by: list[str] = []
    gaps: list[str] = []

    if installed_pin_version():
        depends = installed_pin_depends()
        uncovered = [p for p in state.packages if p not in depends]
        if uncovered:
            gaps.append(f"{PIN_PACKAGE} does not depend on: {', '.join(uncovered)}")
        else:
            protected_by.append("dependency package")

    recorded = read_hold_manifest()
    if recorded:
        held = currently_held()
        dropped = [p for p in recorded if p not in held]
        uncovered = [p for p in state.packages if p not in held]
        if dropped:
            gaps.append(f"recorded holds no longer applied: {', '.join(sorted(dropped))}")
        if uncovered:
            gaps.append(f"not held: {', '.join(uncovered)}")
        if not dropped and not uncovered:
            protected_by.append("apt-mark hold")

    return protected_by, gaps


def prompt_method(state: JitState) -> Optional[Method]:
    """Ask which enforcement method to use. Returns None if the user declines."""
    print("\nHow should apt be prevented from removing these packages?")
    print()
    print("  1) Dependency package  (recommended)")
    print("     Generates a small .deb that Depends on them, so apt models the")
    print("     dependency properly: removal is refused, an upgrade that would")
    print("     break it warns first, and autoremove can never reap it. Security")
    print("     updates still apply normally.")
    print()
    print("  2) apt-mark hold")
    print("     Marks the packages manual and held. Nothing is generated and it is")
    print("     reversible with apt-mark, but a hold also blocks security updates")
    print("     for those packages, and other tooling reports them as kept back.")
    print()
    print("  3) Neither - just report status from now on")
    print()

    while True:
        try:
            choice = input("  Choice [1]: ").strip() or "1"
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if choice == "1":
            return Method.DEPENDS
        if choice == "2":
            return Method.HOLD
        if choice == "3":
            return None
        print("  Enter 1, 2, or 3.")


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

    recorded = read_hold_manifest()
    if recorded:
        print(f"  apt-mark hold:      {', '.join(sorted(recorded))}")
    else:
        print("  apt-mark hold:      none recorded")

    print(f"  apt hook:           "
          f"{'installed' if APT_HOOK_FILE.is_file() else 'not installed'}"
          f" ({APT_HOOK_FILE})")

    invocation = Path(sys.argv[0]).name
    protected_by, gaps = protection_gaps(state)

    if protected_by:
        print(f"\n  Protected by: {', '.join(protected_by)}")
    if gaps:
        print("\n  *** Protection does not cover what llvmjit.so needs now ***")
        for gap in gaps:
            print(f"    {gap}")
        print("\n  This is what a rebuild against a different LLVM looks like: the")
        print("  old runtime is still protected while the new one is exposed.")
        print(f"  Re-run: sudo {invocation} protect")
    elif not protected_by:
        print("\n  Nothing is protecting these packages. An LLVM upgrade can still")
        print(f"  break JIT. Run: sudo {invocation} protect")

    if state.is_broken:
        print(f"\nJIT is BROKEN: {len(state.missing)} dependency/dependencies unresolved.")
        return EXIT_BROKEN

    print("\nJIT dependencies all resolve.")
    return EXIT_OK


def live_jit_check(args: argparse.Namespace) -> Optional[str]:
    """Force a JIT compilation through psql. Returns an error string, or None."""
    psql = shutil.which("psql") or str(PG_BIN / "psql")
    if not Path(psql).is_file():
        return "psql not found; cannot run the live check"

    # Zeroing the cost thresholds is what forces the provider to load; the
    # default jit_above_cost is exactly why this breakage stays hidden.
    sql = (
        "SET jit = on; "
        "SET jit_above_cost = 0; "
        "SET jit_inline_above_cost = 0; "
        "SET jit_optimize_above_cost = 0; "
        "SELECT count(*) FROM generate_series(1, 1000);"
    )
    cmd = [psql, "-XAtq", "-v", "ON_ERROR_STOP=1"]
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
        error = live_jit_check(args)
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
        print(f"    sudo {Path(sys.argv[0]).name} protect", file=sys.stderr)
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
        print(f"  Re-run: sudo {Path(sys.argv[0]).name} protect", file=sys.stderr)
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

    if not state.packages:
        print("Error: none of llvmjit.so's dependencies are owned by dpkg packages.",
              file=sys.stderr)
        print("  There is nothing for apt to protect. If LLVM was itself built from",
              file=sys.stderr)
        print("  source, it is already outside the package manager's reach.",
              file=sys.stderr)
        for entry in state.untracked:
            print(f"  untracked: {entry}", file=sys.stderr)
        return EXIT_ERROR

    print(f"JIT module:  {state.module}")
    print(f"Depends on:  {', '.join(state.packages)}")
    if state.untracked:
        print("Not protected (no owning package):")
        for entry in state.untracked:
            print(f"  {entry}")

    method = args.method
    if method is None:
        if not sys.stdin.isatty():
            print("\nError: no --method given and stdin is not a terminal.",
                  file=sys.stderr)
            print("  Pass --method depends or --method hold.", file=sys.stderr)
            return EXIT_ERROR
        method = prompt_method(state)
        if method is None:
            print("\nNo protection applied. Status and check still work; re-run")
            print(f"'sudo {Path(sys.argv[0]).name} protect' to change your mind.")
            return EXIT_OK
        print()

    if method is Method.DEPENDS:
        result = apply_depends(state, args.dry_run)
        superseded, remove_superseded = Method.HOLD, remove_hold
    else:
        result = apply_hold(state, args.dry_run)
        superseded, remove_superseded = Method.DEPENDS, remove_depends

    if result != EXIT_OK:
        return result

    # Applying one method does not disable the other. A leftover hold keeps
    # blocking security updates; a leftover package keeps pinning a runtime we
    # no longer build against. Remove it only once the new protection is in
    # place, so there is no unprotected window.
    if has_protection(superseded):
        print(f"\nRemoving superseded '{superseded}' protection:")
        remove_superseded(args.dry_run)

    return EXIT_OK


def cmd_unprotect(args: argparse.Namespace) -> int:
    """Remove protection, returning the runtime to normal apt policy."""
    require_linux()
    require_dpkg()

    print("Removing protection:")
    if args.method in (None, Method.DEPENDS):
        remove_depends(args.dry_run)
    if args.method in (None, Method.HOLD):
        remove_hold(args.dry_run)

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
DPkg::Post-Invoke {{ "{script} --pg-config '{pg_config}' check --hook --quiet || true"; }};
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
    print(f"'sudo {Path(sys.argv[0]).name} unprotect' if that is what you want.")
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
  sudo %(prog)s protect             # enforce the dependency (asks how)
  sudo %(prog)s protect -m depends  # ... or choose the method up front
  sudo %(prog)s install-hook        # check automatically after every apt run

After rebuilding PostgreSQL against a newer LLVM, re-run "sudo %(prog)s protect".
Either method re-derives the dependency set from the rebuilt module, protects
the new runtime, and releases the old one for "apt autoremove" to reclaim.
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

    protect = subparsers.add_parser("protect",
                                    help="Enforce the dependency against apt")
    protect.add_argument("-m", "--method", type=Method, choices=list(Method),
                         help="depends (generated package) or hold (apt-mark); "
                              "default: ask")

    unprotect = subparsers.add_parser("unprotect", help="Remove the protection")
    unprotect.add_argument("-m", "--method", type=Method, choices=list(Method),
                           help="Remove only this method's protection (default: both)")

    subparsers.add_parser("install-hook",
                          help="Run the check after every apt transaction")
    subparsers.add_parser("uninstall-hook",
                          help="Remove the apt hook and the copied script")

    args = parser.parse_args()
    if not args.command:
        args.command = "status"
    # Defaults for options that exist only on some subparsers.
    for option in ("live", "dbname", "hook", "method"):
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
