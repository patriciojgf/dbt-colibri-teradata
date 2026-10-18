from sqlglot import exp
from sqlglot.lineage import maybe_parse
from sqlglot.tokens import Tokenizer, TokenType


_TYPE_ATTRIBUTE_HINTS = ("CHARACTER", "CASESPECIFIC", "UPPERCASE", "CS")


def strip_teradata_type_attributes(sql: str) -> str:
    """Remove Teradata column-type attributes that SQLGlot cannot parse.

    ``CAST(x AS VARCHAR(50) CHARACTER SET UNICODE NOT CASESPECIFIC)`` makes the
    whole statement unparseable in SQLGlot's Teradata dialect. These attributes
    only affect storage and comparison, never which columns feed which, so
    dropping them keeps lineage intact. Removed:

    * ``CHARACTER SET <name>`` and ``[NOT] CASESPECIFIC`` anywhere;
    * ``[NOT] CS`` and ``[NOT] UPPERCASE`` only inside the type of a ``CAST``,
      since ``cs`` is also a common alias.

    Works on tokens, so string literals and comments are never touched.
    """
    upper = sql.upper()
    if not any(hint in upper for hint in _TYPE_ATTRIBUTE_HINTS):
        return sql
    try:
        tokens = Tokenizer(dialect="teradata").tokenize(sql)
    except Exception:
        return sql

    def word(i):
        if 0 <= i < len(tokens) and tokens[i].token_type != TokenType.STRING:
            return tokens[i].text.upper()
        return None

    spans = []          # (start, end) character ranges to delete
    depth = 0
    casts = []          # stack of [paren depth of the CAST, inside its type?]
    i = 0
    while i < len(tokens):
        tok, w = tokens[i], word(i)
        if tok.token_type == TokenType.L_PAREN:
            depth += 1
        elif tok.token_type == TokenType.R_PAREN:
            if casts and casts[-1][0] == depth:
                casts.pop()
            depth -= 1
        elif w == "CAST" and i + 1 < len(tokens) and tokens[i + 1].token_type == TokenType.L_PAREN:
            casts.append([depth + 1, False])
        elif tok.token_type == TokenType.ALIAS and casts and casts[-1][0] == depth:
            casts[-1][1] = True

        in_cast_type = bool(casts) and casts[-1][1] and casts[-1][0] == depth
        start = i
        if w == "NOT":
            i += 1
            w = word(i)
        if w == "CHARACTER" and word(i + 1) == "SET" and word(i + 2) and start == i:
            spans.append((tokens[i].start, tokens[i + 2].end))
            i += 3
            continue
        if w == "CASESPECIFIC" or (in_cast_type and w in ("CS", "UPPERCASE")):
            spans.append((tokens[start].start, tokens[i].end))
            i += 1
            continue
        i = start + 1

    if not spans:
        return sql
    out, last = [], 0
    for begin, end in spans:
        out.append(sql[last:begin])
        last = end + 1
    out.append(sql[last:])
    return "".join(out)


def parse_sql(sql, dialect="teradata"):
    """Parse a single SQL statement with SQLGlot's Teradata dialect."""
    if isinstance(sql, str) and dialect == "teradata":
        sql = strip_teradata_type_attributes(sql)
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
