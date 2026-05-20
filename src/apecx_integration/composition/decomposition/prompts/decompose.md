Decompose a scientific task into 2–5 independent sub-tasks, each solvable by a single workflow.

Rules:
- If the task is already atomic (one workflow can solve it), return "decomposable": false with an empty "subtasks" list. Do NOT invent sub-tasks.
- Otherwise return "decomposable": true and 2–5 sub-task descriptions. Each sub-task must be self-contained and independently runnable; together they must cover the whole task.
- Keep each sub-task description short and imperative (one line).
- Output ONLY a single JSON object. No prose, no explanation, no markdown code fences.

Output schema:
{"decomposable": <true|false>, "subtasks": [<string>, ...]}
