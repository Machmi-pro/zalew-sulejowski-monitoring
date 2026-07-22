"""
Wylicza rzędną zbiornika Sulejów na podstawie krzywej pojemność <-> rzędna,
dopasowanej empirycznie do historycznych danych z data/sulejow.json.

DLACZEGO TEN SKRYPT ISTNIEJE:
Rzędna podawana w komunikatach PGW Wody Polskie pochodzi z osobnego akapitu
opisowego (nie z tabeli pojemności) i bywa aktualizowana rzadziej / z
opóźnieniem względem samej pojemności - potrafi się "zaciąć" na tej samej
wartości przez kilka dni, mimo że pojemność w tabeli się zmienia (patrz:
diagnostyka z 2026-07-20/21/22, gdzie rzędna 165,54 powtórzyła się 3 dni
z rzędu). Ponieważ zależność pojemność<->rzędna jest w praktyce gładka
i monotoniczna (kształt misy zbiornika), można ją odtworzyć empirycznie
z historycznych par danych i użyć do policzenia "wygładzonej", spójnej
z pojemnością wartości rzędnej na dziś.

WAŻNE ZASTRZEŻENIE: to jest przybliżenie empiryczne (regresja na danych
obserwacyjnych), NIE oficjalna krzywa batymetryczna PGW Wody Polskie.
Wyniki poza obserwowanym zakresem pojemności są ekstrapolacją i mogą być
zawodne - dlatego oznaczamy to osobnym polem `poza_zakresem_modelu`.

Współczynniki są przeliczane OD NOWA przy każdym uruchomieniu (nie są
zapisywane na stałe) - zgodnie z tym, że dokładność krzywej powinna
tylko rosnąć w miarę przybywania nowych punktów danych w sulejow.json.

Uruchamiane automatycznie przez .github/workflows/update-data.yml,
zaraz PO pobierz.py (potrzebuje świeżego sulejow.json na wejściu):
    python scripts/oblicz_rzedna.py
"""

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

SULEJOW_PATH = Path(__file__).resolve().parent.parent / "data" / "sulejow.json"
OUT_PATH = Path(__file__).resolve().parent.parent / "data" / "rzedna_wyliczenie.json"

STOPIEN_WIELOMIANU = 2

# Poniżej tej liczby unikalnych punktów nie próbujemy dopasowywać krzywej -
# wielomian 2. stopnia na garstce punktów łatwo przeuczyć (overfitting).
MIN_PUNKTOW_DO_DOPASOWANIA = 8

OKRESY_PORCJI = [
    ("1d", 1),
    ("14d", 14),
    ("30d", 30),
]


def wczytaj_dane():
    if not SULEJOW_PATH.exists():
        print(f"Brak pliku {SULEJOW_PATH}", file=sys.stderr)
        sys.exit(1)
    rows = json.loads(SULEJOW_PATH.read_text(encoding="utf-8"))
    if not rows:
        print("sulejow.json jest puste - nic do policzenia.", file=sys.stderr)
        sys.exit(1)
    return rows


def unikalne_pary_poj_rzedna(rows):
    """
    Zwraca posortowaną listę unikalnych (poj_aktualna_mln_m3, rzedna_m_npm)
    do dopasowania krzywej. Wiersze bez rzędnej (rzedna_m_npm is None) są
    pomijane - nie mają jak wejść do regresji.
    """
    pary = {
        (r["poj_aktualna_mln_m3"], r["rzedna_m_npm"])
        for r in rows
        if r.get("rzedna_m_npm") is not None and r.get("poj_aktualna_mln_m3") is not None
    }
    return sorted(pary)


def dopasuj_krzywa(pary):
    """
    Dopasowuje wielomian STOPIEN_WIELOMIANU do par (poj, rzedna).
    Zwraca (coeffs, poj_min, poj_max, n_punktow) albo None, jeśli za mało
    danych do sensownego dopasowania.
    """
    if len(pary) < MIN_PUNKTOW_DO_DOPASOWANIA:
        return None
    xs = np.array([p[0] for p in pary], dtype=float)
    ys = np.array([p[1] for p in pary], dtype=float)
    coeffs = np.polyfit(xs, ys, STOPIEN_WIELOMIANU)
    return coeffs, float(xs.min()), float(xs.max()), len(pary)


def oblicz_rzedna_z_poj(poj, coeffs):
    return round(float(np.polyval(coeffs, poj)), 3)


