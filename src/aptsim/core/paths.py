"""Lab path guards.

Every filesystem mutation performed by the framework goes through this module.
The guard enforces three invariants:

1. The lab root itself is a disposable location (not ``/``, not ``/usr``, not
   ``$HOME``, not a system configuration tree).
2. Every resolved target stays inside the lab root, even in the presence of
   ``..`` segments or symlinks.
3. Host persistence locations (``/etc``, ``~/.config/autostart``, crontab
   spools, systemd unit directories, shell rc files) are rejected outright.

The framework never writes outside the lab; the guard exists so that a
misconfigured lab root cannot turn a simulation into a host modification.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .errors import PathViolation

# Exact filesystem roots that are never acceptable as a lab root and are never
# acceptable as a mutation target.
BLOCKED_ROOTS: tuple[str, ...] = (
    "/",
    "/etc",
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/boot",
    "/dev",
    "/proc",
    "/sys",
    "/var",
    "/run",
    "/opt",
    "/root",
    "/home",
)

# Subtrees that must not contain the lab root even though they are otherwise
# ordinary directories.
SYSTEM_TREES: tuple[str, ...] = (
    "/etc",
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/boot",
    "/dev",
    "/proc",
    "/sys",
    "/var",
    "/run",
    "/root",
)

BLOCKED_HOME_NAMES: tuple[str, ...] = (
    ".config",
    ".local",
    ".ssh",
    ".gnupg",
    ".bashrc",
    ".zshrc",
    ".profile",
    ".bash_profile",
    ".bash_login",
    ".pam_environment",
)

# Absolute host locations that a simulated persistence mechanism must never
# touch, regardless of lab root configuration.
BLOCKED_TARGETS: tuple[str, ...] = (
    "/etc/cron.d",
    "/etc/crontab",
    "/var/spool/cron",
    "/etc/systemd/system",
    "/lib/systemd/system",
    "/usr/lib/systemd/system",
    "/etc/xdg/autostart",
    "/etc/rc.local",
    "/etc/profile.d",
)


def _resolved(path: str | os.PathLike[str]) -> Path:
    return Path(path).expanduser().resolve(strict=False)


@dataclass(frozen=True)
class LabPaths:
    """Resolved directory layout for one disposable lab."""

    root: Path
    victim_subdir: str = "victim"
    backup_subdir: str = ".backup"
    sessions_subdir: str = "sessions"
    plugins_subdir: str = "plugins"
    persistence_subdir: str = "persistence"
    c2_subdir: str = "c2"

    @classmethod
    def build(cls, root: str | os.PathLike[str], **layout: str) -> "LabPaths":
        resolved = _resolved(root)
        validate_lab_root(resolved)
        defaults = {
            "victim_subdir": "victim",
            "backup_subdir": ".backup",
            "sessions_subdir": "sessions",
            "plugins_subdir": "plugins",
            "persistence_subdir": "persistence",
            "c2_subdir": "c2",
        }
        defaults.update({k: v for k, v in layout.items() if k in defaults and v})
        return cls(root=resolved, **defaults)

    # -- derived locations -------------------------------------------------
    @property
    def victim(self) -> Path:
        return self.root / self.victim_subdir

    @property
    def backup_root(self) -> Path:
        return self.root / self.backup_subdir

    @property
    def sessions(self) -> Path:
        return self.root / self.sessions_subdir

    @property
    def plugins(self) -> Path:
        return self.root / self.plugins_subdir

    @property
    def persistence(self) -> Path:
        return self.root / self.persistence_subdir

    @property
    def c2(self) -> Path:
        return self.root / self.c2_subdir

    @property
    def cleanup_reports(self) -> Path:
        return self.root / "cleanup-reports"

    def session_dir(self, session_id: str) -> Path:
        return self._inside(self.sessions / session_id, what="session directory")

    def ensure_layout(self) -> None:
        for directory in (
            self.root,
            self.victim,
            self.backup_root,
            self.sessions,
            self.plugins,
            self.persistence,
            self.c2,
            self.cleanup_reports,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    # -- guard -------------------------------------------------------------
    def is_inside(self, target: str | os.PathLike[str]) -> bool:
        return _resolved(target).is_relative_to(self.root)

    def _inside(self, target: Path, *, what: str) -> Path:
        resolved = _resolved(target)
        if not resolved.is_relative_to(self.root):
            raise PathViolation(f"{what} escapes lab root: {resolved} not under {self.root}")
        _reject_blocked(resolved)
        return resolved

    def guard(self, target: str | os.PathLike[str], *, what: str = "path") -> Path:
        """Resolve *target* and assert it stays inside the disposable lab."""
        return self._inside(Path(target), what=what)

    def resolve_relative(self, relative: str | os.PathLike[str], *, what: str = "path") -> Path:
        """Resolve a lab-relative path, rejecting absolute paths and traversal."""
        raw = Path(relative)
        if raw.is_absolute():
            raise PathViolation(f"{what} must be lab-relative, got absolute path: {raw}")
        if any(part == ".." for part in raw.parts):
            raise PathViolation(f"{what} must not contain '..': {raw}")
        return self._inside(self.root / raw, what=what)

    def guard_persistence(self, relative: str | os.PathLike[str]) -> Path:
        """Only allow simulated persistence artifacts under the persistence dir."""
        target = self.resolve_relative(relative, what="persistence target")
        if not target.is_relative_to(self.persistence):
            raise PathViolation(
                f"persistence target must live under {self.persistence}, got {target}"
            )
        return target

    def guard_victim(self, target: str | os.PathLike[str]) -> Path:
        resolved = self.guard(target)
        if not resolved.is_relative_to(self.victim):
            raise PathViolation(f"operation limited to victim tree, got {resolved}")
        return resolved

    def assert_not_host_target(self, target: str | os.PathLike[str]) -> None:
        resolved = _resolved(target)
        _reject_blocked(resolved)
        for blocked in BLOCKED_TARGETS:
            blocked_path = Path(blocked)
            if resolved == blocked_path or resolved.is_relative_to(blocked_path):
                raise PathViolation(f"host persistence target blocked: {resolved}")


def validate_lab_root(root: Path) -> None:
    resolved = _resolved(root)
    if str(resolved) in BLOCKED_ROOTS:
        raise PathViolation(f"lab root must not be a system location: {resolved}")
    for tree in SYSTEM_TREES:
        if resolved.is_relative_to(tree):
            raise PathViolation(f"lab root must not live under a system tree: {resolved}")

    home = Path.home().resolve()
    if resolved == home:
        raise PathViolation("lab root must not be the user home directory")
    if resolved.is_relative_to(home):
        relative = resolved.relative_to(home)
        if any(part.startswith(".") for part in relative.parts):
            raise PathViolation(f"lab root must not live in a hidden home directory: {resolved}")
    if resolved.is_relative_to(Path("/tmp")) and resolved == Path("/tmp"):
        raise PathViolation("lab root must not be /tmp itself; use a dedicated subdirectory")
    _reject_blocked(resolved)


def _reject_blocked(resolved: Path) -> None:
    if str(resolved) in BLOCKED_ROOTS:
        raise PathViolation(f"path resolves to a protected system location: {resolved}")
    for blocked in BLOCKED_TARGETS:
        blocked_path = Path(blocked)
        if resolved == blocked_path or resolved.is_relative_to(blocked_path):
            raise PathViolation(f"path resolves into a host persistence location: {resolved}")
    for name in BLOCKED_HOME_NAMES:
        candidate = Path.home() / name
        if resolved == candidate or resolved.is_relative_to(candidate):
            raise PathViolation(f"path resolves into a host config location: {resolved}")
