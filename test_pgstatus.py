#!/usr/bin/env python3
"""
Tests for pgstatus.py's JIT/LLVM reporting.

Runs anywhere -- no PostgreSQL, no LLVM, no ldd. The reporting exists to catch
a dependency that fails silently, so it is worth checking that it reads the
loader's answer correctly rather than a plausible-looking guess.

Usage:
    ./test_pgstatus.py
"""

import contextlib
import io
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.resolve()))
import pgstatus as g


failures = []
checks = 0


def check(label, got, want):
    global checks
    checks += 1
    if got != want:
        failures.append(f"{label}\n     got:  {got!r}\n     want: {want!r}")


LDD_EMBEDDED = """\tlinux-vdso.so.1 (0x00007ffc8d5f0000)
\tlibLLVM.so.18.1 => /usr/local/postgresql-18.6/lib/libLLVM.so.18.1 (0x00007f9a18000000)
\tlibstdc++.so.6 => /lib/x86_64-linux-gnu/libstdc++.so.6 (0x00007f9a1c000000)
"""

LDD_SYSTEM = LDD_EMBEDDED.replace(
    "/usr/local/postgresql-18.6/lib/libLLVM.so.18.1",
    "/lib/x86_64-linux-gnu/libLLVM.so.18.1")

LDD_BROKEN = """\tlinux-vdso.so.1 (0x00007ffc8d5f0000)
\tlibLLVM.so.18.1 => not found
\tlibstdc++.so.6 => /lib/x86_64-linux-gnu/libstdc++.so.6 (0x00007f9a1c000000)
"""


def _install(tmp, with_llvmjit=True):
    """A PostgreSQL installation tree, real enough to inspect."""
    root = Path(tmp) / "postgresql-18.6"
    (root / "bin").mkdir(parents=True)
    (root / "lib").mkdir()
    (root / "bin" / "pg_ctl").write_text("")
    if with_llvmjit:
        (root / "lib" / "llvmjit.so").write_text("")
    return root


def _stub_ldd(output):
    """Make inspect_jit see this ldd output, and no pg_config."""
    class Result:
        returncode = 0
        stdout = output

    def run(cmd, **kw):
        if cmd and cmd[0] == "ldd":
            return Result()
        raise OSError("no pg_config here")     # falls back to bin/../lib
    return run


def test_embedded_runtime_is_recognised():
    """The point of embedding: report it as belonging to the installation."""
    with tempfile.TemporaryDirectory() as tmp:
        root = _install(tmp)
        saved = (g.subprocess.run, g.newest_llvm_version)
        g.subprocess.run = _stub_ldd(LDD_EMBEDDED.replace(
            "/usr/local/postgresql-18.6/lib", str(root / "lib")))
        g.newest_llvm_version = lambda: "18.1.3"
        try:
            info = g.inspect_jit(root / "bin" / "pg_ctl")
        finally:
            g.subprocess.run, g.newest_llvm_version = saved

    check("module found", info.built, True)
    check("soname read", info.soname, "libLLVM.so.18.1")
    check("resolved", info.resolved, True)
    check("recognised as embedded", info.embedded, True)
    check("version from the soname", info.runtime_version, "18.1")


def test_system_runtime_is_not_reported_as_embedded():
    """An installation that still depends on the distro's LLVM must not read as
    self-contained -- that is the whole distinction being reported."""
    with tempfile.TemporaryDirectory() as tmp:
        root = _install(tmp)
        saved = (g.subprocess.run, g.newest_llvm_version)
        g.subprocess.run = _stub_ldd(LDD_SYSTEM)
        g.newest_llvm_version = lambda: "18.1.3"
        try:
            info = g.inspect_jit(root / "bin" / "pg_ctl")
        finally:
            g.subprocess.run, g.newest_llvm_version = saved

    check("resolved", info.resolved, True)
    check("not embedded", info.embedded, False)
    check("runtime path is the system one", str(info.runtime_path),
          "/lib/x86_64-linux-gnu/libLLVM.so.18.1")


def test_unresolvable_runtime_is_reported_as_broken():
    """'not found' is the state that costs a production query, so it must never
    be mistaken for a resolved path."""
    with tempfile.TemporaryDirectory() as tmp:
        root = _install(tmp)
        saved = (g.subprocess.run, g.newest_llvm_version)
        g.subprocess.run = _stub_ldd(LDD_BROKEN)
        g.newest_llvm_version = lambda: "18.1.3"
        try:
            info = g.inspect_jit(root / "bin" / "pg_ctl")
        finally:
            g.subprocess.run, g.newest_llvm_version = saved

    check("soname still read", info.soname, "libLLVM.so.18.1")
    check("not resolved", info.resolved, False)
    check("no runtime path invented", info.runtime_path, None)


