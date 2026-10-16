#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
process_playlist.py

Zbiera polskojęzyczne kanały z kilku publicznych playlist M3U/M3U8 oraz z
oficjalnego serwisu TVP, dopasowuje do nich EPG z epg.ovh, sprawdza czy
strumienie faktycznie działają (i z jaką przepustowością) oraz zapisuje wynik
do gracy.m3u w kolejności odpowiadającej MUX-om naziemnym we Wrocławiu.

Względem wersji pierwotnej poprawiono m.in.:
  * normalizację nazw - uwzględnia znaki diakrytyczne (ł, ó, ę, ą, ż, ź, ć, ń),
    dzięki czemu "TVP3 Wrocław" trafia w priorytet MUX,
  * parser M3U - nie gubi kanałów, po których #EXTINF stoi #EXTVLCOPT/#KODIPROP,
    a nazwę kanału bierze po pierwszym przecinku POZA cudzysłowami (wcześniej
    przecinek z atrybutu http-user-agent ucinał nazwę i psuł EPG oraz kolejność),
  * EPG (~70 MB) - strumieniowe parsowanie (iterparse + clear), limit rozmiaru,
    dłuższy timeout, walidacja; skrypt nie zapisuje pustej listy,
  * dopasowanie EPG - najpierw tvg-id ze źródła, potem dopasowanie dokładne,
    na końcu fuzzy z wysokim progiem i ochroną przed niejednoznacznością
    (koniec sklejania np. "BabyFirst" z "BBC First"),
  * realny test strumienia - pobierany jest segment HLS (nie sama playlista!),
    mierzona jest przepustowość, nieudane próby są powtarzane,
  * oficjalne źródło TVP (serwis TVP Stream / TVP VOD) - kanały TVP Dokument
    i TVP Nauka, których nie ma w innych playlistach; adresy TVP są tokenowe
    i krótkotrwałe, więc manifest pobierany jest na świeżo przy każdym
    uruchomieniu (identyfikatory kanałów są w konfiguracji niżej),
  * limit połączeń globalnie i per-host, TLS weryfikowany,
  * tryb lokalny: uruchomienie z GRACY_PUSH=1 wgrywa gotową listę na GitHub przez
    API (token w GITHUB_TOKEN albo w pliku github_token.txt) - to pozwala tworzyć
    listę z polskiego adresu IP, bo TVP wydaje adresy strumieni tylko dla Polski,
  * zapis atomowy, sortowanie deterministyczne, nagłówek url-tvg + x-tvg-url.

Wymaga: Python 3.9+ oraz aiohttp.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import ssl
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from dataclasses import dataclass
from difflib import SequenceMatcher
from urllib.parse import urljoin, urlsplit

import aiohttp

log = logging.getLogger("gracy")

# ---------------------------------------------------------------------------
# Konfiguracja
# ---------------------------------------------------------------------------

# Źródła M3U/M3U8: (adres, czy źródło jest z założenia polskojęzyczne)
INDEKSY_ZRODEL: list[tuple[str, bool]] = [
    ("https://iptv-org.github.io/iptv/languages/pol.m3u", True),
    ("https://iptv-org.github.io/iptv/countries/pl.m3u", True),
    (
        "https://raw.githubusercontent.com/Free-TV/IPTV/refs/heads/master/"
        "playlists/playlist_poland.m3u8",
        True,
    ),
    (
        "https://raw.githubusercontent.com/Romaxa55/world_ip_tv/refs/heads/"
        "master/output/pl.m3u",
        True,
    ),
]

EPG_URL = "https://epg.ovh/pl.xml"
OUTPUT_FILE = os.environ.get("GRACY_OUTPUT", "gracy.m3u")

# --- Oficjalne źródło TVP (serwis TVP Stream / TVP VOD) --------------------
# Każdy kanał ma w serwisie TVP identyfikator "video_id". Adres manifestu jest
# tokenowy i krótkotrwały (token zawiera datę), więc pobieramy go na świeżo przy
# każdym uruchomieniu: video_id -> TVPlayer2 API -> adres master.m3u8.
# Sprawdzone: strumienie są bez DRM, ale TVP wydaje adresy tylko dla polskich
# adresów IP - z zagranicy odpowiedź API nie zawiera adresu manifestu.
KANALY_TVP: list[tuple[str, str]] = [
    # kanały, których nie ma w innych źródłach
    ("TVP Dokument", "53795159"),
    ("TVP Nauka", "71345993"),
]

# Pozostałe oficjalne kanały TVP - świadomie nieużywane, bo są już w innych
# źródłach, a ich tokeny wygasają (mogłyby wypaść między uruchomieniami):
KANALY_TVP_ZAPASOWE: list[tuple[str, str]] = [
    ("TVP 1", "51689486"),
    ("TVP 2", "51696811"),
    ("TVP Info", "51696820"),
    ("TVP Sport", "51696827"),
    ("TVP Rozrywka", "51696825"),
    ("TVP Historia", "51696819"),
]

