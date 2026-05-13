Write a nanobrain `BaseStep` subclass named `WordCountStep` whose `process()` method takes a dict `{"text": str}` and returns `{"count": <number of whitespace-separated words>}`. Treat consecutive whitespace as one separator. Empty string → 0.

Requirements:
- Subclass `nanobrain.core.step.BaseStep`.
- Implement `async def process(self, input_data: dict, **kwargs) -> dict`.
- Do NOT override `execute()`.
- The class must be loadable via `BaseStep.from_config(yaml_path)`.

Return ONLY a single ```python fenced block with the full module source. No prose.
