"""Workflow catalog → MCP tool registrar.

The general mechanism for exposing pre-made nanobrain workflows as MCP
tools that any MCP client (Claude Desktop, etc.) can invoke. This is
distinct from ``discovery.py``'s ``list_workflows`` / ``describe_workflow``
tools: those read the **composer catalog** (buildable component
manifests). The catalog here is a registry of **pre-built, runnable
nanobrain workflows** exposed one-per-MCP-tool.

How it works
------------
1. ``load_catalog(path)`` parses + validates a YAML catalog into a
   ``WorkflowCatalog`` (Pydantic, ``extra='forbid'``).
2. ``register_workflows(server, catalog)`` walks the catalog and, for
   each entry, registers ONE MCP tool on the FastMCP server. Each tool
   has its catalog-declared ``tool_name``, ``description``, and
   ``input_schema`` exposed in ``tools/list``.
3. On invocation, the per-entry runner loads the Workflow (lazy +
   cached per-entry), calls ``workflow.run(input_data, timeout=...)``,
   and returns the workflow outputs (or ``{"error": ...}`` on any
   failure shape).

FastMCP API constraint (STEP 0 finding)
---------------------------------------
``FastMCP.tool()`` accepts ``name=`` and ``description=`` but the input
schema is derived from the function signature via
``func_metadata(fn).arg_model.model_json_schema()`` (see
``mcp/server/fastmcp/tools/base.py:Tool.from_function``). There is no
``parameters=`` / ``inputSchema=`` argument. To match a catalog's
declared ``input_schema``, we synthesize a per-entry async function via
``exec()`` whose signature mirrors the schema's ``properties`` (name,
type, default). FastMCP then generates the matching tools/list schema.

FAIL-LOUD discipline
--------------------
- Catalog parse / validation error → ``ValueError`` from ``load_catalog``
  (the registry never silently presents an empty tool surface).
- Per-entry import / synthesis error → logged ERROR, entry added to
  ``RegistrationReport.failed``, OTHER entries still register (a single
  malformed entry must not break the whole catalog).
- Per-entry prerequisites unmet at registration → tool IS still
  registered with ``[UNAVAILABLE: …]`` suffix; its body returns an
  actionable ``{"error": ...}`` on call. Silent absence from
  ``tools/list`` is forbidden — the operator finds the misconfiguration
  by seeing the marker, not by wondering why the tool disappeared.
- Per-entry runtime failure → ``{"error": "<type>: <msg>"}`` (MCP
  transport stays clean; the body carries the failure).
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from textwrap import dedent
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic catalog schema (extra='forbid' per workspace rule)
# ---------------------------------------------------------------------------


class WorkflowSourceYAML(BaseModel):
    """A workflow loaded via ``Workflow.from_config(path)``."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["yaml"]
    path: str
    """YAML path. Relative paths resolve against the apecx_integration
    package root (the directory containing ``mcp_surface/``)."""


