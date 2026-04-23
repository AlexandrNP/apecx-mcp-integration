If — and only if — you emit novel Python, every novel step must:

1. Appear under ``steps:`` in the workflow YAML with a ``class`` that
   exactly matches the novel Python's class name (``<step_id>:
   {class: "<your.module.NewClass>", config: {...}}``).
2. Have its source code inside the separate ``novel_python`` fenced
   code block, keyed by the step_id::

       ```novel_python
       <step_id_1>: |
         import ...
         class NewClass(BaseStep):
             async def process(self, input_data, **kwargs):
                 ...
       <step_id_2>: |
         ...
       ```

3. Obey the T13 sandbox import whitelist. The whitelist lives at
   ``configs/sandbox/import_whitelist.txt`` and covers stdlib
   essentials, numpy, pandas, pydantic, nanobrain, apecx_db_integration,
   apecx_integration. Any import outside this list will be REJECTED
   by the scanner before the workflow runs.

4. Implement ``async def process(self, input_data, **kwargs)`` and
   NOT override ``execute``. The framework enforces this at step
   initialization.

5. Use ``from_config`` pattern (no direct constructors for nanobrain
   ``BaseStep`` or ``DataUnit`` subclasses).

If you CANNOT satisfy these constraints for some novel step, DO NOT
emit a novel_python block. Emit a workflow that uses only library
components and note in the workflow's top-level ``description:``
field which capability is missing.
