"""Indian-script lyrics in Latin letters ("മലയാളം" -> "malayaalam").

The Unicode blocks for Devanagari, Bengali, Gurmukhi, Gujarati, Oriya,
Tamil, Telugu, Kannada and Malayalam share one layout (inherited from
ISCII): the same offset in each block is the same sound. So one table of
offsets covers them all. It is a readable phonetic spelling (long vowels
doubled, "zh" for Malayalam/Tamil ഴ/ழ), not a scholarly standard, which
is what singing along needs.
"""
from __future__ import annotations

BLOCKS = (0x0900, 0x0980, 0x0A00, 0x0A80, 0x0B00, 0x0B80, 0x0C00, 0x0C80, 0x0D00)

# offset -> Latin, by kind
VOWELS = {0x05: "a", 0x06: "aa", 0x07: "i", 0x08: "ii", 0x09: "u", 0x0A: "uu", 0x0B: "ru",
          0x0C: "lu", 0x0E: "e", 0x0F: "e", 0x10: "ai", 0x12: "o", 0x13: "o", 0x14: "au",
          0x60: "ruu", 0x61: "luu"}
SIGNS = {0x3E: "aa", 0x3F: "i", 0x40: "ii", 0x41: "u", 0x42: "uu", 0x43: "ru", 0x44: "ruu",
         0x46: "e", 0x47: "e", 0x48: "ai", 0x4A: "o", 0x4B: "o", 0x4C: "au", 0x57: "au",
         0x62: "lu", 0x63: "luu"}
CONSONANTS = {0x15: "k", 0x16: "kh", 0x17: "g", 0x18: "gh", 0x19: "ng", 0x1A: "ch", 0x1B: "chh",
              0x1C: "j", 0x1D: "jh", 0x1E: "nj", 0x1F: "t", 0x20: "th", 0x21: "d", 0x22: "dh",
              0x23: "n", 0x24: "th", 0x25: "th", 0x26: "d", 0x27: "dh", 0x28: "n", 0x29: "n",
              0x2A: "p", 0x2B: "ph", 0x2C: "b", 0x2D: "bh", 0x2E: "m", 0x2F: "y", 0x30: "r",
              0x31: "r", 0x32: "l", 0x33: "l", 0x34: "zh", 0x35: "v", 0x36: "sh", 0x37: "sh",
              0x38: "s", 0x39: "h", 0x58: "q", 0x59: "kh", 0x5A: "gh", 0x5B: "z", 0x5C: "r",
              0x5D: "rh", 0x5E: "f", 0x5F: "y"}
MARKS = {0x01: "n", 0x02: "m", 0x03: "h"}       # candrabindu, anusvara, visarga
VIRAMA, NUKTA = 0x4D, 0x3C
# Malayalam chillus: a consonant with no vowel, as one letter.
CHILLU = {0x0D7A: "n", 0x0D7B: "n", 0x0D7C: "r", 0x0D7D: "l", 0x0D7E: "l", 0x0D7F: "k", 0x0D54: "m",
          0x0D55: "y", 0x0D56: "zh"}
ZW = {0x200C, 0x200D}


def _offset(ch: str):
    code = ord(ch)
    for base in BLOCKS:
        if base <= code < base + 0x80:
            return code - base
    return None


DEVANAGARI, MALAYALAM = 0x0900, 0x0D00
# Per-script spellings where the shared table reads wrong.
LOCAL = {DEVANAGARI: {0x24: "t", 0x25: "th"}}


def has_indic(text: str) -> bool:
    return any(_offset(c) is not None or ord(c) in CHILLU for c in text or "")


def _word(word: str) -> str:
    out: list[str] = []
    pending = False          # a consonant still carrying its inherent "a"
    block = None
    chars = [c for c in word if ord(c) not in ZW]
    for i, ch in enumerate(chars):
        code = ord(ch)
        if code in CHILLU:
            if pending:
                out.append("a")
            out.append(CHILLU[code])
            pending, block = False, MALAYALAM
            continue
        off = _offset(ch)
        if off is None:
            if pending:
                out.append("a")
            pending = False
            out.append(ch)
            continue
        block = code - off
        if off in CONSONANTS:
            if pending:
                out.append("a")
            # Malayalam/Tamil "nr" (ന്റ) is said "nt".
            if off == 0x31 and out and out[-1] == "n" and i >= 2 and _offset(chars[i - 1]) == VIRAMA:
                out.append("t")
            else:
                out.append(LOCAL.get(block, {}).get(off, CONSONANTS[off]))
            pending = True
        elif off in SIGNS:
            out.append(SIGNS[off])
            pending = False
        elif off == VIRAMA:
            pending = False
            # A Malayalam word ending in a virama ends in a short "u" sound.
            if block == MALAYALAM and i == len(chars) - 1:
                out.append("u")
        elif off == NUKTA:
            continue
        elif off in VOWELS:
            if pending:
                out.append("a")
            out.append(VOWELS[off])
            pending = False
        elif off in MARKS:
            if pending:
                out.append("a")
            out.append(MARKS[off])
            pending = False
        elif 0x66 <= off <= 0x6F:            # digits
            if pending:
                out.append("a")
            out.append(str(off - 0x66))
            pending = False
        else:
            if pending:
                out.append("a")
            pending = False
    # Hindi drops a word's last inherent "a" (kamal, not kamala).
    if pending and not (block == DEVANAGARI and len(out) > 1):
        out.append("a")
    return "".join(out)


def to_latin(text: str) -> str:
    """Indian-script letters spelled in Latin; everything else unchanged."""
    import re

    return "".join(_word(part) if has_indic(part) else part for part in re.split(r"(\s+)", text or ""))


def latin_lines(lines: list) -> list:
    """Synced lines with their text in Latin letters (same timestamps and
    word count, so word timing still lines up)."""
    return [(t, to_latin(text) if has_indic(text) else text) for t, text in lines]