class WorkflowSourceLightweight(BaseModel):
    """A workflow loaded via a Python callable that returns a Workflow.

    The callable is invoked once per cache-miss (typically at first MCP
    tool call). Conventionally it builds a
    ``nanobrain.lightweight.WorkflowBuilder``, configures it, and
    returns ``builder.load()``.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["lightweight"]
    module: str
    """Dotted Python module path (e.g. ``my_pkg.workflows.alpha``)."""
    function: str
    """Name of the no-arg callable in the module that returns a Workflow."""


WorkflowSource = Annotated[
    WorkflowSourceYAML | WorkflowSourceLightweight,
    Field(discriminator="kind"),
]


class WorkflowRequirements(BaseModel):
    """Prerequisites checked before EACH tool call (env can change).

    Module checks use ``importlib.util.find_spec`` — they verify the
    module is importable WITHOUT importing it (importing a heavy module
    at startup may have side effects).
    """

    model_config = ConfigDict(extra="forbid")

    env: list[str] = Field(default_factory=list)
    """Env vars that must be set AND non-empty."""
    modules: list[str] = Field(default_factory=list)
    """Python modules that must be importable."""
    binaries: list[str] = Field(default_factory=list)
    """Executables that must be on PATH (checked via ``shutil.which``) — e.g. an external
    aligner like ``mafft``. Lets a binary-dependent workflow report honest availability via
    ``list_workflows`` instead of only failing at run time."""
    unavailable_hint: str = ""
    """Optional human-facing guidance appended to the unavailable-tool error
    when prerequisites are NOT met. Use it to be HONEST about an
    infrastructure dependency the user can't infer from a bare ``env var
    $RHEA_MCP_URL is not set`` — e.g. "needs Docker + Rhea; without them use
    the MAFFT path (viral_conserved_sites) or the LLM-only analysis
    (viral_epitope_analysis)". Keeps the no-silent-failure contract:
    a locked Docker/Rhea workflow names its working alternative instead of
    just refusing."""


class WorkflowCatalogEntry(BaseModel):
    """One pre-made workflow exposed as one MCP tool."""

    model_config = ConfigDict(extra="forbid")

    tool_name: str
    """MCP tool name (shown in tools/list, used in tools/call)."""
    description: str
    """Tool description (shown in tools/list)."""
    source: WorkflowSource
    input_schema: dict[str, Any]
    """JSON Schema for the MCP tool parameters. The synthesized
    function's signature is derived from ``properties`` + ``required``;
    FastMCP regenerates an equivalent schema from that signature."""
    requires: WorkflowRequirements = Field(default_factory=WorkflowRequirements)
    timeout_seconds: float = 600.0
    """Maximum wall time for the workflow cascade to drain. Passed as
    ``timeout`` to ``Workflow.run``."""
    input_envelope_key: str | None = None
    """Optional. When set, the runner wraps the MCP-tool kwargs as
    ``{input_envelope_key: kwargs}`` before calling ``workflow.run``.
    Use this when the workflow has a single workflow-level input data
    unit that takes a dict payload, and you want the MCP schema to
    expose the dict's fields flat (rather than nested under the data
    unit name)."""
    settle_ms: int = 200
    """How long ``Workflow.run`` waits after the last cascade activity
    before declaring the cascade drained. A workflow with multi-second
    gaps between steps (e.g. a remote tool call, file I/O) needs a
    larger value — 50ms is too short and causes the cascade to be
    declared drained while later steps are still pending, returning a
    partial result (silent-failure shape). The proven-working baseline
    is 200ms; bump higher (500-2000) for workflows with long quiet
    periods between trigger fires."""
    output_envelope_key: str | None = None
    """Optional. The OUTPUT mirror of ``input_envelope_key``.
    ``Workflow.run`` returns ``{"status": ..., <output_du_name>: <value>,
    ...}``. The runner strips ``status`` and returns the rest, which by
    default is keyed by workflow-level output-data-unit names — awkward
    for MCP clients that expect a flat result dict. When
    ``output_envelope_key`` is set, the runner returns
    ``result[output_envelope_key]`` directly (flat). Use this for a
    workflow with a single output data unit whose value is itself a
    dict that the MCP client wants to see flat. Default ``None``
    preserves the keyed-by-DU-name shape."""
    prewarm_rhea_tools: list[str] = Field(default_factory=list)
    """Optional. Rhea-side tool names whose conda envs must be installed
    BEFORE the MCP server is reported ready. The orchestrator's
    pre-warm phase queries each tool's requirements from Rhea's
    Postgres + invokes ``rhea.agent.utils.install_conda_env`` directly
    — bypassing the Academy actor (so an install failure doesn't wedge
    the actor for the whole session) and populating the Redis
    conda-pack cache so the first user invocation hits the cache.
    Empty by default; declare for workflows whose first-call latency
    or wedge risk is unacceptable. E.g. ``["muscle"]`` for the
    rhea_muscle_alignment workflow."""


class WorkflowCatalog(BaseModel):
    """Top-level catalog file: a list of entries."""

    model_config = ConfigDict(extra="forbid")

    workflows: list[WorkflowCatalogEntry]

    promote_discovered: list[str] = Field(default_factory=list)
    """Names of DISCOVERED workflows to ALSO expose as first-class MCP tools at startup,
    WITHOUT hand-writing a full ``workflows:`` entry. Each name is resolved from filesystem
    discovery (``resolve_catalog_entry``); the tool gets the workflow's own description and a
    typed ``{query}`` signature so a model sees + calls it directly (no list_workflows hop).
    A workflow that needs richer typed params (taxon_id, protein, …) should get a full
    ``workflows:`` entry instead — this list is the lightweight path for query-shaped ones.
    Names already present in ``workflows:`` are skipped (the explicit entry wins)."""


# ---------------------------------------------------------------------------
# Registration report
# ---------------------------------------------------------------------------


@dataclass
class RegistrationReport:
    """Outcome of ``register_workflows``."""

    registered: list[str] = field(default_factory=list)
    """Tool names that were registered AND have their prereqs met."""
    unavailable: list[tuple[str, str]] = field(default_factory=list)
    """``(tool_name, reason)`` for tools registered with the
    [UNAVAILABLE] marker (prereqs unmet at registration time)."""
    failed: list[tuple[str, str]] = field(default_factory=list)
    """``(tool_name_or_index, error_message)`` for entries that could
    not be registered at all (catalog parse / import / synthesis
    error)."""

    def summary_line(self) -> str:
        return (
            f"{len(self.registered)} registered, "
            f"{len(self.unavailable)} unavailable, "
            f"{len(self.failed)} failed"
        )


# ---------------------------------------------------------------------------
# Catalog loading
# ---------------------------------------------------------------------------


def _apecx_package_root() -> Path:
    """The apecx_integration package directory.

    Resolves to ``.../src/apecx_integration``. Relative ``path`` values
    in ``WorkflowSourceYAML`` resolve against this.
    """
    return Path(__file__).resolve().parent.parent


def _packaged_default_catalog_path() -> Path:
    return Path(__file__).resolve().parent / "configs" / "mcp_workflow_catalog.yml"


def load_catalog(path: str | Path | None = None) -> WorkflowCatalog:
    """Load + validate a workflow catalog.

    Args:
        path: Catalog YAML path. ``None`` → load the packaged default
            (``mcp_surface/configs/mcp_workflow_catalog.yml``).

    Returns:
        Parsed + validated ``WorkflowCatalog``.

    Raises:
        FileNotFoundError: catalog file does not exist.
        ValueError: YAML parse error OR Pydantic validation error
            (e.g. unknown field, missing required field, bad
            discriminator value). The framework FAIL-LOUD: a broken
            catalog MUST NOT silently produce an empty tool surface.
    """
    catalog_path = Path(path) if path is not None else _packaged_default_catalog_path()
    if not catalog_path.is_file():
        raise FileNotFoundError(
            f"workflow catalog not found at {catalog_path}. "
            f"Set APECX_MCP_WORKFLOW_CATALOG to override, or ensure the "
            f"packaged default ships with the apecx_integration install."
        )

    try:
        raw = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"workflow catalog at {catalog_path} is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError(
            f"workflow catalog at {catalog_path} must be a YAML mapping with a "
            f"'workflows:' key at the top level; got {type(raw).__name__}"
        )

    try:
        return WorkflowCatalog.model_validate(raw)
    except Exception as exc:
        # Pydantic ValidationError → ValueError with the path included
        # so the operator immediately knows which file is broken.
        raise ValueError(f"workflow catalog at {catalog_path} failed validation: {exc}") from exc


def resolve_catalog_entry(
    name: str, catalog: WorkflowCatalog | None = None
) -> WorkflowCatalogEntry | None:
    """The runnable entry for ``name`` — catalog override if present, else SYNTHESIZED
    from dynamic filesystem discovery.

    This is what makes a workflow runnable by name WITHOUT a catalog registration: the
    hand-written ``mcp_workflow_catalog.yml`` only tunes run-hints (settle_ms, envelope
    keys, prereqs, prewarm) for the few workflows that need them; every other workflow on
    disk resolves to a synthesized entry whose ``source`` comes straight from
    ``workflow_discovery``. Envelope keys are left ``None`` here and auto-derived from the
    LOADED workflow at run time (``run_workflow`` introspects the single workflow-level
    input DU — safe under G122, which deposits to both workflow + first-step inputs and
    fails loud on an unknown key).

    Returns ``None`` only when ``name`` is neither cataloged nor discoverable on disk.
    """
    catalog = catalog if catalog is not None else load_catalog()
    for e in catalog.workflows:
        if e.tool_name == name:
            return e

    from apecx_integration.mcp_surface.workflow_discovery import discover_by_name

    dw = discover_by_name(name)
    if dw is None:
        return None
    return WorkflowCatalogEntry(
        tool_name=dw.name,
        description=dw.description or f"{dw.name} (auto-discovered workflow)",
        source=dw.source,  # dict → discriminated WorkflowSource union (validated)
        input_schema={"type": "object", "additionalProperties": True},
        # No tuned run-hints: network/subprocess workflows need headroom, so default the
        # settle window high (a too-short window yields a partial result, not a wrong one).
        settle_ms=2000,
    )


def sole_data_unit_name(data_units: Any) -> str | None:
    """The single DU name in a ``{name: cfg}`` mapping or ``[name]`` list, else ``None``.

    Used to auto-derive a discovered workflow's input envelope key from its loaded shape.
    Returns ``None`` for 0 or >1 units — the caller then leaves the key unset and lets the
    framework's deposit fail loud rather than guess (no silent mis-routing).
    """
    if isinstance(data_units, dict):
        names = list(data_units.keys())
    elif isinstance(data_units, (list, tuple)):
        names = list(data_units)
    else:
        return None
    return names[0] if len(names) == 1 else None


# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------


def check_prerequisites(reqs: WorkflowRequirements) -> tuple[bool, list[str]]:
    """Return ``(all_met, missing_reasons)``.

    Env: a variable counts as set when ``os.environ.get(name)`` is a
    non-empty string. Modules: ``importlib.util.find_spec(name)`` is
    used so the module itself is NOT imported (avoids triggering side
    effects).
    """
    missing: list[str] = []
    for var in reqs.env:
        value = os.environ.get(var)
        if not value:
            missing.append(f"env var ${var} is not set")
    for module_name in reqs.modules:
        try:
            spec = importlib.util.find_spec(module_name)
        except (ImportError, ValueError) as exc:
            spec = None
            missing.append(f"module '{module_name}' import probe failed: {exc}")
            continue
        if spec is None:
            missing.append(f"module '{module_name}' not importable")
    for binary in reqs.binaries:
        if shutil.which(binary) is None:
            missing.append(f"executable '{binary}' not found on PATH")
    return (len(missing) == 0, missing)


# ---------------------------------------------------------------------------
# Workflow cache + loading
# ---------------------------------------------------------------------------


_workflow_cache: dict[str, Any] = {}
"""tool_name → loaded Workflow. Workflows are long-lived per-process;
``Workflow.run`` resets relevant cascade state between calls (see
test_rhea_muscle_alignment_workflow.py::test_workflow_from_config_against_live_rhea
for the proof of safe-reuse for the demo case). If a future entry's
workflow is NOT safe to reuse, surface that rather than papering over
it with per-call reloads."""


def _resolve_yaml_path(rel_or_abs: str) -> Path:
    """Resolve a YAML path against the apecx_integration package root.

    Absolute paths are returned unchanged. Relative paths are joined to
    the package root (the directory containing ``mcp_surface/``).
    """
    p = Path(rel_or_abs)
    if p.is_absolute():
        return p
    return _apecx_package_root() / p


def _load_workflow_for_entry(entry: WorkflowCatalogEntry) -> Any:
    """Load (or return cached) Workflow instance for one catalog entry.

    Raises:
        FileNotFoundError: YAML source path does not exist.
        ImportError: lightweight source module/function not importable
            or not a Workflow factory.
        TypeError: lightweight source function returned a non-Workflow.
    """
    cached = _workflow_cache.get(entry.tool_name)
    if cached is not None:
        return cached

    source = entry.source
    if source.kind == "yaml":
        yaml_path = _resolve_yaml_path(source.path)
        if not yaml_path.is_file():
            raise FileNotFoundError(
                f"workflow YAML for tool '{entry.tool_name}' not found at {yaml_path} "
                f"(catalog source.path={source.path!r}; resolved against "
                f"apecx_integration package root)"
            )
        from nanobrain.core.workflow import Workflow

        workflow = Workflow.from_config(str(yaml_path))
    else:
        # lightweight
        try:
            mod = importlib.import_module(source.module)
        except ImportError as exc:
            raise ImportError(
                f"workflow factory module '{source.module}' for tool "
                f"'{entry.tool_name}' is not importable: {exc}"
            ) from exc
        factory = getattr(mod, source.function, None)
        if factory is None:
            raise ImportError(
                f"workflow factory '{source.module}.{source.function}' for tool "
                f"'{entry.tool_name}' does not exist (module has no attribute "
                f"{source.function!r})"
            )
        if not callable(factory):
            raise TypeError(
                f"workflow factory '{source.module}.{source.function}' is not "
                f"callable: got {type(factory).__name__}"
            )
        workflow = factory()
        # Late import — only when we have a lightweight workflow to validate.
        from nanobrain.core.workflow import Workflow as _Workflow

        if not isinstance(workflow, _Workflow):
            raise TypeError(
                f"workflow factory '{source.module}.{source.function}' returned "
                f"{type(workflow).__name__}; expected a nanobrain Workflow"
            )

    _workflow_cache[entry.tool_name] = workflow
    return workflow


def _clear_workflow_cache() -> None:
    """Test hook — drop every cached Workflow."""
    _workflow_cache.clear()


# ---------------------------------------------------------------------------
# Tool synthesis (the FastMCP-API workaround)
# ---------------------------------------------------------------------------


# JSON Schema → Python type-annotation strings. We only need names that
# eval inside the synthesized function's globals (we expose builtins).
_JSON_TO_PY_ANNOT = {
    "string": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
    "object": "dict",
    "array": "list",
    "null": "None",
}


def _json_type_to_python_annotation(prop_schema: dict[str, Any]) -> str:
    """Map a JSON-Schema property to a Python type-annotation string.

    Best-effort: covers the common scalar/object/array shapes. Unknown
    or compound types fall back to ``Any``. The runtime contract is
    "the synthesized function receives this kwarg and forwards it to
    the workflow"; pydantic validates the shape before we see it.
    """
    json_type = prop_schema.get("type")
    if isinstance(json_type, list):
        # nullable: ["string", "null"] etc. We treat as "str | None".
        non_null = [t for t in json_type if t != "null"]
        if len(non_null) == 1 and "null" in json_type:
            base = _JSON_TO_PY_ANNOT.get(non_null[0], "Any")
            return f"{base} | None"
        return "Any"
    if isinstance(json_type, str):
        return _JSON_TO_PY_ANNOT.get(json_type, "Any")
    return "Any"


def _synthesize_tool_function(
    entry: WorkflowCatalogEntry,
    runner: Callable[..., Any],
) -> Callable[..., Any]:
    """Build an async function whose signature mirrors ``entry.input_schema``.

    FastMCP derives the tools/list inputSchema from the function's
    signature (``func_metadata(fn).arg_model.model_json_schema()``).
    There is NO ``parameters=`` knob on ``FastMCP.tool()``. So to ship
    the catalog-declared schema to the MCP client, we synthesize a
    function with named parameters that match the schema's
    ``properties``.

    The body is fixed: ``return await runner(**locals_minus_runner)``.
    """
    schema = entry.input_schema
    if not isinstance(schema, dict) or schema.get("type") not in (None, "object"):
        raise ValueError(
            f"catalog entry '{entry.tool_name}' has input_schema that is not a "
            f"JSON Schema object: {schema!r}"
        )
    properties: dict[str, Any] = schema.get("properties") or {}
    required: set[str] = set(schema.get("required") or [])

    # Ordered params: required first (no default), then optional.
    required_params = [name for name in properties if name in required]
    optional_params = [name for name in properties if name not in required]

    param_decls: list[str] = []
    forward_keys: list[str] = []
    for name in required_params:
        annot = _json_type_to_python_annotation(properties[name])
        param_decls.append(f"{name}: {annot}")
        forward_keys.append(name)
    for name in optional_params:
        prop = properties[name]
        annot = _json_type_to_python_annotation(prop)
        # Default = explicit "default" if present, else None for optional.
        if "default" in prop:
            default_repr = repr(prop["default"])
        else:
            default_repr = "None"
            # If we synthesised "| None" for nullables already, fine;
            # otherwise widen so default=None type-checks cleanly.
            if "None" not in annot and annot != "Any":
                annot = f"{annot} | None"
        param_decls.append(f"{name}: {annot} = {default_repr}")
        forward_keys.append(name)

    # The synthesized function name MUST be a valid Python identifier
    # AND match what we'll register with FastMCP. FastMCP allows
    # passing name= explicitly, so the runtime tool name comes from
    # the decorator argument; the function __name__ is for clarity in
    # tracebacks. Use the tool_name when it's a valid identifier;
    # else mangle.
    fn_name = entry.tool_name if entry.tool_name.isidentifier() else "workflow_tool"

    forward_dict = "{" + ", ".join(f"{k!r}: {k}" for k in forward_keys) + "}"
    params_src = ", ".join(param_decls)

    # Triple-quoted docstring carries the catalog description (also
    # passed as description= to FastMCP, but the docstring shows in
    # IDEs and reprs).
    doc_safe = entry.description.replace('"""', '\\"\\"\\"')

    source = dedent(
        f'''
        async def {fn_name}({params_src}) -> dict:
            """{doc_safe}"""
            return await _runner_bound(**{forward_dict})
        '''
    ).strip()

    # eval globals: expose runner + a few types the annotation strings
    # may reference. The annotations are strings at definition time
    # but pydantic / inspect resolve them lazily; expose the names.
    exec_globals: dict[str, Any] = {
        "_runner_bound": runner,
        "Any": Any,
        "dict": dict,
        "list": list,
        "str": str,
        "int": int,
        "float": float,
        "bool": bool,
    }
    exec_locals: dict[str, Any] = {}
    exec(compile(source, f"<workflow_tool:{entry.tool_name}>", "exec"), exec_globals, exec_locals)  # noqa: S102
    fn = exec_locals[fn_name]
    fn.__doc__ = entry.description
    return fn


