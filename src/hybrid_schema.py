"""HybridPatch protocol schema helpers.

The HybridPatch method emits one envelope:
{"protocol":"hybridpatch","plan":{...},"action":{...}}

The schema here is intentionally structural. Content matching and deterministic
execution live in hybrid_executor.py; reference-free output checks live in
hybrid_gate.py.
"""

import json

ROUTE_LOCAL_PATCH = "local_patch"
ROUTE_BULK_PATCH = "bulk_patch"
ROUTE_DSL_RULES = "dsl_rules"
ROUTE_BOUNDED_REWRITE = "bounded_rewrite"

ROUTES = {
    ROUTE_LOCAL_PATCH,
    ROUTE_BULK_PATCH,
    ROUTE_DSL_RULES,
    ROUTE_BOUNDED_REWRITE,
}

# The public implementation exposes one current protocol.
PROTOCOL = "hybridpatch"

# Sentinel prefix for file bodies transported outside the JSON envelope.
# A field carrying "@body:<name>" is resolved to the literal content of the
# matching fenced block in the [FILE BODIES] section, avoiding a second layer
# of JSON string escaping. Inline string content remains available for short
# bodies.
BODY_REF_PREFIX = "@body:"

# Empirical protocol-burden thresholds used as soft telemetry only.
PROTOCOL_BURDEN_LIMITS = {
    "local_op_count": 31,
    "bulk_op_count": 30,
    "anchor_bytes": 4080,
    "envelope_bytes": 2898,
    "explicit_block_id_count": 39,
}

FOOTPRINT_ROUTES = {
    "few_precise_edits": ROUTE_LOCAL_PATCH,
    "many_repeated_edits": ROUTE_BULK_PATCH,
    "block_movement": ROUTE_DSL_RULES,
    "whole_file_change": ROUTE_BOUNDED_REWRITE,
}




LOCAL_OPS = {"replace", "delete", "insert"}
BULK_OPS = {"replace_all", "delete_lines_containing"}
DSL_RULES = {"copy_blocks", "distribute_blocks"}


def route_of(envelope):
    if not isinstance(envelope, dict):
        return None
    action = envelope.get("action")
    if isinstance(action, dict):
        return action.get("route")
    return None


def task_family_of(envelope):
    plan = envelope.get("plan") if isinstance(envelope, dict) else None
    if isinstance(plan, dict):
        return plan.get("task_family")
    return None


def _list_of_str(value):
    return isinstance(value, list) and all(isinstance(x, str) for x in value)


def _check_plan(plan, route, errors):
    """Validate the minimal plan and its route declaration."""
    if not isinstance(plan, dict):
        errors.append("plan must be an object")
        return
    expected = {"task_family", "edit_footprint"}
    actual = set(plan)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        if missing:
            errors.append("plan missing required fields: " + ",".join(missing))
        if extra:
            errors.append("plan has unsupported fields for HybridPatch: " + ",".join(extra))
    family = plan.get("task_family")
    if not isinstance(family, str) or not family.strip():
        errors.append("plan.task_family must be a non-empty string")
    footprint = plan.get("edit_footprint")
    if footprint not in FOOTPRINT_ROUTES:
        errors.append(
            "plan.edit_footprint must be one of "
            + repr(sorted(FOOTPRINT_ROUTES))
        )
    elif route is not None and FOOTPRINT_ROUTES[footprint] != route:
        errors.append(
            "plan_route_mismatch:"
            f"{footprint} requires {FOOTPRINT_ROUTES[footprint]}, got {route}"
        )


def _check_local(action, errors):
    ops = action.get("ops")
    if not isinstance(ops, list):
        errors.append("action.ops must be a list for local_patch")
        return
    for i, op in enumerate(ops):
        tag = f"action.ops[{i}]"
        if not isinstance(op, dict):
            errors.append(f"{tag} must be an object")
            continue
        t = op.get("op")
        if t not in LOCAL_OPS:
            errors.append(f"{tag}.op unknown for local_patch: {t!r}")
            continue
        if op.get("block_id") is None and op.get("file") is not None and not isinstance(op.get("file"), str):
            errors.append(f"{tag}.file must be a string when present")
        if op.get("block_id") is not None and not isinstance(op.get("block_id"), str):
            errors.append(f"{tag}.block_id must be a string when present")
        if t in ("replace", "delete"):
            if not isinstance(op.get("old_text"), str) or not op.get("old_text"):
                errors.append(f"{tag}.old_text must be a non-empty string")
        if t == "replace" and not isinstance(op.get("new_text"), str):
            errors.append(f"{tag}.new_text must be a string")
        if t == "insert":
            if op.get("position") not in ("before", "after"):
                errors.append(f"{tag}.position must be 'before' or 'after'")
            if not isinstance(op.get("anchor_text"), str) or not op.get("anchor_text"):
                errors.append(f"{tag}.anchor_text must be a non-empty string")
            if not isinstance(op.get("new_text"), str):
                errors.append(f"{tag}.new_text must be a string")
        occ = op.get("occurrence")
        if occ is not None and not (isinstance(occ, int) and occ >= 1):
            errors.append(f"{tag}.occurrence must be a positive integer")


