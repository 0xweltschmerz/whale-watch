"""
Interprétation des mouvements de baleines.

Deux niveaux :
  - interpreter()       : lecture d'une alerte isolée, replacée dans la tendance des 24 h / 7 jours
  - construire_recap()  : bilan quotidien des flux exchanges, avec une lecture globale

Tout ceci est une aide à la lecture du marché, pas un conseil d'investissement.
"""
from datetime import timedelta
from html import escape

INCONNU = "wallet inconnu"
PLUSIEURS = "plusieurs wallets"

# Types qui comptent comme une sortie ou une entrée sur les exchanges
SORTIES = {"retrait"}
ENTREES = {"dépôt", "gouv-depot"}
DIRECTION = {"retrait": 1, "dépôt": -1, "gouv-depot": -1, "gouvernement": -1}

SIGNAUX = {
    "retrait": "🟢 Sortie d'exchange",
    "dépôt": "🔴 Entrée sur exchange",
    "gouv-depot": "🏛️🔴 Un État envoie des BTC vers un exchange",
    "gouvernement": "🏛️ Un portefeuille d'État bouge",
    "inter-exchange": "🔁 Transfert entre exchanges",
    "interne": "↔️ Mouvement interne d'un exchange",
    "transfert": "⚪ Transfert entre wallets non identifiés",
}


# ---------------------------------------------------------------- formats
def fmt_nombre(x):
    return f"{x:,.0f}".replace(",", " ")


def fmt_signe(x):
    if round(x) == 0:
        return "0"
    return ("+" if x > 0 else "−") + fmt_nombre(abs(x))


# ---------------------------------------------------------------- calculs sur l'historique
def depuis(hist, maintenant, heures):
    limite = maintenant - timedelta(hours=heures)
    return [r for r in hist if r["date"] >= limite]


def flux_net(lignes):
    """Sorties d'exchanges moins entrées (en BTC). Positif = les BTC quittent les plateformes."""
    sorties = sum(r["montant"] for r in lignes if r["type"] in SORTIES)
    entrees = sum(r["montant"] for r in lignes if r["type"] in ENTREES)
    return sorties - entrees, sorties, entrees


def anciennete_heures(hist, maintenant):
    if not hist:
        return 0
    return (maintenant - min(r["date"] for r in hist)).total_seconds() / 3600


# ---------------------------------------------------------------- lecture d'une alerte
def sens_du_mouvement(a):
    t, src, dst = a["type"], escape(a["source"]), escape(a["dest"])
    if t == "retrait":
        txt = (f"Des BTC quittent {src} pour un portefeuille privé. C'est le schéma typique d'un achat "
               "mis ensuite en stockage longue durée : autant de BTC en moins disponibles à la vente.")
        if a["dest"] not in (INCONNU, PLUSIEURS):
            txt += f" Destinataire identifié : {dst}."
        else:
            txt += (" À nuancer : ce peut aussi être l'exchange qui déplace ses propres réserves "
                    "vers une adresse pas encore étiquetée.")
        return txt
    if t == "dépôt":
        return (f"Des BTC arrivent sur {dst}. On dépose généralement pour vendre, ou pour servir de garantie "
                "à du trading à effet de levier. Un dépôt n'est pas une vente, mais les gros dépôts "
                "précèdent souvent un regain de volatilité.")
    if t == "gouv-depot":
        return (f"{src} envoie des BTC saisis vers {dst}. Les États revendent généralement ces fonds : "
                "c'est l'un des signaux de vente les plus directs.")
    if t == "gouvernement":
        return (f"{src} déplace des BTC saisis. Souvent une étape avant une vente (enchères, desk OTC, "
                "exchange) : à surveiller dans les prochains blocs.")
    if t == "inter-exchange":
        return (f"Transfert de {src} vers {dst} : arbitrage, gestion de liquidité ou desk OTC. "
                "Peu d'impact directionnel.")
    if t == "interne":
        return f"{src} réorganise ses propres portefeuilles. Aucun impact sur le marché."
    if a["dest"] == PLUSIEURS:
        return ("Distribution vers de nombreux portefeuilles : souvent un paiement groupé, par exemple un "
                "exchange non étiqueté qui traite des retraits clients. Signal neutre.")
    return ("Mouvement entre portefeuilles non identifiés : réorganisation interne, vente de gré à gré (OTC) "
            "ou exchange absent de labels.json. Signal neutre tant que les parties sont inconnues.")


def taille_relative(a, hist_7j):
    montants = [r["montant"] for r in hist_7j]
    if len(montants) >= 10:
        p = round(100 * sum(m < a["montant"] for m in montants) / len(montants))
        if p == 100:
            return "Taille : le plus gros mouvement des 7 derniers jours."
        if p >= 50:
            return f"Taille : plus gros que {p} % des alertes des 7 derniers jours."
        return "Taille : dans la moitié basse des alertes de la semaine."
    m = a["montant"]
    if m >= 5000:
        return "Taille : mouvement exceptionnel."
    if m >= 1000:
        return "Taille : très gros mouvement."
    if m >= 500:
        return "Taille : gros mouvement."
    return "Taille : mouvement de baleine classique."