# ---------------------------------------------------------------------------
# The registrar
# ---------------------------------------------------------------------------


def register_workflows(
    server: Any,
    catalog: WorkflowCatalog,
    *,
    logger: logging.Logger | None = None,
) -> RegistrationReport:
    """Register one MCP tool per catalog entry on ``server``.

    Behavior per entry:
      - Synthesize an async function whose signature reflects the
        catalog's ``input_schema`` and whose body dispatches to the
        per-entry runner.
      - Run prerequisite checks. If unmet, suffix the description with
        ``[UNAVAILABLE: <reasons>]`` and bind the function to a static
        runner that returns the actionable error. The tool IS still
        registered (silent absence forbidden by policy).
      - Register with ``server.tool(name=..., description=...)(fn)``.
      - On any exception during synthesis or registration, log ERROR
        and add the entry to ``RegistrationReport.failed``. Other
        entries still register.

    Args:
        server: A FastMCP-like object exposing ``.tool(name=...,
            description=...)`` as a decorator factory. Tests pass a
            tiny capture object.
        catalog: Validated ``WorkflowCatalog``.
        logger: Optional logger for INFO (registered) + WARNING
            (unavailable) + ERROR (failed) emissions.

    Returns:
        ``RegistrationReport`` with the three outcome lists.
    """
    lg = logger or log
    report = RegistrationReport()

    # EVERY workflow tool — explicit ``workflows:`` entries AND ``promote_discovered`` ones —
    # dispatches through the SINGLE guarded path ``run_workflow``. There is NO second runner
    # (the old per-entry ``_runner`` was retired 2026-06-15: it checked only run ``status``,
    # not the output VALUE, so a G127 strand — status 'completed', output empty — returned NULL
    # from the direct first-class tool a weak model calls, even though ``run_workflow`` guarded
    # it. Unifying gives every direct tool the G127 fail-loud + requires_llm gate + param-gap
    # control-return + run-store/provenance.)
    for index, entry in enumerate(catalog.workflows):
        _register_one_entry(
            server, entry, lg, report, identifier=entry.tool_name or f"<entry #{index}>"
        )

    already = set(report.registered) | {tn for tn, _ in report.unavailable}
    for entry in _resolve_promoted_entries(catalog, already, lg):
        _register_one_entry(server, entry, lg, report, identifier=entry.tool_name)

    return report


