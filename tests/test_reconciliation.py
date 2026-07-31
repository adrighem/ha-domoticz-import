"""Tests for transport-neutral reconciliation planning."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace

import pytest

from custom_components.domoticz_sync.core import (
    Availability,
    Capability,
    CapabilityKind,
    ReconciliationAction,
    ReconciliationActionKind,
    SourceIdentity,
    SourceScope,
    TargetBindingError,
    TargetObservation,
    TargetRecord,
    derive_domoticz_target_id,
    plan_reconciliation,
    validate_deterministic_target_bindings,
    validate_deterministic_target_ownership,
)
from custom_components.domoticz_sync.core import reconciliation as reconciliation_module

_SCOPE = SourceScope(system="home_assistant", instance_id="instance-1")


def _source(
    object_id: str,
    *,
    capability_id: str = "state",
    system: str = "home_assistant",
    instance_id: str = "instance-1",
) -> SourceIdentity:
    return SourceIdentity(
        system=system,
        instance_id=instance_id,
        object_id=object_id,
        capability_id=capability_id,
    )


def _numeric(
    object_id: str,
    value: float | None = 21.5,
    availability: Availability = Availability.AVAILABLE,
    *,
    name: str = "Temperature",
    semantic: str | None = "temperature",
    unit: str | None = "celsius",
    state_class: str | None = None,
) -> Capability:
    return Capability(
        source=_source(object_id),
        kind=CapabilityKind.NUMERIC,
        name=name,
        value=value,
        availability=availability,
        semantic=semantic,
        unit=unit,
        state_class=state_class,
    )


def _target(
    object_id: str,
    target_id: str,
    capability: Capability | None = None,
) -> TargetRecord:
    return TargetRecord(
        target_id=target_id,
        capability=capability or _numeric(object_id),
    )


def _plan(
    current: list[Capability],
    known_targets: list[TargetRecord],
    observations: list[TargetObservation] | None = None,
) -> tuple[ReconciliationAction, ...]:
    return plan_reconciliation(
        _SCOPE,
        current,
        known_targets,
        observations=observations,
    )


def test_empty_snapshots_need_no_actions() -> None:
    """An empty initial synchronization is a no-op."""
    assert _plan([], []) == ()


def test_action_kinds_are_deliberately_non_destructive() -> None:
    """Pruning cannot appear accidentally in the Step 4 planner."""
    assert set(ReconciliationActionKind) == {
        ReconciliationActionKind.CREATE,
        ReconciliationActionKind.UPDATE,
        ReconciliationActionKind.MARK_UNAVAILABLE,
    }


def test_new_capabilities_are_created_in_source_identity_order() -> None:
    """Input order cannot change the order in which targets are created."""
    capability_b = _numeric("entity-b", 2)
    capability_a = _numeric("entity-a", 1)

    actions = _plan([capability_b, capability_a], [])

    assert actions == (
        ReconciliationAction(
            kind=ReconciliationActionKind.CREATE,
            capability=capability_a,
        ),
        ReconciliationAction(
            kind=ReconciliationActionKind.CREATE,
            capability=capability_b,
        ),
    )


def test_initially_unavailable_capability_is_created() -> None:
    """Selection creates a target even before its first trustworthy value."""
    capability = _numeric(
        "unavailable",
        None,
        Availability.UNAVAILABLE,
    )

    assert _plan([capability], []) == (
        ReconciliationAction(
            kind=ReconciliationActionKind.CREATE,
            capability=capability,
        ),
    )


def test_unchanged_capability_needs_no_action() -> None:
    """Full equality suppresses unnecessary target writes."""
    capability = _numeric("same")

    assert (
        _plan(
            [capability],
            [TargetRecord("target-1", capability)],
        )
        == ()
    )


def test_pending_unchanged_capability_is_retried_without_inventory() -> None:
    """A durable pending intent recovers when the peer lacks inventory support."""
    capability = _numeric("pending")

    assert _plan(
        [capability],
        [TargetRecord("opaque-target", capability, pending=True)],
    ) == (
        ReconciliationAction(
            kind=ReconciliationActionKind.UPDATE,
            target_id="opaque-target",
            capability=capability,
        ),
    )


@pytest.mark.parametrize(
    "availability", (Availability.UNKNOWN, Availability.UNAVAILABLE)
)
def test_pending_non_available_capability_is_retried_without_inventory(
    availability: Availability,
) -> None:
    """Legacy recovery keeps the explicit non-available action kind."""
    capability = _numeric("pending", None, availability)

    actions = _plan(
        [capability],
        [TargetRecord("opaque-target", capability, pending=True)],
    )

    assert actions[0].kind is ReconciliationActionKind.MARK_UNAVAILABLE
    assert not actions[0].stale


@pytest.mark.parametrize(
    "changed",
    (
        _numeric("changed", 22.0),
        _numeric("changed", name="Living room temperature"),
        _numeric("changed", semantic="humidity"),
        _numeric("changed", unit="fahrenheit"),
        _numeric("changed", state_class="measurement"),
        Capability(
            source=_source("changed"),
            kind=CapabilityKind.BINARY,
            name="Temperature",
            value=True,
            semantic="temperature",
        ),
    ),
    ids=(
        "value",
        "name",
        "semantic",
        "unit",
        "state_class",
        "kind",
    ),
)
def test_any_capability_change_is_updated(changed: Capability) -> None:
    """Available updates compare the complete snapshot, not only its value."""
    previous = _numeric("changed")

    assert _plan(
        [changed],
        [TargetRecord("opaque-target", previous)],
    ) == (
        ReconciliationAction(
            kind=ReconciliationActionKind.UPDATE,
            target_id="opaque-target",
            capability=changed,
        ),
    )


@pytest.mark.parametrize(
    "availability",
    (Availability.UNKNOWN, Availability.UNAVAILABLE),
)
def test_explicit_non_available_change_is_preserved(
    availability: Availability,
) -> None:
    """An explicit source state is not confused with a missing capability."""
    changed = _numeric("changed", None, availability)

    assert _plan(
        [changed],
        [TargetRecord("opaque-target", _numeric("changed"))],
    ) == (
        ReconciliationAction(
            kind=ReconciliationActionKind.MARK_UNAVAILABLE,
            target_id="opaque-target",
            capability=changed,
            stale=False,
        ),
    )


@pytest.mark.parametrize(
    "availability",
    (Availability.UNKNOWN, Availability.UNAVAILABLE),
)
def test_unchanged_non_available_state_is_reasserted(
    availability: Availability,
) -> None:
    """Runtime-only target availability is restored on every connection."""
    capability = _numeric("unchanged", None, availability)

    assert _plan(
        [capability],
        [TargetRecord("target-7", capability)],
    ) == (
        ReconciliationAction(
            kind=ReconciliationActionKind.MARK_UNAVAILABLE,
            target_id="target-7",
            capability=capability,
            stale=False,
        ),
    )


def test_equivalent_numeric_int_and_float_values_need_no_update() -> None:
    """Normal numeric equality avoids churn without adding a tolerance."""
    previous = _numeric("changed", 1)
    current = _numeric("changed", 1.0)

    assert _plan([current], [TargetRecord("target-1", previous)]) == ()


def test_missing_known_target_is_marked_unavailable() -> None:
    """Disappearing capabilities retain identity and metadata without deletion."""
    previous = _numeric("missing")

    assert _plan([], [TargetRecord("target-7", previous)]) == (
        ReconciliationAction(
            kind=ReconciliationActionKind.MARK_UNAVAILABLE,
            target_id="target-7",
            capability=replace(
                previous,
                value=None,
                availability=Availability.UNAVAILABLE,
            ),
            stale=True,
        ),
    )


def test_unknown_missing_target_is_marked_unavailable() -> None:
    """A missing unknown capability advances to unavailable."""
    previous = _numeric("missing", None, Availability.UNKNOWN)

    action = _plan([], [TargetRecord("target-7", previous)])

    assert action[0].kind is ReconciliationActionKind.MARK_UNAVAILABLE
    assert action[0].capability.availability is Availability.UNAVAILABLE
    assert action[0].stale


def test_explicitly_unavailable_missing_target_becomes_stale() -> None:
    """Missing is recorded even if the last explicit state was unavailable."""
    previous = _numeric("missing", None, Availability.UNAVAILABLE)

    assert _plan([], [TargetRecord("target-7", previous)]) == (
        ReconciliationAction(
            kind=ReconciliationActionKind.MARK_UNAVAILABLE,
            target_id="target-7",
            capability=previous,
            stale=True,
        ),
    )


def test_stale_mark_unavailable_is_reasserted() -> None:
    """A missing target's runtime-only timeout is restored after restart."""
    previous = _numeric("missing", None, Availability.UNAVAILABLE)

    assert _plan([], [TargetRecord("target-7", previous, stale=True)]) == (
        ReconciliationAction(
            kind=ReconciliationActionKind.MARK_UNAVAILABLE,
            target_id="target-7",
            capability=previous,
            stale=True,
        ),
    )


