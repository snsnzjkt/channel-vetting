"""
Outreach email templates and the render boundary.

The copy is transcribed from the two brand-approved samples (Home Theater and
Lifestyle Sofa). Only two exist, so v1 ships two — see "Template granularity"
in OUTREACH_PLAN.md for why that is a known mismatch with the keyword breadth,
and why the fix is narrowing keywords rather than inventing brand copy here.

Everything dangerous about templating happens in `render()`, deliberately in
one place:

  - **Header injection.** The mailer assembles an RFC-822 message. A channel
    named "Ch\\r\\nBcc: evil@x" reaching a header line would inject headers into
    mail DKIM-signed by the brand's own domain. `Channel Name` is
    attacker-controlled (`main.py` flags it as such), so CR/LF/NUL are stripped
    from EVERY substituted value regardless of which part it lands in — the
    body today, a subject tomorrow.
  - **Untrusted URL in an href.** The pipeline writes `Channel URL` from a
    derived channel id, but `render()` reads whatever is in the Airtable cell
    *now*, and any collaborator can paste `javascript:...` there. Escaping is
    not validation, so the URL is matched against a shape and rebuilt from the
    channel id when it fails.
  - **csv_safe() round trip.** Stored values carry a leading apostrophe when
    they start with = + - @, so a creator named "-Bob's AV" would render as
    "Hey '-Bob's AV,". Undone on the way in.
  - **HTML escaping** on the HTML part only; the plaintext part takes the raw
    (control-stripped) value.

TEMPLATE_VERSION is pinned by a body-hash test. Editing copy without bumping
the version fails that test, which is the point: the ledger records which
version a creator received, and it must not be able to lie.
"""
import html
import re

from text_safety import csv_unsafe

# Bumped deliberately when any subject or body below changes. A test hashes the
# rendered bodies and fails on an unbumped edit.
TEMPLATE_VERSION = "2026-08-14.1"

# Only these may appear as {placeholders}. An unknown one raises rather than
# rendering a half-substituted email — a literal "{channel_nmae}" reaching a
# creator is worse than a loud failure.
ALLOWED_PLACEHOLDERS = frozenset({"channel_name", "channel_url"})

# Control characters that could break out of a header or truncate a body.
_CONTROL_CHARS = re.compile(r"[\r\n\x00]")

# A YouTube channel URL we are willing to put behind a link in brand email.
# Compiled at module level like every other pattern here. re caches compiles,
# but the cache is cleared wholesale on overflow, so "cached" is a property of
# the process rather than a guarantee.
_CHANNEL_ID_RE = re.compile(r"UC[A-Za-z0-9_-]{22}")
_HTTP_URL_RE = re.compile(r"^https?://[^\s<>\"']+$")
# CAN-SPAM accepts an EMAIL-based opt-out as well as a web one, and a mailto:
# needs no form, no hosting and nobody's permission — which matters when the
# sending domain belongs to someone else. Deliberately narrow: an address with
# an optional ?subject=/?body= query, and nothing that could carry script.
_MAILTO_RE = re.compile(r"^mailto:[^\s<>\"'@]+@[^\s<>\"'?]+(?:\?[^\s<>\"']*)?$")

_SAFE_CHANNEL_URL = re.compile(
    r"^https://www\.youtube\.com/(?:channel/UC[A-Za-z0-9_-]{22}|@[A-Za-z0-9._-]+)$"
)

# Used when a creator's display name is missing or is only punctuation, so the
# greeting never renders as "Hey ,".
FALLBACK_GREETING_NAME = "there"


class TemplateError(ValueError):
    """Raised when a template cannot be rendered safely. Never send on this."""


def strip_control_chars(value: str) -> str:
    """Remove CR, LF and NUL from a substituted value. See the module docstring."""
    return _CONTROL_CHARS.sub("", value or "")


