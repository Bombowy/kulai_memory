# KulAI Memory — zakres MVP

Priorytetem jest trwała pamięć wypowiedzi i prosty głosowy RAG. Rozwój Fact
Intelligence (5A.1–5A.6) jest wstrzymany; istniejący RAG pozostaje bez przebudowy.

```text
NOTE: mikrofon → Whisper → STORAGE: canonical Memory w PostgreSQL
                              ↓ automatyczny embedding BGE-M3
Pytanie tekstem / głosem → RETRIEVAL: BGE-M3 + zapisane Memories
                        → ASK: Qwen3.5:9b + źródła → opcjonalny TTS
```

Zwykły Desktop obsługuje NOTE, automatyczne indeksowanie, tekstowy ASK, ASK BY
VOICE, Memory Library i opcjonalny lokalny TTS. Android ma NOTE / ASK / MEMORY
oraz lokalny TTS. Zapisane Memories i embeddingi pozostają w PostgreSQL po
zamknięciu aplikacji; historia pytań i niezapisane próby nie są trwałą pamięcią.

Regresja MVP-RESET potwierdziła te ścieżki na syntetycznych danych w bazach
marker-owned, w tym odczyt po uruchomieniu drugiego procesu. Krótkie testy
rzeczywistych Whisper, BGE-M3, Qwen i syntezy Windows TTS przeszły. Przechwytywanie
mikrofonu i odtwarzanie dźwięku były kontrolowane. Do ręcznego odbioru pozostają
mikrofon, odsłuch, ergonomia Desktop, Android przez USB oraz odczyt po restarcie
komputera. Wyniki i rozróżnienie testów automatycznych/ręcznych: `wynik.txt`.

## Uruchomienie lokalne

Z katalogu repozytorium, przy działającym PostgreSQL i Ollama z już dostępnymi
modelami, bez dodatkowych migracji:

```powershell
.\backend\.venv\Scripts\python.exe .\scripts\desktop.py
```

Android debug: podłącz telefon z debugowaniem USB. W pierwszym terminalu:

```powershell
.\backend\.venv\Scripts\python.exe .\scripts\dev.py --security-mode LOCAL_DEV
```

W drugim terminalu, również z katalogu repozytorium:

```powershell
Push-Location .\mobile\android
.\gradlew.bat assembleDebug
Pop-Location
adb reverse tcp:8000 tcp:8000
adb install -r .\mobile\android\app\build\outputs\apk\debug\app-debug.apk
adb shell am start -n com.kulai.memory/.MainActivity
```

LOCAL_DEV jest wyłącznie lokalnym trybem debug przez loopback i ADB reverse.
Nie zmieniaj konfiguracji SECURE ani zabezpieczeń transportu. Zakończ backend
przez Ctrl+C; mapowanie USB można usunąć przez `adb reverse --remove tcp:8000`.
Szczegóły klienta: [Android README](../mobile/android/README.md).

## Odłożone funkcje i zachowany kod

Wstrzymane są: ekstrakcja faktów, review i decyzje APPROVE/REJECT/REVOKE,
deduplikacja i konflikty, trwały storage faktów, Approved Facts Retrieval,
Fact-RAG i Fact Workspace. Kod, osobny graf migracji i testy 5A.1–5A.5 pozostają
na main. Ich demonstratory wymagają ręcznego uruchomienia; zwykły NOTE/ASK ich
nie wywołuje i nie wymaga facts schema. Nie dodano feature flag.

Niezacommitowane wcześniej 5A.6 zachowano osobno:

- Gałąź: `archive/kulai-memory-fact-intelligence-paused` na origin.
- Commit: `44aea6e45c3120617ccbf3fe3d4b30d402c2945a`.
- Zawartość: wspólny Fact Workspace, launcher, testy, fixture ewaluacji,
  rozszerzenie workera i dokumentacja; bez raportu i prywatnych danych.

Aby w przyszłości obejrzeć archiwum bez zmiany bieżącego checkoutu:

```powershell
git fetch origin
git worktree add ..\kulai_memory_facts_paused archive/kulai-memory-fact-intelligence-paused
```

Przed ponownym rozwijaniem tego kodu przeczytaj sekcję 5A.6 README na tej gałęzi.
Nie instaluj schematu faktów na main. Archiwizacja nie oznacza ręcznego PASS GUI.
Najkrótszy dalszy krok MVP to ręczny odbiór istniejącego NOTE → ASK → TTS na
Desktop i Androidzie oraz po restarcie, a potem naprawa wyłącznie znalezionych
usterek.