TVP_CONFIG_URL = (
    "https://www.tvp.pl/sess/TVPlayer2/api.php?"
    "id={video_id}&@method=getTvpConfig&@callback=cb"
)
TVP_TIMEOUT = 15.0
TVP_PROBY = 3                # API TVP czasem odpowiada bez adresu - ponawiamy

# --- Wgrywanie wyniku na GitHub (tryb lokalny) -----------------------------
# Uruchomienie z GRACY_PUSH=1 powoduje, że po zapisaniu gracy.m3u skrypt wgrywa
# go na GitHub przez API. Token bierzemy ze zmiennej GITHUB_TOKEN albo z pliku
# github_token.txt obok skryptu. Dzięki temu lista może być generowana z polskiego
# IP (TVP wydaje adresy strumieni tylko dla adresów z Polski), a runner GitHuba
# działa jako zapas.
GITHUB_REPO = os.environ.get("GRACY_GITHUB_REPO", "gracy72/kodi-iptv")
GITHUB_PATH = os.environ.get("GRACY_GITHUB_PATH", "gracy.m3u")
GITHUB_BRANCH = os.environ.get("GRACY_GITHUB_BRANCH", "main")
PLIK_TOKENU = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "github_token.txt"
)

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Kodi/21.0"}

HTTP_TIMEOUT = 20.0          # pobieranie playlist źródłowych
EPG_TIMEOUT = 300.0          # pobieranie EPG (~70 MB)
EPG_SOCK_READ = 120.0
EPG_MAX_BYTES = 300 * 1024 * 1024
STREAM_TIMEOUT = 8.0         # pojedynczy test strumienia
PROBY_STRUMIENIA = 3         # ile razy próbować, gdy strumień nie odpowiedział

MAX_ROWNOLOGE = 40           # ile strumieni testować równolegle
MAX_NA_HOST = 4              # ile równolegle do jednego serwera (serwery IPTV
                             #   odrzucają część żądań przy większym tłoku)

MAX_SEGMENT_BYTES = 512 * 1024   # ile bajtów strumienia pobrać do pomiaru
MIN_SEGMENT_BYTES = 16 * 1024    # poniżej tego uznajemy, że nic nie działa
MAX_PLAYLIST_BYTES = 512 * 1024  # bezpiecznik na playlistę HLS
MAX_PLAYLIST_ZRODLA = 8 * 1024 * 1024  # bezpiecznik na playlistę źródłową

FUZZY_PROG = 0.90            # próg podobieństwa dla dopasowania EPG
FUZZY_MARGINES = 0.05        # gdy dwa kanały EPG są bliżej - nie zgadujemy

# 0 = testuj wszystkie; ustaw np. GRACY_LIMIT=40 do szybkich prób
try:
    LIMIT_TESTU = int(os.environ.get("GRACY_LIMIT", "0") or "0")
except ValueError:
    LIMIT_TESTU = 0

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

# Kolejność nieokreślonych stacji na końcu listy
PRIO_POZA_MUX = 9999

# Warianty zapisu nazw, które po normalizacji nie trafiają wprost w listę MUX
ALIASY_MUX = {
    "stopklatkatv": "stopklatka",
    "fokustv": "focustv",
    "tvecho24": "echo24",
}

# ---------------------------------------------------------------------------
# Normalizacja nazw
# ---------------------------------------------------------------------------

# Uwaga: "polska"/"polski"/"polskie" NIE są usuwane - występują w nazwach
# kanałów (np. "Kino Polska"), a nie tylko jako znacznik kraju.
_USUN_TOKENY = re.compile(
    r"\b(hd|fhd|uhd|4k|8k|sd|pl|poland|"
    r"1080p|1080i|720p|576p|576i|480p|360p|stream|live|backup|"
    r"hevc|h265|h264|hls|dash|m3u8)\b"
)


def normalizuj_nazwe(nazwa: str | None) -> str:
    """Czyszczenie nazwy kanału do porównań, grupowania i sortowania.

    Usuwa znaki diakrytyczne (ł -> l, ó -> o, ...), znaczniki jakości
    (HD, FHD, 1080p), kody kraju (PL) i interpunkcję.
    """
    if not nazwa:
        return ""
    tekst = unicodedata.normalize("NFKD", nazwa.casefold())
    # NFKD nie rozkłada "ł", więc podmieniamy jawnie
    tekst = tekst.replace("\u0142", "l")
    tekst = "".join(znak for znak in tekst if not unicodedata.combining(znak))
    tekst = _USUN_TOKENY.sub(" ", tekst)
    tekst = re.sub(r"[^\w\s]", " ", tekst, flags=re.UNICODE)
    return "".join(tekst.split())


def _zbuduj_mape_mux() -> dict[str, int]:
    mapa: dict[str, int] = {}
    for indeks, nazwa in enumerate(KOLEJNOSC_MUX_WROCLAW):
        klucz = normalizuj_nazwe(nazwa)
        if not klucz:
            continue
        if klucz in mapa:
            log.warning("MUX: zduplikowany klucz %r (pozycje %d i %d)",
                        klucz, mapa[klucz], indeks)
            continue
        mapa[klucz] = indeks
    return mapa