async def _run_via_run_workflow(entry: WorkflowCatalogEntry, **kwargs: Any) -> dict[str, Any]:
    """The SINGLE workflow-tool runner: the shared guarded execution core.

    Calls ``eo_primitives._run_resolved_entry`` with the entry ALREADY in hand (the registry
    resolved it) — the same core ``run_workflow`` reaches after name-resolution, so a direct
    first-class tool and ``run_workflow(name, …)`` are identical (full guard stack: requires_llm,
    param-gap, G127 output-value check, run-store, provenance). No per-call catalog re-parse.
    ``None``-valued optional params are dropped so an unset optional never reaches the workflow
    as an explicit ``None``."""
    from apecx_integration.mcp_surface.tools.eo_primitives import _run_resolved_entry

    params = {k: v for k, v in kwargs.items() if v is not None}
    return await _run_resolved_entry(entry, params)


def _register_one_entry(
    server: Any,
    entry: WorkflowCatalogEntry,
    lg: logging.Logger,
    report: RegistrationReport,
    *,
    identifier: str,
    runner: Callable[..., Any] | None = None,
) -> None:
    """Register ONE catalog entry as an MCP tool (live or UNAVAILABLE), recording the outcome.

    All entries (explicit + promoted) take the identical synthesize → prereq-gate → register →
    report path, dispatching through the single ``_run_via_run_workflow`` runner."""
    dispatch = runner or _run_via_run_workflow
    try:
        met, missing = check_prerequisites(entry.requires)
        if met:

            async def _live_dispatch(_entry=entry, _dispatch=dispatch, **kwargs):
                return await _dispatch(_entry, **kwargs)

            fn = _synthesize_tool_function(entry, _live_dispatch)
            server.tool(name=entry.tool_name, description=entry.description)(fn)
            report.registered.append(entry.tool_name)
            lg.info(
                "workflow registry: registered MCP tool '%s' (source=%s)",
                entry.tool_name,
                entry.source.kind,
            )
        else:
            reason = "; ".join(missing)
            hint = entry.requires.unavailable_hint

            async def _unavailable_dispatch(_entry=entry, _reason=reason, _hint=hint, **kwargs):
                return {
                    "error": (
                        f"tool '{_entry.tool_name}' is unavailable because its "
                        f"prerequisites are not met: {_reason}. "
                        f"Fix the configuration (set the missing env vars, "
                        f"install the missing modules) and retry." + (f" {_hint}" if _hint else "")
                    )
                }

            fn = _synthesize_tool_function(entry, _unavailable_dispatch)
            description = f"{entry.description}\n\n[UNAVAILABLE: {reason}]" + (
                f" {hint}" if hint else ""
            )
            server.tool(name=entry.tool_name, description=description)(fn)
            report.unavailable.append((entry.tool_name, reason))
            lg.warning(
                "workflow registry: tool '%s' registered as UNAVAILABLE — %s",
                entry.tool_name,
                reason,
            )
    except Exception as exc:
        report.failed.append((identifier, f"{type(exc).__name__}: {exc}"))
        lg.error(
            "workflow registry: failed to register tool '%s': %s", identifier, exc, exc_info=True
        )


