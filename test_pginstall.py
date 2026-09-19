#!/usr/bin/env python3
"""
Tests for pginstall.py's LLVM toolchain selection.

Runs anywhere -- no LLVM, no PostgreSQL, no network. Covers the decisions that
determine which LLVM PostgreSQL ends up linked against, because getting those
wrong is silent: the build succeeds and the wrong runtime is baked in.

Usage:
    ./test_pginstall.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.resolve()))
import pginstall as p


failures = []
checks = 0


def check(label, got, want):
    global checks
    checks += 1
    if got != want:
        failures.append(f"{label}\n     got:  {got!r}\n     want: {want!r}")


# A real 'pg_config --configure' line, which quotes each argument.
def configure_line(*extra):
    args = ["--prefix=/usr/local/postgresql-18.6", "--with-icu", "--with-openssl"]
    args.extend(extra)
    return " ".join(f"'{a}'" for a in args)


# ---------------------------------------------------------------------------
# Which llvm-config does --build-llvm use?
# ---------------------------------------------------------------------------

def test_private_llvm_config_is_versioned_not_the_symlink():
    """--no-alias skips the /usr/local/llvm symlink. Addressing the toolchain by
    the symlink would then fall through to a system LLVM, silently defeating
    the whole point of --build-llvm."""
    path = p.private_llvm_config("23.1.0")
    check("versioned path", str(path), "/usr/local/llvm-23.1.0/bin/llvm-config")
    check("not the symlink", "/usr/local/llvm/bin" in str(path), False)


def test_find_llvm_config_prefers_a_private_build(monkeypatched=None):
    """A private LLVM must win over any system toolchain, so that later
    PostgreSQL rebuilds keep using it without extra flags."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        make_llvm_tree(base / "llvm")
        private = base / "llvm" / "bin" / "llvm-config"

        saved = p.INSTALL_BASE
        p.INSTALL_BASE = base
        try:
            check("private build wins", p.find_llvm_config(),
                  str(private.resolve()))
        finally:
            p.INSTALL_BASE = saved


# ---------------------------------------------------------------------------
# Does an existing PostgreSQL get rebuilt against a different LLVM?
# ---------------------------------------------------------------------------

def test_rebuild_forced_when_llvm_toolchain_differs():
    """The regression: an existing build already carrying --with-llvm was
    treated as up to date, so './pginstall.py --build-llvm' could build LLVM for
    an hour and leave PostgreSQL linked to the system runtime."""
    existing = configure_line("--with-llvm", "LLVM_CONFIG=/usr/bin/llvm-config")
    required = ["--with-llvm", "LLVM_CONFIG=/usr/local/llvm-23.1.0/bin/llvm-config"]

    saved = p.get_pg_configure_flags
    p.get_pg_configure_flags = lambda path: existing
    try:
        missing = p.check_pg_needs_rebuild(Path("/usr/local/postgresql-18.6"), required)
        check("toolchain change forces a rebuild", missing,
              ["LLVM_CONFIG=/usr/local/llvm-23.1.0/bin/llvm-config"])
    finally:
        p.get_pg_configure_flags = saved


def test_no_rebuild_when_llvm_toolchain_matches():
    same = "LLVM_CONFIG=/usr/local/llvm-23.1.0/bin/llvm-config"
    existing = configure_line("--with-llvm", same)

    saved = p.get_pg_configure_flags
    p.get_pg_configure_flags = lambda path: existing
    try:
        check("identical toolchain needs no rebuild",
              p.check_pg_needs_rebuild(Path("/x"), ["--with-llvm", same]), [])
    finally:
        p.get_pg_configure_flags = saved


def test_rebuild_forced_when_llvm_was_never_enabled():
    saved = p.get_pg_configure_flags
    p.get_pg_configure_flags = lambda path: configure_line()
    try:
        check("non-JIT build needs a rebuild",
              p.check_pg_needs_rebuild(Path("/x"), ["--with-llvm"]), ["--with-llvm"])
    finally:
        p.get_pg_configure_flags = saved


def test_rebuild_check_reports_undetermined_rather_than_assuming_fine():
    """The regression: get_pg_configure_flags() failing (pg_config errors, times
    out, etc.) used to be silently treated as 'nothing missing', so a broken
    check could skip a needed rebuild without telling anyone."""
    saved = p.get_pg_configure_flags
    p.get_pg_configure_flags = lambda path: None
    try:
        check("undetermined is distinct from 'nothing missing'",
              p.check_pg_needs_rebuild(Path("/x"), ["--with-llvm"]), None)
    finally:
        p.get_pg_configure_flags = saved


