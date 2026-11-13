"""Treat every piece of non-user text as data, never as instructions.

Page text, email bodies, chat messages, accessible names and model output all
reach Xenon from people who are not Roshan. This module holds the small,
dependency-free helpers that keep that text inert:

* ``strip_invisible`` removes the characters attackers use to hide or reorder
  text (zero-width, bidi controls, tag characters) and applies NFKC, so
  "Se\\u200bnd" and full-width "\uff33\uff45\uff4e\uff44" compare equal to "Send".
* ``skeleton`` folds the common Latin look-alikes from Greek and Cyrillic, so
  a button named with a Greek capital Alpha still matches the pattern for
  its Latin twin.
* ``wrap`` delimits untrusted text for a model with a tag the text itself
  cannot close.
* ``redact_secrets`` removes key, token, JWT, PEM and OTP shapes before any
  cloud call.
* ``exact_span`` checks that a span a model extracted really is Roshan's own
  words, and that it did not drop a negation next to it.

Nothing here imports anything heavy, so it is safe to use on the hot path.
"""
from __future__ import annotations

import re
import unicodedata

# ---------------------------------------------------------------------------
# Invisible and bidi characters
# ---------------------------------------------------------------------------

# Explicit list on top of the Unicode "Cf" (format) category. Some invisible
# characters are letters or marks by category (Hangul fillers, the combining
# grapheme joiner, variation selectors), so the category test alone misses
# them.
_EXTRA_INVISIBLE = frozenset(
    {
        "\u00ad",  # soft hyphen (Cf, listed for clarity)
        "\u034f",  # combining grapheme joiner (Mn)
        "\u115f",  # Hangul choseong filler (Lo)
        "\u1160",  # Hangul jungseong filler (Lo)
        "\u17b4",  # Khmer vowel inherent aq (Mn)
        "\u17b5",  # Khmer vowel inherent aa (Mn)
        "\u180e",  # Mongolian vowel separator
        "\u2800",  # Braille pattern blank (renders as nothing)
        "\u3164",  # Hangul filler (Lo)
        "\uffa0",  # half-width Hangul filler (Lo)
        "\ufffc",  # object replacement character
    }
)

_KEEP_CONTROLS = frozenset({"\n", "\t"})


def _is_invisible(char: str) -> bool:
    if char in _KEEP_CONTROLS:
        return False
    if char in _EXTRA_INVISIBLE:
        return True
    code = ord(char)
    # Variation selectors (FE00-FE0F, E0100-E01EF) and tag characters
    # (E0000-E007F, used for "ASCII smuggling" of hidden prompts).
    if 0xFE00 <= code <= 0xFE0F or 0xE0100 <= code <= 0xE01EF or 0xE0000 <= code <= 0xE007F:
        return True
    category = unicodedata.category(char)
    # Cf covers ZWSP/ZWNJ/ZWJ, LRM/RLM, the bidi embeddings, overrides and
    # isolates, word joiner, BOM and the invisible math operators. Cc covers
    # C0/C1 controls (kept: newline and tab). Co/Cn are private-use and
    # unassigned code points, which have no business in a button name.
    return category in {"Cf", "Cc", "Co", "Cn", "Cs"}


def has_invisible(text: str) -> bool:
    """True when ``text`` carries invisible, bidi or control characters."""
    return any(_is_invisible(char) for char in str(text or ""))


_BIDI_REORDER = frozenset("\u202e\u2067\u200f\u061c\u202b")  # RLO, RLI, RLM, ALM, RLE


def has_bidi_reordering(text: str) -> bool:
    """True when ``text`` uses right-to-left controls that reorder display.

    "\\u202edneS" displays as "Send"; callers that match names should also
    try the reversed string.
    """
    return any(char in _BIDI_REORDER for char in str(text or ""))


def has_tag_characters(text: str) -> bool:
    """True when ``text`` carries Unicode tag characters (E0000-E007F), the
    "ASCII smuggling" channel for hidden prompts aimed at another AI."""
    return any(0xE0000 <= ord(char) <= 0xE007F for char in str(text or ""))


def strip_invisible(text: str) -> str:
    """Remove invisible, bidi and control characters, then apply NFKC.

    Carriage returns become newlines so "\\r\\n" cannot hide a second line.
    The pass runs before and after NFKC because normalisation can itself
    produce a filler character from a compatibility form.
    """
    if not text:
        return ""
    text = str(text).replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(char for char in text if not _is_invisible(char))
    text = unicodedata.normalize("NFKC", text)
    return "".join(char for char in text if not _is_invisible(char))


# ---------------------------------------------------------------------------
# Confusable skeletons
# ---------------------------------------------------------------------------

