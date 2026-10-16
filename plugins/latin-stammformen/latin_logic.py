"""Pure logic for the Stammformen trainer (latin-stammformen): no Core, no
file I/O, no clocks -- everything here is a plain function of its inputs so
test_latin_stammformen.py can exercise it directly.

What lives here:
  - verb validation/normalisation (the ONE place a verb's fields are
    checked, whether the verb came from an import file, a bulk paste, a
    scanned photo or a manual edit),
  - the import parsers: the spin's own JSON backup, the original single-file
    Gemini HTML (its INITIAL_VERBS array is read with a small JSON5-style
    parser -- the page is DATA, it is never evaluated), and the semicolon
    bulk-paste format,
  - Test-mode answer checking (forgiving on case, accents, commas, spacing
    and "(!)" marks; strict on the forms themselves),
  - parsing/validating a Gemini photo-scan reply, and a token estimate for
    when ai-test doesn't report real usage.

Nothing in here knows about any particular textbook: the word list is
always imported data, never part of the package.
"""

from __future__ import annotations

import base64
import binascii
import html as html_lib
import json
import math
import re
import secrets
import unicodedata
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Schema constants
# ---------------------------------------------------------------------------

FORMAT_NAME = "sotrice-latin-stammformen"
SCHEMA_VERSION = 1

CONJ_VALUES = ("1", "2", "3", "M", "4", "irreg", "semidep", "dep")

# Field limits. Generous for real Latin/German text, tight enough that a
# runaway AI reply or a pasted wall of text can't bloat the data file.
MAX_INF = 60
MAX_PRES = 80
MAX_PERF = 60
MAX_PPP = 60
MAX_MEANING = 120
MAX_CAT = 60
MAX_TITLE = 80
MAX_GROUP_NAME = 60
MAX_LESSON = 999
MAX_ROWS_PER_IMPORT = 2000
NO_PPP = "---"

DEFAULT_CAT_PASTE = "Uploaded Vocab"
DEFAULT_CAT_SCAN = "Scanned page"


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

_COMBINING = re.compile(r"[̀-ͯ]")
_ZERO_WIDTH = re.compile(r"[​-‏‪-‮⁠-⁤﻿]")


def clean_text(value: Any, max_len: int) -> str:
    """A field value as a clean, single-line, NFC string of at most
    `max_len` characters. Control characters and zero-width/bidi marks are
    dropped, all whitespace runs collapse to one space. Never raises."""
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        return ""
    value = unicodedata.normalize("NFC", value)
    value = _ZERO_WIDTH.sub("", value)
    value = "".join(" " if ch in "\t\r\n   " else ch for ch in value if unicodedata.category(ch) != "Cc" or ch in "\t\r\n")
    value = re.sub(r"\s+", " ", value).strip()
    return value[:max_len].rstrip()


def fold(text: Any) -> str:
    """Lower-case, accent-/macron-insensitive form of `text` (also ss for
    the sharp s and ae/oe for the ligatures) -- the basis of search and
    answer checking. 'delére', 'Deléré' and 'delere' fold to the same
    string."""
    if not isinstance(text, str):
        return ""
    text = unicodedata.normalize("NFD", text.lower())
    text = _COMBINING.sub("", text)
    return text.replace("ß", "ss").replace("æ", "ae").replace("œ", "oe").replace("ſ", "s")


_PARENS = re.compile(r"\([^)]*\)")


def strip_notes(text: str) -> str:
    """Removes parenthesised annotations such as '(!)', '(unpers.)',
    '(3. Sg.)' -- notes in a textbook list, not part of the form."""
    return _PARENS.sub(" ", text)


# ---------------------------------------------------------------------------
# Verb validation
# ---------------------------------------------------------------------------

def _issue(field: str, level: str, message: str) -> dict:
    return {"field": field, "level": level, "message": message}


_CONJ_ALIASES = {
    "1": "1", "a": "1", "a-konj": "1", "a-konjugation": "1", "akonjugation": "1",
    "2": "2", "e": "2", "e-konj": "2", "e-konjugation": "2", "ekonjugation": "2",
    "3": "3", "konsonantisch": "3", "konsonantische konjugation": "3",
    "m": "M", "3i": "M", "3-i": "M", "3 i": "M", "mixed": "M", "gemischt": "M",
    "mischkonjugation": "M", "mischkonj": "M", "i-stamm": "M",
    "4": "4", "i-konj": "4", "i-konjugation": "4", "ikonjugation": "4",
    "irreg": "irreg", "irreg.": "irreg", "irregular": "irreg", "irregulaer": "irreg",
    "unregelmassig": "irreg", "unregelmaessig": "irreg", "unregelmaessige": "irreg", "anomal": "irreg",
    "semidep": "semidep", "semidep.": "semidep", "semideponens": "semidep", "semideponentia": "semidep",
    "dep": "dep", "dep.": "dep", "deponens": "dep", "deponentia": "dep",
}