def test_a_build_without_jit_is_not_an_error():
    with tempfile.TemporaryDirectory() as tmp:
        root = _install(tmp, with_llvmjit=False)
        info = g.inspect_jit(root / "bin" / "pg_ctl")
    check("nothing built", info.built, False)
    check("nothing to resolve", info.resolved, False)


def test_update_is_flagged_only_when_llvm_is_actually_newer():
    same = g.JitInfo(built=True, soname="libLLVM.so.18.1", system_version="18.1.3")
    check("same major.minor is not an update", same.update_available, False)

    newer = g.JitInfo(built=True, soname="libLLVM.so.18.1", system_version="20.1.2")
    check("newer LLVM is an update", newer.update_available, True)

    older = g.JitInfo(built=True, soname="libLLVM.so.20.1", system_version="18.1.3")
    check("older LLVM is not an update", older.update_available, False)

    unknown = g.JitInfo(built=True, soname="libLLVM.so", system_version="20.1.2")
    check("no version, no claim", unknown.update_available, False)


def test_notes_name_the_problem_for_a_system_runtime():
    """A note is what a user actually sees; silence here is the failure mode."""
    instance = g.PostgreSQLInstance(name="18", pg_ctl_path=Path("/nonexistent/bin/pg_ctl"))
    saved = g.inspect_jit
    g.inspect_jit = lambda pg_ctl: g.JitInfo(
        built=True, module=Path("/i/lib/llvmjit.so"), soname="libLLVM.so.18.1",
        runtime_path=Path("/lib/x86_64-linux-gnu/libLLVM.so.18.1"),
        resolved=True, embedded=False, system_version="20.1.2")
    try:
        g.enrich_jit([instance])
    finally:
        g.inspect_jit = saved

    check("warns the runtime is outside the install",
          any("outside this installation" in n for n in instance.notes), True)
    check("mentions the newer LLVM",
          any("Rebuild PostgreSQL to use the newer one" in n for n in instance.notes), True)


def test_embedded_runtime_earns_no_warning():
    instance = g.PostgreSQLInstance(name="18", pg_ctl_path=Path("/nonexistent/bin/pg_ctl"))
    saved = g.inspect_jit
    g.inspect_jit = lambda pg_ctl: g.JitInfo(
        built=True, module=Path("/i/lib/llvmjit.so"), soname="libLLVM.so.18.1",
        runtime_path=Path("/i/lib/libLLVM.so.18.1"),
        resolved=True, embedded=True, system_version="18.1.3")
    try:
        g.enrich_jit([instance])
    finally:
        g.inspect_jit = saved
    check("nothing to warn about", instance.notes, [])


def test_summary_line_states_the_problem_plainly():
    """The expanded listing is where most people will see this, so each state
    has to be distinguishable at a glance."""
    check("no JIT info, no line", g.jit_summary(None), None)
    check("not built is said outright",
          g.jit_summary(g.JitInfo(built=False)),
          "not built (configured without --with-llvm)")
    check("embedded",
          g.jit_summary(g.JitInfo(built=True, soname="libLLVM.so.18.1",
                                  resolved=True, embedded=True)),
          "libLLVM.so.18.1 (embedded in this installation)")
    check("outside the installation",
          g.jit_summary(g.JitInfo(built=True, soname="libLLVM.so.18.1",
                                  resolved=True, embedded=False)),
          "libLLVM.so.18.1 (outside this installation)")
    check("broken is not buried",
          g.jit_summary(g.JitInfo(built=True, soname="libLLVM.so.18.1",
                                  resolved=False)),
          "libLLVM.so.18.1 — NOT FOUND, JIT is broken")
    check("a newer LLVM is mentioned",
          g.jit_summary(g.JitInfo(built=True, soname="libLLVM.so.18.1",
                                  resolved=True, embedded=True,
                                  system_version="20.1.2")),
          "libLLVM.so.18.1 (embedded in this installation); LLVM 20.1.2 available")


def _stub_input(*answers):
    """Answer the selection prompt with these, in order.

    pgstatus does not define 'input', so a module attribute shadows the builtin
    for the duration. Returns a restore callable that removes it again.
    """
    replies = iter(answers)
    g.input = lambda prompt="": next(replies)
    return lambda: g.__dict__.pop("input", None)


def _run_main(argv, instances):
    """Run main() with a fixed set of discovered instances."""
    saved = (sys.argv, g.discover_all_instances, g.enrich_jit)
    sys.argv = argv
    g.discover_all_instances = lambda **kw: instances
    g.enrich_jit = lambda insts: None
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = g.main()
    finally:
        sys.argv, g.discover_all_instances, g.enrich_jit = saved
    return code, out.getvalue(), err.getvalue()


