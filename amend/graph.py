"""amend/graph.py — deterministic code graph for FastAPI + SQLAlchemy repos.

Builds a JSON graph of nodes (routes, models, columns, SQL views, jobs, log
calls, file writes, functions) and edges (include_router, mount, dependency,
column reads/writes, view lineage, job→view, job→file_write, log→arg).

Sensitive data classes are loaded from amend/data_classes.yaml and propagate
through SQL view column aliases (via sqlglot lineage).

Public API
----------
build(repo_root) -> Graph
Graph.to_dict() -> dict
Graph.routes_missing_dependency(dep_name) -> list[Node]
Graph.sinks_of(data_class) -> list[Node]
Graph.trace(data_class) -> list[list[str]]
"""
from __future__ import annotations

import ast
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# sqlglot is optional for view lineage; degrade gracefully
try:
    import sqlglot
    import sqlglot.lineage as _lineage

    _SQLGLOT_OK = True
except ImportError:  # pragma: no cover
    _SQLGLOT_OK = False

_HERE = Path(__file__).parent
_DATA_CLASSES_YAML = _HERE / "data_classes.yaml"


# ──────────────────────────────────────────────────────────────────────────────
# Configuration loading
# ──────────────────────────────────────────────────────────────────────────────


def _load_amend_yaml(repo_root: Path) -> dict:
    """Load compliance/amend.yaml from the target repo when present."""
    cfg_path = repo_root / "compliance" / "amend.yaml"
    if cfg_path.exists():
        with open(cfg_path) as fh:
            return yaml.safe_load(fh) or {}
    return {}


def _load_data_classes(override: dict | None = None) -> dict[str, list[str]]:
    """Return {class_name: [pattern, ...]} from data_classes.yaml."""
    with open(_DATA_CLASSES_YAML) as fh:
        raw = yaml.safe_load(fh)
    result: dict[str, list[str]] = {}
    for cls_name, spec in raw.get("data_classes", {}).items():
        result[cls_name] = [p.lower() for p in spec.get("patterns", [])]
    if override:
        for cls_name, patterns in override.items():
            result[cls_name] = [p.lower() for p in patterns]
    return result


# ──────────────────────────────────────────────────────────────────────────────
# Node / Edge data classes
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class Node:
    id: str
    kind: str  # route|mount|router|model|column|sql_view|view_column|job|log_call|file_write|function
    file: str
    line: int
    attrs: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {"id": self.id, "kind": self.kind, "file": self.file, "line": self.line}
        d.update(self.attrs)
        return d


@dataclass
class Edge:
    src: str
    dst: str
    kind: str  # include_router|mount|dependency|reads|writes|aliases|job_reads|job_writes|log_arg

    def to_dict(self) -> dict:
        return {"src": self.src, "dst": self.dst, "kind": self.kind}


# ──────────────────────────────────────────────────────────────────────────────
# Graph container
# ──────────────────────────────────────────────────────────────────────────────


