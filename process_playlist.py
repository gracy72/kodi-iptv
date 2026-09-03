import concurrent.futures
from difflib import SequenceMatcher
import re
import urllib.request
import xml.etree.ElementTree as ET

# Główne polskie źródła M3U
ZRODLA_POLSKIE = [
    "https://iptv-org.github.io/iptv/languages/pol.m3u",
    "https://iptv-org.github.io/iptv/countries/pl.m3u",
]

# Międzynarodowe źródła kategoryczne (będą filtrowane pod kątem języka polskiego)
ZRODLA_KATEGORYCZNE = [
    "https://iptv-org.github.io/iptv/categories/news.m3u",
    "https://iptv-org.github.io/iptv/categories/movies.m3u",
    "https://iptv-org.github.io/iptv/categories/music.m3u",
    "https://iptv-org.github.io/iptv/categories/general.m3u",
    "https://iptv-org.github.io/iptv/categories/entertainment.m3u",
    "https://iptv-org.github.io/iptv/categories/sports.m3u",
]

EPG_URL = "https://epg.ovh/pl.xml"
OUTPUT_FILE = "wszystkie_dzialajace_kodi.m3u"

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Kodi/21.0"}


def pobierz_dane(url: str) -> str:
    """Pobiera treść tekstową z podanego adresu URL."""
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as response:
        return response.read().decode("utf-8", errors="ignore")


def normalizuj_nazwe(nazwa: str) -> str:
    """Czyszczenie nazwy kanału do porównania z bazą EPG."""
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
    """Testuje połączenie ze strumieniem bez pobierania całego pliku."""
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
    """Szuka najbardziej prawdopodobnego ID w bazie EPG na podstawie podobieństwa tekstu."""
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


def czy_kanal_jest_polski(extinf: str, epg_id: str | None, z_polskiej_listy: bool) -> bool:
    """Weryfikuje, czy dany kanał jest polskojęzyczny."""
    # 1. Zawsze przepuszczamy kanały pochodzące bezpośrednio z polskich playlist
    if z_polskiej_listy:
        return True

    extinf_lower = extinf.lower()

    # 2. Sprawdzamy obecność polskich tagów językowych/krajowych w nagłówku M3U
    if 'tvg-language="pol"' in extinf_lower or 'tvg-country="pl"' in extinf_lower:
        return True

    # 3. Jeśli kanał z listy kategorycznej pasuje do polskiego EPG z epg.ovh
    if epg_id is not None:
        return True

    return False


def przetworz_liste():
    """Główna funkcja: pobiera, filtruje, testuje i zapisuje M3U."""
    mapa_epg = pobierz_baze_epg()

    surowe_kanaly = []
    unikalne_urle = set()

    print("2. Pobieranie i selekcja źródeł M3U...")

    # Pobieranie polskich list (z oznaczeniem z_polskiej_listy = True)
    for url in ZRODLA_POLSKIE:
        try:
            m3u_text = pobierz_dane(url)
            linie = m3u_text.splitlines()
            i = 0
            while i < len(linie):
                linia = linie[i].strip()
                if linia.startswith("#EXTINF:"):
                    if i + 1 < len(linie) and not linie[i + 1].startswith("#"):
                        stream_url = linie[i + 1].strip()
                        if stream_url not in unikalne_urle:
                            unikalne_urle.add(stream_url)
                            surowe_kanaly.append((linia, stream_url, True))
                        i += 1
                i += 1
        except Exception as e:
            print(f"   Błąd pobierania {url}: {e}")

    # Pobieranie list kategorycznych (z oznaczeniem z_polskiej_listy = False)
    for url in ZRODLA_KATEGORYCZNE:
        try:
            m3u_text = pobierz_dane(url)
            linie = m3u_text.splitlines()
            i = 0
            while i < len(linie):
                linia = linie[i].strip()
                if linia.startswith("#EXTINF:"):
                    if i + 1 < len(linie) and not linie[i + 1].startswith("#"):
                        stream_url = linie[i + 1].strip()
                        if stream_url not in unikalne_urle:
                            unikalne_urle.add(stream_url)
                            surowe_kanaly.append((linia, stream_url, False))
                        i += 1
                i += 1
        except Exception as e:
            print(f"   Błąd pobierania {url}: {e}")

    print(f"3. Testowanie sygnału dla {len(surowe_kanaly)} unikalnych stacji...")
    finalne_stacje = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
        testy = {
            executor.submit(spradz_czy_stream_dziala, item[1]): item
            for item in surowe_kanaly
        }

        przetworzone = 0
        lacznie = len(surowe_kanaly)

        for future in concurrent.futures.as_completed(testy):
            przetworzone += 1
            extinf, stream_url, z_polskiej_listy = testy[future]
            dziala = future.result()

            nazwa_kanalu = extinf.rsplit(",", 1)[-1].strip()

            if dziala:
                epg_id = dopasuj_epg_fuzzy(
                    nazwa_kanalu, mapa_epg, min_podobienstwo=0.60
                )

                # KLUCZOWY FILTR JĘZYKOWY
                if czy_kanal_jest_polski(extinf, epg_id, z_polskiej_listy):
                    if epg_id:
                        if 'tvg-id="' in extinf:
                            extinf = re.sub(
                                r'tvg-id="[^"]*"', f'tvg-id="{epg_id}"', extinf
                            )
                        else:
                            extinf = extinf.replace(
                                "#EXTINF:-1", f'#EXTINF:-1 tvg-id="{epg_id}"'
                            )

                    finalne_stacje.append((extinf, stream_url))
                    print(
                        f"   [{przetworzone}/{lacznie}] [AKCEPTACJA PL]"
                        f" {nazwa_kanalu}"
                    )
                else:
                    print(
                        f"   [{przetworzone}/{lacznie}] [ODRZUCENIE - NIE-PL]"
                        f" {nazwa_kanalu}"
                    )

    # Zapis wyniku do pliku M3U
    zapis_linie = ['#EXTM3U url-tvg="https://epg.ovh/pl.xml"']
    for extinf, url in finalne_stacje:
        zapis_linie.append(extinf)
        zapis_linie.append(url)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(zapis_linie))

    print(
        f"\nZakończono! Zapisano {len(finalne_stacje)} działających polskich"
        f" stacji do {OUTPUT_FILE}"
    )


if __name__ == "__main__":
    przetworz_liste()
