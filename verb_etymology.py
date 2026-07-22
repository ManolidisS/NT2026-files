"""
verb_etymology.py

Classify English verbs as 'Latinate', 'Germanic', or 'Unknown/Mixed' using the
kaikki.org wiktextract SQLite database (built by your extraction script).

Usage:
    python verb_etymology.py word1 [word2 ...]    # classify words from CLI
    python verb_etymology.py --test               # run sanity-check word list
    python verb_etymology.py --build              # pre-classify ALL verbs into
                                                  # a 'verb_origins' table

As a library:
    from verb_etymology import classify
    classify("street")   -> 'Latinate'
    classify("father")   -> 'Germanic'
"""

import json
import re
import sqlite3
import sys
from functools import lru_cache

DB_PATH = "/experiment/etymology/english_wiktionary.db"

LATINATE_LABEL = "Latinate"
GERMANIC_LABEL = "Germanic"
UNKNOWN_LABEL = "Unknown/Mixed"

# --------------------------------------------------------------------------
# Language-code sets (Wiktionary codes as they appear in etymology templates)
# --------------------------------------------------------------------------

LATINATE_CODES = {
    # Latin and its etymology-only variants (both legacy and modern codes)
    "la", "LL.", "ML.", "VL.", "NL.", "EL.", "RL.",
    "la-lat", "la-cla", "la-med", "la-vul", "la-new", "la-ecc", "la-ren",
    # Romance languages
    "fr", "frm", "fro", "xno", "nrf", "ONF.", "wa",          # French family
    "it", "roa-oit", "scn", "nap", "vec", "co", "sc",        # Italian family
    "es", "osp", "pt", "roa-opt", "gl", "ca", "oc", "pro",   # Iberian/Occitan
    "ro", "rm", "fur", "lld", "ist", "dlm",                  # other Romance
    # Hellenic (treated as Latinate per project decision)
    "grc", "el", "gkm", "grk-pro",
}

GERMANIC_CODES = {
    # English stages & close siblings (decisive only if nothing deeper found)
    "enm", "ang", "sco",
    # Proto-stages
    "gem-pro", "gmw-pro", "gmq-pro",
    # North Germanic
    "non", "non-oen", "non-own", "is", "fo", "da", "sv", "no", "nb", "nn",
    # West Germanic
    "nl", "dum", "odt", "vls", "zea", "li", "af",
    "de", "gmh", "goh", "lb", "yi",
    "nds", "nds-de", "nds-nl", "gml", "osx",
    "fy", "ofs", "frr", "stq",
    "frk",  # Frankish (source of many Old French words of Germanic origin)
    # East Germanic
    "got",
}

# Codes that explicitly do NOT decide anything (we keep walking the chain)
# e.g. "ine-pro" (PIE), "en" (modern-English formations -> handled via
# morphology templates instead).

# --------------------------------------------------------------------------
# Template names
# --------------------------------------------------------------------------

DERIVATION_TEMPLATES = {
    "inh", "inherited", "inh+",
    "bor", "borrowed", "bor+",
    "der", "derived",
    "lbor", "learned borrowing",
    "slbor", "semi-learned borrowing",
    "obor", "orthographic borrowing",
    "ubor", "unadapted borrowing",
    "psm", "phono-semantic matching",
}

MORPHOLOGY_TEMPLATES = {
    "af", "affix", "suf", "suffix", "pre", "prefix", "con", "confix",
    "com", "compound", "blend", "univerbation",
}

# --------------------------------------------------------------------------
# Prose fallback (used only when structured templates are missing)
# --------------------------------------------------------------------------

# Cut the prose at the first marker that introduces cognates / asides,
# so "Cognate with Latin pater" does not poison Germanic words.
_TRUNCATE_RE = re.compile(
    r"(Cognate|cognate|Compare\s|compare\s|Related to|related to|"
    r"Doublet of|doublet of|More at|See also|Displaced|displaced|"
    r"Equivalent to|equivalent to|Akin to|akin to)"
)

