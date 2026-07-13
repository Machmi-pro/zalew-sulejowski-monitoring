"""
Pobiera najnowszy komunikat "Informacja o sytuacji hydrologiczno-meteorologicznej"
publikowany przez PGW Wody Polskie, wyciąga dane dla zbiornika Sulejów (Pilica)
i dopisuje nowy wiersz do data/sulejow.json (jeśli dana data jeszcze tam nie istnieje).

Uruchamiane automatycznie przez .github/workflows/update-data.yml (cron),
ale można też odpalić ręcznie:  python scripts/pobierz.py
"""

import json
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
import pdfplumber
import io

LIST_URL = "https://www.gov.pl/web/wody-polskie/sytuacja-hydrologiczna"

# Nagłówki wymuszające pominięcie cache (przeglądarek pośredniczących / CDN).
# Strona gov.pl bywa serwowana ze starej, zcache'owanej kopii (widzieliśmy to
# wielokrotnie - np. "Materiały" pokazujące plik sprzed 2 miesięcy). Same
# nagłówki nie dają gwarancji ominięcia CDN-a, ale to najtańsza rzecz do
# wypróbowania, zanim uznamy, że trzeba ręcznie wklejać linki.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; SulejowMonitor/1.0)",
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}
DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "sulejow.json"

# Vnorm (pojemność normalna) używana do liczenia % napełnienia, na wypadek
# gdyby komunikat go akurat nie zawierał
VNORM_FALLBACK = 75.1


def fetch_list_page():
    """
    Pobiera stronę listy komunikatów, próbując ominąć cache CDN-a.
    Dokłada losowy parametr w URL (część CDN-ów honoruje query string jako
    część klucza cache) razem z nagłówkami no-cache.
    """
    cache_buster = f"?_cb={int(time.time())}{random.randint(1000, 9999)}"
    resp = requests.get(LIST_URL + cache_buster, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp


def get_materialy_link(html_text: str):
    """
    Wyciąga link z sekcji "Materiały" - to ZAWSZE najświeższy, aktualny plik
    (nazwany "Sytuacja_hydrologiczna_YYYY-MM-DD"), inny format niż wpisy
    w "Archiwalnych komunikatach". To najszybszy sposób na dane z bieżącego dnia,
    bez czekania aż trafią do archiwum.
    """
    # Usuń niewidoczne znaki (zero-width space itp.), które gov.pl wstawia w tekst
    clean = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", html_text)
    pattern = re.compile(
        r'href="(https://www\.gov\.pl/attachment/[a-f0-9-]+)"[^>]*>\s*'
        r'Sytuacja[_\s]*hydrologiczna[_\s]*(\d{4}-\d{2}-\d{2})',
        re.IGNORECASE,
    )
    match = pattern.search(clean)
    if not match:
        return None
    url, date_str = match.group(1), match.group(2)
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return None
    return (d, url)


def get_archive_links(html_text: str):
    """Wyciąga (data, url) z listy archiwalnych komunikatów na stronie Wód Polskich."""
    pattern = re.compile(
        r'href="(https://www\.gov\.pl/attachment/[a-f0-9-]+)"[^>]*>\s*'
        r'(?:Skrócony\s+)?Komunikat[^<]*z\s+dnia\s+(\d{1,2}\.\d{1,2}\.\d{4})',
        re.IGNORECASE,
    )
    results = []
    for url, date_str in pattern.findall(html_text):
        try:
            d = datetime.strptime(date_str, "%d.%m.%Y").date()
            results.append((d, url))
        except ValueError:
            continue
    # najnowsze najpierw, bez duplikatów dat (bierzemy pierwsze wystąpienie)
    seen = set()
    unique = []
    for d, url in sorted(results, key=lambda x: x[0], reverse=True):
        if d not in seen:
            seen.add(d)
            unique.append((d, url))
    return unique


def extract_sulejow(pdf_bytes: bytes):
    """Parsuje PDF komunikatu i wyciąga dane dla Zb. Sulejów (Pilica)."""
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)

    row = re.search(
        r"Zb\.\s*Sulejów\s*\n?\s*\(Pilica\)\s*"
        r"([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+(\d+)",
        full_text,
    )
    rzedna = re.search(
        r"Zbiorniku Wodnym Sulejów rzędna wody górnej na godz\.\s*[\d:]+\s*UTC wynosiła\s*([\d,]+)",
        full_text,
    )
    if not row:
        return None

    def f(x):
        return float(x.replace(",", "."))

    vnorm = f(row.group(4))
    vakt = f(row.group(3))

    return {
        "odplyw_m3s": f(row.group(1)),
        "doplyw_m3s": f(row.group(2)),
        "poj_aktualna_mln_m3": vakt,
        "poj_normalna_mln_m3": vnorm,
        "poj_max_mln_m3": f(row.group(5)),
        "rezerwa_wymagana_mln_m3": f(row.group(6)),
        "rezerwa_aktualna_mln_m3": f(row.group(7)),
        "rezerwa_procent": int(row.group(8)),
        "rzedna_m_npm": f(rzedna.group(1)) if rzedna else None,
        "napelnienie_procent": round(vakt / (vnorm or VNORM_FALLBACK) * 100, 1),
    }