def test_build_postgresql_requires_the_concrete_llvm_identity():
    """End of the chain: build_postgresql must ask for the specific
    LLVM_CONFIG, not merely for --with-llvm. Requiring only the flag is what let
    an existing system-LLVM build look up to date."""
    seen = {}

    def fake_check(install_path, required_flags):
        seen["flags"] = required_flags
        return []          # "nothing missing" -> returns early, which is fine

    saved = (p.check_existing, p.check_pg_needs_rebuild, p.find_llvm_config)
    p.check_existing = lambda path: True
    p.check_pg_needs_rebuild = fake_check
    # If the toolchain was passed in, discovery must not run at all.
    def no_discovery():
        raise AssertionError("find_llvm_config() called despite an explicit toolchain")
    p.find_llvm_config = no_discovery
    try:
        p.build_postgresql(
            "18.6", dry_run=True, with_llvm=True, no_alias=True,
            llvm_config="/usr/local/llvm-23.1.0/bin/llvm-config",
        )
    except AssertionError as exc:
        check("explicit toolchain is not re-resolved", str(exc), "(not called)")
    finally:
        p.check_existing, p.check_pg_needs_rebuild, p.find_llvm_config = saved

    check("--with-llvm required", "--with-llvm" in seen.get("flags", []), True)
    check("concrete LLVM_CONFIG required",
          "LLVM_CONFIG=/usr/local/llvm-23.1.0/bin/llvm-config" in seen.get("flags", []),
          True)


def test_private_build_is_rediscovered_without_an_alias():
    """--build-llvm --no-alias creates no /usr/local/llvm symlink. If discovery
    only looked there, a later ordinary rebuild would silently fall back to the
    system toolchain the private build exists to avoid."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        for version in ("20.1.8", "23.1.0"):
            make_llvm_tree(base / f"llvm-{version}")
        # Deliberately no 'llvm' symlink, as --no-alias leaves it.
        check("no alias present", (base / "llvm").exists(), False)

        saved = p.INSTALL_BASE
        p.INSTALL_BASE = base
        try:
            got = p.find_llvm_config()
            check("finds the versioned private build", got,
                  str((base / "llvm-23.1.0" / "bin" / "llvm-config").resolve()))
        finally:
            p.INSTALL_BASE = saved


def test_private_rediscovery_prefers_the_newest_version():
    """Numeric ordering, so llvm-9 cannot outrank llvm-23."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        for version in ("9.0.1", "23.1.0"):
            make_llvm_tree(base / f"llvm-{version}")

        saved = p.INSTALL_BASE
        p.INSTALL_BASE = base
        try:
            check("23 beats 9", p.find_llvm_config(),
                  str((base / "llvm-23.1.0" / "bin" / "llvm-config").resolve()))
        finally:
            p.INSTALL_BASE = saved


# ---------------------------------------------------------------------------
# Is a half-installed LLVM mistaken for a finished one?
# ---------------------------------------------------------------------------

def make_llvm_tree(base, with_clang=True, with_runtime=True):
    (base / "bin").mkdir(parents=True, exist_ok=True)
    (base / "lib").mkdir(parents=True, exist_ok=True)
    (base / "bin" / "llvm-config").write_text("#!/bin/sh\n")
    (base / "bin" / "llvm-config").chmod(0o755)
    if with_clang:
        (base / "bin" / "clang").write_text("#!/bin/sh\n")
        (base / "bin" / "clang").chmod(0o755)
    if with_runtime:
        (base / "lib" / "libLLVM.so.23.1").write_text("")
    return base


def test_find_llvm_runtime_libs_excludes_the_c_api_wrapper():
    with tempfile.TemporaryDirectory() as tmp:
        libdir = Path(tmp)
        (libdir / "libLLVM.so.23.1").write_text("")
        (libdir / "libLLVM-C.so.23.1").write_text("")
        found = [f.name for f in p.find_llvm_runtime_libs(libdir)]
        check("real runtime found", "libLLVM.so.23.1" in found, True)
        check("C API wrapper excluded", "libLLVM-C.so.23.1" in found, False)


def test_find_llvm_runtime_libs_ignores_dangling_symlinks():
    with tempfile.TemporaryDirectory() as tmp:
        libdir = Path(tmp)
        (libdir / "libLLVM.so.23.1").symlink_to(libdir / "does-not-exist")
        check("dangling symlink not returned",
              p.find_llvm_runtime_libs(libdir), [])