def test_available_reappearance_clears_stale_state_with_update() -> None:
    """A source returning with a value makes its existing target current."""
    stale = _numeric("returning", None, Availability.UNAVAILABLE)
    current = _numeric("returning", 19.0)

    assert _plan(
        [current],
        [TargetRecord("target-7", stale, stale=True)],
    ) == (
        ReconciliationAction(
            kind=ReconciliationActionKind.UPDATE,
            target_id="target-7",
            capability=current,
            stale=False,
        ),
    )


@pytest.mark.parametrize(
    "availability",
    (Availability.UNKNOWN, Availability.UNAVAILABLE),
)
def test_non_available_reappearance_clears_stale_state(
    availability: Availability,
) -> None:
    """A present non-value is explicit source state, no longer stale."""
    stale = _numeric("returning", None, Availability.UNAVAILABLE)
    current = _numeric("returning", None, availability)

    assert _plan(
        [current],
        [TargetRecord("target-7", stale, stale=True)],
    ) == (
        ReconciliationAction(
            kind=ReconciliationActionKind.MARK_UNAVAILABLE,
            target_id="target-7",
            capability=current,
            stale=False,
        ),
    )


def test_mixed_actions_have_one_deterministic_order() -> None:
    """Action kind and input collection order do not affect plan ordering."""
    missing = _numeric("entity-c", 3)
    changed_before = _numeric("entity-b", 1)
    changed_now = _numeric("entity-b", 2)
    created = _numeric("entity-a", 1)

    actions = _plan(
        [changed_now, created],
        [
            TargetRecord("target-c", missing),
            TargetRecord("target-b", changed_before),
        ],
    )

    assert [action.kind for action in actions] == [
        ReconciliationActionKind.CREATE,
        ReconciliationActionKind.UPDATE,
        ReconciliationActionKind.MARK_UNAVAILABLE,
    ]
    assert [action.capability.source.object_id for action in actions] == [
        "entity-a",
        "entity-b",
        "entity-c",
    ]


