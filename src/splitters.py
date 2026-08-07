"""Byte-exact structural splitting for HybridPatch block addressing."""
import hashlib
from dataclasses import dataclass
from typing import List
import re


@dataclass
class Block:
    block_id: int        # sequential index within the document
    start: int           # byte offset (inclusive)
    end: int             # byte offset (exclusive)
    data: bytes          # raw bytes data[start:end]

    @property
    def hash(self) -> str:
        return hashlib.sha1(self.data).hexdigest()[:12]

    @property
    def anchor(self) -> str:
        # content-addressed anchor: what a patch references to relocate a block
        return self.hash


# ---------------------------------------------------------------------------
# byte-line helpers (keep trailing "\n" so concatenation is exact)
# ---------------------------------------------------------------------------
def to_lines(data: bytes) -> List[bytes]:
    """Split into lines, each keeping its trailing b'\\n'. join(lines)==data."""
    out, start = [], 0
    for i, b in enumerate(data):
        if b == 0x0A:  # '\n'
            out.append(data[start:i + 1])
            start = i + 1
    if start < len(data):
        out.append(data[start:])
    elif len(data) == 0:
        out = []
    return out


def _finalize(pieces: List[bytes]) -> List[Block]:
    """Turn an ordered list of byte-pieces into Blocks with offsets."""
    blocks, off = [], 0
    for idx, p in enumerate(pieces):
        blocks.append(Block(idx, off, off + len(p), p))
        off += len(p)
    return blocks


# ---------------------------------------------------------------------------
# fixed  — K-line windows
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# blank  — split on runs of blank lines (blank run attaches to preceding block)
# ---------------------------------------------------------------------------
def _is_blank(line: bytes) -> bool:
    return line.strip(b" \t\r\n") == b""


# ---------------------------------------------------------------------------
# line  — one block per physical line
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# struct — adaptive: blank-record / marker / bracket-indent / intra-line, capped
# ---------------------------------------------------------------------------
CAP = 1200            # byte cap above which a block is recursively sub-split
GIANT_RATIO = 0.30    # one line >= this fraction of doc => giant-line mode


def _blank_segments(lines: List[bytes]) -> List[bytes]:
    """Same policy as split_blank, returns list of byte pieces."""
    pieces, cur, seen = [], [], False
    for ln in lines:
        cur.append(ln)
        if _is_blank(ln):
            if seen:
                pieces.append(b"".join(cur)); cur, seen = [], False
        else:
            seen = True
    if cur:
        pieces.append(b"".join(cur))
    return pieces or [b"".join(lines)]


def _detect_marker(lines: List[bytes]):
    """Find a repeating full-line marker that partitions the doc.
    Returns (marker_bytes, mode) where mode is 'after' (terminator) or
    'before' (initiator prefix), or (None, None)."""
    import collections
    # terminator: exact full lines repeating >=3x and short
    full = collections.Counter(l.strip() for l in lines if l.strip())
    cand = [(k, v) for k, v in full.items() if v >= 3 and len(k) <= 12]
    if cand:
        marker = max(cand, key=lambda kv: kv[1])[0]
        return marker, "after"
    # initiator prefix: line-start token repeating (BEGIN:, level-0 GEDCOM "0 ",
    # XML open tag) — use first token prefix
    prefixes = collections.Counter()
    for l in lines:
        s = l.lstrip()
        if not s.strip():
            continue
        m = re.match(rb"(BEGIN:|0 |<[A-Za-z][\w:]*\b)", s)
        if m:
            prefixes[m.group(1)] += 1
    if prefixes:
        pfx, n = prefixes.most_common(1)[0]
        if n >= 3:
            return pfx, "before"
    return None, None


def _split_by_marker(lines, marker, mode) -> List[bytes]:
    pieces, cur = [], []
    for ln in lines:
        s = ln.strip()
        if mode == "after":
            cur.append(ln)
            if s == marker:
                pieces.append(b"".join(cur)); cur = []
        else:  # before: start a new block when line begins with marker prefix
            if ln.lstrip().startswith(marker) and cur:
                pieces.append(b"".join(cur)); cur = []
            cur.append(ln)
    if cur:
        pieces.append(b"".join(cur))
    return pieces or [b"".join(lines)]