def normalize_conj(raw: Any) -> Optional[str]:
    """Maps the spellings a person or an AI might use onto one of
    CONJ_VALUES, or None if it isn't recognisable."""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        raw = str(int(raw)) if float(raw).is_integer() else ""
    if not isinstance(raw, str):
        return None
    text = fold(raw).strip(" .()[]")
    if not text:
        return None
    if text in _CONJ_ALIASES:
        return _CONJ_ALIASES[text]
    first = re.split(r"[\s(]", text, maxsplit=1)[0].strip(" .")
    if first in _CONJ_ALIASES:  # "M (Mischkonjugation)", "1 (a-Konjugation)"
        return _CONJ_ALIASES[first]
    # "3. Konjugation", "2-e" ...
    m = re.match(r"^([1-4])\b\s*(?:[.\-]?\s*(i)\b)?", text)
    if m:
        if m.group(1) == "3" and m.group(2):
            return "M"
        return m.group(1)
    return None


_DASHES = {"", "-", "--", "---", "----", "–", "—", "−", "‒", "n/a", "none", "keins", "kein"}


def normalize_ppp(raw: Any) -> str:
    text = clean_text(raw, MAX_PPP)
    if text.lower() in _DASHES or re.fullmatch(r"[-‐-―−\s]+", text or "-"):
        return NO_PPP
    return text


_TRUTHY = {"true", "yes", "y", "ja", "x", "1", "*", "★", "bold", "core", "fett"}


def to_bool(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return raw != 0
    if isinstance(raw, str):
        return raw.strip().lower() in _TRUTHY
    return False


def to_lesson(raw: Any) -> Optional[int]:
    """A lesson number 0..MAX_LESSON from an int, a float, or text such as
    '12', '12.', 'Lektion 12', 'Lec. 12'. None if there isn't one."""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        if isinstance(raw, float) and not raw.is_integer():
            return None
        value = int(raw)
    elif isinstance(raw, str):
        m = re.search(r"\d{1,4}", raw)
        if not m:
            return None
        value = int(m.group(0))
    else:
        return None
    return value if 0 <= value <= MAX_LESSON else None


_SUSPICIOUS = re.compile(r"[<>{}\[\]\\|`]|\d")


def normalize_verb(raw: Any, defaults: Optional[dict] = None) -> tuple[dict, list[dict]]:
    """Validates and normalises ONE verb. Returns (clean_fields, issues):
    `clean_fields` always has every field (best effort even when invalid, so
    an editable preview can show it); `issues` is a list of
    {field, level: "error"|"warning", message}. A verb may be stored only
    when no issue has level "error". The result never carries an `id`."""
    defaults = defaults or {}
    issues: list[dict] = []
    if not isinstance(raw, dict):
        raw = {}
        issues.append(_issue("inf", "error", "Not a verb entry."))

    inf = clean_text(raw.get("inf"), MAX_INF)
    pres = clean_text(raw.get("pres"), MAX_PRES)
    perf = clean_text(raw.get("perf"), MAX_PERF)
    meaning = clean_text(raw.get("meaning"), MAX_MEANING)
    ppp = normalize_ppp(raw.get("ppp"))
    cat = clean_text(raw.get("cat"), MAX_CAT) or clean_text(defaults.get("cat"), MAX_CAT)

    if not inf:
        issues.append(_issue("inf", "error", "The infinitive is missing."))
    elif not re.search(r"[^\W\d_]", inf):
        issues.append(_issue("inf", "error", "The infinitive has no letters."))
    if not pres:
        issues.append(_issue("pres", "error", "The 1st/2nd person singular present is missing."))
    if not perf:
        issues.append(_issue("perf", "error", "The perfect form is missing."))
    if not meaning:
        issues.append(_issue("meaning", "error", "The German meaning is missing."))

    conj_raw = raw.get("conj")
    conj = normalize_conj(conj_raw)
    if conj is None:
        conj = normalize_conj(defaults.get("conj")) if (conj_raw in (None, "") and defaults.get("conj")) else None
    if conj is None:
        issues.append(_issue("conj", "error", "Choose a conjugation (1, 2, 3, M, 4, irreg, semidep or dep)."))

    lec_raw = raw.get("lec")
    lec = to_lesson(lec_raw)
    if lec is None:
        if lec_raw in (None, ""):
            lec = to_lesson(defaults.get("lec", 1))
            if lec is None:
                lec = 1
        else:
            lec = to_lesson(defaults.get("lec", 1)) or 1
            issues.append(_issue("lec", "error", f"'{clean_text(lec_raw, 20)}' is not a lesson number (0-{MAX_LESSON})."))

    for name, text in (("inf", inf), ("pres", pres), ("perf", perf), ("ppp", ppp)):
        if text and _SUSPICIOUS.search(strip_notes(text)):
            issues.append(_issue(name, "warning", "Contains digits or unusual symbols -- check it against the book."))

    bold = to_bool(raw.get("bold", defaults.get("bold", False)))
    clean = {
        "inf": inf,
        "conj": conj or "",
        "pres": pres,
        "perf": perf,
        "ppp": ppp,
        "meaning": meaning,
        "lec": lec,
        "cat": cat,
        "bold": bold,
    }
    return clean, issues


def has_errors(issues: list[dict]) -> bool:
    return any(i["level"] == "error" for i in issues)


def verb_signature(v: dict, weak: bool = False) -> tuple:
    """Content signature used ONLY at import time to spot a verb that is
    already in the list (never as a key for stored data -- verbs are keyed
    by their stored id). The full signature includes the category and
    lesson, so a textbook that lists the same infinitive in two sections
    keeps both entries; the weak one (forms only) is for pasted/scanned
    verbs, whose category and lesson are only defaults."""
    forms = (
        fold(strip_notes(v.get("inf", ""))).strip(),
        fold(strip_notes(v.get("pres", ""))).strip(),
        fold(strip_notes(v.get("perf", ""))).strip(),
        fold(strip_notes(v.get("ppp", ""))).strip(),
    )
    return forms if weak else forms + (fold(v.get("cat", "")).strip(), v.get("lec"))


_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


def valid_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_ID_RE.match(value))


