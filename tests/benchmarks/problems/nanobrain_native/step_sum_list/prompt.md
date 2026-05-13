Write a nanobrain `BaseStep` subclass named `SumListStep` whose `process()` method takes a dict `{"values": list[float]}` and returns `{"total": sum(values)}`.

Requirements:
- Subclass `nanobrain.core.step.BaseStep`.
- Implement `async def process(self, input_data: dict, **kwargs) -> dict`.
- Do NOT override `execute()`.
- The class must be loadable via `BaseStep.from_config(yaml_path)`.

Return ONLY a single ```python fenced block with the full module source. No prose.
