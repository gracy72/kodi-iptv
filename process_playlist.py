import asyncio
from difflib import SequenceMatcher
import re
import xml.etree.ElementTree as ET
import aiohttp

# Publiczne źródła M3U do automatycznego przeszukania
INDEKSY_ZRODEL = [
    "https://iptv-org.github.io/iptv/languages/pol.m3u",
    "https://iptv-org.github.io/iptv/countries/pl.m3u",
    "https://raw.githubusercontent.com/Free-TV/IPTV/refs/heads/master/playlists/playlist_poland.m3u8",
    "https://raw.githubusercontent.com/Romaxa55/world_ip_tv/refs/heads/master/output/pl.m3u",
]

EPG_URL = "https://epg.ovh/pl.xml"
OUTPUT_FILE = "wszystkie_dzialajace_kodi.m3u"

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Kodi/21.0"}


def normalizuj_nazwe(nazwa: str) -> str:
    """Ujednolica nazwę kanału do łatwego porównywania z bazą EPG i usuwania duplikatów."""
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


async def pobierz_tekst_async(
    session: aiohttp.ClientSession, url: str, timeout: float = 15.0
) -> str:
    """Asynchronicznie pobiera treść tekstową ze wskazanego adresu URL."""
    try:
        async with session.get(
            url,
            headers=HEADERS,
            timeout=aiohttp.ClientTimeout(total=timeout),
            ssl=False,
        ) as response:
            if response.status == 200:
                return await response.text(errors="ignore")
    except Exception as e:
        print(f"   Błąd pobierania {url}: {e}")
    return ""


async def sprawdz_stream_async(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    url: str,
    timeout: float = 3.5,
) -> bool:
    """Asynchronicznie testuje dostępność sygnału wideo bez pobierania całego pliku."""
    async with semaphore:
        try:
            naglowki = {**HEADERS, "Range": "bytes=0-1024"}
            async with session.get(
                url,
                headers=naglowki,
                timeout=aiohttp.ClientTimeout(total=timeout),
                ssl=False,
            ) as response:
                return response.status in (200, 206)
        except Exception:
            return False


async def pobierz_baze_epg(session: aiohttp.ClientSession) -> dict[str, str]:
    """Pobiera i parsuje plik EPG XML."""
    print("1. Pobieranie bazy EPG z epg.ovh...")
    mapa_epg = {}
    xml_data = await pobierz_tekst_async(session, EPG_URL)

    if xml_data:
        try:
            root = ET.fromstring(xml_data)
            for channel in root.findall("channel"):
                channel_id = channel.get("id")
                if not channel_id:
                    continue

                mapa_epg[normalizuj_nazwe(channel_id)] = channel_id
                for display_name in channel.findall("display-name"):
                    if display_name.text:
                        mapa_epg[normalizuj_nazwe(display_name.text)] = (
                            channel_id
                        )

            print(f"   Załadowano {len(mapa_epg)} reguł z EPG.")
        except Exception as e:
            print(f"   Błąd parsowania XML EPG: {e}")

    return mapa_epg


def dopasuj_epg_fuzzy(
    nazwa_stacji: str, mapa_epg: dict[str, str], min_podobienstwo: float = 0.60
) -> str | None:
    """Szuka najlepszego dopasowania nazwy w bazie EPG."""
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


def czy_kanal_jest_polski(
    extinf: str, epg_id: str | None, jest_pl_source: bool
) -> bool:
    """Weryfikuje polskojęzyczny charakter stacji."""
    if jest_pl_source:
        return True

    extinf_lower = extinf.lower()
    if (
        'tvg-language="pol"' in extinf_lower
        or 'tvg-country="pl"' in extinf_lower
    ):
        return True

    if epg_id is not None:
        return True

    return False


async def przetworz_liste_async():
    """Główna pętla asynchroniczna pobierająca, testująca i zapisująca dane."""
    # Tworzymy pojedynczą sesję HTTP dla całego programu
    async with aiohttp.ClientSession() as session:
        mapa_epg = await pobierz_baze_epg(session)

        print("2. Pobieranie list M3U z internetu...")
        surowe_kanaly = []
        unikalne_urle = set()

        for zrodlo_url in INDEKSY_ZRODEL:
            m3u_text = await pobierz_tekst_async(session, zrodlo_url)
            if not m3u_text:
                continue

            jest_pl_source = any(
                kraj in zrodlo_url.lower()
                for kraj in ["pol.m3u", "pl.m3u", "poland"]
            )
            linie = m3u_text.splitlines()

            i = 0
            while i < len(linie):
                linia = linie[i].strip()
                if linia.startswith("#EXTINF:"):
                    if i + 1 < len(linie) and not linie[i + 1].startswith("#"):
                        stream_url = linie[i + 1].strip()
                        if stream_url not in unikalne_urle:
                            unikalne_urle.add(stream_url)
                            surowe_kanaly.append(
                                (linia, stream_url, jest_pl_source)
                            )
                        i += 1
                i += 1

        print(
            f"3. Szybkie asynchroniczne testowanie {len(surowe_kanaly)} stacji..."
        )

        # Semafory ograniczają liczbę jednocześnie otwartych połączeń do 40, aby nie obciążać łącza
        semaphore = asyncio.Semaphore(40)

        # Tworzymy listę asynchronicznych zadań
        zadania_testow = [
            sprawdz_stream_async(session, semaphore, url)
            for _, url, _ in surowe_kanaly
        ]

        # Wykonujemy wszystkie testy jednocześnie
        wyniki_testow = await asyncio.gather(*zadania_testow)

        finalne_stacje = []
        zobaczone_kanaly = set()

        for (extinf, stream_url, jest_pl_source), dziala in zip(
            surowe_kanaly, wyniki_testow
        ):
            nazwa_kanalu = extinf.rsplit(",", 1)[-1].strip()

            if dziala:
                epg_id = dopasuj_epg_fuzzy(
                    nazwa_kanalu, mapa_epg, min_podobienstwo=0.60
                )

                if czy_kanal_jest_polski(extinf, epg_id, jest_pl_source):
                    klucz_kanalu = (
                        epg_id if epg_id else normalizuj_nazwe(nazwa_kanalu)
                    )

                    if klucz_kanalu in zobaczone_kanaly:
                        continue

                    zobaczone_kanaly.add(klucz_kanalu)

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

        # Zapis do pliku końcowego
        zapis_linie = ['#EXTM3U url-tvg="https://epg.ovh/pl.xml"']
        for extinf, url in finalne_stacje:
            zapis_linie.append(extinf)
            zapis_linie.append(url)

        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(zapis_linie))

        print(
            f"\nSukces! Zapisano {len(finalne_stacje)} unikalnych polskich"
            f" stacji do {OUTPUT_FILE}"
        )


if __name__ == "__main__":
    # Uruchomienie głównej pętli zdarzeń asyncio
    asyncio.run(przetworz_liste_async())
