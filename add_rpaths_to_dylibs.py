#!/usr/bin/env python3

# Cross-platform helper that prints commands to fix up rpaths.
# - macOS: generates install_name_tool commands to switch versioned dylibs to @rpath
#          and point internal dependencies at @rpath versions.
# - Linux: generates patchelf commands that add/set RPATH on ELF binaries or .so
#          files so they can find the supplied library directory. By default it
#          also cascades into the referenced RPATH directories to fix their
#          own RPATHs so transitive deps resolve.
# Nothing is executed directly; review the output and pipe to bash when ready.

import argparse
import re
import subprocess
import sys
from pathlib import Path


# --------------------------
# macOS helpers (@rpath work)
# --------------------------
def get_base_rpath_name_from_filename(filename_str):
    """
    Derives a base rpath name like "libNAME.MAJOR.dylib"
    from a more specific filename like "libNAME.MAJOR.MINOR.dylib".
    Example: "libicuuc.77.1.dylib" -> "libicuuc.77.dylib"
    Returns None if pattern doesn't match.
    """
    # Regex to capture (libNAME.VERSION) from libNAME.VERSION.anything.dylib or libNAME.VERSION.dylib
    # It captures up to the first version number component.
    match = re.match(r"^(lib[a-zA-Z0-9_-]+?\.[0-9]+).*", filename_str)
    if match:
        return f"{match.group(1)}.dylib"
    return None

def get_dylib_dependencies(dylib_path):
    """
    Uses otool -L to get a list of dependencies for a dylib.
    Returns a list of strings (raw paths from otool).
    """
    try:
        # Ensure dylib_path is a string for subprocess
        result = subprocess.run(['otool', '-L', str(dylib_path)], capture_output=True, text=True, check=True, encoding='utf-8')
        
        # DEBUG: Uncomment to see raw otool output for a specific library
        # print(f"#       DEBUG (otool raw for {dylib_path.name}):\n{result.stdout.strip()}", file=sys.stderr)

        lines = result.stdout.strip().split('\n')
        dependencies = []
        if len(lines) > 1:
            for line in lines[1:]: # Skip first line (library itself)
                # Ensure the line has content before trying to split
                if line.strip(): 
                    dep_path = line.strip().split(' ')[0]
                    dependencies.append(dep_path)
        return dependencies
    except subprocess.CalledProcessError as e:
        print(f"Error running otool for {dylib_path}: {e.stderr}", file=sys.stderr)
        return []
    except FileNotFoundError:
        print(f"Error: otool command not found. Ensure Xcode Command Line Tools are installed.", file=sys.stderr)
        sys.exit(1)


def process_macos(lib_dir_path: Path):
    print(f"# Processing macOS dylibs in: {lib_dir_path}")

    dylib_to_modify_info = {}
    rpath_id_lookup = {}

    # Identify versioned dylibs we should modify.
    for item in lib_dir_path.iterdir():
        if item.name.startswith('lib') and item.name.endswith('.dylib') and \
           not item.is_symlink() and \
           re.match(r"^lib[a-zA-Z0-9_-]+?\.[0-9]+(\.[0-9]+)*\.dylib$", item.name):

            base_name_for_rpath = get_base_rpath_name_from_filename(item.name)
            if base_name_for_rpath:
                target_rpath_id = f"@rpath/{base_name_for_rpath}"
                dylib_to_modify_info[item] = target_rpath_id
                rpath_id_lookup[base_name_for_rpath] = target_rpath_id
                rpath_id_lookup[item.name] = target_rpath_id
            else:
                print(f"# Warning: Could not determine base rpath name for {item.name}", file=sys.stderr)

    if not dylib_to_modify_info:
        print(f"No modifiable versioned dylib files found in {lib_dir_path}", file=sys.stderr)
        return

    print("\n# Phase 1 (macOS): Setting library IDs (install names)")
    for dylib_path, target_id in dylib_to_modify_info.items():
        print(f"install_name_tool -id \"{target_id}\" \"{dylib_path}\"")

    print("\n# Phase 2 (macOS): Updating internal dependencies")
    for dylib_path_to_modify, _ in dylib_to_modify_info.items():
        print(f"\n# --- Processing dependencies for: {dylib_path_to_modify} ---")
        dependencies = get_dylib_dependencies(dylib_path_to_modify)

        for dep_str_from_otool in dependencies:
            if dep_str_from_otool.startswith(('/usr/lib/', '/System/', '@executable_path/', '@loader_path/', '@rpath/')):
                continue

            dep_basename = Path(dep_str_from_otool).name
            if dep_basename in rpath_id_lookup:
                target_rpath_for_dep = rpath_id_lookup[dep_basename]
                print(f"install_name_tool -change \"{dep_str_from_otool}\" \"{target_rpath_for_dep}\" \"{dylib_path_to_modify}\"")

    print("\n# macOS finished.")
    print("# Review the commands above. If they look correct, you can pipe the output of this script to bash:")
    print(f"#   {Path(sys.argv[0]).name} {lib_dir_path} | bash")


