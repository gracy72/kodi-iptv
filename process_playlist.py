import concurrent.futures
from difflib import SequenceMatcher
import re
import urllib.request
import xml.etree.ElementTree as ET

# Ustawienia ścieżek źródłowych i wyjściowych
M3U_URL = "https://iptv-org.github.io/iptv/languages/pol.m3u"
EPG_URL = "https://epg.ovh/pl.xml"
OUTPUT_FILE = "wszystkie_dzialajace_kodi.m3u"

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Kodi/21.0"}


def pobierz_dane(url: str) -> str:
    """Pobiera zawartość tekstową ze wskazanego adresu URL."""
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as response:
        return response.read().decode("utf-8", errors="ignore")


def normalizuj_nazwe(nazwa: str) -> str:
    """Ujednolica nazwę kanału (usuwa frazy jakościowe, spacje i znaki specjalne)."""
    if not nazwa:
        return ""
    nazwa = nazwa.lower()
    nazwa = re.sub(
        r"\b(hd|fhd|uhd|4k|sd|pl|poland|polski|1080p|720p|stream|live)\b",
        "",
        nazwa,
    )
    nazwa = re.sub(r"[^\w\s]", "", nazwa)
    return "".join(nazwa.split())


def spradz_czy_stream_dziala(url_streamu: str, timeout: float = 3.5) -> bool:
    """Sprawdza, czy strumień wideo przesyła bajty danych w sieci."""
    try:
        req = urllib.request.Request(url_streamu, headers=HEADERS)
        req.add_header("Range", "bytes=0-1024")
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status in (200, 206)
    except Exception:
        return False


def pobierz_baze_epg() -> dict[str, str]:
    """Tworzy słownik EPG: znormalizowana_nazwa -> id_w_xml."""
    print("1. Pobieranie bazy EPG XML...")
    mapa_epg = {}
    try:
        xml_data = pobierz_dane(EPG_URL)
        root = ET.fromstring(xml_data)

        for channel in root.findall("channel"):
            channel_id = channel.get("id")
            if not channel_id:
                continue

            mapa_epg[normalizuj_nazwe(channel_id)] = channel_id
            for display_name in channel.findall("display-name"):
                if display_name.text:
                    mapa_epg[normalizuj_nazwe(display_name.text)] = channel_id

        print(f"   Załadowano {len(mapa_epg)} reguł z EPG.")
    except Exception as e:
        print(f"   Błąd pobierania EPG: {e}")

    return mapa_epg


def dopasuj_epg_fuzzy(
    nazwa_stacji: str, mapa_epg: dict[str, str], min_podobienstwo: float = 0.60
) -> str | None:
    """Wyszukuje najbardziej prawdopodobne ID w bazie EPG na podstawie podobieństwa tekstu."""
    norm_stacja = normalizuj_nazwe(nazwa_stacji)
    if not norm_stacja:
        return None

    if norm_stacja in mapa_epg:
        return mapa_epg[norm_stacja]

    najlepszy_id = None
    najwyzszy_wynik = 0.0

    for norm_epg, epg_id in mapa_epg.items():
        podobienstwo = SequenceMatcher(None, norm_stacja, norm_epg).ratio()
        if podobienstwo > najwyzszy_wynik and podobienstwo >= min_podobienstwo:
            najwyzszy_wynik = podobienstwo
            najlepszy_id = epg_id

    return najlepszy_id


def przetworz_liste():
    """Główna logika przetwarzania i generowania pliku wyjściowego."""
    mapa_epg = pobierz_baze_epg()

    print("2. Pobieranie listy M3U...")
    try:
        m3u_text = pobierz_dane(M3U_URL)
        linie = m3u_text.splitlines()
    except Exception as e:
        print(f"   Błąd pobierania M3U: {e}")
        return

    kanaly = []
    i = 0
    while i < len(linie):
        linia = linie[i].strip()
        if linia.startswith("#EXTINF:"):
            if i + 1 < len(linie) and not linie[i + 1].startswith("#"):
                kanaly.append((linia, linie[i + 1].strip()))
                i += 1
        i += 1

    print(f"3. Testowanie sygnału dla {len(kanaly)} kanałów...")
    finalne_stacje = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
        testy = {
            executor.submit(spradz_czy_stream_dziala, url): (extinf, url)
            for extinf, url in kanaly
        }

        for future in concurrent.futures.as_completed(testy):
            extinf, url = testy[future]
            dziala = future.result()

            nazwa_kanalu = extinf.rsplit(",", 1)[-1].strip()

            if dziala:
                epg_id = dopasuj_epg_fuzzy(
                    nazwa_kanalu, mapa_epg, min_podobienstwo=0.60
                )

                if epg_id:
                    if 'tvg-id="' in extinf:
                        extinf = re.sub(
                            r'tvg-id="[^"]*"', f'tvg-id="{epg_id}"', extinf
                        )
                    else:
                        extinf = extinf.replace(
                            "#EXTINF:-1", f'#EXTINF:-1 tvg-id="{epg_id}"'
                        )

                # Zachowujemy każdy działający strumień (z EPG lub bez)
                finalne_stacje.append((extinf, url))

    # Zapis wyniku do pliku M3U
    zapis_linie = ['#EXTM3U url-tvg="https://epg.ovh/pl.xml"']
    for extinf, url in finalne_stacje:
        zapis_linie.append(extinf)
        zapis_linie.append(url)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(zapis_linie))

    print(f"Zapisano {len(finalne_stacje)} działających stacji do {OUTPUT_FILE}")


if __name__ == "__main__":
    przetworz_liste()
