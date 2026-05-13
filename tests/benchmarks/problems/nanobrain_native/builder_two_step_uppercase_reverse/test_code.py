import asyncio

assert "build_workflow" in globals(), "candidate did not define build_workflow()"
assert "UpperStep" in globals() and "ReverseStep" in globals()


async def _drive():
    wf = build_workflow()  # noqa: F821  -- defined by candidate code at runtime
    await wf.initialize()
    children = (
        getattr(wf, "child_steps", None)
        or getattr(wf, "_child_steps", None)
        or getattr(wf, "steps", None)
        or {}
    )
    upper = next((s for n, s in children.items() if "upper" in n.lower()), None)
    reverse = next((s for n, s in children.items() if "reverse" in n.lower()), None)
    assert upper is not None and reverse is not None, (
        f"missing upper/reverse step in {list(children)}"
    )

    in_dus = list(upper.step_input_data_units.keys())
    assert in_dus, f"upper has no input DUs: {upper.step_input_data_units}"
    out_dus = list(reverse.step_output_data_units.keys())
    assert out_dus, f"reverse has no output DUs: {reverse.step_output_data_units}"

    envelope = await wf.process({in_dus[0]: {"text": "hello"}})
    assert envelope.get("status") in ("data_flow_initiated", "completed"), envelope
    drained = await wf.wait_for_cascade(timeout=20.0, settle_ms=100)
    assert drained, "cascade did not drain — likely auto_transfer issue or missing trigger"

    value = await reverse.step_output_data_units[out_dus[0]].get()
    if isinstance(value, dict):
        if value.get("text") == "OLLEH":
            return True
        for inner in value.values():
            if isinstance(inner, dict) and inner.get("text") == "OLLEH":
                return True
    raise AssertionError(f"expected OLLEH; got {value!r}")


assert asyncio.run(_drive())