def test_source_identity_change_creates_new_and_marks_old_unavailable() -> None:
    """Source identity is the reconciliation key and is never guessed."""
    old = _numeric("old-entity")
    new = _numeric("new-entity")

    actions = _plan(
        [new],
        [TargetRecord("target-old", old)],
    )

    assert [action.kind for action in actions] == [
        ReconciliationActionKind.CREATE,
        ReconciliationActionKind.MARK_UNAVAILABLE,
    ]


def test_capability_id_is_part_of_source_identity() -> None:
    """One source object may expose multiple independent capabilities."""
    temperature = replace(
        _numeric("multi", 20),
        source=_source("multi", capability_id="temperature"),
    )
    humidity = replace(
        _numeric("multi", 50, semantic="humidity", unit="percent"),
        source=_source("multi", capability_id="humidity"),
    )

    actions = _plan([temperature, humidity], [])

    assert len(actions) == 2
    assert {action.capability.source.capability_id for action in actions} == {
        "temperature",
        "humidity",
    }


def test_current_capability_outside_scope_is_rejected() -> None:
    """A partial snapshot cannot affect or silently include another instance."""
    outside = replace(
        _numeric("outside"),
        source=_source("outside", instance_id="instance-2"),
    )

    with pytest.raises(ValueError, match="outside source scope"):
        _plan([outside], [])


