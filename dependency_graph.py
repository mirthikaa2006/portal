"""Dependency-Graph Extraction Pipeline.

Extracts module-level dependencies using Python AST analysis and static manifests,
inverts import declarations into source -> dependents mappings, deduplicates edges,
tracks artifact hashes for staleness detection, and writes to dependencies.json format.
"""

from __future__ import annotations

import ast
import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Set, Tuple

from pydantic import BaseModel, Field

from impact_selector.models import DependencyEdge

logger = logging.getLogger("impact_selector.dependency_graph")


class RawImportDeclaration(BaseModel):
    """Raw import statement extracted from source AST."""

    importer_file: str
    imported_module: str
    level: int = 0  # 0 = absolute, 1 = relative same dir, 2 = parent dir, etc.
    line_number: int = 0


class SourceFileInput(BaseModel):
    """Input specification for source file to be parsed."""

    path: str
    content: Optional[str] = None
    language: str = "python"
    sha256_hash: Optional[str] = None


class StalenessReport(BaseModel):
    """Result of checking dependency graph freshness against current disk state."""

    is_stale: bool
    stale_edges_count: int
    missing_source_files: list[str] = Field(default_factory=list)
    missing_dependent_files: list[str] = Field(default_factory=list)
    modified_files: list[str] = Field(default_factory=list)
    details: list[str] = Field(default_factory=list)


class DependencyGraphMetadata(BaseModel):
    """Envelope metadata for versioned dependency graph snapshots."""

    version: str = "1.0.0"
    extracted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    file_hashes: dict[str, str] = Field(default_factory=dict)
    edge_count: int = 0


def compute_content_sha256(content: str | bytes) -> str:
    """Calculate SHA256 hex digest for content."""
    if isinstance(content, str):
        content = content.encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def compute_file_sha256(file_path: Path | str) -> Optional[str]:
    """Calculate SHA256 hex digest for a file on disk."""
    path = Path(file_path)
    if not path.is_file():
        return None
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        logger.warning(f"Failed to read file {file_path} for hashing: {exc}")
        return None


def extract_python_ast_imports(importer_path: str, code: str) -> list[RawImportDeclaration]:
    """Parse Python source code via AST and extract all import statements.

    Handles:
    - import foo
    - import foo.bar as baz
    - from foo import bar
    - from .foo import bar
    - from ..foo import bar
    """
    declarations: list[RawImportDeclaration] = []
    try:
        tree = ast.parse(code, filename=importer_path)
    except SyntaxError as exc:
        logger.warning(f"Syntax error parsing {importer_path}: {exc}")
        return declarations

    normalized_importer = importer_path.replace("\\", "/")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                declarations.append(
                    RawImportDeclaration(
                        importer_file=normalized_importer,
                        imported_module=alias.name,
                        level=0,
                        line_number=node.lineno,
                    )
                )
        elif isinstance(node, ast.ImportFrom):
            module_name = node.module or ""
            declarations.append(
                RawImportDeclaration(
                    importer_file=normalized_importer,
                    imported_module=module_name,
                    level=node.level,
                    line_number=node.lineno,
                )
            )

    return declarations


def resolve_import_to_path(
    importer_file: str,
    imported_module: str,
    level: int = 0,
    known_files: Optional[Set[str]] = None,
) -> Optional[str]:
    """Resolve an import declaration to a normalized repository-relative file path."""
    importer_pure = PurePosixPath(importer_file.replace("\\", "/"))

    # Convert dot-delimited module to path components
    module_parts = [p for p in imported_module.split(".") if p]

    resolved_candidates: list[str] = []

    if level > 0:
        # Relative import: resolve relative to importer's containing directory
        parent = importer_pure.parent
        # In Python: level 1 = current dir, level 2 = parent dir, etc.
        for _ in range(level - 1):
            parent = parent.parent
        base_dir = parent
        if module_parts:
            target_base = base_dir.joinpath(*module_parts)
            resolved_candidates.append(f"{target_base}.py")
            resolved_candidates.append(f"{target_base}/__init__.py")
        else:
            resolved_candidates.append(f"{base_dir}/__init__.py")
    else:
        # Absolute import
        if module_parts:
            joined = "/".join(module_parts)
            resolved_candidates.append(f"{joined}.py")
            resolved_candidates.append(f"{joined}/__init__.py")

    # Match against known_files if provided
    if known_files:
        for candidate in resolved_candidates:
            if candidate in known_files:
                return candidate

    return resolved_candidates[0] if resolved_candidates else None


