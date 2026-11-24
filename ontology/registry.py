"""Ontology registry: load, validate, materialize, and export Mermaid diagrams."""

from __future__ import annotations

import importlib.util
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ONTOLOGY_DIR = Path(__file__).resolve().parent
VALID_PROPERTY_TYPES = frozenset(
    {"string", "integer", "number", "boolean", "date", "datetime", "enum"}
)
VALID_CARDINALITIES = frozenset({"many_to_one", "one_to_one", "one_to_many"})
VALID_ON_GUARD_FAIL = frozenset(
    {"hold_and_alert_owner", "hand_to_person", "send_with_flags"}
)
VALID_APPROVALS = frozenset({"none", "owner", "reviewed_change"})


class OntologyValidationError(ValueError):
    """Raised when ontology YAML definitions fail structural or code-link checks."""


class MissingGuardError(RuntimeError):
    """Raised when an action declares a guard id not implemented in actions.guards."""


@dataclass(frozen=True)
class PropertySpec:
    name: str
    type: str
    description: str = ""
    values: tuple[str, ...] = ()
    derived_by: str | None = None
    set_by: str | None = None


@dataclass(frozen=True)
class ObjectTypeSpec:
    name: str
    description: str
    key: tuple[str, ...]
    maintained_by: str
    backed_by: str
    simulated: bool
    properties: dict[str, PropertySpec] = field(default_factory=dict)

    @property
    def ledger_table(self) -> str:
        prefix = "ledger."
        if not self.backed_by.startswith(prefix):
            return self.backed_by
        return self.backed_by[len(prefix) :]


@dataclass(frozen=True)
class LinkTypeSpec:
    name: str
    from_type: str
    to_type: str
    cardinality: str
    via: tuple[str, ...]
    time_varying: bool = False
    set_by: str | None = None
    description: str = ""


@dataclass(frozen=True)
class GuardSpec:
    id: str
    rule: str


@dataclass(frozen=True)
class ActionTypeSpec:
    name: str
    description: str
    performed_by: str
    reads: tuple[str, ...]
    creates: tuple[str, ...]
    updates: tuple[str, ...]
    guards: tuple[GuardSpec, ...]
    on_guard_fail: str
    approval: str
    side_effects: tuple[str, ...]


