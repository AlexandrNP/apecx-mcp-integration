Write a nanobrain `BaseStep` subclass named `DoubleStep` whose `process()` method takes a dict `{"n": int}` and returns `{"doubled": n * 2}`.

Requirements:
- Subclass `nanobrain.core.step.BaseStep`.
- Implement `async def process(self, input_data: dict, **kwargs) -> dict`.
- Do NOT override `execute()`.
- The class must be loadable via `BaseStep.from_config(yaml_path)`.

Return ONLY a single ```python fenced block with the full module source. No prose.
