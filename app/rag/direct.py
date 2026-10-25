
"""Answer document questions without an LLM by quoting source sentences."""

import re
from dataclasses import dataclass

from app.rag.chunking import _SENTENCE_END
from app.rag.embeddings import _STOP, _stem


_GENERIC = frozenset(
    (
        "what which who whom when where why how many much does did do "
        "can could would should will please tell show give about policy "
        "policies company document documents say says said state states "
        "stated mention mentions according our your their any there here "
        "need needs want know like get have has had been being also is are "
        "was were the a an for to of in on at by from with and or it this that"
    ).split()
)

_BULLET = re.compile(r"^\s*(?:[-*\u2022]|\d{1,3}[.)])\s+")
_HEADING = re.compile(r"^\s*\d+(?:\.\d+)*[.)]?\s+(.+?)\s*$")

INTRO = "According to the company documents:"
FOOTER = (
    '(Quoted directly from the documents. Ask me to "explain" or '
    '"summarise" if you want an AI-written answer.)'
)

MAX_QUOTE_CHARS = 260
MAX_DIRECT_SENTENCES = 1
_WINDOW_WORDS, _WINDOW_STEP = 32, 16

# Normalize equivalent policy terms to a single canonical term.
_SYNONYM_MAP = {
    "hotel": "hotel",
    "accommodation": "hotel",
    "lodging": "hotel",
    "reimbursement": "reimbursement",
    "reimbursable": "reimbursement",
    "reimbursed": "reimbursement",
    "reimburs": "reimbursement",
    "reimburse": "reimbursement",
    "limit": "limit",
    "maximum": "limit",
    "max": "limit",
    "cap": "limit",
    "expense": "expense",
    "expenses": "expense",
    "cost": "expense",
}


@dataclass
class Direct:
    text: str
    refs: list


def terms(text: str) -> set[str]:
    """Return normalized, meaningful terms."""
    return {
        _stem(word)
        for word in re.findall(r"[a-z0-9][a-z0-9'-]*", text.lower())
        if len(word) > 2 and word not in _STOP and word not in _GENERIC
    }


def _match_terms(text: str) -> set[str]:
    """Normalize terms and their common policy synonyms."""
    return {
        _SYNONYM_MAP.get(word, word)
        for word in terms(text)
    }


def _windows(sentence: str) -> list[str]:
    """Split unusually long text into overlapping verbatim windows."""
    words = sentence.split()

    if len(words) <= _WINDOW_WORDS:
        return [sentence]

    result = []
    for start in range(0, len(words), _WINDOW_STEP):
        piece_words = words[start : start + _WINDOW_WORDS]
        if not piece_words:
            break

        piece = " ".join(piece_words)

        if start > 0:
            piece = "\u2026 " + piece
        if start + _WINDOW_WORDS < len(words):
            piece += " \u2026"

        result.append(piece)

    return result


def sentences(text: str) -> list[str]:
    """Extract sentences, splitting unusually long sentences into windows."""
    result = []

    for line in text.splitlines():
        line = _BULLET.sub("", line).strip()

        if not line or _HEADING.match(line):
            continue

        for sentence in _SENTENCE_END.split(line):
            sentence = sentence.strip()

            if len(sentence) < 25:
                continue

            if len(sentence) > MAX_QUOTE_CHARS:
                result.extend(_windows(sentence))
            else:
                result.append(sentence)

    return result


def _section_sentences(
    text: str,
    fallback_section: str = "",
) -> list[tuple[str, str]]:
    """Return sentences paired with the heading active when they appear."""
    active_section = fallback_section
    result = []

    for line in text.splitlines():
        line = _BULLET.sub("", line).strip()

        if not line:
            continue

        match = _HEADING.match(line)

        if (
            match
            and len(line) <= 100
            and not re.search(r"[.!?]\s*$", line)
        ):
            active_section = line
            continue

        for sentence in _SENTENCE_END.split(line):
            sentence = sentence.strip()

            if len(sentence) < 25:
                continue

            pieces = (
                _windows(sentence)
                if len(sentence) > MAX_QUOTE_CHARS
                else [sentence]
            )

            result.extend((piece, active_section) for piece in pieces)

    return result


def pick(
    question: str,
    passages: list[dict],
    min_overlap: float = 0.5,
    max_sentences: int = MAX_DIRECT_SENTENCES,
) -> Direct | None:
    """Select the best-matching verbatim sentence with its citation."""
    question_terms = _match_terms(question)

    if len(question_terms) < 2 or not passages:
        return None

    records = []

    for rank, passage in enumerate(passages):
        text = passage.get("text") or ""
        fallback_section = passage.get("section") or ""

        for position, (sentence, heading) in enumerate(
            _section_sentences(text, fallback_section)
        ):
            records.append(
                (
                    rank,
                    position,
                    sentence,
                    passage.get("ref", "UNKNOWN"),
                    heading,
                )
            )

    if not records:
        return None

    scored = []

    for rank, position, sentence, ref, heading in records:
        sentence_terms = _match_terms(sentence)
        overlap = len(question_terms & sentence_terms)
        coverage = overlap / len(question_terms)

        heading_terms = _match_terms(heading)
        heading_overlap = len(question_terms & heading_terms)
        heading_bonus = 0.05 if heading_overlap >= 2 else 0.0

        # Require at least two meaningful matching terms.
        if overlap < 2 or coverage < min_overlap:
            continue

        score = coverage + heading_bonus
        scored.append(
            (score, coverage, rank, position, sentence, ref)
        )

    if not scored:
        return None

    scored.sort(
        key=lambda item: (
            -item[0],
            -item[1],
            item[2],
            item[3],
        )
    )

    chosen = []
    seen_sentences = set()

    for _, _, _, _, sentence, ref in scored:
        normalized = sentence.casefold()

        if normalized in seen_sentences:
            continue

        seen_sentences.add(normalized)
        chosen.append((sentence, ref))

        if len(chosen) >= max(1, max_sentences):
            break

    if not chosen:
        return None

    quoted_lines = [
        f"- {sentence} [{ref}]"
        for sentence, ref in chosen
    ]
    refs = list(dict.fromkeys(ref for _, ref in chosen))

    return Direct(
        text="\n".join([INTRO, *quoted_lines, "", FOOTER]),
        refs=refs,
    )
