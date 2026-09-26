"""
Obligation validator for regulation change tracking.

Obligations are YAML records grounding regulatory change in specific clause text.
"""
from __future__ import annotations

import glob as _glob
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from amend.regdiff import Version, DiffEntry, normalize


REQUIRED_FIELDS = {"id", "citation", "version", "change", "quote", "statement", "shape", "status"}
VALID_CHANGES = {"added", "removed", "modified", "renumbered", "unchanged"}
VALID_SHAPES = {"code", "process", "contract"}
VALID_STATUSES = {"proposed", "approved", "rejected"}


@dataclass
class Obligation:
    id: str
    citation: str
    version: str          # date string
    change: str
    quote: str
    statement: str
    shape: str
    status: str
    exceptions: list[str] = field(default_factory=list)
    approved_by: Optional[str] = None


@dataclass
class ValidationProblem:
    obligation_id: str
    field: Optional[str]
    message: str

    def __str__(self) -> str:
        loc = f"[{self.field}] " if self.field else ""
        return f"{self.obligation_id}: {loc}{self.message}"


def load(path: str | Path) -> list[Obligation]:
    """Load one YAML file or every *.yaml in a folder.

    Accepts three top-level shapes per YAML document:
    - A list of obligation records
    - A mapping with an "obligations" key whose value is a list
    - A single obligation record (plain mapping)

    Anything else raises ValueError naming the file.
    """
    p = Path(path)
    if p.is_dir():
        files = sorted(p.glob("*.yaml"))
    else:
        files = [p]

    obligations: list[Obligation] = []
    for f in files:
        with open(f, "r", encoding="utf-8") as fh:
            docs = list(yaml.safe_load_all(fh))
        for doc in docs:
            if doc is None:
                # Empty document — skip silently
                continue
            if isinstance(doc, list):
                # Top-level list of records
                items = doc
            elif isinstance(doc, dict):
                if isinstance(doc.get("obligations"), list):
                    # Mapping with an "obligations" key
                    items = doc["obligations"]
                else:
                    # Single obligation record
                    items = [doc]
            else:
                raise ValueError(
                    f"{f}: unexpected top-level YAML type {type(doc).__name__!r}; "
                    "expected a list, a mapping with 'obligations', or a single record"
                )
            for item in items:
                ob = _dict_to_obligation(item)
                obligations.append(ob)
    return obligations


def _dict_to_obligation(d: dict) -> Obligation:
    return Obligation(
        id=str(d.get("id", "")),
        citation=str(d.get("citation", "")),
        version=str(d.get("version", "")),
        change=str(d.get("change", "")),
        quote=str(d.get("quote", "")),
        statement=str(d.get("statement", "")),
        shape=str(d.get("shape", "")),
        status=str(d.get("status", "")),
        exceptions=[str(e) for e in d.get("exceptions", [])],
        approved_by=d.get("approved_by"),
    )


def _clause_text_with_children(citation: str, version: Version) -> str:
    """
    Return the normalized text of a clause plus all its sub-paragraphs,
    concatenated with a space.
    """
    prefix = citation + "("
    parts = []
    # Include the clause itself
    if citation in version.clauses:
        parts.append(version.clauses[citation])
    # Include all children (citations that start with citation + "(")
    for cit, text in version.clauses.items():
        if cit.startswith(prefix):
            parts.append(text)
    return normalize(" ".join(parts))


def validate(
    obligations: list[Obligation],
    versions: dict[str, Version],
    redline: list[DiffEntry],
) -> list[ValidationProblem]:
    """
    Validate obligations against the provided versions and redline.
    Returns a list of ValidationProblem objects.
    """
    problems: list[ValidationProblem] = []
    seen_ids: dict[str, int] = {}

    # Build a lookup: citation → DiffEntry from the redline
    redline_by_new_cit: dict[str, DiffEntry] = {}
    redline_by_old_cit: dict[str, DiffEntry] = {}
    for entry in redline:
        redline_by_new_cit[entry.citation] = entry
        if entry.old_citation:
            redline_by_old_cit[entry.old_citation] = entry

    for ob in obligations:
        pid = ob.id or "<unknown>"

        # --- Required fields ---
        for f in REQUIRED_FIELDS:
            val = getattr(ob, f, None)
            if val is None or str(val).strip() == "":
                problems.append(ValidationProblem(pid, f, "required field is missing"))

        # --- Duplicate id ---
        if ob.id:
            if ob.id in seen_ids:
                problems.append(ValidationProblem(pid, "id", "duplicate id"))
            else:
                seen_ids[ob.id] = 1

        # --- Enum validation ---
        if ob.change and ob.change not in VALID_CHANGES:
            problems.append(ValidationProblem(pid, "change", f"unknown change type '{ob.change}'"))
        if ob.shape and ob.shape not in VALID_SHAPES:
            problems.append(ValidationProblem(pid, "shape", f"unknown shape '{ob.shape}'"))
        if ob.status and ob.status not in VALID_STATUSES:
            problems.append(ValidationProblem(pid, "status", f"unknown status '{ob.status}'"))

        # --- Citation must exist in that version ---
        version_obj = versions.get(ob.version)
        if version_obj is None:
            problems.append(ValidationProblem(pid, "version", f"version '{ob.version}' not loaded"))
        else:
            if ob.citation not in version_obj.clauses:
                problems.append(ValidationProblem(
                    pid, "citation",
                    f"citation '{ob.citation}' does not exist in version {ob.version}",
                ))
            else:
                # --- Quote must be verbatim substring ---
                full_text = _clause_text_with_children(ob.citation, version_obj)
                norm_quote = normalize(ob.quote)
                if norm_quote not in full_text:
                    problems.append(ValidationProblem(
                        pid, "quote",
                        "quote is not a verbatim substring of the clause text",
                    ))

                # --- Exceptions must be verbatim substrings ---
                for exc in ob.exceptions:
                    norm_exc = normalize(exc)
                    if norm_exc not in full_text:
                        problems.append(ValidationProblem(
                            pid, "exceptions",
                            f"exception quote not found in clause text: {exc[:60]}",
                        ))

        # --- Change must agree with redline ---
        if ob.citation and ob.change:
            entry = redline_by_new_cit.get(ob.citation)
            if entry is None:
                # Maybe it was removed – check old citations
                entry = redline_by_old_cit.get(ob.citation)
            if entry is not None:
                if ob.change != entry.change:
                    problems.append(ValidationProblem(
                        pid, "change",
                        f"claimed change '{ob.change}' disagrees with redline '{entry.change}'",
                    ))

        # --- approved requires approved_by ---
        if ob.status == "approved" and not ob.approved_by:
            problems.append(ValidationProblem(
                pid, "approved_by",
                "status is 'approved' but approved_by is missing",
            ))

    return problems