def _split_bracket_indent(piece: bytes) -> List[bytes]:
    """Sub-split a brace/indent-structured byte piece (json/xml/yaml/code).
    Cut after a line where bracket depth returns to its running base level."""
    lines = to_lines(piece)
    if len(lines) <= 1:
        return _split_intraline(piece)
    pieces, cur, depth = [], [], 0
    base = None
    for ln in lines:
        cur.append(ln)
        for c in ln:
            if c in (0x7B, 0x5B):      # { [
                depth += 1
            elif c in (0x7D, 0x5D):    # } ]
                depth -= 1
        if base is None:
            base = depth
        # cut at a line that returns to base depth and ends an element
        stripped = ln.rstrip()
        if depth <= base and stripped[-1:] in (b",", b"}", b"]", b">"):
            pieces.append(b"".join(cur)); cur = []
    if cur:
        pieces.append(b"".join(cur))
    return pieces if len(pieces) > 1 else lines


def _split_intraline(piece: bytes) -> List[bytes]:
    """Sub-split a single very long line by the best repeating delimiter."""
    # candidate delimiters; keep delimiter attached to the LEFT piece
    candidates = [
        rb"(?<=\s)(?=\d+\.\s)",     # chess move numbers "12. "
        rb"(?<=')(?=[A-Z]{2,}\+)",  # edifact-ish segment ends "'"
        rb"(?<=\})(?=,?\s*\{)",     # json objects on one line
        rb",",                       # csv-ish
        rb";\s*",                    # statements
        rb"\t",                      # tabular
    ]
    best = None
    for pat in candidates:
        parts = re.split(pat, piece)
        # re.split with lookarounds keeps content; rebuild to stay exact
        if pat in (rb",", rb";\s*", rb"\t"):
            # these consume the delimiter; rejoin by re-finding
            segs = _split_keep(piece, pat)
        else:
            segs = parts
        segs = [s for s in segs if s]
        if len(segs) >= 4 and b"".join(segs) == piece:
            if best is None or len(segs) > len(best):
                best = segs
    return best if best else [piece]


def _split_keep(piece: bytes, pat: bytes) -> List[bytes]:
    """Split keeping the delimiter attached to the preceding segment."""
    out, last = [], 0
    for m in re.finditer(pat, piece):
        out.append(piece[last:m.end()])
        last = m.end()
    if last < len(piece):
        out.append(piece[last:])
    return out


def _cap_split(piece: bytes) -> List[bytes]:
    """Ensure no piece exceeds CAP; recursively apply the gentlest sub-split."""
    if len(piece) <= CAP:
        return [piece]
    lines = to_lines(piece)
    if len(lines) == 1:
        sub = _split_intraline(piece)
    else:
        sub = _split_bracket_indent(piece)
        if len(sub) <= 1:
            sub = lines  # fall back to line split
    if len(sub) <= 1:
        return [piece]   # irreducible
    out = []
    for s in sub:
        out.extend(_cap_split(s) if len(s) > CAP else [s])
    return out


def split_struct2(data: bytes) -> List[Block]:
    """Split bytes into stable structural blocks with exact full coverage."""
    if not data:
        return []
    lines = to_lines(data)
    total = len(data)
    max_line = max((len(l) for l in lines), default=0)
    if max_line / max(1, total) >= GIANT_RATIO:
        primary = []
        for ln in lines:
            primary.extend(_split_intraline(ln) if len(ln) > CAP else [ln])
    else:
        primary = _blank_segments(lines)
        biggest = max((len(p) for p in primary), default=0)
        marker, mode = _detect_marker(lines)
        use_marker = False
        if biggest / max(1, total) > 0.60:
            use_marker = marker is not None
        elif marker is not None:
            mseg = _split_by_marker(lines, marker, mode)
            # prefer marker only if it segments substantially finer AND uniformly
            if len(mseg) >= 2 * max(1, len(primary)) and len(mseg) >= 4:
                primary = mseg
                use_marker = None  # already applied
        if use_marker is True:
            primary = _split_by_marker(lines, marker, mode)
        elif use_marker is False and biggest / max(1, total) > 0.60:
            primary = _split_bracket_indent(data)
    pieces = []
    for p in primary:
        pieces.extend(_cap_split(p))
    return _finalize(pieces)
