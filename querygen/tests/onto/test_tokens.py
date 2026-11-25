import pytest

from app.odoo.tokens import has_token_shape, token_hash

GOOD = "eyJ2IjoxLCJ0eXAiOiJxdWVyeSJ9.dGhpcy1pcy1hLW1hYw"


def test_two_base64url_parts_and_a_dot_is_a_token():
    assert has_token_shape(GOOD)
    assert has_token_shape("a-_.b_-")


@pytest.mark.parametrize("bad", [
    None, "", "nodot", ".b", "a.", "a.b.c", "a b.c", "a+b.c", "a/b.c", "a=.b",
    "Bearer " + GOOD, "x" * 5000 + ".y",
])
def test_everything_else_is_not(bad):
    assert not has_token_shape(bad)


def test_newline_or_inner_space_is_not_a_token():
    # `$` matches before a trailing newline; fullmatch must refuse it, as Odoo's fullmatch does.
    assert not has_token_shape(GOOD + "\n")
    assert not has_token_shape("\n" + GOOD)
    assert not has_token_shape(GOOD[:10] + " " + GOOD[10:])


def test_a_raw_api_key_has_no_token_shape():
    # Mobile API keys are single opaque strings (no dot): they must be refused.
    assert not has_token_shape("odk_live_0123456789abcdef0123456789abcdef")


def test_hash_is_stable_and_hides_the_token():
    h = token_hash(GOOD)
    assert h == token_hash(GOOD) and len(h) == 64 and GOOD not in h
    assert token_hash(GOOD + "x") != h
