"""Deterministic executor for HybridPatch."""

from dataclasses import dataclass, field
import fnmatch
import re

from patch_schema import block_id_for, build_block_table
from splitters import split_struct2
from hybrid_schema import (
    ROUTE_LOCAL_PATCH,
    ROUTE_BULK_PATCH,
    ROUTE_DSL_RULES,
    ROUTE_BOUNDED_REWRITE,
    BODY_REF_PREFIX,
    PROTOCOL,
    route_of,
    task_family_of,
    measure_protocol_burden,
    protocol_burden_overages,
    validate_hybrid_envelope,
)


def _resolve_body(value, bodies):
    """Resolve a content field, dereferencing a ``@body:<name>`` sentinel."""
    if isinstance(value, str) and value.startswith(BODY_REF_PREFIX):
        name = value[len(BODY_REF_PREFIX):]
        if bodies and name in bodies:
            return bodies[name], None
        return None, name
    return value, None


def _normalize_body_ref_value(value):
    """Normalize whitespace around a ``@body:`` sentinel and its name."""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(BODY_REF_PREFIX):
            return BODY_REF_PREFIX + stripped[len(BODY_REF_PREFIX):].strip()
    return value


_BODY_FIELDS = {"old_text", "anchor_text", "new_text", "text", "content"}


def _normalize_body_refs(action):
    """Normalize body references in local, bulk, and bounded actions."""
    if not isinstance(action, dict):
        return action
    out = dict(action)
    for key in ("ops", "files"):
        items = action.get(key)
        if isinstance(items, list):
            out[key] = [
                {k: (_normalize_body_ref_value(v) if k in _BODY_FIELDS else v)
                 for k, v in item.items()} if isinstance(item, dict) else item
                for item in items
            ]
    return out


def _unescape_ws(s):
    """Interpret literal over-escaped whitespace as real whitespace."""
    return s.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t").replace("\\r", "\r")


def _ws_pattern(needle, unescape=True):
    """Build a whitespace-tolerant regex. Raw tokens are preferred so backslash-dense formats remain intact."""
    tokens = (_unescape_ws(needle) if unescape else needle).split()
    if not tokens:
        return None
    return r"\s+".join(re.escape(tok) for tok in tokens)


def _file_text_bounds(filename, blocks, edited):
    """Reconstruct a file's current text and per-block byte-offset bounds so a
    file-level match span can be mapped back onto the block structure. Bounds are
    (block_id, start, end) over the joined text; blocks byte-cover the file."""
    parts, bounds, off = [], [], 0
    for b in blocks:
        bid = block_id_for(filename, b.block_id)
        data = edited[bid].decode("utf-8", errors="replace")
        bounds.append((bid, off, off + len(data)))
        parts.append(data)
        off += len(data)
    return "".join(parts), bounds


def _touched_blocks(bounds, start, end):
    """Blocks overlapping [start, end). For a zero-width span (insert), return the
    single block that owns the position (strictly-inside preferred, else the
    boundary block)."""
    if start == end:
        inside = [k for k, (_bid, bs, be) in enumerate(bounds) if bs <= start < be]
        if inside:
            return [inside[0]]
        boundary = [k for k, (_bid, bs, be) in enumerate(bounds) if bs <= start <= be]
        return [boundary[-1]] if boundary else []
    return [k for k, (_bid, bs, be) in enumerate(bounds) if not (be <= start or bs >= end)]


def _apply_file_span(edited, edited_ids, bounds, start, end, replacement):
    """Replace text[start:end] with `replacement` across the overlapping blocks,
    editing only those blocks (undeclared blocks stay byte-identical, preserving
    the core invariant). Returns True on success."""
    touched = _touched_blocks(bounds, start, end)
    if not touched:
        return False
    fi, li = touched[0], touched[-1]
    fbid, fbs, _fbe = bounds[fi]
    lbid, lbs, _lbe = bounds[li]
    ftext = edited[fbid].decode("utf-8", errors="replace")
    ltext = edited[lbid].decode("utf-8", errors="replace")
    if fi == li:
        new_text = ftext[:start - fbs] + replacement + ftext[end - fbs:]
        edited[fbid] = new_text.encode("utf-8")
        edited_ids.add(fbid)
    else:
        edited[fbid] = (ftext[:start - fbs] + replacement).encode("utf-8")
        edited_ids.add(fbid)
        for k in touched[1:-1]:
            edited[bounds[k][0]] = b""
            edited_ids.add(bounds[k][0])
        edited[lbid] = ltext[end - lbs:].encode("utf-8")
        edited_ids.add(lbid)
    return True


