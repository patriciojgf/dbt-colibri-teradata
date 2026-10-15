# dbt-colibri for Teradata

Column-level lineage report for **dbt-teradata** projects. A Teradata-only fork of [b-ned/dbt-colibri](https://github.com/b-ned/dbt-colibri): it reads `manifest.json` and `catalog.json`, traces every column back to its sources with SQLGlot's Teradata dialect, and writes a standalone HTML dashboard.

## Install

```bash
pip install git+https://github.com/patriciojgf/dbt-colibri-teradata
```

## Use

```bash
dbt compile
dbt docs generate      # catalog.json is required for column resolution
colibri generate       # writes dist/index.html and dist/colibri-manifest.json
```

Options: `--manifest`, `--catalog`, `--output-dir`, `--light` (drops compiled SQL from the report for very large projects), `--debug`.

## What differs from upstream

- **Only the `teradata` adapter is accepted.** Any other `adapter_type` in the manifest is rejected.
- **Two-part names.** Objects resolve as `database.object`, matching the `relation_name` dbt-teradata emits (`"BDW_STG"."CLIENTES"`). The profile database dbt stores in `node.database` is ignored, and the catalog's NULL `database` is handled.
- **Case-insensitive identifiers.** Teradata compares names case-insensitively even when quoted, so tables, columns and SQL identifiers are all folded to lowercase before matching. `"BDW_STG"."clientes"` in SQL matches `CLIENTES` in the catalog.
- **Teradata SQL.** `SEL`, `QUALIFY`, `ZEROIFNULL`, `CAST ... FORMAT`, `TOP`, `SAMPLE` and a leading `LOCKING ROW FOR ACCESS` are parsed.
- **Ephemeral models referenced with an alias** (`JOIN __dbt__cte__x s`) are attributed to the ephemeral model, for both column and join/filter lineage.
- **No outbound calls by default.** The PyPI version check was removed, and the report's anonymous usage telemetry is off unless you pass `--enable-telemetry`.
- The report tree groups objects as `teradata › <database> › <object>`.

Tables referenced in SQL that are not dbt models or sources show up as `_HARDCODED_REF___<database>.<table>`.

## Limitations

- Teradata OTF (three-part `datalake.db.table`) relations are not supported for column resolution.
- Python models are skipped.

## Development

```bash
uv sync --dev
uv run pytest
```

Tests use synthetic artifacts shaped like real dbt-teradata output (`tests/teradata_fixtures.py`).

## License

MIT, see [LICENSE](LICENSE). Based on [dbt-colibri](https://github.com/b-ned/dbt-colibri) by bned, which builds on [dbt-column-lineage-extractor](https://github.com/canva-public/dbt-column-lineage-extractor) and [SQLGlot](https://github.com/tobymao/sqlglot).