def test_info_needs_no_name_when_there_is_one_instance():
    """'pgstatus.py info' on a single-instance machine was an error telling the
    user to type the name it had just printed."""
    only = g.PostgreSQLInstance(name="18", port=5432)
    code, out, err = _run_main(["pgstatus.py", "info"], [only])
    check("succeeds", code, 0)
    check("describes the only instance", "Instance: 18" in out, True)


def test_info_without_a_terminal_says_what_the_choices_are():
    """Piped or scripted, there is no one to prompt -- so the error has to carry
    the names and the command that lists them, not just a complaint."""
    instances = [g.PostgreSQLInstance(name="18"), g.PostgreSQLInstance(name="17")]
    saved = g.stdin_is_interactive
    g.stdin_is_interactive = lambda: False
    try:
        code, out, err = _run_main(["pgstatus.py", "info"], instances)
    finally:
        g.stdin_is_interactive = saved
    check("refuses to guess", code, 1)
    check("names the candidates", "18, 17" in err, True)
    check("shows a usable example", "info 18" in err, True)
    check("points at the listing command", "list" in err, True)

    code, out, err = _run_main(["pgstatus.py", "info"], [])
    check("nothing to describe", code, 1)
    check("says so rather than listing nothing",
          "No instances were found" in err, True)


def test_info_prompts_when_there_is_a_terminal():
    """Several instances and a person present: offer the list and let them pick."""
    instances = [g.PostgreSQLInstance(name="18", port=5432),
                 g.PostgreSQLInstance(name="17", port=5433)]
    saved = g.stdin_is_interactive
    g.stdin_is_interactive = lambda: True
    restore_input = _stub_input("2")
    try:
        code, out, err = _run_main(["pgstatus.py", "info"], instances)
    finally:
        g.stdin_is_interactive = saved
        restore_input()

    check("succeeds", code, 0)
    check("lists the choices", "1. 18" in out and "2. 17" in out, True)
    check("describes the chosen one", "Instance: 17" in out, True)


def test_selection_accepts_a_name_and_can_be_declined():
    instances = [g.PostgreSQLInstance(name="18"), g.PostgreSQLInstance(name="17")]
    saved = g.stdin_is_interactive
    g.stdin_is_interactive = lambda: True
    restore_input = lambda: None
    try:
        restore_input = _stub_input("17")
        with contextlib.redirect_stdout(io.StringIO()):
            check("a typed name is a valid answer",
                  g.choose_instance(instances).name, "17")

        restore_input = _stub_input("q")
        with contextlib.redirect_stdout(io.StringIO()):
            check("quitting chooses nothing", g.choose_instance(instances), None)

        restore_input = _stub_input("99", "1")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            check("re-asks after a bad answer",
                  g.choose_instance(instances).name, "18")
        check("says why it re-asked", "Not one of the choices" in out.getvalue(), True)
    finally:
        g.stdin_is_interactive = saved
        restore_input()


def test_check_is_silent_when_there_is_nothing_to_say():
    """It runs on every login. A report that prints when all is well is one
    people stop reading, which defeats the point of printing at all."""
    healthy = g.PostgreSQLInstance(name="18", port=5432)
    healthy.jit = g.JitInfo(built=True, soname="libLLVM.so.18.1", resolved=True,
                            embedded=True, system_version="18.1.3")
    report, count = g.format_check([healthy])
    check("nothing printed", report, "")
    check("nothing counted", count, 0)


def test_check_reports_each_problem_with_the_command_that_fixes_it():
    broken = g.PostgreSQLInstance(name="18", port=5432, stale_postmaster_pid=True)
    broken.jit = g.JitInfo(built=True, soname="libLLVM.so.18.1", resolved=False,
                           system_version="20.1.2")
    report, count = g.format_check([broken])
    check("counts every issue", count, 3)
    check("stale pid reported", "stale postmaster.pid" in report, True)
    check("broken JIT reported", "JIT is broken" in report, True)
    check("newer LLVM reported", "LLVM 20.1.2 is installed" in report, True)
    check("suggests the rebuild", "pginstall.py --component postgresql" in report, True)


