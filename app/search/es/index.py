"""Elasticsearch index definition — ported from ecom-elastic ``ProductDocumentV3``.

This is the single source of truth for the index *shape*: analysis settings
(char filters, tokenizers, analyzers, normalizer) and field mappings. Kept as
plain ``dict`` literals (not ``elasticsearch-dsl`` ``Document`` classes) so the
body is portable, diff-able and can be dumped/validated standalone.

Port deltas vs V3 (§4 of the plan):

* Languages **ru + ro** only (V3 also had en/uk). ``name_ru`` carries the
  keyboard-layout + transliteration subfields; ``name_ro`` carries the
  diacritic-fold ``.clean`` subfield — same split as V3.
* **No ``group_id``** (single-tenant) and **no completion contexts**.
* **No brand / additional_codes / group_product_id / custom_entity** fields —
  evix has no such columns. Their query tiers are dropped in ``query.py``.
* Adds ``category_{lang}``, ``attrs_{lang}`` (attribute values) and low-boost
  ``desc_{lang}`` — the evix analogues of V3's category/keywords signals.
* The keyboard/transliteration magic uses ``char_filter`` of ``type: mapping``
  exactly as V3 — **no ICU plugin required**.
"""

import re

# Languages indexed. Mirrors ``app.models.catalog.ALLOWED_LANGS`` but kept local
# so this module has no ORM import (it must be dumpable standalone for validation).
LANGS: tuple[str, ...] = ("ru", "ro")

_CODE_SEP_RE = re.compile(r"[-/,.\s]")


def normalize_code(code: str | None) -> str:
    """Strip separators and upper-case an article code (V3 ``normalize_code``).

    ``"H-4010/2"`` → ``"H40102"``. Used both when indexing (``normalized_code``)
    and when querying, so a search for ``"h 4010"`` reaches ``"H-4010"``.
    """
    if not code:
        return ""
    return _CODE_SEP_RE.sub("", code).upper()


# --- Analysis (verbatim port of the V3 char_filters / analyzers, ru+ro) -------

_ANALYSIS: dict = {
    "normalizer": {
        "lowercase_normalizer": {
            "type": "custom",
            "filter": ["lowercase", "asciifolding"],
        }
    },
    "char_filter": {
        # RU keyboard (ЙЦУКЕН) typed while the layout was Latin, and vice-versa.
        "ru_to_en_keyboard": {
            "type": "mapping",
            "mappings": [
                "й => q", "ц => w", "у => e", "к => r", "е => t", "н => y",
                "г => u", "ш => i", "щ => o", "з => p", "х => [", "ъ => ]",
                "ф => a", "ы => s", "в => d", "а => f", "п => g", "р => h",
                "о => j", "л => k", "д => l", "ж => ;", "э => '",
                "я => z", "ч => x", "с => c", "м => v", "и => b", "т => n",
                "ь => m", "б => ,", "ю => .",
                "Й => Q", "Ц => W", "У => E", "К => R", "Е => T", "Н => Y",
                "Г => U", "Ш => I", "Щ => O", "З => P", "Х => [", "Ъ => ]",
                "Ф => A", "Ы => S", "В => D", "А => F", "П => G", "Р => H",
                "О => J", "Л => K", "Д => L", "Ж => :", "Э => \"",
                "Я => Z", "Ч => X", "С => C", "М => V", "И => B", "Т => N",
                "Ь => M", "Б => <", "Ю => >",
            ],
        },
        "en_to_ru_keyboard": {
            "type": "mapping",
            "mappings": [
                "q => й", "w => ц", "e => у", "r => к", "t => е", "y => н",
                "u => г", "i => ш", "o => щ", "p => з", "[ => х", "] => ъ",
                "a => ф", "s => ы", "d => в", "f => а", "g => п", "h => р",
                "j => о", "k => л", "l => д", "; => ж", "' => э",
                "z => я", "x => ч", "c => с", "v => м", "b => и", "n => т",
                "m => ь", ", => б", ". => ю",
                "Q => Й", "W => Ц", "E => У", "R => К", "T => Е", "Y => Н",
                "U => Г", "I => Ш", "O => Щ", "P => З",
                "A => Ф", "S => Ы", "D => В", "F => А", "G => П", "H => Р",
                "J => О", "K => Л", "L => Д",
                "Z => Я", "X => Ч", "C => С", "V => М", "B => И", "N => Т",
                "M => Ь",
            ],
        },
        # Cyrillic → Latin transliteration (so "Bronhipret" finds "Бронхипрет").
        "translit_ru": {
            "type": "mapping",
            "mappings": [
                "а => a", "б => b", "в => v", "г => g", "д => d", "е => e",
                "ё => yo", "ж => zh", "з => z", "и => i", "й => y", "к => k",
                "л => l", "м => m", "н => n", "о => o", "п => p", "р => r",
                "с => s", "т => t", "у => u", "ф => f", "х => kh", "ц => ts",
                "ч => ch", "ш => sh", "щ => shch", "ъ => ", "ы => y", "ь => ",
                "э => e", "ю => yu", "я => ya",
                "А => A", "Б => B", "В => V", "Г => G", "Д => D", "Е => E",
                "Ё => YO", "Ж => ZH", "З => Z", "И => I", "Й => Y", "К => K",
                "Л => L", "М => M", "Н => N", "О => O", "П => P", "Р => R",
                "С => S", "Т => T", "У => U", "Ф => F", "Х => KH", "Ц => TS",
                "Ч => CH", "Ш => SH", "Щ => SHCH", "Ъ => ", "Ы => Y", "Ь => ",
                "Э => E", "Ю => YU", "Я => YA",
            ],
        },
        # Romanian diacritic fold (Moldovan keyboards often omit ă/î/ș/ț/â).
        "ro_diacritics": {
            "type": "mapping",
            "mappings": [
                "ă => a", "î => i", "ș => s", "ț => t", "â => a",
                "Ă => A", "Î => I", "Ș => S", "Ț => T", "Â => A",
            ],
        },
    },
    "tokenizer": {
        "edge_ngram_tokenizer": {
            "type": "edge_ngram",
            "min_gram": 2,
            "max_gram": 15,
            "token_chars": ["letter", "digit"],
        },
        "code_edge_ngram_tokenizer": {
            "type": "edge_ngram",
            "min_gram": 2,
            "max_gram": 20,
            "token_chars": ["letter", "digit", "symbol", "punctuation"],
        },
        "ngram_fallback_tokenizer": {
            "type": "ngram",
            "min_gram": 3,
            "max_gram": 4,
            "token_chars": ["letter", "digit"],
        },
    },
    "analyzer": {
        "standard_clean": {
            "tokenizer": "standard",
            "filter": ["lowercase", "asciifolding"],
        },
        "edge_ngram_analyzer": {
            "tokenizer": "edge_ngram_tokenizer",
            "filter": ["lowercase", "asciifolding"],
        },
        # Search-time analyzer for edge_ngram fields: plain standard, so the
        # query is NOT itself edge-ngrammed (that would match too much).
        "edge_ngram_search": {
            "tokenizer": "standard",
            "filter": ["lowercase", "asciifolding"],
        },
        "keyboard_ru_analyzer": {
            "tokenizer": "standard",
            "filter": ["lowercase"],
            "char_filter": ["ru_to_en_keyboard"],
        },
        "keyboard_en_analyzer": {
            "tokenizer": "standard",
            "filter": ["lowercase"],
            "char_filter": ["en_to_ru_keyboard"],
        },
        "translit_ru_analyzer": {
            "tokenizer": "standard",
            "filter": ["lowercase", "asciifolding"],
            "char_filter": ["translit_ru"],
        },
        "ro_clean_analyzer": {
            "tokenizer": "standard",
            "filter": ["lowercase", "asciifolding"],
            "char_filter": ["ro_diacritics"],
        },
        "code_edge_ngram_analyzer": {
            "tokenizer": "code_edge_ngram_tokenizer",
            "filter": ["uppercase"],
        },
        "code_search_analyzer": {
            "tokenizer": "keyword",
            "filter": ["uppercase"],
        },
        "ngram_fallback_analyzer": {
            "tokenizer": "ngram_fallback_tokenizer",
            "filter": ["lowercase", "asciifolding"],
        },
        "ngram_fallback_search": {
            "tokenizer": "ngram_fallback_tokenizer",
            "filter": ["lowercase", "asciifolding"],
        },
    },
}


