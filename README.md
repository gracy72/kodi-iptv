# Gracy IPTV — lista aktualizowana w chmurze GitHub

Lista jest generowana automatycznie **codziennie o 04:00 czasu polskiego**
(03:00 UTC) przez GitHub Actions i wgrywana do repozytorium
`gracy72/kodi-iptv`, do którego dopięte jest Kodi.

## Pliki

| Plik | Rola |
|------|------|
| `gracy_gui.py` | Program z okienkiem: **aktualizacja ręczna** + podgląd raportu |
| `process_playlist.py` | Główny skrypt: pobiera źródła i EPG, testuje strumienie, sortuje MUX Wrocław, (opcjonalnie) wgrywa na GitHub |
| `github_token.txt` | Token GitHub (PAT) do ręcznego wgrania listy |
| `gracy.m3u` | Wynikowa lista kanałów |
| `gracy_archiwum.m3u` | Archiwum martwych/niedostępnych kanałów (**100 dni**) |
| `gracy_wykluczone.txt` | Kanały wykluczone z listy (fragment nazwy lub URL) — trafiają do archiwum |
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
- **Wykluczenia** (`gracy_wykluczone.txt`) — np. strumień działa, ale się rwie
  (Sci Fi) → do archiwum, nie do Kodi. Aby przywrócić, usuń linię z pliku.

## Aktualizacja ręczna (program)

1. Dwukrotnie kliknij `start_gui.bat` (albo `gracy_gui.py`).
2. Wpisz token GitHub i kliknij **Zapisz token**.
3. **Aktualizuj ręcznie** — uruchomi aktualizację teraz i pokaże raport.
4. Checkbox **Wgraj na GitHub** — czy po aktualizacji wgrać listę do repo.

Ręczna aktualizacja z polskiego IP to **furtka ratunkowa**: przywraca kanały,
które chmura (IP z USA) błędnie uznała za martwe.