def _collect_local_matches(candidate_files, file_blocks, edited, needle):
    """Find exact and whitespace-tolerant whole-file matches for an anchor."""
    exact, ws = [], []
    file_texts = []
    for filename in candidate_files:
        blocks = file_blocks.get(filename, [])
        if not blocks:
            continue
        text, bounds = _file_text_bounds(filename, blocks, edited)
        file_texts.append((filename, text, bounds))
        s = 0
        while True:
            pos = text.find(needle, s)
            if pos < 0:
                break
            exact.append((filename, bounds, pos, pos + len(needle)))
            s = pos + max(1, len(needle))
    pats = [p for p in dict.fromkeys(
        (_ws_pattern(needle, unescape=False), _ws_pattern(needle))) if p]
    for pat in pats:
        for filename, text, bounds in file_texts:
            for m in re.finditer(pat, text):
                ws.append((filename, bounds, m.start(), m.end()))
        if ws:
            break
    return exact, ws


@dataclass
class HybridExecLog:
    ops_total: int = 0
    ops_accepted: int = 0
    ops_rejected: int = 0
    op_details: list = field(default_factory=list)
    edited_block_ids: list = field(default_factory=list)
    used_emit_file: bool = False
    n_source_blocks: int = 0
    survived_blocks: int = 0
    bytes_preserved: int = 0
    bytes_emitted: int = 0
    bytes_total_output: int = 0
    preservation_violations: int = 0
    noop: bool = False
    error: str = None
    hybrid: dict = field(default_factory=dict)

    @property
    def op_accept_rate(self):
        n = self.ops_accepted + self.ops_rejected
        return (self.ops_accepted / n) if n else 1.0

    @property
    def survival_rate(self):
        return (self.survived_blocks / self.n_source_blocks) if self.n_source_blocks else 1.0

    @property
    def preservation_rate(self):
        return (self.bytes_preserved / self.bytes_total_output) if self.bytes_total_output else 1.0

    def reject_reasons(self):
        return [d for d in self.op_details if d.get("status") == "rejected"]

    def to_dict(self):
        return {
            "ops_total": self.ops_total,
            "ops_accepted": self.ops_accepted,
            "ops_rejected": self.ops_rejected,
            "op_accept_rate": round(self.op_accept_rate, 4),
            "edited_blocks": len(self.edited_block_ids),
            "used_emit_file": self.used_emit_file,
            "n_source_blocks": self.n_source_blocks,
            "survived_blocks": self.survived_blocks,
            "survival_rate": round(self.survival_rate, 4),
            "bytes_preserved": self.bytes_preserved,
            "bytes_emitted": self.bytes_emitted,
            "bytes_total_output": self.bytes_total_output,
            "preservation_rate": round(self.preservation_rate, 4),
            "preservation_violations": self.preservation_violations,
            "noop": self.noop,
            "reject_reasons": self.reject_reasons(),
            "error": self.error,
            "hybrid": self.hybrid,
        }


def _matches_any_target(filename, target_filenames):
    if not target_filenames:
        return True
    for t in target_filenames:
        if ("*" in t or "?" in t):
            if fnmatch.fnmatch(filename, t):
                return True
        elif filename == t:
            return True
    return False


def _splitlines_keepends(text):
    if text == "":
        return []
    return text.splitlines(True)


def _accept(log, idx, op, **extra):
    log.ops_accepted += 1
    row = {"i": idx, "op": op, "status": "accepted"}
    row.update(extra)
    log.op_details.append(row)


def _reject(log, idx, op, reason, **extra):
    log.ops_rejected += 1
    row = {"i": idx, "op": op, "status": "rejected", "reason": reason}
    row.update(extra)
    log.op_details.append(row)


def _scope_files(op, file_blocks):
    scope = op.get("scope")
    if isinstance(scope, list) and scope:
        return [f for f in scope if f in file_blocks]
    if isinstance(op.get("file"), str) and op.get("file") in file_blocks:
        return [op.get("file")]
    return list(file_blocks)