def test_complete_llvm_install_is_recognised():
    with tempfile.TemporaryDirectory() as tmp:
        tree = make_llvm_tree(Path(tmp) / "llvm-23.1.0")
        check("complete install", p.llvm_install_is_complete(tree), True)


def test_partial_llvm_install_is_rejected():
    """A failed 'cmake --install' leaves the directory behind. Treating that as
    success makes every retry a no-op and can alias a broken tree."""
    with tempfile.TemporaryDirectory() as tmp:
        no_clang = make_llvm_tree(Path(tmp) / "a", with_clang=False)
        check("missing clang rejected", p.llvm_install_is_complete(no_clang), False)

        no_runtime = make_llvm_tree(Path(tmp) / "b", with_runtime=False)
        check("missing shared runtime rejected",
              p.llvm_install_is_complete(no_runtime), False)

        check("empty directory rejected",
              p.llvm_install_is_complete(Path(tmp) / "nonexistent"), False)


# ---------------------------------------------------------------------------
# Is the private LLVM runtime embedded into the PostgreSQL install?
# ---------------------------------------------------------------------------

def _embed_test_setup(tmp, with_llvmjit=True, with_runtime_lib=True):
    pkglibdir = Path(tmp) / "pkglibdir"
    pkglibdir.mkdir()
    if with_llvmjit:
        (pkglibdir / "llvmjit.so").write_text("")
    llvm_libdir = Path(tmp) / "llvm-23.1.0" / "lib"
    llvm_libdir.mkdir(parents=True)
    if with_runtime_lib:
        (llvm_libdir / "libLLVM.so.23.1").write_text("")
    return pkglibdir, llvm_libdir


def test_embed_skips_on_darwin():
    """patchelf's rpath rewriting is ELF-specific; on macOS the build-time
    absolute rpath (install_name) must be left alone, not overwritten."""
    with tempfile.TemporaryDirectory() as tmp:
        pkglibdir, llvm_libdir = _embed_test_setup(tmp)
        saved = (p.get_platform, p.run_build_cmd)
        p.get_platform = lambda: "darwin"
        p.run_build_cmd = lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("run_build_cmd called on darwin"))
        try:
            p.embed_private_llvm_runtime(pkglibdir, llvm_libdir, verbose=False)
        finally:
            p.get_platform, p.run_build_cmd = saved


def test_embed_warns_without_acting_when_llvmjit_missing():
    with tempfile.TemporaryDirectory() as tmp:
        pkglibdir, llvm_libdir = _embed_test_setup(tmp, with_llvmjit=False)
        saved = (p.get_platform, p.run_build_cmd)
        p.get_platform = lambda: "linux"
        p.run_build_cmd = lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("run_build_cmd called with no llvmjit.so present"))
        try:
            p.embed_private_llvm_runtime(pkglibdir, llvm_libdir, verbose=False)
        finally:
            p.get_platform, p.run_build_cmd = saved


def test_embed_warns_without_acting_when_no_runtime_lib():
    with tempfile.TemporaryDirectory() as tmp:
        pkglibdir, llvm_libdir = _embed_test_setup(tmp, with_runtime_lib=False)
        saved = (p.get_platform, p.run_build_cmd)
        p.get_platform = lambda: "linux"
        p.run_build_cmd = lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("run_build_cmd called with no runtime library present"))
        try:
            p.embed_private_llvm_runtime(pkglibdir, llvm_libdir, verbose=False)
        finally:
            p.get_platform, p.run_build_cmd = saved


def test_embed_warns_without_acting_when_patchelf_missing():
    with tempfile.TemporaryDirectory() as tmp:
        pkglibdir, llvm_libdir = _embed_test_setup(tmp)
        saved = (p.get_platform, p.find_system_patchelf, p.run_build_cmd)
        p.get_platform = lambda: "linux"
        p.find_system_patchelf = lambda: None
        p.run_build_cmd = lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("run_build_cmd called with no patchelf available"))
        try:
            p.embed_private_llvm_runtime(pkglibdir, llvm_libdir, verbose=False)
        finally:
            p.get_platform, p.find_system_patchelf, p.run_build_cmd = saved