def safe_channel_url(url: str, channel_id: str = "") -> str:
    """
    Return a channel URL that is safe to put in an href, or raise.

    Order matters: accept the stored URL if it matches the shape, otherwise
    REBUILD it from the channel id, and only then give up. Rebuilding means a
    hand-mangled cell costs nothing — the channel id is the stable identity and
    the URL is derived from it anyway.
    """
    candidate = strip_control_chars(url).strip()
    if _SAFE_CHANNEL_URL.match(candidate):
        return candidate
    if _CHANNEL_ID_RE.fullmatch(channel_id or ""):
        return f"https://www.youtube.com/channel/{channel_id}"
    raise TemplateError(
        f"unsafe or unrecognised channel URL {url!r} and no usable Channel ID "
        f"({channel_id!r}) to rebuild it from"
    )


def safe_unsubscribe_url(url: str) -> str:
    """
    Return an opt-out link safe to put in an href, or raise.

    Accepts http(s) OR mailto:. Both are valid CAN-SPAM opt-out mechanisms, and
    mailto: is the one that needs no form, no hosting and nobody else's
    permission — which is decisive when the sending domain belongs to a
    different organisation than the person configuring this.

    Everything else is refused, `javascript:` and `data:` above all: a link
    inside mail DKIM-signed by the brand's own domain is the worst place in the
    system to be lax about a scheme. This value comes from config, which a human
    edits, and the send path refuses to start without it — so it is guaranteed
    to be present and therefore guaranteed to be rendered.

    NOTE the operational difference, which this function cannot enforce. A web
    form pointed at the DO NOT CONTACT table suppresses the address by itself. A
    mailto: only produces an email in someone's inbox, and a human still has to
    act on it — an opt-out you receive and ignore is worse than not offering one.
    """
    candidate = strip_control_chars(url).strip()
    if _HTTP_URL_RE.match(candidate) or _MAILTO_RE.match(candidate):
        return candidate
    raise TemplateError(
        f"unsubscribe URL {url!r} is neither an http(s) URL nor a mailto: "
        f"address; refusing to put it in a link"
    )


def greeting_name(channel_name: str) -> str:
    """The name used in the greeting, with a neutral fallback."""
    cleaned = strip_control_chars(channel_name).strip()
    # A name of only punctuation/whitespace would read as an empty greeting.
    return cleaned if any(ch.isalnum() for ch in cleaned) else FALLBACK_GREETING_NAME


# --- The approved copy -------------------------------------------------------
# Subjects match the samples. Bodies are the samples' text/plain and text/html
# parts with the two placeholders substituted where the originals read
# "Channel name" and "Channel name with hyperlink".

_HOME_THEATER_TEXT = """Hey {channel_name},

I hope this message finds you well! I'm James, the social media coordinator at \
Valencia Theater Seating (https://valenciatheaterseating.com/). I came across \
your fantastic Youtube channel ( {channel_url} ) and couldn't help but love \
your content!

At Valencia, we're all about premium, first-rated luxury furniture, especially \
when it comes to our specialty - premium Italian leather seating. With over 22+ \
styles on our website, there's something for everyone.

Our home theater seating has gained quite the reputation as the BEST, and our \
customers, along with influencers like Youthman, Great Scotts, and \
SuperCarBlondie, have raved about us in their Youtube reviews.

We'd love to collaborate with you and sponsor one of your videos by providing \
our exclusive and premium products in exchange for some amazing content.

Looking forward to the possibility of working together!

Cheers,

James
Social Media Coordinator
james@valenciatheaterseating.com
www.valenciatheaterseating.com
"""

_HOME_THEATER_HTML = """<div dir="ltr">
<p>Hey {channel_name},</p>
<p>I hope this message finds you well! I'm James, the social media coordinator at
<a href="https://valenciatheaterseating.com/">Valencia Theater Seating</a>. I came
across your fantastic Youtube channel ( <a href="{channel_url}">{channel_name}</a> )
and couldn't help but love your content!</p>
<p>At Valencia, we're all about premium, first-rated luxury furniture, especially
when it comes to our specialty &ndash; premium Italian leather seating. With over
22+ styles on our website, there's something for everyone.</p>
<p>Our home theater seating has gained quite the reputation as the BEST, and our
customers, along with influencers like
<a href="https://www.youtube.com/watch?v=KTgUdGX6c5Q">Youthman</a>,
<a href="https://www.youtube.com/watch?v=HKPdY4_D-is">Great Scotts</a>, and
<a href="https://www.youtube.com/watch?v=b_nYBE4TY2o">SuperCarBlondie</a>,
have raved about us in their Youtube reviews.</p>
<p>We'd love to collaborate with you and sponsor one of your videos by providing
our exclusive and premium products in exchange for some amazing content.</p>
<p>Looking forward to the possibility of working together!</p>
<p>Cheers,</p>
<p><strong>James</strong><br>Social Media Coordinator<br>
<a href="mailto:james@valenciatheaterseating.com">james@valenciatheaterseating.com</a><br>
<a href="https://valenciatheaterseating.com/">www.valenciatheaterseating.com</a></p>
</div>
"""

