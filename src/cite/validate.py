"""Thin, agent-friendly validation wrapper around models.missing_required_fields."""

from cite.models import CITE_TYPES, missing_required_fields


def validate(cite_type: str, record: dict) -> dict:
    """Return {"status": "ok"} if valid, else
       {"status": "missing_fields", "cite_type": cite_type, "missing": [...],
        "hint": "re-run: cite add <file> --manual --type <cite_type> --field <key>=... ..."}.
    Build the hint listing each missing token as ``--field <token>=...``.
    Raise ValueError (let it propagate) if cite_type not in CITE_TYPES.
    """
    if cite_type not in CITE_TYPES:
        raise ValueError(
            f"Unknown cite_type {cite_type!r}; must be one of {', '.join(CITE_TYPES)}"
        )

    missing = missing_required_fields(cite_type, record)
    if not missing:
        return {"status": "ok"}

    field_flags = " ".join(f"--field {token}=..." for token in missing)
    hint = (
        f"re-run: cite add <file> --manual --type {cite_type} {field_flags}"
    )
    return {
        "status": "missing_fields",
        "cite_type": cite_type,
        "missing": missing,
        "hint": hint,
    }