MAPA_MUX = _zbuduj_mape_mux()

# ---------------------------------------------------------------------------
# Model danych
# ---------------------------------------------------------------------------


@dataclass
class KanalZrodlowy:
    """Pojedynczy wpis wyciągnięty z playlisty źródłowej."""

    extinf: str
    url: str
    nazwa: str
    tvg_id: str | None
    zrodlo_pl: bool


@dataclass
class WybranyKanal:
    """Kanał, który trafi do pliku wynikowego."""

    prio: int
    klucz_nazwy: str
    extinf: str
    url: str
    kbps: float


# ---------------------------------------------------------------------------
# Parsowanie playlist M3U
# ---------------------------------------------------------------------------

_RE_TVG_ID = re.compile(r'tvg-id="([^"]*)"', re.IGNORECASE)
_RE_TVP_M3U8 = re.compile(r'https?://\S+master\.m3u8')


def _nazwa_z_extinf(extinf: str) -> str:
    """Zwraca nazwę kanału, czyli tekst po PIERWSZYM przecinku POZA cudzysłowami.

    Przecinki w wartościach atrybutów (np. http-user-agent="... KHTML, like
    Gecko) ...") są w cudzysłowach, więc ich nie bierzemy; przecinki w samej
    nazwie kanału zostają.
    """
    w_cudzyslowie = False
    for indeks, znak in enumerate(extinf):
        if znak == '"':
            w_cudzyslowie = not w_cudzyslowie
        elif znak == "," and not w_cudzyslowie:
            return extinf[indeks + 1:].strip()
    return ""


def _tvg_id_z_extinf(extinf: str) -> str | None:
    dopasowanie = _RE_TVG_ID.search(extinf)
    if not dopasowanie:
        return None
    wartosc = dopasowanie.group(1).strip()
    return wartosc or None


def parsuj_playliste(
    tekst: str, zrodlo_pl: bool, widziane_url: set[str]
) -> list[KanalZrodlowy]:
    """Wyciąga kanały z playlisty M3U.

    URL bierzemy z pierwszej niekomentarzowej linii po #EXTINF, dzięki czemu
    kanały z #EXTVLCOPT / #KODIPROP / #EXTGRP nie są gubione.
    """
    kanaly: list[KanalZrodlowy] = []
    extinf: str | None = None

    for surowa in tekst.splitlines():
        linia = surowa.strip()
        if not linia:
            continue
        if linia.startswith("#EXTINF"):
            extinf = linia
            continue
        if linia.startswith("#"):
            # #EXTM3U, #EXTVLCOPT, #KODIPROP, #EXTGRP ... - nie przerywają kanału
            continue
        if extinf is None:
            continue  # adres bez metadanych - pomijamy
        if linia in widziane_url:
            extinf = None
            continue
        widziane_url.add(linia)
        kanaly.append(
            KanalZrodlowy(
                extinf=extinf,
                url=linia,
                nazwa=_nazwa_z_extinf(extinf),
                tvg_id=_tvg_id_z_extinf(extinf),
                zrodlo_pl=zrodlo_pl,
            )
        )
        extinf = None

    return kanaly


# ---------------------------------------------------------------------------
# EPG: pobranie i dopasowanie
# ---------------------------------------------------------------------------


def _parsuj_epg(sciezka: str) -> dict[str, str]:
    """Strumieniowo czyta XML EPG - trzyma w pamięci tylko mapę kanałów."""
    mapa: dict[str, str] = {}
    korzen = None

    for zdarzenie, element in ET.iterparse(sciezka, events=("start", "end")):
        if zdarzenie == "start":
            if korzen is None:
                korzen = element
            continue

        if element.tag == "channel":
            channel_id = element.get("id")
            if channel_id:
                klucz = normalizuj_nazwe(channel_id)
                if klucz:
                    mapa.setdefault(klucz, channel_id)
                for nazwa in element.findall("display-name"):
                    if nazwa.text:
                        klucz = normalizuj_nazwe(nazwa.text)
                        if klucz:
                            mapa.setdefault(klucz, channel_id)
            element.clear()
            if korzen is not None:
                korzen.clear()  # zwalnia też <programme> z pamięci
        elif element.tag == "programme":
            element.clear()
            if korzen is not None:
                korzen.clear()

    return mapa


