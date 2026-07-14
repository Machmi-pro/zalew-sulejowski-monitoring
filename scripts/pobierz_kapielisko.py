"""
pobierz_kapielisko.py
----------------------
Scraper danych kapieliska "Kapielisko na sztucznym zbiorniku rzeki Pilicy"
(Zalew Sulejowski, Smardzewice) ze strony Serwisu Kapieliskowego GIS.

Zrodlo: https://sk.gis.gov.pl/kapielisko/{KAPIELISKO_ID}

Uwaga: strona nie udostepnia publicznego API (sprawdzone) - dane pobierane
sa przez scraping HTML. Selektory oparte sa na etykietach tekstowych
(np. "Temperatura wody:", "Data oceny"), a nie na klasach CSS, co czyni
scraper bardziej odpornym na drobne zmiany w markupie strony. Jesli GIS
zmieni strukture strony, w pierwszej kolejnosci sprawdz funkcje
_find_label_value() i parsowanie tabel historii.

Wyjscie: kapielisko_930.json (nadpisywane przy kazdym uruchomieniu),
z mozliwoscia dopisania do historii lokalnej (patrz ARCHIWIZUJ_HISTORIE).

Uzycie:
    pip install requests beautifulsoup4 --break-system-packages
    python pobierz_kapielisko.py
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

KAPIELISKO_ID = 930
URL = f"https://sk.gis.gov.pl/kapielisko/{KAPIELISKO_ID}"
# Zapis do data/kapielisko_930.json w KORZENIU repo - obok istniejącego
# data/sulejow.json. Uwaga: uzywamy Path.cwd() (katalog roboczy), a NIE
# Path(__file__).parent, bo skrypt moze leziec w scripts/ - a GitHub Actions
# (i normalne uruchomienie "python scripts/pobierz_kapielisko.py" z roota repo)
# ustawia katalog roboczy na korzen repo. Path(__file__).parent dawal wtedy
# scripts/data/... co bylo przyczyna bledu "brak zmian" w workflow.
OUTPUT_PATH = Path.cwd() / "data" / f"kapielisko_{KAPIELISKO_ID}.json"

# Jesli True, kazdy odczyt jest doklejany do lokalnej historii pomiarow
# pogodowych (osobno od historii ocen wody, ktora GIS trzyma sam).
ARCHIWIZUJ_HISTORIE = True
HISTORIA_PATH = Path.cwd() / "data" / f"kapielisko_{KAPIELISKO_ID}_historia_pomiarow.jsonl"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ZbiornikSulejowskiMonitor/1.0; "
                  "+https://github.com/) scraper danych publicznych GIS"
}


def pobierz_html(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text


def _text_after_label(soup: BeautifulSoup, label: str):
    """Znajduje element zawierajacy dokladnie dana etykiete (np. 'Temperatura wody:')
    i zwraca tekst nastepnego sasiedniego elementu / wezla tekstowego."""
    node = soup.find(string=re.compile(re.escape(label)))
    if node is None:
        return None
    # tekst czesto siedzi w kolejnym elemencie rodzenstwa albo w tym samym bloku
    parent = node.parent
    # sprobuj nastepnego rodzenstwa z tekstem
    sib = parent.find_next_sibling()
    if sib and sib.get_text(strip=True):
        return sib.get_text(strip=True)
    # sprobuj tekstu bezposrednio po etykiecie w tym samym bloku nadrzednym
    full_text = parent.get_text(" ", strip=True)
    m = re.search(re.escape(label) + r"\s*(.+)", full_text)
    if m:
        val = m.group(1).strip()
        if val:
            return val
    # ostatecznie: nastepny element w drzewie po wezle tekstowym
    nxt = node.find_next(string=True)
    return nxt.strip() if nxt else None


def _extract_number(text):
    if text is None:
        return None
    m = re.search(r"-?\d+(?:[.,]\d+)?", text)
    if not m:
        return None
    return float(m.group(0).replace(",", "."))


def parsuj_warunki(soup: BeautifulSoup) -> dict:
    """Sekcja 'Warunki': temperatura wody/powietrza, wiatr, data pomiaru."""
    temp_powietrza = _extract_number(_text_after_label(soup, "Temperatura powietrza"))
    temp_wody = _extract_number(_text_after_label(soup, "Temperatura wody"))
    wiatr = _extract_number(_text_after_label(soup, "Predkosc wiatru")
                             or _text_after_label(soup, "Prędkość wiatru"))
    data_pomiaru = _text_after_label(soup, "Data ostatniego pomiaru")

    return {
        "temp_wody_c": temp_wody,
        "temp_powietrza_c": temp_powietrza,
        "wiatr_ms": wiatr,
        "data_pomiaru": _normalizuj_date(data_pomiaru),
    }


def parsuj_ocene_aktualna(soup: BeautifulSoup) -> dict:
    """Sekcja 'Ocena wody' - aktualny status na gorze strony."""
    data_oceny = _text_after_label(soup, "Data oceny")
    nastepne_badanie = _text_after_label(soup, "Nastepne badanie") \
        or _text_after_label(soup, "Następne badanie")

    # tresc oceny (np. "Woda przydatna do kapieli") - szukamy bloku sekcji "Ocena wody"
    ocena_naglowek = soup.find(string=re.compile("Ocena wody"))
    ocena_tekst = None
    if ocena_naglowek:
        blok = ocena_naglowek.find_parent()
        if blok:
            # najblizszy element listy / paragrafu z fraza "przydatna"/"nieprzydatna"
            kandydat = blok.find_next(string=re.compile("przydatna", re.IGNORECASE))
            if kandydat:
                ocena_tekst = kandydat.strip()

    przydatna = None
    if ocena_tekst:
        przydatna = ocena_tekst.lower().startswith("woda przydatna")

    return {
        "ocena_tekst": ocena_tekst,
        "przydatna": przydatna,
        "data_oceny": _normalizuj_date(data_oceny),
        "nastepne_badanie": _normalizuj_date(nastepne_badanie),
    }


def parsuj_historie(soup: BeautifulSoup) -> list:
    """Parsuje wszystkie tabele historii ocen (sekcje rozwijane pod
    'Pokaz oceny wody'). Nie polega na strukturze zakladek sezonow w HTML -
    kazdy wiersz ma pelna date (DD/MM/YYYY), wiec grupowanie po roku robimy
    pozniej w group_by_rok(), niezaleznie od zagniezdzenia w markupie."""
    historia = []

    for table in soup.find_all("table"):
        naglowki = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        if not any("ocena" in h for h in naglowki):
            continue  # to nie jest tabela ocen wody

        for row in table.find_all("tr")[1:]:  # pomijamy naglowek
            cells = [td.get_text(" ", strip=True) for td in row.find_all("td")]
            if not cells or not cells[0]:
                continue

            data_oceny = cells[0] if len(cells) > 0 else None
            ocena = cells[1] if len(cells) > 1 else None
            e_coli = cells[2] if len(cells) > 2 else None
            enterokoki = cells[3] if len(cells) > 3 else None
            nastepne = cells[4] if len(cells) > 4 else None

            # rozbicie oceny i ew. przyczyny niezdatnosci ("Przyczyna: Zakwit sinic")
            przyczyna = None
            if ocena and "Przyczyna" in ocena:
                czesci = ocena.split("Przyczyna", 1)
                ocena_glowna = czesci[0].strip(" -")
                przyczyna = czesci[1].lstrip(":").strip(" -")
                ocena = ocena_glowna

            historia.append({
                "data_oceny": _normalizuj_date(data_oceny),
                "ocena": ocena,
                "przyczyna": przyczyna or None,
                "e_coli": _extract_number(e_coli),
                "enterokoki": _extract_number(enterokoki),
                "nastepne_badanie": _normalizuj_date(nastepne),
            })

    # usuniecie duplikatow (ta sama data moze wystapic w wiecej niz jednej tabeli)
    unikalne = {(h["data_oceny"], h["ocena"]): h for h in historia}
    wynik = sorted(unikalne.values(), key=lambda h: h["data_oceny"] or "", reverse=True)
    return wynik


def _normalizuj_date(tekst):
    """Konwertuje 'DD/MM/YYYY' -> 'YYYY-MM-DD'. Zwraca None jesli nie da sie sparsowac."""
    if not tekst:
        return None
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", tekst)
    if not m:
        return None
    dd, mm, yyyy = m.groups()
    return f"{yyyy}-{mm}-{dd}"


def grupuj_po_roku(historia: list) -> dict:
    """Zwraca {'2026': [...], '2025': [...], ...} na podstawie roku z data_oceny.
    Wiersze bez mozliwej do sparsowania daty trafiaja do klucza 'brak_daty'."""
    sezony = {}
    for wpis in historia:
        rok = wpis["data_oceny"][:4] if wpis.get("data_oceny") else "brak_daty"
        sezony.setdefault(rok, []).append(wpis)
    # sortowanie wewnatrz roku malejaco po dacie
    for rok in sezony:
        sezony[rok].sort(key=lambda w: w.get("data_oceny") or "", reverse=True)
    return sezony


def zbuduj_alert(ocena_aktualna: dict) -> dict:
    """Prosta logika alertu do wykorzystania na froncie (baner czerwony/zielony)."""
    if ocena_aktualna.get("przydatna") is True:
        return {"poziom": "ok", "komunikat": "Woda przydatna do kapieli"}
    if ocena_aktualna.get("przydatna") is False:
        powod = ""
        return {
            "poziom": "warn",
            "komunikat": f"Woda NIEPRZYDATNA do kapieli"
                         + (f" ({powod})" if powod else ""),
        }
    return {"poziom": "unknown", "komunikat": "Brak aktualnej oceny"}


def main():
    try:
        html = pobierz_html(URL)
    except requests.RequestException as e:
        print(f"Blad pobierania strony: {e}", file=sys.stderr)
        sys.exit(1)

    soup = BeautifulSoup(html, "html.parser")

    warunki = parsuj_warunki(soup)
    ocena_aktualna = parsuj_ocene_aktualna(soup)
    historia = parsuj_historie(soup)
    sezony = grupuj_po_roku(historia)
    alert = zbuduj_alert(ocena_aktualna)

    wynik = {
        "kapielisko_id": KAPIELISKO_ID,
        "nazwa": "Kapielisko na sztucznym zbiorniku rzeki Pilicy (Zalew Sulejowski)",
        "zrodlo_url": URL,
        "pobrano_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "warunki": warunki,
        "ocena_aktualna": ocena_aktualna,
        "alert": alert,
        "sezony": sezony,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(wynik, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Zapisano: {OUTPUT_PATH}")

    if ARCHIWIZUJ_HISTORIE:
        wpis = {
            "pobrano_utc": wynik["pobrano_utc"],
            "temp_wody_c": warunki["temp_wody_c"],
            "temp_powietrza_c": warunki["temp_powietrza_c"],
            "wiatr_ms": warunki["wiatr_ms"],
            "ocena_przydatna": ocena_aktualna["przydatna"],
        }
        with HISTORIA_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(wpis, ensure_ascii=False) + "\n")
        print(f"Dopisano do historii: {HISTORIA_PATH}")

    # podglad w konsoli
    print(json.dumps(wynik, ensure_ascii=False, indent=2)[:800], "...")


if __name__ == "__main__":
    main()