def test_known_target_outside_scope_is_ignored() -> None:
    """An empty snapshot only marks targets belonging to its source instance."""
    outside = replace(
        _numeric("outside"),
        source=_source("outside", instance_id="instance-2"),
    )

    assert _plan([], [TargetRecord("other-target", outside)]) == ()


def test_plan_requires_source_scope() -> None:
    """Planning cannot proceed without an explicit source boundary."""
    with pytest.raises(TypeError, match="scope must be a SourceScope"):
        plan_reconciliation(None, [], [])


@pytest.mark.parametrize("field_name", ("system", "instance_id"))
def test_source_scope_requires_non_empty_components(field_name: str) -> None:
    """A scope must identify exactly one source-system instance."""
    values = {"system": "home_assistant", "instance_id": "instance-1"}
    values[field_name] = " "

    with pytest.raises(ValueError, match=field_name):
        SourceScope(**values)


def test_source_scope_rejects_surrounding_whitespace() -> None:
    """Scope matching cannot depend on invisible whitespace."""
    with pytest.raises(ValueError, match="surrounding whitespace"):
        SourceScope(system="home_assistant", instance_id=" instance-1")


def test_duplicate_current_source_identity_is_rejected() -> None:
    """A snapshot cannot contain two values for one source identity."""
    capability = _numeric("duplicate")

    with pytest.raises(ValueError, match="duplicate source identity"):
        _plan([capability, capability], [])


def test_duplicate_known_source_identity_is_rejected() -> None:
    """One source identity cannot map to multiple target records."""
    capability = _numeric("duplicate")

    with pytest.raises(ValueError, match="duplicate source identity"):
        _plan(
            [],
            [
                TargetRecord("target-1", capability),
                TargetRecord("target-2", capability),
            ],
        )


def test_duplicate_target_id_is_rejected() -> None:
    """One opaque target ID cannot represent multiple source identities."""
    with pytest.raises(ValueError, match="duplicate target_id"):
        _plan(
            [],
            [
                _target("entity-1", "same-target"),
                _target("entity-2", "same-target"),
            ],
        )


def test_duplicate_target_id_outside_scope_is_still_rejected() -> None:
    """Corrupt global target mappings fail closed in every scoped plan."""
    outside = replace(
        _numeric("outside"),
        source=_source("outside", instance_id="instance-2"),
    )

    with pytest.raises(ValueError, match="duplicate target_id"):
        _plan(
            [],
            [
                _target("inside", "same-target"),
                TargetRecord("same-target", outside),
            ],
        )


@pytest.mark.parametrize("target_id", ("", "  "))
def test_target_record_requires_non_empty_target_id(target_id: str) -> None:
    """Known records must identify the already-created target."""
    with pytest.raises(ValueError, match="target_id must not be empty"):
        TargetRecord(target_id, _numeric("entity"))


@pytest.mark.parametrize("target_id", (" target-1", "target-1 "))
def test_target_record_rejects_surrounding_whitespace(target_id: str) -> None:
    """Target identity cannot depend on invisible whitespace."""
    with pytest.raises(ValueError, match="surrounding whitespace"):
        TargetRecord(target_id, _numeric("entity"))