def _check_local_boundary(action, errors, editable_filenames=None):
    ops = action.get("ops")
    if not isinstance(ops, list):
        return
    known = set(editable_filenames) if editable_filenames is not None else None
    for i, op in enumerate(ops):
        if not isinstance(op, dict):
            continue
        tag = f"action.ops[{i}]"
        filename = op.get("file")
        if not isinstance(filename, str) or not filename.strip():
            errors.append(f"{tag}.file must be a non-empty string for HybridPatch local_patch")
        elif known is not None and filename not in known:
            errors.append(f"{tag}.file is not an editable file: {filename}")
        # The concrete file declaration is the sole operation boundary.
        for forbidden_selector in ("block_id", "scope"):
            if forbidden_selector in op:
                errors.append(
                    f"{tag}.{forbidden_selector} is unsupported for HybridPatch "
                    "local_patch; use file only"
                )


def _check_bulk(action, errors):
    ops = action.get("ops")
    if not isinstance(ops, list):
        errors.append("action.ops must be a list for bulk_patch")
        return
    for i, op in enumerate(ops):
        tag = f"action.ops[{i}]"
        if not isinstance(op, dict):
            errors.append(f"{tag} must be an object")
            continue
        t = op.get("op")
        if t not in BULK_OPS:
            errors.append(f"{tag}.op unknown for bulk_patch: {t!r}")
            continue
        scope = op.get("scope")
        if scope is not None and not _list_of_str(scope):
            errors.append(f"{tag}.scope must be a list of strings")
        if t == "replace_all":
            if not isinstance(op.get("old_text"), str) or not op.get("old_text"):
                errors.append(f"{tag}.old_text must be a non-empty string")
            if not isinstance(op.get("new_text"), str):
                errors.append(f"{tag}.new_text must be a string")
        if t == "delete_lines_containing":
            if not isinstance(op.get("text"), str) or not op.get("text"):
                errors.append(f"{tag}.text must be a non-empty string")
        for key in ("expected_count_min", "expected_count_exact"):
            value = op.get(key)
            if value is not None and not (isinstance(value, int) and value >= 0):
                errors.append(f"{tag}.{key} must be a non-negative integer")


def _check_bulk_boundary(action, errors, editable_filenames=None):
    ops = action.get("ops")
    if not isinstance(ops, list):
        return
    known = set(editable_filenames) if editable_filenames is not None else None
    for i, op in enumerate(ops):
        if not isinstance(op, dict):
            continue
        scope = op.get("scope")
        if (not isinstance(scope, list) or not scope
                or any(not isinstance(name, str) or not name.strip() for name in scope)):
            errors.append(
                f"action.ops[{i}].scope must be a non-empty list of non-empty strings "
                "for HybridPatch bulk_patch"
            )
            continue
        if known is not None:
            for filename in scope:
                if filename not in known:
                    errors.append(
                        f"action.ops[{i}].scope contains non-editable file: {filename}"
                    )


def _check_dsl(action, errors):
    rules = action.get("rules")
    if not isinstance(rules, list):
        errors.append("action.rules must be a list for dsl_rules")
        return
    for i, rule in enumerate(rules):
        tag = f"action.rules[{i}]"
        if not isinstance(rule, dict):
            errors.append(f"{tag} must be an object")
            continue
        kind = rule.get("rule")
        if kind not in DSL_RULES:
            errors.append(f"{tag}.rule unknown: {kind!r}")
            continue
        if kind == "copy_blocks":
            output = rule.get("output") or rule.get("file")
            if not isinstance(output, str) or not output:
                errors.append(f"{tag}.output must be a non-empty string")
            if not _list_of_str(rule.get("block_ids")):
                errors.append(f"{tag}.block_ids must be a list of strings")
        elif kind == "distribute_blocks":
            assignments = rule.get("assignments")
            if not isinstance(assignments, list):
                errors.append(f"{tag}.assignments must be a list")
            else:
                for j, item in enumerate(assignments):
                    if not isinstance(item, dict):
                        errors.append(f"{tag}.assignments[{j}] must be an object")
                        continue
                    if not isinstance(item.get("block_id"), str):
                        errors.append(f"{tag}.assignments[{j}].block_id must be a string")
                    if not isinstance(item.get("file"), str):
                        errors.append(f"{tag}.assignments[{j}].file must be a string")
            discard = rule.get("discard_block_ids") or []
            if not _list_of_str(discard):
                errors.append(f"{tag}.discard_block_ids must be a list of strings")


def _check_bounded_rewrite(action, errors):
    files = action.get("files")
    if not isinstance(files, list) or not files:
        errors.append("action.files must be a non-empty list for bounded_rewrite")
        return
    for i, item in enumerate(files):
        tag = f"action.files[{i}]"
        if not isinstance(item, dict):
            errors.append(f"{tag} must be an object")
            continue
        if not isinstance(item.get("file"), str) or not item.get("file"):
            errors.append(f"{tag}.file must be a non-empty string")
        if not isinstance(item.get("content"), str):
            errors.append(f"{tag}.content must be a string")


