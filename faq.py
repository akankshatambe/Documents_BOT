"""
Instant answers to the questions drivers ask most often, so the bot can help
right away instead of making them wait on the customs team.

How it works: FAQ_ENTRIES is checked top to bottom, and the first entry whose
"keywords" phrase appears anywhere in the driver's message wins. No AI, no
external service - just a plain text match, so it's fast, free, and works
even if something else on the internet is down.

To edit the content: change the text in "answer", or add/remove keyword
phrases - no other code needs to change. Put more specific entries above more
general ones (e.g. "red pbn" above the generic "what is pbn") so a specific
question doesn't get caught by a broader one first.
"""

FAQ_ENTRIES = [
    {
        "keywords": ["red pbn", "red channel", "pbn red", "pbn is red"],
        "answer": (
            "*Red PBN* means Revenue has flagged your load for a physical customs "
            "check at the port. Head to the customs control area on arrival - don't "
            "board without going through it. Keep your paperwork and phone handy."
        ),
    },
    {
        "keywords": ["orange pbn", "orange channel", "pbn orange", "pbn is orange"],
        "answer": (
            "*Orange PBN* means a documentary check only - no physical inspection. "
            "Have your paperwork (CMR, invoice, transit docs) ready to show at the "
            "customs desk before you board."
        ),
    },
    {
        "keywords": ["green pbn", "green channel", "pbn green", "pbn is green"],
        "answer": "*Green PBN* means you're cleared - no customs check needed, go straight to boarding.",
    },
    {
        "keywords": ["what is pbn", "what's pbn", "whats pbn", "what does pbn mean", "pbn mean"],
        "answer": (
            "*PBN* (Pre-Boarding Notification) is the customs clearance status Irish "
            "Revenue issues for your load before you board the ferry - Green (cleared), "
            "Orange (paperwork check) or Red (physical check). Tap *Check clearance "
            "(PBN)* from the menu and enter your PBN ID to look yours up."
        ),
    },
    {
        "keywords": ["gmr", "gvms"],
        "answer": (
            "*GMR* (Goods Movement Reference) is generated in *GVMS* and links your "
            "load's customs declarations together for UK border checks - you'll need "
            "it at UK ports like Dover. Your office issues it once your declarations "
            "are lodged."
        ),
    },
    {
        "keywords": ["ncts", "transit closure", "transit is open", "transit open", "what is transit", "t1 "],
        "answer": (
            "*NCTS/transit* lets goods move under customs control between countries "
            "without paying duty at every border - it has to be opened before you "
            "travel and closed (discharged) at destination. It's separate from your "
            "GMR/PBN status, so check both if you're not sure."
        ),
    },
    {
        "keywords": ["mrn"],
        "answer": (
            "*MRN* (Movement/Master Reference Number) is the unique reference on your "
            "customs declaration - usually on the paperwork the office gives you, or "
            "on the transit accompanying document."
        ),
    },
    {
        "keywords": ["cmr"],
        "answer": "*CMR* is the international consignment note for the load - proof of what's on board and delivery terms. Keep the signed copy with you.",
    },
    {
        "keywords": ["traces", "dafm", "phytosanitary", "health cert", "plant health"],
        "answer": (
            "Food, plant or animal loads may need a *phytosanitary/health certificate* "
            "logged in TRACES (DAFM checks) as well as customs clearance. If you've got "
            "that kind of load and aren't sure your paperwork is in order, tap *Message "
            "the team* and send a photo of what you have."
        ),
    },
    {
        "keywords": ["held", "detained", "stopped at customs", "being inspected"],
        "answer": (
            "If you've been stopped for inspection, that's normal for Red PBN / "
            "documentary-check loads - stay put and keep your paperwork to hand. Tap "
            "*Message the team* and send your trailer number and location so the "
            "team can follow up."
        ),
    },
    {
        "keywords": ["how long will", "how long does", "waiting time", "how much longer"],
        "answer": (
            "Clearance time depends on port traffic and check type - Green is "
            "immediate, Orange/Red depend on the queue. If you've been waiting a "
            "long time, tap *Message the team* with your trailer number and we'll "
            "chase it up for you."
        ),
    },
]


def match_faq(text):
    """Return the answer text for the first matching FAQ entry, or None."""
    if not text:
        return None
    t = text.lower()
    for entry in FAQ_ENTRIES:
        if any(kw in t for kw in entry["keywords"]):
            return entry["answer"]
    return None