def test_target_record_rejects_non_string_target_id() -> None:
    """Target IDs remain opaque strings rather than platform-specific numbers."""
    with pytest.raises(TypeError, match="target_id must be a string"):
        TargetRecord(42, _numeric("entity"))


def test_existing_target_actions_require_target_id() -> None:
    """Only CREATE may defer target allocation to its adapter."""
    with pytest.raises(TypeError, match="target_id must be a string"):
        ReconciliationAction(
            kind=ReconciliationActionKind.UPDATE,
            capability=_numeric("entity"),
        )


def test_create_action_rejects_preallocated_target_id() -> None:
    """The target adapter, not the neutral planner, allocates new IDs."""
    with pytest.raises(ValueError, match="must not have a target_id"):
        ReconciliationAction(
            kind=ReconciliationActionKind.CREATE,
            target_id="target-1",
            capability=_numeric("entity"),
        )


def test_mark_unavailable_action_requires_unavailable_snapshot() -> None:
    """The dedicated action cannot accidentally carry an available value."""
    with pytest.raises(ValueError, match="require a non-available"):
        ReconciliationAction(
            kind=ReconciliationActionKind.MARK_UNAVAILABLE,
            target_id="target-1",
            capability=_numeric("entity"),
        )


def test_update_action_requires_available_snapshot() -> None:
    """Explicit non-available state uses the dedicated mark action."""
    with pytest.raises(ValueError, match="update actions require an available"):
        ReconciliationAction(
            kind=ReconciliationActionKind.UPDATE,
            target_id="target-1",
            capability=_numeric("entity", None, Availability.UNKNOWN),
        )


def test_only_mark_unavailable_action_may_be_stale() -> None:
    """Stale cannot leak into unrelated adapter operations."""
    with pytest.raises(ValueError, match="only mark-unavailable"):
        ReconciliationAction(
            kind=ReconciliationActionKind.CREATE,
            capability=_numeric("entity", None, Availability.UNAVAILABLE),
            stale=True,
        )


@pytest.mark.parametrize(
    "record",
    (
        TargetRecord,
        lambda target_id, capability, stale: ReconciliationAction(
            kind=ReconciliationActionKind.MARK_UNAVAILABLE,
            target_id=target_id,
            capability=capability,
            stale=stale,
        ),
    ),
    ids=("target-record", "action"),
)
def test_stale_requires_unavailable_capability(record: object) -> None:
    """Stale is reserved for values synthesized from a missing snapshot."""
    with pytest.raises(ValueError, match="stale records require"):
        record("target-1", _numeric("entity"), True)


@pytest.mark.parametrize("stale", (None, "yes", 1))
def test_stale_requires_bool(stale: object) -> None:
    """Persistence cannot accidentally deserialize truthy stale flags."""
    with pytest.raises(TypeError, match="stale must be a bool"):
        TargetRecord("target-1", _numeric("entity"), stale=stale)


@pytest.mark.parametrize("pending", (None, "yes", 1))
def test_pending_requires_bool(pending: object) -> None:
    """Persistence cannot accidentally deserialize truthy pending flags."""
    with pytest.raises(TypeError, match="pending must be a bool"):
        TargetRecord("target-1", _numeric("entity"), pending=pending)


def test_pending_target_record_cannot_also_be_stale() -> None:
    """An unfinished write and a confirmed missing source are distinct states."""
    capability = _numeric("entity", None, Availability.UNAVAILABLE)

    with pytest.raises(ValueError, match="pending records must not be stale"):
        TargetRecord("target-1", capability, stale=True, pending=True)


def test_reconciliation_records_are_immutable() -> None:
    """Plans remain stable while an adapter applies them."""
    action = ReconciliationAction(
        kind=ReconciliationActionKind.CREATE,
        capability=_numeric("entity"),
    )

    with pytest.raises(FrozenInstanceError):
        action.target_id = "target-1"