class Graph:
    def __init__(self) -> None:
        self.nodes: dict[str, Node] = {}
        self.edges: list[Edge] = []

    def add_node(self, node: Node) -> Node:
        if node.id not in self.nodes:
            self.nodes[node.id] = node
        return self.nodes[node.id]

    def add_edge(self, src: str, dst: str, kind: str) -> None:
        e = Edge(src, dst, kind)
        if e not in self.edges:
            self.edges.append(e)

    def to_dict(self) -> dict:
        return {
            "nodes": [n.to_dict() for n in self.nodes.values()],
            "edges": [e.to_dict() for e in self.edges],
        }

    # ── Queries ───────────────────────────────────────────────────────────────

    def routes_missing_dependency(self, dep_name: str) -> list[Node]:
        """Return route nodes that expose data but lack *dep_name* in their dependency chain.

        Includes:
        - routes attached to a router that does NOT declare dep_name (transitively)
        - routes under a mounted sub-app (mounts bypass router-level deps)

        Excludes:
        - routes whose router was never included or mounted (unreachable)
        """
        result: list[Node] = []

        # Collect all deps (direct + inherited via post-processing) per route
        route_deps: dict[str, set[str]] = {}
        for e in self.edges:
            if e.kind == "dependency" and e.src in self.nodes:
                src = self.nodes[e.src]
                if src.kind == "route":
                    route_deps.setdefault(e.src, set()).add(e.dst)

        # Routes under a mount are always flagged (mount bypasses router deps)
        mounted_route_ids: set[str] = {
            n.id for n in self.nodes.values()
            if n.kind == "route" and n.attrs.get("mounted")
        }

        for node in self.nodes.values():
            if node.kind != "route":
                continue
            # Skip routes in routers that are never included/mounted
            if node.attrs.get("unreachable"):
                continue

            # Check if route itself (or inherited dep) declares the dep
            own_deps = route_deps.get(node.id, set())
            if dep_name in own_deps:
                continue

            # Check if route is mounted (mounted routes bypass router deps)
            if node.id in mounted_route_ids:
                result.append(node)
                continue

            # Route has no dep_name — only flag if it accesses data
            # (skip /health-style pure ops routes)
            if node.attrs.get("accesses_data", False):
                result.append(node)

        return result

    def sinks_of(self, data_class: str) -> list[Node]:
        """Return log_call, file_write nodes that carry data_class columns."""
        # Collect all column/view_column IDs for this data_class (bidirectional alias expansion)
        col_ids = {
            n.id
            for n in self.nodes.values()
            if n.kind in ("column", "view_column")
            and data_class in n.attrs.get("data_classes", [])
        }
        # Expand through aliases bidirectionally
        changed = True
        while changed:
            changed = False
            for e in self.edges:
                if e.kind == "aliases":
                    if e.dst in col_ids and e.src not in col_ids:
                        col_ids.add(e.src)
                        changed = True
                    if e.src in col_ids and e.dst not in col_ids:
                        col_ids.add(e.dst)
                        changed = True

        # Also collect sql_view IDs whose view_columns are in col_ids
        # (so we can trace job_reads sql_view -> file_write)
        relevant_view_ids: set[str] = set()
        for n in self.nodes.values():
            if n.kind == "view_column" and n.id in col_ids:
                view_name = n.attrs.get("view", "")
                view_id = f"sql_view:{view_name}"
                if view_id in self.nodes:
                    relevant_view_ids.add(view_id)

        result: list[Node] = []
        seen: set[str] = set()

        # Direct: log_arg / writes edges to a sensitive col
        for e in self.edges:
            if e.kind in ("log_arg", "writes") and e.dst in col_ids:
                src = self.nodes.get(e.src)
                if src and src.kind in ("log_call", "file_write") and src.id not in seen:
                    result.append(src)
                    seen.add(src.id)

        # Job path: job --job_reads--> sql_view (containing gov_id col)
        #                --job_writes--> file_write
        for job in self.nodes.values():
            if job.kind != "job":
                continue
            reads_relevant_view = any(
                e.kind == "job_reads" and e.src == job.id and e.dst in relevant_view_ids
                for e in self.edges
            )
            if reads_relevant_view:
                for e in self.edges:
                    if e.kind == "job_writes" and e.src == job.id:
                        fw = self.nodes.get(e.dst)
                        if fw and fw.kind == "file_write" and fw.id not in seen:
                            result.append(fw)
                            seen.add(fw.id)
        return result

    def trace(self, data_class: str) -> list[list[str]]:
        """Return paths from source columns through views/jobs to sinks for data_class.

        Path structure: column -> view_column -> job -> file_write
        e.g.: applications.ssn -> v_uw_export.tin -> job:jobs/export_underwriting.py -> file_write:...
        """
        # Seeds: base table columns with this data_class
        seeds = [
            n.id
            for n in self.nodes.values()
            if n.kind == "column" and data_class in n.attrs.get("data_classes", [])
        ]

        paths: list[list[str]] = []

        # For each seed column, find view_columns that alias it
        for seed_id in seeds:
            # Step 1: find view_columns that alias this column (alias edge: vc -> col)
            aliasing_vcs = [
                e.src for e in self.edges
                if e.kind == "aliases" and e.dst == seed_id and e.src in self.nodes
                and self.nodes[e.src].kind == "view_column"
            ]
            for vc_id in aliasing_vcs:
                vc_node = self.nodes[vc_id]
                view_name = vc_node.attrs.get("view", "")
                view_id = f"sql_view:{view_name}"

                # Step 2: find jobs that read this view
                jobs_reading = [
                    e.src for e in self.edges
                    if e.kind == "job_reads" and e.dst == view_id and e.src in self.nodes
                ]
                for job_id in jobs_reading:
                    # Step 3: find file_writes for this job
                    fws = [
                        e.dst for e in self.edges
                        if e.kind == "job_writes" and e.src == job_id and e.dst in self.nodes
                    ]
                    if fws:
                        for fw_id in fws:
                            paths.append([seed_id, vc_id, job_id, fw_id])
                    else:
                        paths.append([seed_id, vc_id, job_id])

        return paths


# ──────────────────────────────────────────────────────────────────────────────
# AST visitor helpers
# ──────────────────────────────────────────────────────────────────────────────


def _rel(path: Path, repo_root: Path) -> str:
    try:
        return path.relative_to(repo_root).as_posix()
    except ValueError:
        return path.as_posix()


def _decorator_names(dec_list: list) -> list[str]:
    """Return flat string names for decorators."""
    names = []
    for d in dec_list:
        if isinstance(d, ast.Attribute):
            names.append(f"{ast.unparse(d.value)}.{d.attr}")
        elif isinstance(d, ast.Call):
            names.append(ast.unparse(d.func))
        elif isinstance(d, ast.Name):
            names.append(d.id)
        else:
            names.append(ast.unparse(d))
    return names


def _extract_string(node: ast.expr | None) -> str | None:
    """Extract a string literal value from an AST node."""
    if node is None:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        # f-string — best effort
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant):
                parts.append(str(v.value))
            else:
                parts.append("{...}")
        return "".join(parts)
    return None


def _call_func_name(call: ast.Call) -> str:
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return ast.unparse(call.func)


def _depends_name(node: ast.expr) -> str | None:
    """If node is Depends(something), return 'something'."""
    if isinstance(node, ast.Call):
        func = node.func
        func_name = func.attr if isinstance(func, ast.Attribute) else (func.id if isinstance(func, ast.Name) else None)
        if func_name == "Depends" and node.args:
            arg = node.args[0]
            if isinstance(arg, ast.Name):
                return arg.id
            if isinstance(arg, ast.Attribute):
                return arg.attr
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Column/data-class classification
# ──────────────────────────────────────────────────────────────────────────────


def _classify_column(col_name: str, data_classes: dict[str, list[str]]) -> list[str]:
    """Return list of data-class names that match col_name."""
    result = []
    col_lower = col_name.lower()
    for cls, patterns in data_classes.items():
        for pat in patterns:
            if pat in col_lower:
                result.append(cls)
                break
    return result


# ──────────────────────────────────────────────────────────────────────────────
# SQLAlchemy model parser
# ──────────────────────────────────────────────────────────────────────────────


