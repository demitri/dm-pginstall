#!/usr/bin/env python3
"""
PostgreSQL Instance Manager

Discovers and manages PostgreSQL instances on Linux and macOS.
Supports systemd services, Homebrew services, and pg_ctl-managed instances.
Scans well-known filesystem locations for dormant data directories (e.g.
after a reboot when no service is configured) and shows advisory notes.

Usage:
    pgstatus.py                     # List all instances
    pgstatus.py list                # List all instances
    pgstatus.py info <instance>     # Show detailed info
    pgstatus.py start <instance>    # Start an instance
    pgstatus.py stop <instance>     # Stop an instance
    pgstatus.py restart <instance>  # Restart an instance
    pgstatus.py -D /path/to/pgdata  # Scan a specific data directory

Options:
    --json          Output as JSON (for list, info)
    --expand        Show expanded details (for list)
    --dry-run       Show what would be done (for start/stop/restart)
    -D, --pgdata    Additional data directory to scan (repeatable)
"""

import argparse
import json
import os
import platform
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

# Constants - match the project conventions
PG_BASE = Path("/usr/local/postgresql")
PG_BIN = PG_BASE / "bin"
PG_CONFIG_BASE = Path("/etc/postgresql")
PG_MACOS_CONFIG_BASE = Path("/usr/local/etc/postgresql")


class InstanceStatus(Enum):
    """Status of a PostgreSQL instance."""
    RUNNING = "running"
    STOPPED = "stopped"
    DORMANT = "dormant"
    UNKNOWN = "unknown"


class ServiceType(Enum):
    """Type of service management for the instance."""
    SYSTEMD = "systemd"
    HOMEBREW = "homebrew"
    LAUNCHD = "launchd"
    PGCTL = "pg_ctl"
    UNKNOWN = "unknown"


@dataclass
class PostgreSQLInstance:
    """Represents a discovered PostgreSQL instance."""
    name: str
    pid: Optional[int] = None
    status: InstanceStatus = InstanceStatus.UNKNOWN
    data_directory: Optional[Path] = None
    port: Optional[int] = None
    version: Optional[str] = None
    service_type: ServiceType = ServiceType.UNKNOWN
    service_name: Optional[str] = None
    config_file: Optional[Path] = None
    env_file: Optional[Path] = None
    log_file: Optional[Path] = None
    pg_ctl_path: Optional[Path] = None
    stale_postmaster_pid: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        result = {}
        for k, v in asdict(self).items():
            if isinstance(v, Path):
                result[k] = str(v) if v else None
            elif isinstance(v, Enum):
                result[k] = v.value
            else:
                result[k] = v
        return result


def get_platform() -> str:
    """Return 'linux' or 'darwin'."""
    system = platform.system().lower()
    if system == "darwin":
        return "darwin"
    elif system == "linux":
        return "linux"
    else:
        return "unknown"


# =============================================================================
# Process Detection (cross-platform)
# =============================================================================