def test_inventory_reasserts_an_unchanged_owned_target() -> None:
    """A real Unit 1 observation triggers idempotent remote convergence."""
    capability = _numeric("same")
    target_id = derive_domoticz_target_id(capability.source)

    assert _plan(
        [capability],
        [TargetRecord(target_id, capability)],
        [TargetObservation(target_id, (1,))],
    ) == (
        ReconciliationAction(
            kind=ReconciliationActionKind.UPDATE,
            target_id=target_id,
            capability=capability,
        ),
    )


@pytest.mark.parametrize(
    ("availability", "expected_kind"),
    (
        (Availability.AVAILABLE, ReconciliationActionKind.UPDATE),
        (Availability.UNKNOWN, ReconciliationActionKind.MARK_UNAVAILABLE),
        (Availability.UNAVAILABLE, ReconciliationActionKind.MARK_UNAVAILABLE),
    ),
)
@pytest.mark.parametrize("observed_units", (None, (), (1,)))
def test_inventory_retries_current_pending_target(
    availability: Availability,
    expected_kind: ReconciliationActionKind,
    observed_units: tuple[int, ...] | None,
) -> None:
    """Durable intent repairs a missing, empty, or exact Unit 1 owned target."""
    value = 21.5 if availability is Availability.AVAILABLE else None
    capability = _numeric("pending", value, availability)
    target_id = derive_domoticz_target_id(capability.source)
    observations = (
        [] if observed_units is None else [TargetObservation(target_id, observed_units)]
    )

    actions = _plan(
        [capability],
        [TargetRecord(target_id, capability, pending=True)],
        observations,
    )

    assert actions == (
        ReconciliationAction(
            kind=expected_kind,
            target_id=target_id,
            capability=capability,
        ),
    )


def test_inventory_blocks_pending_retry_when_owned_parent_has_sibling_units() -> None:
    """Durable intent does not override ambiguous remote Unit ownership."""
    capability = _numeric("pending-compound")
    target_id = derive_domoticz_target_id(capability.source)

    assert (
        _plan(
            [capability],
            [TargetRecord(target_id, capability, pending=True)],
            [TargetObservation(target_id, (1, 2))],
        )
        == ()
    )


def test_inventory_does_not_resurrect_absent_pending_source_without_unit_one() -> None:
    """A superseded intent stays local when its source and remote Unit 1 are gone."""
    capability = _numeric("pending-removed")
    target_id = derive_domoticz_target_id(capability.source)
    record = TargetRecord(target_id, capability, pending=True)

    assert _plan([], [record], []) == ()
    assert (
        _plan(
            [],
            [record],
            [TargetObservation(target_id, ())],
        )
        == ()
    )


def test_inventory_marks_absent_pending_source_stale_when_unit_one_exists() -> None:
    """A completed remote write is retired if its source disappeared meanwhile."""
    capability = _numeric("pending-removed")
    target_id = derive_domoticz_target_id(capability.source)

    actions = _plan(
        [],
        [TargetRecord(target_id, capability, pending=True)],
        [TargetObservation(target_id, (1,))],
    )

    assert actions[0].kind is ReconciliationActionKind.MARK_UNAVAILABLE
    assert actions[0].stale


@pytest.mark.parametrize(
    "availability",
    (Availability.AVAILABLE, Availability.UNKNOWN, Availability.UNAVAILABLE),
)
def test_inventory_repairs_a_missing_current_owned_target(
    availability: Availability,
) -> None:
    """A catalog binding remains ownership proof after remote deletion."""
    value = 21.5 if availability is Availability.AVAILABLE else None
    capability = _numeric("missing-remotely", value, availability)
    target_id = derive_domoticz_target_id(capability.source)

    actions = _plan(
        [capability],
        [TargetRecord(target_id, capability)],
        [],
    )

    expected_kind = (
        ReconciliationActionKind.UPDATE
        if availability is Availability.AVAILABLE
        else ReconciliationActionKind.MARK_UNAVAILABLE
    )
    assert actions[0].kind is expected_kind
    assert actions[0].target_id == target_id
    assert not actions[0].stale