def new_id(prefix: str, taken: set[str]) -> str:
    """A fresh opaque id (prefix + 12 random hex chars) not in `taken`."""
    while True:
        candidate = f"{prefix}_{secrets.token_hex(6)}"
        if candidate not in taken:
            return candidate


def conj_kind(v: dict) -> str:
    """The conjugation filter value a verb belongs to. A deponent or
    semi-deponent keeps its digit conjugation in the data, so the 'dep' and
    'semidep' filter values are read from the forms themselves: a passive
    1st person (-or) is a deponent, an active one with a '... sum' perfect
    is a semi-deponent. A conj already stored as dep/semidep wins."""
    conj = v.get("conj", "")
    if conj in ("dep", "semidep"):
        return conj
    pres_first = fold(strip_notes(v.get("pres", ""))).replace(",", " ").split()
    first = pres_first[0] if pres_first else ""
    perf_words = fold(strip_notes(v.get("perf", ""))).split()
    if first.endswith("or") and len(first) > 3:
        return "dep"
    if len(perf_words) >= 2 and perf_words[-1] == "sum" and not first.endswith("or"):
        return "semidep"
    return conj


# ---------------------------------------------------------------------------
# JSON5-style parser (for the original page's INITIAL_VERBS array)
# ---------------------------------------------------------------------------

class Json5Error(ValueError):
    pass


_IDENT_START = re.compile(r"[A-Za-z_$]")
_IDENT = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_NUMBER = re.compile(r"[+-]?(?:0[xX][0-9a-fA-F]+|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)")
_SIMPLE_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v", "0": "\0",
                   "\\": "\\", '"': '"', "'": "'", "/": "/"}


