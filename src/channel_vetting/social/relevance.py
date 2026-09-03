"""
The pet-content requirement, and the artist/seller exclusion.

WHY THIS IS A LOCAL GATE AND NOT A VENDOR FILTER. The obvious server-side
lever, `keywords_not_in_description`, is ACCEPTED by both platforms and has NO
EFFECT — probed 2026-09-03 with and without it, the result total came back
byte-identical (11,102,276 both times on TikTok). An accepted-but-inert filter
is more dangerous than an unsupported one, because nothing tells you it did
nothing. So the exclusion runs here, on captions the 0.03 posts screen has
already paid for.

TWO RULES, and they answer different questions:

  1. PET CONTENT IS REQUIRED. The operator's instruction, 2026-09-03: the
     content must focus on pets. This is POSITIVE evidence required, which is
     the opposite of the YouTube path's `off_target_reason` — that one is
     deliberately negative-evidence-only, because a positive "must match" gate
     was built, measured and REJECTED there in 2026-08 for discarding a real
     prospect. The difference is not carelessness: there, relevance was a
     preference; here, no pet means no product. The draft: "is there a
     recurring subject we could turn into a figurine? No subject, no product,
     however good the account is."

     To avoid repeating the YouTube failure, the bar is a SHARE of posts rather
     than a single match, and the vocabulary is deliberately wide.

  2. SELLERS ARE EXCLUDED. The first real run admitted four pet-portrait
     artists, because the gift-intent hashtags are used by people ADVERTISING
     pet art rather than by owners. An artist is a competitor, not a customer.

Both are judged over the sampled window, per the draft: "a hashtag is not a
niche. One use of #dogsofinstagram doesn't make someone a pet creator. Judge
from the last 20 posts."
"""
import logging
import re

from channel_vetting import config

logger = logging.getLogger(__name__)

REASON_NO_PET_CONTENT = "no_pet_content"
REASON_PET_CONTENT_UNKNOWN = "pet_content_unknown_no_captions"
REASON_SELLS_PET_ART = "sells_pet_art_or_products"

# Wide on purpose — see rule 1. Breed names are included because a breed
# account often never says "dog", and diminutives because pet captions are
# written the way people talk.
PET_TERMS = frozenset({
    "dog", "dogs", "doggo", "doggy", "puppy", "puppies", "pup", "pupper",
    "cat", "cats", "kitty", "kitten", "kittens", "feline", "meow", "purr",
    "pet", "pets", "paw", "paws", "furbaby", "fur baby", "furry friend",
    "rescue", "adopted", "adoption", "shelter", "foster", "vet", "vets",
    "leash", "kennel", "crate", "treats", "tail wag", "zoomies", "snoot",
    "boop", "floof", "woof", "bark", "barking", "whiskers", "litter box",
    "corgi", "dachshund", "frenchie", "french bulldog", "husky", "huskies",
    "golden retriever", "goldie", "labrador", "lab", "poodle", "shiba",
    "shih tzu", "chihuahua", "pug", "beagle", "border collie", "aussie",
    "ragdoll", "bengal", "siamese", "maine coon", "tabby", "calico",
    "orange cat", "black cat", "tuxedo cat",
    "dogmom", "dogdad", "catmom", "catdad", "petparent", "dogsoftiktok",
    "catsoftiktok", "dogsofinstagram", "catsofinstagram", "petsofinstagram",
    "adoptdontshop", "rescuedog", "rescuecat", "fosterfail",
})

# Signals that the account SELLS pet likenesses rather than owning a pet.
SELLER_TERMS = frozenset({
    "commission", "commissions", "commissioned", "custom order", "custom orders",
    "etsy", "shop now", "my shop", "order yours", "order now", "dm to order",
    "link in bio to order", "available now", "now taking orders", "slots open",
    "print", "prints", "poster", "sticker", "stickers",
    "portrait artist", "pet portrait", "petportrait", "custompetportrait",
    "custompetart", "handmade", "small business", "smallbusiness",
    "my art", "my painting", "artwork", "illustration", "illustrator",
    "acrylic", "watercolor", "watercolour", "oil painting", "sketch",
    "crochet", "clay", "sculpt", "resin",
})

_WORD = re.compile(r"[a-z][a-z' ]*")


def _normalise(caption: str) -> str:
    """Lowercased, with hashtag punctuation flattened so '#dogmom' matches."""
    return re.sub(r"[#_\-]+", "", (caption or "").lower())


def _share_mentioning(captions, terms) -> float:
    """Fraction of captions containing at least one of `terms`."""
    usable = [c for c in captions if isinstance(c, str) and c.strip()]
    if not usable:
        return 0.0
    hits = 0
    for caption in usable:
        text = _normalise(caption)
        if any(term.replace(" ", "") in text.replace(" ", "") for term in terms):
            hits += 1
    return hits / len(usable)


def pet_content_reason(metrics, *, name: str = "") -> str | None:
    """
    Why this creator fails the pet-content requirement, or None if they pass.

    `metrics` is a PostMetrics. Judged on captions, which the posts screen has
    already bought, so this gate adds no credits.
    """
    captions = tuple(getattr(metrics, "captions", ()) or ())
    haystack = captions + ((name,) if name else ())

    # A creator whose posts carry NO caption text cannot be judged. Rejected
    # rather than admitted, because pet content is a requirement and admitting
    # an unconfirmed one spends a reviewer's attention on a coin flip. Given its
    # own reason so its frequency is visible — if this turns out to be common,
    # loosen it deliberately rather than by accident.
    if not any(isinstance(c, str) and c.strip() for c in captions):
        return REASON_PET_CONTENT_UNKNOWN

    pet_share = _share_mentioning(captions, PET_TERMS)
    if pet_share < config.SOCIAL_MIN_PET_CAPTION_SHARE:
        return REASON_NO_PET_CONTENT

    # Only applied to accounts that ALREADY look like pet accounts, so a pet
    # owner who once mentioned a sticker is not thrown out: the test is whether
    # selling is a THEME, not whether the word appears.
    seller_share = _share_mentioning(haystack, SELLER_TERMS)
    if seller_share >= config.SOCIAL_MAX_SELLER_CAPTION_SHARE:
        return REASON_SELLS_PET_ART

    return None
