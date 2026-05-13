Write a nanobrain workflow that uppercases a string then reverses it. Build it programmatically using `nanobrain.lightweight.WorkflowBuilder`.

The candidate must be a SINGLE Python module containing:

1. Two `BaseStep` subclasses `UpperStep` and `ReverseStep`.
   - `UpperStep.process({"text": s})` returns `{"text": s.upper()}`.
   - `ReverseStep.process({"text": s})` returns `{"text": s[::-1]}`.

2. A module-level function `build_workflow()` that:
   - Imports `WorkflowBuilder` from `nanobrain.lightweight`.
   - Adds both steps via `builder.add_step(name, "__main__.UpperStep", input_data_units={...}, output_data_units={...}, triggers=[...])` (you need to declare each step's input/output DUs and at least one DataUnitChangeTrigger so the cascade fires).
   - Adds a `DirectLink` from upper's output DU to reverse's input DU via `builder.add_link("upper.upper_output", "reverse.reverse_input", link_type="direct")` (the builder injects `auto_transfer: true` automatically under config_version: 2).
   - Returns the loaded workflow via `builder.load()`.

Use `__main__.UpperStep` and `__main__.ReverseStep` as the dotted class paths.

Return ONLY a single ```python fenced block with the full module source. No prose.
