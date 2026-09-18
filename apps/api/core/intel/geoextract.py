# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Extract geographic coordinates and classify severity from free-text.

Strategy (in order):
  1. Explicit decimal coords  ("35.5N 12.3E", "35.5, 12.3")
  2. Degrees-minutes          ("35°30'N 12°15'E")
  3. Known Mediterranean place names (longest-match first)
  4. None — caller decides whether to discard or use a default zone
"""
from __future__ import annotations
import math
import re
from typing import Optional

from core.intel.landmask import in_operational_region, nearest_sea_point

# ── Mediterranean gazetteer ───────────────────────────────────────────────────
# (lat, lon) centroids of places frequently mentioned in SAR/migration reports.
# Sorted by specificity (more-specific entries shadow generic ones in matching).
_PLACES: dict[str, tuple[float, float]] = {
    # Departure points (Libya/Tunisia)
    "zuwara":              (32.92, 12.08),
    "zuwarah":             (32.92, 12.08),
    "sabrata":             (32.79, 12.48),
    "misrata":             (32.37, 15.09),
    "khums":               (32.64, 14.26),
    "tripoli":             (32.90, 13.18),
    "benghazi":            (32.12, 20.07),
    "derna":               (32.75, 22.65),
    # Sfax's own coordinates are the city itself, on land. A report reading
    # "boat in distress close to #Sfax" (the actual real-world case this was
    # found from) is always offshore — nudged into the Gulf of Gabès, toward
    # the Kerkennah Islands, same fix already applied to Crete for the same
    # on-landmass-centroid reason.
    "sfax":                (34.70, 10.95),
    "sousse":              (35.83, 10.64),
    "kairouan":            (35.68, 10.10),
    "zarzis":              (33.50, 11.11),
    "gabes":               (33.88, 10.10),
    "jerba":               (33.80, 10.85),
    "medenine":            (33.35, 10.50),
    "ben gardane":         (33.14, 11.22),
    "thyna":               (34.71, 10.72),
    # Italian arrival points
    "lampedusa":           (35.50, 12.60),
    "linosa":              (35.87, 12.86),
    "pantelleria":         (36.83, 11.96),
    "porto empedocle":     (37.29, 13.53),
    "agrigento":           (37.31, 13.58),
    "pozzallo":            (36.73, 14.85),
    "augusta":             (37.23, 15.22),
    "catania":             (37.50, 15.09),
    "palermo":             (38.12, 13.35),
    "trapani":             (38.02, 12.51),
    "mazara del vallo":    (37.66, 12.59),
    "marsala":             (37.80, 12.44),
    # Small but very frequently-labelled Central Med SAR waypoints — the
    # busiest Alarm Phone corridor, so these double as pixel-calibration
    # anchors for map_pin_geolocate.py the same way the Crete towns do.
    "portopalo":           (36.68, 15.13),
    "capo passero":        (36.68, 15.14),
    "lampione":            (35.53, 12.32),
    "kerkennah":           (34.65, 11.20),
    "houmt souk":          (33.87, 10.86),
    "zawiya":              (32.75, 12.73),
    "al zawiya":           (32.75, 12.73),
    "messina":             (38.19, 15.56),
    "reggio calabria":     (38.11, 15.65),
    "calabria":            (38.11, 16.55),
    "croton":              (39.08, 17.13),
    "crotone":             (39.08, 17.13),
    "taranto":             (40.46, 17.25),
    "bari":                (41.12, 16.87),
    "brindisi":            (40.63, 17.95),
    "lecce":               (40.35, 18.17),
    "ancona":              (43.61, 13.50),
    # Malta
    "malta":               (35.90, 14.51),
    "valletta":            (35.90, 14.51),
    "sliema":              (35.91, 14.50),
    # Greece (Aegean route)
    "lesvos":              (39.10, 26.55),
    "mytilene":            (39.10, 26.55),
    "chios":               (38.37, 26.14),
    "samos":               (37.75, 26.97),
    "kos":                 (36.89, 27.29),
    "rhodes":              (36.43, 28.22),
    "dodecanese":          (36.50, 28.00),
    "piraeus":             (37.94, 23.65),
    "athens":              (37.98, 23.73),
    "kalymnos":            (36.9959, 26.9950),
    # Crete's own geometric centroid (35.24, 24.81) lands ON the island's
    # landmass — every real report using this fallback is a boat "south of
    # Crete" (the actual SAR route), so the centroid is placed just off the
    # island's south coast instead, clearly in open water rather than on land.
    "crete":               (34.85, 24.81),
    "kriti":               (34.85, 24.81),
    "gavdos":              (34.84, 24.08),
    # Crete coastal towns — real (precise) coordinates, not zone centroids.
    # Alarm Phone's map screenshots for "south of Crete" reports consistently
    # label these on the basemap even when no coordinate text is printed, so
    # they double as calibration anchors for map_pin_geolocate.py.
    "heraklion":           (35.34, 25.13),
    "iraklio":             (35.34, 25.13),
    "rethymno":            (35.37, 24.47),
    "rethimno":            (35.37, 24.47),
    "chania":              (35.52, 24.02),
    "ierapetra":           (35.01, 25.74),
    "agios nikolaos":      (35.19, 25.72),
    "sitia":               (35.20, 26.10),
    "palekastro":          (35.20, 26.25),
    "moires":              (35.05, 24.87),
    "spinalonga":          (35.30, 25.75),
    "chrisi":              (34.90, 25.72),
    # Turkey (departure Aegean)
    "izmir":               (38.42, 27.14),
    "cesme":               (38.33, 26.30),
    "bodrum":              (37.03, 27.43),
    "marmaris":            (36.85, 28.26),
    "canakkale":           (40.15, 26.41),
    "dikili":              (39.07, 26.89),
    "kusadasi":            (37.86, 27.26),
    "datca":               (36.73, 27.68),
    "didim":               (37.38, 27.26),
    "foca":                (38.67, 26.76),
    "ayvalik":             (39.32, 26.69),
    # Sea zones
    "strait of sicily":    (37.00, 11.50),
    "sicilian channel":    (37.00, 11.50),
    "sicily channel":      (37.00, 11.50),
    "canal de sicile":     (37.00, 11.50),
    "canal de sicilia":    (37.00, 11.50),
    "central mediterranean": (35.00, 15.00),
    "central med":         (35.00, 15.00),
    "sicily":              (37.60, 14.01),
    "sardinia":            (40.10, 9.10),
    "aegean sea":          (37.50, 25.00),
    "aegean":              (37.50, 25.00),
    "ionian sea":          (38.00, 20.00),
    "ionian":              (38.00, 20.00),
    "tyrrhenian":          (40.00, 12.00),
    "adriatic":            (42.00, 15.00),
    "libyan waters":       (32.00, 15.00),
    "libyan coast":        (32.00, 15.00),
    "tunisian waters":     (37.00, 10.00),
    "tunisian coast":      (37.00, 10.00),
    # A country's geographic centroid is not always its coastline -- Libya's
    # true centroid sits deep in the Sahara, ~150km+ from the Mediterranean,
    # useless as a seed point for a maritime search area (verified: a 120km
    # search radius from the old centroid never reaches open water at all).
    # This is the central Libyan coast (near Sirte) instead.
    "libya":               (32.00, 18.00),
    "tunisia":             (33.89, 9.53),
    "ceuta":               (35.89, -5.32),
    "chafarinas islands":  (35.18, -2.43),
    "chafarinas":          (35.18, -2.43),
    "mediterranean":       (35.00, 18.00),
    "med sea":             (35.00, 18.00),
    # Western Mediterranean (Algeria/Morocco → Spain route)
    "oran":                (35.70, -0.63),
    "bordj el bahri":      (36.7906, 3.2497),
    "bordjelbahri":        (36.7906, 3.2497),
    "ain taya":            (36.7913, 3.2926),
    "aïn taya":            (36.7913, 3.2926),
    "aintaya":             (36.7913, 3.2926),
    "tipaza":              (36.5897, 2.4489),
    "tipasa":              (36.5897, 2.4489),
    "cherchell":           (36.6050, 2.1908),
    "cherchel":            (36.6050, 2.1908),
    "ibiza":               (38.91, 1.43),
    "balearic islands":    (39.50, 2.80),
    "almeria":             (36.83, -2.46),
    "cartagena":           (37.61, -0.99),
    "melilla":             (35.29, -2.94),
    # Red Sea / Horn (wider coverage for non-Med SAR)
    "suez":                (30.00, 32.54),
    "suez canal":          (30.70, 32.34),
    "red sea":             (20.00, 38.00),
    "gulf of aden":        (12.00, 47.00),
    "horn of africa":      (11.00, 51.00),
    # Same fix as Libya above: the country's geographic centroid is well
    # inland, ~200km+ from any coast (verified: zero sea points within
    # 120km). This is the Indian Ocean coast near Mogadishu instead.
    "somalia":             (2.05, 45.35),
    "djibouti":            (11.82, 42.59),
    "aden":                (12.78, 45.03),
}

# _PLACES entries that name a whole sea basin, strait, island group or country
# rather than a specific point. These need to lose to any specific place named
# in the same text — "close to #Sfax, #Tunisia" must resolve to Sfax, not to
# Tunisia's country centroid (which sits inland, nowhere near the coast) just
# because "tunisia" is a longer string than "sfax". They're also useless (or
# actively misleading) as a pixel-calibration anchor for map_pin_geolocate.py,
# since a country/sea centroid can sit hundreds of km from where the name is
# actually printed on a map.
_IMPRECISE_PLACE_NAMES = frozenset({
    "strait of sicily", "sicilian channel", "sicily channel",
    "canal de sicile", "canal de sicilia", "central mediterranean",
    "central med", "sicily", "sardinia", "aegean sea", "aegean",
    "ionian sea", "ionian", "tyrrhenian", "adriatic", "libyan waters",
    "libyan coast", "tunisian waters", "tunisian coast", "libya", "tunisia",
    "mediterranean", "med sea", "balearic islands", "red sea",
    "gulf of aden", "horn of africa", "somalia", "dodecanese", "calabria",
})

# Precise-point subset usable as pixel-calibration anchors (see above) and as
# the first-priority tier for text matching.
PRECISE_PLACES: dict[str, tuple[float, float]] = {
    name: coords for name, coords in _PLACES.items() if name not in _IMPRECISE_PLACE_NAMES
}

# Sorted longest-first for greedy matching within each tier, but every precise
# place is tried before any imprecise/broad one regardless of string length —
# see _IMPRECISE_PLACE_NAMES above for why plain length-only sorting is wrong.
_PLACES_SORTED = sorted(PRECISE_PLACES.items(), key=lambda x: -len(x[0])) + sorted(
    ((name, coords) for name, coords in _PLACES.items() if name in _IMPRECISE_PLACE_NAMES),
    key=lambda x: -len(x[0]),
)

# Country/sea-basin fallback — only tried when no specific place/coordinate
# matches. These names span hundreds of km, so each carries its own much
# wider radius_m; callers must render this as an area, never a precise pin.
_REGIONS: dict[str, tuple[float, float, float]] = {
    "algeria":                    (36.75, 3.50, 220_000),
    "algérie":                    (36.75, 3.50, 220_000),
    "algerie":                    (36.75, 3.50, 220_000),
    "western mediterranean":      (37.50, 1.50, 180_000),
    "westernmed":                 (37.50, 1.50, 180_000),
    "méditerranée occidentale":   (37.50, 1.50, 180_000),
    "méditerranéeoccidentale":    (37.50, 1.50, 180_000),
    "mediterranee occidentale":   (37.50, 1.50, 180_000),
    "alboran sea":                (35.90, -3.00, 100_000),
    "morocco":                    (33.50, -6.50, 220_000),
    "maroc":                      (33.50, -6.50, 220_000),
}
_REGIONS_SORTED = sorted(_REGIONS.items(), key=lambda x: -len(x[0]))


def extract_region_coords(text: str) -> Optional[tuple[tuple[float, float], float]]:
    """Broad country/sea-basin fallback for when no specific place or explicit
    coordinate can be found. Returns ((lat, lon), radius_m) — the caller must
    surface this as an area indicator, not a precise-looking pin."""
    tl = text.lower()
    for region, (lat, lon, radius_m) in _REGIONS_SORTED:
        if region in tl:
            return (lat, lon), radius_m
    return None


# ── Distress & emergency keyword sets ────────────────────────────────────────
DISTRESS_KW = frozenset([
    # English
    # "sos" deliberately excluded here -- it is also the first word of
    # several NGO names (SOS Mediterranee, SOS Humanity, ...), so a bare
    # substring match here promoted an org's own press release / RSS
    # footer to a distress incident (docs/ALERT_RECOGNITION_BASELINE.md).
    # _SOS_MARKER_RE below already excludes those known forms with a
    # word-boundary + negative-lookahead; is_distress() reuses it directly.
    "mayday", "distress", "sinking", "capsized", "capsize",
    "overloaded", "taking water", "engine failure", "boat missing",
    # "rescue operation" deliberately excluded -- too weak/ambiguous alone
    # (matched abstract policy language like "funding for search and
    # rescue operations"); is_direct_distress_call() never relied on it
    # either. Genuine rescue-in-progress reports pair with a stronger
    # keyword (taking water, engine failure, an explicit SOS marker, ...).
    "people drowning", "drowning", "shipwreck",
    "overcrowded boat", "rubber boat sinking", "missing migrants",
    # Italian
    "naufragio", "affondamento", "dispersi", "soccorso in mare",
    "barcone", "barconi", "imbarcazione in difficoltà", "affogati",
    # French
    "naufrage", "détresse maritime", "embarcation en difficulté",
    # Arabic (transliterated common forms in Latin-script tweets)
    "gharaq", "gharak", "markab yughraq",
    # Generic SAR
    "man overboard", "mob alert", "epirb", "121.5", "406 mhz",
    "pyrotechnic", "flare sighted", "life raft", "lifeboat",
])

RESCUE_KW = frozenset([
    "rescue", "rescued", "rescuing", "soccorso", "salvato", "salvati",
    "recovered", "safely aboard", "brought on board", "onboard safe",
    "picked up", "intercepted", "towed to safety",
])

INFO_KW = frozenset([
    "spotted", "detected", "reported", "vessel sighted", "boat sighted",
    "monitoring", "in contact", "following", "tracking",
])

_CRITICAL_TERMS = frozenset([
    "mayday", "sinking", "capsized", "drowning", "dispersi",
    "غرق", "naufragio", "man overboard", "life raft",
])
_HIGH_TERMS = frozenset([
    "distress", "sos", "rescue operation", "soccorso", "naufrage",
    "overloaded", "engine failure", "missing migrants",
])
_MEDIUM_TERMS = frozenset([
    "spotted", "rubber boat", "zodiac", "dinghy", "barcone",
    "contact lost", "missing", "irregolari", "migranti",
])

# ``SOS`` is also the first word in several NGO names. Treating a footer such
# as "appeared first on SOS MEDITERRANEE" as an emergency promoted every
# article from that RSS feed to a distress incident. Keep genuine SOS text,
# but exclude the well-known organisation-name forms.
_SOS_MARKER_RE = re.compile(
    r"(?:🆘|🆘️|\bmayday\b|\bsos\b"
    r"(?!\s+(?:m[eé]diterran[eé]e|mediterranee|humanity|balkanroute)\b))",
    re.I,
)

# A report clearly framed as commemorating/marking a past event -- an
# anniversary, a memorial, a vigil -- is not a new incident report, no
# matter which distress keywords it happens to repeat while doing so (e.g.
# "Last year's shipwreck anniversary was marked with a vigil in
# Lampedusa"). Shared by is_distress() and is_direct_distress_call(), both
# of which independently false-positived on this shape
# (docs/ALERT_RECOGNITION_BASELINE.md).
_RETROSPECTIVE_COMMEMORATION_RE = re.compile(
    r"\b(?:anniversary|memorial|vigil|commemorat\w*)\b", re.I
)

_DIRECT_DISTRESS_PATTERNS = tuple(
    pattern if isinstance(pattern, re.Pattern) else re.compile(pattern, re.I)
    for pattern in (
        _SOS_MARKER_RE,
        r"\b(?:people|persons|lives|passengers|migrants)\s+(?:are\s+)?(?:in distress|at risk|in danger)\b",
        r"\b(?:boat|vessel|dinghy|rubber boat)\b.{0,45}\b(?:in distress|taking water|sinking|capsized)\b",
        r"\b(?:taking water|people drowning|drifting at sea|no fuel left|engine (?:does not|doesn't|stopped|failed|is not) work)\b",
        r"\b(?:rescue|medical assistance)\s+(?:is|are)\s+(?:urgently\s+|immediately\s+)?(?:needed|required)\b",
        r"\b(?:ask|asked|asking|call|called|calling)\s+for\s+(?:an?\s+)?(?:urgent|immediate)\s+(?:search and rescue|rescue|medical assistance|assistance)\b",
        r"\b(?:critical situation|critical condition)\b.{0,100}\b(?:authorities|coast ?guard|rescue|assistance)\b",
        # A first report of a shipwreck is itself an active distress call, even
        # when it doesn't repeat "in distress"/"sinking" — Alarm Phone posts
        # these as standalone incident openers (e.g. "Shipwreck in the
        # WesternMed. We were alerted by relatives…" / "Naufrage en
        # Méditerranée... Des proches ont signalé..."). Already-resolved
        # reports are excluded above via _RESOLVED_DISTRESS_PATTERNS first.
        r"\b(?:shipwreck|naufrage|naufragio)\b",
        r"\b(?:people|persons|migrants)\s+(?:are\s+)?missing\b",
    )
)
_RESOLVED_DISTRESS_PATTERNS = tuple(
    re.compile(pattern, re.I)
    for pattern in (
        r"\b(?:all|everyone|the group|the people)\s+(?:is|are|were)\s+(?:now\s+)?safe\b",
        r"\b(?:was|were|has been|have been)\s+(?:rescued|disembarked|transferred to the mainland)\b",
        r"\b(?:rescue|operation)\s+(?:was|is|has been)\s+(?:completed|concluded)\b",
        r"\barrived safely\b",
        r"\b(?:the\s+)?(?:people|persons|group|everyone|they)\s+"
        r"(?:have\s+|has\s+)?arrived\s+(?:on|in|at)\s+\S+",
        r"\b(?:the\s+)?(?:people|persons|group|everyone|they)\s+"
        r"(?:have\s+|has\s+)?(?:disembarked|landed)\s+(?:on|in|at)\s+\S+",
        r"\b(?:reached|made\s+it\s+to)\s+(?:land|shore|safety)\b",
    )
)
# A rescue mentioned alongside any of these is one step in a still-ongoing
# crisis (a pushback/forced-return/disembarkation dispute), not a resolved
# ending — see is_resolved_distress's docstring for the motivating report.
_RESOLUTION_OVERRIDE_RE = re.compile(
    r"\b(?:pushback|forced back|refuses?\s+to\s+disembark|denied\s+disembark"
    r"|at\s+risk\s+of\s+being|not\s+(?:yet\s+)?safe|outrageous"
    r"|not\s+(?:yet\s+)?rescued|waiting\s+to\s+be\s+rescued"
    r"|needs?\s+to\s+be\s+disembarked|still\s+(?:in\s+)?(?:danger|distress|at\s+risk)"
    r"|situation\s+is\s+not\s+resolved|no\s+news\s+of\s+them)\b",
    re.I,
)

# Explicit evidence that an update keeps or re-opens an incident.  These
# signals take precedence over a bare rescue word: rescue can be only one step
# in an ongoing pushback, unsafe transfer or disembarkation crisis.
_ONGOING_INCIDENT_PATTERNS = tuple(
    re.compile(pattern, re.I)
    for pattern in (
        r"\b(?:situation|case|incident)\s+(?:is|remains?)\s+not\s+resolved\b",
        r"\bnot\s+(?:yet\s+)?(?:rescued|safe)\b",
        r"\b(?:still\s+)?waiting\s+to\s+be\s+rescued\b",
        r"\b(?:still|remain(?:s|ing)?|(?:are|is)\s+(?:still\s+)?)\s*"
        r"(?:in\s+)?(?:danger|distress|at\s+risk)\b",
        r"\b(?:rescue|assistance)\s+(?:is\s+)?(?:still\s+)?(?:urgent|needed|required)\b",
        r"\bneeds?\s+to\s+be\s+disembarked\b",
        r"\b(?:forced|taken|heading)\s+(?:back\s+)?(?:to|towards?)\b.{0,80}"
        r"\b(?:unsafe|not\s+safe|pushback|forced\s+return)\b",
        r"\bcountry\s+of\s+safety\b.{0,80}\b(?:is\s+not|isn't|not\s+safe)\b",
        r"\b(?:(?:no|without)\s+news|lost\s+contact|without\s+contact"
        r"|(?:unable|not\s+able)\s+to\s+reach|(?:cannot|can't|couldn't)\s+reach)\b",
    )
)
# A report can also conclude with a known, final outcome (survivors accounted
# for, some confirmed missing) rather than a purely positive one. Either way
# there is no longer an active SAR situation needing live tracking — this is
# distinct from _RESOLVED_DISTRESS_PATTERNS (kept ingestion-side, unchanged)
# and used only for the public Live map's lifecycle colouring (see live.py),
# so widening it here cannot change what gets ingested as a distress call.
_CONCLUDED_OUTCOME_PATTERNS = tuple(
    re.compile(pattern, re.I)
    for pattern in (
        # English
        r"\bsurvivors?\s+(?:were|was|have been)?\s*found\b",
        r"\b(?:were|was)\s+(?:found|hospitalis|hospitaliz)",
        r"\b(?:people|persons|migrants)?\s*remain(?:s|ing)?\s+missing\b",
        r"\bstill\s+missing\b",
        r"\bconfirmed\s+dead\b",
        r"\bbod(?:y|ies)\s+(?:were|was)?\s*recovered\b",
        # A known interception / forced-return outcome is terminal for the
        # immediate SAR call even when it is a harmful outcome rather than a
        # safe rescue. Real production case: SOS Mediterranee reported a
        # Mayday relay, then in the same post stated that it witnessed the
        # Libyan Coast Guard intercept the boat and return those on board.
        r"\bwitnessed\b.{0,160}\bintercept\w*\b.{0,160}\b(?:and\s+)?return\w*\b.{0,100}\b(?:to|back\s+to)\b",
        r"\bintercept(?:ed|ion)\b.{0,160}\b(?:returned|forced\s+back|taken\s+back|brought\s+back)\b.{0,100}\b(?:to|towards?)\b",
        # Retrospective/aftermath reports: past-tense rescue/tragedy write-ups
        # (e.g. "⚫️ Massacre in the Atlantic ... brought ashore 38 people ...
        # after drifting at sea for 25 days"). The active variant "drifting at
        # sea" (no "for") still reads as an open incident and is not matched.
        r"\bmassacre\b",
        r"\bbrought\s+ashore\b",
        r"\bdrifting\s+at\s+sea\s+for\b",
        # French — Alarm Phone posts most reports bilingually (EN/FR); without
        # these, a French duplicate of an already-concluded English report
        # would show active/red while its English twin shows resolved/green.
        r"\bsurvivants?\s+(?:ont\s+été|ont\s+ete)?\s*retrouv[ée]s?\b",
        r"\b(?:ont\s+été|ont\s+ete)\s+(?:retrouv[ée]s?|hospitalis[ée]s?)\b",
        r"\b(?:sont\s+)?(?:toujours\s+)?port[ée]s?\s+disparu(?:e|s|es)?\b",
        r"\bd[ée]c[ée]d[ée]s?\s+confirm[ée]s?\b",
        r"\bcorps\s+(?:ont\s+été|ont\s+ete|a\s+été|a\s+ete)?\s*retrouv[ée]s?\b",
        # Italian
        r"\bsopravvissuti\s+(?:sono\s+stati\s+)?trovati\b",
        r"\brisultano\s+(?:ancora\s+)?dispersi\b",
        r"\bcorpi\s+(?:sono\s+stati\s+)?recuperati\b",
        # A common Alarm Phone follow-up phrasing that the "were/was found"
        # pattern above misses: "we have now learned that the people have
        # been found and taken to a reception centre" (present-perfect, not
        # simple past). Real production case: a still-"needs_review" case
        # whose only textual reply was exactly this, never landing on
        # resolved. "reception centre" alone is also matched since Alarm
        # Phone sometimes reports that outcome without repeating "found".
        r"\b(?:have|has)\s+been\s+found\b",
        r"\btaken\s+to\s+a\s+reception\s+(?:centre|center)\b",
    )
)


def is_concluded_incident(text: str) -> bool:
    """True once a report's own text describes a final, known outcome.

    Broader than is_resolved_distress: also matches conclusive-but-not-purely-
    happy reports (e.g. "8 survivors were found... 4 people remain missing"),
    which describe a closed incident just as much as a clean rescue does.
    Lifecycle-only — never used to gate ingestion/auto-drift.

    Mirrors the SOS override from is_direct_distress_call: an explicit "🆘"
    opener marks an ongoing active call ("🆘 ... They were found by the police.
    Since then we have no news" is NOT a closed incident), so it suppresses
    concluded-outcome wording; a hard resolved outcome ("🆘 ... everyone is
    safe") still closes the incident.
    """
    normalised = re.sub(r"\s+", " ", text).strip()
    if not normalised:
        return False
    if is_ongoing_incident(normalised):
        return False
    if is_resolved_distress(normalised):
        return True
    # SOS / Mayday is evidence that a call existed, not proof that it remains
    # open after the same report gives a final outcome. Ongoing-language is
    # checked first above, so "🆘 ... found by police; since then no news"
    # still remains active while a completed interception/return does not.
    return any(pattern.search(normalised) for pattern in _CONCLUDED_OUTCOME_PATTERNS)


def is_ongoing_incident(text: str) -> bool:
    """True when an update explicitly says the emergency remains open."""
    normalised = re.sub(r"\s+", " ", text or "").strip()
    return bool(normalised) and any(
        pattern.search(normalised) for pattern in _ONGOING_INCIDENT_PATTERNS
    )

# ── Regex patterns ────────────────────────────────────────────────────────────
_RE_DECIMAL_NS = re.compile(
    r"(\d{1,2}(?:\.\d{1,5})?)\s*°?\s*([NnSs])"
    r"[,\s/]+(\d{1,3}(?:\.\d{1,5})?)\s*°?\s*([EeWw])"
)
_RE_DECIMAL_PAIR = re.compile(
    r"(?<!\d)([+-]?(?:3[0-9]|4[0-4])\.\d{2,5})[,\s/]+([+-]?(?:[0-3]?\d|1[0-7]\d)\.\d{2,5})(?!\d)"
)
_MIN = "[\x27‘’′]"  # ascii apostrophe, left/right curly quote, prime
_DEG = r"[°º]"
_RE_DMS = re.compile(
    r"(\d{1,2})" + _DEG + r"(\d{1,2})" + _MIN + r"?\s*([NnSs])[,\s]+"
    r"(\d{1,3})" + _DEG + r"(\d{1,2})" + _MIN + r"?\s*([EeWw])"
)
# Alarm Phone / map format: "N 34° 30’ ..." or "S 34°30’" etc.
_RE_DMS_PREFIX = re.compile(
    r"([NnSs])\s*(\d{1,2})\s*" + _DEG + r"\s*(\d{1,2})\s*" + _MIN
    + r"[^EeWw]{0,25}"
    + r"([EeWw])\s*(\d{1,3})\s*" + _DEG + r"\s*(\d{1,2})\s*" + _MIN
)
_RE_POSITION_LABEL = re.compile(
    r"(?:position|pos|coord|gps|location)[:\s]+([^\n]{5,60})", re.I
)
_RE_OCR_PREFIX_COORD = re.compile(
    r"([NS])\s*([0-9OQ@]{1,3})\s*[°º]\s*"
    r"([0-9OQ@]{1,2}(?:[.,][0-9OQ@]+)?)\s*['’′°º\"”″]"
    r"(?:\s*([0-9OQ@]{1,2}(?:[.,][0-9OQ@]+)?)\s*[\"”″])?"
    r"[^EW]{0,35}"
    r"([EW])\s*([0-9OQ@]{1,3})\s*[°º]\s*"
    r"([0-9OQ@]{1,2}(?:[.,][0-9OQ@]+)?)\s*['’′°º\"”″]"
    r"(?:\s*([0-9OQ@]{1,2}(?:[.,][0-9OQ@]+)?)\s*[\"”″])?",
    re.I,
)
_RE_OCR_DMM_PREFIX = re.compile(
    r"(?<![A-Z])([NS])\s*[|:]?\s*([0-9OQ@]{1,3})\s*[°º]\s*"
    r"([0-9OQ@]{1,2}[.,][0-9OQ@]+)"
    r"[^EW]{0,35}"
    r"(?<![A-Z])([EW])\s*[|:]?\s*([0-9OQ@]{1,3})\s*[°º]\s*"
    r"([0-9OQ@]{1,2}[.,][0-9OQ@]+)",
    re.I,
)
_RE_OCR_DMS_SUFFIX = re.compile(
    r"([0-9OQ@]{1,2})\s*[°º]\s*"
    r"([0-9OQ@]{1,2})\s*['’′:\s]\s*"
    r"([0-9OQ@]{1,2}(?:[.,][0-9OQ@]+)?)\s*[°º\"”″]?\s*([NS])"
    r"[^0-9NSEW]{0,24}"
    r"([0-9OQ@]{1,3})\s*[°º]\s*"
    r"([0-9OQ@]{1,2})\s*['’′:\s]\s*"
    r"([0-9OQ@]{1,2}(?:[.,][0-9OQ@]+)?)\s*[°º\"”″]?\s*([EW])",
    re.I,
)
_RELATIVE_DISTANCE = r"(\d{1,3}(?:\.\d+)?)\s*(km|kilomet(?:er|re)s?|nm|nautical miles?)"
_RELATIVE_DIRECTION = (
    r"(north|south|east|west|north[ -]?east|north[ -]?west|"
    r"south[ -]?east|south[ -]?west)"
)
_BEARINGS = {
    "north": 0.0,
    "northeast": 45.0,
    "east": 90.0,
    "southeast": 135.0,
    "south": 180.0,
    "southwest": 225.0,
    "west": 270.0,
    "northwest": 315.0,
}


def extract_numeric_coords(text: str) -> Optional[tuple[float, float]]:
    """Return only explicit numeric coordinates, never a place-name centroid.

    The result is region-gated (``_valid``) and sea-snapped: a boat position
    read out of text or off a map label must be inside the area SeaCommons
    covers and in the water.
    """
    raw = _extract_numeric_coords_raw(text)
    return nearest_sea_point(*raw) if raw is not None else None


def _extract_numeric_coords_raw(text: str) -> Optional[tuple[float, float]]:
    # OCR commonly confuses digits for letters on the small text of a map
    # label. Only fold letters that are never a hemisphere marker (N/S/E/W)
    # or unit -- folding S->5 would corrupt a southern latitude.
    ocr_text = text.upper().translate(str.maketrans({
        "O": "0", "Q": "0", "@": "0",
        "I": "1", "|": "1", "!": "1",
        "Z": "2", "B": "8",
    }))
    dms_suffix = _RE_OCR_DMS_SUFFIX.search(ocr_text)
    if dms_suffix:
        lat_minutes, lat_seconds = float(dms_suffix.group(2)), float(dms_suffix.group(3).replace(",", "."))
        lon_minutes, lon_seconds = float(dms_suffix.group(6)), float(dms_suffix.group(7).replace(",", "."))
        if lat_minutes < 60 and lon_minutes < 60 and lat_seconds < 60 and lon_seconds < 60:
            lat = float(dms_suffix.group(1)) + lat_minutes / 60 + lat_seconds / 3600
            lon = float(dms_suffix.group(5)) + lon_minutes / 60 + lon_seconds / 3600
            if dms_suffix.group(4) == "S":
                lat = -lat
            if dms_suffix.group(8) == "W":
                lon = -lon
            if _valid(lat, lon):
                return round(lat, 6), round(lon, 6)
    dmm_match = _RE_OCR_DMM_PREFIX.search(ocr_text)
    if dmm_match:
        lat_minutes = float(dmm_match.group(3).replace(",", "."))
        lon_minutes = float(dmm_match.group(6).replace(",", "."))
        if lat_minutes >= 60 or lon_minutes >= 60:
            dmm_match = None
        else:
            lat = float(dmm_match.group(2)) + lat_minutes / 60
            lon = float(dmm_match.group(5)) + lon_minutes / 60
    if dmm_match:
        if dmm_match.group(1).upper() == "S":
            lat = -lat
        if dmm_match.group(4).upper() == "W":
            lon = -lon
        if _valid(lat, lon):
            return round(lat, 6), round(lon, 6)
    ocr_match = _RE_OCR_PREFIX_COORD.search(ocr_text)
    if ocr_match:
        def component(degrees: str, minutes: str, seconds: Optional[str]) -> Optional[float]:
            deg = float(degrees.replace(",", "."))
            minute = float(minutes.replace(",", "."))
            second = float(seconds.replace(",", ".")) if seconds else 0.0
            if minute >= 60 or second >= 60:
                return None
            return deg + minute / 60 + second / 3600

        lat = component(ocr_match.group(2), ocr_match.group(3), ocr_match.group(4))
        lon = component(ocr_match.group(6), ocr_match.group(7), ocr_match.group(8))
        if lat is not None and lon is not None:
            if ocr_match.group(1).upper() == "S":
                lat = -lat
            if ocr_match.group(5).upper() == "W":
                lon = -lon
            if _valid(lat, lon):
                return round(lat, 6), round(lon, 6)

    # 0. Look for "Position: ..." prefix first — common in Alarm Phone tweets
    m = _RE_POSITION_LABEL.search(text)
    snippet = m.group(1) if m else text

    # 1. Decimal with N/E suffix  e.g. "35.5N 12.3E"
    m = _RE_DECIMAL_NS.search(snippet) or _RE_DECIMAL_NS.search(text)
    if m:
        lat, lon = float(m.group(1)), float(m.group(3))
        if m.group(2).lower() == "s":
            lat = -lat
        if m.group(4).lower() == "w":
            lon = -lon
        if _valid(lat, lon):
            return lat, lon

    # 2. Decimal pair  e.g. "35.52, 12.30" (restricted to Mediterranean range)
    m = _RE_DECIMAL_PAIR.search(snippet) or _RE_DECIMAL_PAIR.search(text)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        if _valid(lat, lon):
            return lat, lon

    # 3a. DMS  e.g. "35°30'N 12°15'E"
    m = _RE_DMS.search(text)
    if m:
        lat = int(m.group(1)) + int(m.group(2)) / 60.0
        lon = int(m.group(4)) + int(m.group(5)) / 60.0
        if m.group(3).lower() == "s":
            lat = -lat
        if m.group(6).lower() == "w":
            lon = -lon
        if _valid(lat, lon):
            return lat, lon

    # 3b. DMS prefix form  e.g. "N 34° 30' ... E 013° 15'"
    m = _RE_DMS_PREFIX.search(text)
    if m:
        lat = int(m.group(2)) + int(m.group(3)) / 60.0
        lon = int(m.group(5)) + int(m.group(6)) / 60.0
        if m.group(1).lower() == "s":
            lat = -lat
        if m.group(4).lower() == "w":
            lon = -lon
        if _valid(lat, lon):
            return lat, lon

    return None


def extract_coords(text: str) -> Optional[tuple[float, float]]:
    """
    Return (lat, lon) from text, or None.
    Tries explicit numeric coordinates before a known-place centroid.
    """
    numeric = extract_numeric_coords(text)
    if numeric:
        # An explicit readout is trusted, but an OCR/typo'd digit can still put
        # a boat a few km inland — snap onto water (a no-op when already at sea).
        return nearest_sea_point(*numeric)

    relative = extract_relative_coords(text)  # already sea-snapped internally
    if relative:
        return relative

    # 4. Place name gazetteer (longest match first). Every report here is a
    # boat, always at sea — a place's own centroid can still legitimately
    # land on its landmass (a small island's true geometric center, a
    # coastal city itself), so nudge onto the nearest sea point rather than
    # plotting a boat on dry land. See core.intel.landmask for why this is
    # a search rather than a per-place hand-curated offset.
    tl = text.lower()
    for place, coords in _PLACES_SORTED:
        if place in tl:
            return nearest_sea_point(*coords)

    return None


def place_match_precision(text: str) -> Optional[str]:
    """Which gazetteer tier a bare place-name match would resolve through.

    Mirrors extract_coords's own place-lookup order (precise tier first,
    longest match within each tier) without duplicating its numeric/relative
    branches -- callers only need this after extract_coords has already
    fallen through to the gazetteer, to size the reported uncertainty
    honestly: a country/sea-scale name (_IMPRECISE_PLACE_NAMES) implies a
    far larger "could be anywhere in here" than a specific city or small
    island does, and today both were reported with the same flat radius.
    """
    tl = text.lower()
    for place, _coords in _PLACES_SORTED:
        if place in tl:
            return "imprecise" if place in _IMPRECISE_PLACE_NAMES else "precise"
    return None


def find_all_place_matches(text: str) -> list[tuple[str, tuple[float, float], str]]:
    """Every distinct gazetteer place mentioned in text, precise-tier first.

    Unlike extract_coords (first match only), used to build a search-area
    polygon that actually follows what a report says when it names more
    than one place -- "informed authorities in Italy and Malta" implies a
    corridor between the two, not just whichever matches first.
    Deduplicated by resolved coordinate, since aliases of the same place
    (e.g. "malta"/"valletta") must not double-count as two distinct points.
    """
    tl = text.lower()
    seen: set[tuple[float, float]] = set()
    matches: list[tuple[str, tuple[float, float], str]] = []
    for place, coords in _PLACES_SORTED:
        if place in tl and coords not in seen:
            seen.add(coords)
            matches.append((place, coords, "imprecise" if place in _IMPRECISE_PLACE_NAMES else "precise"))
    return matches


_SEVERE_WEATHER_TERMS = (
    "severe weather", "bad weather", "storm", "stormy", "high seas",
    "rough sea", "rough seas", "gale", "strong wind", "strong winds",
    "heavy wind", "heavy winds", "big waves", "high waves",
    "mauvais temps", "tempête", "mer agitée", "mare mosso", "maltempo",
)


def mentions_severe_weather(text: str) -> bool:
    """Whether the report itself claims rough conditions.

    Gates area_extract.extract_area's weather-based narrowing: wave-height
    data is only used to shrink a search area when the report actually
    asserts bad weather is involved -- never as an independent, unprompted
    guess at where a boat might be.
    """
    tl = text.lower()
    return any(term in tl for term in _SEVERE_WEATHER_TERMS)


def extract_relative_coords(text: str) -> Optional[tuple[float, float]]:
    """Resolve statements such as ``50 km south of Crete``.

    The result is an approximate point derived from a declared distance and a
    gazetteer centroid. Callers must retain an uncertainty label; this is never
    equivalent to an explicit GPS position.
    """
    normalized = re.sub(r"[#_]", " ", text.lower())
    normalized = re.sub(r"\s+", " ", normalized)

    def offset(origin: tuple[float, float], match: re.Match[str]) -> Optional[tuple[float, float]]:
        distance = float(match.group(1))
        unit = match.group(2).lower()
        if unit == "nm" or unit.startswith("nautical"):
            distance *= 1.852
        direction = re.sub(r"[ -]", "", match.group(3).lower())
        bearing = math.radians(_BEARINGS[direction])
        lat, lon = origin
        north_km = math.cos(bearing) * distance
        east_km = math.sin(bearing) * distance
        estimated_lat = lat + north_km / 111.32
        lon_scale = max(0.2, math.cos(math.radians(lat)))
        estimated_lon = lon + east_km / (111.32 * lon_scale)
        if _valid(estimated_lat, estimated_lon):
            # "20 km south of Crete" can land the computed point back on the
            # island; the boat is offshore, so nudge onto water.
            return nearest_sea_point(round(estimated_lat, 5), round(estimated_lon, 5))
        return None

    for place, origin in _PLACES_SORTED:
        place_pattern = re.escape(place).replace(r"\ ", r"\s+")
        pattern = re.compile(
            _RELATIVE_DISTANCE
            + r"\s+"
            + _RELATIVE_DIRECTION
            + r"\s+(?:of|from)\s+(?:the\s+(?:island|coast)\s+of\s+)?"
            + place_pattern,
            re.I,
        )
        match = pattern.search(normalized)
        if not match:
            continue
        return offset(origin, match)

    # Alarm Phone can name the island in one sentence, then write "50 km
    # south of the island" in the next. Resolve this only when the post names
    # exactly one gazetteer location, avoiding an arbitrary place selection.
    generic = re.search(
        _RELATIVE_DISTANCE
        + r"\s+"
        + _RELATIVE_DIRECTION
        + r"\s+(?:of|from)\s+(?:the\s+)?(?:island|coast)\b",
        normalized,
        re.I,
    )
    if generic:
        origins = {
            origin
            for place, origin in _PLACES_SORTED
            if re.search(
                r"\b" + re.escape(place).replace(r"\ ", r"\s+") + r"\b",
                normalized,
            )
        }
        if len(origins) == 1:
            return offset(next(iter(origins)), generic)
    return None


def _valid(lat: float, lon: float) -> bool:
    """A coordinate is usable only if it is a real lat/lon AND inside the
    maritime area SeaCommons covers. A pair parsed out of unrelated tweet text
    (a date, a hashtag, an OCR misread) is almost always outside it — plotting
    it as a distress position somewhere in the Indian Ocean is worse than
    returning None and letting a weaker place-name match take over."""
    return (
        -90 <= lat <= 90
        and -180 <= lon <= 180
        and in_operational_region(lat, lon)
    )


def is_distress(text: str) -> bool:
    """True if text contains any distress keyword.

    Deliberately a loose pre-filter -- core.intel.twitter_monitor uses it
    only to decide whether a tweet is worth ingesting at all;
    is_direct_distress_call() is what actually sets an event's severity,
    coordinates and public is_distress metadata. Still excludes the same
    known non-distress shapes that stricter function excludes
    (docs/ALERT_RECOGNITION_BASELINE.md), so an obviously non-distress post
    doesn't create ingestion noise.
    """
    tl = text.lower()
    if _RETROSPECTIVE_COMMEMORATION_RE.search(tl):
        return False
    if _SOS_MARKER_RE.search(tl):
        return True
    return any(kw in tl for kw in DISTRESS_KW)


def is_direct_distress_call(text: str) -> bool:
    """Return True only for an actionable, direct distress or SAR request.

    This intentionally excludes retrospective reporting and generic concern.
    Live uses this stricter signal than ``is_distress`` so a mention of a past
    shipwreck or an already completed rescue cannot become an operational call.
    """
    normalised = re.sub(r"\s+", " ", text).strip()
    if not normalised:
        return False
    if any(pattern.search(normalised) for pattern in _RESOLVED_DISTRESS_PATTERNS):
        return False
    # A commemoration of a past incident (an anniversary, a memorial, a
    # vigil) is not itself an active call, however many distress keywords
    # it repeats while doing so -- e.g. "Last year's shipwreck anniversary
    # was marked with a vigil in Lampedusa" (docs/ALERT_RECOGNITION_BASELINE.md).
    if _RETROSPECTIVE_COMMEMORATION_RE.search(normalised):
        return False
    # A Mayday/SOS marker establishes that an emergency call existed, but a
    # final outcome in the same report retires it from the operational Live
    # surface. Explicit ongoing language takes precedence in
    # is_concluded_incident(), so "🆘 ... found by police; since then no news"
    # is still active while "Mayday ... intercepted and returned" is not.
    if is_concluded_incident(normalised):
        return False
    return any(pattern.search(normalised) for pattern in _DIRECT_DISTRESS_PATTERNS)


def is_resolved_distress(text: str) -> bool:
    """True only when the text explicitly reports a completed safe outcome.

    A rescue mention is not automatically a resolved incident: Alarm Phone
    posts like "...was over night rescued by Merchant Vessel Safi Lion. Even
    though Crete is clearly the closest port, @HCoastGuard refuses to
    disembark the people... This is outrageous!" describe an ONGOING rights
    violation (pushback / forced return / refused disembarkation) that just
    happens to mention a rescue as one step in a still-active crisis — a bare
    "rescued" match there would wrongly show the incident as a green,
    resolved marker on the live map. When that kind of override language is
    present, only the strict phrase patterns below (which require "was/were
    rescued" as a tight, unbroken phrase) can still mark it resolved.
    """
    normalised = re.sub(r"\s+", " ", text).strip()
    if is_ongoing_incident(normalised):
        return False
    if not _RESOLUTION_OVERRIDE_RE.search(normalised) and re.search(r"\brescued\s*!*", normalised, re.I):
        return True
    return any(pattern.search(normalised) for pattern in _RESOLVED_DISTRESS_PATTERNS)


def is_rescue(text: str) -> bool:
    tl = text.lower()
    return any(kw in tl for kw in RESCUE_KW)


def classify_severity(text: str) -> str:
    """Return 'critical' | 'high' | 'medium' | 'low'."""
    tl = text.lower()
    if any(kw in tl for kw in _CRITICAL_TERMS):
        return "critical"
    if any(kw in tl for kw in _HIGH_TERMS):
        return "high"
    if any(kw in tl for kw in _MEDIUM_TERMS):
        return "medium"
    return "low"
