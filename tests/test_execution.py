"""Tests for confirmation-based, restart-safe reconciliation execution."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

import pytest

from custom_components.domoticz_sync.core.capabilities import (
    Availability,
    Capability,
    CapabilityKind,
    SourceIdentity,
)
from custom_components.domoticz_sync.core.catalog import (
    CatalogFormatError,
    TargetCatalog,
    catalog_from_document,
)
from custom_components.domoticz_sync.core.execution import (
    ApplyConfirmation,
    CatalogStorageError,
    ExecutionConflictError,
    ExecutionStatus,
    ReconciliationExecutor,
    TargetActionError,
    TargetAdapterError,
    async_execute_reconciliation,
)
from custom_components.domoticz_sync.core.reconciliation import (
    ReconciliationAction,
    ReconciliationActionKind,
    SourceScope,
    TargetRecord,
)

_SCOPE = SourceScope("home_assistant", "instance-1")


def _source(object_id: str, *, instance_id: str = "instance-1") -> SourceIdentity:
    return SourceIdentity(
        system="home_assistant",
        instance_id=instance_id,
        object_id=object_id,
        capability_id="state",
    )


def _numeric(
    object_id: str,
    value: float | None = 21.5,
    availability: Availability = Availability.AVAILABLE,
) -> Capability:
    return Capability(
        source=_source(object_id),
        kind=CapabilityKind.NUMERIC,
        name=object_id.replace("-", " ").title(),
        value=value,
        availability=availability,
        semantic="temperature",
        unit="celsius",
    )


@dataclass(frozen=True)
class _FakeTarget:
    target_id: str
    capability: Capability
    stale: bool


class _FakeRemote:
    """Target-system state that survives executor and adapter restarts."""

    def __init__(self, events: list[str] | None = None) -> None:
        self.targets: dict[str, _FakeTarget] = {}
        self.next_target_number = 1
        self.events = events if events is not None else []

    def add(
        self,
        target_id: str,
        capability: Capability,
        *,
        stale: bool = False,
    ) -> None:
        """Add target state that exists independently from the catalog."""
        self.targets[target_id] = _FakeTarget(target_id, capability, stale)

    def for_source(self, source: SourceIdentity) -> list[_FakeTarget]:
        """Return every remote target carrying one source provenance key."""
        return [
            target
            for target in self.targets.values()
            if target.capability.source == source
        ]


class _FakeTargetAdapter:
    """Idempotent full-state target adapter with controlled failures."""

    def __init__(self, remote: _FakeRemote) -> None:
        self.remote = remote
        self.calls: list[ReconciliationAction] = []
        self.fail_sources: set[SourceIdentity] = set()
        self.mutate_then_fail_sources: set[SourceIdentity] = set()
        self.fail_globally = False
        self.confirm_wrong_source = False
        self.confirm_wrong_target = False

    async def async_apply(
        self,
        action: ReconciliationAction,
    ) -> ApplyConfirmation:
        """Ensure one complete desired state and confirm its stable target."""
        await asyncio.sleep(0)
        self.calls.append(action)
        source = action.capability.source
        self.remote.events.append(f"apply:{source.object_id}")

        if self.fail_globally:
            raise TargetAdapterError("target instance unavailable")
        if source in self.fail_sources:
            raise TargetActionError("one target rejected the desired state")

        if action.kind is ReconciliationActionKind.CREATE:
            matches = self.remote.for_source(source)
            if len(matches) > 1:
                raise TargetActionError("ambiguous source provenance")
            if matches:
                target_id = matches[0].target_id
            else:
                target_id = f"target-{self.remote.next_target_number}"
                self.remote.next_target_number += 1
        else:
            assert action.target_id is not None
            target_id = action.target_id
            existing = self.remote.targets.get(target_id)
            if existing is None or existing.capability.source != source:
                raise TargetActionError("target mapping no longer exists")

        self.remote.targets[target_id] = _FakeTarget(
            target_id,
            action.capability,
            action.stale,
        )
        if source in self.mutate_then_fail_sources:
            raise TargetActionError("confirmation was lost after mutation")

        confirmation_source = _source("wrong") if self.confirm_wrong_source else source
        confirmation_target = (
            "different-target" if self.confirm_wrong_target else target_id
        )
        return ApplyConfirmation(confirmation_target, confirmation_source)


class _FakeCatalogStorage:
    """Atomic in-memory document storage with ambiguous failure injection."""

    def __init__(
        self,
        document: object = None,
        events: list[str] | None = None,
    ) -> None:
        self.document = deepcopy(document)
        self.events = events if events is not None else []
        self.load_calls = 0
        self.save_calls = 0
        self.fail_load = False
        self.fail_before_save_calls: set[int] = set()
        self.fail_after_save_calls: set[int] = set()

    async def async_load(self) -> Any:
        """Return an isolated copy of durable state."""
        self.load_calls += 1
        if self.fail_load:
            raise CatalogStorageError("catalog unavailable")
        return deepcopy(self.document)

    async def async_save(self, document: Any) -> None:
        """Atomically replace durable state or simulate uncertain failure."""
        self.save_calls += 1
        self.events.append(f"save:{self.save_calls}")
        if self.save_calls in self.fail_before_save_calls:
            raise CatalogStorageError("save failed before replacement")
        self.document = deepcopy(document)
        if self.save_calls in self.fail_after_save_calls:
            raise CatalogStorageError("save confirmation was lost")

    def catalog(self) -> TargetCatalog:
        """Decode currently durable state for assertions."""
        return catalog_from_document(self.document)


@pytest.mark.asyncio
async def test_create_is_confirmed_before_catalog_commit() -> None:
    """A new mapping becomes durable only after its target confirms."""
    events: list[str] = []
    remote = _FakeRemote(events)
    storage = _FakeCatalogStorage(events=events)
    executor = ReconciliationExecutor(_FakeTargetAdapter(remote), storage)
    capability = _numeric("sensor-a")

    report = await executor.async_reconcile(_SCOPE, [capability])

    assert events == ["apply:sensor-a", "save:1"]
    assert [result.status for result in report.results] == [ExecutionStatus.COMMITTED]
    record = report.catalog.get(capability.source)
    assert record is not None
    assert record.target_id == "target-1"
    assert record.capability == capability
    assert storage.catalog() == report.catalog
    assert len(remote.targets) == 1


@pytest.mark.asyncio
async def test_restart_with_committed_catalog_is_a_no_op() -> None:
    """Reloading durable mappings avoids duplicate targets and writes."""
    remote = _FakeRemote()
    storage = _FakeCatalogStorage()
    first_adapter = _FakeTargetAdapter(remote)
    await ReconciliationExecutor(first_adapter, storage).async_reconcile(
        _SCOPE,
        [_numeric("sensor-a")],
    )
    saves_after_first_run = storage.save_calls
    second_adapter = _FakeTargetAdapter(remote)

    report = await ReconciliationExecutor(
        second_adapter,
        storage,
    ).async_reconcile(_SCOPE, [_numeric("sensor-a")])

    assert report.actions == ()
    assert report.results == ()
    assert second_adapter.calls == []
    assert storage.save_calls == saves_after_first_run
    assert len(remote.targets) == 1


@pytest.mark.asyncio
async def test_update_commits_complete_new_state() -> None:
    """A changed capability updates the same target and durable record."""
    remote = _FakeRemote()
    storage = _FakeCatalogStorage()
    executor = ReconciliationExecutor(_FakeTargetAdapter(remote), storage)
    await executor.async_reconcile(_SCOPE, [_numeric("sensor-a", 20.0)])

    changed = _numeric("sensor-a", 22.0)
    report = await executor.async_reconcile(_SCOPE, [changed])

    assert report.actions[0].kind is ReconciliationActionKind.UPDATE
    record = report.catalog.get(changed.source)
    assert record is not None
    assert record.target_id == "target-1"
    assert record.capability == changed
    assert remote.targets["target-1"].capability == changed


@pytest.mark.asyncio
async def test_initially_unavailable_capability_is_created_without_a_value() -> None:
    """Selection can create a target before its first trustworthy reading."""
    capability = _numeric(
        "sensor-a",
        None,
        Availability.UNAVAILABLE,
    )
    remote = _FakeRemote()
    storage = _FakeCatalogStorage()

    report = await ReconciliationExecutor(
        _FakeTargetAdapter(remote),
        storage,
    ).async_reconcile(_SCOPE, [capability])

    assert report.actions[0].kind is ReconciliationActionKind.CREATE
    assert report.catalog.records[0].capability == capability
    assert not report.catalog.records[0].stale


@pytest.mark.parametrize(
    "availability",
    (Availability.UNKNOWN, Availability.UNAVAILABLE),
)
@pytest.mark.asyncio
async def test_committed_non_available_state_is_reasserted(
    availability: Availability,
) -> None:
    """Every connection reapplies runtime-only target availability."""
    capability = _numeric("sensor-a", None, availability)
    remote = _FakeRemote()
    storage = _FakeCatalogStorage()
    adapter = _FakeTargetAdapter(remote)
    executor = ReconciliationExecutor(adapter, storage)
    await executor.async_reconcile(_SCOPE, [capability])
    calls_after_create = len(adapter.calls)

    repeated = await executor.async_reconcile(_SCOPE, [capability])

    assert repeated.actions[0].kind is ReconciliationActionKind.MARK_UNAVAILABLE
    assert repeated.results[0].status is ExecutionStatus.COMMITTED
    assert len(adapter.calls) == calls_after_create + 1
    assert adapter.calls[-1] == repeated.actions[0]
    assert not repeated.catalog.records[0].stale


@pytest.mark.asyncio
async def test_explicit_unavailable_missing_and_reappearance_are_distinct() -> None:
    """Stale tracks disappearance separately from explicit source availability."""
    remote = _FakeRemote()
    storage = _FakeCatalogStorage()
    executor = ReconciliationExecutor(_FakeTargetAdapter(remote), storage)
    available = _numeric("sensor-a", 20.0)
    await executor.async_reconcile(_SCOPE, [available])

    explicit_unknown = _numeric("sensor-a", None, Availability.UNKNOWN)
    unknown_report = await executor.async_reconcile(_SCOPE, [explicit_unknown])
    assert unknown_report.actions[0].kind is ReconciliationActionKind.MARK_UNAVAILABLE
    assert not unknown_report.catalog.records[0].stale
    assert (
        unknown_report.catalog.records[0].capability.availability
        is Availability.UNKNOWN
    )

    missing_report = await executor.async_reconcile(_SCOPE, [])
    assert missing_report.catalog.records[0].stale
    assert (
        missing_report.catalog.records[0].capability.availability
        is Availability.UNAVAILABLE
    )

    repeated = await executor.async_reconcile(_SCOPE, [])
    assert repeated.actions[0].kind is ReconciliationActionKind.MARK_UNAVAILABLE
    assert repeated.results[0].status is ExecutionStatus.COMMITTED
    assert repeated.catalog.records[0].stale

    returned = await executor.async_reconcile(_SCOPE, [available])
    assert returned.actions[0].kind is ReconciliationActionKind.UPDATE
    assert not returned.catalog.records[0].stale


@pytest.mark.asyncio
async def test_create_save_failure_retries_by_adopting_the_same_target() -> None:
    """The create-save crash window cannot produce a duplicate target."""
    remote = _FakeRemote()
    storage = _FakeCatalogStorage()
    storage.fail_before_save_calls.add(1)
    capability = _numeric("sensor-a")

    failed = await ReconciliationExecutor(
        _FakeTargetAdapter(remote),
        storage,
    ).async_reconcile(_SCOPE, [capability])

    assert failed.results[0].status is ExecutionStatus.APPLIED_NOT_COMMITTED
    assert failed.persistence_uncertain
    assert failed.catalog == TargetCatalog()
    assert storage.document is None
    assert len(remote.targets) == 1
    original_target_id = next(iter(remote.targets))

    retried = await ReconciliationExecutor(
        _FakeTargetAdapter(remote),
        storage,
    ).async_reconcile(_SCOPE, [capability])

    assert retried.results[0].status is ExecutionStatus.COMMITTED
    assert retried.catalog.records[0].target_id == original_target_id
    assert len(remote.targets) == 1


@pytest.mark.asyncio
async def test_lost_target_confirmation_retries_without_duplicate_create() -> None:
    """A mutation followed by an entity error remains safe to replay."""
    remote = _FakeRemote()
    storage = _FakeCatalogStorage()
    first_adapter = _FakeTargetAdapter(remote)
    first_adapter.mutate_then_fail_sources.add(_source("sensor-a"))
    capability = _numeric("sensor-a")

    failed = await ReconciliationExecutor(
        first_adapter,
        storage,
    ).async_reconcile(_SCOPE, [capability])

    assert failed.results[0].status is ExecutionStatus.TARGET_NOT_CONFIRMED
    assert failed.catalog == TargetCatalog()
    assert storage.document is None
    assert len(remote.targets) == 1

    retried = await ReconciliationExecutor(
        _FakeTargetAdapter(remote),
        storage,
    ).async_reconcile(_SCOPE, [capability])

    assert retried.results[0].status is ExecutionStatus.COMMITTED
    assert retried.catalog.records[0].target_id == "target-1"
    assert len(remote.targets) == 1


@pytest.mark.asyncio
async def test_save_may_have_landed_before_confirmation_was_lost() -> None:
    """A fresh load resolves an ambiguous storage acknowledgement safely."""
    remote = _FakeRemote()
    storage = _FakeCatalogStorage()
    storage.fail_after_save_calls.add(1)
    capability = _numeric("sensor-a")

    failed = await ReconciliationExecutor(
        _FakeTargetAdapter(remote),
        storage,
    ).async_reconcile(_SCOPE, [capability])

    assert failed.persistence_uncertain
    assert failed.catalog == TargetCatalog()
    assert len(storage.catalog()) == 1

    retried = await ReconciliationExecutor(
        _FakeTargetAdapter(remote),
        storage,
    ).async_reconcile(_SCOPE, [capability])

    assert retried.actions == ()
    assert len(remote.targets) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changed", "expected_kind"),
    (
        (_numeric("sensor-a", 25.0), ReconciliationActionKind.UPDATE),
        (
            _numeric("sensor-a", None, Availability.UNAVAILABLE),
            ReconciliationActionKind.MARK_UNAVAILABLE,
        ),
    ),
)
async def test_changed_state_is_retryable_after_save_failure(
    changed: Capability,
    expected_kind: ReconciliationActionKind,
) -> None:
    """Repeated update and unavailable operations converge after restart."""
    remote = _FakeRemote()
    storage = _FakeCatalogStorage()
    await ReconciliationExecutor(
        _FakeTargetAdapter(remote),
        storage,
    ).async_reconcile(_SCOPE, [_numeric("sensor-a", 20.0)])
    storage.fail_before_save_calls.add(2)

    failed = await ReconciliationExecutor(
        _FakeTargetAdapter(remote),
        storage,
    ).async_reconcile(_SCOPE, [changed])

    assert failed.actions[0].kind is expected_kind
    assert failed.results[0].status is ExecutionStatus.APPLIED_NOT_COMMITTED
    assert storage.catalog().records[0].capability.value == 20.0
    assert remote.targets["target-1"].capability == changed

    retried = await ReconciliationExecutor(
        _FakeTargetAdapter(remote),
        storage,
    ).async_reconcile(_SCOPE, [changed])

    assert retried.results[0].status is ExecutionStatus.COMMITTED
    assert retried.catalog.records[0].capability == changed
    assert len(remote.targets) == 1


@pytest.mark.asyncio
async def test_stale_mark_is_retryable_after_save_failure() -> None:
    """A missing source eventually persists stale without deleting its target."""
    remote = _FakeRemote()
    storage = _FakeCatalogStorage()
    await ReconciliationExecutor(
        _FakeTargetAdapter(remote),
        storage,
    ).async_reconcile(_SCOPE, [_numeric("sensor-a")])
    storage.fail_before_save_calls.add(2)

    failed = await ReconciliationExecutor(
        _FakeTargetAdapter(remote),
        storage,
    ).async_reconcile(_SCOPE, [])

    assert failed.results[0].status is ExecutionStatus.APPLIED_NOT_COMMITTED
    assert remote.targets["target-1"].stale
    assert not storage.catalog().records[0].stale

    retried = await ReconciliationExecutor(
        _FakeTargetAdapter(remote),
        storage,
    ).async_reconcile(_SCOPE, [])

    assert retried.results[0].status is ExecutionStatus.COMMITTED
    assert retried.catalog.records[0].stale
    assert len(remote.targets) == 1


@pytest.mark.asyncio
async def test_entity_failure_does_not_block_unrelated_actions() -> None:
    """An expected per-target rejection leaves other mappings progressing."""
    remote = _FakeRemote()
    storage = _FakeCatalogStorage()
    adapter = _FakeTargetAdapter(remote)
    adapter.fail_sources.add(_source("sensor-b"))
    executor = ReconciliationExecutor(adapter, storage)

    report = await executor.async_reconcile(
        _SCOPE,
        [_numeric("sensor-c"), _numeric("sensor-b"), _numeric("sensor-a")],
    )

    assert [result.status for result in report.results] == [
        ExecutionStatus.COMMITTED,
        ExecutionStatus.TARGET_NOT_CONFIRMED,
        ExecutionStatus.COMMITTED,
    ]
    assert [
        record.capability.source.object_id for record in report.catalog.records
    ] == ["sensor-a", "sensor-c"]
    assert storage.save_calls == 2
    assert set(remote.targets) == {"target-1", "target-2"}


@pytest.mark.asyncio
async def test_catalog_save_failure_stops_later_target_operations() -> None:
    """A global durability failure does not widen the uncertain mutation window."""
    remote = _FakeRemote()
    storage = _FakeCatalogStorage()
    storage.fail_before_save_calls.add(1)
    adapter = _FakeTargetAdapter(remote)

    report = await ReconciliationExecutor(adapter, storage).async_reconcile(
        _SCOPE,
        [_numeric("sensor-b"), _numeric("sensor-a")],
    )

    assert [call.capability.source.object_id for call in adapter.calls] == ["sensor-a"]
    assert [
        action.capability.source.object_id for action in report.remaining_actions
    ] == ["sensor-b"]
    assert len(remote.targets) == 1
    assert storage.document is None


@pytest.mark.asyncio
async def test_earlier_commits_survive_a_later_save_failure() -> None:
    """Copy-on-write retains the last atomically confirmed catalog."""
    remote = _FakeRemote()
    storage = _FakeCatalogStorage()
    storage.fail_before_save_calls.add(2)
    adapter = _FakeTargetAdapter(remote)

    report = await ReconciliationExecutor(adapter, storage).async_reconcile(
        _SCOPE,
        [_numeric("sensor-c"), _numeric("sensor-b"), _numeric("sensor-a")],
    )

    assert [result.status for result in report.results] == [
        ExecutionStatus.COMMITTED,
        ExecutionStatus.APPLIED_NOT_COMMITTED,
    ]
    assert [
        record.capability.source.object_id for record in report.catalog.records
    ] == ["sensor-a"]
    assert storage.catalog() == report.catalog
    assert [
        action.capability.source.object_id for action in report.remaining_actions
    ] == ["sensor-c"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "document",
    (
        {},
        {"version": 999, "targets": []},
        {"version": 1, "targets": "not-a-list"},
    ),
)
async def test_corrupt_or_future_catalog_aborts_before_target_calls(
    document: object,
) -> None:
    """Persisted ambiguity never becomes an empty catalog and duplicate create."""
    remote = _FakeRemote()
    adapter = _FakeTargetAdapter(remote)
    executor = ReconciliationExecutor(adapter, _FakeCatalogStorage(document))

    with pytest.raises(CatalogFormatError):
        await executor.async_reconcile(_SCOPE, [_numeric("sensor-a")])

    assert adapter.calls == []
    assert remote.targets == {}


@pytest.mark.asyncio
async def test_catalog_load_failure_aborts_before_target_calls() -> None:
    """Unavailable persistence is never mistaken for an empty catalog."""
    remote = _FakeRemote()
    adapter = _FakeTargetAdapter(remote)
    storage = _FakeCatalogStorage()
    storage.fail_load = True

    with pytest.raises(CatalogStorageError):
        await ReconciliationExecutor(adapter, storage).async_reconcile(
            _SCOPE,
            [_numeric("sensor-a")],
        )

    assert adapter.calls == []
    assert remote.targets == {}


@pytest.mark.asyncio
async def test_concurrent_runs_share_one_catalog_wide_lock() -> None:
    """Two simultaneous empty-catalog snapshots still create one target."""
    remote = _FakeRemote()
    storage = _FakeCatalogStorage()
    adapter = _FakeTargetAdapter(remote)
    executor = ReconciliationExecutor(adapter, storage)
    capability = _numeric("sensor-a")

    first, second = await asyncio.gather(
        executor.async_reconcile(_SCOPE, [capability]),
        executor.async_reconcile(_SCOPE, [capability]),
    )

    assert len(remote.targets) == 1
    assert storage.save_calls == 1
    assert sorted((len(first.actions), len(second.actions))) == [0, 1]


@pytest.mark.asyncio
async def test_unrecorded_remote_target_is_adopted() -> None:
    """An orphan from a create-save failure is found by source provenance."""
    capability = _numeric("sensor-a")
    remote = _FakeRemote()
    remote.add("target-existing", capability)
    storage = _FakeCatalogStorage()

    report = await ReconciliationExecutor(
        _FakeTargetAdapter(remote),
        storage,
    ).async_reconcile(_SCOPE, [capability])

    assert report.catalog.records[0].target_id == "target-existing"
    assert len(remote.targets) == 1


@pytest.mark.asyncio
async def test_ambiguous_remote_provenance_fails_closed() -> None:
    """Multiple orphan matches are never guessed or persisted."""
    capability = _numeric("sensor-a")
    remote = _FakeRemote()
    remote.add("target-a", capability)
    remote.add("target-b", capability)
    storage = _FakeCatalogStorage()

    report = await ReconciliationExecutor(
        _FakeTargetAdapter(remote),
        storage,
    ).async_reconcile(_SCOPE, [capability])

    assert report.results[0].status is ExecutionStatus.TARGET_NOT_CONFIRMED
    assert report.catalog == TargetCatalog()
    assert storage.save_calls == 0
    assert len(remote.targets) == 2


@pytest.mark.asyncio
async def test_wrong_confirmation_is_never_persisted() -> None:
    """An adapter cannot redirect a source through its confirmation."""
    remote = _FakeRemote()
    storage = _FakeCatalogStorage()
    adapter = _FakeTargetAdapter(remote)
    adapter.confirm_wrong_source = True

    with pytest.raises(ExecutionConflictError, match="different source"):
        await ReconciliationExecutor(adapter, storage).async_reconcile(
            _SCOPE,
            [_numeric("sensor-a")],
        )

    assert storage.save_calls == 0
    assert storage.document is None


@pytest.mark.asyncio
async def test_stale_plan_is_rejected_before_target_side_effects() -> None:
    """Execution cannot apply an action whose target disagrees with the catalog."""
    capability = _numeric("sensor-a", 20.0)
    catalog = TargetCatalog([TargetRecord("target-1", capability)])
    action = ReconciliationAction(
        kind=ReconciliationActionKind.UPDATE,
        target_id="different-target",
        capability=_numeric("sensor-a", 21.0),
    )
    remote = _FakeRemote()
    adapter = _FakeTargetAdapter(remote)
    storage = _FakeCatalogStorage(catalog.to_dict())

    with pytest.raises(ExecutionConflictError, match="does not match"):
        await async_execute_reconciliation(catalog, [action], adapter, storage)

    assert adapter.calls == []
    assert storage.save_calls == 0


@pytest.mark.asyncio
async def test_target_wide_failure_aborts_instead_of_repeating_per_entity() -> None:
    """Authentication or connection failure is not treated as entity-local."""
    remote = _FakeRemote()
    adapter = _FakeTargetAdapter(remote)
    adapter.fail_globally = True
    storage = _FakeCatalogStorage()

    with pytest.raises(TargetAdapterError):
        await ReconciliationExecutor(adapter, storage).async_reconcile(
            _SCOPE,
            [_numeric("sensor-a"), _numeric("sensor-b")],
        )

    assert len(adapter.calls) == 1
    assert storage.save_calls == 0


@pytest.mark.asyncio
async def test_result_never_contains_adapter_error_message() -> None:
    """Shared reports expose an error category without leaking message content."""
    remote = _FakeRemote()
    adapter = _FakeTargetAdapter(remote)
    adapter.fail_sources.add(_source("sensor-a"))

    report = await ReconciliationExecutor(
        adapter,
        _FakeCatalogStorage(),
    ).async_reconcile(_SCOPE, [_numeric("sensor-a")])

    assert "rejected the desired state" not in repr(report)


def test_step_five_public_contract_has_no_delete_operation() -> None:
    """The persistence and execution layers remain deliberately non-destructive."""
    assert not hasattr(TargetCatalog, "remove")
    assert not hasattr(_FakeTargetAdapter, "async_delete")
