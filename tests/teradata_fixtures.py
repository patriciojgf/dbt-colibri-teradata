"""Synthetic dbt-teradata artifacts shaped like the real adapter output.

Mirrors what dbt-teradata writes:
  - ``metadata.adapter_type`` is ``teradata``.
  - ``relation_name`` is two-part and quoted: ``"BDW_STG"."CLIENTES"``.
  - ``node["database"]`` holds the profile database, which is NOT part of the
    object name (dbt-teradata's include_policy drops it).
  - catalog ``metadata.database`` is NULL; the Teradata database is in
    ``metadata.schema`` and names keep the case they were created with.
"""

from __future__ import annotations

import copy

PROJECT = "banco"
PROFILE_DB = "DBT_USER"

SRC_CLIENTES = f"source.{PROJECT}.stg.clientes"
SRC_SALDOS = f"source.{PROJECT}.stg.saldos"
EPH_SALDOS = f"model.{PROJECT}.stg_saldos_vigentes"
DIM_CLIENTE = f"model.{PROJECT}.dim_cliente"
FCT_RESUMEN = f"model.{PROJECT}.fct_resumen"
FCT_CAMBIO = f"model.{PROJECT}.fct_cambio"

EPH_SQL = (
    'SELECT Cliente_Id, Saldo, Fecha\n'
    'FROM "BDW_STG"."SALDOS"\n'
    'QUALIFY ROW_NUMBER() OVER (PARTITION BY Cliente_Id ORDER BY Fecha DESC) = 1'
)

DIM_SQL = (
    "WITH __dbt__cte__stg_saldos_vigentes AS (\n" + EPH_SQL + "\n)\n"
    "SEL c.Cliente_Id AS cliente_id,\n"
    "    TRIM(c.Nombre) AS nombre,\n"
    "    CAST(c.Fecha_Alta AS DATE FORMAT 'YYYY-MM-DD') AS fecha_alta,\n"
    "    ZEROIFNULL(s.Saldo) AS saldo_actual\n"
    'FROM "BDW_STG"."clientes" c\n'  # lowercase on purpose: catalog says CLIENTES
    "LEFT JOIN __dbt__cte__stg_saldos_vigentes s ON c.Cliente_Id = s.Cliente_Id\n"
    "WHERE c.Segmento = 'PYME'"
)

RESUMEN_SQL = 'LOCKING ROW FOR ACCESS\nSELECT * FROM "BDW_GOLD"."dim_cliente"'

CAMBIO_SQL = (
    'SELECT d.cliente_id, d.saldo_actual * tc.Cotizacion AS saldo_usd\n'
    'FROM "BDW_GOLD"."dim_cliente" d\n'
    "CROSS JOIN BDW_REF.TIPO_CAMBIO tc\n"
    "WHERE tc.Moneda = 'USD'"
)


def _col(name, data_type="VARCHAR(100)"):
    return {"name": name, "description": "", "data_type": data_type, "tags": []}


def _cat_col(name, index, data_type):
    return {"type": data_type, "index": index, "name": name, "comment": None}


def _catalog_entry(unique_id, schema, name, columns):
    return {
        "metadata": {
            "type": "table",
            "schema": schema,
            "name": name,
            "database": None,
            "comment": None,
            "owner": None,
        },
        "columns": {
            col: _cat_col(col, i + 1, dtype) for i, (col, dtype) in enumerate(columns)
        },
        "stats": {},
        "unique_id": unique_id,
    }


def _model(name, schema, sql, depends_on, columns, materialized="table"):
    node = {
        "resource_type": "model",
        "path": f"{name}.sql",
        "original_file_path": f"models/{name}.sql",
        "name": name,
        "alias": name,
        "database": PROFILE_DB,
        "schema": schema,
        "raw_code": sql,
        "compiled_code": sql,
        "config": {"materialized": materialized},
        "depends_on": {"nodes": list(depends_on)},
        "columns": {c: _col(c) for c in columns},
        "description": "",
        "tags": [],
        "meta": {},
    }
    if materialized != "ephemeral":
        node["relation_name"] = f'"{schema}"."{name}"'
    return node