async def pobierz_baze_epg(session: aiohttp.ClientSession) -> dict[str, str]:
    """Pobiera EPG na dysk i buduje mapę nazwa -> id kanału w EPG."""
    log.info("1. Pobieranie bazy EPG z %s ...", EPG_URL)
    limit = aiohttp.ClientTimeout(
        total=EPG_TIMEOUT, connect=20.0, sock_read=EPG_SOCK_READ
    )
    sciezka: str | None = None
    try:
        deskryptor, sciezka = tempfile.mkstemp(prefix="epg_", suffix=".xml")
        os.close(deskryptor)

        pobrane = 0
        async with otworz(session, EPG_URL, timeout=limit) as odpowiedz:
            if odpowiedz.status != 200:
                log.warning("EPG: HTTP %s - pomijam EPG", odpowiedz.status)
                return {}
            with open(sciezka, "wb") as plik:
                async for fragment in odpowiedz.content.iter_chunked(1 << 16):
                    plik.write(fragment)
                    pobrane += len(fragment)
                    if pobrane > EPG_MAX_BYTES:
                        log.warning(
                            "EPG: przekroczono limit %d MB - przerywam",
                            EPG_MAX_BYTES // (1024 * 1024),
                        )
                        break

        if pobrane == 0:
            log.warning("EPG: puste odpowiedzi - pomijam EPG")
            return {}

        mapa = _parsuj_epg(sciezka)
        log.info("   EPG: %.1f MB, %d reguł dopasowania.", pobrane / 1e6, len(mapa))
        if len(mapa) < 50:
            log.warning("EPG: podejrzanie mało reguł - wynik może być bez tvg-id")
        return mapa
    except Exception as blad:  # noqa: BLE001 - EPG jest opcjonalne
        log.warning("EPG: nie udało się pobrać/przetworzyć (%s)", blad)
        return {}
    finally:
        if sciezka:
            try:
                os.unlink(sciezka)
            except OSError:
                pass


