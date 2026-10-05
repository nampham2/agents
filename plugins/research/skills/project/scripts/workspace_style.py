"""Advisory style findings for project records.

Pure text in, findings out: nothing here reads a project, raises for bad input or blocks a command.
The check covers form only (sentence length, paragraph length, preferred words, contractions and
three verb forms). The other writing rules need a grammar parser and stay prose-only. Text that is
not the author's wording is exempt: code, diagrams, tables, headings, quotes, comments, hashes, URLs.
"""

from __future__ import annotations

import re
from typing import Any, NamedTuple

LIMIT_STEP = 20
LIMIT_TEXT = 25
MAX_PARAGRAPH_SENTENCES = 6
MAX_FINDINGS = 5

# Plain replacement for each longer or vaguer wording. Keys are lowercase; a key must never be a
# project glossary term (a contract test refuses the overlap). The same table is printed in
# references/writing-rules-extra.md, and a contract test keeps the two equal.
PREFERRED_WORDS: dict[str, str] = {
    "a majority of": "most",
    "a number of": "some",
    "accordingly": "so",
    "acquire": "get",
    "additionally": "also",
    "aforementioned": "this",
    "alter": "change",
    "amend": "change",
    "amongst": "among",
    "approximately": "about",
    "ascertain": "find out",
    "as well as": "and",
    "assist": "help",
    "at the present time": "now",
    "at this time": "now",
    "attempt": "try",
    "be able to": "can",
    "are able to": "can",
    "is able to": "can",
    "is capable of": "can",
    "because of the fact that": "because",
    "commence": "start",
    "concerning": "about",
    "conclude": "end",
    "consequently": "so",
    "construct": "build",
    "currently": "now",
    "demonstrate": "show",
    "depart": "leave",
    "display": "show",
    "due to the fact that": "because",
    "eliminate": "remove",
    "employ": "use",
    "encounter": "find",
    "endeavor": "try",
    "ensure": "make sure",
    "excessive": "too much",
    "facilitate": "help",
    "furthermore": "also",
    "henceforth": "from now",
    "hence": "so",
    "however": "but",
    "in addition": "also",
    "in case of": "if",
    "in order to": "to",
    "in regard to": "about",
    "in the event that": "if",
    "indicate": "show",
    "initiate": "start",
    "inquire": "ask",
    "inform": "tell",
    "locate": "find",
    "make use of": "use",
    "modify": "change",
    "moreover": "also",
    "necessitate": "need",
    "nevertheless": "but",
    "numerous": "many",
    "obtain": "get",
    "occur": "happen",
    "optimal": "best",
    "optimize": "improve",
    "owing to": "because",
    "perform": "do",
    "pertaining to": "about",
    "possess": "have",
    "presently": "now",
    "prevent": "stop",
    "prior to": "before",
    "provide": "give",
    "provided that": "if",
    "purchase": "buy",
    "receive": "get",
    "regarding": "about",
    "reside": "stay",
    "retain": "keep",
    "retrieve": "get",
    "so as to": "to",
    "subsequent to": "after",
    "subsequently": "then",
    "substantial": "large",
    "sufficient": "enough",
    "terminate": "stop",
    "thereafter": "then",
    "therefore": "so",
    "the majority of": "most",
    "thus": "so",
    "transmit": "send",
    "utilization": "use",
    "utilize": "use",
    "utilise": "use",
    "leverage": "use",
    "verify": "check",
    "whilst": "while",
    "with regard to": "about",
}

