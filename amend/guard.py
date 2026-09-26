"""amend/guard.py — zero-LLM compliance gate for FastAPI + SQLAlchemy repos.

Four rules:
  G-MFA       : route exposes customer data but lacks require_mfa, or is under a mount
                (excludes auth endpoints that establish a session).
                Cites 16 CFR 314.4(c)(5).
  G-MFA-LOGIN : login or token endpoint that issues a token without verifying a second factor.
                Cites 16 CFR 314.4(c)(5).
  G-LOG       : logging call whose arguments include a request body or sensitive field
                (not mere method/path/status/duration).  Cites 314.4(c)(3).
  G-EXPORT    : job writes a government_id-class column (after alias tracing) to a file.
                Cites 314.4(c)(3) and 314.4(c)(6)(i).

Public API
----------
run_guard(repo_root, cfg=None) -> list[Failure]
cmd_guard(args) -> None   (CLI entry point)

A Failure has: file, line, rule, clause, message.
"""
from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from amend.graph import Graph, Node, _classify_column, _load_amend_yaml, _load_data_classes, build as build_graph


# ──────────────────────────────────────────────────────────────────────────────
# Failure data class
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class Failure:
    file: str
    line: int
    rule: str
    clause: str
    message: str

    def __str__(self) -> str:
        return f"{self.file}:{self.line}  {self.rule}  {self.clause}  {self.message}"

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "line": self.line,
            "rule": self.rule,
            "clause": self.clause,
            "message": self.message,
        }


# ──────────────────────────────────────────────────────────────────────────────
# Helpers: sensitive argument detection for G-LOG
# ──────────────────────────────────────────────────────────────────────────────

# An argument is considered to carry sensitive data if it matches any of these
# (request body / payload / ORM instance / sensitive field names).
_BODY_ARG_PATTERNS = [
    r"\bbody\b",
    r"\bpayload\b",
    r"\b__dict__\b",
    r"\bssn\b",
    r"\btin\b",
    r"\bbank_account\b",
    r"\baccount_number\b",
    r"\brouting\b",
    r"\bincome\b",
]

_BODY_ARG_RE = re.compile("|".join(_BODY_ARG_PATTERNS), re.IGNORECASE)

# These argument patterns carry ONLY safe metadata (not sensitive data)
_SAFE_ARG_RE = re.compile(
    r"^(?:"
    r"request\.method"
    r"|request\.url\.path"
    r"|request\.url"
    r"|response\.status_code"
    r"|duration_ms"
    r"|method"
    r"|path"
    r"|status"
    r"|app\.id"
    r"|application_id"
    r")$",
    re.IGNORECASE,
)

# A format-string arg is safe if it only references metadata tokens
_SAFE_FORMAT_RE = re.compile(
    r"^['\"].*(?:method=|path=|status=|duration_ms=).*['\"]$",
    re.IGNORECASE,
)

# A format-string that references body is sensitive
_BODY_FORMAT_RE = re.compile(r"body=", re.IGNORECASE)


def _arg_carries_body(arg_text: str, data_classes: dict[str, list[str]]) -> bool:
    """Return True if this single log-call argument carries sensitive/body data."""
    stripped = arg_text.strip()

    # Safe metadata args are never sensitive
    if _SAFE_ARG_RE.match(stripped):
        return False

    # Format strings: only flag if 'body=' is referenced
    if (stripped.startswith('"') or stripped.startswith("'")):
        return bool(_BODY_FORMAT_RE.search(stripped))

    # Non-format arg: check body patterns
    if _BODY_ARG_RE.search(stripped):
        return True

    # Check against data-class column name patterns
    lower = stripped.lower()
    for patterns in data_classes.values():
        for pat in patterns:
            if pat in lower:
                return True

    return False


# ──────────────────────────────────────────────────────────────────────────────
# G-MFA check
# ──────────────────────────────────────────────────────────────────────────────

_MFA_CLAUSE = "16 CFR 314.4(c)(5)"

# Auth endpoints that establish a session are exempt from G-MFA (they ARE the auth layer).
# They are instead checked by G-MFA-LOGIN.
_AUTH_PATH_RE = re.compile(r"/(login|token|auth|signin|sign-in|logout|refresh)$", re.IGNORECASE)