_PROSE_PHRASES = [
    # (phrase, class) -- longer phrases first so alternation prefers them
    ("Proto-West Germanic", GERMANIC_LABEL),
    ("Proto-Germanic", GERMANIC_LABEL),
    ("Old English", GERMANIC_LABEL),
    ("Middle English", GERMANIC_LABEL),
    ("Old Norse", GERMANIC_LABEL),
    ("Old High German", GERMANIC_LABEL),
    ("Middle High German", GERMANIC_LABEL),
    ("Middle Low German", GERMANIC_LABEL),
    ("Old Saxon", GERMANIC_LABEL),
    ("Old Frisian", GERMANIC_LABEL),
    ("Old Dutch", GERMANIC_LABEL),
    ("Middle Dutch", GERMANIC_LABEL),
    ("Low German", GERMANIC_LABEL),
    ("Dutch", GERMANIC_LABEL),
    ("Germanic", GERMANIC_LABEL),
    ("German", GERMANIC_LABEL),
    ("Frankish", GERMANIC_LABEL),
    ("Gothic", GERMANIC_LABEL),
    ("Danish", GERMANIC_LABEL),
    ("Swedish", GERMANIC_LABEL),
    ("Norwegian", GERMANIC_LABEL),
    ("Icelandic", GERMANIC_LABEL),
    ("Yiddish", GERMANIC_LABEL),
    ("Scots", GERMANIC_LABEL),
    ("Old Northern French", LATINATE_LABEL),
    ("Anglo-Norman", LATINATE_LABEL),
    ("Old French", LATINATE_LABEL),
    ("Middle French", LATINATE_LABEL),
    ("Norman", LATINATE_LABEL),
    ("French", LATINATE_LABEL),
    ("Latin", LATINATE_LABEL),     # also matches Late/Vulgar/Medieval Latin
    ("Italian", LATINATE_LABEL),
    ("Spanish", LATINATE_LABEL),
    ("Portuguese", LATINATE_LABEL),
    ("Catalan", LATINATE_LABEL),
    ("Occitan", LATINATE_LABEL),
    ("Romanian", LATINATE_LABEL),
    ("Ancient Greek", LATINATE_LABEL),
    ("Koine Greek", LATINATE_LABEL),
    ("Byzantine Greek", LATINATE_LABEL),
    ("Greek", LATINATE_LABEL),
]
_PROSE_RE = re.compile(
    r"\b(" + "|".join(re.escape(p) for p, _ in _PROSE_PHRASES) + r")\b"
)
_PROSE_MAP = dict(_PROSE_PHRASES)

# --------------------------------------------------------------------------
# Database connection
# --------------------------------------------------------------------------

_conn = None

def get_conn(db_path=DB_PATH):
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(db_path)
    return _conn

# --------------------------------------------------------------------------
# Entry-level classification
# --------------------------------------------------------------------------

def _chain_langs(entry):
    """Ordered list of source-language codes from the derivation chain."""
    langs = []
    for t in entry.get("etymology_templates") or []:
        if t.get("name") in DERIVATION_TEMPLATES:
            code = (t.get("args") or {}).get("2", "")
            if code:
                langs.append(code)
    return langs


def _classify_chain(langs):
    """Deepest decisive ancestor wins. Returns label or None."""
    for code in reversed(langs):
        if code in LATINATE_CODES:
            return LATINATE_LABEL
        if code in GERMANIC_CODES:
            return GERMANIC_LABEL
    return None


def _morphology_parts(entry):
    """Component morphemes from affix/compound templates ({{suffix|en|odd|ity}} ...)."""
    parts = []
    for t in entry.get("etymology_templates") or []:
        name = t.get("name")
        if name in MORPHOLOGY_TEMPLATES:
            args = t.get("args") or {}
            if args.get("1") != "en":
                continue
            i = 2
            while str(i) in args:
                p = args[str(i)].strip()
                if p:
                    parts.append((name, i, p))
                i += 1
    return parts


def _classify_prose(text):
    """Fallback: regex over the plaintext etymology, deepest mention wins."""
    if not text:
        return None
    m = _TRUNCATE_RE.search(text)
    if m:
        text = text[: m.start()]
    matches = list(_PROSE_RE.finditer(text))
    if not matches:
        return None
    return _PROSE_MAP[matches[-1].group(1)]


def _classify_entry(entry, depth=0):
    """Classify a single Wiktionary entry (one homograph/etymology)."""
    # 1. Structured derivation chain (most reliable)
    label = _classify_chain(_chain_langs(entry))
    if label:
        return label

    # 2. Word formed within English: classify its morphemes recursively
    if depth < 2:
        parts = _morphology_parts(entry)
        if parts:
            results = set()
            for name, i, p in parts:
                # suffix args may be stored without the hyphen
                candidates = [p]
                if not p.startswith("-") and not p.endswith("-"):
                    if name in ("suf", "suffix") and i >= 3:
                        candidates.insert(0, "-" + p)
                    if name in ("pre", "prefix") and i == 2:
                        candidates.insert(0, p + "-")
                for cand in candidates:
                    r = _classify_word(cand, depth + 1, any_pos=True)
                    if r != UNKNOWN_LABEL:
                        results.add(r)
                        break
            if len(results) == 1:
                return results.pop()
            if len(results) > 1:
                return UNKNOWN_LABEL  # genuinely mixed, e.g. odd + -ity

    # 3. Inflected form ("dogs") -> classify the lemma
    if depth < 2:
        for sense in entry.get("senses") or []:
            for fo in sense.get("form_of") or []:
                lemma = fo.get("word")
                if lemma and lemma != entry.get("word"):
                    r = _classify_word(lemma, depth + 1)
                    if r != UNKNOWN_LABEL:
                        return r

    # 4. Prose fallback
    label = _classify_prose(entry.get("etymology_text"))
    if label:
        return label

    return UNKNOWN_LABEL