def build_dependency_graph(
    source_files: list[SourceFileInput],
    static_dependencies: Optional[dict[str, list[str]]] = None,
    repo_root: Optional[Path] = None,
) -> tuple[list[DependencyEdge], DependencyGraphMetadata]:
    """Extract and invert dependency relationships into source -> dependents mappings.

    If file B imports file A, then when A changes, B is affected.
    Therefore, A is the `source` and B is in `dependents` of A.
    """
    known_files = {f.path.replace("\\", "/") for f in source_files}
    file_hashes: dict[str, str] = {}

    # Map: source -> set of dependent files
    dependents_map: dict[str, set[str]] = {}

    for src_input in source_files:
        normalized_path = src_input.path.replace("\\", "/")

        # Resolve content
        content = src_input.content
        if content is None and repo_root is not None:
            disk_path = repo_root / normalized_path
            if disk_path.is_file():
                try:
                    content = disk_path.read_text(encoding="utf-8")
                except OSError as exc:
                    logger.warning(f"Could not read {disk_path}: {exc}")

        # Compute hash
        if src_input.sha256_hash:
            file_hashes[normalized_path] = src_input.sha256_hash
        elif content is not None:
            file_hashes[normalized_path] = compute_content_sha256(content)

        # Parse python files
        if (
            src_input.language.lower() == "python"
            or normalized_path.endswith(".py")
        ) and content is not None:
            raw_imports = extract_python_ast_imports(normalized_path, content)
            for imp in raw_imports:
                resolved = resolve_import_to_path(
                    importer_file=normalized_path,
                    imported_module=imp.imported_module,
                    level=imp.level,
                    known_files=known_files,
                )
                if resolved and resolved in known_files and resolved != normalized_path:
                    # 'resolved' is the imported source; 'normalized_path' depends on it
                    dependents_map.setdefault(resolved, set()).add(normalized_path)

    # Merge static dependency mappings (e.g. config/app.yaml -> src/auth/session.py)
    if static_dependencies:
        for source_file, dep_list in static_dependencies.items():
            norm_source = source_file.replace("\\", "/")
            for dep in dep_list:
                norm_dep = dep.replace("\\", "/")
                if norm_dep != norm_source:
                    dependents_map.setdefault(norm_source, set()).add(norm_dep)

    # Convert to sorted list of DependencyEdge objects
    edge_list: list[DependencyEdge] = []
    for source in sorted(dependents_map.keys()):
        deps = sorted(dependents_map[source])
        if deps:
            edge_list.append(DependencyEdge(source=source, dependents=deps))

    metadata = DependencyGraphMetadata(
        version="1.0.0",
        extracted_at=datetime.now(timezone.utc),
        file_hashes=file_hashes,
        edge_count=len(edge_list),
    )

    return edge_list, metadata


def check_graph_staleness(
    graph: list[DependencyEdge],
    snapshot_hashes: dict[str, str],
    repo_root: Optional[Path] = None,
) -> StalenessReport:
    """Validate dependency graph against current disk state or file hashes.

    Detects:
    - Files referenced in graph that no longer exist on disk (deleted/refactored)
    - Files whose hash has changed since graph generation
    """
    missing_sources: set[str] = set()
    missing_dependents: set[str] = set()
    modified_files: set[str] = set()
    details: list[str] = []

    for edge in graph:
        # Check source
        if repo_root is not None:
            src_path = repo_root / edge.source
            if not src_path.exists():
                missing_sources.add(edge.source)
                details.append(f"Source file deleted or moved: {edge.source}")
            else:
                curr_hash = compute_file_sha256(src_path)
                expected_hash = snapshot_hashes.get(edge.source)
                if expected_hash and curr_hash and curr_hash != expected_hash:
                    modified_files.add(edge.source)
                    details.append(f"Source file modified since graph creation: {edge.source}")

        # Check dependents
        for dep in edge.dependents:
            if repo_root is not None:
                dep_path = repo_root / dep
                if not dep_path.exists():
                    missing_dependents.add(dep)
                    details.append(f"Dependent file deleted or moved: {dep}")
                else:
                    curr_hash = compute_file_sha256(dep_path)
                    expected_hash = snapshot_hashes.get(dep)
                    if expected_hash and curr_hash and curr_hash != expected_hash:
                        modified_files.add(dep)

    is_stale = bool(missing_sources or missing_dependents or modified_files)
    stale_count = len(missing_sources) + len(missing_dependents) + len(modified_files)

    return StalenessReport(
        is_stale=is_stale,
        stale_edges_count=stale_count,
        missing_source_files=sorted(missing_sources),
        missing_dependent_files=sorted(missing_dependents),
        modified_files=sorted(modified_files),
        details=details,
    )


def write_dependencies_json(
    edges: list[DependencyEdge],
    output_path: Path | str,
) -> None:
    """Serialize dependency graph to standard dependencies.json format."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [edge.model_dump() for edge in edges]
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    logger.info(f"Wrote {len(edges)} dependency edges to {output_path}")