class _ModelVisitor(ast.NodeVisitor):
    """Collect SQLAlchemy model classes and their column definitions."""

    def __init__(self, file_rel: str, data_classes: dict[str, list[str]]) -> None:
        self.file_rel = file_rel
        self.data_classes = data_classes
        self.models: list[tuple[str, int, list[tuple[str, int]]]] = []
        # [(model_name, line, [(col_name, line), ...]), ...]

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        # Heuristic: class has __tablename__ attribute
        tablename = None
        columns: list[tuple[str, int]] = []
        for item in node.body:
            if isinstance(item, ast.Assign):
                for t in item.targets:
                    if isinstance(t, ast.Name) and t.id == "__tablename__":
                        if isinstance(item.value, ast.Constant):
                            tablename = item.value.value
            if isinstance(item, ast.AnnAssign) or isinstance(item, ast.Assign):
                col_names: list[str] = []
                if isinstance(item, ast.Assign):
                    for t in item.targets:
                        if isinstance(t, ast.Name):
                            col_names.append(t.id)
                elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    col_names.append(item.target.id)
                # Check if value is Column(...)
                value = item.value if isinstance(item, ast.Assign) else item.value
                if value and isinstance(value, ast.Call):
                    func_name = (
                        value.func.id
                        if isinstance(value.func, ast.Name)
                        else (value.func.attr if isinstance(value.func, ast.Attribute) else "")
                    )
                    if func_name == "Column":
                        for cn in col_names:
                            columns.append((cn, item.lineno))
        if tablename:
            self.models.append((tablename, node.lineno, columns))
        self.generic_visit(node)


# ──────────────────────────────────────────────────────────────────────────────
# FastAPI router/route parser
# ──────────────────────────────────────────────────────────────────────────────


