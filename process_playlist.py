import asyncio
from difflib import SequenceMatcher
import re
import time
import xml.etree.ElementTree as ET
import aiohttp

# Źródła M3U/M3U8 podzielone na polskie (przepuszczane) oraz międzynarodowe (filtrowane)
INDEKSY_ZRODEL = [
    # (Adres URL, czy_zrodlo_jest_domyslnie_polskie)
    ("https://iptv-org.github.io/iptv/languages/pol.m3u", True),
    ("https://iptv-org.github.io/iptv/countries/pl.m3u", True),
    (
        "https://raw.githubusercontent.com/Free-TV/IPTV/refs/heads/master/playlists/playlist_poland.m3u8",
        True,
    ),
    (
        "https://raw.githubusercontent.com/Romaxa55/world_ip_tv/refs/heads/master/output/pl.m3u",
        True,
    ),
    # Kategorie tematyczne (wymagają polskiego EPG lub tagu tvg-language/tvg-country)
    ("https://iptv-org.github.io/iptv/categories/movies.m3u", False),
    ("https://iptv-org.github.io/iptv/categories/science.m3u", False),
    ("https://iptv-org.github.io/iptv/categories/news.m3u", False),
]

EPG_URL = "https://epg.ovh/pl.xml"
OUTPUT_FILE = "gracy.m3u"

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Kodi/21.0"}

# Priorytet MUX Naziemnej Telewizji Cyfrowej (Wrocław i MUX L4)
KOLEJNOSC_MUX_WROCLAW = [
    "tvp1",
    "tvp2",
    "tvp3wroclaw",
    "tvp3",
    "polsat",
    "tvn",
    "tv4",
    "tvpuls",
    "tvn7",
    "puls2",
    "tv6",
    "superpolsat",
    "tvpinfo",
    "tvpsport",
    "tvpkultura",
    "tvphistoria",
    "tvpabc",
    "tvprozrywka",
    "tvpdokument",
    "tvpnauka",
    "tvppolonia",
    "tvpworld",
    "eskatv",
    "ttv",
    "polotv",
    "antenahd",
    "antena",
    "tvtrwam",
    "stopklatka",
    "focustv",
    "wydarzenia24",
    "metro",
    "zoomtv",
    "nowatv",
    "wp",
    "echo24",
    "starstv",
    "polsatnews2",
]


def normalizuj_nazwe(nazwa: str) -> str:
    """Czyszczenie nazwy kanału do porównań i sortowania."""
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


MAPA_MUX = {
    normalizuj_nazwe(nazwa): idx
    for idx, nazwa in enumerate(KOLEJNOSC_MUX_WROCLAW)
}


async def pobierz_tekst_async(
    session: aiohttp.ClientSession, url: str, timeout: float = 15.0
) -> str:
    """Asynchroniczne pobieranie zawartości tekstowej z podanego URL."""
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


async def mierzenie_predkosci_streamu(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    url: str,
    timeout: float = 4.0,
) -> tuple[bool, float]:
    """Testowanie dostępności i pomiar prędkości transferu strumienia (KB/s)."""
    async with semaphore:
        start_time = time.time()
        try:
            naglowki = {**HEADERS, "Range": "bytes=0-131072"}
            async with session.get(
                url,
                headers=naglowki,
                timeout=aiohttp.ClientTimeout(total=timeout),
                ssl=False,
            ) as response:
                if response.status in (200, 206):
                    dane = await response.read()
                    czas_pobierania = time.time() - start_time
                    if czas_pobierania > 0 and len(dane) > 0:
                        predkosc_kbps = (len(dane) / 1024) / czas_pobierania
                        return True, round(predkosc_kbps, 2)
                    return True, 1.0
        except Exception:
            pass
        return False, 0.0


async def pobierz_baze_epg(session: aiohttp.ClientSession) -> dict[str, str]:
    """Pobieranie przewodnika programowego EPG XML z epg.ovh."""
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
    """Szukanie pasującego identyfikatora EPG."""
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
    """Weryfikuje, czy dany kanał kwalifikuje się jako polskojęzyczny."""
    # 1. Przepuszczamy kanały pochodzące ze stricte polskich playlist
    if jest_pl_source:
        return True

    extinf_lower = extinf.lower()

    # 2. Sprawdzamy obecność tagów językowych/krajowych w nagłówku M3U
    if (
        'tvg-language="pol"' in extinf_lower
        or 'tvg-country="pl"' in extinf_lower
    ):
        return True

    # 3. Przepuszczamy kanał z kategorii ogólnej TYLKO WTEDY, gdy pasuje do polskiego EPG z epg.ovh
    if epg_id is not None:
        return True

    return False


