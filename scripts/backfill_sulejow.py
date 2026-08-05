"""
Backfill historii zbiornika Sulejów (Zalew Sulejowski) - dane "co tydzień".

Samodzielny skrypt (nie wymaga innych plików w repo poza standardowymi
bibliotekami requests i pdfplumber). Pobiera listę archiwalnych komunikatów
PGW Wody Polskie, dla każdej daty docelowej (co N dni, domyślnie 7) znajduje
najbliższy realnie opublikowany komunikat, pobiera PDF i wyciąga wiersz
"Zb. Sulejów (Pilica)" z tabeli zbiorników retencyjnych.

Uruchomienie lokalne:
    pip install -r requirements.txt
    python scripts/backfill_sulejow.py --start 2026-01-01 --end 2026-08-05 \
        --step-days 7 --output data/sulejow-2026.json

Uruchomienie przez GitHub Actions: patrz .github/workflows/backfill.yml
(workflow_dispatch, parametry start/end/step-days/output).

Format jednego wpisu wynikowego (JSON, lista takich obiektów, posortowana wg daty):
{
  "data": "2026-01-05",
  "rzedna_m_npm": 166.28,
  "poj_aktualna_mln_m3": 69.2,
  "poj_normalna_mln_m3": 75.1,
  "napelnienie_procent": 92.1,
  "doplyw_m3s": 17.3,
  "odplyw_m3s": 20.0,
  "rezerwa_aktualna_mln_m3": 15.1,
  "zrodlo_url": "https://www.gov.pl/attachment/18531805-6a21-43d3-b452-6589c7f466ee"
}
"""

import argparse
import io
import json
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
import pdfplumber

LIST_URL = "https://www.gov.pl/web/wody-polskie/sytuacja-hydrologiczna"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; SulejowBackfill/1.0)",
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}

# Oczekiwany zakres poj_normalna_mln_m3 dla zbiornika Sulejów - sanity-check,
# żeby wykryć sytuację, w której regex złapał wiersz innego zbiornika w tabeli.
VNORM_MIN, VNORM_MAX = 60.0, 90.0
VNORM_FALLBACK = 75.1


def fetch_list_page():
    resp = requests.get(LIST_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


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
        r"Zb\.\s*Sulejów"
        r"[^\n\d]{0,80}"
        r"(?:\n[^\d\n]{0,40})?"
        r"(?:\d+\s+)?"
        r"([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+(\d+)"
        r"(?:\s*\n?\s*\(Pilica\))?",
        full_text,
    )
    rzedna = re.search(
        r"Zbiorniku\s+Wodnym\s+Sulejów\s+rzędna\s+wody\s+górnej\s+na\s+godz\.\s*[\d:]+\s*UTC\s+wynosiła\s*([\d,]+)",
        full_text,
    )
    if not row:
        return None

    def f(x):
        return float(x.replace(",", "."))

    vnorm = f(row.group(4))
    vakt = f(row.group(3))

    if not (VNORM_MIN <= vnorm <= VNORM_MAX):
        print(
            f"[OSTRZEŻENIE] poj_normalna={vnorm} poza oczekiwanym zakresem "
            f"({VNORM_MIN}-{VNORM_MAX}) - prawdopodobnie zły wiersz tabeli. Pomijam.",
            file=sys.stderr,
        )
        return None

    return {
        "rzedna_m_npm": f(rzedna.group(1)) if rzedna else None,
        "poj_aktualna_mln_m3": vakt,
        "poj_normalna_mln_m3": vnorm,
        "napelnienie_procent": round(vakt / (vnorm or VNORM_FALLBACK) * 100, 1),
        "doplyw_m3s": f(row.group(2)),
        "odplyw_m3s": f(row.group(1)),
        "rezerwa_aktualna_mln_m3": f(row.group(7)),
    }


def find_nearest(target: date, archive_links, max_delta_days: int):
    best = None
    best_key = None
    for d, url in archive_links:
        delta = abs((d - target).days)
        if delta > max_delta_days:
            continue
        key = (delta, 0 if d <= target else 1)
        if best is None or key < best_key:
            best = (d, url)
            best_key = key
    return best


def load_existing(output_path: Path):
    if output_path.exists():
        return json.loads(output_path.read_text(encoding="utf-8"))
    return []


def save(output_path: Path, entries):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    entries_sorted = sorted(entries, key=lambda e: e["data"])
    output_path.write_text(
        json.dumps(entries_sorted, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start", required=True, help="Data startowa YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="Data końcowa YYYY-MM-DD")
    parser.add_argument("--step-days", type=int, default=7, help="Odstęp w dniach między próbkami (domyślnie 7)")
    parser.add_argument("--max-delta", type=int, default=5, help="Maks. liczba dni odchylenia przy szukaniu najbliższego komunikatu")
    parser.add_argument("--output", default="data/sulejow.json", help="Ścieżka pliku wyjściowego JSON")
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    output_path = Path(args.output)

    print(f"[BACKFILL] Pobieram listę komunikatów z {LIST_URL} ...")
    html_text = fetch_list_page()
    archive_links = get_archive_links(html_text)
    print(f"[BACKFILL] Znaleziono {len(archive_links)} unikalnych dat w archiwum.")

    existing = load_existing(output_path)
    existing_dates = {e["data"] for e in existing}

    targets = []
    d = start
    while d <= end:
        targets.append(d)
        d += timedelta(days=args.step_days)
    print(f"[BACKFILL] Cele: {len(targets)} dat, od {targets[0]} do {targets[-1]} (co {args.step_days} dni).")

    added = skipped_existing = skipped_no_match = skipped_no_data = 0

    for target in targets:
        match = find_nearest(target, archive_links, args.max_delta)
        if not match:
            print(f"[BACKFILL] {target}: brak komunikatu w promieniu {args.max_delta} dni - pomijam.")
            skipped_no_match += 1
            continue

        real_date, url = match
        iso_real = real_date.isoformat()

        if iso_real in existing_dates:
            print(f"[BACKFILL] {target} -> {iso_real}: już jest w danych - pomijam.")
            skipped_existing += 1
            continue

        try:
            pdf_resp = requests.get(url, headers=HEADERS, timeout=30)
            pdf_resp.raise_for_status()
            row = extract_sulejow(pdf_resp.content)
        except Exception as e:
            print(f"[BACKFILL] {target} -> {iso_real}: błąd pobierania/parsowania: {e}", file=sys.stderr)
            skipped_no_data += 1
            continue

        if row is None:
            print(f"[BACKFILL] {target} -> {iso_real}: brak wiersza Sulejowa w komunikacie - pomijam.")
            skipped_no_data += 1
            continue

        row["data"] = iso_real
        row["zrodlo_url"] = url
        existing.append(row)
        existing_dates.add(iso_real)
        added += 1
        print(f"[BACKFILL] {target} -> {iso_real}: dodano (poj. aktualna {row['poj_aktualna_mln_m3']} mln m3).")

        time.sleep(0.5)

    if added:
        save(output_path, existing)
        print(f"\n[BACKFILL] Zapisano {added} nowych wpisów do {output_path}")
    else:
        print("\n[BACKFILL] Brak nowych wpisów do zapisania.")

    print(f"[BACKFILL] Podsumowanie: dodano={added}, już istniały={skipped_existing}, "
          f"brak dopasowania={skipped_no_match}, brak danych={skipped_no_data}")


if __name__ == "__main__":
    main()

