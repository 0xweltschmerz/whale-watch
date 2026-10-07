#!/usr/bin/env python3
"""
Whale Watch — alertes Telegram automatiques sur les gros mouvements Bitcoin, avec interprétation.

Modes :
  python whale_watch.py --une-fois        analyse les nouveaux blocs puis s'arrête (GitHub Actions)
  python whale_watch.py                   tourne en continu (serveur, Raspberry Pi)
  python whale_watch.py --test-telegram   envoie un message de test
  python whale_watch.py --bloc 865000     analyse un bloc précis, affichage seul (débogage)
  python whale_watch.py --recap           envoie le récapitulatif quotidien maintenant
"""
import argparse
import csv
import json
import logging
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from interpretation import INCONNU, PLUSIEURS, SIGNAUX, construire_recap, fmt_nombre, interpreter

# ---------------------------------------------------------------- configuration
APIS = [u.strip() for u in os.getenv("MEMPOOL_API", "https://mempool.space/api,https://blockstream.info/api").split(",")]
SEUIL_BTC = float(os.getenv("SEUIL_BTC", "100"))
INTERVALLE = int(os.getenv("INTERVALLE_SEC", "60"))
IGNORER_INTERNES = os.getenv("IGNORER_INTERNES", "1") == "1"
MAX_RATTRAPAGE = int(os.getenv("MAX_RATTRAPAGE", "12"))      # blocs rattrapés au maximum après une pause
RECAP_HEURE = int(os.getenv("RECAP_HEURE", "8"))             # heure du récap quotidien (heure locale)
FUSEAU = ZoneInfo(os.getenv("FUSEAU", "Europe/Paris"))
PANNE_MINUTES = 30                                           # délai avant de signaler une panne
TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()

BASE = Path(__file__).resolve().parent
LABELS_FILE = BASE / "labels.json"
STATE_FILE = BASE / "state.json"
CSV_FILE = BASE / "alertes.csv"
COLONNES = ["date_utc", "bloc", "txid", "montant_btc", "prix_usd", "source", "dest", "type"]
SAT = 100_000_000

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("whale")
SESSION = requests.Session()
SESSION.headers["User-Agent"] = "whale-watch/2.0"


def maintenant():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- accès aux données
def api_get(path, as_json=True):
    """GET avec réessais, en basculant sur l'API de secours si la principale ne répond pas."""
    derniere_erreur = None
    for essai in range(4):
        for base in APIS:
            try:
                r = SESSION.get(f"{base}{path}", timeout=20)
                if r.status_code == 429:
                    derniere_erreur = "limite de requêtes"
                    continue
                r.raise_for_status()
                return r.json() if as_json else r.text.strip()
            except (requests.RequestException, ValueError) as e:
                derniere_erreur = e
        time.sleep(4 * (essai + 1))
    raise RuntimeError(f"API injoignable ({path}) : {derniere_erreur}")


def prix_btc():
    for url, extraire in (("https://mempool.space/api/v1/prices", lambda j: j["USD"]),
                          ("https://api.coinbase.com/v2/prices/BTC-USD/spot", lambda j: j["data"]["amount"])):
        try:
            return float(extraire(SESSION.get(url, timeout=15).json()))
        except Exception:
            continue
    return None


def hauteur_tip():
    return int(api_get("/blocks/tip/height", as_json=False))


def txs_du_bloc(hauteur):
    h = api_get(f"/block-height/{hauteur}", as_json=False)
    info = api_get(f"/block/{h}")
    txs = []
    for start in range(0, info["tx_count"], 25):
        txs.extend(api_get(f"/block/{h}/txs/{start}"))
        time.sleep(0.1)
    return txs


# ---------------------------------------------------------------- étiquettes
def charger_labels():
    """Renvoie {adresse: {"nom": ..., "type": exchange|gouvernement|entite}}."""
    if not LABELS_FILE.exists():
        return {}
    brut = json.loads(LABELS_FILE.read_text(encoding="utf-8"))
    labels = {}
    for adr, v in brut.items():
        if adr.startswith("_"):
            continue
        labels[adr] = v if isinstance(v, dict) else {"nom": v, "type": "exchange"}
    return labels