class _Json5Parser:
    """A small recursive-descent parser for the JS-literal subset a
    Gemini-written page uses: objects with bare or quoted keys, arrays,
    single/double-quoted strings, numbers, true/false/null, // and /* */
    comments and trailing commas. It parses DATA only -- there is no
    evaluation of anything, no identifiers as values, no expressions."""

    MAX_DEPTH = 32
    MAX_ITEMS = 20000

    def __init__(self, text: str, pos: int = 0) -> None:
        self.text = text
        self.pos = pos
        self.items = 0

    def fail(self, message: str) -> "Json5Error":
        line = self.text.count("\n", 0, self.pos) + 1
        return Json5Error(f"{message} (line {line})")

    def skip(self) -> None:
        text, n = self.text, len(self.text)
        while self.pos < n:
            ch = text[self.pos]
            if ch in " \t\r\n﻿ ":
                self.pos += 1
            elif text.startswith("//", self.pos):
                end = text.find("\n", self.pos)
                self.pos = n if end == -1 else end + 1
            elif text.startswith("/*", self.pos):
                end = text.find("*/", self.pos + 2)
                if end == -1:
                    raise self.fail("Unterminated comment")
                self.pos = end + 2
            else:
                break

    def parse_value(self, depth: int = 0) -> Any:
        if depth > self.MAX_DEPTH:
            raise self.fail("Nested too deeply")
        self.skip()
        if self.pos >= len(self.text):
            raise self.fail("Unexpected end of data")
        ch = self.text[self.pos]
        if ch == "{":
            return self.parse_object(depth)
        if ch == "[":
            return self.parse_array(depth)
        if ch in "\"'":
            return self.parse_string()
        m = _NUMBER.match(self.text, self.pos)
        if m and (ch.isdigit() or ch in "+-."):
            self.pos = m.end()
            token = m.group(0)
            if token.lower().lstrip("+-").startswith("0x"):
                return int(token, 16)
            if re.fullmatch(r"[+-]?\d+", token):
                return int(token)
            return float(token)
        m = _IDENT.match(self.text, self.pos)
        if m:
            word = m.group(0)
            self.pos = m.end()
            if word == "true":
                return True
            if word == "false":
                return False
            if word == "null":
                return None
            raise self.fail(f"Unexpected word '{word}' (only data is allowed here)")
        raise self.fail(f"Unexpected character {ch!r}")

    def parse_string(self) -> str:
        quote = self.text[self.pos]
        self.pos += 1
        out: list[str] = []
        text, n = self.text, len(self.text)
        while self.pos < n:
            ch = text[self.pos]
            if ch == quote:
                self.pos += 1
                return "".join(out)
            if ch == "\n":
                raise self.fail("Unterminated string")
            if ch == "\\":
                self.pos += 1
                if self.pos >= n:
                    break
                esc = text[self.pos]
                if esc in _SIMPLE_ESCAPES:
                    out.append(_SIMPLE_ESCAPES[esc])
                    self.pos += 1
                elif esc == "u":
                    digits = text[self.pos + 1:self.pos + 5]
                    if not re.fullmatch(r"[0-9a-fA-F]{4}", digits):
                        raise self.fail("Bad \\u escape")
                    out.append(chr(int(digits, 16)))
                    self.pos += 5
                elif esc == "x":
                    digits = text[self.pos + 1:self.pos + 3]
                    if not re.fullmatch(r"[0-9a-fA-F]{2}", digits):
                        raise self.fail("Bad \\x escape")
                    out.append(chr(int(digits, 16)))
                    self.pos += 3
                elif esc == "\n":  # line continuation
                    self.pos += 1
                else:
                    out.append(esc)
                    self.pos += 1
                continue
            out.append(ch)
            self.pos += 1
        raise self.fail("Unterminated string")

    def parse_array(self, depth: int) -> list:
        self.pos += 1
        result: list = []
        while True:
            self.skip()
            if self.pos >= len(self.text):
                raise self.fail("Unterminated array")
            if self.text[self.pos] == "]":
                self.pos += 1
                return result
            result.append(self.parse_value(depth + 1))
            self.items += 1
            if self.items > self.MAX_ITEMS:
                raise self.fail("Too many items")
            self.skip()
            if self.pos < len(self.text) and self.text[self.pos] == ",":
                self.pos += 1
            elif self.pos < len(self.text) and self.text[self.pos] == "]":
                continue
            else:
                raise self.fail("Expected ',' or ']'")

    def parse_object(self, depth: int) -> dict:
        self.pos += 1
        result: dict = {}
        while True:
            self.skip()
            if self.pos >= len(self.text):
                raise self.fail("Unterminated object")
            ch = self.text[self.pos]
            if ch == "}":
                self.pos += 1
                return result
            if ch in "\"'":
                key = self.parse_string()
            else:
                m = _IDENT.match(self.text, self.pos) or re.compile(r"\d+").match(self.text, self.pos)
                if not m:
                    raise self.fail("Expected a property name")
                key = m.group(0)
                self.pos = m.end()
            self.skip()
            if self.pos >= len(self.text) or self.text[self.pos] != ":":
                raise self.fail("Expected ':' after a property name")
            self.pos += 1
            result[key] = self.parse_value(depth + 1)
            self.items += 1
            if self.items > self.MAX_ITEMS:
                raise self.fail("Too many items")
            self.skip()
            if self.pos < len(self.text) and self.text[self.pos] == ",":
                self.pos += 1
            elif self.pos < len(self.text) and self.text[self.pos] == "}":
                continue
            else:
                raise self.fail("Expected ',' or '}'")