def _dopasuj_fuzzy(norm: str, mapa_epg: dict[str, str]) -> str | None:
    """Dopasowanie rozmyte z prefiltrem i ochroną przed niejednoznacznością."""
    wyniki: dict[str, float] = {}
    dlugosc = len(norm)
    pierwszy = norm[0]

    for kandydat, epg_id in mapa_epg.items():
        if not kandydat or kandydat[0] != pierwszy:
            continue
        if abs(len(kandydat) - dlugosc) > max(4, dlugosc // 2):
            continue
        podobienstwo = SequenceMatcher(None, norm, kandydat).ratio()
        if podobienstwo >= FUZZY_PROG and podobienstwo > wyniki.get(epg_id, 0.0):
            wyniki[epg_id] = podobienstwo

    if not wyniki:
        return None

    ranking = sorted(wyniki.items(), key=lambda para: para[1], reverse=True)
    if len(ranking) > 1 and (ranking[0][1] - ranking[1][1]) < FUZZY_MARGINES:
        # dwa różne kanały EPG pasują podobnie - nie zgadujemy
        log.debug("EPG: niejednoznaczne dopasowanie %r (%s)", norm, ranking[:3])
        return None
    return ranking[0][0]


def dopasuj_epg(kanal: KanalZrodlowy, mapa_epg: dict[str, str]) -> str | None:
    """Zwraca id kanału w EPG albo None."""
    if not mapa_epg:
        return None

    # 1. tvg-id z playlisty źródłowej (najwyższa pewność)
    if kanal.tvg_id:
        klucze = [kanal.tvg_id]
        baza = kanal.tvg_id.split("@", 1)[0]
        baza = baza.split(".", 1)[0]
        if baza and baza != kanal.tvg_id:
            klucze.append(baza)

        for klucz in klucze:
            znormalizowany = normalizuj_nazwe(klucz)
            if znormalizowany and znormalizowany in mapa_epg:
                return mapa_epg[znormalizowany]

    # 2. dopasowanie dokładne po nazwie wyświetlanej
    znormalizowana = normalizuj_nazwe(kanal.nazwa)
    if not znormalizowana:
        return None
    if znormalizowana in mapa_epg:
        return mapa_epg[znormalizowana]

    # 3. dopasowanie rozmyte
    return _dopasuj_fuzzy(znormalizowana, mapa_epg)


def czy_kanal_jest_polski(extinf: str, epg_id: str | None) -> bool:
    """Weryfikacja przynależności kanału do polskojęzycznych."""
    dolny = extinf.lower()
    if 'tvg-language="pol"' in dolny or 'tvg-country="pl"' in dolny:
        return True
    return epg_id is not None


def pobierz_prio_mux(nazwa: str, epg_id: str | None) -> int:
    """Priorytet kanału wg kolejności MUX Wrocław."""
    for kandydat in (nazwa, epg_id):
        if not kandydat:
            continue
        klucz = normalizuj_nazwe(kandydat)
        klucz = ALIASY_MUX.get(klucz, klucz)
        if klucz in MAPA_MUX:
            return MAPA_MUX[klucz]
    return PRIO_POZA_MUX


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


@asynccontextmanager
async def otworz(
    session: aiohttp.ClientSession,
    url: str,
    dodatkowe: dict[str, str] | None = None,
    timeout: aiohttp.ClientTimeout | None = None,
):
    """Otwiera odpowiedź HTTP; przy błędzie certyfikatu ponawia bez weryfikacji."""
    naglowki = {**HEADERS, **(dodatkowe or {})}
    limit = timeout or aiohttp.ClientTimeout(total=HTTP_TIMEOUT)

    try:
        odpowiedz = await session.get(url, headers=naglowki, timeout=limit)
    except (
        ssl.SSLError,
        aiohttp.ClientSSLError,
        aiohttp.ClientConnectorCertificateError,
    ) as blad:
        log.debug("TLS: %s (%s) - ponawiam bez weryfikacji certyfikatu", url, blad)
        odpowiedz = await session.get(
            url, headers=naglowki, timeout=limit, ssl=False
        )

    try:
        yield odpowiedz
    finally:
        odpowiedz.close()


async def pobierz_tekst(
    session: aiohttp.ClientSession, url: str, timeout: float = HTTP_TIMEOUT
) -> str:
    """Pobiera tekst (playlista M3U/M3U8).

    Uwaga: używamy iter_chunked, a nie content.read(n) - w aiohttp 3.x
    read(n) zwraca tylko to, co akurat dotarło do bufora, czyli obcina plik.
    """
    limit = aiohttp.ClientTimeout(total=timeout, connect=min(10.0, timeout))
    try:
        async with otworz(session, url, timeout=limit) as odpowiedz:
            if odpowiedz.status != 200:
                log.warning("HTTP %s dla %s", odpowiedz.status, url)
                return ""
            dane = bytearray()
            async for fragment in odpowiedz.content.iter_chunked(1 << 16):
                dane += fragment
                if len(dane) > MAX_PLAYLIST_ZRODLA:
                    log.warning("Playlista %s przekracza limit rozmiaru", url)
                    break
            return bytes(dane).decode("utf-8", "ignore")
    except Exception as blad:  # noqa: BLE001
        log.warning("Błąd pobierania %s: %s", url, blad)
        return ""


async def pobierz_kanaly_tvp(session: aiohttp.ClientSession) -> list[KanalZrodlowy]:
    """Zamienia identyfikatory TVP na świeże adresy oficjalnych manifestów HLS.

    TVP wydaje tokenowe adresy (token zawiera datę), więc nie da się ich
    wpisać na stałe do playlisty - trzeba je pobrać przy każdym uruchomieniu.
    """
    if not KANALY_TVP:
        return []
    log.info("   TVP: pobieranie oficjalnych adresów (%d kanałów)...", len(KANALY_TVP))
    kanaly: list[KanalZrodlowy] = []
    for nazwa, video_id in KANALY_TVP:
        adres_api = TVP_CONFIG_URL.format(video_id=video_id)
        tekst = ""
        for proba in range(TVP_PROBY):
            tekst = await pobierz_tekst(session, adres_api, TVP_TIMEOUT)
            if tekst and _RE_TVP_M3U8.search(tekst.replace(r"\/", "/")):
                break
            if proba + 1 < TVP_PROBY:
                log.debug("   TVP %s: brak manifestu (próba %d) - ponawiam",
                          nazwa, proba + 1)
                await asyncio.sleep(1.5 * (proba + 1))
        if not tekst:
            log.warning("   TVP %s: brak odpowiedzi API", nazwa)
            continue
        adresy = sorted(set(_RE_TVP_M3U8.findall(tekst.replace(r"\/", "/"))))
        if not adresy:
            log.warning("   TVP %s: brak adresu manifestu w odpowiedzi API", nazwa)
            continue
        kanaly.append(
            KanalZrodlowy(
                extinf=(
                    f'#EXTINF:-1 tvg-id="{nazwa}" '
                    f'group-title="TVP (oficjalny)",{nazwa}'
                ),
                url=adresy[0],
                nazwa=nazwa,
                tvg_id=nazwa,
                zrodlo_pl=True,
            )
        )
        log.debug("   TVP %s -> %s", nazwa, adresy[0])
    log.info("   TVP: gotowych adresów: %d/%d", len(kanaly), len(KANALY_TVP))
    return kanaly


# ---------------------------------------------------------------------------
# Test strumienia
# ---------------------------------------------------------------------------


async def _pobierz_probke(
    session: aiohttp.ClientSession, url: str, timeout: float, limit_bajtow: int
) -> tuple[bool, float, bytes, str]:
    """Pobiera ograniczoną porcję danych i liczy przepustowość (kbit/s).

    Zwraca (czy_cokolwiek_pobrano, kbit/s, dane, content-type).
    """
    limit = aiohttp.ClientTimeout(
        total=timeout, connect=min(6.0, timeout), sock_read=timeout
    )
    start = time.monotonic()
    dane = bytearray()
    typ = ""
    try:
        async with otworz(session, url, timeout=limit) as odpowiedz:
            typ = (odpowiedz.headers.get("Content-Type") or "").lower()
            if odpowiedz.status not in (200, 206):
                return False, 0.0, b"", typ
            if "html" in typ:
                return False, 0.0, b"", typ

            async for fragment in odpowiedz.content.iter_chunked(32768):
                dane += fragment
                if len(dane) >= limit_bajtow:
                    break
                if time.monotonic() - start >= timeout * 0.75:
                    break
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ssl.SSLError) as blad:
        log.debug("Test %s nieudany: %s", url, blad)
        return False, 0.0, bytes(dane), typ

    if not dane:
        return False, 0.0, b"", typ

    czas = max(time.monotonic() - start, 1e-3)
    kbps = (len(dane) * 8 / 1000.0) / czas
    return True, round(kbps, 1), bytes(dane), typ


