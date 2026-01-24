#!/usr/bin/env python3
"""
Create and enable a PostgreSQL systemd service.

This script creates a systemd service file for PostgreSQL,
initializes the database if needed, and starts the server.
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

SYSTEMD_SERVICE_TEMPLATE = """\
[Unit]
Description=PostgreSQL database server
Documentation=https://www.postgresql.org/docs/
After=network.target

[Service]
Type=forking
User={user}
Group={group}

Environment=PGDATA={pgdata}
Environment=PGPORT={port}

ExecStart={pg_bin}/pg_ctl start -D {pgdata} -l {logfile} -o "-p {port}"
ExecStop={pg_bin}/pg_ctl stop -D {pgdata} -m fast
ExecReload={pg_bin}/pg_ctl reload -D {pgdata}

TimeoutSec=300

[Install]
WantedBy=multi-user.target
"""


def check_root():
    """Check if running as root (required for systemd operations)."""
    if os.geteuid() != 0:
        print("Error: This script must be run as root (use sudo).", file=sys.stderr)
        sys.exit(1)


def check_linux():
    """Check if running on Linux."""
    if sys.platform != "linux":
        print("Error: This script is Linux-specific (requires systemd).", file=sys.stderr)
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


def create_service_file(
    pgdata: Path,
    logfile: Path,
    port: int,
    user: str,
    group: str,
    service_name: str = "postgresql",
) -> Path:
    """Create the systemd service file."""
    service_content = SYSTEMD_SERVICE_TEMPLATE.format(
        user=user,
        group=group,
        pgdata=pgdata,
        port=port,
        logfile=logfile,
        pg_bin=PG_BIN,
    )

    service_path = Path(f"/etc/systemd/system/{service_name}.service")

    print(f"  Creating service file {service_path}...")
    service_path.write_text(service_content)

    return service_path


def enable_and_start_service(service_name: str = "postgresql"):
    """Enable and start the PostgreSQL service."""
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
        print("\nCheck logs with: journalctl -u postgresql", file=sys.stderr)
        sys.exit(1)

    print(f"  Service {service_name} started successfully.")


def show_status(service_name: str = "postgresql"):
    """Show the service status."""
    print("\nService status:")
    subprocess.run(["systemctl", "status", service_name, "--no-pager"])


def main():
    parser = argparse.ArgumentParser(
        description="Create and enable a PostgreSQL systemd service",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  sudo ./create_pg_service.py
  sudo ./create_pg_service.py --pgdata /var/lib/postgresql/data
  sudo ./create_pg_service.py --pgdata /data/pg --logfile /var/log/postgresql.log --port 5433
""",
    )
    parser.add_argument(
        "--pgdata",
        type=str,
        help="PostgreSQL data directory (default: prompt or /usr/local/postgresql/data)",
    )
    parser.add_argument(
        "--logfile",
        type=str,
        help="Log file location (default: prompt or /var/log/postgresql/postgresql.log)",
    )
    parser.add_argument(
        "--port",
        type=int,
        help="Port number (default: prompt or 5432)",
    )
    parser.add_argument(
        "--service-name",
        type=str,
        default="postgresql",
        help="Name of the systemd service (default: postgresql)",
    )
    parser.add_argument(
        "--user",
        type=str,
        help="User to run PostgreSQL as (default: postgres)",
    )

    args = parser.parse_args()

    # Checks
    check_linux()
    check_root()
    check_postgresql_installed()

    # Get or create postgres user
    if args.user:
        # User specified a custom user
        try:
            pw = pwd.getpwnam(args.user)
            gr = grp.getgrgid(pw.pw_gid)
            user, group = args.user, gr.gr_name
            print(f"Using specified user: {user}")
        except KeyError:
            print(f"Error: User '{args.user}' does not exist.", file=sys.stderr)
            sys.exit(1)
    else:
        # Default to postgres user (create if needed)
        user, group = ensure_postgres_user()

    print(f"Creating PostgreSQL service for user: {user}")
    print()

    # Get parameters (prompt if not provided)
    if args.pgdata:
        pgdata = Path(args.pgdata)
    else:
        pgdata = Path(prompt_for_value("PostgreSQL data directory", "/usr/local/postgresql/data"))

    if args.logfile:
        logfile = Path(args.logfile)
    else:
        logfile = Path(prompt_for_value("Log file location", "/var/log/postgresql/postgresql.log"))

    if args.port:
        port = args.port
    else:
        port = int(prompt_for_value("Port number", "5432"))

    print()
    print("Configuration:")
    print(f"  Data directory: {pgdata}")
    print(f"  Log file:       {logfile}")
    print(f"  Port:           {port}")
    print(f"  User:           {user}")
    print(f"  Group:          {group}")
    print()

    # Create resources
    print("Setting up PostgreSQL service...")
    create_logfile_directory(logfile, user)
    initialize_database(pgdata, user)
    create_service_file(pgdata, logfile, port, user, group, args.service_name)
    enable_and_start_service(args.service_name)

    show_status(args.service_name)

    print()
    print("PostgreSQL service is now running.")
    print()
    print("Useful commands:")
    print(f"  sudo systemctl status {args.service_name}   # Check status")
    print(f"  sudo systemctl stop {args.service_name}     # Stop server")
    print(f"  sudo systemctl start {args.service_name}    # Start server")
    print(f"  sudo systemctl restart {args.service_name}  # Restart server")
    print(f"  journalctl -u {args.service_name}           # View logs")
    print()
    print(f"Connect with: {PG_BIN}/psql -p {port}")


if __name__ == "__main__":
    main()
