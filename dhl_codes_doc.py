#!/usr/bin/env python3
"""Generate docs/codes-suivi-dhl.md, the DHL tracking codes reference, in Markdown so GitLab and GitHub render it.

Alert columns, priorities, colours, limits and the status example come from the tracker's own
modules, so the page cannot drift from what the tracker does: tests/test_dhl_codes_doc.py fails
until the page is regenerated with `python dhl_codes_doc.py`.
"""
import re
from dataclasses import dataclass
from pathlib import Path

from alert_dispatch import DEFAULT_HELPDESK_TAGS, DEFAULT_HELPDESK_TEAM, DEFAULT_TICKET_SKIP_CODES
from automated_tracker import TRACKING_WINDOW_DAYS
from dhl_client import MIN_SECONDS_BETWEEN_CALLS
from odoo_client import TRACKING_EXPIRED_PREFIX, format_status_text
from shipment_alerts import ALERT_CODES, ALERT_MAX_EVENT_AGE, FAMILIES

OUTPUT_PATH = Path(__file__).resolve().parent / "docs" / "codes-suivi-dhl.md"
EXPRESS_EVENT_CODE_COUNT = 40  # official DHL Express event codes (API/status_1.csv)
DHL_DAILY_QUOTA = 250  # DHL developer portal default service level (not enforced by the tracker); see README
HOURLY_CALLS_PER_DAY = 24
EXAMPLE_TRACKING_NUMBER = "1234567890"  # fictitious: the GitHub copy of this page is public
STATUS_EXAMPLE = ("Shipment on hold pending duty payment", "Pay the duties online", "HP")
NO_ALERT = "—"

PRIORITY_LABELS = {"0": "Basse", "1": "Moyenne", "2": "Haute", "3": "Urgente"}
GROUP_TITLES = {
    "flow": "Parcours normal",
    "customs": "Douane & paiement",
    "delivery": "Échec de livraison & adresse",
    "return": "Refus, retour, dommage",
    "incident": "Incidents DHL",
}


@dataclass(frozen=True)
class EventCode:
    code: str
    dhl_label: str
    meaning: str
    group: str
    no_estimated_delivery: bool = False  # "no EDD" in DHL's list
    new_since_june_2024: bool = False