def test_inventory_does_not_claim_a_remote_only_deterministic_target() -> None:
    """An exact DeviceID without a local binding is still not ownership proof."""
    capability = _numeric("remote-only")
    target_id = derive_domoticz_target_id(capability.source)

    assert (
        _plan(
            [capability],
            [],
            [TargetObservation(target_id, (1,))],
        )
        == ()
    )
    assert (
        _plan(
            [capability],
            [],
            [TargetObservation(target_id, ())],
        )
        == ()
    )


def test_inventory_still_creates_when_no_remote_target_exists() -> None:
    """An empty authoritative inventory permits a genuinely new target."""
    capability = _numeric("new")

    assert _plan([capability], [], []) == (
        ReconciliationAction(
            kind=ReconciliationActionKind.CREATE,
            capability=capability,
        ),
    )


def test_inventory_ignores_unrelated_remote_targets() -> None:
    """Unrelated hardware-local devices neither block nor become catalog state."""
    capability = _numeric("new")

    actions = _plan(
        [capability],
        [],
        [TargetObservation("UNRELATED", (1,))],
    )

    assert actions[0].kind is ReconciliationActionKind.CREATE


def test_inventory_blocks_ambiguous_units_without_mutation() -> None:
    """Unit siblings prevent the exporter from claiming a parent container."""
    capability = _numeric("compound")
    target_id = derive_domoticz_target_id(capability.source)

    assert (
        _plan(
            [capability],
            [TargetRecord(target_id, capability)],
            [TargetObservation(target_id, (1, 2))],
        )
        == ()
    )


def test_inventory_repairs_a_current_binding_in_an_empty_parent() -> None:
    """A catalog-owned empty container may safely regain its expected Unit 1."""
    capability = _numeric("empty-parent")
    target_id = derive_domoticz_target_id(capability.source)

    actions = _plan(
        [capability],
        [TargetRecord(target_id, capability)],
        [TargetObservation(target_id, ())],
    )

    assert actions[0].kind is ReconciliationActionKind.UPDATE


def test_inventory_does_not_resurrect_a_missing_stale_target() -> None:
    """A deleted target for an unselected source remains deleted."""
    capability = _numeric("stale", None, Availability.UNAVAILABLE)
    target_id = derive_domoticz_target_id(capability.source)

    assert (
        _plan(
            [],
            [TargetRecord(target_id, capability, stale=True)],
            [],
        )
        == ()
    )

    assert (
        _plan(
            [],
            [TargetRecord(target_id, capability, stale=True)],
            [TargetObservation(target_id, ())],
        )
        == ()
    )


def test_inventory_reasserts_a_present_stale_target() -> None:
    """A retained stale target gets its runtime timeout restored."""
    capability = _numeric("stale", None, Availability.UNAVAILABLE)
    target_id = derive_domoticz_target_id(capability.source)

    actions = _plan(
        [],
        [TargetRecord(target_id, capability, stale=True)],
        [TargetObservation(target_id, (1,))],
    )

    assert actions[0].kind is ReconciliationActionKind.MARK_UNAVAILABLE
    assert actions[0].stale


def test_inventory_rejects_a_non_deterministic_catalog_binding() -> None:
    """Local persistence cannot redirect an inventory-aware repair."""
    capability = _numeric("redirected")

    with pytest.raises(TargetBindingError, match="deterministic target identity"):
        _plan(
            [capability],
            [TargetRecord("HAWRONG", capability)],
            [],
        )


def test_combined_catalog_binding_validation_rejects_duplicates() -> None:
    """Catalogs combined across capability kinds remain globally unambiguous."""
    capability = _numeric("duplicate")
    record = TargetRecord(
        derive_domoticz_target_id(capability.source),
        capability,
    )

    with pytest.raises(TargetBindingError, match="duplicate source identity"):
        validate_deterministic_target_bindings([record, record])


