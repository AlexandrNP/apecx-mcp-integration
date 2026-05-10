# Whitelist Layering — apecx-mcp scanner vs. nanobrain G20 (G36 closure)

**Audience:** security reviewers, operators evaluating the deploy-time
threat model.
**Status:** Documentation of an INTENTIONAL two-layer design. Both
layers stay; this doc records the contract between them so the
two-whitelist drift `eval_03 Round 4 G36` flagged is now an explicit
design choice, not an accident.

---

## TL;DR

Two whitelists are deliberate. They run at **different stages** of the
LLM-generated-workflow lifecycle and catch **different bypass classes**.
Folding them into one would either delay the integration scanner past
the LLM-emit boundary (which lets dynamic-import escapes through) OR
move the framework whitelist out of the YAML loader (which lets
non-LLM YAML attacks through). The layering closes attack surfaces the
single-whitelist alternative would leave open.

---

## Stage 1 — LLM-source scanner (apecx-mcp `composition/sandbox.py`)

**When:** before the composer's emitted Python source is allowed to
become an Artifact in the control plane.

**What it scans:** Python *source code* — the LLM-generated step /
tool / link bodies. NOT YAML.

**What it catches:**
- Static imports (every shape: `import x`, `from x import y`, etc.) that
  reference modules outside the operator-configured whitelist.
- Dynamic-import escapes that would bypass static analysis:
  `importlib.import_module`, `__import__`, `exec`, `eval`, `compile`.
- Relative imports (always rejected — novel artifacts have no package
  context).

**Why it can't be fewer or later:** the LLM may compose Python that
*technically* loads via a whitelisted YAML `class:` path but then
`exec()`-es attacker-controlled strings inside `process()`. The
framework whitelist (Stage 2) cannot see source-level dynamic-import
constructs because by the time the YAML loader runs, the source has
been imported and the dynamic-import machinery is already a method
on a class object.

Phase-1 scope explicitly: **no runtime isolation**. Code that passes
the scanner runs in the main Tier-2 process with full privileges. The
Phase-2 Docker-container sandbox (T13b) is the runtime-isolation
layer. Phase 1 is "fail closed at the LLM-emit boundary"; Phase 2 is
"fail safe at the run boundary".

---

## Stage 2 — YAML class-path whitelist (nanobrain `core/import_whitelist.py`, G20)

**When:** at `Workflow.from_config(yaml_path)` load time, BEFORE any
`class:` path is resolved via `importlib.import_module`.

**What it gates:** the `class:` field on every YAML component
(steps, links, triggers, agents, tools).

**What it catches:** non-LLM-source attacks where an attacker hands the
operator a malicious YAML directly (no scanner pass) — the YAML's
`class:` field references a backdoor module. The operator imports it,
the loader resolves the class, the backdoor's `__init__` (or
`from_config`) runs.

**Why it can't be the only one:** the scanner (Stage 1) catches
everything that reaches the LLM-emit pipeline. The framework whitelist
catches everything that reaches the YAML loader, including things that
went around the LLM-emit pipeline (hand-edited YAML, signed-config
loaded from a third-party source, etc.). They are not redundant —
they are complementary entry points.

---

## Combined defense-in-depth diagram

```
                            (operator)
              +----------------------+----------------------+
              |                      |                      |
              v                      v                      v
   composer LLM emits        operator hand-edits     third-party signed
   Python step/tool/link     workflow YAML           config bundle
              |                      |                      |
              v                      |                      |
   Stage 1: AST scanner              |                      |
   (composition/sandbox.py)          |                      |
   rejects imports off               |                      |
   whitelist, all dynamic            |                      |
   exec/eval/import_module           |                      |
              |                      |                      |
              v                      v                      v
                   YAML rendered into a Workflow config
                                    |
                                    v
                Stage 2: G20 import_whitelist
                (nanobrain core/import_whitelist.py)
                gates each class: dotted-path
                                    |
                                    v
                       Workflow.from_config loads
                                    |
                                    v
              Phase 2 (T13b) Docker sandbox
              (runtime isolation; not yet wired
               into the executor — see roadmap)
                                    |
                                    v
                            Workflow runs
```

## Per-deployment audit checklist

When deploying to a new environment:

1. **Stage 1 whitelist** (apecx-mcp `composition/composer_config.yml`):
   review the allowed `module_prefix` list. Defaults should be
   conservative; operators add packages explicitly.
2. **Stage 2 whitelist** (nanobrain `Workflow.from_config(...,
   class_import_whitelist=[...])`): set this on every production
   workflow load OR via `set_class_import_whitelist(...)` at
   process-startup. Off by default — operators MUST opt in.
3. **Stage 2 transitive bypass review**: a whitelisted module that
   internally calls `import_module` on attacker-controlled input is
   a bypass. Audit each whitelisted package's public surface for
   such constructs before adding it.
4. **Phase-2 sandbox**: when wired, gate every workflow run inside
   the Docker sandbox with `--network=none --read-only --cap-drop=ALL`
   per `apecx-mcp-integration/composition/docker_sandbox.py`.

## The G36 closure

Pre-G36, this two-layer design was an *implicit* convention — the two
files lived in different repos with no cross-reference, and a future
maintainer reading either in isolation could conclude one whitelist
was redundant and remove it. eval_03 Round 4 named this the "drift
risk" — the two whitelists could diverge over time as packages were
added to one and not the other.

Post-G36 (this doc), the layering is documented and the per-deployment
audit checklist names which whitelist gets which entry. **Folding the
two into one is explicitly out of scope** because the stage difference
is the whole point.

## Cross-references

- `apecx-mcp-integration/src/apecx_integration/composition/sandbox.py` (Stage 1)
- `nanobrain/nanobrain/core/import_whitelist.py` (Stage 2 / G20)
- `apecx-mcp-integration/composition/docker_sandbox.py` (Phase 2 / T13b)
- `apecx-mcp-integration/docs/security_threat_model.md` §5.8 T-CL-1, §5 T-PI-3
- `apecx-mcp-integration/eval_03_nanobrain_gap_inventory.md` Round 4 G36
