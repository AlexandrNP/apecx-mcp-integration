Write a nanobrain `BaseStep` subclass named `UpperStep` whose `process()` method takes a dict `{"text": str}` and returns `{"output": text.upper()}`.

Requirements:
- Subclass `nanobrain.core.step.BaseStep`.
- Implement `async def process(self, input_data: dict, **kwargs) -> dict`.
- Do NOT override `execute()` — the framework forbids it.
- The class must be loadable via `BaseStep.from_config(yaml_path)`.

Return ONLY a single ```python fenced block with the full module source (imports + class). No prose.
