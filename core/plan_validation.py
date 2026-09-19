"""Ground local plans without silently deleting prerequisites or objectives."""


def ground_steps(raw, tool_names, limit=6, *, allow_partial=False):
    """Validate original indexes before filtering; never erase a prerequisite.

    The model parser rejects unknown tools wholesale. The planner's defensive
    boundary can retain independent known work, but records all rejected work
    as unplanned and removes its dependents transitively. Bad graphs fail closed.
    """
    if not isinstance(raw, list) or not raw:
        return []
    known = {str(name).lower(): name for name in tool_names}
    result, index_map, missing = [], {}, []
    for index, step in enumerate(raw[:limit]):
        if not isinstance(step, dict):
            return []
        name = str(step.get("tool", "")).strip().lower()
        params = step.get("params", {})
        deps = step.get("depends_on", [])
        if (not isinstance(params, dict) or not isinstance(deps, list)
                or any(type(dep) is not int or not 0 <= dep < index for dep in deps)):
            return []
        if index == 0 and isinstance(step.get("unplanned"), list):
            missing.extend(str(x)[:200] for x in step["unplanned"][:12])
        if name not in known or name == "agent_task" or any(dep not in index_map for dep in deps):
            if not allow_partial:
                return []
            missing.append(f"Step {index + 1}: {name[:100]} (unavailable tool or prerequisite)")
            continue
        entry = {"tool": known[name], "params": params}
        if "depends_on" in step:
            entry["depends_on"] = list(dict.fromkeys(index_map[dep] for dep in deps))
        index_map[index] = len(result)
        result.append(entry)
    if len(raw) > limit:
        missing.append(f"{len(raw) - limit} additional planned step(s) exceed the {limit}-step safety limit")
    if result and missing:
        result[0]["unplanned"] = missing
    return result