EVENT_CODES = (
    EventCode("PU", "Shipment pick up", "Colis enlevé chez l'expéditeur.", "flow"),
    EventCode("SA", "Shipment acceptance", "Pris en charge par DHL.", "flow"),
    EventCode("PL", "Processed at location", "Traité dans un site DHL.", "flow"),
    EventCode("DF", "Depart facility", "A quitté un site DHL.", "flow"),
    EventCode("AF", "Arrived facility", "Arrivé dans un site DHL.", "flow"),
    EventCode("AR", "Arrival in delivery facility", "Arrivé à l'agence qui va livrer.", "flow"),
    EventCode("WC", "With delivering courier", "En tournée de livraison.", "flow"),
    EventCode("OK", "Delivery", "Livré.", "flow", no_estimated_delivery=True),
    EventCode("TR", "Record of transfer", "Transfert enregistré entre deux réseaux.", "flow"),
    EventCode("SM", "Scheduled for movement", "Planifié sur un prochain acheminement.", "flow", new_since_june_2024=True),
    EventCode("FD", "Forward destination (DD's expected)",
              "Réacheminé vers une autre destination, livraison toujours attendue.", "flow"),
    EventCode("AD", "Agreed delivery", "Date ou lieu de livraison convenu avec le destinataire.", "flow"),
    EventCode("SC", "Service changed", "Produit ou service DHL modifié.", "flow"),
    EventCode("IC", "In clearance processing", "En cours de dédouanement.", "customs"),
    EventCode("RR", "Response received", "Réponse reçue, souvent de la douane.", "customs"),
    EventCode("CR", "Clearance release", "Dédouané.", "customs"),
    EventCode("BR", "Broker release", "Libéré par le transitaire.", "customs", no_estimated_delivery=True),
    EventCode("BN", "Customer broker notified", "Transitaire du destinataire prévenu.", "customs"),
    EventCode("CD", "Controllable clearance delay",
              "Retard de douane que l'on peut débloquer : document ou information manquante.", "customs"),
    EventCode("UD", "Uncontrollable clearance delay",
              "Retard de douane hors de notre main : contrôle, inspection.", "customs"),
    EventCode("HP", "Held for payment", "Bloqué jusqu'au paiement des droits et taxes.", "customs"),
    EventCode("PY", "Payment", "Paiement reçu.", "customs", new_since_june_2024=True),
    EventCode("ND", "Not delivered", "Non livré.", "delivery"),
    EventCode("NH", "Not home", "Destinataire absent.", "delivery", no_estimated_delivery=True),
    EventCode("MD", "Missed delivery cycle", "Tournée de livraison manquée.", "delivery"),
    EventCode("CA", "Closed on arrival", "Destinataire fermé au passage du livreur.", "delivery",
              no_estimated_delivery=True),
    EventCode("CC", "Awaiting cnee collection", "À retirer par le destinataire en agence ou point relais.",
              "delivery", no_estimated_delivery=True),
    EventCode("BA", "Bad address", "Adresse incorrecte ou incomplète.", "delivery"),
    EventCode("CM", "Customer moved", "Le destinataire a déménagé.", "delivery"),
    EventCode("RD", "Refused delivery", "Refusé par le destinataire.", "return", no_estimated_delivery=True),
    EventCode("RT", "Returned to consignor", "Retourné à l'expéditeur, donc à nous.", "return",
              no_estimated_delivery=True),
    EventCode("DD", "Delivered damaged", "Livré endommagé.", "return", no_estimated_delivery=True),
    EventCode("PD", "Partial delivery", "Livraison partielle : des colis manquent.", "return",
              no_estimated_delivery=True),
    EventCode("DS", "Destroyed / disposal", "Envoi détruit.", "return", no_estimated_delivery=True),
    EventCode("OH", "On hold", "Envoi en attente chez DHL.", "incident"),
    EventCode("SS", "Shipment stopped", "Envoi stoppé.", "incident", no_estimated_delivery=True),
    EventCode("MS", "Mis-sort", "Erreur de tri, réacheminement en cours.", "incident"),
    EventCode("MC", "Miscode", "Erreur de code ou d'étiquette.", "incident"),
    EventCode("TP", "Forwarded to 3rd party - no DD's",
              "Confié à un transporteur tiers, plus de suivi de livraison.", "incident", no_estimated_delivery=True),
    EventCode("CS", "Closed shipment", "Dossier d'envoi clôturé.", "incident", no_estimated_delivery=True),
)

GLOBAL_STATUSES = (
    ("pre-transit", "Étiquette créée, pas encore remis à DHL."),
    ("transit", "En route, y compris en douane ou en attente."),
    ("delivered", "Livré. L'envoi est marqué livré dans Odoo."),
    ("failure", "Échec : non livré, retour, perte."),
    ("unknown", "DHL ne sait pas encore."),
)
NUMERIC_STATUSES = (("104", "pre-transit"), ("102", "transit"), ("101", "delivered"), ("103", "failure"))

API_FIELDS = (
    ("Statut global", "status.statusCode", "Oui", "Toujours présent (champ obligatoire)."),
    ("Code du dernier événement", "events[].status", "Oui", "Code Express de 2 lettres."),
    ("Description", "description", "Oui", "En anglais, ou en français avec `language=fr`."),
    ("Prochaines étapes", "status.nextSteps", "Parfois", "Seulement quand DHL a une consigne."),
    ("Historique complet", "events[]", "Oui", "30 à 45 événements par envoi, avec date, heure et lieu."),
    ("Lieu de l'événement", "location.address", "Oui", "Ville ou hub, par exemple CINCINNATI HUB."),
    ("Produit", "details.product", "Oui", "EXPRESS 12:00 (Y), EXPRESS WORLDWIDE (P)."),
    ("Origine et destination", "origin, destination", "Oui", "Ville et pays seulement."),
    ("Colis", "details.totalNumberOfPieces", "Oui", "Nombre de colis et leurs identifiants."),
    ("Preuve de livraison", "details.proofOfDelivery", "Parfois", "Liens vers le POD et la signature, une fois livré."),
    ("Expéditeur et destinataire", "details.shipper, consignee", "Oui", ""),
    ("Date de livraison estimée", "estimatedTimeOfDelivery", "Non", "Vide sur nos envois Express."),
    ("Poids et dimensions", "details.weight, dimensions", "Non", "Vides sur nos envois Express."),
)

