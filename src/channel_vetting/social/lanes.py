"""
The sourcing lanes from the criteria draft, verbatim where possible.

"Pet is the primary lane, with people and TRPG secondary." The draft is
emphatic about ordering: "Search the gift-intent tags first. They surface
creators whose audiences already buy custom likenesses, which is the shortest
path to a sale." And on TRPG: "high intent, but crowded. Every figurine company
is already in those inboxes. Pet accounts are far less picked-over."

So PET_GIFT_INTENT is searched before PET_BROAD, and TRPG comes last.

The hashtag lists are kept even though the vendor's discovery endpoint takes a
natural-language brief rather than tags, for two reasons: the `nlp_search`
briefs below are built from the same vocabulary, and the tags are what a human
uses for the hand-vetting pass the draft asks for before any of this is trusted
("hand-vet 20-30 pet creators against this -> then automate").

One warning from the draft that no filter can enforce, so it belongs here where
whoever tunes the lanes will read it: "a hashtag is not a niche. One use of
#dogsofinstagram doesn't make someone a pet creator. Judge from the last 20
posts."
"""

PET_GIFT_INTENT = (
    "#custompetportrait", "#petportrait", "#petlovergift",
    "#giftsforpetlovers", "#petgifts", "#petkeepsake", "#custompetart",
)
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
PET_PHOTOGRAPHY = ("#petphotography", "#dogportrait", "#petsofig")

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

# Bio and search keywords the draft lists, for the hand-vetting pass and for
# any future keyword-based source.
PET_KEYWORDS = (
    "dog mom", "cat dad", "pet parent", "pet photographer", "rescue mom",
)
PEOPLE_KEYWORDS = ("gift guide", "custom gift", "cosplayer", "OC artist", "new mom")
TRPG_KEYWORDS = ("dungeon master", "DM", "D&D character", "mini painter", "actual play")


# `nlp_search` briefs. Optional per the vendor ("filters work on their own"),
# and they only fill in fields we did not set, so a poor brief costs relevance
# rather than correctness. Written to describe the SUBJECT requirement the draft
# puts underneath everything: "is there a recurring subject we could turn into a
# figurine? No subject, no product, however good the account is."
LANES = (
    {
        "key": "pet_gift_intent",
        "priority": 1,
        "tags": PET_GIFT_INTENT,
        "nlp_search": (
            "pet owners in the United States and Canada who commission custom "
            "portraits, paintings or personalised keepsakes of their own named "
            "dog or cat, and post photos of that specific pet"
        ),
    },
    {
        "key": "pet_single_identity",
        "priority": 2,
        "tags": PET_BROAD,
        "nlp_search": (
            "accounts in the United States and Canada that are about one named "
            "dog or cat, where the pet is the identity of the account and "
            "appears in clear front-facing photos"
        ),
    },
    {
        "key": "pet_breed",
        "priority": 3,
        "tags": PET_BREED,
        "nlp_search": (
            "breed-community dog and cat accounts in the United States and "
            "Canada, such as corgi, dachshund, french bulldog, husky or orange "
            "cat, with a recognisable single pet"
        ),
    },
    {
        "key": "pet_photography",
        "priority": 4,
        "tags": PET_PHOTOGRAPHY,
        "nlp_search": (
            "pet photographers in the United States and Canada who shoot "
            "well-lit portrait photographs of dogs and cats"
        ),
    },
    {
        "key": "pet_rescue",
        "priority": 5,
        "tags": PET_RESCUE,
        "nlp_search": (
            "rescue and adoption storytellers in the United States and Canada "
            "with one consistent adopted pet, not rotating foster animals"
        ),
    },
    {
        "key": "people",
        "priority": 6,
        "tags": PEOPLE_TAGS,
        "nlp_search": (
            "couples, families and new parents in the United States and Canada "
            "posting milestone and gifting content, plus cosplayers and artists "
            "with their own original characters"
        ),
    },
    {
        "key": "trpg",
        "priority": 7,
        "tags": TRPG_TAGS,
        "nlp_search": (
            "Dungeons and Dragons players, dungeon masters and miniature "
            "painters in the United States and Canada"
        ),
    },
)


def lanes_in_order():
    """Lanes cheapest-intent-first, as the draft prescribes."""
    return tuple(sorted(LANES, key=lambda lane: lane["priority"]))