def pobierz_prio_mux(nazwa_kanalu: str, epg_id: str | None) -> int:
    """Określanie priorytetu pozycji na liście wg MUX Wrocław."""
    norm_nazwa = normalizuj_nazwe(nazwa_kanalu)
    if norm_nazwa in MAPA_MUX:
        return MAPA_MUX[norm_nazwa]

    if epg_id:
        norm_epg = normalizuj_nazwe(epg_id)
        if norm_epg in MAPA_MUX:
            return MAPA_MUX[norm_epg]

    return 9999


async def przetworz_liste_async():
    """Główna logika przetwarzania playlist."""
    async with aiohttp.ClientSession() as session:
        mapa_epg = await pobierz_baze_epg(session)

        print("2. Pobieranie list M3U/M3U8...")
        surowe_kanaly = []
        unikalne_urle = set()

        for zrodlo_url, jest_pl_source in INDEKSY_ZRODEL:
            m3u_text = await pobierz_tekst_async(session, zrodlo_url)
            if not m3u_text:
                continue

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
            f"3. Testowanie i pomiar prędkości dla {len(surowe_kanaly)} strumieni..."
        )

        semaphore = asyncio.Semaphore(35)
        zadania_testow = [
            mierzenie_predkosci_streamu(session, semaphore, url)
            for _, url, _ in surowe_kanaly
        ]

        wyniki_testow = await asyncio.gather(*zadania_testow)

        grupy_stacji: dict[str, list[tuple[float, str, str, str | None]]] = {}

        for (extinf, stream_url, jest_pl_source), (dziala, predkosc_kbps) in zip(
            surowe_kanaly, wyniki_testow
        ):
            if dziala:
                nazwa_kanalu = extinf.rsplit(",", 1)[-1].strip()
                epg_id = dopasuj_epg_fuzzy(
                    nazwa_kanalu, mapa_epg, min_podobienstwo=0.60
                )

                # KLUCZOWY FILTR JĘZYKOWY
                if not czy_kanal_jest_polski(extinf, epg_id, jest_pl_source):
                    continue

                klucz_stacji = (
                    epg_id if epg_id else normalizuj_nazwe(nazwa_kanalu)
                )

                if klucz_stacji not in grupy_stacji:
                    grupy_stacji[klucz_stacji] = []

                grupy_stacji[klucz_stacji].append(
                    (predkosc_kbps, extinf, stream_url, epg_id)
                )

        print(f"4. Wybór najszybszych wariantów dla {len(grupy_stacji)} stacji...")
        wybrane_stacje = []

        for klucz_stacji, warianty in grupy_stacji.items():
            warianty.sort(key=lambda x: x[0], reverse=True)
            najszybszy = warianty[0]

            predkosc, extinf, stream_url, epg_id = najszybszy
            nazwa_kanalu = extinf.rsplit(",", 1)[-1].strip()

            if epg_id:
                if 'tvg-id="' in extinf:
                    extinf = re.sub(
                        r'tvg-id="[^"]*"', f'tvg-id="{epg_id}"', extinf
                    )
                else:
                    extinf = extinf.replace(
                        "#EXTINF:-1", f'#EXTINF:-1 tvg-id="{epg_id}"'
                    )

            prio_mux = pobierz_prio_mux(nazwa_kanalu, epg_id)
            wybrane_stacje.append(
                (prio_mux, nazwa_kanalu.lower(), extinf, stream_url, predkosc)
            )

        wybrane_stacje.sort(key=lambda x: (x[0], x[1]))

        zapis_linie = ['#EXTM3U url-tvg="https://epg.ovh/pl.xml"']
        for prio, nazwa, extinf, url, predkosc in wybrane_stacje:
            zapis_linie.append(extinf)
            zapis_linie.append(url)

        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(zapis_linie))

        print(
            f"\nSukces! Zapisano {len(wybrane_stacje)} czystych polskich stacji do"
            f" {OUTPUT_FILE}."
        )


if __name__ == "__main__":
    asyncio.run(przetworz_liste_async())