# Typed default for a promoted DISCOVERED workflow: a single required ``query`` so the model
# sees a usable parameter (the discovery default is untyped ``additionalProperties: true``,
# which synthesizes a NO-arg tool the model can't drive). ``additionalProperties: true`` is kept
# so a caller MAY still pass extra keys, which ``run_workflow`` forwards + param-gap-validates.
_DEFAULT_PROMOTED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "The scientist's free-text question for this workflow.",
        }
    },
    "required": ["query"],
    "additionalProperties": True,
}


def _resolve_promoted_entries(
    catalog: WorkflowCatalog, already_registered: set[str], lg: logging.Logger
) -> list[WorkflowCatalogEntry]:
    """Build registrable entries for ``catalog.promote_discovered`` names.

    Each is resolved via ``resolve_catalog_entry`` (so its ``source`` + runner path match an
    explicit entry exactly); a discovery-synthesized entry's untyped schema is replaced with the
    typed ``{query}`` default so the tool exposes a usable parameter. Names already registered as
    explicit entries are skipped; unknown names are warned + skipped (never a hard failure)."""
    entries: list[WorkflowCatalogEntry] = []
    for name in catalog.promote_discovered:
        if name in already_registered:
            continue  # an explicit workflows: entry already registered it
        entry = resolve_catalog_entry(name, catalog)
        if entry is None:
            lg.warning(
                "workflow registry: promote_discovered name '%s' is not a discoverable workflow "
                "— skipped (check spelling / that its dir exists under composition/workflows/).",
                name,
            )
            continue
        if not (isinstance(entry.input_schema, dict) and entry.input_schema.get("properties")):
            entry = entry.model_copy(update={"input_schema": dict(_DEFAULT_PROMOTED_SCHEMA)})
        entries.append(entry)
    return entries


__all__ = [
    "RegistrationReport",
    "WorkflowCatalog",
    "WorkflowCatalogEntry",
    "WorkflowRequirements",
    "WorkflowSource",
    "WorkflowSourceLightweight",
    "WorkflowSourceYAML",
    "check_prerequisites",
    "load_catalog",
    "register_workflows",
]