def _name_field(lang: str) -> dict:
    """Multi-field mapping for ``name_{lang}`` (subfields depend on language)."""
    fields: dict = {
        "exact": {"type": "keyword", "normalizer": "lowercase_normalizer"},
        "prefix": {
            "type": "text",
            "analyzer": "edge_ngram_analyzer",
            "search_analyzer": "edge_ngram_search",
        },
        "ngram": {
            "type": "text",
            "analyzer": "ngram_fallback_analyzer",
            "search_analyzer": "ngram_fallback_search",
        },
    }
    if lang == "ru":
        fields["keyboard"] = {"type": "text", "analyzer": "keyboard_ru_analyzer"}
        fields["translit"] = {"type": "text", "analyzer": "translit_ru_analyzer"}
    if lang == "ro":
        fields["clean"] = {"type": "text", "analyzer": "ro_clean_analyzer"}
    return {"type": "text", "analyzer": "standard_clean", "fields": fields}


def _build_mappings() -> dict:
    props: dict = {
        "id": {"type": "long"},
        "is_active": {"type": "boolean"},
        "priority": {"type": "integer"},
        # Code: exact keyword + edge_ngram prefix subfield.
        "code": {
            "type": "keyword",
            "fields": {
                "prefix": {
                    "type": "text",
                    "analyzer": "code_edge_ngram_analyzer",
                    "search_analyzer": "code_search_analyzer",
                }
            },
        },
        "normalized_code": {"type": "keyword"},
    }
    for lang in LANGS:
        props[f"name_{lang}"] = _name_field(lang)
        props[f"category_{lang}"] = {"type": "text", "analyzer": "standard_clean"}
        props[f"attrs_{lang}"] = {"type": "text", "analyzer": "standard_clean"}
        props[f"desc_{lang}"] = {"type": "text", "analyzer": "standard_clean"}
        # Completion field with an explicit ``standard_clean`` analyzer: the
        # default ``simple`` analyzer DROPS digits, so a prefix like "3d" would
        # collapse to "d" and suggest every name starting with D. standard_clean
        # keeps "3d" as a token and folds diacritics (so "cas" suggests "Căști").
        props[f"name_{lang}_completion"] = {
            "type": "completion",
            "analyzer": "standard_clean",
            "search_analyzer": "standard_clean",
        }
    return {"properties": props}


# The full index creation body (``PUT /<index>``).
INDEX_BODY: dict = {
    "settings": {
        "number_of_replicas": 0,
        "number_of_shards": 1,
        "max_ngram_diff": 13,
        "analysis": _ANALYSIS,
    },
    "mappings": _build_mappings(),
}