TITLE_LEVELS = "Un événement, trois niveaux"
TITLE_CODES = f"Les {EXPRESS_EVENT_CODE_COUNT} codes événement Express"
TITLE_TICKETS = "Quand un ticket est ouvert"
TITLE_API = "Ce que l'API renvoie pour nos envois"
TITLE_LIMITS = "Limites à connaître"


def alert_label(code: str) -> str:
    """Alert column: what the tracker does when this code is a shipment's latest event."""
    alert = ALERT_CODES.get(code)
    if alert is None:
        return NO_ALERT
    if code in DEFAULT_TICKET_SKIP_CODES:
        return "Mattermost"
    return f"**Ticket** · {PRIORITY_LABELS[alert.family.priority]}"


def _codes(codes) -> str:
    return " ".join(f"`{code}`" for code in codes)


def _anchor(title: str) -> str:
    """Heading anchor as GitLab and GitHub build it (lowercase, punctuation dropped, spaces to hyphens)."""
    return re.sub(r"[^\w\- ]", "", title.lower()).replace(" ", "-")


def _table(header: tuple, rows) -> list:
    return [
        f"| {' | '.join(header)} |",
        f"|{'|'.join('---' for _ in header)}|",
        *(f"| {' | '.join(row)} |" for row in rows),
    ]


def _intro() -> list:
    contents = " · ".join(
        f"[{title}](#{_anchor(title)})"
        for title in (TITLE_LEVELS, TITLE_CODES, TITLE_TICKETS, TITLE_API, TITLE_LIMITS)
    )
    return [
        "# Codes de suivi DHL",
        "",
        "> Page générée par `dhl_codes_doc.py` depuis le code du tracker : ne pas la modifier à la main. "
        "Après un changement des codes d'alerte, relancer `python dhl_codes_doc.py` ; "
        "`tests/test_dhl_codes_doc.py` échoue tant que la page n'est pas à jour.",
        "",
        "Ce que DHL renvoie pour chacun de nos envois Express, comment lire ses codes, "
        "et lesquels ouvrent un ticket dans le Helpdesk.",
        "",
        "Sources : API DHL Shipment Tracking – Unified v1.5.8 (`API/track_v1.5.8.yaml`), "
        "liste officielle des codes Express DHL (`API/status_1.csv`), réponses réelles de nos envois "
        "(octobre 2026). Tous nos transporteurs DHL dans Odoo sont des produits Express.",
        "",
        f"**Sommaire :** {contents}",
    ]


def _levels() -> list:
    return [
        f"## {TITLE_LEVELS}",
        "",
        "Chaque événement DHL porte trois informations superposées. Exemple, avec un numéro fictif : "
        f"l'envoi `{EXAMPLE_TRACKING_NUMBER}` en EXPRESS 12:00, de Bruxelles vers l'Ohio, "
        "le 2026-10-06 à 05:51 (−04:00) à CINCINNATI HUB, Ohio, USA.",
        "",
        *_table(("Niveau", "Champ API", "Exemple", "À quoi il sert"), (
            ("1 · Statut global", "`status.statusCode`", "`transit`",
             f"{len(GLOBAL_STATUSES)} valeurs communes à tous les services DHL. "
             "**Seule information utilisée pour décider qu'un envoi est livré.**"),
            ("2 · Code Express", "`events[].status`", "`DF`",
             f"Depart facility. {EXPRESS_EVENT_CODE_COUNT} codes de 2 lettres qui disent ce qui s'est passé, "
             "et donc s'il faut agir."),
            ("3 · Description", "`description`", "Shipment has departed from a DHL facility CINCINNATI HUB - USA",
             "Texte libre, en français avec `language=fr` : « L'envoi a quitté un site DHL ». "
             "Jamais utilisé pour décider : « The shipment could not be delivered » contient aussi "
             "le mot « delivered »."),
        )),
        "",
        f"### Les {len(GLOBAL_STATUSES)} statuts globaux",
        "",
        *_table(("Statut", "Signification"), ((f"`{status}`", meaning) for status, meaning in GLOBAL_STATUSES)),
        "",
        f"### Les {len(NUMERIC_STATUSES)} codes numériques Express",
        "",
        "Ils figurent sur le statut global de l'envoi, pas sur les événements.",
        "",
        *_table(("Code", "Statut global"), ((f"`{code}`", f"`{status}`") for code, status in NUMERIC_STATUSES)),
    ]