def test_embed_copies_runtime_and_rpaths_to_origin():
    """The whole point: llvmjit.so ends up carrying its own copy of the LLVM
    runtime, rpathed to $ORIGIN, so deleting the private LLVM tree later can't
    silently break JIT the way a retired system LLVM does."""
    with tempfile.TemporaryDirectory() as tmp:
        pkglibdir, llvm_libdir = _embed_test_setup(tmp)
        calls = []
        saved = (p.get_platform, p.find_system_patchelf, p.run_build_cmd)
        p.get_platform = lambda: "linux"
        p.find_system_patchelf = lambda: "/usr/bin/patchelf"
        p.run_build_cmd = lambda cmd, **kw: calls.append(cmd)
        try:
            p.embed_private_llvm_runtime(pkglibdir, llvm_libdir, verbose=False)
        finally:
            p.get_platform, p.find_system_patchelf, p.run_build_cmd = saved

        copy_calls = [c for c in calls if c[:2] == ["sudo", "cp"]]
        patch_calls = [c for c in calls if "patchelf" in c[0]]
        check("copies the runtime library", len(copy_calls), 1)
        check("copy source is the private LLVM's lib", copy_calls[0][3],
              str(llvm_libdir / "libLLVM.so.23.1"))
        check("copy destination is pkglibdir", copy_calls[0][4],
              str(pkglibdir / "libLLVM.so.23.1"))
        check("patchelf sets rpath to $ORIGIN", patch_calls[0][1:],
              ["--set-rpath", "$ORIGIN", str(pkglibdir / "llvmjit.so")])


