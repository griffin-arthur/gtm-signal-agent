from signal_agent.ingestors.html_util import strip_html


def test_removes_tags_and_decodes_entities():
    out = strip_html("<p>Hello &amp; welcome</p>")
    assert out == "Hello & welcome"


def test_script_contents_dropped():
    out = strip_html("<div>keep</div><script>alert('bad')</script><p>also keep</p>")
    assert "alert" not in out
    assert "keep" in out
    assert "also keep" in out


def test_block_tags_produce_newlines():
    out = strip_html("<p>One</p><p>Two</p><p>Three</p>")
    assert out == "One\nTwo\nThree"


def test_collapses_internal_whitespace():
    out = strip_html("<p>word    spaced\t\tout</p>")
    assert out == "word spaced out"


def test_handles_empty_and_none():
    assert strip_html(None) == ""
    assert strip_html("") == ""


def test_malformed_html_falls_back():
    # Unclosed tag shouldn't crash.
    out = strip_html("<div>text without close")
    assert "text without close" in out


def test_list_items_newline_separated():
    out = strip_html("<ul><li>A</li><li>B</li></ul>")
    assert out == "A\nB"