def _canonical_envelope(envelope):
    try:
        return json.dumps(
            envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError):
        return ""


def _resolved_burden_text(value, bodies):
    if not isinstance(value, str):
        return ""
    stripped = value.strip()
    if stripped.startswith(BODY_REF_PREFIX):
        name = stripped[len(BODY_REF_PREFIX):].strip()
        if isinstance(bodies, dict) and isinstance(bodies.get(name), str):
            return bodies[name]
    return value


def measure_protocol_burden(envelope, bodies=None):
    """Return deterministic explicit-protocol burden measurements.

    Anchor fields are measured after resolving a body reference. Reusing the
    same body in two anchor fields therefore counts twice, matching the amount
    of matching work explicitly requested by the envelope.
    """
    canonical = _canonical_envelope(envelope)
    action = envelope.get("action") if isinstance(envelope, dict) else None
    action = action if isinstance(action, dict) else {}
    route = action.get("route")
    ops = action.get("ops") if isinstance(action.get("ops"), list) else []
    rules = action.get("rules") if isinstance(action.get("rules"), list) else []
    files = action.get("files") if isinstance(action.get("files"), list) else []
    local_count = len(ops) if route == ROUTE_LOCAL_PATCH else 0
    bulk_count = len(ops) if route == ROUTE_BULK_PATCH else 0
    if route in (ROUTE_LOCAL_PATCH, ROUTE_BULK_PATCH):
        explicit_count = len(ops)
    elif route == ROUTE_DSL_RULES:
        explicit_count = len(rules)
    elif route == ROUTE_BOUNDED_REWRITE:
        explicit_count = len(files)
    else:
        explicit_count = 0

    anchor_bytes = 0
    if route == ROUTE_LOCAL_PATCH:
        keys = ("old_text", "anchor_text")
    elif route == ROUTE_BULK_PATCH:
        keys = ("old_text", "text")
    else:
        keys = ()
    for op in ops:
        if not isinstance(op, dict):
            continue
        for key in keys:
            if key in op:
                anchor_bytes += len(_resolved_burden_text(op.get(key), bodies).encode("utf-8"))

    explicit_ids = 0
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if rule.get("rule") == "copy_blocks":
            ids = rule.get("block_ids")
            if isinstance(ids, list):
                explicit_ids += sum(1 for value in ids if isinstance(value, str))
        elif rule.get("rule") == "distribute_blocks":
            assignments = rule.get("assignments")
            if isinstance(assignments, list):
                explicit_ids += sum(
                    1 for item in assignments
                    if isinstance(item, dict) and isinstance(item.get("block_id"), str)
                )
            discard = rule.get("discard_block_ids")
            if isinstance(discard, list):
                explicit_ids += sum(1 for value in discard if isinstance(value, str))

    return {
        "local_op_count": local_count,
        "bulk_op_count": bulk_count,
        "explicit_op_count": explicit_count,
        "anchor_bytes": anchor_bytes,
        "envelope_chars": len(canonical),
        "envelope_bytes": len(canonical.encode("utf-8")),
        "explicit_block_id_count": explicit_ids,
    }


def protocol_burden_overages(measurements):
    """Return empirical-threshold crossings as soft telemetry details."""
    measurements = measurements or {}
    return {
        key: {"actual": measurements.get(key), "threshold": threshold}
        for key, threshold in PROTOCOL_BURDEN_LIMITS.items()
        if isinstance(measurements.get(key), (int, float))
        and measurements[key] > threshold
    }


def validate_hybrid_envelope(envelope, bodies=None, editable_filenames=None):
    """Return (errors, warnings). Errors are repair-triggering schema failures."""
    errors, warnings = [], []
    if not isinstance(envelope, dict):
        return ["envelope must be a JSON object"], warnings
    if envelope.get("protocol") != PROTOCOL:
        errors.append(f"protocol must be {PROTOCOL!r}")
    action = envelope.get("action")
    if not isinstance(action, dict):
        _check_plan(envelope.get("plan"), None, errors)
        errors.append("action must be an object")
        return errors, warnings
    route = action.get("route")
    _check_plan(envelope.get("plan"), route, errors)
    if route not in ROUTES:
        errors.append(f"action.route unknown: {route!r}")
        return errors, warnings
    if route == ROUTE_LOCAL_PATCH:
        _check_local(action, errors)
        _check_local_boundary(action, errors, editable_filenames=editable_filenames)
    elif route == ROUTE_BULK_PATCH:
        _check_bulk(action, errors)
        _check_bulk_boundary(action, errors, editable_filenames=editable_filenames)
    elif route == ROUTE_DSL_RULES:
        _check_dsl(action, errors)
    elif route == ROUTE_BOUNDED_REWRITE:
        _check_bounded_rewrite(action, errors)

    return errors, warnings