def test_an_embedded_runtime_is_not_nagged_about():
    """The point of embedding is that there is nothing left to warn about; a
    warning that survives the fix trains people to ignore it."""
    inst = g.PostgreSQLInstance(name="18", port=5432)
    inst.jit = g.JitInfo(built=True, soname="libLLVM.so.18.1", resolved=True,
                         embedded=True, system_version="18.1.3")
    check("no issues", g.instance_issues(inst), [])

    outside = g.PostgreSQLInstance(name="17", port=5433)
    outside.jit = g.JitInfo(built=True, soname="libLLVM.so.18.1",
                            runtime_path=Path("/lib/x86_64-linux-gnu/libLLVM.so.18.1"),
                            resolved=True, embedded=False, system_version="18.1.3")
    problems = [problem for problem, _ in g.instance_issues(outside)]
    check("an external runtime is worth saying",
          any("outside this installation" in p for p in problems), True)


def test_check_exit_code_reflects_whether_anything_was_reported():
    """Scripts and timers need to know without parsing the text."""
    healthy = g.PostgreSQLInstance(name="18", port=5432)
    healthy.jit = g.JitInfo(built=True, soname="libLLVM.so.18.1", resolved=True,
                            embedded=True)
    code, out, err = _run_main(["pgstatus.py", "check"], [healthy])
    check("quiet means zero", (code, out), (0, ""))

    broken = g.PostgreSQLInstance(name="18", stale_postmaster_pid=True)
    code, out, err = _run_main(["pgstatus.py", "check"], [broken])
    check("something to report means non-zero", code, 1)
    check("and it is printed", "stale postmaster.pid" in out, True)


def _installation(tmp, version, jit=None, alias=False):
    root = Path(tmp) / f"postgresql-{version}"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "pg_config").write_text("")
    return g.Installation(version=version, path=root,
                          pg_config=root / "bin" / "pg_config",
                          is_alias_target=alias, jit=jit)


def test_installations_are_found_without_anything_running():
    """The gap this closes: an idle build is invisible to instance discovery,
    yet its JIT module is what keeps an old system LLVM installed."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        for version in ("12.20", "18.6"):
            (base / f"postgresql-{version}" / "bin").mkdir(parents=True)
            (base / f"postgresql-{version}" / "bin" / "pg_config").write_text("")
        (base / "postgresql").symlink_to(base / "postgresql-18.6")

        saved = (g.INSTALL_BASE, g.PG_BASE, g.inspect_jit)
        g.INSTALL_BASE = base
        g.PG_BASE = base / "postgresql"
        g.inspect_jit = lambda pg_ctl: None
        try:
            found = g.discover_installations()
        finally:
            g.INSTALL_BASE, g.PG_BASE, g.inspect_jit = saved

    check("both builds found", [i.version for i in found], ["18.6", "12.20"])
    check("the symlink is not counted as a build", len(found), 2)
    check("alias target identified",
          [i.version for i in found if i.is_alias_target], ["18.6"])


def test_an_installation_knows_which_instances_run_from_it():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        (base / "postgresql-18.6" / "bin").mkdir(parents=True)
        (base / "postgresql-18.6" / "bin" / "pg_config").write_text("")
        (base / "postgresql-18.6" / "bin" / "pg_ctl").write_text("")
        (base / "postgresql").symlink_to(base / "postgresql-18.6")
        # The instance was found through the alias, as most are.
        instance = g.PostgreSQLInstance(
            name="18", pg_ctl_path=base / "postgresql" / "bin" / "pg_ctl")

        saved = (g.INSTALL_BASE, g.PG_BASE, g.inspect_jit)
        g.INSTALL_BASE = base
        g.PG_BASE = base / "postgresql"
        g.inspect_jit = lambda pg_ctl: None
        try:
            found = g.discover_installations([instance])
        finally:
            g.INSTALL_BASE, g.PG_BASE, g.inspect_jit = saved

    check("alias resolved to the real build", found[0].used_by, ["18"])


def test_check_stays_quiet_about_an_idle_build_that_merely_links_system_llvm():
    """It is worth listing, not worth alarming about every login -- but a
    runtime that has actually gone missing is."""
    with tempfile.TemporaryDirectory() as tmp:
        linked = _installation(tmp, "12.22", jit=g.JitInfo(
            built=True, soname="libLLVM.so.18.1", resolved=True, embedded=False))
        check("listed in the inventory", len(g.installation_issues(linked)), 1)
        check("not in the login report",
              g.installation_issues(linked, breaking_only=True), [])

    with tempfile.TemporaryDirectory() as tmp:
        broken = _installation(tmp, "12.20", jit=g.JitInfo(
            built=True, soname="libLLVM.so.18.1", resolved=False))
        check("a missing runtime is reported either way",
              len(g.installation_issues(broken, breaking_only=True)), 1)
        report, count = g.format_check([], [broken])
        check("and reaches the login report", "JIT is broken" in report, True)
        check("counted", count, 1)


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
