"""Build demo dbt-teradata artifacts (manifest.json + catalog.json).

A small Gold layer for a bank: staging sources in BDW_STG, an ephemeral model,
dimensions and a fact in BDW_GOLD, plus a table outside dbt (BDW_REF).
The artifacts follow dbt-teradata's real output format, so the report built
from them looks like the one you get from a real project.

    python docs/demo/build_demo.py docs/demo/target
    colibri generate --manifest docs/demo/target/manifest.json \
                     --catalog docs/demo/target/catalog.json \
                     --output-dir docs/demo/dist
"""

import json
import sys
from pathlib import Path

PROJECT = "gold_banco"
PROFILE_DB = "DBT_USER"


def src(name):
    return f"source.{PROJECT}.stg.{name}"


def mdl(name):
    return f"model.{PROJECT}.{name}"


SOURCES = {
    "clientes": ("CLIENTES", [
        ("Cliente_Id", "INTEGER", "Identificador único del cliente"),
        ("Nombre", "VARCHAR(100)", "Nombre y apellido"),
        ("Cuit", "CHAR(11)", "CUIT/CUIL"),
        ("Segmento_Cd", "CHAR(3)", "Código de segmento comercial"),
        ("Fecha_Alta", "DATE", "Fecha de alta en el banco"),
        ("Sucursal_Id", "SMALLINT", "Sucursal de radicación"),
    ]),
    "cuentas": ("CUENTAS", [
        ("Cuenta_Id", "BIGINT", "Número de cuenta"),
        ("Cliente_Id", "INTEGER", "Titular"),
        ("Producto_Cd", "CHAR(2)", "CA / CC / PF"),
        ("Moneda_Cd", "CHAR(3)", "ARS / USD"),
        ("Fecha_Apertura", "DATE", ""),
        ("Estado_Cd", "CHAR(1)", "A=activa, B=baja"),
    ]),
    "movimientos": ("MOVIMIENTOS", [
        ("Movimiento_Id", "BIGINT", ""),
        ("Cuenta_Id", "BIGINT", ""),
        ("Fecha_Proceso", "DATE", "Fecha contable"),
        ("Importe", "DECIMAL(18,2)", "Importe en moneda de la cuenta"),
        ("Tipo_Mov_Cd", "CHAR(2)", "DB / CR"),
        ("Canal_Cd", "CHAR(3)", "HB / ATM / SUC"),
    ]),
    "sucursales": ("SUCURSALES", [
        ("Sucursal_Id", "SMALLINT", ""),
        ("Descripcion", "VARCHAR(60)", ""),
        ("Zona", "VARCHAR(30)", ""),
        ("Provincia", "VARCHAR(30)", ""),
    ]),
}