def _is_auth_endpoint(route: Node) -> bool:
    """Return True if the route is an authentication endpoint (login/token/etc.)."""
    # Use full_path if available, else raw path
    path = route.attrs.get("full_path") or route.attrs.get("path", "")
    method = route.attrs.get("method", "")
    return bool(_AUTH_PATH_RE.search(path)) and method in ("POST", "PUT")


def _check_mfa(g: Graph, mfa_dep: str) -> list[Failure]:
    failures: list[Failure] = []

    missing = g.routes_missing_dependency(mfa_dep)
    for route in missing:
        # Exempt auth endpoints that establish a session (covered by G-MFA-LOGIN instead)
        if _is_auth_endpoint(route):
            continue

        full_path = route.attrs.get("full_path") or route.attrs.get("path", "?")
        failures.append(
            Failure(
                file=route.file,
                line=route.line,
                rule="G-MFA",
                clause=_MFA_CLAUSE,
                message=(
                    f"Route {route.attrs.get('method','?')} {full_path} "
                    f"({'mounted sub-app' if route.attrs.get('mounted') else 'router-level'}) "
                    f"exposes customer data without {mfa_dep}"
                ),
            )
        )
    return failures


# ──────────────────────────────────────────────────────────────────────────────
# G-MFA-LOGIN check
# ──────────────────────────────────────────────────────────────────────────────

_MFA_LOGIN_CLAUSE = "16 CFR 314.4(c)(5)"

# Patterns that indicate a function body verifies a second factor before issuing a token
_MFA_CHECK_RE = re.compile(r"\botp\b|\btotp\b|\bfactor\b|\bmfa\b|\bsecond.factor\b", re.IGNORECASE)
# Patterns that indicate a token/session is issued
_TOKEN_ISSUE_RE = re.compile(r"\bcreate_token\b|\baccess_token\b|\bsession\b", re.IGNORECASE)


def _check_mfa_login(g: Graph, repo_root: Path) -> list[Failure]:
    """Flag POST login/token endpoints that issue a token without verifying a second factor."""
    from amend.graph import _find_python_files, _rel
    failures: list[Failure] = []

    py_files = _find_python_files(repo_root)

    # Build a map of file_rel -> source for function body inspection
    source_map: dict[str, str] = {}
    for py_path in py_files:
        file_rel = _rel(py_path, repo_root)
        try:
            source_map[file_rel] = py_path.read_text()
        except Exception:
            pass

    for route in g.nodes.values():
        if route.kind != "route":
            continue
        method = route.attrs.get("method", "")
        if method != "POST":
            continue
        full_path = route.attrs.get("full_path") or route.attrs.get("path", "")
        if not _AUTH_PATH_RE.search(full_path):
            continue

        # It's a POST auth endpoint — inspect the function body
        func_name = route.attrs.get("func", "")
        file_rel = route.file
        source = source_map.get(file_rel, "")
        if not source:
            continue

        try:
            tree = ast.parse(source)
        except Exception:
            continue

        func_body = None
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
                func_body = ast.unparse(node)
                break

        if func_body is None:
            continue

        # Only flag if the function issues a token AND does NOT check a second factor
        issues_token = bool(_TOKEN_ISSUE_RE.search(func_body))
        checks_mfa = bool(_MFA_CHECK_RE.search(func_body))

        if issues_token and not checks_mfa:
            failures.append(
                Failure(
                    file=route.file,
                    line=route.line,
                    rule="G-MFA-LOGIN",
                    clause=_MFA_LOGIN_CLAUSE,
                    message=(
                        f"Route {method} {full_path} issues a token/session "
                        f"without verifying a second factor (314.4(c)(5))"
                    ),
                )
            )

    return failures


# ──────────────────────────────────────────────────────────────────────────────
# G-LOG check
# ──────────────────────────────────────────────────────────────────────────────

_LOG_CLAUSE = "16 CFR 314.4(c)(3)"


def _check_log(g: Graph, data_classes: dict[str, list[str]]) -> list[Failure]:
    """Flag log calls where at least one argument carries sensitive/body data.

    A log call whose arguments are only method, path, status or duration is NOT a finding.
    """
    failures: list[Failure] = []
    for node in g.nodes.values():
        if node.kind != "log_call":
            continue
        args = node.attrs.get("args", [])
        sensitive_args = [
            arg for arg in args
            if _arg_carries_body(arg, data_classes)
        ]
        if sensitive_args:
            failures.append(
                Failure(
                    file=node.file,
                    line=node.line,
                    rule="G-LOG",
                    clause=_LOG_CLAUSE,
                    message=(
                        f"Log call passes sensitive argument: {sensitive_args[0]}"
                    ),
                )
            )
    return failures


