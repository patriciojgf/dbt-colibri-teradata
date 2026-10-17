"""Relationships between dbt nodes, for the "Relaciones" (ER diagram) view.

Two sources of relationships are combined:

* **Declared**: dbt ``relationships`` tests in the manifest.
* **Inferred**: equality conditions in ``JOIN ... ON a = b`` of the compiled SQL.

A JOIN usually relates the *inputs* of a model (a staging source and a
dimension). In a dimensional model the useful relationship is the one on the
*output*: ``fct_movimientos.cuenta_sk -> dim_cuenta.cuenta_sk``. When a model
column is a pass-through/rename of one side of a JOIN pair, the relationship is
"lifted" to that model column, pointing at the key side of the pair.

Cardinality comes only from dbt metadata: a column is a key when it has a
``unique`` test or a ``primary_key`` constraint.
"""

from __future__ import annotations

import logging
import re
from typing import Dict, Iterable, List, Optional, Tuple

from sqlglot import exp
from sqlglot.optimizer.scope import Scope

from .lineage_extractor.lineage import prepare_scope

logger = logging.getLogger("colibri")

Endpoint = Tuple[str, str]  # (dbt unique_id, lowercase column name)

_HARDCODED_PREFIX = "_HARDCODED_REF___"
_DECLARED_TESTS = {"relationships", "relationships_where"}
_LIFT_TYPES = {"pass-through", "rename"}


