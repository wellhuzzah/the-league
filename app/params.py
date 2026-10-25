from fastapi import HTTPException, Query

MAX_LIMIT = 100
MIN_SEARCH = 2


def Limit(default: int):
    """?limit= for a list endpoint: 1 to MAX_LIMIT, keeping the endpoint's own default.
    Out-of-range values get a 422 instead of a huge response (or a 500 for negatives). Top-N lists
    keep every row tied at the cutoff, so a response can be longer than the limit (top_n)."""
    return Query(default, ge=1, le=MAX_LIMIT)


def top_n(query: str, rank_by: str, order_by: str, n: str) -> str:
    """SQL for the top n rows of query (a SELECT with no ORDER BY or LIMIT), ranked by rank_by.
    League rule: equal values share a rank, so every row tied at the cutoff is kept and the list
    can run past n. Rows come back in order_by (rank_by plus tie-breakers, written against the
    query's output column names) with an extra column "place", the shared rank; drop it with
    without() unless the endpoint shows it. n is a SQL parameter or literal, e.g. "$1"."""
    return f"""
        SELECT * FROM (
            SELECT q.*, RANK() OVER (ORDER BY {rank_by}) AS place
            FROM ({query}) q
        ) ranked
        WHERE place <= {n}
        ORDER BY {order_by}
    """


def without(rows, *keys):
    """Rows as dicts minus the given keys (default "place"), for helper columns a response
    shouldn't carry."""
    drop = set(keys or ("place",))
    return [{k: v for k, v in dict(r).items() if k not in drop} for r in rows]


def search_term(name: str) -> str:
    """A user-typed player name, stripped; 422 if shorter than MIN_SEARCH characters,
    since a one-character ILIKE '%x%' matches nearly every row."""
    term = name.strip()
    if len(term) < MIN_SEARCH:
        raise HTTPException(status_code=422, detail=f"search needs at least {MIN_SEARCH} characters")
    return term


def like_escape(text: str) -> str:
    """text with LIKE/ILIKE wildcards escaped so it matches literally (backslash is the default ESCAPE)."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