# ──────────────────────────────────────────────────────────────────────────────
# G-EXPORT check
# ──────────────────────────────────────────────────────────────────────────────

_EXPORT_CLAUSE = "16 CFR 314.4(c)(3) and 314.4(c)(6)(i)"


def _check_export(g: Graph, data_classes: dict[str, list[str]]) -> list[Failure]:
    """Flag jobs that write a government_id-class column to a file."""
    failures: list[Failure] = []

    # Collect all government_id column IDs (including view aliases)
    gov_id_cols: set[str] = set()
    for n in g.nodes.values():
        if n.kind in ("column", "view_column") and "government_id" in n.attrs.get("data_classes", []):
            gov_id_cols.add(n.id)

    # Propagate through aliases
    changed = True
    while changed:
        changed = False
        for e in g.edges:
            if e.kind == "aliases":
                if e.src in gov_id_cols and e.dst not in gov_id_cols:
                    gov_id_cols.add(e.dst)
                    changed = True
                if e.dst in gov_id_cols and e.src not in gov_id_cols:
                    gov_id_cols.add(e.src)
                    changed = True

    # For each job, check if it reads a view that contains a gov_id col, and writes a file
    job_nodes = [n for n in g.nodes.values() if n.kind == "job"]
    for job in job_nodes:
        # Views this job reads
        reads_views: set[str] = set()
        for e in g.edges:
            if e.kind == "job_reads" and e.src == job.id:
                reads_views.add(e.dst)

        # Check if any view contains a gov_id column
        gov_id_via_view = False
        for view_id in reads_views:
            view_name = g.nodes[view_id].attrs.get("view_name", "") if view_id in g.nodes else ""
            for n in g.nodes.values():
                if n.kind == "view_column" and n.attrs.get("view") == view_name:
                    if n.id in gov_id_cols:
                        gov_id_via_view = True
                        break
            if gov_id_via_view:
                break

        # Also check direct SQL query strings for column mentions
        if not gov_id_via_view:
            for e in g.edges:
                if e.kind == "log_arg" and e.src.startswith(f"log_call:{job.file}"):
                    arg = e.dst
                    for patterns in data_classes.values() if data_classes else []:
                        for pat in patterns:
                            if pat in arg.lower():
                                gov_id_via_view = True
                                break

        if not gov_id_via_view:
            continue

        # Check if this job writes any files
        for e in g.edges:
            if e.kind == "job_writes" and e.src == job.id:
                fw_node = g.nodes.get(e.dst)
                if fw_node and fw_node.kind == "file_write":
                    failures.append(
                        Failure(
                            file=job.file,
                            line=fw_node.line,
                            rule="G-EXPORT",
                            clause=_EXPORT_CLAUSE,
                            message=(
                                f"Job {job.attrs.get('name','?')} writes government_id-class column "
                                f"(after alias trace) to file at line {fw_node.line}"
                            ),
                        )
                    )

    return failures


# ──────────────────────────────────────────────────────────────────────────────
# Main entry point
# ──────────────────────────────────────────────────────────────────────────────


def run_guard(repo_root: Path | str, cfg: dict | None = None) -> list[Failure]:
    """Build the code graph and run all guard checks. Returns list of Failure."""
    repo_root = Path(repo_root)
    if cfg is None:
        cfg = _load_amend_yaml(repo_root)

    data_classes = _load_data_classes(cfg.get("data_classes"))
    mfa_dep = cfg.get("mfa_dependency", "require_mfa")

    g = build_graph(repo_root, cfg)

    failures: list[Failure] = []
    failures.extend(_check_mfa(g, mfa_dep))
    failures.extend(_check_mfa_login(g, repo_root))
    failures.extend(_check_log(g, data_classes))
    failures.extend(_check_export(g, data_classes))
    return failures


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────


def cmd_guard(args) -> None:
    import json

    repo_root = Path(args.repo)
    json_out = getattr(args, "json", None)

    failures = run_guard(repo_root)

    for f in failures:
        print(str(f))

    if json_out:
        out_path = Path(json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w") as fh:
            json.dump([f.to_dict() for f in failures], fh, indent=2)

    if failures:
        sys.exit(1)
    else:
        print("guard: no failures.")
