"""Tests for ontology/registry.py loading, validation, guard enforcement, and Mermaid export."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from actions.guards import GUARD_REGISTRY
from ontology.registry import (
    ONTOLOGY_DIR,
    MissingGuardError,
    OntologyValidationError,
    load_registry,
)


def _copy_ontology(tmp_path: Path) -> Path:
    dest = tmp_path / "ontology"
    dest.mkdir()
    for name in ("objects.yaml", "links.yaml", "actions.yaml"):
        shutil.copy2(ONTOLOGY_DIR / name, dest / name)
    return dest


def test_real_ontology_loads_and_validates() -> None:
    registry = load_registry(validate=True, check_modules=True, check_guards=True)
    assert len(registry.object_types) == 15
    assert len(registry.simulated_objects()) == 14
    assert not registry.get_object("BacklogLine").simulated
    assert len(registry.link_types) == 22
    assert len(registry.action_types) == 8

    declared_guards = {
        g.id for act in registry.action_types.values() for g in act.guards
    }
    assert declared_guards <= set(GUARD_REGISTRY)


def test_broken_link_endpoint_fails_validation(tmp_path: Path) -> None:
    ont = _copy_ontology(tmp_path)
    links_path = ont / "links.yaml"
    data = yaml.safe_load(links_path.read_text(encoding="utf-8"))
    data["link_types"]["broken_link"] = {
        "from": "Box",
        "to": "NonExistentObject",
        "cardinality": "many_to_one",
        "via": "Box.material_id",
    }
    links_path.write_text(yaml.safe_dump(data), encoding="utf-8")

    with pytest.raises(OntologyValidationError, match="NonExistentObject"):
        load_registry(ont, validate=True)


def test_broken_via_property_fails_validation(tmp_path: Path) -> None:
    ont = _copy_ontology(tmp_path)
    links_path = ont / "links.yaml"
    data = yaml.safe_load(links_path.read_text(encoding="utf-8"))
    data["link_types"]["box_material"]["via"] = "Box.missing_prop"
    links_path.write_text(yaml.safe_dump(data), encoding="utf-8")

    with pytest.raises(OntologyValidationError, match="missing_prop"):
        load_registry(ont, validate=True)


def test_broken_derived_module_fails_validation(tmp_path: Path) -> None:
    ont = _copy_ontology(tmp_path)
    obj_path = ont / "objects.yaml"
    data = yaml.safe_load(obj_path.read_text(encoding="utf-8"))
    data["object_types"]["Material"]["properties"]["small_part"]["derived_by"] = "logic.no_such_module"
    obj_path.write_text(yaml.safe_dump(data), encoding="utf-8")

    with pytest.raises(OntologyValidationError, match="logic.no_such_module"):
        load_registry(ont, validate=True)


def test_unimplemented_guard_fails_validation_and_refuses_action(tmp_path: Path) -> None:
    ont = _copy_ontology(tmp_path)
    act_path = ont / "actions.yaml"
    data = yaml.safe_load(act_path.read_text(encoding="utf-8"))
    data["action_types"]["publish_count_list"]["guards"].append(
        {"id": "nonexistent_guard", "rule": "must fail"}
    )
    act_path.write_text(yaml.safe_dump(data), encoding="utf-8")

    with pytest.raises(OntologyValidationError, match="nonexistent_guard"):
        load_registry(ont, validate=True)

    unvalidated = load_registry(ont, validate=False)
    with pytest.raises(MissingGuardError, match="nonexistent_guard"):
        unvalidated.require_guards_implemented("publish_count_list")


import re


def _parse_mermaid_edges(markdown_text: str) -> set[tuple[str, str, str]]:
    """Extract (from_type, to_type, link_name) tuples from Mermaid blocks in Markdown."""
    blocks = re.findall(r"```mermaid\s*(.*?)```", markdown_text, flags=re.DOTALL)
    edges: set[tuple[str, str, str]] = set()
    edge_re = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*-->\|([^|]+)\|\s*([A-Za-z_][A-Za-z0-9_]*)\s*$")
    for block in blocks:
        for line in block.splitlines():
            m = edge_re.match(line)
            if m:
                edges.add((m.group(1), m.group(3), m.group(2).strip()))
    return edges


def test_mermaid_export_contains_all_links() -> None:
    registry = load_registry(validate=True)
    diagram = registry.to_mermaid()
    assert diagram.startswith("graph LR\n")
    expected_edges = {
        (link.from_type, link.to_type, link_name)
        for link_name, link in registry.link_types.items()
    }
    assert _parse_mermaid_edges(f"```mermaid\n{diagram}\n```") == expected_edges

    docs_ontology = Path(__file__).resolve().parent.parent / "docs" / "ONTOLOGY.md"
    if docs_ontology.exists():
        doc_edges = _parse_mermaid_edges(docs_ontology.read_text(encoding="utf-8"))
        assert doc_edges == expected_edges
