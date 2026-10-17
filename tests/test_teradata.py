import json
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from dbt_colibri.cli.cli import cli
from dbt_colibri.lineage_extractor.extractor import DbtColumnLineageExtractor
from dbt_colibri.report.generator import DbtColibriReportGenerator
from teradata_fixtures import (
    DIM_CLIENTE,
    EPH_SALDOS,
    FCT_CAMBIO,
    FCT_RESUMEN,
    SRC_CLIENTES,
    SRC_SALDOS,
    build_artifacts,
)


def _extractor(manifest=None, catalog=None):
    default_manifest, default_catalog = build_artifacts()
    manifest = manifest or default_manifest
    catalog = catalog or default_catalog
    with patch("dbt_colibri.utils.json_utils.read_json") as mock:
        mock.side_effect = [manifest, catalog]
        return DbtColumnLineageExtractor("manifest.json", "catalog.json")


@pytest.fixture(scope="module")
def lineage():
    return _extractor().extract_project_lineage()


@pytest.fixture(scope="module")
def parents(lineage):
    return lineage["lineage"]["parents"]


def _edge(node, column, lineage_type):
    return {"dbt_node": node, "column": column, "lineage_type": lineage_type}


def _assert_edges(actual, *expected):
    def key(edge):
        return (edge["dbt_node"], edge["column"], edge["lineage_type"])

    assert sorted(map(key, actual)) == sorted(map(key, expected))


# --- adapter ---------------------------------------------------------------

@pytest.mark.parametrize("adapter", ["snowflake", "postgres", "sqlserver"])
def test_rejects_other_adapters(adapter):
    manifest, catalog = build_artifacts()
    manifest["metadata"]["adapter_type"] = adapter
    with pytest.raises(ValueError, match="only supports the dbt-teradata adapter"):
        _extractor(manifest, catalog)


def test_rejects_missing_adapter():
    manifest, catalog = build_artifacts()
    del manifest["metadata"]["adapter_type"]
    with pytest.raises(ValueError, match="adapter_type not found"):
        _extractor(manifest, catalog)


# --- catalog / name resolution ----------------------------------------------

def test_schema_dict_is_two_level_and_lowercase():
    schema = _extractor().schema_dict
    # dbt-teradata catalogs have database NULL: no None key, no profile db.
    assert set(schema) == {"bdw_stg", "bdw_gold"}
    assert schema["bdw_stg"]["clientes"] == {
        "cliente_id": "INTEGER",
        "nombre": "VARCHAR(100)",
        "fecha_alta": "DATE",
        "segmento": "VARCHAR(10)",
    }


def test_relation_keys_are_database_dot_object():
    keys = _extractor()._table_to_node
    assert keys["bdw_stg.clientes"]["unique_id"] == SRC_CLIENTES
    assert keys["bdw_gold.dim_cliente"]["unique_id"] == DIM_CLIENTE
    # The profile database must never leak into the key.
    assert not any(k.startswith("dbt_user.") for k in keys)


def test_no_errors(lineage):
    assert lineage["errors"] == []


# --- column lineage --------------------------------------------------------

def test_teradata_sql_constructs(parents):
    """SEL, TRIM, CAST ... FORMAT, ZEROIFNULL and a quoted lowercase table name
    that the catalog stores in uppercase."""
    dim = parents[DIM_CLIENTE]
    _assert_edges(dim["cliente_id"], _edge(SRC_CLIENTES, "cliente_id", "pass-through"))
    _assert_edges(dim["nombre"], _edge(SRC_CLIENTES, "nombre", "transformation"))
    _assert_edges(dim["fecha_alta"], _edge(SRC_CLIENTES, "fecha_alta", "transformation"))


def test_aliased_ephemeral_cte_is_attributed_to_ephemeral(parents):
    _assert_edges(
        parents[DIM_CLIENTE]["saldo_actual"],
        _edge(EPH_SALDOS, "saldo", "transformation"),
    )


def test_ephemeral_with_qualify_traces_to_source(parents):
    eph = parents[EPH_SALDOS]
    for col in ("cliente_id", "saldo", "fecha"):
        _assert_edges(eph[col], _edge(SRC_SALDOS, col, "pass-through"))


def test_structural_filter_and_join(parents):
    dim = parents[DIM_CLIENTE]
    _assert_edges(dim["__colibri_filter__"], _edge(SRC_CLIENTES, "segmento", "filter"))
    _assert_edges(
        dim["__colibri_join__"],
        _edge(SRC_CLIENTES, "cliente_id", "join"),
        _edge(EPH_SALDOS, "cliente_id", "join"),
    )