def load_existing():
    if DATA_PATH.exists():
        return json.loads(DATA_PATH.read_text(encoding="utf-8"))
    return []


def save(entries):
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    entries_sorted = sorted(entries, key=lambda e: e["data"])
    DATA_PATH.write_text(
        json.dumps(entries_sorted, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main():
    existing = load_existing()
    existing_dates = {e["data"] for e in existing}

    resp = fetch_list_page()

    # 1) Najpierw próbujemy sekcji "Materiały" - to zawsze najświeższy plik,
    #    dostępny szybciej niż wpis w archiwum (który bywa opóźniony/cache'owany).
    materialy = get_materialy_link(resp.text)

    # 2) Uzupełniamy o listę archiwalną (dla nadrobienia zaległych dni)
    archive_links = get_archive_links(resp.text)

    # DIAGNOSTYKA: jeśli regex "Materiały" nie trafił, wypisz surowy fragment
    # HTML zaraz po nagłówku "Aktualna sytuacja hydrologiczno-meteorologiczna" -
    # to unikalny punkt zaczepienia tuż przed właściwą sekcją "Materiały"
    # (w odróżnieniu od "Materiały do pobrania" w menu nawigacyjnym).
    if not materialy:
        clean_for_search = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", resp.text)
        kotwica = "Aktualna sytuacja hydrologiczno-meteorologiczna"
        idx = clean_for_search.find(kotwica)
        if idx != -1:
            fragment = clean_for_search[idx:idx + 800]
            print(f"[DIAGNOSTYKA] Fragment HTML po kotwicy '{kotwica}':")
            print(repr(fragment))
        else:
            print(f"[DIAGNOSTYKA] Nie znaleziono kotwicy '{kotwica}' na stronie.")

    links = []
    if materialy:
        print(f"[DIAGNOSTYKA] Sekcja 'Materiały' wskazuje na: data={materialy[0].isoformat()}, url={materialy[1]}")
    else:
        print("[DIAGNOSTYKA] Nie znaleziono linku w sekcji 'Materiały' (regex nie trafił).")

    if archive_links:
        najnowszy_archiwalny = archive_links[0]
        print(f"[DIAGNOSTYKA] Najnowszy wpis w archiwum: data={najnowszy_archiwalny[0].isoformat()}, url={najnowszy_archiwalny[1]}")
    else:
        print("[DIAGNOSTYKA] Nie znaleziono żadnych linków archiwalnych.")

    print(f"[DIAGNOSTYKA] Najnowsza data już zapisana w sulejow.json: "
          f"{max(existing_dates) if existing_dates else 'brak danych'}")

    if materialy:
        links.append(materialy)
    for d, url in archive_links:
        if not links or d != links[0][0]:
            links.append((d, url))

    if not links:
        print("Nie znaleziono żadnych linków na stronie listy komunikatów.", file=sys.stderr)
        sys.exit(1)

    # Sprawdzamy tylko kilka najnowszych dni - jeśli już je mamy, nic nie robimy
    added = 0
    for date, url in links[:5]:
        iso_date = date.isoformat()
        if iso_date in existing_dates:
            print(f"[DIAGNOSTYKA] Data {iso_date} już jest w danych - pomijam.")
            continue
        try:
            pdf_resp = requests.get(url, headers=HEADERS, timeout=30)
            pdf_resp.raise_for_status()
            data = extract_sulejow(pdf_resp.content)
        except Exception as e:
            print(f"Błąd przy przetwarzaniu {url}: {e}", file=sys.stderr)
            continue

        if data is None:
            print(f"Brak danych o Sulejowie w komunikacie z {iso_date}", file=sys.stderr)
            continue

        data["data"] = iso_date
        data["zrodlo_url"] = url
        existing.append(data)
        existing_dates.add(iso_date)
        added += 1
        print(f"Dodano wpis za {iso_date}: pojemność {data['poj_aktualna_mln_m3']} mln m3")

    if added:
        save(existing)
        print(f"Zapisano {added} nowych wpisów do {DATA_PATH}")
    else:
        print("Brak nowych danych do dodania (wszystko już aktualne).")


if __name__ == "__main__":
    main()
