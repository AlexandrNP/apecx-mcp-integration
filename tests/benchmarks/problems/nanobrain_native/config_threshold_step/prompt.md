Write a nanobrain `BaseStep` subclass named `ThresholdStep` that filters a list of numbers against a configurable threshold.

Requirements:
- Subclass `nanobrain.core.step.StepConfig` to add a `threshold: float` field with a default of 0.0. Call the subclass `ThresholdStepConfig`.
- Subclass `nanobrain.core.step.BaseStep` and override `_get_config_class` to return `ThresholdStepConfig`.
- Read the threshold from the step config in `_init_from_config` (or via the configured attribute on the step instance).
- Implement `async def process(self, input_data: dict, **kwargs) -> dict` taking `{"items": list[float]}` and returning `{"above": [x for x in items if x > self._threshold]}`.
- Do NOT override `execute()`.
- The class must be loadable via `BaseStep.from_config(yaml_path)` where the YAML sets `threshold:` under the step config.

Return ONLY a single ```python fenced block with the full module source. No prose.
