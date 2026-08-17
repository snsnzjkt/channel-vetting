"""
Tests for the render boundary.

This is where attacker-controlled text meets an RFC-822 message signed by the
brand's own domain, so most of these are about what must NOT come out the other
side: injected headers, a javascript: href, a mangled greeting.
"""
import hashlib

import pytest

import outreach_templates as T


def _render(niche="Home Theater", **kw):
    base = dict(
        channel_name="Bane Tech",
        channel_url="https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv",
        channel_id="UCabcdefghijklmnopqrstuv",
    )
    base.update(kw)
    return T.render(niche, **base)


# --- Both niches render ------------------------------------------------------

@pytest.mark.parametrize("niche", ["Home Theater", "Lifestyle Sofa"])
def test_both_niches_render_both_parts(niche):
    out = _render(niche)
    assert out["subject"]
    assert "Bane Tech" in out["text"]
    assert "Bane Tech" in out["html"]
    assert out["template_version"] == T.TEMPLATE_VERSION


def test_templates_cover_every_niche_in_the_registry():
    """A niche with no template must be caught here, not at send time."""
    from niches import NICHES

    assert set(NICHES) <= set(T.TEMPLATES), (
        f"niches without a template: {set(NICHES) - set(T.TEMPLATES)}"
    )


def test_unknown_niche_raises_rather_than_guessing():
    with pytest.raises(T.TemplateError) as exc:
        _render("Kitchen Islands")
    assert "no template" in str(exc.value)


def test_the_two_niches_get_different_copy():
    """F13: the wrong template is brand damage, not a cosmetic slip."""
    ht = _render("Home Theater")["text"]
    ls = _render("Lifestyle Sofa")["text"]
    assert ht != ls
    assert "home theater seating" in ht
    assert "Lifestyle Series" in ls


# --- Header injection --------------------------------------------------------

@pytest.mark.parametrize(
    "evil",
    [
        "Ch\r\nBcc: evil@attacker.tld",
        "Ch\nBcc: evil@attacker.tld",
        "Ch\rSubject: hijacked",
        "Ch\x00null",
    ],
)
def test_control_characters_are_stripped_from_every_substituted_value(evil):
    out = _render(channel_name=evil)
    for part in (out["subject"], out["text"], out["html"]):
        assert "\r" not in part
        assert "\x00" not in part
    # The injected header text may survive as inert body text, but never as a
    # line of its own that a parser could read as a header.
    assert "\nBcc:" not in out["text"]
    assert "\nBcc:" not in out["html"]


def test_strip_control_chars_is_exposed_for_the_mailer_to_reuse():
    assert T.strip_control_chars("a\r\nb\x00c") == "abc"


# --- URL validation ----------------------------------------------------------

@pytest.mark.parametrize(
    "hostile",
    [
        "javascript:alert(1)",
        "https://www.youtube.com.evil.tld/@bane",
        "http://www.youtube.com/@bane",          # not https
        "https://youtube.com/@bane",             # missing www, not our written shape
        "  javascript:alert(1)  ",
    ],
)
def test_hostile_url_is_rebuilt_from_the_channel_id_not_escaped(hostile):
    """Escaping is not validation — the href must never carry the input."""
    out = _render(channel_url=hostile)
    assert hostile.strip() not in out["html"]
    assert "https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv" in out["html"]


def test_handle_form_url_is_accepted():
    out = _render(channel_url="https://www.youtube.com/@banetech")
    assert "https://www.youtube.com/@banetech" in out["html"]


def test_unsafe_url_with_no_usable_channel_id_raises():
    with pytest.raises(T.TemplateError) as exc:
        _render(channel_url="javascript:alert(1)", channel_id="")
    assert "rebuild" in str(exc.value)


def test_bad_channel_id_shape_is_not_trusted_either():
    with pytest.raises(T.TemplateError):
        _render(channel_url="javascript:alert(1)", channel_id="not-a-channel-id")


# --- HTML escaping -----------------------------------------------------------

def test_channel_name_is_html_escaped_in_the_html_part_only():
    out = _render(channel_name='Bane <script>alert("x")</script>')
    assert "<script>" not in out["html"]
    assert "&lt;script&gt;" in out["html"]
    # Plaintext is not an HTML context; escaping it would show entities to the
    # reader in a client that renders the text/plain alternative.
    assert "<script>" in out["text"]


# --- csv_safe round trip -----------------------------------------------------

def test_csv_safe_apostrophe_is_undone_before_greeting():
    """Stored as "'-Bob AV"; must not greet "Hey '-Bob AV,"."""
    out = _render(channel_name="'-Bob AV")
    assert "Hey -Bob AV," in out["text"]
    assert "'-Bob AV" not in out["text"]


def test_an_honest_leading_apostrophe_survives():
    out = _render(channel_name="'Round Midnight Audio")
    assert "'Round Midnight Audio" in out["text"]


# --- Empty / junk names ------------------------------------------------------

@pytest.mark.parametrize("blank", ["", "   ", "!!!", "---", None])
def test_blank_or_punctuation_only_name_falls_back(blank):
    out = _render(channel_name=blank)
    assert f"Hey {T.FALLBACK_GREETING_NAME}," in out["text"]
    assert "Hey ," not in out["text"]


# --- Placeholder allowlist ---------------------------------------------------

def test_unknown_placeholder_raises_rather_than_half_rendering():
    """A literal "{channel_nmae}" reaching a creator is worse than a crash."""
    with pytest.raises(T.TemplateError) as exc:
        T._substitute("Hi {channel_nmae}", {"channel_name": "x", "channel_url": "y"})
    assert "channel_nmae" in str(exc.value)