MODELS = {
    "stg_cuentas_activas": dict(
        materialized="ephemeral",
        deps=[src("cuentas")],
        sql="""SELECT Cuenta_Id, Cliente_Id, Producto_Cd, Moneda_Cd, Fecha_Apertura
FROM "BDW_STG"."CUENTAS"
WHERE Estado_Cd = 'A'
QUALIFY ROW_NUMBER() OVER (PARTITION BY Cuenta_Id ORDER BY Fecha_Apertura DESC) = 1""",
        columns=[("cuenta_id", ""), ("cliente_id", ""), ("producto_cd", ""),
                 ("moneda_cd", ""), ("fecha_apertura", "")],
    ),
    "dim_sucursal": dict(
        deps=[src("sucursales")],
        sql="""SEL s.Sucursal_Id AS sucursal_sk,
    TRIM(s.Descripcion) AS sucursal,
    UPPER(s.Zona) AS zona,
    s.Provincia AS provincia
FROM "BDW_STG"."SUCURSALES" s""",
        columns=[("sucursal_sk", "SMALLINT", "Clave de sucursal"), ("sucursal", "VARCHAR(60)", ""),
                 ("zona", "VARCHAR(30)", ""), ("provincia", "VARCHAR(30)", "")],
    ),
    "dim_cliente": dict(
        deps=[src("clientes"), mdl("dim_sucursal")],
        sql="""SEL c.Cliente_Id AS cliente_sk,
    TRIM(c.Nombre) AS nombre,
    c.Cuit AS cuit,
    CASE c.Segmento_Cd WHEN 'PYM' THEN 'PYME' WHEN 'IND' THEN 'Individuos' ELSE 'Otros' END AS segmento,
    CAST(c.Fecha_Alta AS DATE FORMAT 'YYYY-MM-DD') AS fecha_alta,
    s.sucursal AS sucursal_radicacion,
    s.zona AS zona
FROM "BDW_STG"."CLIENTES" c
LEFT JOIN "BDW_GOLD"."dim_sucursal" s ON c.Sucursal_Id = s.sucursal_sk""",
        columns=[("cliente_sk", "INTEGER", "Clave del cliente"), ("nombre", "VARCHAR(100)", ""),
                 ("cuit", "CHAR(11)", ""), ("segmento", "VARCHAR(10)", "Segmento comercial"),
                 ("fecha_alta", "DATE", ""), ("sucursal_radicacion", "VARCHAR(60)", ""),
                 ("zona", "VARCHAR(30)", "")],
    ),
    "dim_cuenta": dict(
        deps=[mdl("stg_cuentas_activas")],
        sql="""WITH __dbt__cte__stg_cuentas_activas AS (
SELECT Cuenta_Id, Cliente_Id, Producto_Cd, Moneda_Cd, Fecha_Apertura
FROM "BDW_STG"."CUENTAS"
WHERE Estado_Cd = 'A'
QUALIFY ROW_NUMBER() OVER (PARTITION BY Cuenta_Id ORDER BY Fecha_Apertura DESC) = 1
)
SELECT ca.cuenta_id AS cuenta_sk,
    ca.cliente_id AS cliente_sk,
    CASE ca.producto_cd WHEN 'CA' THEN 'Caja de ahorro' WHEN 'CC' THEN 'Cuenta corriente' ELSE 'Plazo fijo' END AS producto,
    ca.moneda_cd AS moneda,
    ca.fecha_apertura
FROM __dbt__cte__stg_cuentas_activas ca""",
        columns=[("cuenta_sk", "BIGINT", "Clave de cuenta"), ("cliente_sk", "INTEGER", ""),
                 ("producto", "VARCHAR(20)", ""), ("moneda", "CHAR(3)", ""),
                 ("fecha_apertura", "DATE", "")],
    ),
    "fct_movimientos": dict(
        materialized="incremental",
        deps=[src("movimientos"), mdl("dim_cuenta")],
        sql="""LOCKING ROW FOR ACCESS
SELECT m.Movimiento_Id AS movimiento_id,
    m.Fecha_Proceso AS fecha_proceso,
    cu.cuenta_sk,
    cu.cliente_sk,
    m.Canal_Cd AS canal,
    CASE WHEN m.Tipo_Mov_Cd = 'DB' THEN -m.Importe ELSE m.Importe END AS importe_moneda_origen,
    ZEROIFNULL(CASE WHEN m.Tipo_Mov_Cd = 'DB' THEN -m.Importe ELSE m.Importe END * tc.Cotizacion) AS importe_ars
FROM "BDW_STG"."MOVIMIENTOS" m
JOIN "BDW_GOLD"."dim_cuenta" cu ON m.Cuenta_Id = cu.cuenta_sk
LEFT JOIN BDW_REF.TIPO_CAMBIO tc
    ON tc.Moneda_Cd = cu.moneda AND tc.Fecha = m.Fecha_Proceso
WHERE m.Fecha_Proceso >= ADD_MONTHS(CURRENT_DATE, -13)""",
        columns=[("movimiento_id", "BIGINT", ""), ("fecha_proceso", "DATE", ""),
                 ("cuenta_sk", "BIGINT", ""), ("cliente_sk", "INTEGER", ""),
                 ("canal", "CHAR(3)", ""), ("importe_moneda_origen", "DECIMAL(18,2)", ""),
                 ("importe_ars", "DECIMAL(18,2)", "Importe convertido a pesos")],
    ),
    "agg_saldo_mensual_cliente": dict(
        deps=[mdl("fct_movimientos"), mdl("dim_cliente")],
        sql="""SELECT TRUNC(f.fecha_proceso, 'MM') AS mes,
    f.cliente_sk,
    d.segmento,
    d.zona,
    SUM(f.importe_ars) AS saldo_neto_ars,
    COUNT(*) AS cantidad_movimientos
FROM "BDW_GOLD"."fct_movimientos" f
JOIN "BDW_GOLD"."dim_cliente" d ON f.cliente_sk = d.cliente_sk
GROUP BY 1, 2, 3, 4
HAVING COUNT(*) > 0""",
        columns=[("mes", "DATE", "Primer día del mes"), ("cliente_sk", "INTEGER", ""),
                 ("segmento", "VARCHAR(10)", ""), ("zona", "VARCHAR(30)", ""),
                 ("saldo_neto_ars", "DECIMAL(18,2)", "Créditos menos débitos, en pesos"),
                 ("cantidad_movimientos", "INTEGER", "")],
    ),
}


