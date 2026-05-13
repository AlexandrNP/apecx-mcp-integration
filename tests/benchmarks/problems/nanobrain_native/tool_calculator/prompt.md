Write a nanobrain `ToolBase` subclass named `CalculatorTool` that evaluates a simple arithmetic expression involving `+`, `-`, `*`, `/`, integers, and parentheses.

Requirements:
- Subclass `nanobrain.core.tool.ToolBase`.
- Implement `async def execute(self, expression: str) -> dict` returning `{"result": <numeric result>}`.
- The expression is a trusted string from the caller; use Python's `ast.literal_eval` + a safe evaluator, OR `eval` with an empty globals dict — but you MUST reject any non-arithmetic character (raise `ValueError`).
- Do NOT use `os`, `subprocess`, or any module that allows side effects.
- The class must be loadable via `ToolBase.from_config(yaml_path)`.

Return ONLY a single ```python fenced block with the full module source. No prose.