_LIFESTYLE_TEXT = """Hi {channel_name},

I am James, the Marketing Coordinator with Valencia Theater Seating, and I hope \
you are having a good week.

I am reaching out today because I came across your YouTube channel \
( {channel_url} ) and I love your content! We here at Valencia would love to \
help you out as a creator and sponsor one of your videos!

Valencia Lifestyle Series \
(https://us.valenciatheaterseating.com/collections/lifestyle) is the leading \
luxury furniture retailer that is known for its premium Italian leather and \
high-quality fabric seatings. Our fine contemporary sofas, sectionals, accent \
chairs and ottomans are carefully and uniquely designed to elevate your space \
into modern luxury living. We have over 22+ styles on our website you can \
browse and take a look at.

Every Valencia piece is thoughtfully crafted by our maestro and every piece is \
a story waiting to be told.

We have been widely shared as the BEST in premium leather and high-quality \
fabric furniture by our customer base, YouTube influencers, and home decor \
reviews such as Elysia English and Our Sweet Sunny Days.

If you would be interested in receiving this exclusive and premium product in \
exchange for content, we would love to work with you and your brand.

Hope to chat soon.

Thank you

Best Regards,

James
Social Media Coordinator
james@valenciatheaterseating.com
www.valenciatheaterseating.com
"""

_LIFESTYLE_HTML = """<div dir="ltr">
<p>Hi {channel_name},</p>
<p>I am James, the Marketing Coordinator with Valencia Theater Seating, and I hope
you are having a good week.</p>
<p>I am reaching out today because I came across your YouTube channel
( <a href="{channel_url}">{channel_name}</a> ) and I love your content! We here at
Valencia would love to help you out as a creator and sponsor one of your videos!</p>
<p><a href="https://us.valenciatheaterseating.com/collections/lifestyle">Valencia
Lifestyle Series</a> is the leading luxury furniture retailer that is known for its
premium Italian leather and high-quality fabric seatings. Our fine contemporary
sofas, sectionals, accent chairs and ottomans are carefully and uniquely designed to
elevate your space into modern luxury living. We have over 22+ styles on our website
you can browse and take a look at.</p>
<p>Every Valencia piece is thoughtfully crafted by our maestro and every piece is a
story waiting to be told.</p>
<p>We have been widely shared as the BEST in premium leather and high-quality fabric
furniture by our customer base, YouTube influencers, and home decor reviews such as
<a href="https://www.youtube.com/watch?v=zuK6OXeFT0U&amp;t=3s">Elysia English</a> and
<a href="https://www.youtube.com/watch?v=Ui8_Bs8HUpI">Our Sweet Sunny Days</a>.</p>
<p>If you would be interested in receiving this exclusive and premium product in
exchange for content, we would love to work with you and your brand.</p>
<p>Hope to chat soon.</p>
<p>Thank you</p>
<p>Best Regards,</p>
<p><strong>James</strong><br>Social Media Coordinator<br>
<a href="mailto:james@valenciatheaterseating.com">james@valenciatheaterseating.com</a><br>
<a href="https://valenciatheaterseating.com/">www.valenciatheaterseating.com</a></p>
</div>
"""

TEMPLATES = {
    "Home Theater": {
        "subject": "Business Inquiry",
        "text": _HOME_THEATER_TEXT,
        "html": _HOME_THEATER_HTML,
    },
    "Lifestyle Sofa": {
        "subject": "Business Inquiry",
        "text": _LIFESTYLE_TEXT,
        "html": _LIFESTYLE_HTML,
    },
}