def test_incomplete_private_build_is_not_selected():
    """An interrupted 'cmake --install' leaves a newer tree with llvm-config but
    no clang. Preferring it by version alone would fail the PostgreSQL build
    later, having passed over a complete older install."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        make_llvm_tree(base / "llvm-20.1.8")                       # complete
        make_llvm_tree(base / "llvm-23.1.0", with_clang=False)     # interrupted

        saved = p.INSTALL_BASE
        p.INSTALL_BASE = base
        try:
            check("skips the incomplete newer tree", p.find_llvm_config(),
                  str((base / "llvm-20.1.8" / "bin" / "llvm-config").resolve()))
        finally:
            p.INSTALL_BASE = saved


def test_pinned_versions_need_no_network():
    """Pinning exists to avoid depending on upstream availability. Detecting
    everything first and overriding afterwards meant a fully pinned config
    still failed offline or when GitHub rate-limited."""
    def explode():
        raise AssertionError("upstream was queried despite a pinned version")

    saved = {name: getattr(p, name) for name in
             ("get_latest_openssl_version", "get_latest_icu_version",
              "get_latest_postgresql_version", "get_latest_q3c_version",
              "get_latest_llvm_version", "get_latest_readline_version")}
    for name in saved:
        setattr(p, name, explode)

    with tempfile.TemporaryDirectory() as tmp:
        config = Path(tmp) / "pginstall.conf"
        config.write_text(
            "[versions]\n"
            "postgresql = 18.6\nopenssl = 3.6.1\nicu = 76.1\n"
            "q3c = 2.0.1\nllvm = 20.1.8\nreadline = 8.2\n"
        )
        try:
            versions = p.load_versions(config, exclude_ast=True, build_llvm=True)
            check("llvm pinned without a network call", versions["llvm"], "20.1.8")
            check("postgresql pinned", versions["postgresql"], "18.6")
        except AssertionError as exc:
            check("no upstream query for pinned versions", str(exc), "(none)")
        finally:
            for name, fn in saved.items():
                setattr(p, name, fn)


def test_non_executable_binaries_are_not_a_complete_install():
    """A file is not a program. An interrupted install can leave a
    non-executable stub that PostgreSQL cannot run."""
    with tempfile.TemporaryDirectory() as tmp:
        tree = make_llvm_tree(Path(tmp) / "llvm-23.1.0")
        check("baseline is complete", p.llvm_install_is_complete(tree), True)

        (tree / "bin" / "clang").chmod(0o644)
        check("non-executable clang rejected",
              p.llvm_install_is_complete(tree), False)

        (tree / "bin" / "clang").chmod(0o755)
        (tree / "bin" / "llvm-config").chmod(0o644)
        check("non-executable llvm-config rejected",
              p.llvm_install_is_complete(tree), False)


def test_component_build_does_not_query_unrelated_upstreams():
    """'--component llvm' must not fail because an unrelated upstream is
    unreachable or rate-limiting."""
    def explode(name):
        def boom():
            raise AssertionError(f"queried {name} for --component llvm")
        return boom

    saved = {}
    for name in ("get_latest_openssl_version", "get_latest_icu_version",
                 "get_latest_postgresql_version", "get_latest_q3c_version",
                 "get_latest_ast_version", "get_latest_pgast_version"):
        saved[name] = getattr(p, name)
        setattr(p, name, explode(name))
    saved["get_latest_llvm_version"] = p.get_latest_llvm_version
    p.get_latest_llvm_version = lambda: "23.1.0"

    try:
        versions = p.load_versions(None, build_llvm=True, component="llvm")
        check("only llvm resolved", sorted(versions), ["llvm"])
        check("llvm version present", versions["llvm"], "23.1.0")
    except AssertionError as exc:
        check("no unrelated upstream queried", str(exc), "(none)")
    finally:
        for name, fn in saved.items():
            setattr(p, name, fn)


def test_postgresql_component_with_build_llvm_resolves_the_llvm_version():
    """Regression: trimming version discovery to the selected component removed
    the LLVM version that '--component postgresql --build-llvm' needs to locate
    the private toolchain, crashing the run with KeyError: 'llvm'."""
    saved = {}
    for name in ("get_latest_openssl_version", "get_latest_icu_version",
                 "get_latest_q3c_version", "get_latest_ast_version",
                 "get_latest_pgast_version"):
        saved[name] = getattr(p, name)
        setattr(p, name, lambda: (_ for _ in ()).throw(
            AssertionError("unrelated upstream queried")))
    saved["get_latest_postgresql_version"] = p.get_latest_postgresql_version
    saved["get_latest_llvm_version"] = p.get_latest_llvm_version
    p.get_latest_postgresql_version = lambda: "18.6"
    p.get_latest_llvm_version = lambda: "23.1.0"

    try:
        versions = p.load_versions(None, build_llvm=True, component="postgresql")
        check("llvm version resolved", versions.get("llvm"), "23.1.0")
        check("postgresql version resolved", versions.get("postgresql"), "18.6")
        check("still no unrelated versions", sorted(versions),
              ["llvm", "postgresql"])
        # And without --build-llvm it must stay trimmed.
        plain = p.load_versions(None, build_llvm=False, component="postgresql")
        check("no llvm when not building it", sorted(plain), ["postgresql"])
    finally:
        for name, fn in saved.items():
            setattr(p, name, fn)


def test_skip_extensions_avoids_their_upstream_queries():
    """A full run with --skip-extensions never builds q3c/ast/pgast, so it must
    not depend on their upstreams being reachable either."""
    saved = {}
    for name in ("get_latest_q3c_version", "get_latest_ast_version",
                 "get_latest_pgast_version"):
        saved[name] = getattr(p, name)
        setattr(p, name, lambda: (_ for _ in ()).throw(
            AssertionError("skipped extension's upstream queried")))
    saved["get_latest_openssl_version"] = p.get_latest_openssl_version
    saved["get_latest_icu_version"] = p.get_latest_icu_version
    saved["get_latest_postgresql_version"] = p.get_latest_postgresql_version
    saved["get_latest_readline_version"] = p.get_latest_readline_version
    p.get_latest_openssl_version = lambda: "3.6.1"
    p.get_latest_icu_version = lambda: "76.1"
    p.get_latest_postgresql_version = lambda: "18.6"
    p.get_latest_readline_version = lambda: "8.2"  # only queried on macOS

    try:
        versions = p.load_versions(None, skip_extensions=True)
        expected = ["icu", "openssl", "postgresql"]
        if p.get_platform() == "darwin":
            expected.append("readline")
        check("no q3c/ast/pgast when skipped", sorted(versions), sorted(expected))

        # An explicit '--component q3c' still wins over --skip-extensions,
        # matching the dispatch in main() that builds it regardless.
        p.get_latest_q3c_version = lambda: "2.0.1"
        explicit = p.load_versions(None, skip_extensions=True, component="q3c")
        check("explicit component overrides --skip-extensions",
              explicit.get("q3c"), "2.0.1")
    finally:
        for name, fn in saved.items():
            setattr(p, name, fn)


def test_existing_private_llvm_config_rejects_a_system_toolchain():
    """Regression: '--component postgresql --build-llvm' used to require
    load_versions()'s network-detected *latest* LLVM release to exist on disk,
    breaking every run once upstream shipped a newer release than what was
    actually built. It must discover whatever private LLVM exists instead --
    but never accept a system one as a substitute."""
    saved = (p.find_llvm_config, p.INSTALL_BASE)
    p.INSTALL_BASE = Path("/usr/local")
    try:
        p.find_llvm_config = lambda: "/usr/local/llvm-20.1.8/bin/llvm-config"
        check("private build accepted", p.find_existing_private_llvm_config(),
              "/usr/local/llvm-20.1.8/bin/llvm-config")

        p.find_llvm_config = lambda: "/usr/bin/llvm-config"
        check("system toolchain rejected",
              p.find_existing_private_llvm_config(), None)

        p.find_llvm_config = lambda: None
        check("nothing found is None", p.find_existing_private_llvm_config(), None)
    finally:
        p.find_llvm_config, p.INSTALL_BASE = saved


def test_shared_runtime_must_be_a_real_file():
    """A glob match is not a usable library: an interrupted install can leave a
    directory or a dangling symlink with a matching name."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)

        as_dir = make_llvm_tree(base / "a", with_runtime=False)
        (as_dir / "lib" / "libLLVM.so.23.1").mkdir()
        check("directory is not a runtime", p.llvm_install_is_complete(as_dir), False)

        dangling = make_llvm_tree(base / "b", with_runtime=False)
        (dangling / "lib" / "libLLVM.so.23.1").symlink_to(base / "gone.so")
        check("dangling symlink is not a runtime",
              p.llvm_install_is_complete(dangling), False)

        c_only = make_llvm_tree(base / "c", with_runtime=False)
        (c_only / "lib" / "libLLVM-C.so").write_text("")
        check("libLLVM-C alone does not count",
              p.llvm_install_is_complete(c_only), False)


# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------

def test_build_llvm_requires_a_cxx_toolchain():
    """The documented Linux prerequisites install a C compiler only; LLVM is
    C++ and also needs cmake and ninja."""
    plain = p.get_required_tools(exclude_ast=True)
    with_llvm = p.get_required_tools(exclude_ast=True, build_llvm=True)
    for tool in ("cmake", "ninja", "c++"):
        check(f"{tool} required with --build-llvm", tool in with_llvm, True)
        check(f"{tool} not required otherwise", tool in plain, False)


def test_find_llvm_config_resolves_the_alias_to_a_versioned_path():
    """PostgreSQL bakes this path into its rpath. Returning the mutable
    /usr/local/llvm alias would mean repointing it at a newer LLVM breaks an
    existing build -- the same "runtime moved underneath us" failure this
    feature exists to prevent, self-inflicted."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        make_llvm_tree(base / "llvm-23.1.0")
        (base / "llvm").symlink_to(base / "llvm-23.1.0")

        saved = p.INSTALL_BASE
        p.INSTALL_BASE = base
        try:
            got = p.find_llvm_config()
            check("resolved to the versioned path", got,
                  str((base / "llvm-23.1.0" / "bin" / "llvm-config").resolve()))
            check("alias not returned", "/llvm/bin/" in got, False)
        finally:
            p.INSTALL_BASE = saved


def test_prerequisite_commands_use_real_package_names():
    """c++ and ninja are tool names, not packages. Falling through literally
    produced install commands naming packages that do not exist."""
    tools = ["cmake", "ninja", "c++"]

    apt = p.get_package_names_for_tools(tools, "apt")
    check("apt: ninja-build", "ninja-build" in apt, True)
    check("apt: no literal ninja", "ninja" in apt, False)
    # build-essential supplies g++ on Debian/Ubuntu.
    check("apt: c++ covered", ("g++" in apt) or ("build-essential" in apt), True)
    check("apt: no literal c++", "c++" in apt, False)

    dnf = p.get_package_names_for_tools(tools, "dnf")
    check("dnf: gcc-c++", "gcc-c++" in dnf, True)
    check("dnf: ninja-build", "ninja-build" in dnf, True)
    check("dnf: no literal c++", "c++" in dnf, False)


# ---------------------------------------------------------------------------
# Release selection
# ---------------------------------------------------------------------------

def test_latest_llvm_version_skips_prereleases():
    releases = [
        {"tag_name": "llvmorg-24.1.0-rc1", "prerelease": True},
        {"tag_name": "llvmorg-23.1.0-rc3", "prerelease": False},
        {"tag_name": "llvmorg-23.1.0", "prerelease": False},
    ]
    saved = p.github_api_get
    p.github_api_get = lambda path: releases
    try:
        check("picks the newest stable release", p.get_latest_llvm_version(), "23.1.0")
    finally:
        p.github_api_get = saved


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