def _czy_hls(url: str, typ: str, dane: bytes) -> bool:
    if urlsplit(url).path.lower().endswith(".m3u8"):
        return True
    if "mpegurl" in typ:
        return True
    return dane[:512].lstrip().lower().startswith(b"#extm3u")


def _wybierz_wariant(tekst: str, baza: str) -> tuple[str, int] | None:
    """Z playlisty głównej HLS wybiera wariant o najwyższym BANDWIDTH."""
    linie = [linia.strip() for linia in tekst.splitlines()]
    najlepszy: tuple[str, int] | None = None

    for indeks, linia in enumerate(linie):
        if not linia.upper().startswith("#EXT-X-STREAM-INF"):
            continue
        pasuje = re.search(r"BANDWIDTH=(\d+)", linia, re.IGNORECASE)
        bandwidth = int(pasuje.group(1)) if pasuje else 0

        kolejny = indeks + 1
        while kolejny < len(linie) and (
            not linie[kolejny] or linie[kolejny].startswith("#")
        ):
            kolejny += 1
        if kolejny >= len(linie):
            continue

        if najlepszy is None or bandwidth > najlepszy[1]:
            najlepszy = (urljoin(baza, linie[kolejny]), bandwidth)

    return najlepszy


def _wybierz_segment(tekst: str, baza: str) -> str | None:
    """Z playlisty mediów HLS wybiera adres segmentu do pomiaru."""
    adresy: list[str] = []
    for linia in tekst.splitlines():
        linia = linia.strip()
        if not linia or linia.startswith("#"):
            continue
        adresy.append(urljoin(baza, linia))

    if not adresy:
        return None
    # pierwszy segment bywa jeszcze zapisywany na serwerze - bierzemy kolejny
    return adresy[1] if len(adresy) > 1 else adresy[0]


async def _sprawdz_hls(
    session: aiohttp.ClientSession, url: str, tekst: str, timeout: float
) -> tuple[bool, float]:
    """Mierzy realną przepustowość pobierając segment HLS, nie playlistę."""
    if "#EXTM3U" not in tekst[:8192]:
        return False, 0.0

    baza = url
    wymagane_bps: int | None = None

    if "#EXT-X-STREAM-INF" in tekst:
        wariant = _wybierz_wariant(tekst, baza)
        if wariant is None:
            return False, 0.0
        adres_wariantu, wymagane_bps = wariant
        pobrano, _, dane, _ = await _pobierz_probke(
            session, adres_wariantu, timeout, MAX_PLAYLIST_BYTES
        )
        if not dane:
            return False, 0.0
        tekst = dane.decode("utf-8", "ignore")
        baza = adres_wariantu

    segment = _wybierz_segment(tekst, baza)
    if segment is None:
        log.debug("HLS: brak segmentów w %s", url)
        return False, 0.0

    pobrano, kbps, dane, _ = await _pobierz_probke(
        session, segment, timeout, MAX_SEGMENT_BYTES
    )
    if not pobrano or len(dane) < MIN_SEGMENT_BYTES:
        return False, 0.0

    if wymagane_bps and kbps * 1000 < wymagane_bps * 0.7:
        log.debug(
            "Za mało pasma dla %s: %.0f kbit/s < wymagane %.0f kbit/s",
            url, kbps, wymagane_bps / 1000,
        )
        return False, kbps

    return True, kbps


async def _sprawdz(session: aiohttp.ClientSession, url: str, timeout: float) -> tuple[bool, float]:
    pobrano, kbps, dane, typ = await _pobierz_probke(
        session, url, timeout, MAX_SEGMENT_BYTES
    )
    if not pobrano:
        return False, 0.0

    if _czy_hls(url, typ, dane):
        return await _sprawdz_hls(session, url, dane.decode("utf-8", "ignore"), timeout)

    if len(dane) < MIN_SEGMENT_BYTES:
        return False, 0.0
    return True, kbps


def _host(url: str) -> str:
    return urlsplit(url).netloc or url


def _sem_dla_hosta(host: str, semafory: dict[str, asyncio.Semaphore]) -> asyncio.Semaphore:
    sem = semafory.get(host)
    if sem is None:
        sem = asyncio.Semaphore(MAX_NA_HOST)
        semafory[host] = sem
    return sem


async def sprawdz_strumien(
    session: aiohttp.ClientSession,
    kanal: KanalZrodlowy,
    sem_globalny: asyncio.Semaphore,
    semafory_hostow: dict[str, asyncio.Semaphore],
    timeout: float = STREAM_TIMEOUT,
) -> tuple[bool, float]:
    """Testuje strumień. Serwery IPTV często odrzucają część żądań, dlatego
    nieudany test powtarzamy (z krótką przerwą), zanim uznamy kanał za martwy."""
    async with sem_globalny:
        async with _sem_dla_hosta(_host(kanal.url), semafory_hostow):
            for proba in range(max(1, PROBY_STRUMIENIA)):
                try:
                    wynik = await _sprawdz(session, kanal.url, timeout)
                except Exception as blad:  # noqa: BLE001
                    log.debug("Test %s zakończony błędem: %s", kanal.url, blad)
                    wynik = (False, 0.0)
                if wynik[0]:
                    return wynik
                if proba + 1 < PROBY_STRUMIENIA:
                    await asyncio.sleep(0.4 * (proba + 1))
            return False, 0.0