class OntologyRegistry:
    """In-memory representation of objects, links, and actions from ontology/*.yaml."""

    def __init__(
        self,
        object_types: dict[str, ObjectTypeSpec],
        link_types: dict[str, LinkTypeSpec],
        action_types: dict[str, ActionTypeSpec],
    ) -> None:
        self.object_types = object_types
        self.link_types = link_types
        self.action_types = action_types

    def get_object(self, name: str) -> ObjectTypeSpec:
        if name not in self.object_types:
            raise KeyError(f"Unknown object type: {name}")
        return self.object_types[name]

    def get_action(self, name: str) -> ActionTypeSpec:
        if name not in self.action_types:
            raise KeyError(f"Unknown action type: {name}")
        return self.action_types[name]

    def simulated_objects(self) -> list[ObjectTypeSpec]:
        return [spec for spec in self.object_types.values() if spec.simulated]

    def require_guards_implemented(
        self,
        action_name: str,
        guard_registry: dict[str, Any] | None = None,
    ) -> list[Any]:
        """Return guard callables for an action, refusing if any guard id is missing."""
        spec = self.get_action(action_name)
        if guard_registry is None:
            from actions.guards import GUARD_REGISTRY

            guard_registry = GUARD_REGISTRY

        missing = [g.id for g in spec.guards if g.id not in guard_registry]
        if missing:
            raise MissingGuardError(
                f"Action '{action_name}' cannot run: missing guard implementation(s): {missing}"
            )
        return [guard_registry[g.id] for g in spec.guards]

    def validate(
        self,
        *,
        check_modules: bool = True,
        check_guards: bool = True,
        guard_registry: dict[str, Any] | None = None,
    ) -> None:
        """Validate internal consistency, module references, and guard implementations."""
        errors: list[str] = []

        for obj_name, obj in self.object_types.items():
            if not obj.key:
                errors.append(f"Object '{obj_name}' must declare at least one key property")
            if not obj.backed_by.startswith("ledger."):
                errors.append(
                    f"Object '{obj_name}' backed_by '{obj.backed_by}' must start with 'ledger.'"
                )
            for key_prop in obj.key:
                if key_prop not in obj.properties:
                    errors.append(
                        f"Object '{obj_name}' key property '{key_prop}' not found in properties"
                    )
            for prop_name, prop in obj.properties.items():
                if prop.type not in VALID_PROPERTY_TYPES:
                    errors.append(
                        f"Object '{obj_name}.{prop_name}' has unknown type '{prop.type}'"
                    )
                if prop.type == "enum" and not prop.values:
                    errors.append(
                        f"Object '{obj_name}.{prop_name}' is enum without values"
                    )
                if check_modules:
                    for field_label, mod_name in (
                        ("derived_by", prop.derived_by),
                        ("set_by", prop.set_by),
                    ):
                        if mod_name and importlib.util.find_spec(mod_name) is None:
                            errors.append(
                                f"Object '{obj_name}.{prop_name}' {field_label} "
                                f"module '{mod_name}' does not exist"
                            )

        for link_name, link in self.link_types.items():
            if link.from_type not in self.object_types:
                errors.append(
                    f"Link '{link_name}' from unknown object type '{link.from_type}'"
                )
            if link.to_type not in self.object_types:
                errors.append(
                    f"Link '{link_name}' to unknown object type '{link.to_type}'"
                )
            if link.cardinality not in VALID_CARDINALITIES:
                errors.append(
                    f"Link '{link_name}' has invalid cardinality '{link.cardinality}'"
                )
            if not link.via:
                errors.append(f"Link '{link_name}' must specify 'via'")
            elif link.from_type in self.object_types:
                from_spec = self.object_types[link.from_type]
                for via_ref in link.via:
                    if "." not in via_ref:
                        errors.append(
                            f"Link '{link_name}' via '{via_ref}' must be 'Type.property'"
                        )
                        continue
                    ref_type, ref_prop = via_ref.split(".", 1)
                    if ref_type != link.from_type:
                        errors.append(
                            f"Link '{link_name}' via '{via_ref}' must reference from type '{link.from_type}'"
                        )
                    elif ref_prop not in from_spec.properties:
                        errors.append(
                            f"Link '{link_name}' via property '{via_ref}' does not exist"
                        )
            if check_modules and link.set_by:
                if importlib.util.find_spec(link.set_by) is None:
                    errors.append(
                        f"Link '{link_name}' set_by module '{link.set_by}' does not exist"
                    )

        if check_guards and guard_registry is None:
            from actions.guards import GUARD_REGISTRY

            guard_registry = GUARD_REGISTRY

        for act_name, act in self.action_types.items():
            if act.on_guard_fail not in VALID_ON_GUARD_FAIL:
                errors.append(
                    f"Action '{act_name}' has invalid on_guard_fail '{act.on_guard_fail}'"
                )
            if act.approval not in VALID_APPROVALS:
                errors.append(
                    f"Action '{act_name}' has invalid approval '{act.approval}'"
                )
            for bucket, refs in (
                ("reads", act.reads),
                ("creates", act.creates),
                ("updates", act.updates),
            ):
                for ref in refs:
                    if "." in ref:
                        ref_type, ref_prop = ref.split(".", 1)
                        if ref_type not in self.object_types:
                            errors.append(
                                f"Action '{act_name}' {bucket} unknown object type '{ref_type}'"
                            )
                        elif ref_prop not in self.object_types[ref_type].properties:
                            errors.append(
                                f"Action '{act_name}' {bucket} unknown property '{ref}'"
                            )
                    else:
                        if ref not in self.object_types:
                            errors.append(
                                f"Action '{act_name}' {bucket} unknown object type '{ref}'"
                            )
            if check_guards:
                assert guard_registry is not None
                for guard in act.guards:
                    if guard.id not in guard_registry:
                        errors.append(
                            f"Action '{act_name}' references unimplemented guard '{guard.id}'"
                        )

        if errors:
            bullet_list = "\n  - ".join(errors)
            raise OntologyValidationError(f"Ontology validation failed:\n  - {bullet_list}")

    def materialize(self, ledger_conn: sqlite3.Connection) -> dict[str, int]:
        """Build one TEMP SQLite view per simulated object type over the latest snapshot rows.

        Using TEMP views works on both read-write and strict ?mode=ro ledger connections.
        Returns a mapping of object type name -> materialized row count.
        """
        counts: dict[str, int] = {}
        for spec in self.simulated_objects():
            table = spec.ledger_table
            props = list(spec.properties.keys())
            key_cols = ", ".join(f'"{k}"' for k in spec.key)
            select_cols = ", ".join(f'"{p}"' for p in props)
            view_name = spec.name

            ledger_conn.execute(f'DROP VIEW IF EXISTS temp."{view_name}"')
            ledger_conn.execute(
                f"""
                CREATE TEMP VIEW "{view_name}" AS
                SELECT {select_cols}
                FROM (
                    SELECT {select_cols},
                           ROW_NUMBER() OVER (
                               PARTITION BY {key_cols}
                               ORDER BY load_seq DESC
                           ) AS _rn
                    FROM "{table}"
                )
                WHERE _rn = 1
                """
            )
            row_count = ledger_conn.execute(
                f'SELECT COUNT(*) FROM temp."{view_name}"'
            ).fetchone()[0]
            counts[view_name] = int(row_count)
        return counts

    def to_mermaid(self) -> str:
        """Export the ontology links graph in Mermaid graph LR syntax."""
        lines = ["graph LR"]
        for link in self.link_types.values():
            lines.append(f"  {link.from_type} -->|{link.name}| {link.to_type}")
        return "\n".join(lines)


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise OntologyValidationError(f"Expected mapping in {path}")
    return data


