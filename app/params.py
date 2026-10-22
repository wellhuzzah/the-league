from fastapi import HTTPException, Query

MAX_LIMIT = 100
MIN_SEARCH = 2


def Limit(default: int):
    """?limit= for a list endpoint: 1 to MAX_LIMIT, keeping the endpoint's own default.
    Out-of-range values get a 422 instead of a huge response (or a 500 for negatives)."""
    return Query(default, ge=1, le=MAX_LIMIT)


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