def _event_row(event: EventCode) -> tuple:
    notes = "".join((
        " _(sans date estimée)_" if event.no_estimated_delivery else "",
        " _(depuis juin 2024)_" if event.new_since_june_2024 else "",
    ))
    return f"`{event.code}`", event.dhl_label, event.meaning + notes, alert_label(event.code)


def _codes_section() -> list:
    skipped = _codes(sorted(DEFAULT_TICKET_SKIP_CODES))
    ticketed = [code for code in ALERT_CODES if code not in DEFAULT_TICKET_SKIP_CODES]
    lines = [
        f"## {TITLE_CODES}",
        "",
        f"{len(ALERT_CODES)} codes signalent un problème : {len(ticketed)} ouvrent un ticket, et {skipped} "
        "sont seulement signalés sur Mattermost, car DHL les règle le plus souvent seul "
        "(réglage par défaut, modifiable avec `HELPDESK_SKIP_CODES`). "
        "Les autres décrivent le parcours normal du colis.",
        "",
        "_sans date estimée_ : DHL ne donne pas de date de livraison estimée avec cet événement "
        "(« no EDD » dans sa liste).",
    ]
    for group, title in GROUP_TITLES.items():
        events = [event for event in EVENT_CODES if event.group == group]
        lines += [
            "",
            f"### {title} · {len(events)} codes",
            "",
            *_table(("Code", "Libellé DHL", "Ce que ça veut dire", "Alerte"), map(_event_row, events)),
        ]
    return [
        *lines,
        "",
        "Vu sur nos envois mais absent de la liste DHL : `SD`, « Shipment information received ». "
        "DHL prévient que de nouveaux codes peuvent apparaître. Un code inconnu n'ouvre jamais de ticket.",
    ]


def _priority_rows() -> list:
    rows = [
        (
            GROUP_TITLES[family.key],
            PRIORITY_LABELS[family.priority],
            f"`{family.color}`",
            _codes(code for code, alert in ALERT_CODES.items()
                   if alert.family.key == family.key and code not in DEFAULT_TICKET_SKIP_CODES),
        )
        for family in FAMILIES.values()
    ]
    return [*rows, ("Sans ticket", "Mattermost seulement", NO_ALERT, _codes(sorted(DEFAULT_TICKET_SKIP_CODES)))]


