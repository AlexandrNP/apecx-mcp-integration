"""Consumer-side disambiguation overrides for the synonym dictionary.

The published ``multiclade-species`` dictionary correctly flags more surface
forms AMBIGUOUS than the prior ``sc-a4c`` build did — including organisms whose
OLD and NEW (post-ICTV-rename) names are kept as two distinct entries (e.g.
"canine distemper virus" vs "Morbillivirus canis"), so the shared acronym
resolves to ``ambiguous`` instead of a single taxon. For a curated set of these,
a single canonical resolution is the intended one.

This overlay restores that resolution WITHOUT rebuilding (or overriding) the
published dictionary: :func:`lookup_entity` consults this map BEFORE the
ambiguous path, so an overridden surface form resolves cleanly (``id_anchored``,
``path="override"``). Each value is the taxon the prior dictionary returned for
that surface form, so harmonization that worked before keeps working; the IRIs
are all present in the current published dictionary's ``entries`` (verified).

Curate as new false-ambiguities surface. The visibility contract is preserved:
the result's ``path`` is ``"override"``, never silently disguised as a dict hit.
"""

from __future__ import annotations


def _t(taxid: str) -> str:
    return f"http://purl.obolibrary.org/obo/NCBITaxon_{taxid}"


# surface_form_normalized -> the prior dict's canonical resolution. The trailing
# comment names the organism and the conflicting alternative the server splits on.
DISAMBIGUATION_OVERRIDES: dict[str, str] = {
    "sars-cov": _t("2901879"),  # SARS-CoV-1 (vs SARS-related-CoV 694009)
    "cdv": _t("3139435"),  # canine distemper virus (vs Morbillivirus canis)
    "isav": _t("2907958"),  # infectious salmon anemia virus (vs Isavirus salaris)
    "infectious salmon anemia virus": _t("2907958"),
    "bovine respiratory syncytial virus": _t("3136119"),  # vs Bovine orthopneumovirus
    "bvdv": _t("11099"),  # Bovine viral diarrhea virus 1 (vs BVDV-2)
    "adenovirus": _t("10535"),  # unidentified adenovirus (vs Adenoviridae family)
    "avian encephalomyelitis virus": _t("2871093"),  # tremovirus A1 (vs Tremovirus A)
}