DESCRIPTIONS = {
    "stg_cuentas_activas": "Última versión de cada cuenta activa (ephemeral).",
    "dim_sucursal": "Dimensión de sucursales.",
    "dim_cliente": "Dimensión de clientes con segmento comercial y sucursal de radicación.",
    "dim_cuenta": "Dimensión de cuentas activas por producto y moneda.",
    "fct_movimientos": "Movimientos de los últimos 13 meses, con signo y convertidos a pesos.",
    "agg_saldo_mensual_cliente": "Saldo neto mensual por cliente, segmento y zona.",
}


def _manifest_col(name, desc=""):
    return {"name": name, "description": desc, "data_type": None, "tags": [], "meta": {}}


def _catalog(uid, schema, name, cols, kind="table"):
    return {
        "metadata": {"type": kind, "schema": schema, "name": name, "database": None,
                     "comment": None, "owner": None},
        "columns": {c: {"type": t, "index": i + 1, "name": c, "comment": None}
                    for i, (c, t) in enumerate(cols)},
        "stats": {}, "unique_id": uid,
    }


def build():
    nodes, sources, cat_nodes, cat_sources = {}, {}, {}, {}

    for name, (ident, cols) in SOURCES.items():
        uid = src(name)
        sources[uid] = {
            "resource_type": "source", "unique_id": uid, "package_name": PROJECT,
            "source_name": "stg", "name": name, "identifier": ident,
            "database": PROFILE_DB, "schema": "BDW_STG",
            "relation_name": f'"BDW_STG"."{ident}"',
            "path": "models/staging/sources.yml",
            "original_file_path": "models/staging/sources.yml",
            "fqn": [PROJECT, "staging", "stg", name],
            "description": f"Tabla {ident} de staging",
            "columns": {c: _manifest_col(c, d) for c, _, d in cols},
            "tags": ["staging"], "meta": {}, "config": {"enabled": True},
        }
        cat_sources[uid] = _catalog(uid, "BDW_STG", ident, [(c, t) for c, t, _ in cols])

    for name, spec in MODELS.items():
        uid = mdl(name)
        mat = spec.get("materialized", "table")
        layer = "staging" if name.startswith("stg_") else "marts"
        cols = spec["columns"]
        node = {
            "resource_type": "model", "unique_id": uid, "package_name": PROJECT,
            "name": name, "alias": name, "database": PROFILE_DB, "schema": "BDW_GOLD",
            "path": f"{layer}/{name}.sql", "original_file_path": f"models/{layer}/{name}.sql",
            "fqn": [PROJECT, layer, name],
            "raw_code": spec["sql"], "compiled_code": spec["sql"],
            "config": {"materialized": mat, "enabled": True},
            "depends_on": {"nodes": spec["deps"], "macros": []},
            "description": DESCRIPTIONS.get(name, ""),
            "columns": {c[0]: _manifest_col(c[0], c[-1] if len(c) == 3 else "") for c in cols},
            "tags": [layer, "gold"] if layer == "marts" else [layer], "meta": {},
        }
        if mat != "ephemeral":
            node["relation_name"] = f'"BDW_GOLD"."{name}"'
            cat_nodes[uid] = _catalog(uid, "BDW_GOLD", name, [(c[0], c[1]) for c in cols])
        nodes[uid] = node

    parent_map, child_map = {}, {}
    for uid, n in {**nodes, **sources}.items():
        deps = n.get("depends_on", {}).get("nodes", [])
        parent_map[uid] = list(deps)
        child_map.setdefault(uid, [])
        for d in deps:
            child_map.setdefault(d, []).append(uid)

    manifest = {
        "metadata": {
            "adapter_type": "teradata", "dbt_version": "1.10.4",
            "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json",
            "project_name": PROJECT, "project_id": "0" * 32, "invocation_id": "demo",
            "generated_at": "2026-10-17T12:00:00Z",
        },
        "nodes": nodes, "sources": sources, "exposures": {}, "metrics": {},
        "semantic_models": {}, "macros": {}, "docs": {},
        "parent_map": parent_map, "child_map": child_map,
    }
    catalog = {"metadata": {"dbt_version": "1.10.4"}, "nodes": cat_nodes,
               "sources": cat_sources, "errors": None}
    return manifest, catalog


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "target")
    out.mkdir(parents=True, exist_ok=True)
    manifest, catalog = build()
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False))
    (out / "catalog.json").write_text(json.dumps(catalog, indent=1, ensure_ascii=False))
    print(f"wrote {out}/manifest.json and {out}/catalog.json")
