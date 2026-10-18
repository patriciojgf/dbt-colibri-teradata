"""Regression tests for reported issues, using the SQL from each report."""

from unittest.mock import patch

import pytest

from dbt_colibri.lineage_extractor.extractor import DbtColumnLineageExtractor
from dbt_colibri.utils.parsing_utils import parse_sql, strip_teradata_type_attributes
from teradata_fixtures import _catalog_entry, _model, _source

PROJECT = "gold"


def _project(models, sources):
    """models: {name: (sql, deps, [(col, type)])}; sources: {name: (schema, ident, [(col, type)])}."""
    nodes, srcs, cat_nodes, cat_sources = {}, {}, {}, {}
    for name, (schema, ident, cols) in sources.items():
        uid = f"source.{PROJECT}.work.{name}"
        srcs[uid] = _source("work", name, schema, ident, [c for c, _ in cols])
        cat_sources[uid] = _catalog_entry(uid, schema, ident, cols)
    for name, (sql, deps, cols) in models.items():
        uid = f"model.{PROJECT}.{name}"
        nodes[uid] = _model(name, "t_gold", sql, deps, [c for c, _ in cols])
        cat_nodes[uid] = _catalog_entry(uid, "t_gold", name, cols)
    parent_map = {uid: n.get("depends_on", {}).get("nodes", []) for uid, n in {**nodes, **srcs}.items()}
    child_map = {uid: [] for uid in parent_map}
    for uid, deps in parent_map.items():
        for dep in deps:
            child_map.setdefault(dep, []).append(uid)
    manifest = {
        "metadata": {"adapter_type": "teradata", "dbt_version": "1.10.22", "project_name": PROJECT},
        "nodes": nodes, "sources": srcs, "exposures": {}, "parent_map": parent_map, "child_map": child_map,
    }
    catalog = {"nodes": cat_nodes, "sources": cat_sources}
    return manifest, catalog


def _lineage(manifest, catalog):
    with patch("dbt_colibri.utils.json_utils.read_json") as mock:
        mock.side_effect = [manifest, catalog]
        extractor = DbtColumnLineageExtractor("manifest.json", "catalog.json")
    return extractor.extract_project_lineage()


def _parents(result, model, column):
    return {(p["dbt_node"].split(".")[-1], p["column"]) for p in result["lineage"]["parents"][f"model.{PROJECT}.{model}"][column]}


# --- Issue #1: UNION ALL at the root lost all column lineage ------------------

UNION_SQL = """WITH REGISTROS AS (
    SELECT KM.MONEDA_ID, MON.MONEDA_COD, MON.MONEDA_DESC
    FROM "t_gold_work"."INT_MONEDA__CATALOGO" AS MON
    INNER JOIN "t_gold_work"."D_MONEDA_KEY_MAP" AS KM
        ON MON.MONEDA_COD = KM.MONEDA_COD
),
DEFAULTS AS (
    SELECT CAST(-1 AS BIGINT) AS MONEDA_ID,
           CAST('NA' AS VARCHAR(3)) AS MONEDA_COD,
           CAST('No Aplica' AS VARCHAR(15)) AS MONEDA_DESC
    FROM (SELECT 1 AS X) AS T1
)
SELECT * FROM REGISTROS
UNION ALL
SELECT * FROM DEFAULTS"""


def _union_project():
    return _project(
        models={"d_moneda": (UNION_SQL, [f"source.{PROJECT}.work.catalogo", f"source.{PROJECT}.work.key_map"],
                             [("MONEDA_ID", "BIGINT"), ("MONEDA_COD", "VARCHAR(3)"), ("MONEDA_DESC", "VARCHAR(15)")])},
        sources={
            "catalogo": ("t_gold_work", "INT_MONEDA__CATALOGO", [("MONEDA_COD", "VARCHAR(3)"), ("MONEDA_DESC", "VARCHAR(15)")]),
            "key_map": ("t_gold_work", "D_MONEDA_KEY_MAP", [("MONEDA_ID", "BIGINT"), ("MONEDA_COD", "VARCHAR(3)")]),
        },
    )


def test_issue_1_union_all_root_keeps_column_lineage():
    result = _lineage(*_union_project())
    assert result["errors"] == []
    assert _parents(result, "d_moneda", "moneda_id") == {("key_map", "moneda_id")}
    assert _parents(result, "d_moneda", "moneda_cod") == {("catalogo", "moneda_cod")}
    assert _parents(result, "d_moneda", "moneda_desc") == {("catalogo", "moneda_desc")}


