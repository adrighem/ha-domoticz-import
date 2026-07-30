"""Host-neutral capability values and source identity.

This module intentionally uses syntax supported by Python 3.9 so it can be
vendored into Domoticz releases independently of the Home Assistant adapter.
"""

from dataclasses import dataclass
from enum import Enum
from math import isfinite
from typing import Optional, Tuple, Union

CapabilityValue = Union[bool, int, float, str]


class CapabilityKind(str, Enum):
    """Supported value shapes."""

    NUMERIC = "numeric"
    BINARY = "binary"
    TEXT = "text"


class Availability(str, Enum):
    """Whether a capability currently has a trustworthy value."""

    AVAILABLE = "available"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class SourceIdentity:
    """Stable identity of one capability in a source system."""

    system: str
    instance_id: str
    object_id: str
    capability_id: str

    def __post_init__(self) -> None:
        """Validate identity components."""
        for field_name in ("system", "instance_id", "object_id", "capability_id"):
            value = getattr(self, field_name)
            if not isinstance(value, str):
                raise TypeError(f"{field_name} must be a string")
            if not value.strip():
                raise ValueError(f"{field_name} must not be empty")
            if value != value.strip():
                raise ValueError(f"{field_name} must not have surrounding whitespace")

    @property
    def key(self) -> Tuple[str, str, str, str]:
        """Return an unambiguous, hashable identity key."""
        return (
            self.system,
            self.instance_id,
            self.object_id,
            self.capability_id,
        )


@dataclass(frozen=True)
class Capability:
    """A current, typed value exposed by a source system."""

    source: SourceIdentity
    kind: CapabilityKind
    name: str
    value: Optional[CapabilityValue]
    availability: Availability = Availability.AVAILABLE
    semantic: Optional[str] = None
    unit: Optional[str] = None
    state_class: Optional[str] = None

    def __post_init__(self) -> None:
        """Keep invalid or ambiguous values out of platform adapters."""
        if not isinstance(self.source, SourceIdentity):
            raise TypeError("source must be a SourceIdentity")
        if not isinstance(self.kind, CapabilityKind):
            raise TypeError("kind must be a CapabilityKind")
        if not isinstance(self.availability, Availability):
            raise TypeError("availability must be an Availability")
        if not isinstance(self.name, str):
            raise TypeError("name must be a string")
        if not self.name.strip():
            raise ValueError("name must not be empty")

        self._validate_optional_label("semantic", self.semantic)
        self._validate_optional_label("unit", self.unit)
        self._validate_optional_label("state_class", self.state_class)
        if (
            self.state_class is not None
            and self.state_class != self.state_class.strip()
        ):
            raise ValueError("state_class must not have surrounding whitespace")

        if self.kind is not CapabilityKind.NUMERIC and self.unit is not None:
            raise ValueError("only numeric capabilities may have a unit")
        if self.kind is not CapabilityKind.NUMERIC and self.state_class is not None:
            raise ValueError("only numeric capabilities may have a state_class")

        if self.availability is not Availability.AVAILABLE:
            if self.value is not None:
                raise ValueError(
                    "unknown or unavailable capabilities must have no value"
                )
            return

        if self.value is None:
            raise ValueError("available capabilities must have a value")
        if self.kind is CapabilityKind.NUMERIC:
            if isinstance(self.value, bool) or not isinstance(self.value, (int, float)):
                raise TypeError("numeric capabilities require an int or float value")
            if isinstance(self.value, float) and not isfinite(self.value):
                raise ValueError("numeric capabilities require a finite value")
        elif self.kind is CapabilityKind.BINARY:
            if not isinstance(self.value, bool):
                raise TypeError("binary capabilities require a bool value")
        elif not isinstance(self.value, str):
            raise TypeError("text capabilities require a string value")

    @staticmethod
    def _validate_optional_label(name: str, value: Optional[str]) -> None:
        """Validate optional textual metadata."""
        if value is None:
            return
        if not isinstance(value, str):
            raise TypeError(f"{name} must be a string or None")
        if not value.strip():
            raise ValueError(f"{name} must not be empty")

    @property
    def is_available(self) -> bool:
        """Return whether the current value can be consumed."""
        return self.availability is Availability.AVAILABLE
