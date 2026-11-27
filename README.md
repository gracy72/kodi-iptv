# Gracy IPTV — lista aktualizowana w chmurze GitHub

Lista jest generowana automatycznie **codziennie o 04:00 czasu polskiego**
(03:00 UTC) przez GitHub Actions i wgrywana do repozytorium
`gracy72/kodi-iptv`, do którego dopięte jest Kodi.

## Pliki

| Plik | Rola |
|------|------|
| `gracy_gui.py` | Program z okienkiem: **aktualizacja ręczna**, zakładki **Wykluczone** i **Mapowanie nazw**, podgląd raportu |
| `process_playlist.py` | Główny skrypt: pobiera źródła i EPG, testuje strumienie, sortuje MUX Wrocław, (opcjonalnie) wgrywa na GitHub |
| `github_token.txt` | Token GitHub (PAT) do ręcznego wgrania listy |
| `gracy.m3u` | Wynikowa lista kanałów |
| `gracy_archiwum.m3u` | Archiwum martwych/niedostępnych kanałów (**100 dni**) |
| `gracy_wykluczone.txt` | Kanały **odstawione** (fragment nazwy, `tvg-id` lub URL) - nie testowane, parkowane w archiwum |
| `gracy_mapowanie.txt` | **Mapowanie nazw**: nazwa ze źródła → nazwa docelowa (+ opcjonalny `tvg-id`) |
| `gracy_dodatkowe.m3u` | Własne kanały próbowane przy każdej aktualizacji |
| `start_gui.bat` | Skrót do uruchomienia okna programu |

## Aktualizacja automatyczna (GitHub)

Workflow `.github/workflows/update.yml`:

- `schedule: 0 3 * * *` — codziennie 03:00 UTC = **04:00 czasu polskiego**
- `workflow_dispatch` — ręczne uruchomienie z zakładki **Actions**
- guard: jeśli nowa lista ma **mniej niż 85%** kanałów starej, zmiana jest
  odrzucana (zabezpieczenie przed awarią źródeł)

## Zasady listy

- **Lista kumulatywna** — działające kanały zostają, źródła tylko dodają nowe.
- **Host całkowicie martwy** (duży serwer, ≥10 adresów, 0 działających) —
  jego kanały **nie trafiają do listy**, tylko do archiwum. Wrócą automatycznie,
  gdy host znów odpowie.
- **Mały/flaky host** — kanał oceniany per sztuka: jedna wpadka zostawia go
  w liście, druga z rzędu przenosi do archiwum.
- **Archiwum 100 dni** — martwy wpis znika na stałe po 100 dniach bez powodzenia.
- **Powrót z archiwum** — wpis musi odpowiedzieć **2 razy z rzędu** (domyślnie;
  zmienna `GRACY_POTWIERDZENIA`), żeby wrócić do listy. Kanał, który „odbija
  się" raz na jakiś czas, nie wraca na listę i nie zeruje licznika 100 dni.
  `GRACY_POTWIERDZENIA=1` przywraca stare zachowanie (wystarczy jedno udane sprawdzenie).
- **Odstawione** (`gracy_wykluczone.txt`) - kanały, których na razie nie chcesz:
  **nie są w ogóle testowane** (oszczędność łącza), nie ma ich w `gracy.m3u`,
  a w archiwum leżą bez daty i bez zegara 100 dni. Wzorzec = fragment nazwy,
  fragment `tvg-id` albo pełny URL (bez wielkości liter). Skreślenie linii
  przywraca normalne sprawdzanie - po 2 udanych przebiegach kanał wraca na listę.
- **Mapowanie nazw** (`gracy_mapowanie.txt`) - "jak kanał ma się nazywać".
  Format: `oryginalna nazwa<TAB>docelowa nazwa[<TAB>docelowy tvg-id]`. Podmieniana
  jest nazwa **oraz** `tvg-id` (gdy 3. kolumny brak - `tvg-id` = nazwa docelowa),
  dzięki czemu kanały z dziwnymi dodatkami w nazwie (`ATM Rozrywka Rebulid`)
  trafiają w EPG. Plik jest w repozytorium, więc nocna aktualizacja też go używa.
  Brak pliku = nazwy bez zmian. Własny `tvg-id` ma sens tylko wtedy, gdy istnieje
  w EPG, którego używasz (inaczej jest tylko kosmetyczny).

## Aktualizacja ręczna (program)

1. Dwukrotnie kliknij `start_gui.bat` (albo `gracy_gui.py`).
2. Wpisz token GitHub i kliknij **Zapisz token**.
3. Zakładka **Wykluczone** - lista wszystkich kanałów z checkboxami;
   zaznaczony kanał = odstawiony (nie ma go w `gracy.m3u`, nie jest testowany).
   Jest wyszukiwarka, "Zaznacz/Odznacz widoczne" i licznik zaznaczonych.
4. Zakładka **Mapowanie nazw** - dodaj regułę (nazwa oryginalna → docelowa).
   Lista nazw podpowiada się z aktualnej listy i archiwum.
5. **Aktualizuj ręcznie** - zapisuje `gracy_wykluczone.txt` i `gracy_mapowanie.txt`,
   uruchamia aktualizację teraz i pokazuje raport.
6. Checkbox **Wgraj na GitHub** — czy po aktualizacji wgrać listę (wraz z plikami
   wykluczeń i mapowania) do repo.

Ręczna aktualizacja z polskiego IP to **furtka ratunkowa**: przywraca kanały,
które chmura (IP z USA) błędnie uznała za martwe.
