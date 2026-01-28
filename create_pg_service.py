#!/usr/bin/env python3
"""
Create and enable a PostgreSQL systemd service.

This script creates a systemd template service file for PostgreSQL,
allowing multiple instances to run simultaneously. Each instance is
identified by a name (e.g., "main", "dev", "test").

Usage:
    sudo ./create_pg_service.py main              # Create instance "main"
    sudo ./create_pg_service.py dev --port 5433   # Create instance "dev" on port 5433

    sudo systemctl start postgresql@main
    sudo systemctl start postgresql@dev
"""

import argparse
import grp
import os
import pwd
import subprocess
import sys
from pathlib import Path

PG_BASE = Path("/usr/local/postgresql")
PG_BIN = PG_BASE / "bin"
PG_CONFIG_BASE = Path("/etc/postgresql")

# Template unit file - %i is replaced by instance name by systemd
# Note: User/Group are set to postgres. For a different user, create a drop-in override:
#   sudo systemctl edit postgresql@<instance>
SYSTEMD_TEMPLATE_UNIT = """\
[Unit]
Description=PostgreSQL database server (%i instance)
Documentation=https://www.postgresql.org/docs/
After=network.target

[Service]
Type=forking
User=postgres
Group=postgres

# Load instance-specific configuration from /etc/postgresql/<instance>/postgresql.conf
# This file uses KEY=value format (same as Environment= directives, but externalized)
# Required variables: PGDATA, PGPORT, PGLOG
EnvironmentFile=/etc/postgresql/%i/postgresql.conf

ExecStart={pg_bin}/pg_ctl start -D $PGDATA -l $PGLOG -o "-p $PGPORT"
ExecStop={pg_bin}/pg_ctl stop -D $PGDATA -m fast
ExecReload={pg_bin}/pg_ctl reload -D $PGDATA

TimeoutSec=300

[Install]
WantedBy=multi-user.target
"""

# Environment file for each instance
INSTANCE_ENV_TEMPLATE = """\
# PostgreSQL instance configuration: {instance}
# Created by create_pg_service.py

PGDATA={pgdata}
PGPORT={port}
PGLOG={logfile}
"""


def check_root():
    """Check if running as root (required for systemd operations)."""
    if os.geteuid() != 0:
        print("Root privileges required. Run with sudo:")
        print(f"  sudo {' '.join(sys.argv)}")
        sys.exit(1)


def check_linux():
    """Check if running on Linux."""
    if sys.platform != "linux":
        if sys.platform == "darwin":
            print("This script creates systemd services for Linux.")
            print("For macOS, use create_pg_service_macos.py instead.")
        else:
            print("This script is Linux-specific (requires systemd).", file=sys.stderr)
        sys.exit(1)


def check_postgresql_installed():
    """Check if PostgreSQL is installed."""
    pg_ctl = PG_BIN / "pg_ctl"
    if not pg_ctl.exists():
        print(f"Error: PostgreSQL not found at {PG_BASE}", file=sys.stderr)
        print("Run pginstall.py first to install PostgreSQL.", file=sys.stderr)
        sys.exit(1)


