"""Unit tests for the ported ES index + query builders (pure, no ES/DB).

These lock the shape of the ported ecom-elastic V3 algorithm: the analysis
settings that make transliteration/keyboard/diacritic search work, and the
function_score bool query's tier structure. Behavioural (live-ES) verification
is covered by the prod regression run — here we just guard the contract.
"""

from app.search.es.index import INDEX_BODY, LANGS, normalize_code
from app.search.es.query import build_search_body


def _should_clauses(body: dict) -> list[dict]:
    return body["query"]["bool"]["must"][0]["function_score"]["query"]["bool"]["should"]


class TestNormalizeCode:
    def test_strips_separators_and_uppercases(self) -> None:
        assert normalize_code("H-4010/2") == "H40102"
        assert normalize_code("br 100.a") == "BR100A"

    def test_empty(self) -> None:
        assert normalize_code("") == ""
        assert normalize_code(None) == ""


class TestIndexBody:
    def test_langs_are_ru_ro(self) -> None:
        assert LANGS == ("ru", "ro")

    def test_char_filters_present(self) -> None:
        cf = INDEX_BODY["settings"]["analysis"]["char_filter"]
        # The four mapping filters that replace the ICU plugin.
        assert {"ru_to_en_keyboard", "en_to_ru_keyboard", "translit_ru", "ro_diacritics"} <= set(cf)
        assert all(cf[name]["type"] == "mapping" for name in cf)

    def test_name_ru_has_keyboard_and_translit_subfields(self) -> None:
        fields = INDEX_BODY["mappings"]["properties"]["name_ru"]["fields"]
        assert {"exact", "prefix", "keyboard", "translit", "ngram"} <= set(fields)

    def test_name_ro_has_clean_subfield(self) -> None:
        fields = INDEX_BODY["mappings"]["properties"]["name_ro"]["fields"]
        assert "clean" in fields
        assert "keyboard" not in fields  # ro has no keyboard subfield

    def test_completion_fields_have_no_context(self) -> None:
        # Single-tenant → no group_id completion context (V3 delta).
        comp = INDEX_BODY["mappings"]["properties"]["name_ru_completion"]
        assert comp == {"type": "completion"}

    def test_code_prefix_subfield(self) -> None:
        code = INDEX_BODY["mappings"]["properties"]["code"]
        assert code["type"] == "keyword"
        assert code["fields"]["prefix"]["analyzer"] == "code_edge_ngram_analyzer"


class TestBuildSearchBody:
    def test_active_filter_and_pagination(self) -> None:
        body = build_search_body("samsung", size=24, from_=48)
        assert body["query"]["bool"]["filter"] == [{"term": {"is_active": True}}]
        assert body["size"] == 24
        assert body["from"] == 48
        assert body["_source"] == ["id"]

    def test_function_score_priority(self) -> None:
        fs = build_search_body("x", size=5)["query"]["bool"]["must"][0]["function_score"]
        assert fs["boost_mode"] == "sum"
        assert fs["query"]["bool"]["minimum_should_match"] == 1
        fn = fs["functions"][0]["field_value_factor"]
        assert fn["field"] == "priority" and fn["modifier"] == "log1p"

    def test_code_and_name_exact_tiers_present(self) -> None:
        clauses = _should_clauses(build_search_body("apple", size=5))
        # Exact code term at boost 1000.
        assert {"term": {"code": {"value": "apple", "boost": 1000}}} in clauses
        # Exact name at boost 2000 for each language.
        for lang in LANGS:
            assert {"term": {f"name_{lang}.exact": {"value": "apple", "boost": 2000}}} in clauses

    def test_single_token_adds_fuzzy_multitoken_does_not(self) -> None:
        single = _should_clauses(build_search_body("samsng", size=5))
        multi = _should_clauses(build_search_body("samsung s25", size=5))

        def has_fuzzy(clauses: list[dict]) -> bool:
            return any(
                "match" in c
                and any(isinstance(v, dict) and v.get("fuzziness") == "AUTO" for v in c["match"].values())
                for c in clauses
            )

        assert has_fuzzy(single) is True
        assert has_fuzzy(multi) is False

    def test_exact_match_mode_has_no_function_score(self) -> None:
        body = build_search_body("24951", size=5, exact_match=True)
        must = body["query"]["bool"]["must"][0]
        assert "function_score" not in must
        assert "bool" in must and must["bool"]["minimum_should_match"] == 1

    def test_suggest_attached_when_prefix_given(self) -> None:
        body = build_search_body("sam", size=5, suggest_prefix="sam")
        assert "suggest" in body
        assert body["suggest"]["name_ru_suggest"]["completion"]["field"] == "name_ru_completion"

    def test_min_score_included_only_when_positive(self) -> None:
        assert "min_score" not in build_search_body("x", size=5, min_score=0)
        assert build_search_body("x", size=5, min_score=12.5)["min_score"] == 12.5