def _spans_conflict(s1, e1, s2, e2):
    """Whether two file-text spans cannot both be applied in one pass.
    Zero-width spans (inserts) at the same position compose deterministically
    (op order = reading order) and boundary-touching inserts are fine; an
    insert strictly inside a replaced/deleted span, or two overlapping
    non-empty spans, conflict."""
    if s1 == e1 and s2 == e2:
        return False
    if s1 == e1:
        return s2 < s1 < e2
    if s2 == e2:
        return s1 < s2 < e1
    return s1 < e2 and s2 < e1


def _run_local(action, edited, file_blocks, log, bodies=None):
    """Apply snapshot-resolved, non-overlapping local operations right-to-left."""
    ops = action.get("ops") or []
    log.ops_total += len(ops)
    generated = 0
    edited_ids = set()
    accepted = []      # (idx, t, filename, start, end, replacement)
    spans_by_file = {}
    for idx, op in enumerate(ops):
        t = op.get("op") if isinstance(op, dict) else None
        if t not in ("replace", "delete", "insert"):
            _reject(log, idx, t, "unknown_local_op")
            continue
        candidate_files = [op.get("file")]
        raw_needle = op.get("anchor_text") if t == "insert" else op.get("old_text")
        needle, unresolved = _resolve_body(raw_needle, bodies)
        if unresolved is not None:
            _reject(log, idx, t, "body_ref_not_found", ref=unresolved)
            continue
        if not isinstance(needle, str) or not needle:
            _reject(log, idx, t, "empty_anchor")
            continue
        exact, ws = _collect_local_matches(candidate_files, file_blocks, edited, needle)
        occ = op.get("occurrence")
        if occ is not None:
            if occ < 1 or occ > len(exact):
                _reject(log, idx, t, "occurrence_out_of_range", matches=len(exact))
                continue
            chosen = exact[occ - 1]
        elif len(exact) == 1:
            chosen = exact[0]
        elif len(exact) > 1:
            _reject(log, idx, t, "not_unique", matches=len(exact))
            continue
        elif len(ws) == 1:
            chosen = ws[0]
        else:
            _reject(log, idx, t, "not_unique" if ws else "not_found", matches=len(ws))
            continue
        filename, _bounds, start, end = chosen
        if t == "insert":
            position = op.get("position")
            if position == "before":
                start, end = start, start
            elif position == "after":
                start, end = end, end
            else:
                _reject(log, idx, t, "bad_position")
                continue
        if t in ("insert", "replace"):
            replacement, unresolved = _resolve_body(op.get("new_text", ""), bodies)
            if unresolved is not None:
                _reject(log, idx, t, "body_ref_not_found", ref=unresolved)
                continue
        else:
            replacement = ""
        prior = spans_by_file.setdefault(filename, [])
        if any(_spans_conflict(ps, pe, start, end) for ps, pe in prior):
            _reject(log, idx, t, "overlapping_span", file=filename)
            continue
        prior.append((start, end))
        accepted.append((idx, t, filename, start, end, replacement))
        if t in ("insert", "replace"):
            generated += len(replacement.encode("utf-8"))
        _accept(log, idx, t, file=filename)
    # Apply right-to-left (start desc; at equal start apply the non-empty span
    # first, then co-located inserts in descending op order so earlier ops'
    # text ends up leftmost). Positions left of every already-applied span are
    # untouched, so snapshot coordinates stay valid; bounds are recomputed per
    # span because earlier applications may have restructured block contents.
    accepted.sort(key=lambda a: (-a[3], 0 if a[3] != a[4] else 1, -a[0]))
    for _idx, _t, filename, start, end, replacement in accepted:
        blocks = file_blocks.get(filename, [])
        _text, bounds = _file_text_bounds(filename, blocks, edited)
        _apply_file_span(edited, edited_ids, bounds, start, end, replacement)
    return edited_ids, generated


def _bulk_file_matches(edited, file_blocks, files, needle):
    """Find non-overlapping literal matches at whole-file scope."""
    matches = []
    for f in files:
        blocks = file_blocks.get(f, [])
        if not blocks:
            continue
        text, _bounds = _file_text_bounds(f, blocks, edited)
        s = 0
        while True:
            pos = text.find(needle, s)
            if pos < 0:
                break
            matches.append((f, pos, pos + len(needle)))
            s = pos + max(1, len(needle))
    return matches