_FENCE = re.compile(r"^\s*(```|~~~)")
_LIST_ITEM = re.compile(r"^(\s*)(?:(\d+)[.)]|[-*+])\s+(.*)$")
_COMMENT_OPEN = "<!--"
_COMMENT_CLOSE = "-->"
_RULE_LINE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_QUOTED = re.compile(r'"[^"\n]*"|“[^”\n]*”')
_LINK = re.compile(r"!?\[([^\]\n]*)\]\([^)\n]*\)")
_TAG = re.compile(r"<[^>\n]+>")
_EMPHASIS = re.compile(r"(?<!\w)[*_]{1,3}(?=\w)|(?<=\w)[*_]{1,3}(?!\w)")
_ABBREVIATION = re.compile(r"\b(e\.g|i\.e|etc|vs|cf)\.", re.IGNORECASE)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"(\[“])")
_CONTRACTION = re.compile(r"\b[A-Za-z]+(?:n't|'re|'ve|'ll|'d|'m)\b", re.IGNORECASE)
_VERB_FORM = re.compile(r"\b(will|should|could)\b", re.IGNORECASE)
_WORD = re.compile(r"[^\W_]", re.UNICODE)

_PREFERRED_KEYS = sorted(PREFERRED_WORDS, key=len, reverse=True)
_PREFERRED_PATTERN = re.compile(
    r"\b(" + "|".join(r"\s+".join(map(re.escape, key.split())) for key in _PREFERRED_KEYS) + r")\b",
    re.IGNORECASE,
)


class Finding(NamedTuple):
    where: str
    rule: str
    message: str

    def __str__(self) -> str:
        return f"{self.where} [{self.rule}] {self.message}"


def _clean(text: str) -> str:
    """Reduce one line of Markdown to the author's own wording."""
    text = text.replace("\u2019", "'")
    text = _INLINE_CODE.sub("CODE", text)
    text = _LINK.sub(r"\1", text)
    text = _TAG.sub(" ", text)
    text = _QUOTED.sub("QUOTE", text)
    text = _EMPHASIS.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


class _Block(NamedTuple):
    kind: str  # "step" for a numbered list item, otherwise "text"
    is_item: bool
    text: str
    starts: list[tuple[int, int]]  # (offset in text, source line) for each line of the block

    def line_at(self, offset: int) -> int:
        return [line for start, line in self.starts if start <= offset][-1]


def _blocks(markdown: str) -> list[_Block]:
    """Split Markdown into blocks of prose: one paragraph or one list item each.

    Fenced code (also when never closed), headings, tables, quotes, comments and rules end the
    current block and add nothing. A sentence that wraps over several lines stays whole, and each
    line keeps its number so a finding can point at the line where its sentence starts.
    """
    blocks: list[_Block] = []
    lines: list[tuple[int, str]] = []
    kind, is_item = "text", False
    in_fence = in_comment = False

    def flush() -> None:
        nonlocal lines
        cleaned = [(number, _clean(text)) for number, text in lines]
        cleaned = [(number, text) for number, text in cleaned if text]
        if cleaned:
            starts, offset = [], 0
            for number, text in cleaned:
                starts.append((offset, number))
                offset += len(text) + 1
            blocks.append(_Block(kind, is_item, " ".join(text for _, text in cleaned), starts))
        lines = []

    for number, raw in enumerate(markdown.splitlines(), 1):
        if in_comment:
            in_comment = _COMMENT_CLOSE not in raw
            continue
        if in_fence:
            in_fence = not _FENCE.match(raw)
            continue
        stripped = raw.strip()
        if _FENCE.match(raw):
            flush()
            in_fence = True
            continue
        if stripped.startswith(_COMMENT_OPEN):
            flush()
            in_comment = _COMMENT_CLOSE not in stripped
            continue
        if not stripped or stripped.startswith(("#", "|", ">")) or _RULE_LINE.match(raw):
            flush()
            continue
        item = _LIST_ITEM.match(raw)
        if item:
            flush()
            kind, is_item = ("step" if item.group(2) else "text"), True
            lines = [(number, item.group(3))]
        elif lines and (not is_item or raw.startswith((" ", "\t"))):
            lines.append((number, stripped))
        else:
            flush()
            kind, is_item = "text", False
            lines = [(number, stripped)]
    flush()
    return blocks