# A compact subset of the Unicode TR39 confusables table: the Greek and
# Cyrillic letters that render identically to Latin ones in UI fonts, plus
# the digit/letter swaps TR39 also folds. Enough to stop "\u0405\u0435nd" or "\u0391mma"
# from slipping past a Latin pattern; not a full TR39 implementation.
_CONFUSABLES = {
    # Cyrillic lower case
    "\u0430": "a", "\u0432": "b", "\u0435": "e", "\u0451": "e", "\u043e": "o", "\u0440": "p", "\u0441": "c",
    "\u0443": "y", "\u0445": "x", "\u0456": "i", "\u0457": "i", "\u0458": "j", "\u0455": "s", "\u0501": "d",
    "\u051b": "q", "\u051d": "w", "\u04bb": "h", "\u04cf": "l", "\u043a": "k", "\u043c": "m", "\u043d": "h",
    "\u0442": "t", "\u04af": "y",
    # Cyrillic upper case
    "\u0410": "A", "\u0412": "B", "\u0415": "E", "\u0401": "E", "\u041a": "K", "\u041c": "M", "\u041d": "H",
    "\u041e": "O", "\u0420": "P", "\u0421": "C", "\u0422": "T", "\u0423": "Y", "\u0425": "X", "\u0405": "S",
    "\u0406": "I", "\u0407": "I", "\u0408": "J", "\u0500": "D", "\u051a": "Q", "\u051c": "W", "\u04ba": "H",
    "\u04c0": "I", "\u04ae": "Y",
    # Greek lower case
    "\u03b1": "a", "\u03b2": "b", "\u03b3": "y", "\u03b5": "e", "\u03b7": "n", "\u03b9": "i", "\u03ba": "k",
    "\u03bd": "v", "\u03bf": "o", "\u03c1": "p", "\u03c4": "t", "\u03c5": "u", "\u03c7": "x", "\u03c9": "w",
    "\u03f2": "c", "\u03f3": "j",
    # Greek upper case
    "\u0391": "A", "\u0392": "B", "\u0395": "E", "\u0396": "Z", "\u0397": "H", "\u0399": "I", "\u039a": "K",
    "\u039c": "M", "\u039d": "N", "\u039f": "O", "\u03a1": "P", "\u03a4": "T", "\u03a5": "Y", "\u03a7": "X",
    "\u03f9": "C",
    # Latin look-alikes outside ASCII
    "\u0131": "i", "\u0237": "j", "\u0142": "l", "\u0251": "a", "\u0261": "g",
    # Apostrophe look-alikes, so "Don\u2019t Save" meets "don't save"
    "\u2019": "'", "\u2018": "'", "\u02bc": "'", "\u02b9": "'", "\u2032": "'", "\u00b4": "'",
    # Armenian letters that pass for Latin in most fonts
    "\u0578": "n", "\u057d": "u", "\u0585": "o", "\u0570": "h", "\u0581": "g", "\u0566": "q", "\u0561": "w",
}

_SCRIPT_PREFIXES = ("LATIN", "GREEK", "CYRILLIC", "ARMENIAN", "HEBREW", "ARABIC")


def skeleton(text: str) -> str:
    """Return a casefolded, confusable-folded form for pattern matching.

    Used only for comparisons. It is never shown to anyone, because folding
    loses information on purpose.
    """
    cleaned = strip_invisible(text)
    folded = "".join(_CONFUSABLES.get(char, char) for char in cleaned)
    # Strip accents so "S\u00e9nd" matches "send" too.
    decomposed = unicodedata.normalize("NFKD", folded)
    without_marks = "".join(c for c in decomposed if not unicodedata.combining(c))
    return without_marks.casefold()


def mixed_script(text: str) -> bool:
    """True when one word mixes Latin with Greek or Cyrillic letters."""
    for word in re.findall(r"\w+", strip_invisible(text)):
        scripts = set()
        for char in word:
            if not char.isalpha():
                continue
            try:
                name = unicodedata.name(char)
            except ValueError:
                continue
            for prefix in _SCRIPT_PREFIXES:
                if name.startswith(prefix):
                    scripts.add(prefix)
                    break
        if len(scripts) > 1:
            return True
    return False


# ---------------------------------------------------------------------------
# Delimiting untrusted text for a model
# ---------------------------------------------------------------------------