def _as_tuple(val: Any) -> tuple[str, ...]:
    if val is None:
        return ()
    if isinstance(val, str):
        return (val,)
    return tuple(str(item) for item in val)


def load_registry(
    ontology_dir: Path | str | None = None,
    *,
    validate: bool = True,
    check_modules: bool = True,
    check_guards: bool = True,
    guard_registry: dict[str, Any] | None = None,
) -> OntologyRegistry:
    """Load objects.yaml, links.yaml, and actions.yaml into an OntologyRegistry."""
    root = Path(ontology_dir) if ontology_dir is not None else ONTOLOGY_DIR
    objects_raw = _load_yaml(root / "objects.yaml")
    links_raw = _load_yaml(root / "links.yaml")
    actions_raw = _load_yaml(root / "actions.yaml")

    object_types: dict[str, ObjectTypeSpec] = {}
    for name, body in (objects_raw.get("object_types") or {}).items():
        props: dict[str, PropertySpec] = {}
        for prop_name, prop_body in (body.get("properties") or {}).items():
            props[prop_name] = PropertySpec(
                name=prop_name,
                type=str(prop_body.get("type", "")),
                description=str(prop_body.get("description", "")),
                values=tuple(str(v) for v in (prop_body.get("values") or ())),
                derived_by=prop_body.get("derived_by"),
                set_by=prop_body.get("set_by"),
            )
        object_types[name] = ObjectTypeSpec(
            name=name,
            description=str(body.get("description", "")),
            key=tuple(str(k) for k in (body.get("key") or ())),
            maintained_by=str(body.get("maintained_by", "")),
            backed_by=str(body.get("backed_by", "")),
            simulated=bool(body.get("simulated", True)),
            properties=props,
        )

    link_types: dict[str, LinkTypeSpec] = {}
    for name, body in (links_raw.get("link_types") or {}).items():
        link_types[name] = LinkTypeSpec(
            name=name,
            from_type=str(body.get("from", "")),
            to_type=str(body.get("to", "")),
            cardinality=str(body.get("cardinality", "")),
            via=_as_tuple(body.get("via")),
            time_varying=bool(body.get("time_varying", False)),
            set_by=body.get("set_by"),
            description=str(body.get("description", "")),
        )

    action_types: dict[str, ActionTypeSpec] = {}
    for name, body in (actions_raw.get("action_types") or {}).items():
        guards = tuple(
            GuardSpec(id=str(g["id"]), rule=str(g.get("rule", "")))
            for g in (body.get("guards") or ())
        )
        action_types[name] = ActionTypeSpec(
            name=name,
            description=str(body.get("description", "")),
            performed_by=str(body.get("performed_by", "")),
            reads=_as_tuple(body.get("reads")),
            creates=_as_tuple(body.get("creates")),
            updates=_as_tuple(body.get("updates")),
            guards=guards,
            on_guard_fail=str(body.get("on_guard_fail", "")),
            approval=str(body.get("approval", "")),
            side_effects=_as_tuple(body.get("side_effects")),
        )

    registry = OntologyRegistry(
        object_types=object_types,
        link_types=link_types,
        action_types=action_types,
    )
    if validate:
        registry.validate(
            check_modules=check_modules,
            check_guards=check_guards,
            guard_registry=guard_registry,
        )
    return registry
