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
        private = base / "llvm" / "bin" / "llvm-config"
        private.parent.mkdir(parents=True)
        private.write_text("#!/bin/sh\n")
        private.chmod(0o755)

        saved = p.INSTALL_BASE
        p.INSTALL_BASE = base
        try:
            check("private build wins", p.find_llvm_config(), str(private))
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


# ---------------------------------------------------------------------------
# Is a half-installed LLVM mistaken for a finished one?
# ---------------------------------------------------------------------------

def make_llvm_tree(base, with_clang=True, with_runtime=True):
    (base / "bin").mkdir(parents=True, exist_ok=True)
    (base / "lib").mkdir(parents=True, exist_ok=True)
    (base / "bin" / "llvm-config").write_text("#!/bin/sh\n")
    if with_clang:
        (base / "bin" / "clang").write_text("#!/bin/sh\n")
    if with_runtime:
        (base / "lib" / "libLLVM.so.23.1").write_text("")
    return base


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
