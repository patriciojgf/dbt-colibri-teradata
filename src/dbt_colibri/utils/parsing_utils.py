from sqlglot import exp
from sqlglot.lineage import maybe_parse


def parse_sql(sql, dialect="teradata"):
    """Parse a single SQL statement with SQLGlot's Teradata dialect."""
    return maybe_parse(sql, dialect=dialect)


def normalize_table_relation_name(name: str) -> str:
    """Normalize a dbt-teradata relation_name (``"DB"."TABLE"``) to ``db.table``.

    Teradata object names are case-insensitive, so the key is lowercased.
    """
    return name.replace('"', "").lower()


def normalize_identifiers(expression):
    """Unquote and lowercase every identifier.

    Teradata compares identifiers case-insensitively even when quoted, while
    SQLGlot treats quoted identifiers as case-sensitive.  Folding them all to
    unquoted lowercase makes the AST match the lowercased schema dict.
    """
    def transform_identifier(node):
        if isinstance(node, exp.Identifier):
            return exp.Identifier(this=node.this.lower(), quoted=False)
        return node

    return expression.transform(transform_identifier)
