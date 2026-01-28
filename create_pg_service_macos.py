#!/usr/bin/env python3
"""
Create and enable a PostgreSQL launchd service on macOS.

This script creates a launchd plist file for PostgreSQL,
allowing multiple instances to run simultaneously. Each instance is
identified by a name (e.g., "main", "dev", "test").

Usage:
    sudo ./create_pg_service_macos.py main              # Create instance "main"
    sudo ./create_pg_service_macos.py dev --port 5433   # Create instance "dev" on port 5433

    sudo launchctl kickstart system/com.postgresql.main
    sudo launchctl kickstart system/com.postgresql.dev
"""

import argparse
import grp
import os
import plistlib
import pwd
import subprocess
import sys
from pathlib import Path

PG_BASE = Path("/usr/local/postgresql")
PG_BIN = PG_BASE / "bin"
PG_CONFIG_BASE = Path("/usr/local/etc/postgresql")
LAUNCHD_PATH = Path("/Library/LaunchDaemons")


def get_plist_label(instance: str) -> str:
    """Get the launchd service label for an instance."""
    return f"com.postgresql.{instance}"


def get_plist_path(instance: str) -> Path:
    """Get the plist file path for an instance."""
    return LAUNCHD_PATH / f"{get_plist_label(instance)}.plist"


def check_root():
    """Check if running as root (required for launchd operations)."""
    if os.geteuid() != 0:
        print("Root privileges required. Run with sudo:")
        print(f"  sudo {' '.join(sys.argv)}")
        sys.exit(1)


def check_macos():
    """Check if running on macOS."""
    if sys.platform != "darwin":
        if sys.platform == "linux":
            print("This script creates launchd services for macOS.")
            print("For Linux, use create_pg_service.py instead.")
        else:
            print("This script is macOS-specific (requires launchd).", file=sys.stderr)
        sys.exit(1)


def check_postgresql_installed():
    """Check if PostgreSQL is installed."""
    pg_ctl = PG_BIN / "pg_ctl"
    if not pg_ctl.exists():
        print(f"Error: PostgreSQL not found at {PG_BASE}", file=sys.stderr)
        print("Run pginstall.py first to install PostgreSQL.", file=sys.stderr)
        sys.exit(1)


def get_real_user() -> str:
    """Get the real user who invoked sudo (not root)."""
    # SUDO_USER is set when running via sudo
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        return sudo_user
    # Fallback to current user if not running via sudo
    return pwd.getpwuid(os.getuid()).pw_name


