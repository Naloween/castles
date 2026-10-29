import csv
import json
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


# ---------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------

BASE_URL = "https://www.bellesdemeures.com"
LISTING_URL = (
    BASE_URL
    + "/vente/france/chateau-luxe/tt-2-tb-13-pl-73/"
)

OUTPUT_CSV = Path("chateaux.csv")
FAILED_LOG = Path("annonces_a_verifier.txt")

# Limite de sécurité pour une première exécution.
# Augmente-la uniquement après avoir vérifié les résultats.
MAX_PAGES = 100

# Pause entre les requêtes : ne pas surcharger le site.
DELAY_SECONDS = 2.5

COLUMNS = [
    "id_annonce",
    "titre",
    "prix_achat",
    "surface_m2",
    "nb_pieces",
    "nb_chambres",
    "description",
    "taxe_fonciere_annonce",
    "charges_annuelles_annonce",
    "lien_annonce",
    "tags",
]

session = requests.Session()
session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (compatible; "
        "PersonalPropertyResearch/1.0)"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9",
})


# ---------------------------------------------------------
# OUTILS DE NORMALISATION
# ---------------------------------------------------------

def clean_text(value):
    if value is None:
        return ""

    if isinstance(value, (int, float)):
        return str(value)

    return re.sub(r"\s+", " ", str(value)).strip()


def to_number(value):
    """Convertit une valeur en nombre, sans inventer de valeur."""
    if value is None:
        return ""

    if isinstance(value, (int, float)):
        return value

    text = clean_text(value)
    text = text.replace("\u202f", "").replace("\xa0", "")
    text = text.replace("€", "").replace("m²", "")
    text = text.replace(" ", "")

    # Formats français : 1 234,56
    if "," in text:
        text = text.replace(".", "").replace(",", ".")

    match = re.search(r"-?\d+(?:\.\d+)?", text)

    if not match:
        return ""

    number = float(match.group())

    return int(number) if number.is_integer() else number


def extract_id(url):
    match = re.search(r"/(\d+)/?(?:\?.*)?$", urlparse(url).path)
    return match.group(1) if match else ""


def extract_tags(description):
    text = clean_text(description).lower()
    tags = []

    categories = {
        "mer": ["plage", "océan", "littoral"],
        "montagne": [
            "montagne", "pyrénées", "alpes", "altitude"
        ],
        "campagne": ["campagne", "prairie", "rural"],
    }

    for tag, keywords in categories.items():
        if any(word in text for word in keywords):
            tags.append(tag)

    return ";".join(tags)


def get_json_ld(soup):
    """Récupère les données JSON-LD présentes dans la page."""
    objects = []

    for script in soup.select(
        'script[type="application/ld+json"]'
    ):
        raw = script.string or script.get_text()

        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue

        if isinstance(data, list):
            objects.extend(data)
        elif isinstance(data, dict):
            objects.append(data)

    # Développe les graphes Schema.org.
    flattened = []

    for obj in objects:
        if isinstance(obj, dict):
            flattened.append(obj)

            graph = obj.get("@graph", [])

            if isinstance(graph, list):
                flattened.extend(
                    item for item in graph
                    if isinstance(item, dict)
                )

    return flattened


def first_value(objects, keys):
    for obj in objects:
        if not isinstance(obj, dict):
            continue

        for key in keys:
            value = obj.get(key)

            if value not in (None, "", [], {}):
                return value

    return ""


def find_offer(objects):
    for obj in objects:
        if not isinstance(obj, dict):
            continue

        offers = obj.get("offers", [])

        if isinstance(offers, dict):
            offers = [offers]

        for offer in offers:
            if isinstance(offer, dict):
                return offer

    return {}


def extract_description(soup, objects):
    description = first_value(
        objects,
        ["description"]
    )

    if description:
        return clean_text(description)

    # Repli sur les principaux blocs de description.
    selectors = [
        '[itemprop="description"]',
        '[class*="description"]',
        '[class*="Description"]',
    ]

    for selector in selectors:
        element = soup.select_one(selector)

        if element:
            text = clean_text(element.get_text(" ", strip=True))

            if len(text) > 100:
                return text

    return ""


def find_metric(text, patterns):
    for pattern in patterns:
        match = re.search(
            pattern,
            text,
            flags=re.IGNORECASE
        )

        if match:
            return to_number(match.group(1))

    return ""


# ---------------------------------------------------------
# EXTRACTION DES PAGES
# ---------------------------------------------------------

def fetch_page(url):
    """
    Télécharge une page accessible.

    Ne contourne pas les protections du site.
    Interrompt le programme en cas de refus ou de limitation.
    """
    response = session.get(
        url,
        timeout=25,
    )

    if response.status_code in (401, 403, 429):
        raise RuntimeError(
            f"Accès refusé ou limité : HTTP "
            f"{response.status_code} pour {url}. "
            "Arrêt de la collecte."
        )

    response.raise_for_status()

    return BeautifulSoup(
        response.text,
        "html.parser"
    )


def find_listing_links(soup):
    """
    Repère les liens de fiches individuelles.
    Le motif doit être vérifié si le site change ses URL.
    """
    links = set()

    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "")

        absolute_url = urljoin(BASE_URL, href)
        parsed = urlparse(absolute_url)

        if parsed.netloc not in (
            "www.bellesdemeures.com",
            "bellesdemeures.com",
        ):
            continue

        path = parsed.path

        if (
            "/annonces/vente/" in path
            and re.search(r"/\d+/?$", path)
        ):
            # Évite les paramètres de suivi.
            links.add(
                f"{parsed.scheme}://{parsed.netloc}{path}"
            )

    return sorted(links)


