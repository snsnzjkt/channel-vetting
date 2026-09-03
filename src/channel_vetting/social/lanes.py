"""
The sourcing lanes: OWNERS of pets, not the people who sell pet art.

REWRITTEN 2026-09-03 after the first real run. Every one of the four TikTok
rows it produced came from the old `pet_gift_intent` lane and every one was a
pet-portrait ARTIST — pawtraitsbynatalia, jamila.draws, tinafigartist,
caros.art1. That is the supply side of custom pet art, i.e. near-competitors,
not owners who would buy a figurine of their own dog.

The cause was the draft's own warning applied to itself: "a hashtag is not a
niche." The gift-intent tags it recommends starting from (#custompetportrait,
#custompetart) are used overwhelmingly by artists ADVERTISING, because a tag
describes what a post is about, not who the poster is.

MEASURED, and this is also why the run looked supply-constrained (limit=1
probes, 0.01 credits each, against the Valencia location zone):

    ai_search query                          TikTok pool   Instagram pool
    OLD "owners who commission portraits"            985            4,872
    NEW "account about one named pet"              9,742           17,011
    NEW "dog mom or dog dad"                      23,413           14,463
    NEW "owner of a corgi/dachshund/..."          26,033           10,373
    NEW "cat parent"                              24,206            4,417
    NEW "adopted a rescue dog or cat"             26,855           81,057

The old lane's ENTIRE addressable pool was 985 creators. Screening 50 of them
and finding 44 below a 3,000-view median is not a strict gate, it is the bottom
of a small barrel. The rewrite is 10-27x more pool AND aimed at the right side
of the market.

RELEVANCE RIDES ON `ai_search`, which is a documented FILTER field and measured
highly selective (11.1M -> 39k on its own). Not on `keywords_not_in_description`:
that field is ACCEPTED on both platforms and has NO EFFECT — probed 2026-09-03,
the result total was byte-identical with and without it. An accepted-but-inert
filter is worse than an unsupported one, so artist exclusion happens LOCALLY in
social/relevance.py instead.
"""

# Hashtag lists, kept for the hand-vetting pass the draft asks for and as the
# vocabulary the ai_search queries are built from. NOT sent to the vendor:
# there is no hashtag filter, and these tags are what surfaced artists.
PET_BROAD = (
    "#dogsoftiktok", "#catsoftiktok", "#dogsofinstagram", "#catsofinstagram",
    "#petsofinstagram", "#petstagram", "#dogmom", "#dogdad", "#catmom",
    "#petparent",
)
PET_BREED = (
    "#corgisofinstagram", "#dachshundlove", "#frenchbulldogsofinstagram",
    "#goldenretrieversofinstagram", "#huskiesofinstagram", "#shibainu",
    "#ragdollcats", "#bengalcat", "#orangecatbehavior",
)
PET_RESCUE = ("#adoptdontshop", "#rescuedog", "#rescuecat", "#fosterfail")
# RETAINED FOR REFERENCE ONLY — these are the tags that surfaced artists.
PET_GIFT_INTENT_ARTIST_HEAVY = (
    "#custompetportrait", "#petportrait", "#petlovergift",
    "#giftsforpetlovers", "#petgifts", "#petkeepsake", "#custompetart",
)
PEOPLE_TAGS = (
    "#couplegoals", "#engaged", "#anniversarygift", "#newborn",
    "#firstbirthday", "#familyportrait", "#cosplay", "#cosplayer",
    "#originalcharacter", "#giftideas", "#personalizedgift", "#customgift",
)
TRPG_TAGS = (
    "#dnd", "#dnd5e", "#ttrpg", "#dungeonsanddragons", "#dndminis",
    "#dndcharacter", "#miniaturepainting", "#paintingminis", "#warhammer",
    "#dmtips",
)

PET_KEYWORDS = ("dog mom", "cat dad", "pet parent", "rescue mom")
PEOPLE_KEYWORDS = ("gift guide", "custom gift", "cosplayer", "OC artist", "new mom")
TRPG_KEYWORDS = ("dungeon master", "DM", "D&D character", "mini painter", "actual play")


# `ai_search` is 3-150 chars of free text. Each query below describes WHO THE
# POSTER IS, not what the post is about — that distinction is the whole fix.
# Every one says "their own" pet somewhere, and none names a product.
#
# `pet_required` marks a lane whose creators must pass the pet-content gate.
# `enabled` defaults the people and TRPG lanes OFF: the operator's instruction
# is that pet content is a MUST, and those two lanes exist to find creators who
# have none — so running them would spend 0.04 a head on creators the gate is
# guaranteed to reject. Flip them on only if the pet requirement is relaxed.
LANES = (
    {
        "key": "pet_single_identity",
        "priority": 1,
        "enabled": True,
        "pet_required": True,
        "tags": PET_BROAD,
        "ai_search": (
            "an account about one named pet dog or cat, posting photos and "
            "videos of their own pet, run by the owner"
        ),
    },
    {
        "key": "pet_breed",
        "priority": 2,
        "enabled": True,
        "pet_required": True,
        "tags": PET_BREED,
        "ai_search": (
            "an owner of a corgi, dachshund, french bulldog, husky or golden "
            "retriever posting their own dog"
        ),
    },
    {
        "key": "pet_dog_parent",
        "priority": 3,
        "enabled": True,
        "pet_required": True,
        "tags": PET_BROAD,
        "ai_search": (
            "a dog mom or dog dad posting daily life with their own dog, "
            "not a business"
        ),
    },
    {
        "key": "pet_cat_parent",
        "priority": 4,
        "enabled": True,
        "pet_required": True,
        "tags": PET_BROAD,
        "ai_search": (
            "a cat parent posting photos and videos of their own cat at home"
        ),
    },
    {
        "key": "pet_rescue",
        "priority": 5,
        "enabled": True,
        "pet_required": True,
        "tags": PET_RESCUE,
        "ai_search": (
            "someone who adopted a rescue dog or cat and posts that pet's story"
        ),
    },
    # --- secondary verticals, OFF by default; see the note above ---
    {
        "key": "people",
        "priority": 6,
        "enabled": False,
        "pet_required": False,
        "tags": PEOPLE_TAGS,
        "ai_search": (
            "a couple, family or new parent posting milestone and gifting "
            "content, or a cosplayer with their own original character"
        ),
    },
    {
        "key": "trpg",
        "priority": 7,
        "enabled": False,
        "pet_required": False,
        "tags": TRPG_TAGS,
        "ai_search": (
            "a Dungeons and Dragons player, dungeon master or miniature "
            "painter posting their own characters"
        ),
    },
)


def lanes_in_order(include_disabled: bool = False):
    """Enabled lanes, highest-intent first. Disabled ones only on request."""
    lanes = LANES if include_disabled else tuple(l for l in LANES if l.get("enabled"))
    return tuple(sorted(lanes, key=lambda lane: lane["priority"]))