# ---------------------------------------------------------------------------
# Zapis wyniku
# ---------------------------------------------------------------------------


def _wstaw_tvg_id(extinf: str, epg_id: str) -> str:
    if _RE_TVG_ID.search(extinf):
        return _RE_TVG_ID.sub(
            lambda _: f'tvg-id="{epg_id}"', extinf, count=1
        )
    if extinf.startswith("#EXTINF:"):
        return re.sub(
            r"^#EXTINF:\s*([-0-9.]+)",
            lambda dopasowanie: f'#EXTINF:{dopasowanie.group(1)} tvg-id="{epg_id}"',
            extinf,
            count=1,
        )
    return extinf


def zapisz_atomowo(sciezka: str, tresc: str) -> None:
    katalog = os.path.dirname(os.path.abspath(sciezka)) or "."
    deskryptor, tymczasowy = tempfile.mkstemp(
        prefix=".gracy_", suffix=".tmp", dir=katalog
    )
    try:
        with os.fdopen(deskryptor, "w", encoding="utf-8", newline="\n") as plik:
            plik.write(tresc)
        os.replace(tymczasowy, sciezka)
    except BaseException:
        try:
            os.unlink(tymczasowy)
        except OSError:
            pass
        raise


def _token_github() -> str | None:
    """Token z GITHUB_TOKEN / GRACY_TOKEN albo z pliku github_token.txt."""
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GRACY_TOKEN")
    if token and token.strip():
        return token.strip()
    try:
        with open(PLIK_TOKENU, encoding="utf-8") as plik:
            zawartosc = plik.read().strip()
        return zawartosc or None
    except OSError:
        return None


def _git_blob_sha(dane: bytes) -> str:
    """SHA-1 obiektu blob w Git (tak samo liczy go GitHub)."""
    naglowek = b"blob " + str(len(dane)).encode("ascii") + b"\0"
    return hashlib.sha1(naglowek + dane).hexdigest()


def _zapytanie_github(
    adres: str, token: str, dane: bytes | None, metoda: str
) -> dict:
    naglowki = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "gracy-iptv",
        "Content-Type": "application/json",
    }
    zadanie = urllib.request.Request(adres, data=dane, headers=naglowki, method=metoda)
    with urllib.request.urlopen(zadanie, timeout=60) as odpowiedz:
        return json.loads(odpowiedz.read().decode("utf-8"))


def wgraj_na_github(sciezka: str) -> None:
    """Wgrywa wynik na GitHub przez API (tryb lokalny, GRACY_PUSH=1).

    Dzięki temu lista może powstawać z polskiego adresu IP - TVP wydaje adresy
    strumieni tylko dla adresów z Polski, więc runner GitHuba ich nie dostanie.
    """
    token = _token_github()
    if not token:
        log.warning(
            "GitHub: brak tokenu (GITHUB_TOKEN albo %s) - pomijam wgrywanie",
            os.path.basename(PLIK_TOKENU),
        )
        return

    with open(sciezka, "rb") as plik:
        zawartosc = plik.read()

    adres = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_PATH}"

    for proba in range(1, 3):
        # 1. odczyt zdalnego blob sha (i porównanie treści - bez zmian nic nie wgrywamy)
        try:
            zdalny = _zapytanie_github(
                f"{adres}?ref={GITHUB_BRANCH}", token, None, "GET"
            )
        except urllib.error.HTTPError as blad:
            if blad.code == 404:  # pliku jeszcze nie ma
                zdalny = {}
            else:
                log.warning("GitHub: nie mogę odczytać pliku (%s)", blad.code)
                return
        except Exception as blad:  # noqa: BLE001
            log.warning("GitHub: błąd sieci przy odczycie (%s)", blad)
            return

        if zdalny.get("sha") == _git_blob_sha(zawartosc):
            log.info("GitHub: plik bez zmian - nic nie wgrywam")
            return

        tresc_zadania = {
            "message": "Aktualizacja gracy.m3u (lokalnie, z polskiego IP)",
            "content": base64.b64encode(zawartosc).decode("ascii"),
            "branch": GITHUB_BRANCH,
        }
        if zdalny.get("sha"):
            tresc_zadania["sha"] = zdalny["sha"]

        # 2. wgranie pliku
        try:
            wynik = _zapytanie_github(
                adres, token, json.dumps(tresc_zadania).encode("utf-8"), "PUT"
            )
            log.info(
                "GitHub: wgrano %s (commit %s)",
                GITHUB_PATH,
                str(wynik.get("commit", {}).get("sha", "?"))[:8],
            )
            return
        except urllib.error.HTTPError as blad:
            tresc_bledu = blad.read()[:200]
            if blad.code in (409, 422) and proba < 2:
                log.warning("GitHub: konflikt (%s) - próbuję jeszcze raz", blad.code)
                time.sleep(2)
                continue
            log.warning("GitHub: nie udało się wgrać (%s): %s", blad.code, tresc_bledu)
            return
        except Exception as blad:  # noqa: BLE001
            log.warning("GitHub: błąd sieci przy wgrywaniu (%s)", blad)
            return


