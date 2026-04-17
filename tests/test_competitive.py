from signal_agent.ingestors.competitive import _co_occurs, COMPETITORS


def test_co_occurs_close_together():
    text = "We evaluated Arthur against Arize and Credo AI for governance."
    assert _co_occurs(text, "Arthur", "Arize") is True
    assert _co_occurs(text, "Arthur", "Credo AI") is True


def test_co_occurs_missing_one_term():
    text = "Acme just launched a new AI product."
    assert _co_occurs(text, "Acme", "Arize") is False


def test_co_occurs_too_far_apart():
    spacer = "x " * 400
    text = f"Arize is interesting. {spacer}Acme just hired a Head of AI."
    # Window default is 600 chars; 800+ chars apart should fail.
    assert _co_occurs(text, "Acme", "Arize", window=300) is False


def test_co_occurs_case_insensitive():
    text = "ACME loves ARIZE for observability."
    assert _co_occurs(text, "acme", "arize") is True


def test_competitors_list_includes_key_vendors():
    # Sanity check: the critical Arthur competitors should be present.
    critical = {"Credo AI", "ModelOp", "WitnessAI", "Arize"}
    assert critical.issubset(set(COMPETITORS))