def extract_listing(url):
    soup = fetch_page(url)
    objects = get_json_ld(soup)
    offer = find_offer(objects)

    page_text = clean_text(
        soup.get_text(" ", strip=True)
    )

    title = first_value(
        objects,
        ["name", "headline"]
    )

    if not title:
        h1 = soup.find("h1")
        title = clean_text(h1.get_text()) if h1 else ""

    description = extract_description(soup, objects)

    price = (
        offer.get("price", "")
        if offer else ""
    )

    if not price:
        price = first_value(
            objects,
            ["price"]
        )

    # Les champs structurés peuvent varier selon le type
    # de données fourni par le site.
    surface = first_value(
        objects,
        ["floorSize", "livingArea"]
    )

    if isinstance(surface, dict):
        surface = surface.get("value", "")

    rooms = first_value(
        objects,
        ["numberOfRooms"]
    )

    bedrooms = first_value(
        objects,
        ["numberOfBedrooms"]
    )

    # Repli sur le texte de la page.
    if not price:
        price = find_metric(
            page_text,
            [
                r"(\d[\d\s\u202f.,]*)\s*€",
            ],
        )

    if not surface:
        surface = find_metric(
            page_text,
            [
                r"(\d[\d\s.,]*)\s*m²",
            ],
        )

    if not rooms:
        rooms = find_metric(
            page_text,
            [
                r"(\d+)\s*pièces?",
            ],
        )

    if not bedrooms:
        bedrooms = find_metric(
            page_text,
            [
                r"(\d+)\s*chambres?",
            ],
        )

    # On ne remplit pas taxe et charges à partir de
    # montants génériques qui pourraient être trompeurs.
    tax = ""
    charges = ""

    for label, target in [
        ("taxe foncière", "tax"),
        ("charges annuelles", "charges"),
    ]:
        pattern = (
            re.escape(label)
            + r"\s*[:\-]?\s*"
            + r"([\d\s\u202f.,]+)\s*€"
        )

        match = re.search(
            pattern,
            page_text,
            flags=re.IGNORECASE
        )

        if match:
            value = to_number(match.group(1))

            if target == "tax":
                tax = value
            else:
                charges = value

    record = {
        "id_annonce": extract_id(url),
        "titre": clean_text(title),
        "prix_achat": to_number(price),
        "surface_m2": to_number(surface),
        "nb_pieces": to_number(rooms),
        "nb_chambres": to_number(bedrooms),
        "description": description,
        "taxe_fonciere_annonce": tax,
        "charges_annuelles_annonce": charges,
        "lien_annonce": url,
        "tags": extract_tags(description),
    }

    return record


# ---------------------------------------------------------
# SAUVEGARDE PROGRESSIVE
# ---------------------------------------------------------

def read_existing_ids():
    if not OUTPUT_CSV.exists():
        return set()

    with OUTPUT_CSV.open(
        "r",
        encoding="utf-8-sig",
        newline=""
    ) as file:
        reader = csv.DictReader(file)

        return {
            row["id_annonce"]
            for row in reader
            if row.get("id_annonce")
        }


def append_record(record):
    exists = OUTPUT_CSV.exists()

    with OUTPUT_CSV.open(
        "a",
        encoding="utf-8",
        newline=""
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=COLUMNS,
            extrasaction="ignore",
        )

        if not exists or OUTPUT_CSV.stat().st_size == 0:
            writer.writeheader()

        writer.writerow(record)


def log_failed(url, error):
    with FAILED_LOG.open(
        "a",
        encoding="utf-8"
    ) as file:
        file.write(f"{url}\t{error}\n")


# ---------------------------------------------------------
# COLLECTE PAGINÉE
# ---------------------------------------------------------

def main():
    seen_ids = read_existing_ids()
    seen_urls = set()

    success = 0
    failures = 0

    print("Démarrage de la collecte.")
    print(f"Annonces déjà présentes : {len(seen_ids)}")

    try:
        for page_number in range(1, MAX_PAGES + 1):

            if page_number == 1:
                page_url = LISTING_URL
            else:
                page_url = (
                    LISTING_URL
                    + f"{page_number}/"
                )

            print(
                f"\nPage de résultats "
                f"{page_number}/{MAX_PAGES}"
            )

            try:
                soup = fetch_page(page_url)
            except Exception as error:
                print(f"Arrêt : {error}")
                break

            links = find_listing_links(soup)

            if not links:
                print(
                    "Aucun lien d'annonce détecté. "
                    "La structure HTML a peut-être changé."
                )
                break

            print(
                f"{len(links)} liens d'annonces "
                "repérés sur cette page."
            )

            new_links = [
                link for link in links
                if link not in seen_urls
            ]

            seen_urls.update(new_links)

            for url in new_links:
                listing_id = extract_id(url)

                if not listing_id:
                    continue

                if listing_id in seen_ids:
                    continue

                try:
                    record = extract_listing(url)

                    if not record["titre"]:
                        raise ValueError(
                            "Titre non détecté"
                        )

                    append_record(record)

                    seen_ids.add(listing_id)
                    success += 1

                    print(
                        f"OK {listing_id} | "
                        f"{record['titre']} | "
                        f"{record['prix_achat']} €"
                    )

                except Exception as error:
                    failures += 1
                    log_failed(url, str(error))

                    print(
                        f"Échec {listing_id} : {error}"
                    )

                time.sleep(DELAY_SECONDS)

            time.sleep(DELAY_SECONDS)

    finally:
        print("\nCollecte terminée ou interrompue.")
        print(f"Nouvelles annonces enregistrées : {success}")
        print(f"Échecs d'extraction : {failures}")
        print(f"CSV : {OUTPUT_CSV.resolve()}")
        print(f"Journal : {FAILED_LOG.resolve()}")


if __name__ == "__main__":
    main()