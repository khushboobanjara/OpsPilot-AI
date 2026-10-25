
"""Structure-aware chunker. Pure functions: text segments in, `Chunk`s out.

Headings are retained with their text. PDF page boundaries are preserved:
a chunk never combines text from different known pages. Overlap is kept
between consecutive chunks on the same page and section.
"""
import re
from dataclasses import dataclass

from app.rag.extract import Segment

_MD_HEADING = re.compile(r"^#{1,6}\s+(\S.*?)\s*#*\s*$")
_NUMBERED = re.compile(
    r"^(?:(?:\d+(?:\.\d+){0,3}[.)]?)|(?:[A-Z][.)])|"
    r"(?:(?:Section|Article|Clause|Chapter|Part)\s+\w+[.:]?))\s+(\S.*)$"
)
_LIST_ITEM = re.compile(r"^\s*(?:[-*•▪◦]|\d{1,3}[.)]|[a-z][.)])\s+\S")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[•\-])")
_BLANK = re.compile(r"\n\s*\n")
MAX_HEADING_CHARS = 100


@dataclass
class Chunk:
    index: int
    text: str
    page: int | None
    section: str | None


@dataclass
class _Unit:
    text: str
    page: int | None
    section: str | None
    heading: bool = False


def heading_of(line: str) -> str | None:
    """Return a heading, avoiding ordinary numbered full-sentence list items."""
    s = line.strip()
    if not s or len(s) > MAX_HEADING_CHARS:
        return None
    m = _MD_HEADING.match(s)
    if m:
        return m.group(1).strip()
    if s[-1] in ".,;:!?" or len(s.split()) > 12:
        return None
    if _NUMBERED.match(s):
        return s
    letters = [c for c in s if c.isalpha()]
    if len(letters) >= 3 and s.upper() == s and len(s) <= 80:
        return s
    return None


def _paragraph_text(lines: list[str]) -> str:
    """Join soft-wrapped lines; retain list items and table rows."""
    out = ""
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        if not out:
            out = ln
        elif (
            _LIST_ITEM.match(ln)
            or " | " in ln
            or " | " in out.rsplit("\n", 1)[-1]
        ):
            out += "\n" + ln
        else:
            out += " " + ln
    return out


def _units(segments: list[Segment]) -> list[_Unit]:
    units: list[_Unit] = []
    section: str | None = None

    for seg in segments:
        for block in _BLANK.split(seg.text):
            pending: list[str] = []
            lines = block.split("\n")

            def flush():
                text = _paragraph_text(pending)
                pending.clear()
                if text:
                    units.append(_Unit(text, seg.page, section))

            for line in lines:
                h = heading_of(line)
                if h:
                    flush()
                    section = h[:200]
                    units.append(_Unit(h, seg.page, section, heading=True))
                else:
                    pending.append(line)
            flush()

    # Glue headings to following text only on the same page.
    merged: list[_Unit] = []
    carry: _Unit | None = None

    for u in units:
        if carry is not None and carry.page != u.page:
            # Do not attach a heading to text from another PDF page.
            merged.append(carry)
            carry = None

        if u.heading:
            carry = _Unit(
                (carry.text + "\n" + u.text) if carry else u.text,
                carry.page if carry else u.page,
                u.section,
                True,
            )
            continue

        if carry:
            u = _Unit(carry.text + "\n" + u.text, u.page, carry.section)
            carry = None

        merged.append(u)

    if carry:
        merged.append(carry)

    return merged


def _hard_split(text: str, size: int) -> list[str]:
    """Pack words up to size, slicing an individual oversized token if needed."""
    out: list[str] = []
    cur = ""

    for word in text.split():
        while len(word) > size:
            if cur:
                out.append(cur)
                cur = ""
            out.append(word[:size])
            word = word[size:]

        if not cur:
            cur = word
        elif len(cur) + 1 + len(word) <= size:
            cur += " " + word
        else:
            out.append(cur)
            cur = word

    if cur:
        out.append(cur)

    return out


