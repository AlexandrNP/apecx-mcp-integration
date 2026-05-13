Write a nanobrain `BaseStep` subclass named `DedupePreserveOrderStep` whose `process()` method takes a dict `{"items": list}` and returns `{"unique": <list with duplicates removed, first occurrence wins, order preserved>}`.

Example: `[1, 2, 1, 3, 2, 4]` → `[1, 2, 3, 4]`.

Requirements:
- Subclass `nanobrain.core.step.BaseStep`.
- Implement `async def process(self, input_data: dict, **kwargs) -> dict`.
- Do NOT override `execute()`.
- The class must be loadable via `BaseStep.from_config(yaml_path)`.

Return ONLY a single ```python fenced block with the full module source. No prose.
