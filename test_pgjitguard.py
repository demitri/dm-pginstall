#!/usr/bin/env python3
"""
Tests for pgjitguard.py

Runs anywhere -- no dpkg, no apt, no PostgreSQL required. The package-management
logic is risky enough that it needs to be testable away from a live Debian box,
so every external command is replaced with recorded fixture output.

Usage:
    ./test_pgjitguard.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.resolve()))
import pgjitguard as g


# Recorded from a source-built PostgreSQL on Ubuntu with --with-llvm.
READELF_OUTPUT = """
Dynamic section at offset 0x1e2d8 contains 30 entries:
  Tag        Type                         Name/Value
 0x0000000000000001 (NEEDED)             Shared library: [libLLVM.so.20.1]
 0x0000000000000001 (NEEDED)             Shared library: [libstdc++.so.6]
 0x0000000000000001 (NEEDED)             Shared library: [libm.so.6]
 0x0000000000000001 (NEEDED)             Shared library: [libgcc_s.so.1]
 0x0000000000000001 (NEEDED)             Shared library: [libc.so.6]
 0x000000000000000e (SONAME)             Library soname: [llvmjit.so]
 0x000000000000001d (RUNPATH)            Library runpath: [/usr/local/lib]
"""

LDD_BROKEN = """\tlinux-vdso.so.1 (0x00007ffc8d5f0000)
\tlibLLVM.so.20.1 => not found
\tlibstdc++.so.6 => /lib/x86_64-linux-gnu/libstdc++.so.6 (0x00007f9a1c000000)
\tlibm.so.6 => /lib/x86_64-linux-gnu/libm.so.6 (0x00007f9a1bf00000)
\tlibgcc_s.so.1 => /lib/x86_64-linux-gnu/libgcc_s.so.1 (0x00007f9a1be00000)
\tlibc.so.6 => /lib/x86_64-linux-gnu/libc.so.6 (0x00007f9a1bc00000)
\t/lib64/ld-linux-x86-64.so.2 (0x00007f9a1c400000)
"""

LDD_HEALTHY = LDD_BROKEN.replace(
    "libLLVM.so.20.1 => not found",
    "libLLVM.so.21.1 => /lib/x86_64-linux-gnu/libLLVM.so.21.1 (0x00007f9a18000000)",
)


class FakeState:
    """Stands in for JitState where only the derived package sets matter."""

    def __init__(self, packages, llvm_packages=None,
                 module="/usr/local/postgresql/lib/llvmjit.so",
                 pg_config="/usr/local/postgresql-18.1/bin/pg_config"):
        self.packages = packages
        # Default: whatever looks like an LLVM runtime package.
        self.llvm_packages = (llvm_packages if llvm_packages is not None
                              else [p for p in packages if "llvm" in p])
        self.module = Path(module)
        self.pg_config = Path(pg_config)
        self.has_jit = True

    @property
    def is_at_risk(self):
        return bool(self.llvm_packages)

    @property
    def llvm_provider(self):
        return "/usr/local/llvm-21.1.0/lib/libLLVM.so.21.1"


failures = []
checks = 0


def check(label, got, want):
    global checks
    checks += 1
    if got != want:
        failures.append(f"{label}\n     got:  {got!r}\n     want: {want!r}")


def stub(**attrs):
    """Temporarily replace module attributes; returns a restore callable."""
    saved = {name: getattr(g, name) for name in attrs}
    for name, value in attrs.items():
        setattr(g, name, value)
    return lambda: [setattr(g, n, v) for n, v in saved.items()]


# ---------------------------------------------------------------------------
# DT_NEEDED / ldd parsing
# ---------------------------------------------------------------------------

def test_direct_needed_takes_only_needed_entries():
    restore = stub(run=lambda cmd, check=True: (0, READELF_OUTPUT, ""),
                   require_tool=lambda n, p: f"/usr/bin/{n}")
    try:
        check("direct_needed", g.direct_needed(Path("/x/llvmjit.so")),
              ["libLLVM.so.20.1", "libstdc++.so.6", "libm.so.6",
               "libgcc_s.so.1", "libc.so.6"])
    finally:
        restore()


def test_resolve_deps_on_broken_module():
    # ldd exits non-zero when something is unresolved; output must still parse.
    restore = stub(run=lambda cmd, check=True: (1, LDD_BROKEN, ""),
                   require_tool=lambda n, p: f"/usr/bin/{n}")
    try:
        resolved = g.resolve_deps(Path("/x/llvmjit.so"))
        check("missing LLVM -> None", resolved.get("libLLVM.so.20.1"), None)
        check("libc resolved", resolved.get("libc.so.6"),
              "/lib/x86_64-linux-gnu/libc.so.6")
        check("vdso has no path, excluded", "linux-vdso.so.1" in resolved, False)
        check("loader keyed by basename", resolved.get("ld-linux-x86-64.so.2"),
              "/lib64/ld-linux-x86-64.so.2")
    finally:
        restore()


def test_resolve_deps_on_healthy_module():
    restore = stub(run=lambda cmd, check=True: (0, LDD_HEALTHY, ""),
                   require_tool=lambda n, p: f"/usr/bin/{n}")
    try:
        resolved = g.resolve_deps(Path("/x/llvmjit.so"))
        check("healthy LLVM resolved", resolved.get("libLLVM.so.21.1"),
              "/lib/x86_64-linux-gnu/libLLVM.so.21.1")
        check("nothing unresolved",
              [s for s, p in resolved.items() if p is None], [])
    finally:
        restore()


# ---------------------------------------------------------------------------
# Stale-protection detection -- the drift the tool exists to catch
# ---------------------------------------------------------------------------

def test_pin_covering_current_packages_is_protected():
    restore = stub(installed_pin_version=lambda: "1.2",
                   installed_pin_depends=lambda: ["libllvm21", "libc6"])
    try:
        by, gaps = g.protection_gaps(FakeState(["libc6", "libllvm21"]))
        check("protected by depends", by, ["dependency package"])
        check("no gaps", gaps, [])
    finally:
        restore()


def test_pin_left_behind_by_rebuild_is_a_gap():
    # The regression codex caught: a pin exists, so the old code reported
    # "protected" -- but it still guards libllvm20 after a rebuild onto 21.
    restore = stub(installed_pin_version=lambda: "1.2",
                   installed_pin_depends=lambda: ["libllvm20", "libc6"])
    try:
        by, gaps = g.protection_gaps(FakeState(["libc6", "libllvm21"]))
        check("not counted as protected", by, [])
        check("gap names the uncovered package", gaps,
              [f"{g.PIN_PACKAGE} does not depend on: libllvm21"])
    finally:
        restore()


def test_no_protection_at_all():
    restore = stub(installed_pin_version=lambda: None)
    try:
        by, gaps = g.protection_gaps(FakeState(["libllvm21"]))
        check("nothing protecting", (by, gaps), ([], []))
    finally:
        restore()


def test_private_llvm_needs_no_protection():
    """After --build-llvm the runtime lives under /usr/local and no dpkg package
    owns it, so apt cannot retire it. That must read as "nothing to protect",
    not as an unprotected gap to nag about."""
    state = FakeState(["libc6", "libstdc++6"], llvm_packages=[])
    check("not at risk", state.is_at_risk, False)
    restore = stub(installed_pin_version=lambda: None)
    try:
        check("no gaps reported", g.protection_gaps(state), ([], []))
    finally:
        restore()
    # Even with a stale pin installed, there is nothing at risk to report.
    restore = stub(installed_pin_version=lambda: "1.2",
                   installed_pin_depends=lambda: ["libllvm20"])
    try:
        check("stale pin irrelevant when nothing is at risk",
              g.protection_gaps(state), ([], []))
    finally:
        restore()


def test_system_llvm_is_still_at_risk():
    check("dpkg-owned LLVM is at risk",
          FakeState(["libc6", "libllvm21"]).is_at_risk, True)


def make_installs(base, versions):
    for version in versions:
        binned = base / f"postgresql-{version}" / "bin"
        binned.mkdir(parents=True)
        (binned / "pg_config").write_text("#!/bin/sh\n")
    return base


def test_pin_covers_every_installation_that_needs_it():
    """One pin package, several installations. Declaring only the one being
    protected would silently drop the packages a sibling depends on -- and on
    the cleanup path, would keep a pin that does not cover the sibling it is
    being kept for."""
    import tempfile as _tf
    with _tf.TemporaryDirectory() as tmp:
        base = make_installs(Path(tmp), ["18.3", "18.6"])
        current = FakeState(["libc6", "libllvm21"],
                            pg_config=str(base / "postgresql-18.6" / "bin" / "pg_config"))
        current.has_jit = True

        class Sibling:            # still on the older system LLVM
            has_jit = True
            is_at_risk = True
            packages = ["libc6", "libllvm20"]

        restore = stub(INSTALL_BASE=base, JitState=lambda pc: Sibling())
        try:
            required, contributors, uninspectable = g.union_pin_packages(current)
            check("union covers both runtimes", required,
                  ["libc6", "libllvm20", "libllvm21"])
            check("both installations contribute", len(contributors), 2)
            check("nothing uninspectable", uninspectable, [])
        finally:
            restore()


def test_pin_gap_detected_when_it_misses_a_sibling():
    """The reported failure: a pin naming libllvm21 was retained as 'needed' by
    a sibling that actually uses libllvm20, leaving the sibling unprotected."""
    import tempfile as _tf
    with _tf.TemporaryDirectory() as tmp:
        base = make_installs(Path(tmp), ["18.3", "18.6"])
        current = FakeState(["libllvm21"],
                            pg_config=str(base / "postgresql-18.6" / "bin" / "pg_config"))
        current.has_jit = True

        class Sibling:
            has_jit = True
            is_at_risk = True
            packages = ["libllvm20"]

        restore = stub(INSTALL_BASE=base, JitState=lambda pc: Sibling(),
                       installed_pin_version=lambda: "1.2",
                       installed_pin_depends=lambda: ["libllvm21"])
        try:
            by, gaps = g.protection_gaps(current)
            check("not counted as protected", by, [])
            check("the sibling's runtime is reported uncovered", gaps,
                  [f"{g.PIN_PACKAGE} does not depend on: libllvm20"])
        finally:
            restore()


def test_uninspectable_sibling_blocks_pin_removal():
    """Conservative on purpose: an install we cannot inspect must not be
    assumed safe, since guessing wrong silently unprotects a working build."""
    import tempfile as _tf
    with _tf.TemporaryDirectory() as tmp:
        base = make_installs(Path(tmp), ["18.3"])
        current = FakeState([], llvm_packages=[],
                            pg_config=str(base / "postgresql-18.6" / "bin" / "pg_config"))
        current.has_jit = True

        def explode(pg_config):
            raise SystemExit(2)

        restore = stub(INSTALL_BASE=base, JitState=explode)
        try:
            required, contributors, uninspectable = g.union_pin_packages(current)
            check("counted as uninspectable", uninspectable,
                  [base / "postgresql-18.3" / "bin" / "pg_config"])
            check("not silently treated as safe", contributors, [])
            check("no packages inferred", required, [])
        finally:
            restore()


def test_uninspectable_sibling_keeps_its_dependencies_in_a_rebuilt_pin():
    """The reported failure: with one known contributor AND one uninspectable
    sibling, the pin was rebuilt from known packages only, dropping whatever
    protected the sibling -- the opposite of the conservative treatment
    uninspectable installs are promised."""
    import tempfile as _tf
    with _tf.TemporaryDirectory() as tmp:
        base = make_installs(Path(tmp), ["18.1", "18.3"])
        current = FakeState(["libllvm21"],
                            pg_config=str(base / "postgresql-18.6" / "bin" / "pg_config"))

        broken = base / "postgresql-18.1" / "bin" / "pg_config"

        class Known:
            has_jit = True
            is_at_risk = True
            packages = ["libllvm21"]

        def inspect(pg_config):
            if Path(pg_config).resolve() == broken.resolve():
                raise SystemExit(2)
            return Known()

        restore = stub(INSTALL_BASE=base, JitState=inspect,
                       installed_pin_version=lambda: "1.4",
                       # The existing pin protects the sibling we cannot inspect.
                       installed_pin_depends=lambda: ["libllvm18", "libllvm21"])
        try:
            required, contributors, uninspectable = g.union_pin_packages(current)
            check("uninspectable sibling reported", uninspectable, [broken])
            check("known contributor still present", len(contributors), 2)
            check("prior dependency carried forward", "libllvm18" in required, True)
            check("union is complete", required, ["libllvm18", "libllvm21"])
        finally:
            restore()


def test_private_sibling_does_not_block_pin_removal():
    import tempfile as _tf
    with _tf.TemporaryDirectory() as tmp:
        base = make_installs(Path(tmp), ["18.3"])
        current = FakeState([], llvm_packages=[],
                            pg_config=str(base / "postgresql-18.6" / "bin" / "pg_config"))
        current.has_jit = True

        class PrivateSibling:
            has_jit = True
            is_at_risk = False
            packages = ["libc6"]

        restore = stub(INSTALL_BASE=base, JitState=lambda pc: PrivateSibling())
        try:
            required, contributors, uninspectable = g.union_pin_packages(current)
            check("nothing needs the pin", (required, contributors, uninspectable),
                  ([], [], []))
        finally:
            restore()


# ---------------------------------------------------------------------------
# Pin version bumping -- must always be an upgrade, never a reinstall
# ---------------------------------------------------------------------------

def test_pin_version_bump_is_monotonic():
    for current, want in [(None, "1.1"), ("1.1", "1.2"), ("1.9", "1.10"),
                          ("1.42", "1.43"), ("nonsense", "1.1")]:
        restore = stub(installed_pin_version=lambda c=current: c)
        try:
            check(f"next_pin_version({current!r})", g.next_pin_version(), want)
        finally:
            restore()


# ---------------------------------------------------------------------------
# Depends parsing off dpkg-query
# ---------------------------------------------------------------------------

def test_installed_pin_depends_strips_versions_and_alternatives():
    restore = stub(
        run=lambda cmd, check=True: (0, "libllvm21 (>= 1:21~), libc6 | libc6-udeb, libstdc++6", ""),
    )
    saved_which = g.shutil.which
    g.shutil.which = lambda name: "/usr/bin/dpkg-query"
    try:
        check("depends parsed", g.installed_pin_depends(),
              ["libllvm21", "libc6", "libstdc++6"])
    finally:
        g.shutil.which = saved_which
        restore()


# ---------------------------------------------------------------------------
# apt hook rendering
# ---------------------------------------------------------------------------

def rendered_hook():
    return g.APT_HOOK_TEMPLATE.format(
        script="/usr/local/sbin/pgjitguard",
        pg_config="/usr/local/postgresql-18.1/bin/pg_config",
    )


def test_apt_hook_stays_valid_apt_conf():
    hook = rendered_hook()
    expected = ('DPkg::Post-Invoke { "/usr/local/sbin/pgjitguard --pg-config '
                "'/usr/local/postgresql-18.1/bin/pg_config' --quiet check "
                '--hook || true"; };')
    check("hook line", expected in hook, True)
    check("braces survive format", hook.count("{") == hook.count("}") == 1, True)


def test_apt_hook_command_actually_parses():
    """Regression: --quiet sat after the subcommand, so argparse rejected the
    whole hook. '|| true' swallowed the exit code, so every apt transaction
    printed a usage error and checked nothing. Asserting on the rendered text
    missed it entirely -- the command has to be executed through parse_args."""
    import shlex

    command = [ln for ln in rendered_hook().splitlines()
               if ln.startswith("DPkg::")][0]
    inner = command.split('"')[1]              # the shell command apt runs
    argv = shlex.split(inner)
    argv = argv[:argv.index("||")]             # drop the "|| true" guard

    saved = sys.argv
    sys.argv = argv
    try:
        args = g.parse_args()
    except SystemExit as exc:
        check("hook command must parse", f"argparse exited {exc.code}", "parsed")
        return
    finally:
        sys.argv = saved

    check("hook runs check", args.command, "check")
    check("hook sets --hook", args.hook, True)
    check("hook sets --quiet", args.quiet, True)
    check("hook names the installation", args.pg_config,
          "/usr/local/postgresql-18.1/bin/pg_config")


def test_jitstate_separates_llvm_packages_from_the_rest():
    """Hold mode is scoped to the LLVM runtime: holding libc6 would block its
    security updates to guard against a retirement that never happens."""
    resolved = {
        "libLLVM.so.21.1": "/lib/x86_64-linux-gnu/libLLVM.so.21.1",
        "libstdc++.so.6": "/lib/x86_64-linux-gnu/libstdc++.so.6",
        "libc.so.6": "/lib/x86_64-linux-gnu/libc.so.6",
    }
    owners = {
        "/lib/x86_64-linux-gnu/libLLVM.so.21.1": "libllvm21",
        "/lib/x86_64-linux-gnu/libstdc++.so.6": "libstdc++6",
        "/lib/x86_64-linux-gnu/libc.so.6": "libc6",
    }
    restore = stub(
        get_pg_setting=lambda pc, flag: "PostgreSQL 18.1",
        find_llvmjit=lambda pc: Path("/usr/local/postgresql/lib/llvmjit.so"),
        direct_needed=lambda m: list(resolved),
        resolve_deps=lambda m: resolved,
        dpkg_owner=lambda path: owners.get(path),
    )
    try:
        state = g.JitState(Path("/usr/local/postgresql-18.1/bin/pg_config"))
        check("depends covers every dpkg-owned dep", state.packages,
              ["libc6", "libllvm21", "libstdc++6"])
        check("LLVM runtime identified", state.llvm_packages, ["libllvm21"])
        check("system LLVM is at risk", state.is_at_risk, True)
        check("nothing unresolved", state.missing, [])
    finally:
        restore()


def test_remediation_command_names_the_installation():
    """Every 'run this to fix it' message must carry --pg-config, or it sends
    the user back to the default symlink."""
    saved = sys.argv
    sys.argv = ["/usr/local/sbin/pgjitguard"]
    try:
        check("absolute path, carries --pg-config",
              g.remediation_command(FakeState(["libllvm21"])),
              "sudo /usr/local/sbin/pgjitguard --pg-config "
              "/usr/local/postgresql-18.1/bin/pg_config protect")
        # './pgjitguard.py' must not be suggested as bare 'pgjitguard.py':
        # sudo's PATH does not include the current directory.
        check("relative invocation is resolved",
              "sudo ./" not in g.remediation_command(FakeState(["libllvm21"])), True)
        # A pg_config path containing a space must survive as one argument.
        spaced = FakeState(["libllvm21"], pg_config="/opt/my pg/bin/pg_config")
        check("spaced path is quoted",
              "'/opt/my pg/bin/pg_config'" in g.remediation_command(spaced), True)
    finally:
        sys.argv = saved


# ---------------------------------------------------------------------------

def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()

    print(f"ran {len(tests)} tests, {checks} checks")
    if failures:
        print(f"\n{len(failures)} FAILURE(S):")
        for failure in failures:
            print(f"  {failure}")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