def interpreter(a, hist, maintenant):
    """Texte d'interprétation d'une alerte. `hist` = alertes déjà enregistrées (sans celle-ci)."""
    h24, h7 = depuis(hist, maintenant, 24), depuis(hist, maintenant, 24 * 7)
    d = DIRECTION.get(a["type"], 0)
    signe = 1 if a["type"] in SORTIES else -1 if a["type"] in ENTREES else 0
    net24 = flux_net(h24)[0] + signe * a["montant"]
    net7 = flux_net(h7)[0] + signe * a["montant"]

    lignes = ["<b>📊 Interprétation</b>", sens_du_mouvement(a), "", taille_relative(a, h7)]

    meme_type = sum(1 for r in h24 if r["type"] == a["type"]) + 1
    if d != 0 and meme_type >= 2:
        nom = {"retrait": "sortie d'exchange", "dépôt": "entrée sur exchange"}.get(a["type"], "mouvement de ce type")
        lignes.append(f"Répétition : {meme_type}e {nom} en 24 h.")

    assez_d_historique = anciennete_heures(hist, maintenant) >= 24
    lignes.append(f"Flux net exchanges : 24 h {fmt_signe(net24)} BTC · 7 j {fmt_signe(net7)} BTC")

    force = 1
    if d != 0:
        if not assez_d_historique:
            lignes.append("Tendance : historique encore trop court (moins de 24 h) pour conclure.")
        elif net7 * d > 0:
            force += 1
            lignes.append("Tendance : ce mouvement va dans le sens de la semaine, le signal se renforce."
                          if d > 0 else
                          "Tendance : s'ajoute à une semaine déjà dominée par les entrées, la pression vendeuse potentielle s'accumule.")
        else:
            lignes.append("Tendance : va à contre-courant de la semaine, signal isolé à confirmer.")
        if a["montant"] >= 1000:
            force += 1
        if a["type"] == "gouv-depot":
            force += 1

    if d == 0:
        lecture = "⚪ neutre"
    else:
        niveau = {1: "faible", 2: "modéré"}.get(force, "fort")
        lecture = f"{'🟢 haussière' if d > 0 else '🔴 baissière'} · signal {niveau}"
    lignes.append(f"<b>Lecture : {lecture}</b> (indicatif)")
    return "\n".join(lignes)


# ---------------------------------------------------------------- récapitulatif quotidien
def construire_recap(hist, prix, maintenant):
    h24, h7 = depuis(hist, maintenant, 24), depuis(hist, maintenant, 24 * 7)
    net24, s24, e24 = flux_net(h24)
    net7 = flux_net(h7)[0]
    jours = max(1.0, min(7.0, anciennete_heures(hist, maintenant) / 24))
    moy = net7 / jours
    n = lambda types: sum(1 for r in h24 if r["type"] in types)

    lignes = ["<b>🐋 Récap baleines — dernières 24 h</b>", ""]
    if prix:
        lignes.append(f"Prix BTC : {fmt_nombre(prix)} $")
    lignes += [
        f"Sorties d'exchanges : {fmt_nombre(s24)} BTC ({n(SORTIES)} tx)",
        f"Entrées sur exchanges : {fmt_nombre(e24)} BTC ({n(ENTREES)} tx)",
        f"<b>Flux net 24 h : {fmt_signe(net24)} BTC</b>",
        f"Flux net 7 j : {fmt_signe(net7)} BTC (moyenne {fmt_signe(moy)} BTC/jour)",
        f"Alertes au total : {len(h24)}",
    ]
    etats = [r for r in h24 if r["type"] in ("gouvernement", "gouv-depot")]
    if etats:
        lignes.append(f"🏛️ Mouvements d'États : {len(etats)} ({fmt_nombre(sum(r['montant'] for r in etats))} BTC)")
    if h24:
        top = max(h24, key=lambda r: r["montant"])
        lignes.append(f"Plus gros mouvement : {fmt_nombre(top['montant'])} BTC de {escape(top['source'])} "
                      f"vers {escape(top['dest'])}")

    lignes += ["", "<b>📊 Interprétation</b>"]
    if not h24:
        lignes.append("Aucun mouvement de baleine détecté sur 24 h : marché calme côté gros portefeuilles.")
        lecture = "⚪ neutre"
    elif abs(net24) < 50:
        lignes.append("Entrées et sorties s'équilibrent : pas de positionnement net des baleines aujourd'hui.")
        lecture = "⚪ neutre"
    elif net24 > 0 and net7 > 0:
        lignes.append("Accumulation : les baleines retirent leurs BTC des exchanges depuis plusieurs jours. "
                      "Moins d'offre sur les plateformes est historiquement un contexte plutôt favorable "
                      "à moyen terme, sans rien dire du timing.")
        lecture = "🟢 haussière" + (" · signal fort" if net24 > moy * 1.5 and anciennete_heures(hist, maintenant) >= 48 else "")
    elif net24 > 0:
        lignes.append("Retournement possible : la journée penche vers l'accumulation alors que la semaine "
                      "était plutôt vendeuse. À confirmer sur les prochains jours.")
        lecture = "🟢 haussière · signal faible"
    elif net7 < 0:
        lignes.append("Distribution : les dépôts sur exchanges dominent depuis plusieurs jours. Des baleines "
                      "se préparent peut-être à vendre ou à se couvrir. Contexte de prudence.")
        lecture = "🔴 baissière" + (" · signal fort" if net24 < moy * 1.5 and anciennete_heures(hist, maintenant) >= 48 else "")
    else:
        lignes.append("Prises de bénéfices possibles : journée vendeuse dans une semaine plutôt "
                      "orientée accumulation. Rien d'alarmant tant que ça ne dure pas.")
        lecture = "🔴 baissière · signal faible"
    if anciennete_heures(hist, maintenant) < 72:
        lignes.append("Historique encore court : la tendance 7 jours se fiabilise au fil des jours.")
    lignes += [f"<b>Lecture : {lecture}</b>", "",
               "<i>Indicateur on-chain, pas un conseil d'investissement. Seuls les flux des adresses "
               "connues (labels.json) sont comptés.</i>"]
    return "\n".join(lignes)