def parse_json5(text: str) -> Any:
    """Parses one JSON5-style value (the whole of `text`)."""
    parser = _Json5Parser(text)
    value = parser.parse_value()
    parser.skip()
    if parser.pos != len(text):
        raise parser.fail("Unexpected data after the value")
    return value


def parse_json5_prefix(text: str, start: int) -> Any:
    """Parses ONE JSON5-style value starting at `start`, ignoring whatever
    follows it (used to lift an array literal out of a larger page)."""
    return _Json5Parser(text, start).parse_value()


_TITLE_TAG = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)


def extract_gemini_page(page: str) -> tuple[str, list]:
    """(title, verb array) from the original single-file Gemini page. The
    array is its INITIAL_VERBS constant (or, if renamed, the first array
    literal whose first object has an `inf` key), read as data -- never
    executed. Raises Json5Error/ValueError if there is none."""
    m = re.search(r"\bINITIAL_VERBS\s*=\s*\[", page)
    if m:
        start = m.end() - 1
    else:
        m = re.search(r"\[\s*\{\s*(?:\"inf\"|'inf'|inf)\s*:", page)
        if not m:
            raise ValueError("No verb list (INITIAL_VERBS) found in this page.")
        start = m.start()
    value = parse_json5_prefix(page, start)
    if not isinstance(value, list):
        raise ValueError("INITIAL_VERBS is not a list.")
    title = ""
    tm = _TITLE_TAG.search(page)
    if tm:
        title = clean_text(html_lib.unescape(tm.group(1)), 200)
        title = re.split(r"\s+[—–|-]\s+", title, maxsplit=1)[0]
    return clean_text(title, MAX_TITLE), value


# ---------------------------------------------------------------------------
# Bulk paste
# ---------------------------------------------------------------------------

_PASTE_HEADER = {"inf", "infinitiv", "infinitive"}


def parse_bulk_paste(text: str) -> tuple[list[dict], list[dict]]:
    """Lines of `infinitive; conj; 1st/2nd_sg; perfekt; ppp; meaning; lesson`
    (lesson optional). Tab-separated lines (a spreadsheet paste) work too.
    Returns (rows, problems); a line with too few fields is reported, not
    silently skipped."""
    rows: list[dict] = []
    problems: list[dict] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        if len(rows) + len(problems) >= MAX_ROWS_PER_IMPORT:
            problems.append({"line": number, "message": f"Stopped after {MAX_ROWS_PER_IMPORT} lines."})
            break
        parts = [p.strip() for p in (line.split(";") if ";" in line else line.split("\t"))]
        if fold(parts[0]) in _PASTE_HEADER:
            continue
        if len(parts) < 6:
            problems.append({"line": number, "message": f"Line {number}: expected at least 6 fields separated by ';' (got {len(parts)})."})
            continue
        row = {"inf": parts[0], "conj": parts[1], "pres": parts[2], "perf": parts[3], "ppp": parts[4], "meaning": parts[5]}
        if len(parts) > 6 and parts[6]:
            row["lec"] = parts[6]
        rows.append(row)
    return rows, problems


# ---------------------------------------------------------------------------
# Import detection
# ---------------------------------------------------------------------------