def _source(source_name, name, schema, identifier, columns):
    return {
        "resource_type": "source",
        "source_name": source_name,
        "name": name,
        "identifier": identifier,
        "database": PROFILE_DB,
        "schema": schema,
        "relation_name": f'"{schema}"."{identifier}"',
        "path": "models/sources.yml",
        "original_file_path": "models/sources.yml",
        "columns": {c: _col(c) for c in columns},
        "description": "",
        "tags": [],
        "meta": {},
        "config": {},
    }


def build_artifacts():
    """Return ``(manifest, catalog)`` dicts for a small Teradata project."""
    nodes = {
        EPH_SALDOS: _model(
            "stg_saldos_vigentes", "BDW_GOLD", EPH_SQL, [SRC_SALDOS],
            ["cliente_id", "saldo", "fecha"], materialized="ephemeral",
        ),
        DIM_CLIENTE: _model(
            "dim_cliente", "BDW_GOLD", DIM_SQL, [SRC_CLIENTES, EPH_SALDOS],
            ["cliente_id", "nombre", "fecha_alta", "saldo_actual"],
        ),
        FCT_RESUMEN: _model(
            "fct_resumen", "BDW_GOLD", RESUMEN_SQL, [DIM_CLIENTE], [], materialized="view",
        ),
        FCT_CAMBIO: _model(
            "fct_cambio", "BDW_GOLD", CAMBIO_SQL, [DIM_CLIENTE], ["cliente_id", "saldo_usd"],
        ),
    }
    sources = {
        SRC_CLIENTES: _source(
            "stg", "clientes", "BDW_STG", "CLIENTES",
            ["cliente_id", "nombre", "fecha_alta", "segmento"],
        ),
        SRC_SALDOS: _source(
            "stg", "saldos", "BDW_STG", "SALDOS", ["cliente_id", "saldo", "fecha"],
        ),
    }

    parent_map, child_map = {}, {}
    for nid, node in {**nodes, **sources}.items():
        deps = node.get("depends_on", {}).get("nodes", [])
        parent_map[nid] = list(deps)
        child_map.setdefault(nid, [])
        for dep in deps:
            child_map.setdefault(dep, []).append(nid)

    manifest = {
        "metadata": {
            "adapter_type": "teradata",
            "dbt_version": "1.10.4",
            "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json",
            "project_name": PROJECT,
            "project_id": "0" * 32,
            "invocation_id": "test-fixture",
        },
        "nodes": nodes,
        "sources": sources,
        "exposures": {},
        "metrics": {},
        "semantic_models": {},
        "parent_map": parent_map,
        "child_map": child_map,
    }

    catalog = {
        "metadata": {"dbt_version": "1.10.4"},
        "nodes": {
            DIM_CLIENTE: _catalog_entry(DIM_CLIENTE, "BDW_GOLD", "dim_cliente", [
                ("cliente_id", "INTEGER"), ("nombre", "VARCHAR(100)"),
                ("fecha_alta", "DATE"), ("saldo_actual", "DECIMAL(18,2)"),
            ]),
            FCT_RESUMEN: _catalog_entry(FCT_RESUMEN, "BDW_GOLD", "fct_resumen", [
                ("cliente_id", "INTEGER"), ("nombre", "VARCHAR(100)"),
                ("fecha_alta", "DATE"), ("saldo_actual", "DECIMAL(18,2)"),
            ]),
            FCT_CAMBIO: _catalog_entry(FCT_CAMBIO, "BDW_GOLD", "fct_cambio", [
                ("cliente_id", "INTEGER"), ("saldo_usd", "DECIMAL(18,2)"),
            ]),
        },
        "sources": {
            # Mixed case, as created in Teradata.
            SRC_CLIENTES: _catalog_entry(SRC_CLIENTES, "BDW_STG", "CLIENTES", [
                ("Cliente_Id", "INTEGER"), ("Nombre", "VARCHAR(100)"),
                ("Fecha_Alta", "DATE"), ("Segmento", "VARCHAR(10)"),
            ]),
            SRC_SALDOS: _catalog_entry(SRC_SALDOS, "BDW_STG", "SALDOS", [
                ("Cliente_Id", "INTEGER"), ("Saldo", "DECIMAL(18,2)"), ("Fecha", "DATE"),
            ]),
        },
        "errors": None,
    }
    return copy.deepcopy(manifest), copy.deepcopy(catalog)
