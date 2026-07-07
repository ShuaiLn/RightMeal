"""Match confidence tests, including the 0.65 threshold boundary."""

from services.matching import match_confidence

THRESHOLD = 0.65


def test_exact_match_is_high():
    score = match_confidence(["large eggs"], "Large Eggs")
    assert score > 0.95


def test_good_product_match_clears_threshold():
    score = match_confidence(
        ["large eggs", "grade a eggs"], "Kroger Grade A Large Eggs, 12 ct"
    )
    assert score >= THRESHOLD


def test_unrelated_product_fails_threshold():
    score = match_confidence(["large eggs"], "Chocolate Chip Cookie Dough")
    assert score < THRESHOLD


def test_partially_related_product_fails_threshold():
    score = match_confidence(["whole milk"], "Whole Wheat Sandwich Bread")
    assert score < THRESHOLD


def test_case_and_punctuation_insensitive():
    a = match_confidence(["peanut butter"], "PEANUT BUTTER!!!")
    b = match_confidence(["peanut butter"], "peanut butter")
    assert a == b


def test_empty_inputs_score_zero():
    assert match_confidence([], "Eggs") == 0.0
    assert match_confidence(["eggs"], "") == 0.0


def test_deterministic():
    args = (["brown rice", "whole grain rice"], "Whole Grain Brown Rice 2 lb Bag")
    assert match_confidence(*args) == match_confidence(*args)