def zbuduj_slownik_dat(rows):
    """{data.date(): wiersz} - przy kilku wpisach tego samego dnia bierzemy ostatni w liście."""
    slownik = {}
    for r in rows:
        try:
            d = datetime.fromisoformat(r["data"]).date()
        except (KeyError, ValueError):
            continue
        slownik[d] = r
    return slownik


def znajdz_wiersz_sprzed(rows_by_date, target_date):
    """
    Zwraca wiersz z datą <= target_date, najbliższy w czasie (dane nie
    przychodzą co dzień - bywają luki, np. weekendy).
    """
    kandydaci = [d for d in rows_by_date if d <= target_date]
    if not kandydaci:
        return None
    najblizsza_data = max(kandydaci)
    return najblizsza_data, rows_by_date[najblizsza_data]


def main():
    rows = wczytaj_dane()
    rows_by_date = zbuduj_slownik_dat(rows)
    if not rows_by_date:
        print("Brak wierszy z poprawną datą w sulejow.json.", file=sys.stderr)
        sys.exit(1)

    najnowsza_data = max(rows_by_date)
    najnowszy_wiersz = rows_by_date[najnowsza_data]
    poj_dzis = najnowszy_wiersz.get("poj_aktualna_mln_m3")

    if poj_dzis is None:
        print(f"Brak poj_aktualna_mln_m3 dla najnowszej daty {najnowsza_data}.", file=sys.stderr)
        sys.exit(1)

    pary = unikalne_pary_poj_rzedna(rows)
    dopasowanie = dopasuj_krzywa(pary)

    if dopasowanie is None:
        print(
            f"Za mało punktów do dopasowania krzywej "
            f"({len(pary)} < {MIN_PUNKTOW_DO_DOPASOWANIA}) - pomijam wyliczenie.",
            file=sys.stderr,
        )
        # Nie nadpisujemy istniejącego pliku losowym brakiem danych - po prostu kończymy.
        sys.exit(0)

    coeffs, poj_min, poj_max, n_punktow = dopasowanie
    rzedna_dzis = oblicz_rzedna_z_poj(poj_dzis, coeffs)
    poza_zakresem = not (poj_min <= poj_dzis <= poj_max)

    wynik = {
        "data": najnowsza_data.isoformat(),
        "poj_aktualna_mln_m3": poj_dzis,
        "rzedna_surowa_m_npm": najnowszy_wiersz.get("rzedna_m_npm"),
        "rzedna_wyliczona_m_npm": rzedna_dzis,
        "poza_zakresem_modelu": poza_zakresem,
        "zakres_modelu_mln_m3": [round(poj_min, 1), round(poj_max, 1)],
        "n_punktow_dopasowania": n_punktow,
        "stopien_wielomianu": STOPIEN_WIELOMIANU,
        "wspolczynniki": [round(float(c), 8) for c in coeffs],
    }

    for etykieta, dni in OKRESY_PORCJI:
        target_date = najnowsza_data - timedelta(days=dni)
        znaleziony = znajdz_wiersz_sprzed(rows_by_date, target_date)
        if znaleziony is None:
            wynik[f"zmiana_{etykieta}_cm"] = None
            wynik[f"zmiana_{etykieta}_data_bazowa"] = None
            continue

        data_bazowa, wiersz_bazowy = znaleziony
        poj_bazowa = wiersz_bazowy.get("poj_aktualna_mln_m3")
        if poj_bazowa is None:
            wynik[f"zmiana_{etykieta}_cm"] = None
            wynik[f"zmiana_{etykieta}_data_bazowa"] = None
            continue

        rzedna_bazowa = oblicz_rzedna_z_poj(poj_bazowa, coeffs)
        wynik[f"zmiana_{etykieta}_cm"] = round((rzedna_dzis - rzedna_bazowa) * 100, 1)
        wynik[f"zmiana_{etykieta}_data_bazowa"] = data_bazowa.isoformat()

    if poza_zakresem:
        print(
            f"UWAGA: dzisiejsza pojemność ({poj_dzis} mln m3) jest poza zakresem "
            f"modelu [{poj_min}, {poj_max}] - wynik to ekstrapolacja.",
            file=sys.stderr,
        )

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(
        json.dumps(wynik, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        f"Zapisano {OUT_PATH}: rzędna wyliczona {rzedna_dzis} m n.p.m. "
        f"(surowa z komunikatu: {wynik['rzedna_surowa_m_npm']}), "
        f"n={n_punktow} punktów, zakres=[{poj_min}, {poj_max}]"
    )


if __name__ == "__main__":
    main()