def _tickets_section() -> list:
    max_age_days = ALERT_MAX_EVENT_AGE.days
    return [
        f"## {TITLE_TICKETS}",
        "",
        "Il n'y a pas de ticket par envoi. Le tracker alerte seulement quand DHL signale un problème "
        f"et que les quatre conditions sont réunies. {_codes(sorted(DEFAULT_TICKET_SKIP_CODES))} "
        "sont signalés sur Mattermost sans ticket.",
        "",
        "1. **Dernier événement DHL** : le tracker lit l'événement le plus récent de l'envoi.",
        f"2. **Code de problème** : son code fait partie des {len(ALERT_CODES)} codes de problème.",
        "3. **Pas encore signalé** : le statut Odoo de l'envoi ne porte pas déjà ce code.",
        f"4. **Récent** : l'événement date de moins de {max_age_days} jours. "
        "Les vieilles histoires sont notées dans Odoo, sans ticket.",
        "",
        "### Ticket Helpdesk",
        "",
        f"- Équipe {DEFAULT_HELPDESK_TEAM}, tag{'s' if len(DEFAULT_HELPDESK_TAGS) > 1 else ''} "
        f"{', '.join(DEFAULT_HELPDESK_TAGS)}.",
        "- Client et commande de vente liés, avec les liens vers la livraison et le suivi DHL.",
        "- Un nouveau problème sur un envoi dont le ticket est encore ouvert devient une note interne "
        "sur ce ticket, pas un second ticket.",
        "- Le tracker n'écrit que des notes internes. Le client ne reçoit aucun e-mail.",
        "",
        "### Message Mattermost",
        "",
        "Un message par alerte, avec la couleur de la famille et le lien vers le ticket. "
        "Actif dès que l'adresse du canal est configurée (`ALERT_WEBHOOK_URL`).",
        "",
        "### Statut dans Odoo",
        "",
        "Le champ statut de la livraison garde le dernier code DHL, ce qui évite de signaler "
        "deux fois le même problème :",
        "",
        "```",
        format_status_text(*STATUS_EXAMPLE),
        "```",
        "",
        "### Priorités et couleurs",
        "",
        *_table(("Famille", "Priorité du ticket", "Couleur Mattermost", "Codes"), _priority_rows()),
    ]


def _api_section() -> list:
    return [
        f"## {TITLE_API}",
        "",
        "Vérifié sur de vrais envois Express en transit et livrés.",
        "",
        *_table(("Donnée", "Champ API", "Disponible", "Remarque"),
                ((label, f"`{path}`", available, note) for label, path, available, note in API_FIELDS)),
    ]


def _limits_section() -> list:
    expired_label = TRACKING_EXPIRED_PREFIX.removeprefix("Status: ")
    return [
        f"## {TITLE_LIMITS}",
        "",
        f"- **Quota DHL** : {DHL_DAILY_QUOTA} appels par jour et au plus un appel toutes les "
        f"{MIN_SECONDS_BETWEEN_CALLS} secondes, partagés par tous les scripts. Le contrôle horaire coûte "
        f"{HOURLY_CALLS_PER_DAY} appels par jour et par envoi suivi.",
        f"- **Fenêtre de {TRACKING_WINDOW_DAYS} jours** : le contrôle automatique ne suit que les livraisons "
        f"validées dans les {TRACKING_WINDOW_DAYS} derniers jours. Un envoi plus ancien ne se suit plus "
        "qu'à la main, par son numéro, avec `shiptracker.py`.",
        "- **Plusieurs numéros dans un seul champ** : Odoo ajoute « ,numéro » à la référence d'un picking "
        "chaque fois qu'une étiquette DHL est créée pour lui ou pour un picking lié (étiquette régénérée, "
        "retour). Ce sont des envois distincts, pas des colis : un envoi Express de plusieurs colis garde "
        "un seul numéro. Le tracker suit chaque numéro, et c'est l'envoi le plus récent qui décide : "
        "un retour encore en route garde le picking suivi, une étiquette jamais utilisée ne bloque rien.",
        f"- **Suivi expiré** : un picking non livré validé depuis plus de {TRACKING_WINDOW_DAYS} jours passe "
        f"en « {expired_label} » et n'est plus suivi. Au-delà, DHL ne connaît plus le numéro, ou renvoie "
        "les événements d'un autre envoi plus récent : DHL réutilise ses numéros Express.",
    ]


def render() -> str:
    sections = (_intro(), _levels(), _codes_section(), _tickets_section(), _api_section(), _limits_section())
    lines = [line for section in sections for line in (*section, "")]
    footer = (
        "---\n\n_Les colonnes Alerte, les priorités, les couleurs et les limites sont générées depuis "
        "le code du tracker : elles ne peuvent pas diverger de ce qu'il fait._\n"
    )
    return "\n".join(lines) + "\n" + footer


def main(output: Path = OUTPUT_PATH) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render(), encoding="utf-8")
    print(f"wrote {output}")


if __name__ == "__main__":  # pragma: no cover
    main()