def ensure_postgres_user() -> tuple[str, str]:
    """Ensure the postgres system user exists. Returns (username, groupname)."""
    username = "postgres"
    groupname = "postgres"

    # Check if user exists
    try:
        pw = pwd.getpwnam(username)
        print(f"  Using existing '{username}' user (uid={pw.pw_uid})")
        gr = grp.getgrgid(pw.pw_gid)
        return username, gr.gr_name
    except KeyError:
        pass

    # Create the postgres group and user
    print(f"  Creating system user '{username}'...")

    # Create group first
    result = subprocess.run(
        ["groupadd", "--system", groupname],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 and "already exists" not in result.stderr:
        print(f"Warning: Could not create group: {result.stderr}", file=sys.stderr)

    # Create user
    result = subprocess.run(
        [
            "useradd",
            "--system",
            "--gid", groupname,
            "--home-dir", str(PG_BASE),
            "--shell", "/bin/false",
            "--comment", "PostgreSQL Server",
            username,
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print(f"Error creating user '{username}': {result.stderr}", file=sys.stderr)
        sys.exit(1)

    print(f"  Created system user '{username}'")
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



def initialize_database(pgdata: Path, user: str):
    """Initialize the database cluster if it doesn't exist."""
    if (pgdata / "PG_VERSION").exists():
        print(f"  Database already initialized at {pgdata}")
        return

    print(f"  Initializing database cluster at {pgdata}...")

    # Create directory if needed
    pgdata.mkdir(parents=True, exist_ok=True)

    # Change ownership to the PostgreSQL user
    pw = pwd.getpwnam(user)
    os.chown(pgdata, pw.pw_uid, pw.pw_gid)

    # Run initdb as the target user
    initdb = PG_BIN / "initdb"
    result = subprocess.run(
        ["sudo", "-u", user, str(initdb), "-D", str(pgdata)],
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


def create_template_unit() -> Path:
    """Create the systemd template unit file if it doesn't exist."""
    template_path = Path("/etc/systemd/system/postgresql@.service")

    if template_path.exists():
        print(f"  Template unit already exists: {template_path}")
        return template_path

    service_content = SYSTEMD_TEMPLATE_UNIT.format(pg_bin=PG_BIN)

    print(f"  Creating template unit: {template_path}...")
    template_path.write_text(service_content)

    return template_path


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

    # Write environment file
    env_content = INSTANCE_ENV_TEMPLATE.format(
        instance=instance,
        pgdata=pgdata,
        port=port,
        logfile=logfile,
    )

    print(f"  Creating instance config: {env_file}...")
    env_file.write_text(env_content)

    return env_file


def enable_and_start_service(instance: str):
    """Enable and start the PostgreSQL instance service."""
    service_name = f"postgresql@{instance}"

    print("  Reloading systemd daemon...")
    subprocess.run(["systemctl", "daemon-reload"], check=True)

    print(f"  Enabling {service_name} service...")
    subprocess.run(["systemctl", "enable", service_name], check=True)

    print(f"  Starting {service_name} service...")
    result = subprocess.run(
        ["systemctl", "start", service_name],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print(f"Error starting service: {result.stderr}", file=sys.stderr)
        print(f"\nCheck logs with: journalctl -u {service_name}", file=sys.stderr)
        sys.exit(1)

    print(f"  Service {service_name} started successfully.")


def show_status(instance: str):
    """Show the service status."""
    service_name = f"postgresql@{instance}"
    print("\nService status:")
    subprocess.run(["systemctl", "status", service_name, "--no-pager"])


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
            result = subprocess.run(
                ["systemctl", "is-active", f"postgresql@{instance}"],
                capture_output=True,
                text=True,
            )
            status = result.stdout.strip()

            print(f"  {instance}:")
            print(f"    Status: {status}")
            print(f"    Port:   {port}")
            print(f"    Data:   {pgdata}")
            print()


def main():
    parser = argparse.ArgumentParser(
        description="Create and enable a PostgreSQL systemd service instance",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  sudo ./create_pg_service.py main                    # Create "main" instance
  sudo ./create_pg_service.py dev --port 5433         # Create "dev" instance on port 5433
  sudo ./create_pg_service.py test --pgdata /data/test --port 5434

  sudo systemctl start postgresql@main                # Start main instance
  sudo systemctl start postgresql@dev                 # Start dev instance

  ./create_pg_service.py --list                       # List all instances (no sudo needed)
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
        help="Log file location (default: /var/log/postgresql/<instance>.log)",
    )
    parser.add_argument(
        "--port",
        type=int,
        help="Port number (default: prompt or 5432)",
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
    check_linux()
    check_root()
    check_postgresql_installed()

    # One-liner description
    print("Creates a PostgreSQL systemd service for automatic startup on boot.")
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
    if instance_config.exists():
        print(f"Error: Instance '{instance}' already exists.", file=sys.stderr)
        print(f"  Config: {instance_config}", file=sys.stderr)
        print(f"\nTo reconfigure, first remove: sudo rm -r {PG_CONFIG_BASE / instance}", file=sys.stderr)
        sys.exit(1)

    # Get or create postgres user
    user, group = ensure_postgres_user()

    print(f"Creating PostgreSQL instance '{instance}'")
    print()

    # Get parameters with instance-aware defaults
    default_pgdata = f"/usr/local/postgresql/data/{instance}"
    default_logfile = f"/var/log/postgresql/{instance}.log"

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

    print()
    print("Configuration:")
    print(f"  Instance:       {instance}")
    print(f"  Data directory: {pgdata}")
    print(f"  Log file:       {logfile}")
    print(f"  Port:           {port}")
    print(f"  Run as:         {user}:{group}")
    print()

    # Create resources
    print("Setting up PostgreSQL instance...")
    create_template_unit()
    create_instance_config(instance, pgdata, logfile, port)
    create_logfile_directory(logfile, user)
    initialize_database(pgdata, user)
    enable_and_start_service(instance)

    show_status(instance)

    service_name = f"postgresql@{instance}"
    print()
    print(f"PostgreSQL instance '{instance}' is now running.")
    print()
    print("Useful commands:")
    print(f"  sudo systemctl status {service_name}   # Check status")
    print(f"  sudo systemctl stop {service_name}     # Stop server")
    print(f"  sudo systemctl start {service_name}    # Start server")
    print(f"  sudo systemctl restart {service_name}  # Restart server")
    print(f"  journalctl -u {service_name}           # View logs")
    print()
    print(f"  ./create_pg_service.py --list          # List all instances")
    print()
    print(f"Connect with: {PG_BIN}/psql -p {port}")


if __name__ == "__main__":
    main()