def test_locking_row_for_access_and_star_expansion(parents):
    resumen = parents[FCT_RESUMEN]
    assert set(resumen) == {"cliente_id", "nombre", "fecha_alta", "saldo_actual"}
    for col, edges in resumen.items():
        _assert_edges(edges, _edge(DIM_CLIENTE, col, "pass-through"))


def test_table_outside_dbt_is_hardcoded_ref(parents):
    hardcoded = "_HARDCODED_REF___bdw_ref.tipo_cambio"
    _assert_edges(
        parents[FCT_CAMBIO]["saldo_usd"],
        _edge(DIM_CLIENTE, "saldo_actual", "transformation"),
        _edge(hardcoded, "cotizacion", "transformation"),
    )
    _assert_edges(parents[FCT_CAMBIO]["__colibri_filter__"], _edge(hardcoded, "moneda", "filter"))


def test_children_map(lineage):
    children = lineage["lineage"]["children"]
    assert {"column": "saldo_actual", "dbt_node": DIM_CLIENTE} in children[EPH_SALDOS]["saldo"]


# --- report ------------------------------------------------------------------

def test_report_tree_groups_by_teradata_database():
    report = DbtColibriReportGenerator(_extractor()).build_full_lineage()
    tree = report["tree"]
    db_tree = tree.get("byDatabase") or tree
    assert "teradata" in json.dumps(db_tree)
    assert "dbt_user" not in json.dumps(db_tree).lower()

    edges = {
        (e["source"], e["sourceColumn"], e["target"], e["targetColumn"])
        for e in report["lineage"]["edges"]
    }
    assert (EPH_SALDOS, "saldo", DIM_CLIENTE, "saldo_actual") in edges


def test_cli_generate_end_to_end(tmp_path):
    manifest, catalog = build_artifacts()
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    (tmp_path / "catalog.json").write_text(json.dumps(catalog))
    out = tmp_path / "dist"

    result = CliRunner().invoke(cli, [
        "generate",
        "--manifest", str(tmp_path / "manifest.json"),
        "--catalog", str(tmp_path / "catalog.json"),
        "--output-dir", str(out),
    ])

    assert result.exit_code == 0, result.output
    html = (out / "index.html").read_text()
    # Telemetry is opted out unless --enable-telemetry is passed.
    assert "<script>window.colibriTelemetry = false;</script>" in html
    data = json.loads((out / "colibri-manifest.json").read_text())
    assert data["metadata"]["adapter_type"] == "teradata"


def test_cli_enable_telemetry_flag(tmp_path):
    manifest, catalog = build_artifacts()
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    (tmp_path / "catalog.json").write_text(json.dumps(catalog))
    out = tmp_path / "dist"

    result = CliRunner().invoke(cli, [
        "generate",
        "--manifest", str(tmp_path / "manifest.json"),
        "--catalog", str(tmp_path / "catalog.json"),
        "--output-dir", str(out),
        "--enable-telemetry",
    ])

    assert result.exit_code == 0, result.output
    assert "<script>window.colibriTelemetry = false;</script>" not in (out / "index.html").read_text()


def test_report_tree_files_external_tables_under_their_database():
    report = DbtColibriReportGenerator(_extractor()).build_full_lineage()
    tree = json.dumps(report["tree"])
    assert "bdw_ref" in tree
    assert '"unknown"' not in tree


def test_demo_project_generates_clean_lineage():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).parents[1] / "docs" / "demo" / "build_demo.py"
    spec = importlib.util.spec_from_file_location("build_demo", path)
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)

    manifest, catalog = demo.build()
    result = _extractor(manifest, catalog).extract_project_lineage()
    assert result["errors"] == []
    parents = result["lineage"]["parents"]["model.gold_banco.fct_movimientos"]
    assert {(e["dbt_node"], e["column"]) for e in parents["importe_ars"]} == {
        ("source.gold_banco.stg.movimientos", "importe"),
        ("source.gold_banco.stg.movimientos", "tipo_mov_cd"),
        ("_HARDCODED_REF___bdw_ref.tipo_cambio", "cotizacion"),
    }


def test_report_hides_cloud_upsell_features(tmp_path):
    manifest, catalog = build_artifacts()
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    (tmp_path / "catalog.json").write_text(json.dumps(catalog))
    out = tmp_path / "dist"
    result = CliRunner().invoke(cli, [
        "generate",
        "--manifest", str(tmp_path / "manifest.json"),
        "--catalog", str(tmp_path / "catalog.json"),
        "--output-dir", str(out),
    ])
    assert result.exit_code == 0, result.output
    head = (out / "index.html").read_text().split("</head>")[0]
    assert 'id="colibri-teradata-hide-cloud"' in head
    for feature in ("saved_views", "git_blame"):
        assert f'utm_content={feature}' in head