# Angle-bracket look-alikes a model might read as a tag delimiter. NFKC
# already folds the full-width and small forms to ASCII, which are then
# escaped like any other bracket.
_ANGLE_LOOKALIKES = {
    "\u2039": "&lt;", "\u203a": "&gt;",   # single guillemets
    "\u2329": "&lt;", "\u232a": "&gt;",   # angle brackets
    "\u27e8": "&lt;", "\u27e9": "&gt;",   # mathematical angle brackets
    "\u3008": "&lt;", "\u3009": "&gt;",   # CJK angle brackets
    "\u276c": "&lt;", "\u276d": "&gt;",   # ornament brackets
    "\u02c2": "&lt;", "\u02c3": "&gt;",   # modifier arrowheads
}

_SOURCE_RE = re.compile(r"[^a-z0-9_.:-]")


def _escape(text: str) -> str:
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return "".join(_ANGLE_LOOKALIKES.get(char, char) for char in text)


def wrap(text: str, source: str, limit: int | None = None) -> str:
    """Delimit ``text`` as untrusted data from ``source``.

    The body is stripped of invisible characters and every ``&``, ``<`` and
    ``>`` (plus their look-alikes) is escaped, so no sequence inside the body
    can close the wrapper or open a fake one. ``source`` is reduced to a
    short safe token so it cannot break out of its attribute either.
    """
    body = strip_invisible(text or "")
    if limit is not None and limit >= 0 and len(body) > limit:
        body = body[:limit] + " [truncated]"
    label = _SOURCE_RE.sub("", strip_invisible(source or "").casefold())[:40] or "unknown"
    return f'<untrusted source="{label}">\n{_escape(body)}\n</untrusted>'


# ---------------------------------------------------------------------------
# Secret redaction
# ---------------------------------------------------------------------------

_PEM_BLOCK = re.compile(
    r"-----BEGIN [A-Z0-9 ]{0,40}-----.*?(?:-----END [A-Z0-9 ]{0,40}-----|\Z)",
    re.DOTALL,
)

# Order matters: specific prefixes before the generic ``sk-`` shape.
_TOKEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}")),
    ("groq_key", re.compile(r"\bgsk_[A-Za-z0-9]{16,}")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}")),
    ("openai_key", re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,}")),
    ("stripe_key", re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("google_key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}")),
    ("aws_key", re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{22,})")),
    ("slack_token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    ("hf_token", re.compile(r"\bhf_[A-Za-z0-9]{30,}")),
    ("npm_token", re.compile(r"\bnpm_[A-Za-z0-9]{30,}")),
    ("gitlab_token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}")),
    # Discord user/bot tokens: base64 user id, timestamp, HMAC.
    ("discord_token", re.compile(r"\b[MNO][A-Za-z\d_-]{23,25}\.[A-Za-z\d_-]{6}\.[A-Za-z\d_-]{27,}")),
    # Telegram bot tokens: "<bot id>:<35 characters>" (a little slack either side).
    ("telegram_token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,40}(?![A-Za-z0-9_-])")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}")),
)

_SECRET_KEYS = (
    r"(api[_-]?key|apikey|secret|client[_-]?secret|access[_-]?key|"
    r"auth[_-]?token|access[_-]?token|refresh[_-]?token|token|password|passwd|pwd|passphrase)"
)
# key = "a quoted value with spaces": the whole quoted value goes, not just
# its first word ('password = "correct horse battery staple"').
_QUOTED_ASSIGNMENT = re.compile(rf"(?i)\b{_SECRET_KEYS}(\s*[:=]\s*)([\"'])([^\"'\n]{{4,}}?)\3")
# key = value / key: value assignments of obviously secret settings.
_ASSIGNMENT = re.compile(rf"(?i)\b{_SECRET_KEYS}(\s*[:=]\s*)([\"']?)([^\s\"']{{6,}})")

# One-time codes: 4-8 digits next to an OTP-ish word (either side), and
# 6-8 digits next to the looser "code"/"pin". Codes are often shown in
# groups ("123-456", "123 456"), so a hyphen or space may split them.
_OTP_WORDS = r"(?:otp|one[- ]time|passcode|verification|2fa|security code|login code|auth(?:entication)? code)"
_CODE_WORDS = r"(?:code|pin)"
_OTP_DIGITS = r"(?:\d{3,4}[- ]\d{3,4}|\d{4,8})"
_CODE_DIGITS = r"(?:\d{3,4}[- ]\d{3,4}|\d{6,8})"
_OTP_AFTER = re.compile(rf"(?i)(\b{_OTP_WORDS}\b[^\d\n]{{0,24}})({_OTP_DIGITS})\b")
_OTP_BEFORE = re.compile(rf"(?i)\b({_OTP_DIGITS})(\b[^\d\n]{{0,24}}\b{_OTP_WORDS}\b)")
_CODE_AFTER = re.compile(rf"(?i)(\b{_CODE_WORDS}\b[^\d\n]{{0,24}})({_CODE_DIGITS})\b")
_CODE_BEFORE = re.compile(rf"(?i)\b({_CODE_DIGITS})(\b[^\d\n]{{0,24}}\b{_CODE_WORDS}\b)")


