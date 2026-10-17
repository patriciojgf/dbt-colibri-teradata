import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner

from dbt_colibri.cli.cli import cli
from dbt_colibri.lineage_extractor.extractor import DbtColumnLineageExtractor
from dbt_colibri.relationships import extract_relationships

ROOT = Path(__file__).parents[1]
P = "model.gold_banco."
SRC = "source.gold_banco.stg."
TIPO_CAMBIO = "_HARDCODED_REF___bdw_ref.tipo_cambio"


def _demo():
    spec = importlib.util.spec_from_file_location("build_demo", ROOT / "docs" / "demo" / "build_demo.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build()


def _extract(manifest=None, catalog=None):
    if manifest is None:
        manifest, catalog = _demo()
    with patch("dbt_colibri.utils.json_utils.read_json") as mock:
        mock.side_effect = [manifest, catalog]
        extractor = DbtColumnLineageExtractor("manifest.json", "catalog.json")
    parents = extractor.extract_project_lineage()["lineage"]["parents"]
    return extract_relationships(extractor, parents)


@pytest.fixture(scope="module")
def result():
    return _extract()


def _rel(result, frm, to):
    def key(e):
        return (e["node"], e["column"])
    for r in result["relationships"]:
        if {key(r["from"]), key(r["to"])} == {frm, to}:
            return r
    raise AssertionError(f"no relationship {frm} <-> {to}")


def test_finds_every_relationship_in_the_demo(result):
    pairs = {
        frozenset([(r["from"]["node"], r["from"]["column"]), (r["to"]["node"], r["to"]["column"])])
        for r in result["relationships"]
    }
    assert pairs == {
        frozenset([(P + "fct_movimientos", "cuenta_sk"), (P + "dim_cuenta", "cuenta_sk")]),
        frozenset([(P + "fct_movimientos", "cliente_sk"), (P + "dim_cliente", "cliente_sk")]),
        frozenset([(P + "agg_saldo_mensual_cliente", "cliente_sk"), (P + "dim_cliente", "cliente_sk")]),
        frozenset([(P + "dim_cliente", "sucursal_sk"), (P + "dim_sucursal", "sucursal_sk")]),
        frozenset([(SRC + "movimientos", "cuenta_id"), (P + "dim_cuenta", "cuenta_sk")]),
        frozenset([(SRC + "clientes", "sucursal_id"), (P + "dim_sucursal", "sucursal_sk")]),
        frozenset([(TIPO_CAMBIO, "moneda_cd"), (P + "dim_cuenta", "moneda")]),
        frozenset([(TIPO_CAMBIO, "fecha"), (SRC + "movimientos", "fecha_proceso")]),
    }


def test_declared_relationship_from_dbt_test(result):
    r = _rel(result, (P + "fct_movimientos", "cuenta_sk"), (P + "dim_cuenta", "cuenta_sk"))
    assert r["declared"] is True
    assert r["direction"] == "declared"
    assert r["from"] == {"node": P + "fct_movimientos", "column": "cuenta_sk"}
    assert r["cardinality"] == "N:1"
    assert r["suggestion"] is None
    assert r["tests"] == ["test.gold_banco.relationships_fct_movimientos_cuenta_sk__cuenta_sk__ref_dim_cuenta_"]
    # Also confirmed by the SQL: fct_movimientos.cuenta_sk is carried from the join.
    assert r["inferred"] is True and r["derived"]


def test_inferred_from_join_with_usage_sql(result):
    r = _rel(result, (P + "fct_movimientos", "cliente_sk"), (P + "dim_cliente", "cliente_sk"))
    assert r["declared"] is False
    assert r["from"]["node"] == P + "fct_movimientos"  # unique test on dim_cliente.cliente_sk
    assert r["cardinality"] == "N:1"
    [usage] = r["usages"]
    assert usage["model"] == P + "agg_saldo_mensual_cliente"
    assert usage["condition"] == "f.cliente_sk = d.cliente_sk"
    assert "JOIN bdw_gold.dim_cliente AS d" in usage["sql"]


def test_lifted_to_model_column_through_lineage(result):
    """clientes.sucursal_id = dim_sucursal.sucursal_sk in dim_cliente, and
    dim_cliente.sucursal_sk is a rename of clientes.sucursal_id."""
    r = _rel(result, (P + "dim_cliente", "sucursal_sk"), (P + "dim_sucursal", "sucursal_sk"))
    assert r["from"]["node"] == P + "dim_cliente"
    assert r["usages"] == []
    [derived] = r["derived"]
    assert derived["model"] == P + "dim_cliente"
    assert derived["via"] == "c.sucursal_id = s.sucursal_sk"


def test_join_through_ephemeral_is_resolved_to_the_ephemeral_model():
    manifest, catalog = _demo()
    node = manifest["nodes"][P + "dim_cuenta"]
    node["compiled_code"] = node["compiled_code"].replace(
        "FROM __dbt__cte__stg_cuentas_activas ca",
        'FROM __dbt__cte__stg_cuentas_activas ca\nJOIN "BDW_GOLD"."dim_cliente" dc ON ca.cliente_id = dc.cliente_sk',
    )
    result = _extract(manifest, catalog)
    r = _rel(result, (P + "stg_cuentas_activas", "cliente_id"), (P + "dim_cliente", "cliente_sk"))
    assert r["to"]["node"] == P + "dim_cliente"
    # dim_cuenta.cliente_sk is carried from the ephemeral side, so it is lifted too.
    _rel(result, (P + "dim_cuenta", "cliente_sk"), (P + "dim_cliente", "cliente_sk"))


def test_unknown_direction_without_keys(result):
    r = _rel(result, (TIPO_CAMBIO, "moneda_cd"), (P + "dim_cuenta", "moneda"))
    assert r["direction"] == "unknown"
    assert r["cardinality"] is None
    assert r["suggestion"] is None


def test_without_unique_tests_cardinality_is_unknown_but_direction_is_guessed():
    manifest, catalog = _demo()
    manifest["nodes"] = {k: v for k, v in manifest["nodes"].items()
                         if (v.get("test_metadata") or {}).get("name") != "unique"}
    result = _extract(manifest, catalog)
    r = _rel(result, (P + "fct_movimientos", "cliente_sk"), (P + "dim_cliente", "cliente_sk"))
    assert r["cardinality"] is None
    assert r["to"]["node"] == P + "dim_cliente"  # first column of a model looks like its key
    assert r["direction"] == "heuristic"
    assert "unique" in r["suggestion"]  # also suggests the missing unique test


@pytest.mark.parametrize("version,tests_key,nested", [
    ("1.10.8", "data_tests", True),
    ("1.10.4", "data_tests", False),
    ("1.7.0", "tests", False),
])
def test_suggested_yaml_is_valid_dbt_yaml(version, tests_key, nested):
    manifest, catalog = _demo()
    manifest["metadata"]["dbt_version"] = version
    r = _rel(_extract(manifest, catalog), (P + "fct_movimientos", "cliente_sk"), (P + "dim_cliente", "cliente_sk"))
    assert r["suggestion"]["path"] == "models/marts/_marts.yml"
    doc = yaml.safe_load(r["suggestion"]["yaml"])
    column = doc["models"][0]["columns"][0]
    assert doc["models"][0]["name"] == "fct_movimientos" and column["name"] == "cliente_sk"
    test = column[tests_key][0]["relationships"]
    args = test["arguments"] if nested else test
    assert args == {"to": "ref('dim_cliente')", "field": "cliente_sk"}


def test_suggested_yaml_for_a_source_column(result):
    r = _rel(result, (SRC + "clientes", "sucursal_id"), (P + "dim_sucursal", "sucursal_sk"))
    doc = yaml.safe_load(r["suggestion"]["yaml"])
    table = doc["sources"][0]["tables"][0]
    assert doc["sources"][0]["name"] == "stg" and table["name"] == "clientes"
    assert table["columns"][0]["name"] == "sucursal_id"


def test_node_metadata_for_the_diagram(result):
    nodes = result["nodes"]
    assert nodes[TIPO_CAMBIO]["kind"] == "external" and nodes[TIPO_CAMBIO]["schema"] == "bdw_ref"
    assert nodes[SRC + "clientes"]["kind"] == "source"
    dim = nodes[P + "dim_cliente"]
    assert dim["kind"] == "model" and dim["schema"] == "BDW_GOLD"
    assert {"name": "cliente_sk", "type": "INTEGER", "key": True} in dim["columns"]


def test_project_without_joins_or_tests_has_no_relationships():
    from teradata_fixtures import build_artifacts
    manifest, catalog = build_artifacts()
    for node in manifest["nodes"].values():
        node["compiled_code"] = node["compiled_code"].split("LEFT JOIN")[0].split("CROSS JOIN")[0]
    assert _extract(manifest, catalog)["relationships"] == []


def test_report_writes_relationships_page_tab_and_json(tmp_path):
    manifest, catalog = _demo()
    # Hostile names must stay inert inside the embedded JSON.
    manifest["nodes"][P + "dim_cliente"]["description"] = "</script><script>alert(1)</script>"
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    (tmp_path / "catalog.json").write_text(json.dumps(catalog))
    out = tmp_path / "dist"
    res = CliRunner().invoke(cli, ["generate", "--manifest", str(tmp_path / "manifest.json"),
                                   "--catalog", str(tmp_path / "catalog.json"), "--output-dir", str(out)])
    assert res.exit_code == 0, res.output

    page = (out / "relaciones.html").read_text()
    assert "/*__COLIBRI_RELATIONSHIPS__*/" not in page
    assert "</script><script>alert(1)" not in page
    raw = page.split('<script id="colibri-relationships-data" type="application/json">')[1].split("</script>")[0]
    data = json.loads(raw)
    assert data["project"] == "gold_banco"
    assert len(data["relationships"]) == 8
    assert data["nodes"][P + "dim_cliente"]["description"] == "</script><script>alert(1)</script>"

    index = (out / "index.html").read_text()
    assert 'id="colibri-teradata-relationships-tab"' in index.split("</head>")[0]

    manifest_out = json.loads((out / "colibri-manifest.json").read_text())
    assert len(manifest_out["relationships"]) == 8


def test_relationship_failure_does_not_break_the_report(tmp_path, monkeypatch):
    import dbt_colibri.report.generator as generator

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(generator, "extract_relationships", boom)
    manifest, catalog = _demo()
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    (tmp_path / "catalog.json").write_text(json.dumps(catalog))
    res = CliRunner().invoke(cli, ["generate", "--manifest", str(tmp_path / "manifest.json"),
                                   "--catalog", str(tmp_path / "catalog.json"),
                                   "--output-dir", str(tmp_path / "dist")])
    assert res.exit_code == 0, res.output
    assert (tmp_path / "dist" / "index.html").exists()
    assert (tmp_path / "dist" / "relaciones.html").exists()


def test_columns_read_from_an_ephemeral_keep_their_real_lineage_type():
    manifest, catalog = _demo()
    with patch("dbt_colibri.utils.json_utils.read_json") as mock:
        mock.side_effect = [manifest, catalog]
        extractor = DbtColumnLineageExtractor("manifest.json", "catalog.json")
    parents = extractor.extract_project_lineage()["lineage"]["parents"][P + "dim_cuenta"]
    types = {col: edges[0]["lineage_type"] for col, edges in parents.items() if edges}
    assert types == {
        "cuenta_sk": "rename",
        "cliente_sk": "rename",
        "producto": "transformation",
        "moneda": "rename",
        "fecha_apertura": "pass-through",
    }