def test_a_placeholder_token_in_the_channel_name_is_not_expanded():
    """
    Regression: substituting one key at a time re-scanned text a previous
    substitution had just inserted, so a creator named "{channel_url}" got it
    expanded. Worse, the loop iterated a frozenset, so WHETHER it happened
    depended on PYTHONHASHSEED — injected on seeds 0/1/2, clean on 3/4/5.
    Single-pass substitution makes it unreachable.
    """
    out = _render(
        channel_name="{channel_url}",
        channel_url="https://www.youtube.com/@evil",
        channel_id="",
    )
    greeting = next(l for l in out["text"].splitlines() if l.startswith("Hey"))
    assert greeting == "Hey {channel_url},"
    assert "youtube.com/@evil" not in greeting


def test_substitution_is_single_pass_for_every_placeholder():
    for token in ("{channel_name}", "{channel_url}"):
        got = T._substitute(
            "A {channel_name} B {channel_url} C",
            {"channel_name": token, "channel_url": token},
        )
        assert got == f"A {token} B {token} C"


def test_no_placeholder_survives_a_render():
    for niche in T.TEMPLATES:
        out = _render(niche)
        for part in (out["text"], out["html"]):
            assert "{channel_name}" not in part
            assert "{channel_url}" not in part


# --- Legal footer ------------------------------------------------------------

def test_footer_is_appended_to_both_parts():
    out = _render(
        footer_text="Valencia, 123 Example St, City, ST 00000",
        unsubscribe_url="https://example.com/unsubscribe?c=abc",
    )
    assert "123 Example St" in out["text"]
    assert "123 Example St" in out["html"]
    assert "unsubscribe?c=abc" in out["text"]
    assert "unsubscribe?c=abc" in out["html"]


def test_unsubscribe_is_a_working_link_in_the_html_part():
    """
    CAN-SPAM requires a FUNCTIONING opt-out. Escaping the URL into a text node
    ships a gate that forces the footer to be present while the opt-out itself
    does not work — and a client rendering "&amp;" hands a reader who
    copy-pastes it a broken URL.
    """
    out = _render(unsubscribe_url="https://example.com/unsub?c=a&b=1")
    assert 'href="https://example.com/unsub?c=a&amp;b=1"' in out["html"]
    assert ">Unsubscribe</a>" in out["html"]
    # Plaintext keeps the raw URL, which is the clickable form there.
    assert "https://example.com/unsub?c=a&b=1" in out["text"]


@pytest.mark.parametrize(
    "hostile",
    [
        "javascript:alert(1)",
        "data:text/html,<script>x</script>",
        "/relative/path",
        "ftp://x/y",
        # mailto: is accepted as a SCHEME, so the payload must still be an
        # address — otherwise widening it would have smuggled script back in.
        "mailto:javascript:alert(1)",
        "mailto:not-an-address",
    ],
)
def test_hostile_unsubscribe_url_raises_rather_than_being_linked(hostile):
    """This value is config, but config is human-edited and lands in an href."""
    with pytest.raises(T.TemplateError):
        _render(unsubscribe_url=hostile)


@pytest.mark.parametrize(
    "opt_out",
    [
        "mailto:james@valenciatheaterseating.com",
        "mailto:james@valenciatheaterseating.com?subject=Unsubscribe",
        "https://airtable.com/shrABC123",
        "http://example.com/unsub",
    ],
)
def test_both_can_spam_opt_out_mechanisms_are_accepted(opt_out):
    """
    CAN-SPAM accepts an EMAIL-based opt-out as well as a web one. mailto: is the
    one that needs no form, no hosting and nobody else's permission, which is
    decisive when the sending domain belongs to a different organisation than
    whoever is configuring this.
    """
    out = _render(unsubscribe_url=opt_out)
    assert f'href="{opt_out}"' in out["html"] or opt_out.replace("&", "&amp;") in out["html"]
    assert opt_out in out["text"]


def test_footer_is_escaped_and_control_stripped():
    out = _render(footer_text="Evil <b>x</b>\r\nBcc: a@b.c")
    assert "<b>" not in out["html"]
    assert "\r" not in out["text"]


def test_no_footer_renders_cleanly():
    """Absence must not leave a dangling separator; --send gates on it elsewhere."""
    out = _render()
    assert not out["text"].rstrip().endswith("---")


# --- Template version pinning ------------------------------------------------

# Hash of every subject+body, so editing copy without bumping TEMPLATE_VERSION
# fails here. The ledger records which version a creator received; it must not
# be able to lie about it.
EXPECTED_TEMPLATE_HASH = "b6f38f0e488ed911"


def test_template_version_is_pinned_to_the_copy():
    digest = hashlib.sha256()
    for niche in sorted(T.TEMPLATES):
        tpl = T.TEMPLATES[niche]
        for key in ("subject", "text", "html"):
            digest.update(tpl[key].encode("utf-8"))
    actual = digest.hexdigest()[:16]
    assert actual == EXPECTED_TEMPLATE_HASH, (
        f"Template copy changed but TEMPLATE_VERSION is still "
        f"{T.TEMPLATE_VERSION!r}.\n"
        f"Bump TEMPLATE_VERSION, then set EXPECTED_TEMPLATE_HASH = {actual!r}.\n"
        f"The Outreach Log records the version a creator received; leaving it "
        f"stale makes the ledger describe an email nobody was sent."
    )