def redact_secrets(text: str) -> str:
    """Replace key, token, JWT, PEM and OTP shapes with ``[REDACTED:kind]``.

    Over-redaction is the accepted failure mode: a false positive costs a
    worse answer, a false negative leaks a credential to a cloud model.
    """
    if not text:
        return ""
    text = _PEM_BLOCK.sub("[REDACTED:pem]", text)
    for kind, pattern in _TOKEN_PATTERNS:
        text = pattern.sub(f"[REDACTED:{kind}]", text)
    text = _QUOTED_ASSIGNMENT.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}[REDACTED:secret]{m.group(3)}", text
    )
    text = _ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}[REDACTED:secret]", text)
    text = _OTP_AFTER.sub(lambda m: f"{m.group(1)}[REDACTED:otp]", text)
    text = _OTP_BEFORE.sub(lambda m: f"[REDACTED:otp]{m.group(2)}", text)
    text = _CODE_AFTER.sub(lambda m: f"{m.group(1)}[REDACTED:otp]", text)
    text = _CODE_BEFORE.sub(lambda m: f"[REDACTED:otp]{m.group(2)}", text)
    return text


# ---------------------------------------------------------------------------
# Exact spans of Roshan's words
# ---------------------------------------------------------------------------

_NEGATORS = frozenset(
    {"not", "no", "never", "nothing", "nor", "neither", "cannot", "without", "nope", "dont", "nahi", "mat"}
)
_IRREGULAR_NEGATIONS = {
    "can't": "can not",
    "won't": "will not",
    "shan't": "shall not",
    "ain't": "is not",
    "cannot": "can not",
}
# "dont", "didnt", "wont" typed without the apostrophe.
_BARE_NEGATION = re.compile(r"^(?:do|does|did|is|are|was|were|wo|ca|could|should|would|must|have|has|had|need)nt$")
# How many words before a span are checked for a negator, within a clause.
_NEGATION_WINDOW = 2


def _span_tokens(text: str) -> list[tuple[str, int]]:
    """Casefold, expand negative contractions, and split into words.

    Each word carries the index of its clause. Clause punctuation is ignored
    for matching ("hi mom" is a span of "Hi, Mom!") but bounds the negation
    window, so "no worries, send it" does not read as a negated "send it".
    """
    text = strip_invisible(text).casefold()
    text = text.replace("\u2019", "'").replace("\u2018", "'").replace("`", "'")
    for contraction, expanded in _IRREGULAR_NEGATIONS.items():
        text = re.sub(rf"\b{re.escape(contraction)}\b", expanded, text)
    text = re.sub(r"n't\b", " not", text)
    tokens: list[tuple[str, int]] = []
    for clause, part in enumerate(re.split(r"[.,;:!?\n]+", text)):
        for word in re.sub(r"[^\w]+", " ", part).split():
            tokens.append((word, clause))
    return tokens


def exact_span(span: str, utterance: str) -> bool:
    """True when ``span`` occurs in ``utterance`` as whole words.

    Matching ignores case, invisible characters, whitespace and punctuation,
    but rejects a match that drops an adjacent negation: span "send it" is
    not an exact span of "don't send it", and "I will" is not one of
    "I will not". Unknowns resolve to False.
    """
    needle = [word for word, _clause in _span_tokens(span)]
    haystack = _span_tokens(utterance)
    if not needle or not haystack:
        return False
    words = [word for word, _clause in haystack]
    size = len(needle)
    for start in range(len(words) - size + 1):
        if words[start : start + size] != needle:
            continue
        if _negated_before(haystack, start, needle) or _negated_after(haystack, start + size, needle):
            continue
        return True
    return False


def _is_negator(word: str) -> bool:
    return word in _NEGATORS or bool(_BARE_NEGATION.match(word))


def _negated_before(tokens: list[tuple[str, int]], start: int, needle: list[str]) -> bool:
    if _is_negator(needle[0]):
        return False
    clause = tokens[start][1]
    for offset in range(1, _NEGATION_WINDOW + 1):
        index = start - offset
        if index < 0 or tokens[index][1] != clause:
            return False
        if _is_negator(tokens[index][0]):
            return True
    return False


def _negated_after(tokens: list[tuple[str, int]], end: int, needle: list[str]) -> bool:
    if _is_negator(needle[-1]) or end >= len(tokens):
        return False
    return tokens[end][1] == tokens[end - 1][1] and _is_negator(tokens[end][0])