def _split_long(text: str, size: int) -> list[str]:
    """Split long text at line, sentence, and word boundaries."""
    if len(text) <= size:
        return [text]

    pieces: list[str] = []
    cur = ""

    for part in (
        p for line in text.split("\n") for p in _SENTENCE_END.split(line)
    ):
        part = part.strip()
        if not part:
            continue

        if len(part) > size:
            if cur:
                pieces.append(cur)
                cur = ""
            pieces.extend(_hard_split(part, size))
        elif not cur:
            cur = part
        elif len(cur) + 1 + len(part) <= size:
            cur += " " + part
        else:
            pieces.append(cur)
            cur = part

    if cur:
        pieces.append(cur)

    return pieces


def _overlap_tail(text: str, overlap: int) -> str:
    """Return a trailing overlap starting at a sentence or word boundary."""
    if overlap <= 0:
        return ""
    if len(text) <= overlap:
        return text.strip()

    tail = text[-overlap:]
    m = _SENTENCE_END.search(tail)
    if m:
        return tail[m.end():].strip()

    sp = tail.find(" ")
    return tail[sp + 1:].strip() if sp != -1 else ""


def chunk_segments(
    segments: list[Segment],
    size: int = 900,
    overlap: int = 150,
    min_chars: int = 40,
) -> list[Chunk]:
    size = max(200, size)
    overlap = max(0, min(overlap, size // 3))

    units: list[_Unit] = []
    for u in _units(segments):
        for piece in _split_long(u.text, size):
            units.append(_Unit(piece, u.page, u.section, u.heading))

    # text, page, section label, ended_by_size, first section, last section
    chunks: list[
        tuple[str, int | None, str | None, bool, str | None, str | None]
    ] = []

    body: list[str] = []
    secs: list[str] = []
    body_len = 0
    page: int | None = None
    section: str | None = None

    def emit(by_size: bool):
        nonlocal body, body_len, secs

        text = "\n\n".join(body).strip()
        if text:
            chunks.append(
                (
                    text,
                    page,
                    (" / ".join(secs)[:200] or None),
                    by_size,
                    secs[0] if secs else None,
                    secs[-1] if secs else None,
                )
            )

        body = []
        body_len = 0
        secs = []

    for u in units:
        sep = 2 if body else 0

        page_changed = (
            body
            and page is not None
            and u.page is not None
            and u.page != page
        )
        size_exceeded = body and body_len + sep + len(u.text) > size
        section_boundary = (
            body
            and u.section != section
            and body_len >= size * 0.35
        )

        if page_changed or size_exceeded or section_boundary:
            # Only size-based splits may overlap, never page/section boundaries.
            by_size = bool(
                size_exceeded
                and not page_changed
                and not section_boundary
            )
            emit(by_size)
            sep = 0

        if not body:
            page, section = u.page, u.section

        body.append(u.text)
        body_len += sep + len(u.text)

        if u.section and u.section not in secs:
            secs.append(u.section)

    if body:
        emit(False)

    # Fold short chunks only into a neighbour on the same page.
    folded: list[list] = []

    for text, pg, label, by_size, first, last in chunks:
        if (
            folded
            and len(text) < min_chars
            and folded[-1][1] == pg
        ):
            folded[-1][0] += "\n\n" + text
            folded[-1][2] = (
                (folded[-1][2] + " / " + label)[:200]
                if label and folded[-1][2]
                else folded[-1][2] or label
            )
            folded[-1][5] = last or folded[-1][5]
            folded[-1][3] = False
            continue

        folded.append([text, pg, label, by_size, first, last])

    out: list[Chunk] = []

    for i, (text, pg, label, _, first, _last) in enumerate(folded):
        prefix = ""

        if i > 0 and overlap:
            previous = folded[i - 1]
            same_page = previous[1] == pg
            same_section = previous[5] == first

            if same_page and same_section and previous[3]:
                prefix = _overlap_tail(previous[0], overlap)

        final_text = (prefix + "\n" + text) if prefix else text
        out.append(Chunk(len(out) + 1, final_text, pg, label))

    return out


def embedding_text(chunk: Chunk, title: str = "") -> str:
    """Build embedding text with title and section context."""
    parts = [title.strip()] if title and title.strip() else []

    if chunk.section and not chunk.text.startswith(chunk.section):
        parts.append(chunk.section)

    parts.append(chunk.text)
    return "\n".join(parts)