def _apply_file_spans_rtl(edited, edited_ids, file_blocks, spans):
    """Apply (filename, start, end, replacement) spans right-to-left per file.
    Bounds are recomputed before each application because earlier applications
    may restructure block contents; positions left of every already-applied
    span stay valid, so descending start order keeps coordinates correct."""
    for filename, start, end, replacement in sorted(spans, key=lambda s: (s[0], -s[1])):
        blocks = file_blocks.get(filename, [])
        _text, bounds = _file_text_bounds(filename, blocks, edited)
        _apply_file_span(edited, edited_ids, bounds, start, end, replacement)


def _run_bulk(action, edited, file_blocks, log, bodies=None):
    """Apply snapshot-resolved bulk operations with deterministic overlap handling."""
    ops = action.get("ops") or []
    log.ops_total += len(ops)
    generated = 0
    edited_ids = set()
    spans_by_file = {}  # filename -> [(start, end, replacement, is_line_delete)]
    for idx, op in enumerate(ops):
        t = op.get("op") if isinstance(op, dict) else None
        files = _scope_files(op, file_blocks)
        if t == "replace_all":
            old, old_unresolved = _resolve_body(op.get("old_text"), bodies)
            if old_unresolved is not None:
                _reject(log, idx, t, "body_ref_not_found", ref=old_unresolved)
                continue
            if not isinstance(old, str) or not old:
                _reject(log, idx, t, "empty_old_text")
                continue
            new, unresolved = _resolve_body(op.get("new_text", ""), bodies)
            if unresolved is not None:
                _reject(log, idx, t, "body_ref_not_found", ref=unresolved)
                continue
            matches = _bulk_file_matches(edited, file_blocks, files, old)
            n = len(matches)
            exact, cmin = op.get("expected_count_exact"), op.get("expected_count_min")
            if n == 0:
                _reject(log, idx, t, "match_zero", scope=files)
                continue
            if isinstance(exact, int) and n != exact:
                _reject(log, idx, t, f"expected_count_mismatch_{n}vs{exact}", scope=files)
                continue
            if isinstance(cmin, int) and n < cmin:
                _reject(log, idx, t, f"expected_count_below_min_{n}vs{cmin}", scope=files)
                continue
            spans = [(f, s, e, new, False) for f, s, e in matches]
            accept_key, accept_val = "match_count", n
        elif t == "delete_lines_containing":
            needle, needle_unresolved = _resolve_body(op.get("text"), bodies)
            if needle_unresolved is not None:
                _reject(log, idx, t, "body_ref_not_found", ref=needle_unresolved)
                continue
            if not isinstance(needle, str) or not needle:
                _reject(log, idx, t, "empty_text")
                continue
            spans = []
            deleted = 0
            for f in files:
                blocks = file_blocks.get(f, [])
                if not blocks:
                    continue
                text, _bounds = _file_text_bounds(f, blocks, edited)
                off = 0
                for line in _splitlines_keepends(text):
                    if needle in line:
                        spans.append((f, off, off + len(line), "", True))
                        deleted += 1
                    off += len(line)
            if deleted == 0:
                _reject(log, idx, t, "match_zero", scope=files)
                continue
            cmin = op.get("expected_count_min")
            if isinstance(cmin, int) and deleted < cmin:
                _reject(log, idx, t, f"expected_count_below_min_{deleted}vs{cmin}", scope=files)
                continue
            new = ""
            accept_key, accept_val = "deleted_lines", deleted
        else:
            _reject(log, idx, t, "unknown_bulk_op")
            continue
        conflict = False
        dup_skip = set()   # indices of THIS op's spans to drop (dup / subsumed)
        prior_drop = {}    # filename -> indices of accepted spans subsumed by THIS op
        for j, (f, s, e, _repl, is_del) in enumerate(spans):
            for k, (ps, pe, _prepl, p_is_del) in enumerate(spans_by_file.get(f, [])):
                if k in prior_drop.get(f, set()):
                    continue
                if is_del and p_is_del and (s, e) == (ps, pe):
                    # equal to an already-accepted span, so it inherits that
                    # span's (clean) conflict status — just don't apply twice
                    dup_skip.add(j)
                    break
                if _spans_conflict(ps, pe, s, e):
                    if p_is_del and not is_del and ps <= s and e <= pe:
                        # C1: this replace span sits entirely inside an accepted
                        # delete-lines span — the line is going away anyway
                        dup_skip.add(j)
                        break
                    if is_del and not p_is_del and s <= ps and pe <= e:
                        # C1: an accepted replace span sits entirely inside this
                        # delete-lines span — swallow it and keep scanning
                        prior_drop.setdefault(f, set()).add(k)
                        continue
                    conflict = True
                    break
            if conflict:
                break
        if conflict:
            # op rejected wholesale: discard any tentative subsumptions
            _reject(log, idx, t, "overlapping_span", scope=files)
            continue
        for f, ks in prior_drop.items():
            spans_by_file[f] = [sp for k, sp in enumerate(spans_by_file[f]) if k not in ks]
        for j, (f, s, e, repl, is_del) in enumerate(spans):
            if j in dup_skip:
                continue
            spans_by_file.setdefault(f, []).append((s, e, repl, is_del))
        if t == "replace_all":
            generated += n * len(new.encode("utf-8"))
        _accept(log, idx, t, scope=files, **{accept_key: accept_val})
    all_spans = [(f, s, e, repl)
                 for f, lst in spans_by_file.items() for (s, e, repl, _d) in lst]
    _apply_file_spans_rtl(edited, edited_ids, file_blocks, all_spans)
    return edited_ids, generated