def test_issue_1_lineage_works_with_the_raw_api():
    """The reproduction from the issue, through the low-level lineage API."""
    from dbt_colibri.lineage_extractor.lineage import lineage, prepare_scope

    expression, scope = prepare_scope(parse_sql(UNION_SQL), dialect="teradata")
    node = lineage("MONEDA_ID", expression, dialect="teradata", scope=scope)
    names = {n.name for n in node.walk()}
    assert "km.moneda_id" in names


def test_column_errors_are_reported_instead_of_silently_emptying_a_model(monkeypatch):
    import dbt_colibri.lineage_extractor.extractor as extractor_module

    def broken(*args, **kwargs):
        raise AttributeError("'Scope' object has no attribute 'union_scopes'")

    monkeypatch.setattr(extractor_module, "lineage", broken)
    result = _lineage(*_union_project())
    [error] = result["errors"]
    assert error["node_id"] == f"model.{PROJECT}.d_moneda"
    assert error["error_type"] == "AttributeError"
    assert sorted(error["columns"]) == ["moneda_cod", "moneda_desc", "moneda_id"]


# --- Issue #2: CAST(... CHARACTER SET ...) made whole models unparseable ------

@pytest.mark.parametrize("cast", [
    "CAST(A.NOMBRE AS VARCHAR(50) CHARACTER SET UNICODE)",
    "CAST(A.NOMBRE AS VARCHAR(50) CHARACTER SET LATIN)",
    "CAST(A.NOMBRE AS CHAR(3) CHARACTER SET UNICODE NOT CASESPECIFIC)",
    "CAST(A.NOMBRE AS VARCHAR(50) CHARACTER SET UNICODE NOT CASESPECIFIC)",
    "CAST(A.NOMBRE AS VARCHAR(50) CASESPECIFIC)",
    "CAST(A.NOMBRE AS VARCHAR(50) NOT CS)",
    "CAST(A.NOMBRE AS VARCHAR(50) UPPERCASE)",
    "CAST(A.NOMBRE AS VARCHAR(10) FORMAT 'X(10)' CHARACTER SET LATIN)",
    "TRIM(CAST(CAST(A.NOMBRE AS VARCHAR(5) CHARACTER SET UNICODE) AS VARCHAR(9) CHARACTER SET LATIN))",
])
def test_issue_2_character_set_variants_parse(cast):
    tree = parse_sql(f"SELECT {cast} AS NOMBRE_NOM FROM DB.T AS A", dialect="teradata")
    assert tree.find_all  # parsed
    assert "CHARACTER SET" not in tree.sql(dialect="teradata").upper()


def test_issue_2_string_literals_and_aliases_are_untouched():
    sql = ("SELECT 'CHARACTER SET LATIN' AS txt, 'NOT CASESPECIFIC' AS t2, cs.x AS uppercase, "
           "CAST(cs.y AS INTEGER) AS cs FROM db.t AS cs")
    assert strip_teradata_type_attributes(sql) == sql


def test_issue_2_model_with_character_set_gets_lineage():
    sql = ('SELECT SUC.SUCURSAL_ID AS SUCURSAL_ID,\n'
           '       CAST(SUC.SUCURSAL_NOM AS VARCHAR(100) CHARACTER SET UNICODE) AS SUCURSAL_NOM,\n'
           '       CAST(SUC.ZONA_COD AS CHAR(3) CHARACTER SET LATIN NOT CASESPECIFIC) AS ZONA_COD\n'
           'FROM "t_gold_work"."SUCURSALES" AS SUC')
    manifest, catalog = _project(
        models={"d_sucursal": (sql, [f"source.{PROJECT}.work.sucursales"],
                               [("SUCURSAL_ID", "INTEGER"), ("SUCURSAL_NOM", "VARCHAR(100)"), ("ZONA_COD", "CHAR(3)")])},
        sources={"sucursales": ("t_gold_work", "SUCURSALES",
                                [("SUCURSAL_ID", "INTEGER"), ("SUCURSAL_NOM", "VARCHAR(100)"), ("ZONA_COD", "CHAR(3)")])},
    )
    result = _lineage(manifest, catalog)
    assert result["errors"] == []
    assert _parents(result, "d_sucursal", "sucursal_nom") == {("sucursales", "sucursal_nom")}
    assert _parents(result, "d_sucursal", "zona_cod") == {("sucursales", "zona_cod")}
    types = {c: e[0]["lineage_type"] for c, e in result["lineage"]["parents"][f"model.{PROJECT}.d_sucursal"].items()}
    assert types["sucursal_nom"] == "transformation"  # a CAST is still a transformation