def get_running_postgres_processes() -> list[dict]:
    """
    Find running postgres processes and extract their info.
    Returns list of dicts with keys: pid, data_dir, port
    """
    processes = []
    try:
        # Use ps to find postgres processes
        result = subprocess.run(
            ["ps", "aux"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return processes

        for line in result.stdout.splitlines():
            # Look for postgres main processes (not child workers)
            # Main process has "-D" flag for data directory
            if "postgres" in line and "-D" in line:
                parts = line.split()
                if len(parts) < 2:
                    continue

                pid = int(parts[1])
                data_dir = None
                port = None

                # Parse the command line for -D and -p flags
                cmd_start = line.find("postgres")
                if cmd_start == -1:
                    continue
                cmd_line = line[cmd_start:]

                # Extract data directory (-D flag)
                match = re.search(r"-D\s+(\S+)", cmd_line)
                if match:
                    data_dir = match.group(1)

                # Extract port (-p flag or from -o "-p ...")
                match = re.search(r"-p\s+(\d+)", cmd_line)
                if match:
                    port = int(match.group(1))

                # Skip worker processes (they have specific process titles)
                if any(worker in cmd_line for worker in [
                    "checkpointer", "background writer", "walwriter",
                    "autovacuum", "stats collector", "logical replication",
                    "walsender", "walreceiver"
                ]):
                    continue

                processes.append({
                    "pid": pid,
                    "data_dir": data_dir,
                    "port": port,
                })

    except (subprocess.TimeoutExpired, subprocess.SubprocessError, ValueError):
        pass

    return processes


# =============================================================================
# Linux: Systemd Detection
# =============================================================================


def discover_systemd_instances() -> list[PostgreSQLInstance]:
    """Discover PostgreSQL instances managed by systemd on Linux."""
    instances = []

    if get_platform() != "linux":
        return instances

    if not PG_CONFIG_BASE.exists():
        return instances

    # Scan /etc/postgresql/<instance>/postgresql.conf environment files
    for instance_dir in PG_CONFIG_BASE.iterdir():
        if not instance_dir.is_dir():
            continue

        env_file = instance_dir / "postgresql.conf"
        if not env_file.exists():
            continue

        instance_name = instance_dir.name
        instance = PostgreSQLInstance(
            name=instance_name,
            service_type=ServiceType.SYSTEMD,
            service_name=f"postgresql@{instance_name}",
            env_file=env_file,
        )

        # Parse environment file for PGDATA, PGPORT, PGLOG
        try:
            config = {}
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    key, value = line.split("=", 1)
                    config[key.strip()] = value.strip()

            if "PGDATA" in config:
                instance.data_directory = Path(config["PGDATA"])
            if "PGPORT" in config:
                try:
                    instance.port = int(config["PGPORT"])
                except ValueError:
                    pass
            if "PGLOG" in config:
                instance.log_file = Path(config["PGLOG"])

        except (OSError, IOError):
            pass

        # Get status from systemctl
        instance.status = get_systemd_status(instance.service_name)

        # Get PID if running
        if instance.status == InstanceStatus.RUNNING:
            instance.pid = get_systemd_pid(instance.service_name)

        # Read version from PG_VERSION file
        if instance.data_directory:
            instance.version = read_pg_version(instance.data_directory)
            instance.config_file = instance.data_directory / "postgresql.conf"

        # Find pg_ctl
        instance.pg_ctl_path = find_pg_ctl()

        instances.append(instance)

    return instances


def get_systemd_status(service_name: str) -> InstanceStatus:
    """Get the status of a systemd service."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", service_name],
            capture_output=True,
            text=True,
            timeout=5,
        )
        status = result.stdout.strip()
        if status == "active":
            return InstanceStatus.RUNNING
        elif status in ("inactive", "failed"):
            return InstanceStatus.STOPPED
        else:
            return InstanceStatus.UNKNOWN
    except (subprocess.TimeoutExpired, subprocess.SubprocessError):
        return InstanceStatus.UNKNOWN


def get_systemd_pid(service_name: str) -> Optional[int]:
    """Get the main PID of a systemd service."""
    try:
        result = subprocess.run(
            ["systemctl", "show", "-p", "MainPID", service_name],
            capture_output=True,
            text=True,
            timeout=5,
        )
        match = re.search(r"MainPID=(\d+)", result.stdout)
        if match:
            pid = int(match.group(1))
            return pid if pid > 0 else None
    except (subprocess.TimeoutExpired, subprocess.SubprocessError, ValueError):
        pass
    return None


# =============================================================================
# macOS: Homebrew and launchd Detection
# =============================================================================


def discover_homebrew_instances() -> list[PostgreSQLInstance]:
    """Discover PostgreSQL instances managed by Homebrew on macOS."""
    instances = []

    if get_platform() != "darwin":
        return instances

    try:
        # Check if brew is available
        result = subprocess.run(
            ["brew", "services", "list"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return instances

        # Parse output for postgresql entries
        for line in result.stdout.splitlines():
            if "postgresql" in line.lower():
                parts = line.split()
                if len(parts) < 2:
                    continue

                service_name = parts[0]
                status_str = parts[1] if len(parts) > 1 else "unknown"

                # Determine instance name (strip version suffix if present)
                instance_name = service_name.replace("postgresql@", "").replace("postgresql", "homebrew")
                if not instance_name or instance_name == "homebrew":
                    instance_name = "homebrew"

                instance = PostgreSQLInstance(
                    name=instance_name,
                    service_type=ServiceType.HOMEBREW,
                    service_name=service_name,
                )

                # Set status
                if status_str == "started":
                    instance.status = InstanceStatus.RUNNING
                elif status_str in ("stopped", "none"):
                    instance.status = InstanceStatus.STOPPED
                else:
                    instance.status = InstanceStatus.UNKNOWN

                # Try to find data directory from Homebrew
                instance.data_directory = find_homebrew_data_dir(service_name)

                if instance.data_directory:
                    instance.version = read_pg_version(instance.data_directory)
                    instance.config_file = instance.data_directory / "postgresql.conf"
                    instance.port = read_port_from_config(instance.config_file)

                instance.pg_ctl_path = find_pg_ctl()

                instances.append(instance)

    except (subprocess.TimeoutExpired, subprocess.SubprocessError, FileNotFoundError):
        pass

    return instances


def discover_launchd_instances() -> list[PostgreSQLInstance]:
    """Discover PostgreSQL instances from launchd on macOS."""
    instances = []

    if get_platform() != "darwin":
        return instances

    # Check both user LaunchAgents and system LaunchDaemons
    # create_pg_service_macos.py creates plists in /Library/LaunchDaemons/
    launchd_dirs = [
        Path.home() / "Library" / "LaunchAgents",
        Path("/Library/LaunchDaemons"),
    ]

    for launchd_dir in launchd_dirs:
        if not launchd_dir.exists():
            continue

        for plist in launchd_dir.glob("*postgres*.plist"):
            try:
                # Extract instance name from plist filename
                # com.postgresql.main.plist -> main
                # homebrew.mxcl.postgresql.plist -> postgresql
                stem = plist.stem
                if stem.startswith("com.postgresql."):
                    instance_name = stem.replace("com.postgresql.", "")
                elif stem.startswith("homebrew.mxcl."):
                    instance_name = stem.replace("homebrew.mxcl.", "")
                else:
                    instance_name = stem.replace(".", "-")

                instance = PostgreSQLInstance(
                    name=instance_name,
                    service_type=ServiceType.LAUNCHD,
                    service_name=stem,
                    env_file=plist,
                )

                # Parse plist for data directory (simplified - would need plistlib for full parsing)
                plist_content = plist.read_text()
                match = re.search(r"-D[</string>\s]*<string>([^<]+)", plist_content)
                if match:
                    instance.data_directory = Path(match.group(1))

                if instance.data_directory:
                    instance.version = read_pg_version(instance.data_directory)
                    instance.config_file = instance.data_directory / "postgresql.conf"
                    instance.port = read_port_from_config(instance.config_file)

                # Check if running via launchctl
                instance.status = get_launchd_status(stem)
                instance.pg_ctl_path = find_pg_ctl()

                instances.append(instance)

            except (OSError, IOError):
                continue

    return instances


def find_homebrew_data_dir(service_name: str) -> Optional[Path]:
    """Find the data directory for a Homebrew PostgreSQL installation."""
    # Common Homebrew data directory locations
    candidates = [
        Path.home() / "Library" / "Application Support" / "Postgres" / "var-17",
        Path.home() / "Library" / "Application Support" / "Postgres" / "var-16",
        Path.home() / "Library" / "Application Support" / "Postgres" / "var-15",
        Path("/usr/local/var/postgres"),
        Path("/opt/homebrew/var/postgres"),
        Path("/usr/local/var/postgresql@17"),
        Path("/opt/homebrew/var/postgresql@17"),
    ]

    for candidate in candidates:
        if candidate.exists() and (candidate / "PG_VERSION").exists():
            return candidate

    return None


def get_launchd_status(service_label: str) -> InstanceStatus:
    """Get the status of a launchd service."""
    try:
        # Check user-level services first
        result = subprocess.run(
            ["launchctl", "list"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if service_label in result.stdout:
            return InstanceStatus.RUNNING

        # Check system-level services (for LaunchDaemons)
        # launchctl print returns 0 if the service is loaded
        result = subprocess.run(
            ["launchctl", "print", f"system/{service_label}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return InstanceStatus.RUNNING

        return InstanceStatus.STOPPED
    except (subprocess.TimeoutExpired, subprocess.SubprocessError):
        return InstanceStatus.UNKNOWN


# =============================================================================
# Process-based Discovery (fallback for both platforms)
# =============================================================================


def discover_process_instances(known_instances: list[PostgreSQLInstance]) -> list[PostgreSQLInstance]:
    """
    Discover PostgreSQL instances from running processes.
    Only adds instances not already found by other methods.
    """
    instances = []
    known_data_dirs = {str(i.data_directory) for i in known_instances if i.data_directory}

    processes = get_running_postgres_processes()

    for proc in processes:
        data_dir = proc.get("data_dir")
        if not data_dir or data_dir in known_data_dirs:
            continue

        data_path = Path(data_dir)
        if not data_path.exists():
            continue

        # Generate instance name from data directory
        instance_name = data_path.name
        if instance_name in ("data", "pgdata"):
            instance_name = data_path.parent.name

        instance = PostgreSQLInstance(
            name=instance_name,
            pid=proc.get("pid"),
            status=InstanceStatus.RUNNING,
            data_directory=data_path,
            port=proc.get("port"),
            service_type=ServiceType.PGCTL,
        )

        instance.version = read_pg_version(data_path)
        instance.config_file = data_path / "postgresql.conf"

        if instance.port is None:
            instance.port = read_port_from_config(instance.config_file)

        instance.pg_ctl_path = find_pg_ctl()

        instances.append(instance)
        known_data_dirs.add(data_dir)

    return instances


# =============================================================================
# Utility Functions
# =============================================================================


def find_pg_ctl() -> Optional[Path]:
    """Find pg_ctl binary."""
    # Check project's standard location first
    if (PG_BIN / "pg_ctl").exists():
        return PG_BIN / "pg_ctl"

    # Check PATH
    try:
        result = subprocess.run(
            ["which", "pg_ctl"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return Path(result.stdout.strip())
    except (subprocess.TimeoutExpired, subprocess.SubprocessError):
        pass

    # Common locations
    candidates = [
        Path("/usr/bin/pg_ctl"),
        Path("/usr/local/bin/pg_ctl"),
        Path("/opt/homebrew/bin/pg_ctl"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate

    return None


def read_pg_version(data_dir: Path) -> Optional[str]:
    """Read PostgreSQL version from PG_VERSION file."""
    version_file = data_dir / "PG_VERSION"
    try:
        if version_file.exists():
            return version_file.read_text().strip()
    except (OSError, IOError, PermissionError):
        pass
    return None


def read_port_from_config(config_file: Path) -> Optional[int]:
    """Read port from postgresql.conf."""
    if not config_file:
        return None

    try:
        if not config_file.exists():
            return None
        content = config_file.read_text()
        # Look for: port = 5432 (with optional quotes and comments)
        match = re.search(r"^\s*port\s*=\s*['\"]?(\d+)['\"]?", content, re.MULTILINE)
        if match:
            return int(match.group(1))
    except (OSError, IOError, ValueError, PermissionError):
        pass

    return 5432  # Default PostgreSQL port


# =============================================================================
# Dormant Instance Discovery Helpers
# =============================================================================


def is_pg_data_directory(path: Path) -> bool:
    """Return True if directory contains PG_VERSION (is a PostgreSQL data dir)."""
    try:
        return path.is_dir() and (path / "PG_VERSION").exists()
    except (OSError, PermissionError):
        return False


def check_stale_postmaster_pid(data_dir: Path) -> tuple[bool, Optional[int], Optional[int]]:
    """
    Parse postmaster.pid and check if the PID is still running.
    Returns (is_stale, pid, port).
    is_stale is True if postmaster.pid exists but the process is not running.
    """
    pid_file = data_dir / "postmaster.pid"
    if not pid_file.exists():
        return (False, None, None)

    try:
        lines = pid_file.read_text().splitlines()
        pid = int(lines[0].strip()) if len(lines) > 0 else None
        port = int(lines[3].strip()) if len(lines) > 3 else None
    except (OSError, ValueError, IndexError, PermissionError):
        return (False, None, None)

    if pid is None:
        return (False, None, port)

    # Check if the process is still running
    try:
        os.kill(pid, 0)
        return (False, pid, port)  # Process is running — not stale
    except ProcessLookupError:
        return (True, pid, port)  # PID doesn't exist — stale
    except PermissionError:
        return (False, pid, port)  # Can't signal it, assume running


def get_pg_ctl_version(pg_ctl_path: Path) -> Optional[int]:
    """Run pg_ctl --version and extract the major version number."""
    try:
        result = subprocess.run(
            [str(pg_ctl_path), "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            match = re.search(r"(\d+)(?:\.\d+)?", result.stdout)
            if match:
                return int(match.group(1))
    except (subprocess.TimeoutExpired, subprocess.SubprocessError, OSError):
        pass
    return None


def read_postmaster_opts(data_dir: Path) -> Optional[str]:
    """Read postmaster.opts to show how the instance was previously started."""
    opts_file = data_dir / "postmaster.opts"
    try:
        if opts_file.exists():
            return opts_file.read_text().strip()
    except (OSError, PermissionError):
        pass
    return None


def scan_directory_for_pg_data(base_path: Path, max_depth: int = 2) -> list[Path]:
    """
    Recursively scan a directory up to max_depth levels for subdirectories
    containing PG_VERSION. Skips symlinks. Returns list of Paths.
    """
    results = []
    if not base_path.is_dir():
        return results

    def _scan(path: Path, depth: int):
        if depth > max_depth:
            return
        try:
            for entry in path.iterdir():
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    if is_pg_data_directory(entry):
                        results.append(entry)
                    else:
                        _scan(entry, depth + 1)
        except PermissionError:
            pass

    # Check the base_path itself first
    if is_pg_data_directory(base_path):
        results.append(base_path)
    else:
        _scan(base_path, 0)

    return results


def scan_log_dirs_for_data_paths() -> list[tuple[Path, Optional[int], Optional[str], Path]]:
    """
    Scan log directories for PostgreSQL log files that contain breadcrumbs
    about data directories.

    Returns list of (data_dir, port, version, log_file_path) tuples.
    """
    log_dirs = [
        Path("/var/log/postgresql/"),
        Path("/usr/local/var/log/postgresql/"),
    ]

    results = []
    seen_data_dirs = set()

    for log_dir in log_dirs:
        if not log_dir.is_dir():
            continue
        try:
            for log_file in log_dir.glob("*.log"):
                if not log_file.is_file():
                    continue
                try:
                    data_dir = None
                    port = None
                    version = None

                    # Read just the first ~50 lines (startup messages)
                    with open(log_file) as f:
                        for line_num, line in enumerate(f):
                            if line_num >= 50:
                                break

                            # Extract data dir from pg_hba.conf references
                            # Format in logs: (/path/to/pg_hba.conf:121)
                            m = re.search(r'(/[^():\s]+/pg_hba\.conf)', line)
                            if m:
                                hba_path = Path(m.group(1))
                                data_dir = hba_path.parent

                            # Extract version from startup line
                            m = re.search(r"starting PostgreSQL (\d+(?:\.\d+)?)", line)
                            if m:
                                version = m.group(1)

                            # Extract port from listening line
                            m = re.search(r"listening on .+ port (\d+)", line)
                            if m:
                                port = int(m.group(1))

                    if data_dir and str(data_dir) not in seen_data_dirs:
                        seen_data_dirs.add(str(data_dir))
                        results.append((data_dir, port, version, log_file))

                except (OSError, PermissionError):
                    continue
        except (OSError, PermissionError):
            continue

    return results


# =============================================================================
# Dormant / Unconfigured Instance Discovery
# =============================================================================


def discover_dormant_instances(
    known_instances: list[PostgreSQLInstance],
    extra_paths: Optional[list[Path]] = None,
) -> list[PostgreSQLInstance]:
    """
    Scan well-known locations + log breadcrumbs for data directories not
    already found by service-based or process-based discovery.
    """
    instances = []
    known_data_dirs = {str(i.data_directory) for i in known_instances if i.data_directory}

    plat = get_platform()

    # Scan paths common to both platforms
    scan_paths = [
        Path("/usr/local/postgresql/data/"),
        Path("/usr/local/pgdata/"),
        Path("/usr/local/pgsql/data/"),       # source install default
    ]

    if plat == "linux":
        scan_paths.extend([
            Path("/var/lib/postgresql/"),      # Debian/Ubuntu
            Path("/var/lib/pgsql/"),           # RHEL/CentOS/Fedora
            Path("/var/lib/pgsql/data/"),      # older RHEL single-instance
        ])

    if plat == "darwin":
        scan_paths.extend([
            Path("/usr/local/var/postgres/"),          # Homebrew (Intel)
            Path("/opt/homebrew/var/postgres/"),        # Homebrew (Apple Silicon)
            Path("/usr/local/var/postgresql@17/"),      # Homebrew versioned (Intel)
            Path("/usr/local/var/postgresql@16/"),
            Path("/usr/local/var/postgresql@15/"),
            Path("/opt/homebrew/var/postgresql@17/"),   # Homebrew versioned (Apple Silicon)
            Path("/opt/homebrew/var/postgresql@16/"),
            Path("/opt/homebrew/var/postgresql@15/"),
            Path.home() / "Library" / "Application Support" / "Postgres",  # Postgres.app
        ])

    # Add user-provided paths
    if extra_paths:
        scan_paths.extend(extra_paths)

    # Filesystem scan
    found_dirs: list[Path] = []
    for base in scan_paths:
        found_dirs.extend(scan_directory_for_pg_data(base))

    # Log breadcrumb scan
    log_results = scan_log_dirs_for_data_paths()
    log_map: dict[str, tuple[Optional[int], Optional[str], Path]] = {}
    for data_dir, port, version, log_path in log_results:
        log_map[str(data_dir)] = (port, version, log_path)
        # If the data dir wasn't found by filesystem scan but exists on disk, add it
        if str(data_dir) not in {str(d) for d in found_dirs} and data_dir.is_dir():
            found_dirs.append(data_dir)

    pg_ctl_path = find_pg_ctl()
    pg_ctl_major = get_pg_ctl_version(pg_ctl_path) if pg_ctl_path else None

    for data_dir in found_dirs:
        dir_str = str(data_dir)
        if dir_str in known_data_dirs:
            # Attach log file to the existing instance if found in log scan
            if dir_str in log_map:
                _, _, log_path = log_map[dir_str]
                for inst in known_instances:
                    if str(inst.data_directory) == dir_str and inst.log_file is None:
                        inst.log_file = log_path
            continue

        # Generate instance name from directory structure
        instance_name = data_dir.name
        if instance_name in ("data", "pgdata"):
            instance_name = data_dir.parent.name

        notes: list[str] = []

        # Read version from PG_VERSION
        version = read_pg_version(data_dir)

        # Read port from postgresql.conf
        config_file = data_dir / "postgresql.conf"
        port = read_port_from_config(config_file) if config_file.exists() else None

        # Log breadcrumb enrichment
        log_file = None
        if dir_str in log_map:
            log_port, log_version, log_path = log_map[dir_str]
            log_file = log_path
            if port is None and log_port is not None:
                port = log_port
            if version is None and log_version is not None:
                version = log_version

        # Check for stale postmaster.pid
        is_stale, stale_pid, pid_port = check_stale_postmaster_pid(data_dir)
        stale_postmaster = False
        if is_stale:
            stale_postmaster = True
            notes.append(
                f"Stale postmaster.pid found (PID {stale_pid} is not running). "
                f"Remove {data_dir}/postmaster.pid before starting."
            )
            if pid_port and port is None:
                port = pid_port

        # Read postmaster.opts
        prev_opts = read_postmaster_opts(data_dir)
        if prev_opts:
            notes.append(f"Previously started as: {prev_opts}")

        # Check pg_ctl version compatibility
        if pg_ctl_path and pg_ctl_major and version:
            try:
                data_major = int(version.split(".")[0])
                if data_major != pg_ctl_major:
                    notes.append(
                        f"Version mismatch: data directory is PG {version} "
                        f"but pg_ctl is PG {pg_ctl_major}. "
                        f"Use a matching pg_ctl to start this instance."
                    )
            except ValueError:
                pass

        # Reboot warning
        if plat == "darwin":
            notes.append(
                "No service configured — starting with pg_ctl won't survive reboot. "
                "Use create_pg_service_macos.py to create a persistent service."
            )
        else:
            notes.append(
                "No service configured — starting with pg_ctl won't survive reboot. "
                "Use create_pg_service.py to create a persistent service."
            )

        instance = PostgreSQLInstance(
            name=instance_name,
            status=InstanceStatus.DORMANT,
            data_directory=data_dir,
            port=port,
            version=version,
            service_type=ServiceType.PGCTL,
            config_file=config_file if config_file.exists() else None,
            log_file=log_file,
            pg_ctl_path=pg_ctl_path,
            stale_postmaster_pid=stale_postmaster,
            notes=notes,
        )

        instances.append(instance)
        known_data_dirs.add(dir_str)

    return instances


def discover_macos_config_instances(
    known_instances: list[PostgreSQLInstance],
) -> list[PostgreSQLInstance]:
    """
    Scan /usr/local/etc/postgresql/<instance>/ config dirs created by
    create_pg_service_macos.py. If the config exists but no launchd plist
    is loaded, report as DORMANT.
    """
    instances = []

    if get_platform() != "darwin":
        return instances

    if not PG_MACOS_CONFIG_BASE.exists():
        return instances

    known_data_dirs = {str(i.data_directory) for i in known_instances if i.data_directory}
    known_names = {i.name for i in known_instances}

    for instance_dir in PG_MACOS_CONFIG_BASE.iterdir():
        if not instance_dir.is_dir():
            continue

        env_file = instance_dir / "postgresql.conf"
        if not env_file.exists():
            continue

        instance_name = instance_dir.name

        # Parse the environment file for PGDATA, PGPORT, PGLOG
        config = {}
        try:
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    key, value = line.split("=", 1)
                    config[key.strip()] = value.strip()
        except (OSError, PermissionError):
            continue

        data_dir = Path(config["PGDATA"]) if "PGDATA" in config else None

        # Skip if already known
        if instance_name in known_names:
            continue
        if data_dir and str(data_dir) in known_data_dirs:
            continue

        port = None
        if "PGPORT" in config:
            try:
                port = int(config["PGPORT"])
            except ValueError:
                pass

        log_file = Path(config["PGLOG"]) if "PGLOG" in config else None

        # Check if launchd plist is loaded
        plist_label = f"com.postgresql.{instance_name}"
        status = get_launchd_status(plist_label)

        if status == InstanceStatus.RUNNING:
            continue  # Already running, should have been found by launchd discovery

        version = read_pg_version(data_dir) if data_dir and data_dir.is_dir() else None

        notes = [
            f"Config exists at {env_file} but launchd service is not loaded. "
            f"Run: sudo launchctl bootstrap system /Library/LaunchDaemons/{plist_label}.plist"
        ]

        instance = PostgreSQLInstance(
            name=instance_name,
            status=InstanceStatus.DORMANT,
            data_directory=data_dir,
            port=port,
            version=version,
            service_type=ServiceType.LAUNCHD,
            service_name=plist_label,
            env_file=env_file,
            log_file=log_file,
            config_file=data_dir / "postgresql.conf" if data_dir else None,
            pg_ctl_path=find_pg_ctl(),
            notes=notes,
        )

        instances.append(instance)
        if data_dir:
            known_data_dirs.add(str(data_dir))

    return instances


def discover_disabled_systemd_instances(
    known_instances: list[PostgreSQLInstance],
) -> list[PostgreSQLInstance]:
    """
    Find disabled/failed systemd template units not caught by
    discover_systemd_instances(). Reports them as STOPPED with notes.
    """
    instances = []

    if get_platform() != "linux":
        return instances

    known_names = {i.name for i in known_instances}

    try:
        result = subprocess.run(
            ["systemctl", "list-units", "--all", "--type=service",
             "--no-legend", "--no-pager", "postgresql@*"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return instances

        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) < 4:
                continue

            unit_name = parts[0]  # e.g. postgresql@foo.service
            active_state = parts[2]  # active, inactive, failed
            sub_state = parts[3]  # running, dead, failed, etc.

            # Extract instance name from unit
            match = re.match(r"postgresql@(.+)\.service", unit_name)
            if not match:
                continue
            instance_name = match.group(1)

            if instance_name in known_names:
                continue

            notes = []
            if active_state == "failed" or sub_state == "failed":
                notes.append(
                    f"Service {unit_name} is in failed state. "
                    f"Check: journalctl -u {unit_name}"
                )

            status = InstanceStatus.STOPPED

            # Try to get data dir from config
            env_file = PG_CONFIG_BASE / instance_name / "postgresql.conf"
            data_dir = None
            port = None
            log_file = None
            version = None

            if env_file.exists():
                try:
                    config = {}
                    for cfg_line in env_file.read_text().splitlines():
                        cfg_line = cfg_line.strip()
                        if "=" in cfg_line and not cfg_line.startswith("#"):
                            key, value = cfg_line.split("=", 1)
                            config[key.strip()] = value.strip()
                    if "PGDATA" in config:
                        data_dir = Path(config["PGDATA"])
                    if "PGPORT" in config:
                        try:
                            port = int(config["PGPORT"])
                        except ValueError:
                            pass
                    if "PGLOG" in config:
                        log_file = Path(config["PGLOG"])
                except (OSError, PermissionError):
                    pass

            if data_dir:
                version = read_pg_version(data_dir)

            instance = PostgreSQLInstance(
                name=instance_name,
                status=status,
                data_directory=data_dir,
                port=port,
                version=version,
                service_type=ServiceType.SYSTEMD,
                service_name=f"postgresql@{instance_name}",
                env_file=env_file if env_file.exists() else None,
                log_file=log_file,
                config_file=data_dir / "postgresql.conf" if data_dir else None,
                pg_ctl_path=find_pg_ctl(),
                notes=notes,
            )

            instances.append(instance)

    except (subprocess.TimeoutExpired, subprocess.SubprocessError):
        pass

    return instances


# =============================================================================
# Instance Resolution
# =============================================================================


def discover_all_instances(
    extra_paths: Optional[list[Path]] = None,
) -> list[PostgreSQLInstance]:
    """Discover all PostgreSQL instances on the system."""
    instances = []

    plat = get_platform()

    if plat == "linux":
        instances.extend(discover_systemd_instances())
        instances.extend(discover_disabled_systemd_instances(instances))
    elif plat == "darwin":
        instances.extend(discover_homebrew_instances())
        instances.extend(discover_launchd_instances())
        instances.extend(discover_macos_config_instances(instances))

    # Add process-based instances (running instances not found by other methods)
    instances.extend(discover_process_instances(instances))

    # Dormant discovery: filesystem + log breadcrumb scan as final catch-all
    instances.extend(discover_dormant_instances(instances, extra_paths=extra_paths))

    # Sort: running first, then stopped, then dormant, then unknown;
    # alphabetical within each group
    status_order = {
        InstanceStatus.RUNNING: 0,
        InstanceStatus.STOPPED: 1,
        InstanceStatus.DORMANT: 2,
        InstanceStatus.UNKNOWN: 3,
    }
    instances.sort(key=lambda i: (status_order.get(i.status, 99), i.name))

    return instances


def resolve_instance(name: str, instances: list[PostgreSQLInstance]) -> Optional[PostgreSQLInstance]:
    """Find an instance by name (exact or partial match)."""
    # Exact match first
    for instance in instances:
        if instance.name == name:
            return instance

    # Partial match
    matches = [i for i in instances if name in i.name]
    if len(matches) == 1:
        return matches[0]

    return None


# =============================================================================
# Service Management
# =============================================================================


def is_system_launchd(instance: PostgreSQLInstance) -> bool:
    """Check if this is a system-level launchd service (in /Library/LaunchDaemons/)."""
    if instance.env_file:
        return str(instance.env_file).startswith("/Library/LaunchDaemons")
    return False


def get_start_command(instance: PostgreSQLInstance) -> list[str]:
    """Get the command to start an instance."""
    if instance.service_type == ServiceType.SYSTEMD:
        return ["sudo", "systemctl", "start", instance.service_name]
    elif instance.service_type == ServiceType.HOMEBREW:
        return ["brew", "services", "start", instance.service_name]
    elif instance.service_type == ServiceType.LAUNCHD:
        if is_system_launchd(instance):
            # System service: use kickstart
            return ["sudo", "launchctl", "kickstart", f"system/{instance.service_name}"]
        else:
            # User service: use load
            return ["launchctl", "load", str(instance.env_file)]
    elif instance.service_type == ServiceType.PGCTL and instance.pg_ctl_path and instance.data_directory:
        cmd = [str(instance.pg_ctl_path), "start", "-D", str(instance.data_directory)]
        if instance.log_file:
            cmd.extend(["-l", str(instance.log_file)])
        return cmd
    return []


def get_stop_command(instance: PostgreSQLInstance) -> list[str]:
    """Get the command to stop an instance."""
    if instance.service_type == ServiceType.SYSTEMD:
        return ["sudo", "systemctl", "stop", instance.service_name]
    elif instance.service_type == ServiceType.HOMEBREW:
        return ["brew", "services", "stop", instance.service_name]
    elif instance.service_type == ServiceType.LAUNCHD:
        if is_system_launchd(instance):
            # System service: use kill SIGTERM
            return ["sudo", "launchctl", "kill", "SIGTERM", f"system/{instance.service_name}"]
        else:
            # User service: use unload
            return ["launchctl", "unload", str(instance.env_file)]
    elif instance.service_type == ServiceType.PGCTL and instance.pg_ctl_path and instance.data_directory:
        return [str(instance.pg_ctl_path), "stop", "-D", str(instance.data_directory), "-m", "fast"]
    return []


def get_restart_command(instance: PostgreSQLInstance) -> list[str]:
    """Get the command to restart an instance."""
    if instance.service_type == ServiceType.SYSTEMD:
        return ["sudo", "systemctl", "restart", instance.service_name]
    elif instance.service_type == ServiceType.HOMEBREW:
        return ["brew", "services", "restart", instance.service_name]
    elif instance.service_type == ServiceType.LAUNCHD:
        if is_system_launchd(instance):
            # System service: kickstart -k restarts
            return ["sudo", "launchctl", "kickstart", "-k", f"system/{instance.service_name}"]
        else:
            # User service: no direct restart, return empty (caller can stop then start)
            return []
    elif instance.service_type == ServiceType.PGCTL and instance.pg_ctl_path and instance.data_directory:
        cmd = [str(instance.pg_ctl_path), "restart", "-D", str(instance.data_directory), "-m", "fast"]
        if instance.log_file:
            cmd.extend(["-l", str(instance.log_file)])
        return cmd
    return []


def run_service_command(cmd: list[str], dry_run: bool = False) -> bool:
    """Execute a service management command."""
    if not cmd:
        print("Error: No command available for this service type.", file=sys.stderr)
        return False

    cmd_str = " ".join(cmd)

    if dry_run:
        print(f"Would run: {cmd_str}")
        return True

    print(f"Running: {cmd_str}")
    try:
        result = subprocess.run(cmd, timeout=60)
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        print("Error: Command timed out.", file=sys.stderr)
        return False
    except subprocess.SubprocessError as e:
        print(f"Error: {e}", file=sys.stderr)
        return False


# =============================================================================
# Output Formatting
# =============================================================================


def service_type_label(service_type: ServiceType) -> str:
    """Return a friendly label for the service type."""
    labels = {
        ServiceType.SYSTEMD: "systemd",
        ServiceType.HOMEBREW: "homebrew",
        ServiceType.LAUNCHD: "launchd",
        ServiceType.PGCTL: "manual",
        ServiceType.UNKNOWN: "-",
    }
    return labels.get(service_type, "-")


def format_list_table(instances: list[PostgreSQLInstance]) -> str:
    """Format instances as a table."""
    if not instances:
        return (
            "No PostgreSQL instances found.\n"
            "Tip: Use -D /path/to/pgdata to check a specific data directory."
        )

    # Column headers and widths
    # "Instance" is Linux terminology (systemd template units); use "Name" on macOS
    name_header = "Instance" if get_platform() == "linux" else "Name"
    headers = [name_header, "Status", "Port", "Version", "Managed", "Data Directory"]
    rows = []

    for inst in instances:
        rows.append([
            inst.name,
            inst.status.value,
            str(inst.port) if inst.port else "-",
            inst.version or "-",
            service_type_label(inst.service_type),
            str(inst.data_directory) if inst.data_directory else "-",
        ])

    # Calculate column widths
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    # Build output
    lines = []

    # Header
    header_line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    lines.append(header_line)
    lines.append("  ".join("-" * w for w in widths))

    # Rows
    for row in rows:
        lines.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))

    # Notes footer for dormant instances
    dormant_with_notes = [i for i in instances if i.status == InstanceStatus.DORMANT and i.notes]
    if dormant_with_notes:
        lines.append("")
        lines.append("Notes:")
        for inst in dormant_with_notes:
            for note in inst.notes:
                lines.append(f"  {inst.name}: {note}")

    return "\n".join(lines)


def format_list_json(instances: list[PostgreSQLInstance]) -> str:
    """Format instances as JSON."""
    return json.dumps([i.to_dict() for i in instances], indent=2)


def format_list_expanded(instances: list[PostgreSQLInstance]) -> str:
    """Format instances with expanded details."""
    if not instances:
        return (
            "No PostgreSQL instances found.\n"
            "Tip: Use -D /path/to/pgdata to check a specific data directory."
        )

    lines = []

    for i, inst in enumerate(instances):
        if i > 0:
            lines.append("")  # Blank line between instances

        # Instance header
        if inst.status == InstanceStatus.RUNNING:
            status_indicator = "+"
        elif inst.status == InstanceStatus.DORMANT:
            status_indicator = "~"
        else:
            status_indicator = "-"
        lines.append(f"[{status_indicator}] {inst.name}")
        lines.append("-" * (len(inst.name) + 4))

        # Status
        lines.append(f"    Status:      {inst.status.value}")
        if inst.pid:
            lines.append(f"    PID:         {inst.pid}")
        if inst.stale_postmaster_pid:
            lines.append(f"    Stale PID:   yes (postmaster.pid exists but process not running)")

        # Configuration
        lines.append(f"    Port:        {inst.port or '-'}")
        lines.append(f"    Version:     {inst.version or '-'}")
        lines.append(f"    Service:     {inst.service_type.value}")

        # Paths
        lines.append(f"    Data Dir:    {inst.data_directory or '-'}")
        if inst.config_file:
            lines.append(f"    Config:      {inst.config_file}")
        if inst.env_file:
            lines.append(f"    Env File:    {inst.env_file}")
        if inst.log_file:
            lines.append(f"    Log:         {inst.log_file}")

        # Commands
        start_cmd = get_start_command(inst)
        stop_cmd = get_stop_command(inst)
        restart_cmd = get_restart_command(inst)

        lines.append("")
        lines.append("    Commands:")
        if start_cmd:
            lines.append(f"      Start:     {' '.join(start_cmd)}")
        if stop_cmd:
            lines.append(f"      Stop:      {' '.join(stop_cmd)}")
        if restart_cmd:
            lines.append(f"      Restart:   {' '.join(restart_cmd)}")

        if inst.service_type == ServiceType.SYSTEMD:
            lines.append(f"      Status:    systemctl status {inst.service_name}")
            lines.append(f"      Logs:      journalctl -u {inst.service_name}")

        # Notes
        if inst.notes:
            lines.append("")
            lines.append("    Notes:")
            for note in inst.notes:
                lines.append(f"      - {note}")

    return "\n".join(lines)


def format_info(instance: PostgreSQLInstance) -> str:
    """Format detailed instance info."""
    lines = []
    lines.append(f"Instance: {instance.name}")
    lines.append("=" * 50)
    lines.append("")

    # Status section
    lines.append("Status:")
    lines.append(f"  Running:     {instance.status.value}")
    if instance.pid:
        lines.append(f"  PID:         {instance.pid}")
    if instance.stale_postmaster_pid:
        lines.append(f"  Stale PID:   yes (postmaster.pid exists but process not running)")
    lines.append("")

    # Configuration section
    lines.append("Configuration:")
    lines.append(f"  Port:        {instance.port or '-'}")
    lines.append(f"  Version:     {instance.version or '-'}")
    lines.append(f"  Data Dir:    {instance.data_directory or '-'}")
    lines.append("")

    # Service section
    lines.append("Service:")
    lines.append(f"  Type:        {instance.service_type.value}")
    if instance.service_name:
        lines.append(f"  Name:        {instance.service_name}")
    if instance.service_type == ServiceType.SYSTEMD:
        lines.append(f"  Unit File:   /etc/systemd/system/postgresql@.service")
    lines.append("")

    # Files section
    lines.append("Files:")
    if instance.config_file:
        lines.append(f"  Config:      {instance.config_file}")
    if instance.env_file:
        lines.append(f"  Env File:    {instance.env_file}")
    if instance.log_file:
        lines.append(f"  Log:         {instance.log_file}")
    lines.append("")

    # Management commands section
    lines.append("Management Commands:")
    start_cmd = get_start_command(instance)
    stop_cmd = get_stop_command(instance)
    restart_cmd = get_restart_command(instance)

    if start_cmd:
        lines.append(f"  Start:       {' '.join(start_cmd)}")
    if stop_cmd:
        lines.append(f"  Stop:        {' '.join(stop_cmd)}")
    if restart_cmd:
        lines.append(f"  Restart:     {' '.join(restart_cmd)}")

    if instance.service_type == ServiceType.SYSTEMD:
        lines.append(f"  Status:      systemctl status {instance.service_name}")
        lines.append(f"  Logs:        journalctl -u {instance.service_name}")

    # Notes section
    if instance.notes:
        lines.append("")
        lines.append("Notes:")
        for note in instance.notes:
            lines.append(f"  - {note}")

    return "\n".join(lines)


def format_info_json(instance: PostgreSQLInstance) -> str:
    """Format detailed instance info as JSON."""
    data = instance.to_dict()
    data["commands"] = {
        "start": get_start_command(instance),
        "stop": get_stop_command(instance),
        "restart": get_restart_command(instance),
    }
    return json.dumps(data, indent=2)


# =============================================================================
# Shell Completions
# =============================================================================


def generate_bash_completion() -> str:
    """Generate bash completion script."""
    return '''# Bash completion for pgstatus.py
# Add to ~/.bashrc: eval "$(./pgstatus.py --completions)"

_pgstatus_completions() {
    local cur prev commands
    COMPREPLY=()
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"
    commands="list info start stop restart"

    case "${prev}" in
        info|start|stop|restart)
            # Complete with instance names
            local instances=$(./pgstatus.py list --json 2>/dev/null | python3 -c "import sys,json; print(' '.join(i['name'] for i in json.load(sys.stdin)))" 2>/dev/null)
            COMPREPLY=( $(compgen -W "${instances}" -- "${cur}") )
            return 0
            ;;
        -D|--pgdata)
            # Complete with directories
            COMPREPLY=( $(compgen -d -- "${cur}") )
            return 0
            ;;
        pgstatus.py|./pgstatus.py)
            COMPREPLY=( $(compgen -W "${commands} --json --expand --dry-run --completions --pgdata -D --help" -- "${cur}") )
            return 0
            ;;
    esac

    if [[ "${cur}" == -* ]]; then
        COMPREPLY=( $(compgen -W "--json --expand --dry-run --completions --pgdata -D --help" -- "${cur}") )
        return 0
    fi

    COMPREPLY=( $(compgen -W "${commands}" -- "${cur}") )
}

complete -F _pgstatus_completions pgstatus.py
complete -F _pgstatus_completions ./pgstatus.py
'''


def generate_zsh_completion() -> str:
    """Generate zsh completion script."""
    return '''#compdef pgstatus.py
# Zsh completion for pgstatus.py
# Add to ~/.zshrc: eval "$(./pgstatus.py --completions)"

_pgstatus() {
    local -a commands instances
    commands=(
        'list:List all PostgreSQL instances'
        'info:Show detailed info about an instance'
        'start:Start an instance'
        'stop:Stop an instance'
        'restart:Restart an instance'
    )

    _arguments -s \\
        '--json[Output as JSON]' \\
        '--expand[Show expanded details with commands]' \\
        '--dry-run[Show what would be done]' \\
        '--completions[Output shell completion script]' \\
        '--help[Show help message]' \\
        '*-D[Additional data directory to scan]:directory:_files -/' \\
        '*--pgdata[Additional data directory to scan]:directory:_files -/' \\
        '1:command:->command' \\
        '2:instance:->instance'

    case "$state" in
        command)
            _describe 'command' commands
            ;;
        instance)
            instances=(${(f)"$(./pgstatus.py list --json 2>/dev/null | python3 -c "import sys,json; [print(i['name']) for i in json.load(sys.stdin)]" 2>/dev/null)"})
            _describe 'instance' instances
            ;;
    esac
}

compdef _pgstatus pgstatus.py
'''


# =============================================================================
# CLI
# =============================================================================


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="PostgreSQL Instance Manager - Discover and manage PostgreSQL instances",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                      # List all instances (same as 'list')
  %(prog)s list                 # List all instances
  %(prog)s list --expand        # List with full details and commands
  %(prog)s list --json          # List as JSON
  %(prog)s info main            # Show detailed info for 'main' instance
  %(prog)s start main           # Start the 'main' instance
  %(prog)s stop main            # Stop the 'main' instance
  %(prog)s restart main         # Restart the 'main' instance
  %(prog)s stop main --dry-run  # Show what would be done
  %(prog)s -D /path/to/pgdata   # Scan a specific data directory

Service management commands (start/stop/restart) may require sudo for
systemd-managed instances.
""",
    )

    parser.add_argument(
        "command",
        nargs="?",
        choices=["list", "info", "start", "stop", "restart"],
        default="list",
        help="Command to run (default: list)",
    )
    parser.add_argument(
        "instance",
        nargs="?",
        help="Instance name (required for info/start/stop/restart)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON (for list, info)",
    )
    parser.add_argument(
        "--expand", "-e",
        action="store_true",
        help="Show expanded details including commands (for list)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done (for start/stop/restart)",
    )
    parser.add_argument(
        "--completions",
        action="store_true",
        help="Output shell completion script",
    )
    parser.add_argument(
        "-D", "--pgdata",
        action="append",
        metavar="DIR",
        help="Additional data directory to scan (repeatable)",
    )

    return parser.parse_args()


def main() -> int:
    """Main entry point."""
    args = parse_args()

    # Handle completions
    if args.completions:
        # Detect shell from environment
        shell = os.environ.get("SHELL", "")
        if "zsh" in shell:
            print(generate_zsh_completion())
        else:
            print(generate_bash_completion())
        return 0

    # Discover instances
    extra_paths = [Path(p) for p in args.pgdata] if args.pgdata else None
    instances = discover_all_instances(extra_paths=extra_paths)

    # Handle commands
    if args.command == "list":
        if args.json:
            print(format_list_json(instances))
        elif args.expand:
            print(format_list_expanded(instances))
        else:
            print(format_list_table(instances))
        return 0

    elif args.command == "info":
        if not args.instance:
            print("Error: Instance name required for 'info' command.", file=sys.stderr)
            return 1

        instance = resolve_instance(args.instance, instances)
        if not instance:
            print(f"Error: Instance '{args.instance}' not found.", file=sys.stderr)
            if instances:
                print(f"Available instances: {', '.join(i.name for i in instances)}", file=sys.stderr)
            return 1

        if args.json:
            print(format_info_json(instance))
        else:
            print(format_info(instance))
        return 0

    elif args.command in ("start", "stop", "restart"):
        if not args.instance:
            print(f"Error: Instance name required for '{args.command}' command.", file=sys.stderr)
            return 1

        instance = resolve_instance(args.instance, instances)
        if not instance:
            print(f"Error: Instance '{args.instance}' not found.", file=sys.stderr)
            if instances:
                print(f"Available instances: {', '.join(i.name for i in instances)}", file=sys.stderr)
            return 1

        if args.command == "start":
            cmd = get_start_command(instance)
        elif args.command == "stop":
            cmd = get_stop_command(instance)
        else:  # restart
            cmd = get_restart_command(instance)

        if not cmd:
            print(f"Error: Cannot {args.command} instance '{instance.name}' - unknown service type.", file=sys.stderr)
            if instance.notes:
                for note in instance.notes:
                    print(f"  Note: {note}", file=sys.stderr)
            return 1

        success = run_service_command(cmd, args.dry_run)
        return 0 if success else 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
