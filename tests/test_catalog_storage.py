"""Tests for the Home Assistant target catalog storage adapter."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.helpers.storage import Store  # noqa: E402

from custom_components.domoticz_sync.catalog_storage import (  # noqa: E402
    HomeAssistantCatalogStorage,
)
from custom_components.domoticz_sync.core import CatalogStorageError  # noqa: E402

ENTRY_ID = "entry-1"
DESTINATION_ID = "domoticz-destination-1"
STORAGE_KEY = "domoticz_sync.target_catalog.entry-1.domoticz-destination-1"
CATALOG = {"version": 1, "targets": []}


@pytest.fixture
def hass_config_dir(hass_tmp_config_dir: str) -> str:
    """Use an isolated configuration directory for persistent Store tests."""
    return hass_tmp_config_dir


@pytest.fixture
def hass_storage() -> dict[str, object]:
    """Exercise the real Store filesystem implementation in the temp directory."""
    return {}


async def test_missing_catalog_can_be_saved_and_loaded(
    hass: HomeAssistant,
) -> None:
    """A genuinely absent catalog is created with its exact namespace wrapper."""
    storage = HomeAssistantCatalogStorage(
        hass,
        entry_id=ENTRY_ID,
        destination_id=DESTINATION_ID,
    )

    assert await storage.async_load() is None

    await storage.async_save(CATALOG)

    assert await storage.async_load() == CATALOG
    raw_store = Store(
        hass,
        1,
        STORAGE_KEY,
        private=True,
        atomic_writes=True,
    )
    assert await raw_store.async_load() == {
        "entry_id": ENTRY_ID,
        "destination_id": DESTINATION_ID,
        "catalog": CATALOG,
    }


@pytest.mark.parametrize(
    "wrapper",
    [
        {
            "entry_id": "another-entry",
            "destination_id": DESTINATION_ID,
            "catalog": CATALOG,
        },
        {
            "entry_id": ENTRY_ID,
            "destination_id": "another-destination",
            "catalog": CATALOG,
        },
        {
            "entry_id": ENTRY_ID,
            "destination_id": DESTINATION_ID,
            "catalog": CATALOG,
            "unexpected": True,
        },
    ],
)
async def test_load_rejects_namespace_or_wrapper_mismatch(
    hass: HomeAssistant,
    wrapper: dict[str, object],
) -> None:
    """Persisted data must use only the exact expected namespace wrapper."""
    raw_store = Store(
        hass,
        1,
        STORAGE_KEY,
        private=True,
        atomic_writes=True,
    )
    await raw_store.async_save(wrapper)
    storage = HomeAssistantCatalogStorage(
        hass,
        entry_id=ENTRY_ID,
        destination_id=DESTINATION_ID,
    )

    with pytest.raises(
        CatalogStorageError,
        match="target catalog could not be loaded",
    ):
        await storage.async_load()


async def test_existing_corrupt_store_fails_closed(
    hass: HomeAssistant,
) -> None:
    """An existing file that Home Assistant loads as absent is not treated as new."""
    raw_store = Store(
        hass,
        1,
        STORAGE_KEY,
        private=True,
        atomic_writes=True,
    )
    await hass.async_add_executor_job(
        _write_corrupt_store,
        Path(raw_store.path),
    )
    storage = HomeAssistantCatalogStorage(
        hass,
        entry_id=ENTRY_ID,
        destination_id=DESTINATION_ID,
    )

    with pytest.raises(
        CatalogStorageError,
        match="target catalog could not be loaded",
    ):
        await storage.async_load()

    with pytest.raises(
        CatalogStorageError,
        match="target catalog storage is unavailable",
    ):
        await storage.async_load()

    replacement = HomeAssistantCatalogStorage(
        hass,
        entry_id=ENTRY_ID,
        destination_id=DESTINATION_ID,
    )
    with pytest.raises(
        CatalogStorageError,
        match="target catalog could not be loaded",
    ):
        await replacement.async_load()


async def test_valid_base_store_takes_precedence_over_old_corrupt_marker(
    hass: HomeAssistant,
) -> None:
    """A valid current catalog is accepted when an old marker also exists."""
    raw_store = Store(
        hass,
        1,
        STORAGE_KEY,
        private=True,
        atomic_writes=True,
    )
    await raw_store.async_save(
        {
            "entry_id": ENTRY_ID,
            "destination_id": DESTINATION_ID,
            "catalog": CATALOG,
        }
    )
    await hass.async_add_executor_job(
        _write_corrupt_marker,
        Path(f"{raw_store.path}.corrupt.previous"),
    )
    storage = HomeAssistantCatalogStorage(
        hass,
        entry_id=ENTRY_ID,
        destination_id=DESTINATION_ID,
    )

    assert await storage.async_load() == CATALOG


async def test_catalog_schema_is_validated_on_load_and_before_save(
    hass: HomeAssistant,
) -> None:
    """Only a valid neutral-core catalog document reaches either boundary."""
    invalid_catalog = {"version": 999, "targets": []}
    raw_store = Store(
        hass,
        1,
        STORAGE_KEY,
        private=True,
        atomic_writes=True,
    )
    await raw_store.async_save(
        {
            "entry_id": ENTRY_ID,
            "destination_id": DESTINATION_ID,
            "catalog": invalid_catalog,
        }
    )
    loader = HomeAssistantCatalogStorage(
        hass,
        entry_id=ENTRY_ID,
        destination_id=DESTINATION_ID,
    )

    with pytest.raises(
        CatalogStorageError,
        match="target catalog could not be loaded",
    ):
        await loader.async_load()

    writer = HomeAssistantCatalogStorage(
        hass,
        entry_id="entry-2",
        destination_id=DESTINATION_ID,
    )
    with pytest.raises(
        CatalogStorageError,
        match="target catalog could not be saved",
    ):
        await writer.async_save(invalid_catalog)


async def test_save_uses_fresh_store_and_rejects_unconfirmed_write(
    hass: HomeAssistant,
) -> None:
    """Pending data on the writer cannot masquerade as persisted data."""

    class PendingOnlyStore:
        """Store double whose writes exist only on the writing instance."""

        instances: list[PendingOnlyStore] = []

        def __init__(
            self,
            _hass: HomeAssistant,
            version: int,
            key: str,
            private: bool = False,
            *,
            atomic_writes: bool = False,
        ) -> None:
            self.version = version
            self.key = key
            self.private = private
            self.atomic_writes = atomic_writes
            self.path = "/not-used"
            self.pending: dict[str, object] | None = None
            self.instances.append(self)

        async def async_save(self, data: dict[str, object]) -> None:
            self.pending = deepcopy(data)

        async def async_load(self) -> dict[str, object] | None:
            return deepcopy(self.pending)

    with patch(
        "custom_components.domoticz_sync.catalog_storage.Store",
        PendingOnlyStore,
    ):
        storage = HomeAssistantCatalogStorage(
            hass,
            entry_id=ENTRY_ID,
            destination_id=DESTINATION_ID,
        )

        with pytest.raises(
            CatalogStorageError,
            match="target catalog save could not be confirmed",
        ):
            await storage.async_save(CATALOG)

    assert len(PendingOnlyStore.instances) == 2
    assert all(instance.version == 1 for instance in PendingOnlyStore.instances)
    assert all(instance.key == STORAGE_KEY for instance in PendingOnlyStore.instances)
    assert all(instance.private for instance in PendingOnlyStore.instances)
    assert all(instance.atomic_writes for instance in PendingOnlyStore.instances)


async def test_save_failure_raises_sanitized_storage_error(
    hass: HomeAssistant,
) -> None:
    """A write exception is contained without exposing its original detail."""

    class FailingStore:
        """Store double that exposes one write failure."""

        def __init__(
            self,
            _hass: HomeAssistant,
            _version: int,
            _key: str,
            private: bool = False,
            *,
            atomic_writes: bool = False,
        ) -> None:
            self.private = private
            self.atomic_writes = atomic_writes
            self.path = "/not-used"

        async def async_save(self, _data: dict[str, object]) -> None:
            raise OSError("private write detail")

        async def async_load(self) -> dict[str, object] | None:
            return None

    with patch(
        "custom_components.domoticz_sync.catalog_storage.Store",
        FailingStore,
    ):
        storage = HomeAssistantCatalogStorage(
            hass,
            entry_id=ENTRY_ID,
            destination_id=DESTINATION_ID,
        )

        with pytest.raises(
            CatalogStorageError,
            match="^target catalog could not be saved$",
        ) as raised:
            await storage.async_save(CATALOG)

    assert raised.value.__cause__ is None
    assert "private write detail" not in str(raised.value)


def _write_corrupt_store(path: Path) -> None:
    """Create an invalid Home Assistant Store file for a fail-closed test."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not-json", encoding="utf-8")


def _write_corrupt_marker(path: Path) -> None:
    """Create one old corruption marker alongside a valid Store file."""
    path.write_text("old marker", encoding="utf-8")