def _sentences(text: str) -> list[tuple[int, str]]:
    """Each sentence of a block with the offset where it starts."""
    protected = _ABBREVIATION.sub(lambda match: match.group(0).replace(".", "\x00"), text)
    spans, last = [], 0
    for match in _SENTENCE_END.finditer(protected):
        spans.append((last, text[last : match.start()]))
        last = match.end()
    spans.append((last, text[last:]))
    return [(offset, sentence.strip()) for offset, sentence in spans if sentence.strip()]


def _words(sentence: str) -> int:
    return sum(1 for token in sentence.split() if _WORD.search(token))


def _sentence_findings(where: str, sentence: str, limit: int) -> list[Finding]:
    found: list[Finding] = []
    count = _words(sentence)
    if count > limit:
        found.append(Finding(where, "length", f"sentence of {count} words (limit {limit})"))
    seen: set[str] = set()
    for match in _PREFERRED_PATTERN.finditer(sentence):
        key = " ".join(match.group(0).lower().split())
        if key not in seen:
            seen.add(key)
            found.append(Finding(where, "word", f"'{key}': write '{PREFERRED_WORDS[key]}'"))
    for match in _CONTRACTION.finditer(sentence):
        found.append(Finding(where, "contraction", f"'{match.group(0)}': write the full form"))
    for match in _VERB_FORM.finditer(sentence):
        found.append(Finding(where, "verb", f"'{match.group(0).lower()}': use the simple present or an imperative"))
    return found


def document_findings(markdown: str, *, limit: int | None = None, step_limit: int = LIMIT_TEXT) -> list[Finding]:
    """Every finding in one Markdown document, in line order.

    Numbered list items use `step_limit`: a numbered list is a procedure only where the caller says so,
    and a list of requirements is a description. `limit` replaces both limits, for a field that is always
    an instruction or a description.
    """
    found: list[Finding] = []
    for block in _blocks(markdown):
        sentences = _sentences(block.text)
        if not block.is_item and len(sentences) > MAX_PARAGRAPH_SENTENCES:
            found.append(
                Finding(
                    f"L{block.starts[0][1]}",
                    "paragraph",
                    f"paragraph of {len(sentences)} sentences (limit {MAX_PARAGRAPH_SENTENCES})",
                )
            )
        for offset, sentence in sentences:
            sentence_limit = limit or (step_limit if block.kind == "step" else LIMIT_TEXT)
            found.extend(_sentence_findings(f"L{block.line_at(offset)}", sentence, sentence_limit))
    return found


def task_field_findings(tasks: object) -> list[Finding]:
    """Findings in the task fields of arbitrary JSON: a value of the wrong type is skipped."""
    if not isinstance(tasks, list):
        return []
    found: list[Finding] = []
    for task in tasks:
        if not isinstance(task, dict) or not isinstance(task.get("id"), str):
            continue
        for field, limit in (("name", None), ("success_criteria", None), ("verification", LIMIT_STEP)):
            value: Any = task.get(field)
            if isinstance(value, str):
                found.extend(
                    Finding(f"{task['id']} {field}", rule, message)
                    for _, rule, message in document_findings(value, limit=limit)
                )
    return found


def section_only(markdown: str, heading: str) -> str:
    """Keep the lines of one `##` section and blank every other line, so line numbers stay true."""
    wanted = re.compile(rf"^##\s+{re.escape(heading)}\s*$", re.IGNORECASE)
    kept: list[str] = []
    inside = False
    for line in markdown.splitlines():
        if re.match(r"^##\s+", line):
            inside = bool(wanted.match(line))
            kept.append("")
        else:
            kept.append(line if inside else "")
    return "\n".join(kept)


def format_warning(label: str, findings: list[Finding]) -> str | None:
    """One advisory warning for a document: the first findings, then a count of the rest."""
    if not findings:
        return None
    shown = "; ".join(str(finding) for finding in findings[:MAX_FINDINGS])
    rest = len(findings) - MAX_FINDINGS
    more = f"; and {rest} more ({len(findings)} findings)" if rest > 0 else ""
    return f"{label} (form only, never an error): {shown}{more}"