# ---------------------------------------------------------------------------
# Główna logika
# ---------------------------------------------------------------------------


async def przetworz_liste_async() -> int:
    zlacze = aiohttp.TCPConnector(limit=MAX_ROWNOLOGE * 2, limit_per_host=MAX_NA_HOST)

    async with aiohttp.ClientSession(connector=zlacze) as session:
        mapa_epg = await pobierz_baze_epg(session)

        log.info("2. Pobieranie list M3U/M3U8 (%d źródeł)...", len(INDEKSY_ZRODEL))
        kanaly: list[KanalZrodlowy] = []
        widziane_url: set[str] = set()

        for adres, czy_pl in INDEKSY_ZRODEL:
            tekst = await pobierz_tekst(session, adres)
            if not tekst:
                log.warning("   Pomijam nieosiągalne źródło: %s", adres)
                continue
            nowe = parsuj_playliste(tekst, czy_pl, widziane_url)
            log.info("   %s -> %d kanałów", adres.rsplit("/", 1)[-1], len(nowe))
            kanaly.extend(nowe)

        kanaly.extend(await pobierz_kanaly_tvp(session))

        if not kanaly:
            log.error("Nie pobrano żadnych kanałów - przerywam bez zapisu.")
            return 1

        do_testu = kanaly[:LIMIT_TESTU] if LIMIT_TESTU > 0 else kanaly
        log.info("3. Testowanie %d strumieni...", len(do_testu))

        sem_globalny = asyncio.Semaphore(MAX_ROWNOLOGE)
        semafory_hostow: dict[str, asyncio.Semaphore] = {}
        zadania = [
            sprawdz_strumien(session, kanal, sem_globalny, semafory_hostow)
            for kanal in do_testu
        ]
        wyniki = await asyncio.gather(*zadania)

        grupy: dict[str, list[tuple[float, KanalZrodlowy, str | None]]] = {}
        dziala = 0

        for kanal, (ok, kbps) in zip(do_testu, wyniki):
            if not ok:
                continue
            dziala += 1

            epg_id = dopasuj_epg(kanal, mapa_epg)

            # kanał z obcego źródła musi mieć potwierdzenie polskości
            if not kanal.zrodlo_pl and not czy_kanal_jest_polski(kanal.extinf, epg_id):
                continue

            klucz = epg_id or normalizuj_nazwe(kanal.nazwa)
            if not klucz:
                continue
            grupy.setdefault(klucz, []).append((kbps, kanal, epg_id))

        log.info(
            "4. Działa %d strumieni z %d, grupuję w %d stacji...",
            dziala, len(do_testu), len(grupy),
        )

        wybrane: list[WybranyKanal] = []
        for warianty in grupy.values():
            warianty.sort(key=lambda element: (-element[0], element[1].extinf))
            kbps, kanal, epg_id = warianty[0]

            extinf = kanal.extinf
            if epg_id:
                extinf = _wstaw_tvg_id(extinf, epg_id)

            prio = pobierz_prio_mux(kanal.nazwa, epg_id)
            klucz_nazwy = normalizuj_nazwe(kanal.nazwa)
            log.debug(
                "wybrano: prio=%d klucz=%r nazwa=%r epg=%r kbps=%.0f",
                prio, klucz_nazwy, kanal.nazwa, epg_id, kbps,
            )

            wybrane.append(
                WybranyKanal(
                    prio=prio,
                    klucz_nazwy=klucz_nazwy,
                    extinf=extinf,
                    url=kanal.url,
                    kbps=kbps,
                )
            )

        wybrane.sort(key=lambda k: (k.prio, k.klucz_nazwy, k.url))

        if not wybrane:
            log.error("Brak działających kanałów - NIE nadpisuję %s", OUTPUT_FILE)
            return 1

        linie = [f'#EXTM3U url-tvg="{EPG_URL}" x-tvg-url="{EPG_URL}"']
        uzyte_extinf: set[str] = set()
        for kanal in wybrane:
            if kanal.extinf in uzyte_extinf:
                continue
            uzyte_extinf.add(kanal.extinf)
            linie.append(kanal.extinf)
            linie.append(kanal.url)

        zapisz_atomowo(OUTPUT_FILE, "\n".join(linie) + "\n")

        if os.environ.get("GRACY_PUSH", "").strip().lower() in ("1", "true", "tak", "yes"):
            wgraj_na_github(OUTPUT_FILE)

        log.info(
            "Sukces: zapisano %d kanałów do %s (MUX Wrocław: %d).",
            len(uzyte_extinf),
            OUTPUT_FILE,
            sum(1 for k in wybrane if k.prio != PRIO_POZA_MUX),
        )
        return 0


def main() -> int:
    poziom = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, poziom, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        return asyncio.run(przetworz_liste_async())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
