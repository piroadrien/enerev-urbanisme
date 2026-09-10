"""
Templates des emails envoyes automatiquement au client (ou en interne) quand
le statut d'un dossier DP change -- contenu valide le 2026-09-10.

Le cas "Refusee" n'envoie PAS d'email au client (une decision defavorable
merite un contact personnalise, pas un email automatique) : une alerte
interne part a la place vers la boite urbanisme@enerev.fr pour que l'equipe
recontacte le client elle-meme.
"""

from gnau_email_parser import (
    STATUT_ACCORDEE,
    STATUT_COMPLETE,
    STATUT_ENVOYEE,
    STATUT_INCOMPLETE,
    STATUT_REFUSEE,
)

# Chaque template : (objet, corps) avec placeholders format() sur les champs
# de la ligne de suivi DP (NumeroDP, Ville, DateDepot, DateEstimee, ...).

CLIENT_TEMPLATES = {
    STATUT_ENVOYEE: (
        "Votre dossier de déclaration préalable a été déposé",
        "Bonjour,\n\n"
        "Votre dossier de déclaration préalable (n° {NumeroDP}) a bien été déposé "
        "auprès de la mairie de {Ville} le {DateDepot}. Le délai d'instruction est "
        "d'environ 1 mois.\n\n"
        "Nous vous tiendrons informé(e) de la suite.\n\n"
        "Cordialement,\nL'équipe ENEREV",
    ),
    STATUT_COMPLETE: (
        "Votre dossier est complet",
        "Bonjour,\n\n"
        "Bonne nouvelle : votre dossier (n° {NumeroDP}) a été déclaré complet par "
        "la mairie de {Ville}. La décision est attendue avant le {DateEstimee}.\n\n"
        "Cordialement,\nL'équipe ENEREV",
    ),
    STATUT_INCOMPLETE: (
        "Pièces complémentaires nécessaires pour votre dossier",
        "Bonjour,\n\n"
        "La mairie de {Ville} nous informe qu'il manque des pièces à votre dossier "
        "(n° {NumeroDP}). Nous nous en occupons et revenons vers vous rapidement "
        "si votre contribution est nécessaire.\n\n"
        "Cordialement,\nL'équipe ENEREV",
    ),
    STATUT_ACCORDEE: (
        "Votre déclaration préalable est accordée !",
        "Bonjour,\n\n"
        "Excellente nouvelle : votre dossier (n° {NumeroDP}) a reçu une décision "
        "favorable de la mairie de {Ville}. Nous restons à votre disposition pour "
        "la suite du projet.\n\n"
        "Cordialement,\nL'équipe ENEREV",
    ),
}

# Alerte interne (pas de template client) pour le cas Refusee.
INTERNAL_REFUS_TEMPLATE = (
    "Décision défavorable sur un dossier DP — à traiter",
    "Décision défavorable reçue sur le dossier {NumeroDP} ({Client}, {Ville}).\n\n"
    "Aucun email n'a été envoyé au client automatiquement -- merci de le "
    "recontacter directement.",
)


def render(template: tuple[str, str], fields: dict) -> tuple[str, str]:
    subject, body = template
    return subject.format(**fields), body.format(**fields)
