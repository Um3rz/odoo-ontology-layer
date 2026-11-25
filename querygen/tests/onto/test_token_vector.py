"""Querygen consumes the cross-repo token vector (plan contract 3.1).

The vector is copied verbatim from od-demo-addons
(odoo_dashboards_agents/tests/onto_token_vector.json, branch onto/p0-store).
Querygen checks shape only; Odoo verifies signatures. These tests pin what
querygen does with the vector's tokens and nothing more.
"""
import json
from pathlib import Path

import pytest

from app.odoo import tokens
from app.odoo.tokens import has_token_shape

VECTOR = json.loads(
    (Path(__file__).parent / "fixtures" / "onto_token_vector.json").read_text(encoding="utf-8")
)
KINDS = ("query", "bot")

# Shape-level negatives derived from each vector token. The vector itself has no tamper section.
MUTATIONS = {
    "base64_padding": lambda e: e["token"] + "=",
    "standard_base64_plus": lambda e: e["token"] + "+",
    "three_parts": lambda e: e["token"] + ".x",
    "empty_signature": lambda e: e["token"].split(".")[0] + ".",
    "empty_payload": lambda e: "." + e["token"].split(".")[1],
    "bearer_prefix": lambda e: "Bearer " + e["token"],
    "payload_json_not_a_token": lambda e: e["signed_bytes"],
}


def test_querygen_shape_regex_is_the_vector_regex():
    assert tokens._SHAPE.pattern == VECTOR["shape_regex"]


@pytest.mark.parametrize("kind", KINDS)
def test_vector_token_is_accepted(kind):
    assert has_token_shape(VECTOR[kind]["token"])


@pytest.mark.parametrize("kind", KINDS)
def test_tampered_signature_still_passes_shape(kind):
    # Mirrors Odoo's test_tampered_signature_is_refused: flip one signature character.
    # Querygen must not reject it here; the refusal is Odoo's whoami.
    head, sig = VECTOR[kind]["token"].split(".")
    tampered = head + "." + ("A" if sig[0] != "A" else "B") + sig[1:]
    assert tampered != VECTOR[kind]["token"]
    assert has_token_shape(tampered)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("mutation", MUTATIONS)
def test_mutated_vector_token_is_refused(kind, mutation):
    assert not has_token_shape(MUTATIONS[mutation](VECTOR[kind]))