class _RouterVisitor(ast.NodeVisitor):
    """Collect APIRouter declarations, route decorators, include_router calls."""

    def __init__(self, file_rel: str) -> None:
        self.file_rel = file_rel
        self.routers: list[dict] = []  # {name, prefix, line, deps}
        self.routes: list[dict] = []  # {func, method, path, line, deps, router_var}
        self.include_router_calls: list[dict] = []  # {caller, router_var, prefix, deps, line}
        self.mounts: list[dict] = []  # {app_var, path, sub_app, line}
        self._current_router: str | None = None
        # import_aliases: local_name -> (module_path, original_name)
        # e.g. "applications_router" -> ("app.routers.applications", "router")
        self.import_aliases: dict[str, tuple[str, str]] = {}

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        module = node.module or ""
        for alias in node.names:
            original = alias.name
            local = alias.asname if alias.asname else alias.name
            self.import_aliases[local] = (module, original)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:  # noqa: N802
        # Detect: router = APIRouter(...) or app = FastAPI(...)
        if isinstance(node.value, ast.Call):
            func = node.value.func
            func_name = func.attr if isinstance(func, ast.Attribute) else (func.id if isinstance(func, ast.Name) else "")
            if func_name == "APIRouter":
                prefix = ""
                deps: list[str] = []
                for kw in node.value.keywords:
                    if kw.arg == "prefix":
                        prefix = _extract_string(kw.value) or ""
                    if kw.arg == "dependencies":
                        deps = _extract_depends_list(kw.value)
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        self.routers.append(
                            {
                                "name": t.id,
                                "prefix": prefix,
                                "line": node.lineno,
                                "deps": deps,
                            }
                        )
        self.generic_visit(node)

    def visit_Expr(self, node: ast.Expr) -> None:  # noqa: N802
        if isinstance(node.value, ast.Call):
            call = node.value
            func = call.func
            if isinstance(func, ast.Attribute):
                method = func.attr
                obj = ast.unparse(func.value)
                if method in ("include_router",):
                    router_arg = ast.unparse(call.args[0]) if call.args else ""
                    prefix = ""
                    deps: list[str] = []
                    for kw in call.keywords:
                        if kw.arg == "prefix":
                            prefix = _extract_string(kw.value) or ""
                        if kw.arg == "dependencies":
                            deps = _extract_depends_list(kw.value)
                    self.include_router_calls.append(
                        {
                            "caller": obj,
                            "router_var": router_arg,
                            "prefix": prefix,
                            "deps": deps,
                            "line": node.lineno,
                        }
                    )
                elif method == "mount":
                    path_arg = _extract_string(call.args[0]) if call.args else ""
                    sub_app = ast.unparse(call.args[1]) if len(call.args) > 1 else ""
                    for kw in call.keywords:
                        if kw.arg == "app":
                            sub_app = ast.unparse(kw.value)
                    self.mounts.append(
                        {
                            "app_var": obj,
                            "path": path_arg or "",
                            "sub_app": sub_app,
                            "line": node.lineno,
                        }
                    )
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._visit_func(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._visit_func(node)

    def _visit_func(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        # Parameter-level dependencies, e.g. `user: dict = Depends(require_mfa)`,
        # protect a route exactly like decorator-level ones.
        defaults = list(node.args.defaults) + [d for d in node.args.kw_defaults if d is not None]
        param_deps = [d for d in (_depends_name(v) for v in defaults) if d]
        for dec in node.decorator_list:
            method, path, router_var, deps = _parse_route_decorator(dec)
            if method:
                deps = deps + [d for d in param_deps if d not in deps]
                self.routes.append(
                    {
                        "func": node.name,
                        "method": method,
                        "path": path or "",
                        "line": node.lineno,
                        "deps": deps,
                        "router_var": router_var,
                    }
                )
        self.generic_visit(node)


def _extract_depends_list(node: ast.expr) -> list[str]:
    """Extract dependency function names from a [Depends(x), ...] list."""
    deps: list[str] = []
    if isinstance(node, ast.List):
        for elt in node.elts:
            d = _depends_name(elt)
            if d:
                deps.append(d)
    return deps


def _parse_route_decorator(dec: ast.expr) -> tuple[str | None, str | None, str | None, list[str]]:
    """Parse @router.get('/path', dependencies=[...]) → (method, path, router_var, deps)."""
    if isinstance(dec, ast.Call):
        func = dec.func
        if isinstance(func, ast.Attribute):
            method_name = func.attr
            if method_name in ("get", "post", "put", "patch", "delete", "head", "options", "route"):
                router_var = ast.unparse(func.value)
                path = _extract_string(dec.args[0]) if dec.args else None
                if path is None:
                    for kw in dec.keywords:
                        if kw.arg == "path":
                            path = _extract_string(kw.value)
                deps: list[str] = []
                for kw in dec.keywords:
                    if kw.arg == "dependencies":
                        deps = _extract_depends_list(kw.value)
                # Also check function parameters for Depends
                return method_name.upper(), path, router_var, deps
    return None, None, None, []


# ──────────────────────────────────────────────────────────────────────────────
# Log/file-write visitor
# ──────────────────────────────────────────────────────────────────────────────


class _SinkVisitor(ast.NodeVisitor):
    """Collect logger.* calls and open(..., 'w') / csv.writer calls."""

    def __init__(self, file_rel: str) -> None:
        self.file_rel = file_rel
        self.log_calls: list[dict] = []
        self.file_writes: list[dict] = []

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        func = node.func
        if isinstance(func, ast.Attribute):
            method = func.attr
            obj = ast.unparse(func.value)
            # Detect logger.info/debug/warning/error/critical
            if method in ("debug", "info", "warning", "error", "critical", "exception"):
                args_text = [ast.unparse(a) for a in node.args]
                self.log_calls.append(
                    {
                        "obj": obj,
                        "method": method,
                        "args": args_text,
                        "line": node.lineno,
                    }
                )
            # Detect csv.writer(fh).writerow / writerows
            if method in ("writerow", "writerows") and "csv" in ast.unparse(node).lower():
                self.file_writes.append(
                    {
                        "kind": "csv_write",
                        "method": method,
                        "line": node.lineno,
                    }
                )
        # Detect open(..., mode) for writing
        if isinstance(func, ast.Name) and func.id == "open":
            mode = ""
            if len(node.args) >= 2:
                mode = _extract_string(node.args[1]) or ""
            for kw in node.keywords:
                if kw.arg == "mode":
                    mode = _extract_string(kw.value) or mode
            if "w" in mode or "a" in mode:
                path_arg = _extract_string(node.args[0]) if node.args else None
                self.file_writes.append(
                    {
                        "kind": "file_open",
                        "path": path_arg or "?",
                        "line": node.lineno,
                    }
                )
        self.generic_visit(node)


# ──────────────────────────────────────────────────────────────────────────────
# Column reference visitor (reads/writes in functions)
# ──────────────────────────────────────────────────────────────────────────────


def _find_column_refs(tree: ast.AST, table_column_map: dict[str, list[str]]) -> list[tuple[str, str, int]]:
    """Return [(table, column, line), ...] for attribute accesses like Model.col."""
    refs = []
    # Build flat map: model_class_name -> tablename
    # We rely on the caller to pass a map of {ClassName: tablename}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            obj = node.value
            if isinstance(obj, ast.Name):
                cls_name = obj.id
                attr = node.attr
                for tablename, col_name in table_column_map.items():
                    if cls_name in col_name:  # col_name is list of (cls, cols)
                        pass
    return refs


# ──────────────────────────────────────────────────────────────────────────────
# SQL view parser (sqlglot lineage)
# ──────────────────────────────────────────────────────────────────────────────


def _parse_sql_views(sql_path: Path, data_classes: dict[str, list[str]]) -> tuple[list[dict], list[dict]]:
    """Parse SQL view definitions; return (views, aliases).

    views: [{name, columns: [col_name, ...], sql}]
    aliases: [{view, view_col, source_table, source_col}]
    """
    if not sql_path.exists():
        return [], []

    sql_text = sql_path.read_text()

    # Use sqlglot to parse CREATE VIEW statements
    views: list[dict] = []
    aliases: list[dict] = []

    if _SQLGLOT_OK:
        try:
            statements = sqlglot.parse(sql_text, dialect="sqlite")
        except Exception:
            statements = []
        for stmt in statements:
            if stmt is None:
                continue
            if stmt.__class__.__name__ == "Create":
                # Extract view name
                view_name = None
                try:
                    view_name = stmt.find(sqlglot.exp.Table).name
                except Exception:
                    pass
                if not view_name:
                    continue

                # Use sqlglot lineage to trace column origins
                view_cols: list[str] = []
                view_aliases: list[dict] = []
                try:
                    select = stmt.find(sqlglot.exp.Select)
                    if select:
                        for expr in select.expressions:
                            alias = expr.alias if hasattr(expr, "alias") and expr.alias else None
                            # Get the actual column name
                            if alias:
                                col_name = alias
                            elif isinstance(expr, sqlglot.exp.Column):
                                col_name = expr.name
                            else:
                                col_name = str(expr)
                            view_cols.append(col_name)

                            # Trace source column via lineage
                            try:
                                source_col = None
                                source_table = None
                                if isinstance(expr, sqlglot.exp.Alias):
                                    inner = expr.this
                                    if isinstance(inner, sqlglot.exp.Column):
                                        source_col = inner.name
                                        if inner.table:
                                            source_table = inner.table
                                elif isinstance(expr, sqlglot.exp.Column):
                                    source_col = expr.name
                                    if expr.table:
                                        source_table = expr.table
                                if source_col:
                                    view_aliases.append(
                                        {
                                            "view": view_name,
                                            "view_col": col_name,
                                            "source_table": source_table,
                                            "source_col": source_col,
                                        }
                                    )
                            except Exception:
                                pass
                except Exception:
                    pass

                views.append({"name": view_name, "columns": view_cols, "sql": str(stmt)})
                aliases.extend(view_aliases)
    else:
        # Fallback: regex-based simple parsing
        create_re = re.compile(
            r"CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s+AS\s+(.*?)(?:;|$)",
            re.IGNORECASE | re.DOTALL,
        )
        alias_re = re.compile(r"(\w+)\s+AS\s+(\w+)", re.IGNORECASE)
        col_re = re.compile(r"SELECT\s+(.*?)\s+FROM\s+(\w+)", re.IGNORECASE | re.DOTALL)
        for m in create_re.finditer(sql_text):
            view_name = m.group(1)
            body = m.group(2)
            cols = []
            view_als = []
            cm = col_re.search(body)
            if cm:
                col_part = cm.group(1)
                table = cm.group(2)
                for col_str in col_part.split(","):
                    col_str = col_str.strip()
                    am = alias_re.match(col_str)
                    if am:
                        src_col, view_col = am.group(1), am.group(2)
                        cols.append(view_col)
                        view_als.append(
                            {
                                "view": view_name,
                                "view_col": view_col,
                                "source_table": table,
                                "source_col": src_col,
                            }
                        )
                    else:
                        col_name = col_str.split(".")[-1]
                        cols.append(col_name)
                        view_als.append(
                            {
                                "view": view_name,
                                "view_col": col_name,
                                "source_table": table,
                                "source_col": col_name,
                            }
                        )
            views.append({"name": view_name, "columns": cols, "sql": body})
            aliases.extend(view_als)

    return views, aliases


# ──────────────────────────────────────────────────────────────────────────────
# Job file parser
# ──────────────────────────────────────────────────────────────────────────────


def _parse_job_file(
    path: Path,
    file_rel: str,
    data_classes: dict[str, list[str]],
) -> tuple[dict | None, list[dict], list[dict], list[dict]]:
    """Parse a job file; return (job_info, file_writes, log_calls, view_reads).

    view_reads: [{view_name, line}]
    """
    try:
        source = path.read_text()
        tree = ast.parse(source)
    except Exception:
        return None, [], [], []

    sink_v = _SinkVisitor(file_rel)
    sink_v.visit(tree)

    # Detect SQL view references: conn.execute("SELECT ... FROM view_name")
    view_reads: list[dict] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            method = func.attr if isinstance(func, ast.Attribute) else None
            if method in ("execute", "executescript") and node.args:
                sql_arg = _extract_string(node.args[0])
                if sql_arg:
                    # Look for FROM <identifier>
                    for m in re.finditer(r"\bFROM\s+(\w+)", sql_arg, re.IGNORECASE):
                        view_reads.append({"view_name": m.group(1), "line": node.lineno})

    job_info = {
        "file": file_rel,
        "line": 1,
    }
    return job_info, sink_v.file_writes, sink_v.log_calls, view_reads


# ──────────────────────────────────────────────────────────────────────────────
# Main graph builder
# ──────────────────────────────────────────────────────────────────────────────


def _find_python_files(root: Path) -> list[Path]:
    py_files = []
    for p in root.rglob("*.py"):
        parts = p.parts
        # Skip __pycache__, .venv, test fixtures
        if any(part in ("__pycache__", ".venv", "node_modules") for part in parts):
            continue
        py_files.append(p)
    return py_files


def build(repo_root: Path | str, cfg: dict | None = None) -> Graph:
    """Build the code graph for the repo at *repo_root*."""
    repo_root = Path(repo_root)
    if cfg is None:
        cfg = _load_amend_yaml(repo_root)

    data_classes = _load_data_classes(cfg.get("data_classes"))
    mfa_dep = cfg.get("mfa_dependency", "require_mfa")
    sink_folders = cfg.get("sink_folders", ["var/buckets", "var/log"])

    g = Graph()

    # ── Step 1: parse SQLAlchemy models ──────────────────────────────────────
    # table_name -> [(col_name, line)]
    table_columns: dict[str, list[tuple[str, int]]] = {}
    # class_name -> table_name
    class_to_table: dict[str, str] = {}
    model_files: dict[str, str] = {}  # table_name -> file_rel

    py_files = _find_python_files(repo_root)
    for py_path in py_files:
        file_rel = _rel(py_path, repo_root)
        try:
            source = py_path.read_text()
            tree = ast.parse(source)
        except Exception:
            continue
        mv = _ModelVisitor(file_rel, data_classes)
        mv.visit(tree)
        for tablename, model_line, columns in mv.models:
            node_id = f"model:{tablename}"
            g.add_node(Node(node_id, "model", file_rel, model_line, {"tablename": tablename}))
            table_columns[tablename] = columns
            model_files[tablename] = file_rel
            for col_name, col_line in columns:
                col_id = f"{tablename}.{col_name}"
                dc = _classify_column(col_name, data_classes)
                g.add_node(
                    Node(
                        col_id,
                        "column",
                        file_rel,
                        col_line,
                        {"table": tablename, "column": col_name, "data_classes": dc},
                    )
                )
        # Collect class->table mapping
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, ast.Assign):
                        for t in item.targets:
                            if isinstance(t, ast.Name) and t.id == "__tablename__":
                                if isinstance(item.value, ast.Constant):
                                    class_to_table[node.name] = item.value.value

    # ── Step 2: parse SQL views ───────────────────────────────────────────────
    sql_dir = repo_root / "sql"
    for sql_file in sql_dir.glob("*.sql") if sql_dir.exists() else []:
        sql_rel = _rel(sql_file, repo_root)
        views, aliases = _parse_sql_views(sql_file, data_classes)
        for v in views:
            view_id = f"sql_view:{v['name']}"
            g.add_node(Node(view_id, "sql_view", sql_rel, 1, {"view_name": v["name"], "columns": v["columns"]}))
            for col_name in v["columns"]:
                vc_id = f"{v['name']}.{col_name}"
                dc = _classify_column(col_name, data_classes)
                g.add_node(
                    Node(
                        vc_id,
                        "view_column",
                        sql_rel,
                        1,
                        {"view": v["name"], "column": col_name, "data_classes": dc},
                    )
                )
        for alias in aliases:
            # view_column -> source column alias edge
            vc_id = f"{alias['view']}.{alias['view_col']}"
            src_table = alias["source_table"] or ""
            src_col_id = f"{src_table}.{alias['source_col']}" if src_table else alias["source_col"]
            if src_col_id in g.nodes or _find_col_id(alias["source_col"], table_columns):
                if not src_table:
                    # Try to find which table this column comes from
                    for tbl, cols in table_columns.items():
                        for cn, _ in cols:
                            if cn == alias["source_col"]:
                                src_col_id = f"{tbl}.{cn}"
                                break
            if vc_id in g.nodes:
                g.add_edge(vc_id, src_col_id, "aliases")
                # Propagate data classes
                src_node = g.nodes.get(src_col_id)
                vc_node = g.nodes[vc_id]
                if src_node and src_node.attrs.get("data_classes"):
                    existing = vc_node.attrs.get("data_classes", [])
                    merged = list(set(existing) | set(src_node.attrs["data_classes"]))
                    vc_node.attrs["data_classes"] = merged

    # ── Step 3: parse router/route files ─────────────────────────────────────
    # First pass: collect all router variable names and their prefixes/deps
    # file_rel -> RouterVisitor
    router_visitors: dict[str, _RouterVisitor] = {}
    for py_path in py_files:
        file_rel = _rel(py_path, repo_root)
        try:
            source = py_path.read_text()
            tree = ast.parse(source)
        except Exception:
            continue
        rv = _RouterVisitor(file_rel)
        rv.visit(tree)
        router_visitors[file_rel] = rv

    # Build router nodes
    # router_key: file_rel::var_name -> Node
    router_node_map: dict[str, str] = {}  # file::varname -> node_id
    for file_rel, rv in router_visitors.items():
        for r in rv.routers:
            rid = f"router:{file_rel}::{r['name']}"
            g.add_node(Node(rid, "router", file_rel, r["line"], {"prefix": r["prefix"], "var": r["name"]}))
            router_node_map[f"{file_rel}::{r['name']}"] = rid
            for dep in r["deps"]:
                g.add_edge(rid, dep, "dependency")

    # Build route nodes and edges
    # inc_prefix_map: (caller_id, target_router_id) -> prefix added by include_router call
    # inc_deps_map:   (caller_id, target_router_id) -> deps added by include_router call
    inc_prefix_map: dict[tuple[str, str], str] = {}
    inc_deps_map: dict[tuple[str, str], list[str]] = {}

    for file_rel, rv in router_visitors.items():
        for route in rv.routes:
            func_name = route["func"]
            router_var = route["router_var"]
            rid_key = f"{file_rel}::{router_var}"
            router_node_id = router_node_map.get(rid_key)

            # Check if this route accesses sensitive data (heuristic: uses db session)
            accesses_data = _route_accesses_data(file_rel, func_name, py_files, repo_root, class_to_table)

            route_id = f"route:{file_rel}::{func_name}"
            attrs = {
                "method": route["method"],
                "path": route["path"],
                "func": func_name,
                "router_id": router_node_id or "",
                "accesses_data": accesses_data,
                "mounted": False,
            }
            g.add_node(Node(route_id, "route", file_rel, route["line"], attrs))

            if router_node_id:
                g.add_edge(router_node_id, route_id, "include_router")

            for dep in route["deps"]:
                g.add_edge(route_id, dep, "dependency")

    # Build include_router and mount edges
    # Also record extra prefix/deps contributed by each include_router call
    for file_rel, rv in router_visitors.items():
        for inc in rv.include_router_calls:
            caller = inc["caller"]
            router_var = inc["router_var"]
            # Resolve caller to a node
            caller_key = f"{file_rel}::{caller}"
            caller_node_id = router_node_map.get(caller_key)
            # Find target router node
            target_key = _resolve_import_router(router_var, file_rel, router_visitors, router_node_map)
            if not caller_node_id:
                # Might be the main app — create a synthetic app router node
                app_key = f"app:{file_rel}::{caller}"
                if app_key not in g.nodes:
                    g.add_node(Node(app_key, "router", file_rel, inc["line"], {"prefix": "", "var": caller}))
                caller_node_id = app_key

            if target_key:
                g.add_edge(caller_node_id, target_key, "include_router")
                # Record per-edge extra prefix and deps from this include_router call
                inc_prefix_map[(caller_node_id, target_key)] = inc.get("prefix", "")
                inc_deps_map[(caller_node_id, target_key)] = inc.get("deps", [])

        for mount in rv.mounts:
            sub_app = mount["sub_app"]
            mount_path = mount["path"]
            mount_id = f"mount:{file_rel}::{mount_path}"
            g.add_node(Node(mount_id, "mount", file_rel, mount["line"], {"prefix": mount_path, "sub_app": sub_app}))

            # Find all routes in the sub-app and mark them as mounted
            _mark_mounted_routes(sub_app, file_rel, router_visitors, router_node_map, g, mount_id)

    # ── Post-process: compute full paths and propagate deps ───────────────────
    # Build router→parent include edges (router_id → set of (parent_id, edge_prefix, edge_deps))
    # A router is "reachable" if it can be traced to an app-level node via include_router edges.
    # We walk from each route upward through include_router edges to accumulate prefix + deps.

    # Map: router_id → list of (caller_id, extra_prefix, extra_deps)
    router_parents: dict[str, list[tuple[str, str, list[str]]]] = {}
    for e in g.edges:
        if e.kind == "include_router":
            src = g.nodes.get(e.src)
            dst = g.nodes.get(e.dst)
            if src and dst and dst.kind in ("router", "route"):
                ep = inc_prefix_map.get((e.src, e.dst), "")
                ed = inc_deps_map.get((e.src, e.dst), [])
                router_parents.setdefault(e.dst, []).append((e.src, ep, ed))

    def _collect_prefixes_and_deps(
        node_id: str,
        visited: set[str],
    ) -> list[tuple[str, list[str]]]:
        """Return list of (accumulated_prefix, accumulated_deps) from all root paths."""
        if node_id in visited:
            return [("", [])]
        visited = visited | {node_id}
        parents = router_parents.get(node_id, [])
        if not parents:
            return [("", [])]
        results = []
        for parent_id, ep, ed in parents:
            parent_node = g.nodes.get(parent_id)
            if parent_node is None:
                continue
            parent_prefix = parent_node.attrs.get("prefix", "")
            parent_deps = [e.dst for e in g.edges if e.kind == "dependency" and e.src == parent_id]
            for ancestor_prefix, ancestor_deps in _collect_prefixes_and_deps(parent_id, visited):
                results.append((
                    ancestor_prefix + parent_prefix + ep,
                    ancestor_deps + parent_deps + ed,
                ))
        return results or [("", [])]

    # Determine reachable routers: a router is reachable if it is ever included by any
    # other node (has at least one entry in router_parents), OR if it is a root caller
    # (app-level synthetic node with no parents — it IS the app).
    # An unreachable router is one that is defined but never referenced by any include_router.
    all_router_ids = {n.id for n in g.nodes.values() if n.kind == "router"}

    # Root callers: synthetic "app:" nodes (FastAPI app instances, not APIRouter).
    # These are the top of the include chain and are always reachable.
    root_caller_ids: set[str] = {
        rid for rid in all_router_ids
        if rid.startswith("app:")
    }
    # A router is reachable if it is a root caller OR is reachable from a root caller
    # via the include chain. We compute this via BFS from root callers.
    reachable_router_ids: set[str] = set(root_caller_ids)
    worklist = list(root_caller_ids)
    # Build: parent_id -> set of child router_ids (routers it includes)
    parent_to_children: dict[str, set[str]] = {}
    for child_id, parents in router_parents.items():
        for parent_id, _ep, _ed in parents:
            parent_to_children.setdefault(parent_id, set()).add(child_id)
    while worklist:
        current = worklist.pop()
        for child in parent_to_children.get(current, set()):
            if child not in reachable_router_ids:
                reachable_router_ids.add(child)
                worklist.append(child)

    # Mark unreachable routers (defined but never included in the app)
    for n in g.nodes.values():
        if n.kind == "router" and n.id not in reachable_router_ids:
            n.attrs["unreachable"] = True

    # Update each route with its full path and all inherited deps
    for n in g.nodes.values():
        if n.kind != "route":
            continue
        if n.attrs.get("mounted"):
            continue  # mounted routes don't inherit main app's prefix/deps

        router_id = n.attrs.get("router_id", "")
        router_node = g.nodes.get(router_id)
        router_prefix = router_node.attrs.get("prefix", "") if router_node else ""
        router_deps = [e.dst for e in g.edges if e.kind == "dependency" and e.src == router_id]

        route_path = n.attrs.get("path", "")

        # Collect all ancestor prefix+dep chains for this router
        chains = _collect_prefixes_and_deps(router_id, set()) if router_id else [("", [])]

        # Use the first (or only) chain to set the canonical full_path
        # If there are multiple mounting points, take the one with the longest prefix
        chains.sort(key=lambda x: len(x[0]), reverse=True)
        ancestor_prefix, ancestor_deps = chains[0] if chains else ("", [])

        full_path = ancestor_prefix + router_prefix + route_path
        n.attrs["full_path"] = full_path

        # Mark router as reachable or not — if router is unreachable, don't propagate deps
        if router_id and g.nodes.get(router_id, Node("", "", "", 0)).attrs.get("unreachable"):
            n.attrs["unreachable"] = True
            continue

        # Propagate all ancestor deps to the route
        all_inherited_deps = set(ancestor_deps) | set(router_deps)
        for dep in all_inherited_deps:
            g.add_edge(n.id, dep, "dependency")

    # ── Step 4: parse job files ───────────────────────────────────────────────
    jobs_dir = repo_root / "jobs"
    if jobs_dir.exists():
        for job_path in jobs_dir.glob("*.py"):
            if job_path.name.startswith("__"):
                continue
            file_rel = _rel(job_path, repo_root)
            job_info, file_writes, log_calls, view_reads = _parse_job_file(job_path, file_rel, data_classes)
            if job_info is None:
                continue
            job_id = f"job:{file_rel}"
            job_name = job_path.stem
            g.add_node(Node(job_id, "job", file_rel, 1, {"name": job_name}))

            # View reads
            for vr in view_reads:
                view_node_id = f"sql_view:{vr['view_name']}"
                if view_node_id in g.nodes:
                    g.add_edge(job_id, view_node_id, "job_reads")

            # File writes
            for fw in file_writes:
                fw_id = f"file_write:{file_rel}:{fw['line']}"
                fw_path = fw.get("path", "?")
                g.add_node(
                    Node(
                        fw_id,
                        "file_write",
                        file_rel,
                        fw["line"],
                        {"path": fw_path, "kind": fw.get("kind", "?")},
                    )
                )
                g.add_edge(job_id, fw_id, "job_writes")

            # Log calls
            for lc in log_calls:
                lc_id = f"log_call:{file_rel}:{lc['line']}"
                g.add_node(
                    Node(
                        lc_id,
                        "log_call",
                        file_rel,
                        lc["line"],
                        {"method": lc["method"], "args": lc["args"]},
                    )
                )
                for arg_text in lc["args"]:
                    g.add_edge(lc_id, arg_text, "log_arg")

    # ── Step 5: parse log calls in app files ─────────────────────────────────
    for py_path in py_files:
        # Skip job files (already handled)
        file_rel = _rel(py_path, repo_root)
        if file_rel.startswith("jobs/"):
            continue
        try:
            source = py_path.read_text()
            tree = ast.parse(source)
        except Exception:
            continue
        sink_v = _SinkVisitor(file_rel)
        sink_v.visit(tree)
        for lc in sink_v.log_calls:
            lc_id = f"log_call:{file_rel}:{lc['line']}"
            g.add_node(
                Node(
                    lc_id,
                    "log_call",
                    file_rel,
                    lc["line"],
                    {"method": lc["method"], "args": lc["args"]},
                )
            )
            for arg_text in lc["args"]:
                g.add_edge(lc_id, arg_text, "log_arg")

    return g


def _find_col_id(col_name: str, table_columns: dict[str, list[tuple[str, int]]]) -> str | None:
    for tbl, cols in table_columns.items():
        for cn, _ in cols:
            if cn == col_name:
                return f"{tbl}.{cn}"
    return None


def _route_accesses_data(
    file_rel: str,
    func_name: str,
    py_files: list[Path],
    repo_root: Path,
    class_to_table: dict[str, str],
) -> bool:
    """Heuristic: return True if function body uses db session or returns sensitive fields."""
    for py_path in py_files:
        if _rel(py_path, repo_root) != file_rel:
            continue
        try:
            source = py_path.read_text()
            tree = ast.parse(source)
        except Exception:
            return False
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
                src = ast.unparse(node)
                # Rough heuristics
                if any(
                    kw in src
                    for kw in ("db.query", "Session", "get_db", ".ssn", ".bank_account", "customer_id", ".income")
                ):
                    return True
    return False


def _resolve_import_router(
    var_name: str,
    file_rel: str,
    router_visitors: dict[str, _RouterVisitor],
    router_node_map: dict[str, str],
) -> str | None:
    """Find the router node ID for a variable reference (possibly from an import).

    Handles:
    - Direct match in same file (e.g. api.include_router(mobile_router))
    - Import aliases: "from .routers.applications import router as applications_router"
      → resolves "applications_router" to the router node in app/routers/applications.py
    """
    # Direct match in same file
    key = f"{file_rel}::{var_name}"
    if key in router_node_map:
        return router_node_map[key]

    # Try to resolve via import alias in the calling file
    rv = router_visitors.get(file_rel)
    if rv and var_name in rv.import_aliases:
        module_path, original_name = rv.import_aliases[var_name]
        # Convert module path to file path candidates
        # e.g. "app.routers.applications" -> "app/routers/applications.py"
        # Also handle relative: ".routers.applications" -> strip leading dots
        module_clean = module_path.lstrip(".")
        candidate_rel = module_clean.replace(".", "/") + ".py"
        # Try direct key
        direct_key = f"{candidate_rel}::{original_name}"
        if direct_key in router_node_map:
            return router_node_map[direct_key]
        # Try suffix search in case the file lives in a subdirectory
        for frel, node_id in router_node_map.items():
            if frel.endswith(candidate_rel) and frel.endswith(f"::{original_name}"):
                return node_id
        # Also try just original_name in any file that ends with candidate_rel
        for k, node_id in router_node_map.items():
            parts = k.split("::")
            if len(parts) == 2 and parts[1] == original_name and parts[0].endswith(candidate_rel):
                return node_id

    # Fallback: search all files for a matching router var by exact name
    for frel_key, node_id in router_node_map.items():
        if frel_key.endswith(f"::{var_name}"):
            return node_id
    return None


def _mark_mounted_routes(
    sub_app_var: str,
    file_rel: str,
    router_visitors: dict[str, _RouterVisitor],
    router_node_map: dict[str, str],
    g: Graph,
    mount_id: str,
) -> None:
    """Find all routes belonging to *sub_app_var* and mark them as mounted."""
    # The sub_app is a FastAPI instance; find files where it includes routers
    rv = router_visitors.get(file_rel)
    if rv:
        for inc in rv.include_router_calls:
            if inc["caller"] == sub_app_var:
                # This is a sub-app include_router
                router_var = inc["router_var"]
                target_key = _resolve_import_router(router_var, file_rel, router_visitors, router_node_map)
                if target_key:
                    g.add_edge(mount_id, target_key, "include_router")
                    # Mark all routes under this router as mounted
                    for e in list(g.edges):
                        if e.src == target_key and e.kind == "include_router":
                            route_node = g.nodes.get(e.dst)
                            if route_node and route_node.kind == "route":
                                route_node.attrs["mounted"] = True

    # Also: find any file that defines an APIRouter included in sub_app_var
    # by checking import aliases in the file
    for other_frel, other_rv in router_visitors.items():
        if other_frel == file_rel:
            continue
        for inc in other_rv.include_router_calls:
            if inc["caller"] == sub_app_var:
                router_var = inc["router_var"]
                target_key = _resolve_import_router(router_var, other_frel, router_visitors, router_node_map)
                if target_key:
                    g.add_edge(mount_id, target_key, "include_router")
                    for e in list(g.edges):
                        if e.src == target_key and e.kind == "include_router":
                            route_node = g.nodes.get(e.dst)
                            if route_node and route_node.kind == "route":
                                route_node.attrs["mounted"] = True


# ──────────────────────────────────────────────────────────────────────────────
# CLI entry point (called from amend/cli.py)
# ──────────────────────────────────────────────────────────────────────────────


def cmd_graph(args) -> None:
    import sys

    repo_root = Path(args.repo)
    out_path = Path(args.out) if args.out else None

    g = build(repo_root)
    d = g.to_dict()

    node_count = len(d["nodes"])
    edge_count = len(d["edges"])
    print(f"Graph: {node_count} nodes, {edge_count} edges")

    # Query results
    missing_mfa = g.routes_missing_dependency("require_mfa")
    print(f"\nRoutes missing require_mfa ({len(missing_mfa)}):")
    for n in missing_mfa:
        print(f"  {n.file}:{n.line}  {n.attrs.get('method','?')} {n.attrs.get('path','?')}")

    dc_names = list(_load_data_classes().keys())
    for dc in dc_names:
        sinks = g.sinks_of(dc)
        paths = g.trace(dc)
        print(f"\nSinks of {dc} ({len(sinks)}):")
        for s in sinks:
            print(f"  {s.file}:{s.line}  {s.kind}")
        print(f"Traces for {dc} ({len(paths)}):")
        for p in paths:
            print("  " + " -> ".join(p))

    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w") as fh:
            json.dump(d, fh, indent=2)
        print(f"\nGraph written to {out_path}")