# --------------------------------------------------------------------------
# Word-level classification
# --------------------------------------------------------------------------

def _fetch_entries(word, any_pos=False):
    cur = get_conn().cursor()
    for w in (word, word.lower(), word.capitalize()):
        if any_pos:
            cur.execute("SELECT data FROM dictionary WHERE word = ? LIMIT 20", (w,))
        else:
            cur.execute(
                "SELECT data FROM dictionary WHERE word = ? AND pos = 'verb' LIMIT 20",
                (w,),
            )
        rows = cur.fetchall()
        if rows:
            return [json.loads(r[0]) for r in rows]
    return []


def _combine(labels):
    decisive = {l for l in labels if l != UNKNOWN_LABEL}
    if len(decisive) == 1:
        return decisive.pop()
    if len(decisive) > 1:
        return UNKNOWN_LABEL  # conflicting homographs -> Mixed
    return UNKNOWN_LABEL


def _classify_word(word, depth=0, any_pos=False):
    entries = _fetch_entries(word, any_pos=any_pos)
    if not entries:
        # last resort for verb lookups: try any part of speech
        if not any_pos:
            entries = _fetch_entries(word, any_pos=True)
        if not entries:
            return UNKNOWN_LABEL
    return _combine(_classify_entry(e, depth) for e in entries)


@lru_cache(maxsize=200_000)
def classify(word):
    """Public API: classify a verb as Latinate / Germanic / Unknown-Mixed."""
    return _classify_word(word.strip())

# --------------------------------------------------------------------------
# Batch pre-classification of every verb in the DB
# --------------------------------------------------------------------------

def build_origin_table(db_path=DB_PATH):
    """One-shot pass: classify every verb and store in 'verb_origins'.
    Afterwards your training loop can do pure indexed lookups:
        SELECT origin FROM verb_origins WHERE word = ?
    """
    import time

    conn = get_conn(db_path)
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS verb_origins")
    cur.execute("CREATE TABLE verb_origins (word TEXT PRIMARY KEY, origin TEXT)")

    print("Collecting per-entry classifications for all verbs...")
    t0 = time.time()
    word_labels = {}
    read_cur = conn.cursor()  # separate cursor: we iterate while classifying
    read_cur.execute("SELECT word, data FROM dictionary WHERE pos = 'verb'")
    n = 0
    while True:
        rows = read_cur.fetchmany(10_000)
        if not rows:
            break
        for word, data in rows:
            try:
                entry = json.loads(data)
            except json.JSONDecodeError:
                continue
            word_labels.setdefault(word, []).append(_classify_entry(entry))
            n += 1
        if n % 100_000 < 10_000:
            print(f"  processed {n:,} verb entries...")

    print(f"Combining and writing {len(word_labels):,} distinct verbs...")
    cur.executemany(
        "INSERT OR REPLACE INTO verb_origins VALUES (?, ?)",
        ((w, _combine(ls)) for w, ls in word_labels.items()),
    )
    conn.commit()

    cur.execute("SELECT origin, COUNT(*) FROM verb_origins GROUP BY origin")
    for origin, c in cur.fetchall():
        print(f"  {origin:14s} {c:,}")
    print(f"Done in {(time.time() - t0) / 60:.1f} minutes.")

# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

TEST_WORDS = [
    "father", "water", "street", "wine", "table", "mountain", "sky",
    "animal", "knowledge", "democracy", "kingdom", "oddity", "war",
    "cheese", "house", "city", "freedom", "liberty", "dog", "dogs",
]

if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print(__doc__)
    elif args[0] == "--build":
        build_origin_table()
    elif args[0] == "--test":
        for w in TEST_WORDS:
            print(f"{w:12s} -> {classify(w)}")
    else:
        for w in args:
            print(f"{w:12s} -> {classify(w)}")
