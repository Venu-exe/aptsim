"""Exception hierarchy for the aptsim lab framework."""

from __future__ import annotations


class AptSimError(Exception):
    """Base class for all framework errors."""


class ConfigError(AptSimError):
    """Raised when configuration is missing, malformed or unsafe."""


class SafetyViolation(AptSimError):
    """Raised when an operation would leave the disposable lab boundary."""


class PathViolation(SafetyViolation):
    """Raised when a path resolves outside the lab root or hits a host location."""


class IntegrityError(AptSimError):
    """Raised when a SHA-256 verification fails during restore."""


class ModuleError(AptSimError):
    """Raised by a simulation module when it cannot complete its work."""


class PluginError(AptSimError):
    """Raised when a dynamically loaded plugin does not satisfy the contract."""


class CommandRejected(AptSimError):
    """Raised when a remote task fails validation before execution."""


class ProtocolError(AptSimError):
    """Raised on malformed, unsigned, replayed or oversized C2 frames."""


class SimulatedCrash(AptSimError):
    """Deliberate interruption used to demonstrate checkpoint/recovery."""