def test_combined_ownership_validation_rejects_a_derived_id_collision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Distinct current sources cannot plan writes to one target identity."""
    monkeypatch.setattr(
        reconciliation_module,
        "derive_domoticz_target_id",
        lambda _source: "HACOLLISION",
    )

    with pytest.raises(TargetBindingError, match="duplicate deterministic"):
        validate_deterministic_target_ownership(
            [_numeric("first"), _numeric("second")],
            [],
        )


def test_combined_ownership_reserves_a_source_for_its_catalog_kind() -> None:
    """A source cannot cross from an inactive catalog into another kind."""
    numeric = _numeric("reserved")
    target_id = derive_domoticz_target_id(numeric.source)
    numeric_record = TargetRecord(target_id, numeric)
    validate_deterministic_target_ownership([numeric], [numeric_record])

    binary = Capability(
        source=numeric.source,
        kind=CapabilityKind.BINARY,
        name="Reserved",
        value=True,
    )
    binary_record = TargetRecord(target_id, binary)

    with pytest.raises(TargetBindingError, match="kind conflicts"):
        validate_deterministic_target_ownership([numeric], [binary_record])


def _source_with_canonical_identity_size(size: int) -> SourceIdentity:
    values = {
        "system": "home_assistant",
        "instance_id": "instance-1",
        "object_id": "",
        "capability_id": "state",
    }
    fixed_size = len(
        json.dumps(
            values,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    )
    values["object_id"] = "x" * (size - fixed_size)
    return SourceIdentity(**values)


def test_domoticz_target_id_accepts_exactly_64_kib_canonical_identity() -> None:
    """The released protocol limit is inclusive at exactly 64 KiB."""
    target_id = derive_domoticz_target_id(
        _source_with_canonical_identity_size(64 * 1024)
    )

    assert target_id.startswith("HA")
    assert len(target_id) == 25


def test_domoticz_target_id_rejects_identity_over_64_kib_before_hashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Oversized provenance fails before consuming hashing resources."""
    source = _source_with_canonical_identity_size(64 * 1024 + 1)

    def unexpected_hash(_identity: bytes) -> object:
        raise AssertionError("oversized identity reached hashing")

    monkeypatch.setattr(reconciliation_module.hashlib, "sha256", unexpected_hash)

    with pytest.raises(ValueError, match="canonical size limit"):
        derive_domoticz_target_id(source)


@pytest.mark.parametrize(
    "field_name",
    ("system", "instance_id", "object_id", "capability_id"),
)
@pytest.mark.parametrize("surrogate", ("\ud800", "\udfff"))
def test_domoticz_target_id_rejects_lone_surrogate_in_every_source_component(
    field_name: str,
    surrogate: str,
) -> None:
    """Invalid Unicode cannot produce a platform-dependent DeviceID."""
    values = {
        "system": "home_assistant",
        "instance_id": "instance-1",
        "object_id": "sensor.temperature",
        "capability_id": "state",
    }
    values[field_name] += surrogate

    with pytest.raises(ValueError, match="invalid Unicode"):
        derive_domoticz_target_id(SourceIdentity(**values))


def test_inventory_rejects_duplicate_target_observations() -> None:
    """One hardware snapshot cannot describe a target parent twice."""
    with pytest.raises(TargetBindingError, match="duplicate observed"):
        _plan(
            [],
            [],
            [
                TargetObservation("HAOBSERVED", ()),
                TargetObservation("HAOBSERVED", ()),
            ],
        )


@pytest.mark.parametrize("units", ((2, 1), (1, 1), (True,), [1]))
def test_target_observation_requires_canonical_unique_units(units: object) -> None:
    """Remote unit identities are immutable, ordered, and duplicate-free."""
    with pytest.raises((TypeError, ValueError)):
        TargetObservation("HAOBSERVED", units)  # type: ignore[arg-type]