# ----------------------
# Linux helpers (patchelf)
# ----------------------
def is_elf_binary(file_path: Path) -> bool:
    """Use `file` to check whether a path is an ELF binary."""
    try:
        result = subprocess.run(['file', '-b', str(file_path)], capture_output=True, text=True, check=True)
        return "ELF" in result.stdout
    except subprocess.CalledProcessError as exc:
        print(f"# Warning: Could not inspect {file_path}: {exc.stderr.strip()}", file=sys.stderr)
        return False
    except FileNotFoundError:
        print("Error: `file` command not found; install it to detect ELF binaries.", file=sys.stderr)
        sys.exit(1)


def get_existing_rpath(elf_path: Path) -> str:
    """Return the current RPATH/RUNPATH via patchelf."""
    try:
        result = subprocess.run(['patchelf', '--print-rpath', str(elf_path)], capture_output=True, text=True, check=True)
        return result.stdout.strip()
    except subprocess.CalledProcessError as exc:
        print(f"# Warning: patchelf --print-rpath failed for {elf_path}: {exc.stderr.strip()}", file=sys.stderr)
        return ""
    except FileNotFoundError:
        print("Error: patchelf not found. Install patchelf to modify ELF rpaths.", file=sys.stderr)
        sys.exit(1)


def emit_patchelf_commands(elf_files: list[Path], rpath_to_add: str, force_rpath: bool):
    """Emit patchelf commands for given ELF files."""
    elf_files = [
        p for p in elf_files
        if p.is_file() and not p.is_symlink() and is_elf_binary(p)
    ]

    for elf_path in elf_files:
        existing_rpath = get_existing_rpath(elf_path)
        rpath_parts = [part for part in existing_rpath.split(':') if part] if existing_rpath else []

        desired_parts = set(rpath_parts)
        desired_parts.add(rpath_to_add)

        # Preserve existing order, then append new paths in deterministic order.
        new_rpath_parts = [part for part in rpath_parts if part in desired_parts]
        for part in sorted(desired_parts):
            if part not in new_rpath_parts:
                new_rpath_parts.append(part)

        new_rpath = ':'.join(new_rpath_parts)

        if new_rpath == existing_rpath:
            print(f"# Skipping {elf_path} (already has desired RPATH)")
            continue

        force_flag = "--force-rpath " if force_rpath else ""
        print(f"patchelf {force_flag}--set-rpath \"{new_rpath}\" \"{elf_path}\"")


def process_linux(target_dir: Path, rpath_to_add: str, force_rpath: bool, cascade_libs: bool):
    print(f"# Processing ELF binaries in: {target_dir}")
    print(f"# RPATH to ensure: {rpath_to_add}")

    target_elves = [
        p for p in target_dir.iterdir()
        if p.is_file() and not p.is_symlink()
    ]

    if not target_elves:
        print(f"No ELF files found in {target_dir}", file=sys.stderr)
    else:
        emit_patchelf_commands(target_elves, rpath_to_add, force_rpath)

    if cascade_libs:
        for rdir in [Path(part) for part in rpath_to_add.split(':') if part]:
            if not rdir.is_dir():
                print(f"# Skipping cascade into {rdir} (not a directory)", file=sys.stderr)
                continue
            print(f"\n# Cascading into dependency directory: {rdir}")
            rdir_elves = [
                p for p in rdir.iterdir()
                if p.is_file() and not p.is_symlink()
            ]
            if not rdir_elves:
                print(f"# No ELF files found in {rdir}", file=sys.stderr)
                continue
            emit_patchelf_commands(rdir_elves, str(rdir), force_rpath)

    print("\n# Linux finished.")
    print("# Review the commands above. If they look correct, you can pipe the output of this script to bash:")
    print(f"#   {Path(sys.argv[0]).name} {target_dir} --rpath \"{rpath_to_add}\" | bash")


# -----------
# Entry point
# -----------
def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate rpath-fixing commands (macOS install_name_tool or Linux patchelf)."
    )
    parser.add_argument(
        "directory",
        help="Directory containing dylibs (macOS) or ELF binaries/libs (Linux) to process."
    )
    parser.add_argument(
        "--rpath",
        help="Linux: RPATH to add/set. Defaults to the provided directory path."
    )
    parser.add_argument(
        "--force-rpath",
        action="store_true",
        help="Linux: pass --force-rpath to patchelf."
    )
    parser.add_argument(
        "--no-cascade",
        action="store_true",
        help="Linux: skip patching ELF files inside the RPATH directories (default is to cascade)."
    )
    return parser.parse_args()


def main():
    args = parse_args()
    target_dir = Path(args.directory).resolve()

    if not target_dir.is_dir():
        print(f"Error: Directory '{target_dir}' not found.", file=sys.stderr)
        sys.exit(1)

    if sys.platform == "darwin":
        process_macos(target_dir)
    elif sys.platform.startswith("linux"):
        rpath_to_add = args.rpath if args.rpath else str(target_dir)
        process_linux(target_dir, rpath_to_add, args.force_rpath, not args.no_cascade)
    else:
        print(f"Unsupported platform: {sys.platform}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
