Write a nanobrain `BaseStep` subclass named `UpperStep` whose `process()` method takes a dict `{"text": str}` and returns `{"output": text.upper()}`.

Requirements:
- Subclass `nanobrain.core.step.BaseStep`.
- Implement `async def process(self, input_data: dict, **kwargs) -> dict`.
- Do NOT override `execute()` — the framework forbids it (raises `ComponentConfigurationError`).
- Use a `StepConfig` subclass with `model_config = ConfigDict(extra="forbid", validate_assignment=False)`.
- The class must be loadable via `BaseStep.from_config(yaml_path)` where the YAML supplies the `name` field.

Return ONLY a single ```python fenced block with the full module source (imports, config class, step class). No prose.