def _run_dsl(action, edited, block_table, log):
    outputs = {}
    included = []
    consumed = set()
    discarded = set()
    route_violations = []
    rules = action.get("rules") or []
    log.ops_total += len(rules)
    for idx, rule in enumerate(rules):
        kind = rule.get("rule") if isinstance(rule, dict) else None
        if kind == "copy_blocks":
            out = rule.get("output") or rule.get("file")
            bids = rule.get("block_ids") or []
            bad = [bid for bid in bids if bid not in edited]
            if bad:
                _reject(log, idx, kind, "unknown_block", block_ids=bad[:10])
                continue
            outputs[out] = b"".join(edited[bid] for bid in bids).decode("utf-8", errors="replace")
            included.extend(bids)
            consumed.update(bids)
            _accept(log, idx, kind, file=out, blocks=len(bids))
        elif kind == "distribute_blocks":
            assignments = rule.get("assignments") or []
            discard = list(rule.get("discard_block_ids") or [])
            grouped = {}
            seen = []
            for item in assignments:
                bid = item.get("block_id") if isinstance(item, dict) else None
                dest = item.get("file") if isinstance(item, dict) else None
                if bid not in edited or not dest:
                    route_violations.append("distribution_unknown_block_or_file")
                    continue
                grouped.setdefault(dest, []).append(bid)
                seen.append(bid)
            for bid in discard:
                if bid in edited:
                    discarded.add(bid)
                    seen.append(bid)
                else:
                    route_violations.append("distribution_unknown_discard_block")
            duplicates = sorted({bid for bid in seen if seen.count(bid) > 1})
            missing = sorted(set(block_table) - set(seen))
            if duplicates:
                route_violations.append("distribution_duplicate_block")
            # Unassigned blocks stay in their source files via copy-forward.
            for dest, bids in grouped.items():
                outputs[dest] = b"".join(edited[bid] for bid in bids).decode("utf-8", errors="replace")
                included.extend(bids)
                consumed.update(bids)
            _accept(log, idx, kind, outputs=sorted(grouped), discarded=len(discarded),
                    missing=len(missing), duplicates=len(duplicates))
        else:
            _reject(log, idx, kind, "unknown_dsl_rule")
    return outputs, included, consumed, discarded, route_violations


def _run_bounded(action, log, bodies=None):
    outputs = {}
    generated = 0
    files = action.get("files") or []
    log.ops_total += len(files)
    for idx, item in enumerate(files):
        filename = item.get("file") if isinstance(item, dict) else None
        raw_content = item.get("content") if isinstance(item, dict) else None
        content, unresolved = _resolve_body(raw_content, bodies)
        if unresolved is not None:
            _reject(log, idx, "bounded_rewrite", "body_ref_not_found", file=filename, ref=unresolved)
            continue
        if not filename or not isinstance(content, str) or filename in outputs:
            _reject(log, idx, "bounded_rewrite", "bad_or_duplicate_file", file=filename)
            continue
        outputs[filename] = content
        generated += len(content.encode("utf-8"))
        _accept(log, idx, "bounded_rewrite", file=filename, generated_bytes=len(content.encode("utf-8")))
    return outputs, generated