_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")


def _substitute(template: str, values: dict) -> str:
    """
    Replace {placeholders} from a fixed allowlist, in a SINGLE pass.

    Single-pass is the security property, not a micro-optimisation. Replacing
    one key at a time re-scans text that a previous substitution just inserted,
    so a creator whose display name is the literal string "{channel_url}" gets
    that token expanded — and because the loop iterated a frozenset, whether it
    happened depended on `PYTHONHASHSEED`. Measured: injected on seeds 0/1/2,
    clean on 3/4/5. Python randomises string hashing per process, so the same
    prospect rendered differently between a local dry run and the CI send, and
    a preview a human approved was not necessarily what would go out.
    `re.sub` with a callback consumes each match from the ORIGINAL template, so
    substituted values are never re-scanned.

    Not str.format(): brand copy is edited by non-engineers and a stray `{` in
    an email body would raise KeyError mid-run rather than being left alone.
    """
    unknown = set(_PLACEHOLDER_RE.findall(template)) - ALLOWED_PLACEHOLDERS
    if unknown:
        raise TemplateError(f"unknown placeholder(s) in template: {sorted(unknown)}")
    return _PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], template)


def render(niche: str, *, channel_name: str, channel_url: str, channel_id: str = "",
           footer_text: str = "", unsubscribe_url: str = "") -> dict:
    """
    Render one email. Returns {subject, text, html, template_version}.

    Raises `TemplateError` for an unknown niche, an unsafe channel URL, or an
    unknown placeholder. A caller must never send on a raised template.

    `footer_text` and `unsubscribe_url` are appended to BOTH parts. They are
    required by CAN-SPAM (and PECR in the UK/EU, which the search zones
    deliberately target) and carry no defaults anywhere in the system — the
    send path refuses to start without them, so the legal requirement is a
    startup failure rather than something to remember.
    """
    template = TEMPLATES.get(niche)
    if template is None:
        raise TemplateError(
            f"no template for niche {niche!r}; known: {sorted(TEMPLATES)}"
        )

    name = greeting_name(csv_unsafe(channel_name))
    url = safe_channel_url(csv_unsafe(channel_url), channel_id)

    text_values = {"channel_name": name, "channel_url": url}
    html_values = {
        "channel_name": html.escape(name, quote=True),
        "channel_url": html.escape(url, quote=True),
    }

    text = _substitute(template["text"], text_values)
    html_body = _substitute(template["html"], html_values)

    if footer_text or unsubscribe_url:
        text_bits = []
        html_bits = []
        if footer_text:
            clean = strip_control_chars(footer_text)
            text_bits.append(clean)
            html_bits.append(html.escape(clean, quote=True))
        if unsubscribe_url:
            # The opt-out must be CLICKABLE in the HTML part, not escaped into
            # a text node. CAN-SPAM requires a functioning mechanism, and an
            # inert string is not one — worse, a client rendering the escaped
            # "&amp;" gives a reader who copy-pastes it a broken URL. Validated
            # first for the same reason the channel link is: this value is
            # config, but config is edited by humans and it lands in an href in
            # mail signed by the brand's domain.
            clean = safe_unsubscribe_url(unsubscribe_url)
            text_bits.append(clean)
            html_bits.append(
                f'<a href="{html.escape(clean, quote=True)}">Unsubscribe</a>'
            )
        text += "\n---\n" + "\n".join(text_bits) + "\n"
        html_body += (
            f'<hr><p style="font-size:12px;color:#666">{"<br>".join(html_bits)}</p>\n'
        )

    return {
        # NOT control-stripped: the subject is a literal defined in this file
        # and carries no substituted value. Stripping it here would be a
        # provable no-op that implies otherwise — if a subject ever takes a
        # placeholder, it must go through _substitute() like the bodies do,
        # which is where untrusted text is actually neutralised.
        "subject": template["subject"],
        "text": text,
        "html": html_body,
        "template_version": TEMPLATE_VERSION,
    }