def user_exists(username: str) -> bool:
    """Check if a user exists on macOS."""
    result = subprocess.run(
        ["dscl", ".", "-read", f"/Users/{username}"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def group_exists(groupname: str) -> bool:
    """Check if a group exists on macOS."""
    result = subprocess.run(
        ["dscl", ".", "-read", f"/Groups/{groupname}"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def user_in_group(username: str, groupname: str) -> bool:
    """Check if a user is a member of a group."""
    try:
        gr = grp.getgrnam(groupname)
        if username in gr.gr_mem:
            return True
        # Also check if it's the user's primary group
        pw = pwd.getpwnam(username)
        return pw.pw_gid == gr.gr_gid
    except KeyError:
        return False


def add_user_to_group(username: str, groupname: str):
    """Add a user to a group on macOS."""
    print(f"  Adding '{username}' to group '{groupname}'...")
    result = subprocess.run(
        ["dseditgroup", "-o", "edit", "-a", username, "-t", "user", groupname],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  Warning: Could not add user to group: {result.stderr}", file=sys.stderr)
    else:
        print(f"  Added '{username}' to group '{groupname}'.")
        print(f"  Note: You may need to log out and back in for group membership to take effect.")


def get_next_uid() -> int:
    """Get the next available UID for a system user (starting at 400)."""
    # System users on macOS typically use UIDs below 500
    # Start at 400 to avoid conflicts
    result = subprocess.run(
        ["dscl", ".", "-list", "/Users", "UniqueID"],
        capture_output=True,
        text=True,
    )
    used_uids = set()
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2:
            try:
                used_uids.add(int(parts[1]))
            except ValueError:
                pass
    for uid in range(400, 500):
        if uid not in used_uids:
            return uid
    raise RuntimeError("No available UID in range 400-499")


def get_next_gid() -> int:
    """Get the next available GID for a system group (starting at 400)."""
    result = subprocess.run(
        ["dscl", ".", "-list", "/Groups", "PrimaryGroupID"],
        capture_output=True,
        text=True,
    )
    used_gids = set()
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2:
            try:
                used_gids.add(int(parts[1]))
            except ValueError:
                pass
    for gid in range(400, 500):
        if gid not in used_gids:
            return gid
    raise RuntimeError("No available GID in range 400-499")


def ensure_user_and_group(username: str) -> tuple[str, str]:
    """Ensure the specified user exists. Returns (username, groupname)."""
    # For non-postgres users, just verify they exist
    if username != "postgres":
        if not user_exists(username):
            print(f"Error: User '{username}' does not exist.", file=sys.stderr)
            sys.exit(1)
        pw = pwd.getpwnam(username)
        gr = grp.getgrgid(pw.pw_gid)
        print(f"  Using existing user '{username}' (uid={pw.pw_uid})")
        return username, gr.gr_name

    # For postgres user, create if needed
    groupname = "postgres"

    # Check if user exists
    if user_exists(username):
        pw = pwd.getpwnam(username)
        print(f"  Using existing '{username}' user (uid={pw.pw_uid})")
        gr = grp.getgrgid(pw.pw_gid)
        return username, gr.gr_name

    print(f"  Creating system user '{username}'...")

    # Create group first if it doesn't exist
    if not group_exists(groupname):
        gid = get_next_gid()
        subprocess.run(["dscl", ".", "-create", f"/Groups/{groupname}"], check=True)
        subprocess.run(
            ["dscl", ".", "-create", f"/Groups/{groupname}", "PrimaryGroupID", str(gid)],
            check=True,
        )
        subprocess.run(
            ["dscl", ".", "-create", f"/Groups/{groupname}", "RealName", "PostgreSQL Server"],
            check=True,
        )
        print(f"  Created group '{groupname}' (gid={gid})")
    else:
        # Get existing group's GID
        result = subprocess.run(
            ["dscl", ".", "-read", f"/Groups/{groupname}", "PrimaryGroupID"],
            capture_output=True,
            text=True,
        )
        gid = int(result.stdout.split(":")[1].strip())

    # Create the user
    uid = get_next_uid()

    subprocess.run(["dscl", ".", "-create", f"/Users/{username}"], check=True)
    subprocess.run(
        ["dscl", ".", "-create", f"/Users/{username}", "UniqueID", str(uid)],
        check=True,
    )
    subprocess.run(
        ["dscl", ".", "-create", f"/Users/{username}", "PrimaryGroupID", str(gid)],
        check=True,
    )
    subprocess.run(
        ["dscl", ".", "-create", f"/Users/{username}", "UserShell", "/usr/bin/false"],
        check=True,
    )
    subprocess.run(
        ["dscl", ".", "-create", f"/Users/{username}", "RealName", "PostgreSQL Server"],
        check=True,
    )
    subprocess.run(
        ["dscl", ".", "-create", f"/Users/{username}", "NFSHomeDirectory", str(PG_BASE)],
        check=True,
    )
    # Hide user from login window
    subprocess.run(
        ["dscl", ".", "-create", f"/Users/{username}", "IsHidden", "1"],
        check=True,
    )

    print(f"  Created system user '{username}' (uid={uid})")
    return username, groupname


def prompt_for_value(prompt: str, default: str = None) -> str:
    """Prompt user for a value with optional default."""
    if default:
        user_input = input(f"{prompt} [{default}]: ").strip()
        return user_input if user_input else default
    else:
        while True:
            user_input = input(f"{prompt}: ").strip()
            if user_input:
                return user_input
            print("  Value required.")


def prompt_yes_no(prompt: str, default: bool = True) -> bool:
    """Prompt user for a yes/no answer."""
    default_str = "Y/n" if default else "y/N"
    while True:
        response = input(f"{prompt} [{default_str}]: ").strip().lower()
        if not response:
            return default
        if response in ("y", "yes"):
            return True
        if response in ("n", "no"):
            return False
        print("  Please enter 'y' or 'n'.")


def get_system_memory_gb() -> float:
    """Get total system memory in GB."""
    result = subprocess.run(
        ["sysctl", "-n", "hw.memsize"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        try:
            bytes_mem = int(result.stdout.strip())
            return bytes_mem / (1024 ** 3)
        except ValueError:
            pass
    # Default fallback
    return 8.0


def get_memory_config(profile: str, total_memory_gb: float) -> dict:
    """
    Get PostgreSQL memory configuration based on profile and available memory.

    Returns a dict with shared_buffers, work_mem, maintenance_work_mem, effective_cache_size.
    """
    # Memory values in MB
    if profile == "lite":
        # Conservative: good for development or shared systems
        # ~10% of RAM for shared_buffers
        shared_buffers = int(total_memory_gb * 1024 * 0.10)
        work_mem = 16  # MB
        maintenance_work_mem = 64  # MB
        effective_cache_size = int(total_memory_gb * 1024 * 0.25)
    elif profile == "medium":
        # Balanced: good for dedicated development machines
        # ~25% of RAM for shared_buffers
        shared_buffers = int(total_memory_gb * 1024 * 0.25)
        work_mem = 64  # MB
        maintenance_work_mem = 256  # MB
        effective_cache_size = int(total_memory_gb * 1024 * 0.50)
    elif profile == "max":
        # Aggressive: for dedicated database servers
        # ~40% of RAM for shared_buffers (PostgreSQL recommends max 40%)
        shared_buffers = int(total_memory_gb * 1024 * 0.40)
        work_mem = 256  # MB
        maintenance_work_mem = 512  # MB
        effective_cache_size = int(total_memory_gb * 1024 * 0.75)
    else:
        # Default (same as lite)
        shared_buffers = 128
        work_mem = 4
        maintenance_work_mem = 64
        effective_cache_size = int(total_memory_gb * 1024 * 0.25)

    # Ensure minimum values
    shared_buffers = max(shared_buffers, 128)
    work_mem = max(work_mem, 4)
    maintenance_work_mem = max(maintenance_work_mem, 64)
    effective_cache_size = max(effective_cache_size, 512)

    return {
        "shared_buffers": f"{shared_buffers}MB",
        "work_mem": f"{work_mem}MB",
        "maintenance_work_mem": f"{maintenance_work_mem}MB",
        "effective_cache_size": f"{effective_cache_size}MB",
    }


def prompt_memory_profile(total_memory_gb: float) -> str:
    """Prompt user to select a memory configuration profile."""
    print()
    print(f"Memory configuration (system has {total_memory_gb:.1f} GB RAM):")
    print()

    lite_config = get_memory_config("lite", total_memory_gb)
    medium_config = get_memory_config("medium", total_memory_gb)
    max_config = get_memory_config("max", total_memory_gb)

    print(f"  lite   - 10% of RAM, good for development or shared systems")
    print(f"           shared_buffers={lite_config['shared_buffers']}, work_mem={lite_config['work_mem']}")
    print()
    print(f"  medium - 25% of RAM, balanced for dedicated development machines")
    print(f"           shared_buffers={medium_config['shared_buffers']}, work_mem={medium_config['work_mem']}")
    print()
    print(f"  max    - 40% of RAM, for dedicated database servers")
    print(f"           shared_buffers={max_config['shared_buffers']}, work_mem={max_config['work_mem']}")
    print()
    print(f"  skip   - Use PostgreSQL defaults (can configure later)")
    print()

    while True:
        choice = input("Memory profile [lite/medium/max/skip] (medium): ").strip().lower()
        if not choice:
            return "medium"
        if choice in ("lite", "medium", "max", "skip"):
            return choice
        print("  Please enter 'lite', 'medium', 'max', or 'skip'.")


def apply_memory_config(pgdata: Path, memory_config: dict):
    """Apply memory configuration to postgresql.conf."""
    conf_file = pgdata / "postgresql.conf"

    if not conf_file.exists():
        print(f"  Warning: {conf_file} not found, skipping memory configuration.")
        return

    print("  Applying memory configuration...")

    # Read existing config
    content = conf_file.read_text()
    lines = content.splitlines()
    new_lines = []

    # Track which settings we've updated
    updated = set()

    for line in lines:
        # Check if this line sets one of our memory parameters
        modified = False
        for param, value in memory_config.items():
            # Match lines like "shared_buffers = 128MB" or "#shared_buffers = 128MB"
            if line.lstrip().startswith(param) or line.lstrip().startswith(f"#{param}"):
                new_lines.append(f"{param} = {value}")
                updated.add(param)
                modified = True
                break

        if not modified:
            new_lines.append(line)

    # Add any settings that weren't in the file
    for param, value in memory_config.items():
        if param not in updated:
            new_lines.append(f"{param} = {value}")

    # Write back
    conf_file.write_text("\n".join(new_lines) + "\n")

    for param, value in memory_config.items():
        print(f"    {param} = {value}")


def ensure_parent_dirs_group_accessible(path: Path, user: str, group: str):
    """Ensure parent directories are traversable by the group.

    This is needed so group members can access the data directory.
    Sets ownership and ensures group execute permission on parent dirs.
    """
    pw = pwd.getpwnam(user)
    gr = grp.getgrnam(group)

    # Collect parent directories that we might need to adjust
    # Stop at well-known system directories
    stop_dirs = {"/", "/usr", "/usr/local", "/var", "/data"}

    parents = []
    current = path.parent
    while str(current) not in stop_dirs and current != current.parent:
        parents.append(current)
        current = current.parent

    # Process from root towards the target (reverse order)
    for parent in reversed(parents):
        if parent.exists():
            # Get current permissions
            current_mode = parent.stat().st_mode
            # Ensure group has execute permission (needed to traverse)
            if not (current_mode & 0o010):  # group execute bit
                new_mode = current_mode | 0o050  # add group r-x
                parent.chmod(new_mode)
            # Set ownership if not already correct
            if parent.stat().st_uid != pw.pw_uid:
                os.chown(parent, pw.pw_uid, gr.gr_gid)


def initialize_database(pgdata: Path, user: str, group: str, allow_group_access: bool = True):
    """Initialize the database cluster if it doesn't exist."""
    if (pgdata / "PG_VERSION").exists():
        print(f"  Database already initialized at {pgdata}")
        return

    print(f"  Initializing database cluster at {pgdata}...")

    # Create directory if needed
    pgdata.mkdir(parents=True, exist_ok=True)

    # Change ownership to the PostgreSQL user
    pw = pwd.getpwnam(user)
    gr = grp.getgrnam(group)
    os.chown(pgdata, pw.pw_uid, gr.gr_gid)

    # Ensure parent directories are traversable by group
    if allow_group_access:
        ensure_parent_dirs_group_accessible(pgdata, user, group)

    # Run initdb as the target user
    # Use --allow-group-access so group members can read the data directory
    initdb = PG_BIN / "initdb"
    cmd = ["sudo", "-u", user, str(initdb), "-D", str(pgdata)]
    if allow_group_access:
        cmd.append("--allow-group-access")

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print(f"Error initializing database: {result.stderr}", file=sys.stderr)
        sys.exit(1)

    print("  Database initialized successfully.")


def create_logfile_directory(logfile: Path, user: str):
    """Create log file directory and set permissions."""
    log_dir = logfile.parent

    if not log_dir.exists():
        print(f"  Creating log directory {log_dir}...")
        log_dir.mkdir(parents=True, exist_ok=True)

    # Change ownership to the PostgreSQL user
    pw = pwd.getpwnam(user)
    os.chown(log_dir, pw.pw_uid, pw.pw_gid)

    # Create empty log file if it doesn't exist
    if not logfile.exists():
        logfile.touch()
    os.chown(logfile, pw.pw_uid, pw.pw_gid)


def create_instance_config(
    instance: str,
    pgdata: Path,
    logfile: Path,
    port: int,
) -> Path:
    """Create the instance-specific configuration directory and environment file."""
    instance_dir = PG_CONFIG_BASE / instance
    env_file = instance_dir / "postgresql.conf"

    # Create instance config directory
    instance_dir.mkdir(parents=True, exist_ok=True)

    # Write environment file (same format as Linux for consistency)
    env_content = f"""\
# PostgreSQL instance configuration: {instance}
# Created by create_pg_service_macos.py

PGDATA={pgdata}
PGPORT={port}
PGLOG={logfile}
"""

    print(f"  Creating instance config: {env_file}...")
    env_file.write_text(env_content)

    return env_file


def create_launchd_plist(
    instance: str,
    pgdata: Path,
    logfile: Path,
    port: int,
    user: str,
    group: str,
) -> Path:
    """Create the launchd plist file for the PostgreSQL instance."""
    label = get_plist_label(instance)
    plist_path = get_plist_path(instance)

    if plist_path.exists():
        print(f"  Plist already exists: {plist_path}")
        return plist_path

    # Create a wrapper script that handles the PostgreSQL lifecycle
    # This is needed because launchd expects a long-running process
    wrapper_dir = PG_CONFIG_BASE / instance
    wrapper_script = wrapper_dir / "pg_wrapper.sh"

    wrapper_content = f"""\
#!/bin/bash
# PostgreSQL wrapper script for launchd
# Instance: {instance}

export PGDATA="{pgdata}"
export PGPORT="{port}"

# Start PostgreSQL in the foreground (required for launchd)
exec "{PG_BIN}/postgres" -D "$PGDATA" -p "$PGPORT"
"""

    print(f"  Creating wrapper script: {wrapper_script}...")
    wrapper_script.write_text(wrapper_content)
    wrapper_script.chmod(0o755)

    # Change ownership of wrapper to postgres user
    pw = pwd.getpwnam(user)
    os.chown(wrapper_script, pw.pw_uid, pw.pw_gid)

    # Create the plist
    plist_content = {
        "Label": label,
        "ProgramArguments": [str(wrapper_script)],
        "UserName": user,
        "GroupName": group,
        "WorkingDirectory": str(pgdata),
        "StandardOutPath": str(logfile),
        "StandardErrorPath": str(logfile),
        "RunAtLoad": True,
        "KeepAlive": {
            "SuccessfulExit": False,  # Restart if exits with non-zero
        },
        "EnvironmentVariables": {
            "PGDATA": str(pgdata),
            "PGPORT": str(port),
        },
        # Soft resource limits
        "SoftResourceLimits": {
            "NumberOfFiles": 1024,
        },
    }

    print(f"  Creating plist: {plist_path}...")
    with open(plist_path, "wb") as f:
        plistlib.dump(plist_content, f)

    # Plist must be owned by root
    os.chown(plist_path, 0, 0)
    plist_path.chmod(0o644)

    return plist_path


def load_service(instance: str):
    """Load and start the PostgreSQL instance service."""
    label = get_plist_label(instance)
    plist_path = get_plist_path(instance)

    print(f"  Loading {label} service...")

    # Bootstrap the service (modern launchctl approach)
    result = subprocess.run(
        ["launchctl", "bootstrap", "system", str(plist_path)],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        # Try the legacy load command if bootstrap fails
        if "already loaded" in result.stderr.lower() or "already bootstrapped" in result.stderr.lower():
            print(f"  Service {label} already loaded, restarting...")
            subprocess.run(["launchctl", "kickstart", "-k", f"system/{label}"])
        else:
            # Try legacy load
            result = subprocess.run(
                ["launchctl", "load", str(plist_path)],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0 and "already loaded" not in result.stderr.lower():
                print(f"Error loading service: {result.stderr}", file=sys.stderr)
                sys.exit(1)

    print(f"  Service {label} loaded successfully.")


def show_status(instance: str):
    """Show the service status."""
    label = get_plist_label(instance)
    print("\nService status:")

    # Use launchctl print for detailed status
    result = subprocess.run(
        ["launchctl", "print", f"system/{label}"],
        capture_output=True,
        text=True,
    )

    if result.returncode == 0:
        # Extract relevant info from output
        lines = result.stdout.splitlines()
        for line in lines:
            if any(key in line.lower() for key in ["state", "pid", "last exit"]):
                print(f"  {line.strip()}")
    else:
        # Fallback to list
        result = subprocess.run(
            ["launchctl", "list", label],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            print(result.stdout)
        else:
            print(f"  Service not found or not running")


def list_instances():
    """List all configured PostgreSQL instances."""
    if not PG_CONFIG_BASE.exists():
        print("No instances configured.")
        return

    instances = [d.name for d in PG_CONFIG_BASE.iterdir() if d.is_dir()]
    if not instances:
        print("No instances configured.")
        return

    print("Configured PostgreSQL instances:")
    print()
    for instance in sorted(instances):
        env_file = PG_CONFIG_BASE / instance / "postgresql.conf"
        if env_file.exists():
            # Parse the environment file
            config = {}
            for line in env_file.read_text().splitlines():
                if "=" in line and not line.startswith("#"):
                    key, value = line.split("=", 1)
                    config[key.strip()] = value.strip()

            port = config.get("PGPORT", "?")
            pgdata = config.get("PGDATA", "?")

            # Check if running
            label = get_plist_label(instance)
            result = subprocess.run(
                ["launchctl", "print", f"system/{label}"],
                capture_output=True,
                text=True,
            )

            if result.returncode == 0:
                # Parse state from output
                status = "loaded"
                for line in result.stdout.splitlines():
                    if "state" in line.lower():
                        if "running" in line.lower():
                            status = "running"
                        break
                # Check if process is actually running
                if "pid" in result.stdout.lower():
                    status = "running"
            else:
                status = "not loaded"

            print(f"  {instance}:")
            print(f"    Status: {status}")
            print(f"    Port:   {port}")
            print(f"    Data:   {pgdata}")
            print()


def main():
    parser = argparse.ArgumentParser(
        description="Create and enable a PostgreSQL launchd service instance (macOS)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  sudo ./create_pg_service_macos.py main                    # Create "main" instance
  sudo ./create_pg_service_macos.py dev --port 5433         # Create "dev" instance on port 5433
  sudo ./create_pg_service_macos.py test --pgdata /data/test --port 5434

  sudo launchctl kickstart system/com.postgresql.main       # Start main instance
  sudo launchctl kill SIGTERM system/com.postgresql.main    # Stop main instance

  ./create_pg_service_macos.py --list                       # List all instances (no sudo needed)
""",
    )
    parser.add_argument(
        "instance",
        nargs="?",
        type=str,
        help="Instance name (e.g., 'main', 'dev', 'test')",
    )
    parser.add_argument(
        "--pgdata",
        type=str,
        help="PostgreSQL data directory (default: /usr/local/postgresql/data/<instance>)",
    )
    parser.add_argument(
        "--logfile",
        type=str,
        help="Log file location (default: /usr/local/var/log/postgresql/<instance>.log)",
    )
    parser.add_argument(
        "--port",
        type=int,
        help="Port number (default: prompt or 5432)",
    )
    parser.add_argument(
        "--user",
        type=str,
        help="User to run PostgreSQL as (default: prompt or 'postgres')",
    )
    parser.add_argument(
        "--memory",
        type=str,
        choices=["lite", "medium", "max", "skip"],
        help="Memory configuration profile (default: prompt)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List all configured instances",
    )

    args = parser.parse_args()

    # Handle --list without requiring root
    if args.list:
        list_instances()
        return

    # All checks first (before any output)
    check_macos()
    check_root()
    check_postgresql_installed()

    # One-liner description
    print("Creates a PostgreSQL launchd service for automatic startup on boot.")
    print()

    # Prompt for instance name if not provided
    if args.instance:
        instance = args.instance
    else:
        print("Instance name identifies this PostgreSQL server (e.g., 'main', 'dev', 'test').")
        print("You can run multiple instances simultaneously on different ports.")
        print()
        instance = prompt_for_value("Instance name")

    # Check if instance already exists
    instance_config = PG_CONFIG_BASE / instance / "postgresql.conf"
    plist_path = get_plist_path(instance)
    if instance_config.exists() or plist_path.exists():
        print(f"Error: Instance '{instance}' already exists.", file=sys.stderr)
        if instance_config.exists():
            print(f"  Config: {instance_config}", file=sys.stderr)
        if plist_path.exists():
            print(f"  Plist:  {plist_path}", file=sys.stderr)
        print(f"\nTo reconfigure, first remove:", file=sys.stderr)
        print(f"  sudo launchctl bootout system/{get_plist_label(instance)}", file=sys.stderr)
        print(f"  sudo rm -r {PG_CONFIG_BASE / instance}", file=sys.stderr)
        print(f"  sudo rm {plist_path}", file=sys.stderr)
        sys.exit(1)

    # Get the real user (who ran sudo)
    real_user = get_real_user()

    # Get user to run PostgreSQL as
    print()
    if args.user:
        pg_user = args.user
    else:
        print("Which user should own the database files and run the server?")
        print(f"  'postgres' - Dedicated system user (recommended for production)")
        print(f"  '{real_user}' - Your current user (convenient for development)")
        print()
        pg_user = prompt_for_value("Run PostgreSQL as user", "postgres")

    # Ensure user exists (create postgres if needed)
    user, group = ensure_user_and_group(pg_user)

    # Offer to add current user to postgres group if applicable
    if user == "postgres" and real_user != "postgres":
        if not user_in_group(real_user, "postgres"):
            print()
            print(f"Your user '{real_user}' is not in the 'postgres' group.")
            print("Adding you to this group allows you to read database files without sudo.")
            if prompt_yes_no(f"Add '{real_user}' to the 'postgres' group?", default=True):
                add_user_to_group(real_user, "postgres")

    print()
    print(f"Creating PostgreSQL instance '{instance}'")
    print()

    # Get parameters with instance-aware defaults
    # Use /usr/local/var for logs on macOS (more common convention)
    default_pgdata = f"/usr/local/postgresql/data/{instance}"
    default_logfile = f"/usr/local/var/log/postgresql/{instance}.log"

    if args.pgdata:
        pgdata = Path(args.pgdata)
    else:
        pgdata = Path(prompt_for_value("PostgreSQL data directory", default_pgdata))

    if args.logfile:
        logfile = Path(args.logfile)
    else:
        logfile = Path(prompt_for_value("Log file location", default_logfile))

    if args.port:
        port = args.port
    else:
        port = int(prompt_for_value("Port number", "5432"))

    # Memory configuration
    total_memory_gb = get_system_memory_gb()
    if args.memory:
        memory_profile = args.memory
    else:
        memory_profile = prompt_memory_profile(total_memory_gb)

    memory_config = None
    if memory_profile != "skip":
        memory_config = get_memory_config(memory_profile, total_memory_gb)

    print()
    print("Configuration:")
    print(f"  Instance:       {instance}")
    print(f"  Data directory: {pgdata}")
    print(f"  Log file:       {logfile}")
    print(f"  Port:           {port}")
    print(f"  Run as:         {user}:{group}")
    if memory_config:
        print(f"  Memory profile: {memory_profile}")
    print()

    # Create resources
    print("Setting up PostgreSQL instance...")
    create_instance_config(instance, pgdata, logfile, port)
    create_logfile_directory(logfile, user)
    initialize_database(pgdata, user, group)

    # Apply memory configuration after initdb creates postgresql.conf
    if memory_config:
        apply_memory_config(pgdata, memory_config)

    create_launchd_plist(instance, pgdata, logfile, port, user, group)
    load_service(instance)

    show_status(instance)

    label = get_plist_label(instance)
    print()
    print(f"PostgreSQL instance '{instance}' is now running.")
    print()
    print("Useful commands:")
    print(f"  sudo launchctl print system/{label}                # Check status")
    print(f"  sudo launchctl kill SIGTERM system/{label}         # Stop server")
    print(f"  sudo launchctl kickstart system/{label}            # Start server")
    print(f"  sudo launchctl kickstart -k system/{label}         # Restart server")
    print(f"  tail -f {logfile}                                  # View logs")
    print()
    print(f"  ./create_pg_service_macos.py --list                # List all instances")
    print()
    print(f"Connect with: {PG_BIN}/psql -p {port}")


if __name__ == "__main__":
    main()