# ---------------------------------------------------------------- analyse d'une transaction
def analyser_tx(tx, labels, seuil=SEUIL_BTC):
    vin = tx.get("vin", [])
    if not vin or vin[0].get("is_coinbase"):
        return None

    adr_entree = {i["prevout"].get("scriptpubkey_address") for i in vin if i.get("prevout")} - {None}
    sorties = [(o["scriptpubkey_address"], o["value"]) for o in tx.get("vout", []) if o.get("scriptpubkey_address")]
    vraies_sorties = [(a, v) for a, v in sorties if a not in adr_entree]   # sans le rendu de monnaie
    montant = sum(v for _, v in vraies_sorties) / SAT
    if montant < seuil:
        return None

    noms_in = Counter(labels[a]["nom"] for a in adr_entree if a in labels)
    source = noms_in.most_common(1)[0][0] if noms_in else INCONNU
    s_type = next((labels[a]["type"] for a in adr_entree if a in labels and labels[a]["nom"] == source), None)

    par_entite = defaultdict(int)
    for a, v in vraies_sorties:
        par_entite[labels[a]["nom"] if a in labels else a] += v
    cle = max(par_entite.items(), key=lambda kv: kv[1])[0]
    lab_dest = next((l for l in labels.values() if l["nom"] == cle), None)
    if lab_dest:
        dest, d_type = cle, lab_dest["type"]
    else:
        dest, d_type = (PLUSIEURS if len(vraies_sorties) > 5 else INCONNU), None

    if s_type == "gouvernement":
        type_ = "gouv-depot" if d_type == "exchange" else "gouvernement"
    elif s_type == "exchange" and d_type == "exchange":
        type_ = "interne" if source == dest else "inter-exchange"
    elif s_type == "exchange":
        type_ = "retrait"
    elif d_type == "exchange":
        type_ = "dépôt"
    else:
        type_ = "transfert"

    return {"txid": tx["txid"], "montant": montant, "source": source, "dest": dest, "type": type_}


# ---------------------------------------------------------------- historique (CSV)
def lire_historique(jours=8):
    if not CSV_FILE.exists():
        return []
    limite = maintenant() - timedelta(days=jours)
    hist = []
    with CSV_FILE.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            d = datetime.fromisoformat(r["date_utc"])
            if d >= limite:
                hist.append({"date": d, "montant": float(r["montant_btc"]), "type": r["type"],
                             "source": r["source"], "dest": r["dest"]})
    return hist


def enregistrer(a, hauteur, prix, quand):
    nouveau = not CSV_FILE.exists()
    with CSV_FILE.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if nouveau:
            w.writerow(COLONNES)
        w.writerow([quand.isoformat(timespec="seconds"), hauteur, a["txid"], round(a["montant"], 8),
                    prix or "", a["source"], a["dest"], a["type"]])


def purger_historique(jours=30):
    """Garde le fichier léger : supprime les alertes de plus de 30 jours."""
    if not CSV_FILE.exists():
        return
    limite = maintenant() - timedelta(days=jours)
    with CSV_FILE.open(encoding="utf-8") as f:
        lignes = [r for r in csv.DictReader(f) if datetime.fromisoformat(r["date_utc"]) >= limite]
    with CSV_FILE.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLONNES)
        w.writeheader()
        w.writerows(lignes)


# ---------------------------------------------------------------- Telegram
def notifier(texte):
    print("\n" + texte + "\n")
    if not (TG_TOKEN and TG_CHAT):
        return
    for essai in range(3):
        try:
            r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                              data={"chat_id": TG_CHAT, "text": texte, "parse_mode": "HTML",
                                    "disable_web_page_preview": "true"}, timeout=15)
            if r.status_code == 429:
                time.sleep(r.json().get("parameters", {}).get("retry_after", 5))
                continue
            if not r.ok:
                log.warning("Telegram a refusé le message : %s", r.text[:200])
            time.sleep(1)  # Telegram limite le débit des messages
            return
        except requests.RequestException as e:
            log.warning("Envoi Telegram impossible : %s", e)
            time.sleep(3)


def sirenes(montant):
    return "🚨" * max(1, sum(montant >= p for p in (100, 500, 1_000, 5_000, 10_000)))


def nom_affiche(n):
    return n if n in (INCONNU, PLUSIEURS) else f"<b>{escape(n)}</b>"


def formater_alerte(a, prix):
    usd = ""
    if prix:
        usd = " (" + f"{a['montant'] * prix / 1e6:,.1f}".replace(",", " ").replace(".", ",") + " M$)"
    return (f"{sirenes(a['montant'])} <b>{fmt_nombre(a['montant'])} BTC</b>{usd}\n"
            f"de {nom_affiche(a['source'])} → {nom_affiche(a['dest'])}\n"
            f"{SIGNAUX[a['type']]}")


def message_complet(a, prix, hist, quand):
    return (f"{formater_alerte(a, prix)}\n\n{interpreter(a, hist, quand)}\n\n"
            f"<a href=\"https://mempool.space/tx/{a['txid']}\">Voir la transaction</a>")


