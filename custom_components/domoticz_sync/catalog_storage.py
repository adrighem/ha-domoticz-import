"""Home Assistant storage adapter for one Domoticz target catalog."""

from __future__ import annotations

import os
from collections.abc import Mapping
from copy import deepcopy
from glob import escape, iglob
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .core import CapabilityKind, CatalogStorageError, catalog_from_document

_STORAGE_VERSION = 1
_STORAGE_KEY_PREFIX = "domoticz_sync.target_catalog"
_BINARY_STORAGE_KEY_PREFIX = "domoticz_sync.target_catalog.binary"
_WRAPPER_KEYS = {"entry_id", "destination_id", "catalog"}


class HomeAssistantCatalogStorage:
    """Persist one destination's target catalog in Home Assistant storage."""

    _storage_key_prefix = _STORAGE_KEY_PREFIX
    _capability_kind = CapabilityKind.NUMERIC

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        entry_id: str,
        destination_id: str,
    ) -> None:
        """Initialize a catalog namespace for one config entry and destination."""
        self._entry_id = _validate_namespace_id(entry_id, "entry_id")
        self._destination_id = _validate_namespace_id(
            destination_id,
            "destination_id",
        )
        self._hass = hass
        self._key = (
            f"{self._storage_key_prefix}.{self._entry_id}.{self._destination_id}"
        )
        self._store = self._new_store()
        self._failed = False

    @property
    def key(self) -> str:
        """Return the Home Assistant storage key for this catalog."""
        return self._key

    async def async_load(self) -> Mapping[str, object] | None:
        """Load the inner catalog, failing closed on ambiguous persisted state."""
        self._ensure_usable()
        store = self._store
        try:
            existed, corrupt_marker = await self._hass.async_add_executor_job(
                _storage_path_state,
                store.path,
            )
            if not existed and corrupt_marker:
                self._fail("target catalog could not be loaded")
            wrapper = await store.async_load()
        except Exception:
            self._fail("target catalog could not be loaded")

        if wrapper is None:
            if existed:
                self._fail("target catalog could not be loaded")
            try:
                exists_after, corrupt_marker = await self._hass.async_add_executor_job(
                    _storage_path_state,
                    store.path,
                )
            except Exception:
                self._fail("target catalog could not be loaded")
            if exists_after or corrupt_marker:
                self._fail("target catalog could not be loaded")
            return None

        try:
            catalog = self._unwrap(wrapper)
            self._validate_catalog(catalog)
        except TypeError, ValueError:
            self._fail("target catalog could not be loaded")
        return catalog

    async def async_save(self, document: Mapping[str, object]) -> None:
        """Atomically replace and independently verify the complete catalog."""
        self._ensure_usable()
        if not isinstance(document, Mapping):
            self._fail("target catalog could not be saved")

        try:
            catalog = deepcopy(dict(document))
            self._validate_catalog(catalog)
        except Exception:
            self._fail("target catalog could not be saved")

        wrapper: dict[str, object] = {
            "entry_id": self._entry_id,
            "destination_id": self._destination_id,
            "catalog": catalog,
        }

        try:
            await self._store.async_save(wrapper)
            persisted = await self._new_store().async_load()
        except Exception:
            self._fail("target catalog could not be saved")

        if not _strict_equal(persisted, wrapper):
            self._fail("target catalog save could not be confirmed")

        try:
            persisted_catalog = self._unwrap(persisted)
            self._validate_catalog(persisted_catalog)
        except TypeError, ValueError:
            self._fail("target catalog save could not be confirmed")

    def _new_store(self) -> Store[dict[str, Any]]:
        """Create a store without reusing pending per-instance write data."""
        return Store(
            self._hass,
            _STORAGE_VERSION,
            self._key,
            private=True,
            atomic_writes=True,
        )

    def _unwrap(self, wrapper: object) -> dict[str, object]:
        """Validate the exact namespace wrapper and return its catalog."""
        if type(wrapper) is not dict or set(wrapper) != _WRAPPER_KEYS:
            raise ValueError("invalid catalog wrapper")
        if (
            type(wrapper["entry_id"]) is not str
            or wrapper["entry_id"] != self._entry_id
            or type(wrapper["destination_id"]) is not str
            or wrapper["destination_id"] != self._destination_id
        ):
            raise ValueError("catalog namespace does not match")

        catalog = wrapper["catalog"]
        if type(catalog) is not dict:
            raise TypeError("catalog document must be an object")
        return catalog

    def _ensure_usable(self) -> None:
        """Keep a detected ambiguous state failed closed for this instance."""
        if self._failed:
            raise CatalogStorageError("target catalog storage is unavailable")

    def _validate_catalog(self, document: object) -> None:
        """Require every record to belong to this storage capability kind."""
        catalog = catalog_from_document(document)
        if any(
            record.capability.kind is not self._capability_kind
            for record in catalog.records
        ):
            raise ValueError("catalog contains an unexpected capability kind")

    def _fail(self, message: str) -> None:
        """Remember one unsafe state and raise a sanitized storage error."""
        self._failed = True
        raise CatalogStorageError(message) from None


class HomeAssistantBinaryCatalogStorage(HomeAssistantCatalogStorage):
    """Persist one destination's binary target catalog separately."""

    _storage_key_prefix = _BINARY_STORAGE_KEY_PREFIX
    _capability_kind = CapabilityKind.BINARY


def _validate_namespace_id(value: object, field: str) -> str:
    """Require a non-empty exact string for a storage namespace component."""
    if type(value) is not str:
        raise TypeError(f"{field} must be a string")
    if not value or value != value.strip():
        raise ValueError(f"{field} must be a non-empty trimmed string")
    return value


def _strict_equal(left: object, right: object) -> bool:
    """Compare JSON-compatible values without bool/int type coercion."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return set(left) == set(right) and all(
            _strict_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _strict_equal(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=True)
        )
    return left == right


def _storage_path_state(path: str) -> tuple[bool, bool]:
    """Return whether the base file or one exact corruption marker exists."""
    if os.path.isfile(path):
        return True, False
    marker_pattern = f"{escape(path)}.corrupt.*"
    return False, next(iglob(marker_pattern), None) is not None