class RelationshipExtractor:
    def __init__(self, extractor, parents: Optional[dict] = None):
        self.extractor = extractor
        self.manifest = extractor.manifest
        self.catalog = extractor.catalog
        self.parents = parents or {}
        self._all_nodes = {**self.manifest.get("nodes", {}), **self.manifest.get("sources", {})}
        self.unique_columns = self._collect_unique_columns()
        self._rels: Dict[frozenset, dict] = {}

    # ------------------------------------------------------------------
    # dbt metadata
    # ------------------------------------------------------------------

    def _test_nodes(self) -> Iterable[dict]:
        for node in self.manifest.get("nodes", {}).values():
            if node.get("resource_type") == "test" and node.get("test_metadata"):
                yield node

    @staticmethod
    def _test_kwargs(test: dict) -> dict:
        meta = test.get("test_metadata") or {}
        kwargs = dict(meta.get("kwargs") or {})
        # dbt >= 1.10 may nest generic test arguments under "arguments".
        if isinstance(kwargs.get("arguments"), dict):
            kwargs.update(kwargs["arguments"])
        return kwargs

    def _collect_unique_columns(self) -> set:
        keys = set()
        for test in self._test_nodes():
            name = (test.get("test_metadata") or {}).get("name")
            col = test.get("column_name") or self._test_kwargs(test).get("column_name")
            node = test.get("attached_node")
            if name == "unique" and col and node:
                keys.add((node, col.strip('"').lower()))
        for node_id, node in self._all_nodes.items():
            for col_name, col in (node.get("columns") or {}).items():
                for constraint in col.get("constraints") or []:
                    if constraint.get("type") == "primary_key":
                        keys.add((node_id, col_name.lower()))
            for constraint in node.get("constraints") or []:
                if constraint.get("type") == "primary_key" and len(constraint.get("columns") or []) == 1:
                    keys.add((node_id, constraint["columns"][0].lower()))
        return keys

    def _catalog_columns(self, node_id: str) -> List[Tuple[str, str]]:
        """[(column, type)] in catalog order; ephemerals fall back to SQL-derived columns."""
        entry = self.catalog.get("nodes", {}).get(node_id) or self.catalog.get("sources", {}).get(node_id)
        if entry:
            cols = sorted(entry.get("columns", {}).values(), key=lambda c: c.get("index") or 0)
            return [(c["name"].lower(), c.get("type") or "") for c in cols]
        if node_id in self.extractor._ephemeral_registry:
            return [(c.lower(), "") for c in self.extractor._resolve_ephemeral_columns(node_id)]
        return []

    def is_key(self, ep: Endpoint) -> bool:
        return ep in self.unique_columns

    def _is_key_like(self, ep: Endpoint) -> bool:
        """Declared key, or the first column of a model (where dimensions keep their key)."""
        if self.is_key(ep):
            return True
        node = self._all_nodes.get(ep[0])
        if not node or node.get("resource_type") not in ("model", "snapshot"):
            return False
        cols = self._catalog_columns(ep[0])
        return bool(cols) and cols[0][0] == ep[1]

    # ------------------------------------------------------------------
    # Collection
    # ------------------------------------------------------------------

    def _rel(self, a: Endpoint, b: Endpoint) -> Optional[dict]:
        if a[0] == b[0]:
            return None
        key = frozenset((a, b))
        if key not in self._rels:
            self._rels[key] = {"endpoints": {a, b}, "declared": None, "tests": [], "usages": [], "derived": []}
        return self._rels[key]

    def _add_declared(self) -> None:
        for test in self._test_nodes():
            name = (test.get("test_metadata") or {}).get("name")
            if name not in _DECLARED_TESTS:
                continue
            kwargs = self._test_kwargs(test)
            src_node = test.get("attached_node")
            src_col = test.get("column_name") or kwargs.get("column_name")
            field = kwargs.get("field")
            deps = [n for n in (test.get("depends_on") or {}).get("nodes", []) if n != src_node]
            if not (src_node and src_col and field and len(deps) == 1):
                continue
            a = (src_node, src_col.strip('"').lower())
            b = (deps[0], field.strip('"').lower())
            rel = self._rel(a, b)
            if rel is not None:
                rel["declared"] = (a, b)
                rel["tests"].append(test.get("unique_id"))

    def _resolve(self, column: exp.Column, scope: Scope, depth: int = 0) -> Optional[Endpoint]:
        """Map a qualified column in *scope* to the dbt node column it reads."""
        if depth > 10:
            return None
        source = scope.sources.get(column.table)
        if isinstance(source, exp.Table):
            key = f"{source.db}.{source.name}".lower()
            match = self.extractor._table_to_node.get(key)
            node_id = match["unique_id"] if match else f"{_HARDCODED_PREFIX}{key}"
            return (node_id, column.name.lower())
        if isinstance(source, Scope):
            selected = scope.selected_sources.get(column.table, (None, None))[0]
            ref_name = (getattr(selected, "name", "") or "").lower()
            ephemeral = self.extractor._ephemeral_cte_to_node.get(ref_name)
            if ephemeral:
                return (ephemeral, column.name.lower())
            select = source.expression
            if isinstance(select, exp.Select):
                for projection in select.expressions:
                    if projection.alias_or_name.lower() == column.name.lower():
                        inner = projection.this if isinstance(projection, exp.Alias) else projection
                        if isinstance(inner, exp.Column):
                            return self._resolve(inner, source, depth + 1)
        return None

    @staticmethod
    def _join_snippet(select: exp.Select, join: exp.Join) -> str:
        from_ = select.args.get("from") or select.args.get("from_")
        head = f"FROM {from_.this.sql(dialect='teradata')}\n" if from_ is not None else ""
        side = (join.args.get("side") or "").upper()
        kind = (join.args.get("kind") or "").upper()
        keyword = " ".join(k for k in (side, kind, "JOIN") if k)
        on = join.args.get("on")
        return f"{head}{keyword} {join.this.sql(dialect='teradata')}\n  ON {on.sql(dialect='teradata')}"

    def _add_inferred(self) -> None:
        extractor = self.extractor
        for model_id, node in extractor.manifest.get("nodes", {}).items():
            if node.get("resource_type") not in ("model", "snapshot") or not node.get("compiled_code"):
                continue
            if node.get("path", "").endswith(".py"):
                continue
            try:
                schema = extractor._generate_schema_dict_from_catalog(extractor._get_parent_nodes_catalog(node))
                parsed = extractor._parse(extractor._stub_ephemeral_ctes(node["compiled_code"]))
                _, root = prepare_scope(parsed, schema=schema, dialect=extractor.dialect)
            except Exception as e:  # never break the report because of one model
                logger.debug(f"Relationships: could not parse {model_id}: {e}")
                continue

            for scope in root.traverse():
                select = scope.expression
                if not isinstance(select, exp.Select):
                    continue
                for join in select.args.get("joins") or []:
                    on = join.args.get("on")
                    if on is None:
                        continue
                    snippet = None
                    for eq in on.find_all(exp.EQ):
                        if not (isinstance(eq.left, exp.Column) and isinstance(eq.right, exp.Column)):
                            continue
                        a, b = self._resolve(eq.left, scope), self._resolve(eq.right, scope)
                        if not a or not b:
                            continue
                        snippet = snippet or self._join_snippet(select, join)
                        usage = {"model": model_id, "sql": snippet, "condition": eq.sql(dialect="teradata")}
                        rel = self._rel(a, b)
                        if rel is not None and usage not in rel["usages"]:
                            rel["usages"].append(usage)
                        self._lift(model_id, a, b, usage)

    def _lift(self, model_id: str, a: Endpoint, b: Endpoint, usage: dict) -> None:
        """Relate model columns that carry one side of a JOIN pair to the key side."""
        key_side = [ep for ep in (a, b) if self._is_key_like(ep)]
        if len(key_side) != 1:
            return
        key = key_side[0]
        for column, parents in (self.parents.get(model_id) or {}).items():
            if column.startswith("__colibri_") or len(parents) != 1:
                continue
            parent = parents[0]
            if parent.get("lineage_type") not in _LIFT_TYPES:
                continue
            carried = (parent["dbt_node"], parent["column"].lower())
            if carried not in (a, b):
                continue
            rel = self._rel((model_id, column.lower()), key)
            if rel is None:
                continue
            derived = {"model": model_id, "column": column.lower(), "via": usage["condition"], "sql": usage["sql"]}
            if derived not in rel["derived"]:
                rel["derived"].append(derived)

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    def _ref(self, node_id: str) -> str:
        node = self._all_nodes.get(node_id) or {}
        if node.get("resource_type") == "source":
            return f"source('{node.get('source_name')}', '{node.get('name')}')"
        return f"ref('{node.get('name') or node_id.split('.')[-1]}')"

    def _dbt_version(self) -> Tuple[int, int, int]:
        version = self.manifest.get("metadata", {}).get("dbt_version") or ""
        m = re.match(r"(\d+)\.(\d+)(?:\.(\d+))?", version)
        return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)) if m else (1, 8, 0)

    def _yaml_path(self, node_id: str) -> str:
        node = self._all_nodes.get(node_id) or {}
        patch = node.get("patch_path") or ""
        if "://" in patch:
            return patch.split("://", 1)[1]
        if node.get("resource_type") == "source":
            return node.get("original_file_path") or "models/sources.yml"
        path = node.get("original_file_path") or f"models/{node_id.split('.')[-1]}.sql"
        return re.sub(r"\.sql$", ".yml", path)

    def _yaml_column_tests(self, column: str, tests: List[str], indent: int) -> str:
        """YAML for one column with its tests, as a list item at *indent* spaces."""
        pad = " " * indent
        key = "data_tests" if self._dbt_version() >= (1, 8, 0) else "tests"
        return f"{pad}- name: {column}\n{pad}  {key}:\n" + "".join(tests)

    def _yaml_relationships_test(self, to: Endpoint, indent: int) -> str:
        pad = " " * indent
        args = [f"to: {self._ref(to[0])}", f"field: {to[1]}"]
        if self._dbt_version() >= (1, 10, 5):
            # dbt >= 1.10.5 expects generic test inputs under "arguments".
            body = f"{pad}      arguments:\n" + "".join(f"{pad}        {a}\n" for a in args)
        else:
            body = "".join(f"{pad}      {a}\n" for a in args)
        return f"{pad}  - relationships:\n{body}"

    def _yaml_for_node(self, node_id: str, column_yaml: str) -> str:
        """Wrap column YAML in the models:/sources: structure of *node_id*."""
        node = self._all_nodes.get(node_id) or {}
        if node.get("resource_type") == "source":
            return (f"sources:\n  - name: {node.get('source_name')}\n    tables:\n"
                    f"      - name: {node.get('name')}\n        columns:\n{column_yaml}")
        key = "snapshots" if node.get("resource_type") == "snapshot" else "models"
        return f"{key}:\n  - name: {node.get('name')}\n    columns:\n{column_yaml}"

    def _suggestion(self, frm: Endpoint, to: Endpoint) -> Optional[dict]:
        node = self._all_nodes.get(frm[0])
        if not node or node.get("resource_type") not in ("model", "snapshot", "source"):
            return None
        if to[0].startswith(_HARDCODED_PREFIX) or to[0] not in self._all_nodes:
            return None
        indent = 10 if node.get("resource_type") == "source" else 6
        col_yaml = self._yaml_column_tests(frm[1], [self._yaml_relationships_test(to, indent + 2)], indent)
        suggestion = {"path": self._yaml_path(frm[0]), "yaml": self._yaml_for_node(frm[0], col_yaml)}
        if not self.is_key(to):
            to_node = self._all_nodes[to[0]]
            to_indent = 10 if to_node.get("resource_type") == "source" else 6
            unique = " " * (to_indent + 2) + "  - unique\n"
            suggestion["unique"] = {
                "path": self._yaml_path(to[0]),
                "yaml": self._yaml_for_node(to[0], self._yaml_column_tests(to[1], [unique], to_indent)),
            }
        return suggestion

    def _orient(self, rel: dict) -> Tuple[Endpoint, Endpoint, str]:
        """Return (from, to, direction_source) with ``to`` on the key side when known."""
        if rel["declared"]:
            return rel["declared"][0], rel["declared"][1], "declared"
        a, b = sorted(rel["endpoints"])
        if self.is_key(a) != self.is_key(b):
            return (b, a, "unique") if self.is_key(a) else (a, b, "unique")
        if rel["derived"]:
            lifted = {(d["model"], d["column"]) for d in rel["derived"]}
            if a in lifted and b not in lifted:
                return a, b, "lineage"
            if b in lifted and a not in lifted:
                return b, a, "lineage"
        if self._is_key_like(a) != self._is_key_like(b):
            return (b, a, "heuristic") if self._is_key_like(a) else (a, b, "heuristic")
        return a, b, "unknown"

    def _node_info(self, node_id: str) -> dict:
        node = self._all_nodes.get(node_id) or {}
        if node_id.startswith(_HARDCODED_PREFIX):
            relation = node_id[len(_HARDCODED_PREFIX):]
            schema, _, name = relation.rpartition(".")
            return {"id": node_id, "name": name, "schema": schema, "kind": "external",
                    "materialized": None, "columns": [], "description": None}
        kind = node.get("resource_type") or "model"
        materialized = (node.get("config") or {}).get("materialized")
        if kind == "model" and materialized == "ephemeral":
            kind = "ephemeral"
        return {
            "id": node_id,
            "name": node.get("alias") or node.get("identifier") or node.get("name") or node_id.split(".")[-1],
            "schema": node.get("schema"),
            "kind": kind,
            "materialized": materialized,
            "description": node.get("description") or None,
            "columns": [
                {"name": c, "type": t, "key": (node_id, c) in self.unique_columns}
                for c, t in self._catalog_columns(node_id)
            ],
        }

    def extract(self) -> dict:
        self._add_declared()
        self._add_inferred()

        relationships, node_ids = [], set()
        for rel in self._rels.values():
            frm, to, direction = self._orient(rel)
            from_key, to_key = self.is_key(frm), self.is_key(to)
            cardinality = "1:1" if (from_key and to_key) else "N:1" if to_key else None
            node_ids.update((frm[0], to[0]))
            relationships.append({
                "id": f"{frm[0]}.{frm[1]}->{to[0]}.{to[1]}",
                "from": {"node": frm[0], "column": frm[1]},
                "to": {"node": to[0], "column": to[1]},
                "declared": bool(rel["declared"]),
                "inferred": bool(rel["usages"] or rel["derived"]),
                "tests": rel["tests"],
                "cardinality": cardinality,
                "direction": direction,
                "usages": rel["usages"],
                "derived": rel["derived"],
                "suggestion": None if rel["declared"] or direction == "unknown" else self._suggestion(frm, to),
            })
        relationships.sort(key=lambda r: (not r["declared"], r["id"]))
        nodes = {nid: self._node_info(nid) for nid in sorted(node_ids)}
        return {"nodes": nodes, "relationships": relationships}


def extract_relationships(extractor, parents: Optional[dict] = None) -> dict:
    return RelationshipExtractor(extractor, parents).extract()
