# dbt-colibri para Teradata

**Linaje a nivel de columna para proyectos dbt sobre Teradata.** Lee los artefactos que ya genera dbt (`manifest.json` y `catalog.json`), parsea el SQL compilado con el dialecto Teradata y arma un reporte HTML autocontenido donde cada columna se puede rastrear hasta su origen.

Es un fork de [b-ned/dbt-colibri](https://github.com/b-ned/dbt-colibri), recortado y adaptado para que funcione bien con `dbt-teradata`.

![Linaje de la columna importe_ars hasta sus columnas de origen](docs/img/column-lineage.png)

<sub>Seleccionando `fct_movimientos.importe_ars` se resaltan las tres columnas de las que sale: `importe` y `tipo_mov_cd` de staging, y `cotizacion` de una tabla de referencia que no está en dbt.</sub>

---

## En 30 segundos

```bash
pip install git+https://github.com/patriciojgf/dbt-colibri-teradata

dbt compile
dbt docs generate        # genera catalog.json, necesario para resolver columnas
colibri generate         # escribe dist/index.html
```

Abrís `dist/index.html` en el navegador y listo. Son archivos estáticos, sin servidor ni dependencias externas: se pueden mandar por mail, subir a un share o publicar en GitHub/GitLab Pages.

```text
Welcome to dbt-colibri for Teradata 🐦
[INFO] Loading dbt manifest and catalog...
[INFO] Detected adapter type: teradata
[INFO] 1/6 ephemeral models not in catalog (columns derived from SQL).
[INFO] Running with configuration:
         dbt-colbri version : 0.3.6+td.1
         dbt version        : 1.10.4
         SQL dialect        : teradata
         dbt project        : gold_banco
[INFO] Extracting lineage data...
[INFO] Generating report...
[INFO] Found 8 relationships

✅ Report completed!
  📁 JSON: dist/colibri-manifest.json
  🌐 HTML: dist/index.html
```

## Cómo funciona

```mermaid
flowchart LR
    subgraph dbt["Tu proyecto dbt-teradata"]
        A["dbt compile"] --> M[("manifest.json<br/>SQL compilado + dependencias")]
        B["dbt docs generate"] --> C[("catalog.json<br/>columnas y tipos reales")]
    end
    M --> P["colibri generate"]
    C --> P
    P --> S["SQLGlot<br/>dialecto Teradata"]
    S --> L["Linaje por columna<br/>+ joins y filtros"]
    S --> R["Relaciones entre tablas<br/>tests dbt + JOINs"]
    L --> H["dist/index.html<br/>reporte de linaje"]
    R --> E["dist/relaciones.html<br/>diagrama ER"]
    L --> J["dist/colibri-manifest.json<br/>linaje y relaciones en JSON"]
    R --> J
```

No se conecta a Teradata: trabaja solo con los archivos que dbt deja en `target/`.

---

## Qué te da

### 1. Linaje de modelos

Para cualquier modelo ves de qué depende y quién lo consume, con el botón **←/→** podés abrir más niveles hacia arriba o hacia abajo. Los sources aparecen en naranja, los modelos en azul y las tablas que no están en dbt en gris.

![Grafo de linaje del modelo agg_saldo_mensual_cliente](docs/img/model-lineage.png)

### 2. Linaje por columna, a través de varios saltos

Hacés clic en una columna y se resalta todo su camino. Acá `saldo_neto_ars` del agregado mensual viene de `fct_movimientos.importe_ars`, que a su vez sale de `movimientos` y de `tipo_cambio.cotizacion`:

![Linaje de saldo_neto_ars a través de varios modelos](docs/img/multi-hop.png)

Cada columna lleva una marca con el tipo de transformación:

| Marca | Tipo | Ejemplo en el demo |
|:---:|---|---|
| **P** | Pass-through: pasa sin cambios | `cuit` en `dim_cliente` |
| **R** | Rename: misma columna, otro nombre | `Canal_Cd AS canal` |
| **T** | Transformation: se calcula | `ZEROIFNULL(... * tc.Cotizacion) AS importe_ars` |
| **U** | Unknown: sin columna de origen | `COUNT(*) AS cantidad_movimientos` |

Además de las columnas del `SELECT`, registra qué columnas se usan en **joins** (`ON`) y **filtros** (`WHERE`, `HAVING`), así sabés qué se rompe aunque una columna no se proyecte.

### 3. Ficha de cada modelo

Columnas con tipo real de Teradata (del catálogo) y la descripción que pusiste en el YAML:

![Columnas de dim_cliente con tipos y descripciones](docs/img/model-info.png)

Y el SQL, tanto el raw como el compilado:

![SQL de fct_movimientos](docs/img/model-code.png)

### 4. Vista por base de datos

<img src="docs/img/warehouse.png" align="right" width="230" alt="Árbol por base de datos Teradata">

Además de la vista por carpetas del proyecto, podés navegar por **base de datos de Teradata**, igual que en Teradata Studio.

Las tablas que se leen en el SQL pero no están declaradas como source (en el demo, `BDW_REF.TIPO_CAMBIO`) aparecen bajo su base, en este caso `bdw_ref`, marcadas como *hardcoded*. Sirve para encontrar dependencias que todavía no pasaron por `sources.yml`.

<br clear="right"/>

### 5. Relaciones entre tablas (diagrama ER)

La pestaña **Relaciones** dibuja el modelo dimensional: qué columna de cada tabla apunta a qué clave de otra, con su cardinalidad.

![Pestaña Relaciones en el reporte](docs/img/relaciones-tab.png)

![Diagrama de relaciones con el detalle de fct_movimientos.cliente_sk → dim_cliente.cliente_sk](docs/img/relaciones.png)

Las relaciones salen de dos lugares:

| Línea | Origen | Ejemplo en el demo |
|---|---|---|
| continua | **Declarada**: un test `relationships` de dbt | `fct_movimientos.cuenta_sk → dim_cuenta` |
| punteada | **Inferida**: un `JOIN ... ON a = b` del SQL compilado | `fct_movimientos.cliente_sk → dim_cliente`, que se une en el agregado mensual |

Cuando el JOIN une tablas de staging con una dimensión, colibri sigue el linaje para llegar a la columna que importa en la capa Gold. Por ejemplo, `dim_cliente` hace `JOIN dim_sucursal ON clientes.sucursal_id = sucursal_sk`; como `dim_cliente.sucursal_sk` sale de `clientes.sucursal_id`, la relación se muestra como `dim_cliente.sucursal_sk → dim_sucursal.sucursal_sk`.

La **cardinalidad** (la pata de gallo marca el lado N) sale de los tests `unique` o de las constraints `primary_key`. Sin ellos la línea se dibuja igual, pero sin cardinalidad.

Al tocar una relación, el panel derecho muestra el JOIN exacto donde se usa y, si no tiene test, **el YAML listo para copiar** en el archivo que corresponde. El formato se adapta a tu versión de dbt: `tests` o `data_tests`, y `arguments:` desde la 1.10.5. Así, lo que el SQL ya da por hecho queda validado en cada `dbt test`.

Otros detalles:
- **Filtros:** solo modelos o también sources, y declaradas o inferidas.
- **Navegación:** buscador de tabla o columna, zoom, y tarjetas que se pueden arrastrar.
- **Foco:** al tocar una tabla se ven solo sus relaciones, con un link a su linaje en el reporte. En proyectos de más de 40 tablas arranca así, en la más conectada.
- **Tema:** respeta el claro u oscuro que elegiste en el reporte.

---

## SQL de Teradata soportado

| Construcción | Ejemplo |
|---|---|
| `SEL` abreviado | `SEL c.Cliente_Id AS cliente_sk ...` |
| `QUALIFY` | `QUALIFY ROW_NUMBER() OVER (PARTITION BY ...) = 1` |
| `LOCKING ROW FOR ACCESS` | al inicio de la consulta |
| `CAST ... FORMAT` | `CAST(c.Fecha_Alta AS DATE FORMAT 'YYYY-MM-DD')` |
| Funciones propias | `ZEROIFNULL`, `ADD_MONTHS`, `TRUNC(..., 'MM')` |
| `TOP`, `SAMPLE` | `SELECT TOP 10 ...`, `... SAMPLE 100` |
| Mayúsculas/minúsculas | `"BDW_STG"."clientes"` matchea con `CLIENTES` del catálogo |
| Modelos ephemeral | `JOIN __dbt__cte__stg_x s` se atribuye al modelo `stg_x` |

## Opciones

```bash
colibri generate --manifest target/manifest.json --catalog target/catalog.json --output-dir dist
```

| Opción | Por defecto | Para qué |
|---|---|---|
| `--manifest` | `target/manifest.json` | Ruta al manifest de dbt |
| `--catalog` | `target/catalog.json` | Ruta al catálogo de dbt |
| `--output-dir` | `dist` | Dónde escribir el HTML y el JSON |
| `--light` | apagado | Omite el SQL del reporte, para proyectos muy grandes |
| `--debug` | apagado | Logging detallado |
| `--enable-telemetry` | apagado | Ver abajo |

Todo lo que muestra el reporte es gratuito: los botones de funciones pagas del proyecto original vienen ocultos.

Por defecto **no hace ninguna llamada a internet**: no consulta PyPI y el reporte no envía telemetría. Si querés mandarle estadísticas anónimas al proyecto original, agregá `--enable-telemetry`.

## Publicarlo en cada deploy

Como es un HTML estático, alcanza con generarlo en el pipeline y publicarlo. Ejemplo para GitLab Pages:

```yaml
pages:
  stage: deploy
  script:
    - pip install dbt-teradata git+https://github.com/patriciojgf/dbt-colibri-teradata
    - dbt deps
    - dbt compile
    - dbt docs generate
    - colibri generate --output-dir public
  artifacts:
    paths: [public]
  only: [main]
```

`dbt docs generate` necesita conexión a Teradata para leer el catálogo, así que el runner tiene que llegar a la base.

## Probarlo sin un proyecto propio

Las capturas de este README salen de un proyecto demo incluido en el repo: un esquema estrella de un banco con staging, dimensiones, un fact incremental, un ephemeral y una tabla de referencia fuera de dbt.

```bash
python docs/demo/build_demo.py docs/demo/target
colibri generate --manifest docs/demo/target/manifest.json \
                 --catalog docs/demo/target/catalog.json \
                 --output-dir docs/demo/dist
```

## Diferencias con el proyecto original

- Solo acepta el adapter `teradata`; cualquier otro `adapter_type` se rechaza.
- Resuelve nombres de dos partes (`base.objeto`), que es como los emite dbt-teradata. Ignora el `database` del profile y el `database` nulo del catálogo.
- Trata los identificadores como Teradata: sin distinguir mayúsculas, aunque estén entre comillas.
- Ignora un `LOCKING ROW FOR ACCESS` inicial.
- Atribuye bien los ephemeral referenciados con alias, también en joins y filtros.
- Agrupa las tablas externas bajo su base de datos en el árbol.
- Sin chequeo de versión contra PyPI y con telemetría apagada por defecto.
- Oculta los botones de funciones pagas de Colibri Cloud (ERD, Saved Views, Git Blame), que en el original solo llevan a su página comercial.
- Agrega la pestaña **Relaciones** con el diagrama ER, que en el original es una función paga.
- Corrige el tipo de linaje de las columnas que pasan por un ephemeral: antes figuraban todas como *transformation*.

## Limitaciones

- Las tablas OTF de Teradata (nombres de tres partes `datalake.db.tabla`) no se resuelven a nivel de columna.
- Los modelos Python se saltean.
- Si un modelo no está en `catalog.json` (por ejemplo, nunca se materializó), sus columnas no se pueden resolver.
- Las relaciones inferidas solo toman igualdades simples entre columnas en el `ON` (`a.x = b.y`). Un JOIN con expresiones (`ON TRIM(a.x) = b.y`) no genera relación.

## Desarrollo

```bash
uv sync --dev
uv run pytest
```

Los tests usan artefactos sintéticos con el formato real de dbt-teradata (`tests/teradata_fixtures.py`) y el proyecto demo.

## Licencia

MIT, ver [LICENSE](LICENSE). Basado en [dbt-colibri](https://github.com/b-ned/dbt-colibri) de bned, que a su vez se apoya en [dbt-column-lineage-extractor](https://github.com/canva-public/dbt-column-lineage-extractor) y [SQLGlot](https://github.com/tobymao/sqlglot).