def apply_hybrid(source_context, envelope, target_filenames=None, splitter_fn=split_struct2,
                 bodies=None):
    log = HybridExecLog()
    block_table, file_blocks = build_block_table(source_context, splitter_fn)
    log.n_source_blocks = len(block_table)
    edited = dict(block_table)
    edited_ids = set()
    included_ids = []
    consumed_ids = set()
    discarded_ids = set()
    generated_bytes = 0

    burden = measure_protocol_burden(envelope, bodies=bodies)
    overages = protocol_burden_overages(burden)
    errors, warnings = validate_hybrid_envelope(
        envelope, bodies=bodies, editable_filenames=source_context.keys())
    route = route_of(envelope)
    log.hybrid.update({
        "schema_errors": errors,
        "schema_warnings": warnings,
        "route": route,
        "task_family": task_family_of(envelope),
        "protocol": PROTOCOL,
        "route_violations": [],
        "protocol_burden": burden,
        "protocol_burden_exceeded": bool(overages),
        "protocol_burden_overages": overages,
    })
    if errors:
        log.error = "schema_error"
        return {}, log

    action = _normalize_body_refs(envelope.get("action") or {})
    output = {}
    if route == ROUTE_LOCAL_PATCH:
        edited_now, generated = _run_local(
            action, edited, file_blocks, log, bodies=bodies)
        edited_ids.update(edited_now)
        generated_bytes += generated
    elif route == ROUTE_BULK_PATCH:
        edited_now, generated = _run_bulk(
            action, edited, file_blocks, log, bodies=bodies)
        edited_ids.update(edited_now)
        generated_bytes += generated
    elif route == ROUTE_DSL_RULES:
        dsl_outputs, inc, consumed, discarded, violations = _run_dsl(
            action, edited, block_table, log)
        output.update(dsl_outputs)
        included_ids.extend(inc)
        consumed_ids.update(consumed)
        discarded_ids.update(discarded)
        log.hybrid["route_violations"] = sorted(set(violations))
    elif route == ROUTE_BOUNDED_REWRITE:
        bounded_outputs, generated = _run_bounded(action, log, bodies=bodies)
        output.update(bounded_outputs)
        generated_bytes += generated

    covered = set(output)
    for filename, blocks in file_blocks.items():
        if filename in covered:
            continue
        if not _matches_any_target(filename, target_filenames):
            continue
        remaining = [
            b for b in blocks
            if block_id_for(filename, b.block_id) not in consumed_ids
            and block_id_for(filename, b.block_id) not in discarded_ids
        ]
        if not remaining:
            continue
        bids = [block_id_for(filename, b.block_id) for b in remaining]
        output[filename] = b"".join(edited[bid] for bid in bids).decode("utf-8", errors="replace")
        included_ids.extend(bids)

    if target_filenames:
        output = {k: v for k, v in output.items() if _matches_any_target(k, target_filenames)}

    log.edited_block_ids = sorted(edited_ids)
    log.bytes_total_output = sum(len(v.encode("utf-8")) for v in output.values())
    survived = {bid for bid in included_ids if bid in block_table and bid not in edited_ids}
    log.survived_blocks = len(survived)
    log.bytes_preserved = sum(len(block_table[bid]) for bid in survived)
    log.preservation_violations = sum(
        1 for bid in block_table if bid not in edited_ids and edited[bid] != block_table[bid]
    )
    log.bytes_emitted = generated_bytes
    log.noop = (output == source_context)
    log.hybrid["bytes_copied_from_source"] = max(0, log.bytes_total_output - generated_bytes)
    log.hybrid["bytes_generated_by_model"] = generated_bytes
    log.hybrid["generated_byte_ratio"] = (
        round(generated_bytes / log.bytes_total_output, 4) if log.bytes_total_output else None
    )
    log.hybrid["bounded_rewrite"] = route == ROUTE_BOUNDED_REWRITE
    log.hybrid["body_refs_supplied"] = len(bodies) if bodies else 0
    log.hybrid["body_ref_unresolved"] = sum(
        1 for d in log.op_details if d.get("status") == "rejected" and d.get("reason") == "body_ref_not_found"
    )
    return output, log