def detect_and_parse(text: str, filename: str = "") -> dict:
    """Recognises what a file/paste is and parses it into
    {kind, title, rows, problems, ...}. kind is one of:
      "backup"  -- this spin's own JSON export (verbs + progress),
      "list"    -- JSON with a `verbs` array (or a bare array of verbs),
      "html"    -- the original single-file Gemini trainer page,
      "paste"   -- semicolon-separated lines.
    Raises ValueError with a readable message when it is none of those."""
    if len(text) > 12 * 1024 * 1024:
        raise ValueError("That file is too large to be a verb list.")
    text = text.lstrip("﻿")
    stripped = text.lstrip()
    if not stripped:
        raise ValueError("There is nothing to import (empty file or paste).")
    base_title = clean_text(re.sub(r"\.[A-Za-z0-9]{1,5}$", "", filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]), MAX_TITLE)

    if stripped[:1] == "<" or ("INITIAL_VERBS" in text and "<script" in text.lower()):
        title, array = extract_gemini_page(text)
        rows = [r for r in array if isinstance(r, dict)]
        problems = []
        if len(rows) != len(array):
            problems.append({"line": None, "message": f"{len(array) - len(rows)} entries in the page were not verb objects and were ignored."})
        return {"kind": "html", "title": title or base_title or "My verb list", "rows": rows, "problems": problems, "has_progress": False}

    if stripped[:1] in "{[":
        try:
            data = json.loads(stripped)
        except ValueError as exc:
            raise ValueError(f"This looks like JSON but isn't valid: {exc}") from exc
        return _parse_json_payload(data, base_title)

    if ";" in text or "\t" in text:
        rows, problems = parse_bulk_paste(text)
        if rows or problems:
            return {"kind": "paste", "title": "", "rows": rows, "problems": problems, "has_progress": False}
    raise ValueError("Nothing in there looks like a verb list (expected a backup .json, the Gemini .html page, or lines like 'amare; 1; amo, amas; amavi; amatum; lieben; 1').")


def _parse_json_payload(data: Any, base_title: str) -> dict:
    if isinstance(data, list):
        return {"kind": "list", "title": base_title or "My verb list",
                "rows": [r for r in data if isinstance(r, dict)], "problems": [], "has_progress": False}
    if not isinstance(data, dict):
        raise ValueError("The JSON isn't a verb list.")
    verbs = data.get("verbs")
    if not isinstance(verbs, list):
        raise ValueError("The JSON has no 'verbs' list.")
    schema = data.get("schema", data.get("format_version", 1))
    if not isinstance(schema, int) or schema > SCHEMA_VERSION:
        raise ValueError(f"This backup was written by a newer version (format {schema}); update the spin first.")
    is_backup = data.get("format") == FORMAT_NAME
    rows = [r for r in verbs if isinstance(r, dict)]
    problems = []
    if len(rows) != len(verbs):
        problems.append({"line": None, "message": f"{len(verbs) - len(rows)} entries were not verb objects and were ignored."})
    has_progress = any(k in data for k in ("mastered", "results", "excluded_lessons", "excluded_groups"))
    return {
        "kind": "backup" if is_backup else "list",
        "title": clean_text(data.get("title"), MAX_TITLE) or base_title or "My verb list",
        "rows": rows,
        "problems": problems,
        "has_progress": has_progress,
        "schema": schema,
        "groups": data.get("groups") if isinstance(data.get("groups"), list) else [],
        "excluded_lessons": data.get("excluded_lessons") if isinstance(data.get("excluded_lessons"), list) else [],
        "excluded_groups": data.get("excluded_groups") if isinstance(data.get("excluded_groups"), list) else [],
        "mastered": data.get("mastered") if isinstance(data.get("mastered"), dict) else {},
        "results": data.get("results") if isinstance(data.get("results"), list) else None,
    }


# ---------------------------------------------------------------------------
# Test-mode answer checking
# ---------------------------------------------------------------------------