# ---------------------------------------------------------------- traitement
def traiter_bloc(hauteur, labels, enregistrer_alertes=True):
    txs = txs_du_bloc(hauteur)
    prix = prix_btc()
    hist = lire_historique()
    n = 0
    for tx in txs:
        a = analyser_tx(tx, labels)
        if not a or (IGNORER_INTERNES and a["type"] == "interne"):
            continue
        quand = maintenant()
        notifier(message_complet(a, prix, hist, quand))
        if enregistrer_alertes:
            enregistrer(a, hauteur, prix, quand)
        hist.append({"date": quand, "montant": a["montant"], "type": a["type"],
                     "source": a["source"], "dest": a["dest"]})
        n += 1
    log.info("Bloc %s : %s transactions, %s alerte(s)", hauteur, len(txs), n)


def lire_etat():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def sauver_etat(etat):
    STATE_FILE.write_text(json.dumps(etat, indent=2), encoding="utf-8")


def envoyer_recap():
    notifier(construire_recap(lire_historique(), prix_btc(), maintenant()))


def recap_si_besoin(etat):
    local = datetime.now(FUSEAU)
    jour = local.date().isoformat()
    if local.hour >= RECAP_HEURE and etat.get("dernier_recap") != jour:
        envoyer_recap()
        etat["dernier_recap"] = jour
        purger_historique()


def une_passe(labels, etat):
    tip = hauteur_tip()
    if etat.get("derniere_hauteur") is None:
        etat["derniere_hauteur"] = tip - 1
        etat["dernier_recap"] = datetime.now(FUSEAU).date().isoformat()  # pas de récap vide au démarrage
        notifier(f"✅ <b>Whale Watch est en ligne</b>\nSurveillance à partir du bloc {tip}, "
                 f"seuil {SEUIL_BTC:g} BTC, {len(labels)} adresses étiquetées.\n"
                 f"Récap quotidien à {RECAP_HEURE} h.")
    for h in range(max(etat["derniere_hauteur"] + 1, tip - MAX_RATTRAPAGE + 1), tip + 1):
        traiter_bloc(h, labels)
        etat["derniere_hauteur"] = h
        sauver_etat(etat)
    recap_si_besoin(etat)


def executer_passe(labels):
    """Une passe protégée : en cas de panne prolongée, prévient une seule fois sur Telegram."""
    etat = lire_etat()
    try:
        une_passe(labels, etat)
        if etat.get("panne_signalee"):
            notifier("✅ Whale Watch fonctionne de nouveau normalement.")
        etat["panne_depuis"], etat["panne_signalee"] = None, False
    except Exception as e:
        log.exception("Échec de la passe")
        debut = etat.get("panne_depuis") or maintenant().isoformat(timespec="seconds")
        etat["panne_depuis"] = debut
        duree = maintenant() - datetime.fromisoformat(debut)
        if not etat.get("panne_signalee") and duree >= timedelta(minutes=PANNE_MINUTES):
            notifier(f"⚠️ <b>Whale Watch en difficulté</b> depuis {int(duree.total_seconds() // 60)} min.\n"
                     f"Erreur : {escape(str(e))[:300]}\nIl réessaie automatiquement.")
            etat["panne_signalee"] = True
    sauver_etat(etat)


def surveiller(labels):
    log.info("Surveillance continue — seuil %s BTC, %s adresses étiquetées, Telegram %s",
             SEUIL_BTC, len(labels), "activé" if TG_TOKEN and TG_CHAT else "NON configuré")
    while True:
        executer_passe(labels)
        time.sleep(INTERVALLE)


# ---------------------------------------------------------------- point d'entrée
if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Alertes baleines Bitcoin")
    p.add_argument("--une-fois", action="store_true", help="analyser les nouveaux blocs puis s'arrêter")
    p.add_argument("--bloc", type=int, help="analyser un bloc précis (affichage seul)")
    p.add_argument("--recap", action="store_true", help="envoyer le récapitulatif maintenant")
    p.add_argument("--test-telegram", action="store_true", help="envoyer un message de test")
    args = p.parse_args()
    labels = charger_labels()
    try:
        if args.test_telegram:
            if not (TG_TOKEN and TG_CHAT):
                sys.exit("TELEGRAM_TOKEN et TELEGRAM_CHAT_ID ne sont pas définis.")
            notifier("👋 Test réussi : Whale Watch peut t'envoyer des messages.")
        elif args.bloc:
            TG_TOKEN = ""  # débogage : affichage seul
            traiter_bloc(args.bloc, labels, enregistrer_alertes=False)
        elif args.recap:
            envoyer_recap()
        elif args.une_fois:
            executer_passe(labels)
        else:
            surveiller(labels)
    except KeyboardInterrupt:
        sys.exit(0)
