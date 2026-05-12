**Prefer composition over generation.**

Every library component available to you has been reviewed, tested,
and integrated. Every novel Python block you emit is an unreviewed
artifact that a human reviewer (Step 4 HITL gate) will need to
approve and that may be rejected by the T13 import-scanner (no
dynamic imports, no banned constructs, narrow whitelist).

For each step in your workflow:

1. **First**, scan the candidate-components list for a component
   whose ``description`` covers the step's purpose. If one matches,
   use it verbatim — reference its ``class`` path and its wrapper
   YAML (or inline its ``config``). Do NOT paraphrase or wrap; the
   library entry is canonical.
2. **Second**, check whether two or more library components composed
   via links can cover the step. Prefer two DirectLinks over one
   novel Python block, even if the novel block would be shorter.
3. **Only if** neither composition nor reuse applies, emit a novel
   Python step. Mark it in the ``novel_python`` fenced block and
   justify its existence by naming what library gap it fills.

When in doubt, pick the library component. A workflow that reuses
known-working components is reviewable; a workflow with unexplained
novel Python is not.

**CLOSED-CLASS RULE — pick or extend, never edit (load-bearing for
adoption, 2026-05-12):**

When an existing library component is *almost* right but not quite,
the path is NEVER "edit the existing class to add a parameter".
The path is always one of:

1. **Pick a different existing component** that fits better.
2. **Compose** two existing components with a DirectLink between them.
3. **Author a NEW class** in the ``novel_python`` fence with a NEW
   class name, give it its own ``class:`` path under ``steps:``, and
   reference it from your workflow YAML. The new file is yours; the
   library tree stays untouched.

Adoption requires that every existing workflow keep working after a
new one ships — if you mutate ``SynthesisContextAssemblyStep`` to fit
ONE workflow, every other workflow depending on that class can
silently break. The closed-class discipline is what makes the library
trustworthy enough to compose against.