def norm_form(text: Any) -> str:
    """A Latin form reduced for comparison: case, accents/macrons, notes in
    parentheses such as '(!)', the '!' mark, commas/semicolons and spacing
    are all ignored; the letters themselves are not."""
    if not isinstance(text, str):
        return ""
    text = fold(strip_notes(text)).replace("!", " ")
    text = re.sub(r"[,;/…]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _is_none_marker(text: str) -> bool:
    return bool(re.fullmatch(r"[\s\-‐-―−]*", text)) or fold(text).strip(" .") in {"keins", "kein", "none", "n/a", "entfallt", "entfaellt"}


def fold_de(text: str) -> str:
    """Like fold(), but a German umlaut becomes its digraph (u-umlaut ->
    'ue') -- the way someone without umlaut keys types it."""
    text = text.replace("\u00e4", "ae").replace("\u00f6", "oe").replace("\u00fc", "ue")
    text = text.replace("\u00c4", "Ae").replace("\u00d6", "Oe").replace("\u00dc", "Ue")
    return fold(text)


def _meaning_variants(expected: str) -> list[str]:
    """Acceptable German answers: the text split at ';' and ',', each part
    with and without its parenthesised bits ('(aus)fuehren' -> 'ausfuehren'
    and 'fuehren'), in both umlaut spellings (u-umlaut and 'ue'), folded so
    case and accents don't matter."""
    out: list[str] = []
    for part in re.split(r"[;,]", expected):
        part = part.strip()
        if not part:
            continue
        for fold_fn in (fold, fold_de):
            without = fold_fn(strip_notes(part)).strip()
            inlined = re.sub(r"\s+", " ", fold_fn(re.sub(r"[()]", "", part)).replace("=", " ")).strip()
            for cand in (without, inlined):
                if cand and cand not in out:
                    out.append(cand)
    return out


def check_answers(verb: dict, answers: dict) -> dict:
    """Checks one Test-mode attempt. `answers` has the typed pres, perf,
    ppp, meaning. Returns {ok: {pres, perf, ppp, meaning}, all_ok, empty}.
    The three Latin forms must match exactly (after the forgiving
    normalisation in norm_form); the German meaning is accepted when it
    equals -- or, for answers of 3+ letters, contains/is contained in --
    any of the listed translations."""
    given = {k: (answers.get(k) if isinstance(answers.get(k), str) else "") for k in ("pres", "perf", "ppp", "meaning")}

    ok_pres = norm_form(given["pres"]) == norm_form(verb.get("pres", "")) != ""
    ok_perf = norm_form(given["perf"]) == norm_form(verb.get("perf", "")) != ""

    expected_ppp = verb.get("ppp", NO_PPP)
    if _is_none_marker(expected_ppp):
        ok_ppp = _is_none_marker(given["ppp"])
    else:
        ok_ppp = norm_form(given["ppp"]) == norm_form(expected_ppp) != ""

    ok_meaning = False
    typed_forms = {re.sub(r"\s+", " ", f(given["meaning"])).strip() for f in (fold, fold_de)}
    variants = _meaning_variants(verb.get("meaning", ""))
    for typed in typed_forms:
        if len(typed) < 3:
            continue
        for variant in variants:
            if typed == variant or (len(variant) >= 3 and (variant in typed or typed in variant)):
                ok_meaning = True
                break
        if ok_meaning:
            break

    ok = {"pres": ok_pres, "perf": ok_perf, "ppp": ok_ppp, "meaning": ok_meaning}
    return {"ok": ok, "all_ok": all(ok.values()), "empty": not any(v.strip() for v in given.values())}


# ---------------------------------------------------------------------------
# Photo scan: prompt, reply parsing, token estimate
# ---------------------------------------------------------------------------

SCAN_PROMPT_VERSION = 3
SCAN_PROMPT = (
    "Extract the Latin verbs printed on this textbook page. Reply with ONLY a JSON array, no commentary. "
    "One object per verb with the keys: "
    '"inf" (infinitive as printed), '
    '"conj" ("1","2","3","M","4","irreg","semidep" or "dep"), '
    '"pres" (1st and 2nd person singular present, e.g. "amo, amas"; deponents "hortor, hortaris"), '
    '"perf" (1st person perfect as printed, e.g. "amavi"; "ausus sum" for semi-deponents), '
    '"ppp" (perfect passive participle as printed, "---" if none), '
    '"meaning" (the German meaning as printed), '
    '"lec" (lesson number as an integer if the page shows one, else 1), '
    '"cat" (the section heading above the verb, else ""). '
    "The present forms are usually NOT printed on the page: conjugate them yourself, and never leave any key empty. "
    "Only verbs that are on the page; do not invent any."
)

_FENCE = re.compile(r"^\s*```[A-Za-z0-9_-]*\s*\n?|\n?```\s*$")


def parse_scan_reply(text: Any) -> tuple[list[dict], list[list[dict]], Optional[str]]:
    """Turns an AI reply into (clean_rows, issues_per_row, fatal_error).
    Tolerates a code fence and chatter around the JSON; accepts a top-level
    array or an object holding one. Every row goes through normalize_verb
    with the scan defaults. fatal_error is set when no usable JSON array
    could be found at all."""
    if not isinstance(text, str) or not text.strip():
        return [], [], "The AI returned an empty reply."
    body = _FENCE.sub("", text.strip())
    data: Any = None
    try:
        data = json.loads(body)
    except ValueError:
        start = min((i for i in (body.find("["), body.find("{")) if i != -1), default=-1)
        if start != -1:
            try:
                data = parse_json5_prefix(body, start)
            except ValueError:
                data = None
    if isinstance(data, dict):
        for value in data.values():
            if isinstance(value, list):
                data = value
                break
    if not isinstance(data, list):
        return [], [], "The AI's reply wasn't a JSON list of verbs."
    rows: list[dict] = []
    all_issues: list[list[dict]] = []
    for item in data[:MAX_ROWS_PER_IMPORT]:
        clean, issues = normalize_verb(item, {"lec": 1, "cat": DEFAULT_CAT_SCAN})
        rows.append(clean)
        all_issues.append(issues)
    if not rows:
        return [], [], "The AI found no verbs on the page."
    return rows, all_issues, None


def scan_is_acceptable(rows: list[dict], all_issues: list[list[dict]]) -> bool:
    """Whether a scan result is good enough NOT to escalate to a bigger
    model: at least one row, and at least half of the rows free of errors."""
    if not rows:
        return False
    good = sum(1 for issues in all_issues if not has_errors(issues))
    return good >= 1 and good * 2 >= len(rows)


def estimate_scan_tokens(mime: str, size_bytes: int, width: Optional[int], height: Optional[int], pdf_pages: Optional[int],
                         prompt_chars: int, output_chars: int) -> dict:
    """A rough token estimate for ONE scan request, used only when ai-test
    doesn't report the real usage. Gemini bills an image as 258 tokens per
    768x768 tile (one tile when both sides are at most 384 px) and a PDF as
    258 per page; text is about 4 characters per token. Reported as an
    estimate, never as a measurement."""
    if mime == "application/pdf":
        media = 258 * max(1, pdf_pages or 1)
    elif width and height:
        media = 258 if (width <= 384 and height <= 384) else 258 * math.ceil(width / 768) * math.ceil(height / 768)
    else:
        media = 258 * max(1, math.ceil(size_bytes / 150_000))
    prompt_tokens = media + math.ceil(prompt_chars / 4)
    output_tokens = math.ceil(output_chars / 4)
    return {"prompt": prompt_tokens, "output": output_tokens, "total": prompt_tokens + output_tokens, "estimated": True}


def count_pdf_pages(data: bytes) -> Optional[int]:
    """Best-effort page count of a PDF (None if it can't be told)."""
    found = len(re.findall(rb"/Type\s*/Page(?![A-Za-z])", data))
    return found or None


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------

SCAN_MIME_TYPES = ("image/jpeg", "image/png", "image/webp", "application/pdf")
MAX_SCAN_IMAGE_BYTES = 4 * 1024 * 1024
MAX_SCAN_PDF_BYTES = 8 * 1024 * 1024


def decode_scan_payload(mime: Any, data_b64: Any) -> tuple[str, bytes]:
    """Validates a scan upload and returns (mime, raw bytes). Raises
    ValueError with a user-readable message."""
    if not isinstance(mime, str) or mime not in SCAN_MIME_TYPES:
        raise ValueError("Only JPEG, PNG, WebP images and PDF files can be scanned.")
    if not isinstance(data_b64, str) or not data_b64:
        raise ValueError("The file is empty.")
    try:
        raw = base64.b64decode(data_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("The file couldn't be read (bad encoding).") from exc
    limit = MAX_SCAN_PDF_BYTES if mime == "application/pdf" else MAX_SCAN_IMAGE_BYTES
    if len(raw) > limit:
        raise ValueError(f"That file is too large to scan ({len(raw) // (1024 * 1024)} MB, limit {limit // (1024 * 1024)} MB).")
    if mime == "image/jpeg" and not raw.startswith(b"\xff\xd8"):
        raise ValueError("The file isn't a JPEG image.")
    if mime == "image/png" and not raw.startswith(b"\x89PNG"):
        raise ValueError("The file isn't a PNG image.")
    if mime == "image/webp" and not (raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"):
        raise ValueError("The file isn't a WebP image.")
    if mime == "application/pdf" and not raw.startswith(b"%PDF"):
        raise ValueError("The file isn't a PDF.")
    return mime, raw


# ---------------------------------------------------------------------------
# Version compare (ai-test capability gate)
# ---------------------------------------------------------------------------

def version_tuple(text: Any) -> tuple[int, ...]:
    if not isinstance(text, str):
        return (0,)
    nums = re.findall(r"\d+", text)
    return tuple(int(n) for n in nums[:4]) or (0,)


def version_at_least(have: Any, need: str) -> bool:
    return version_tuple(have) >= version_tuple(need)

