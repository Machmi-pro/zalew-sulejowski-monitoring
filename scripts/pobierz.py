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
    Pobiera stronę listy komunikatów, pró
