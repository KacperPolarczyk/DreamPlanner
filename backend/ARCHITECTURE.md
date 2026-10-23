# DreamPlanner — architektura techniczna backendu

Status: zatwierdzone decyzje projektowe. Dokument nie stanowi implementacji ani
szczegółowego kontraktu API. Szczegóły tożsamości i sesji opisuje
[AUTH_DESIGN.md](AUTH_DESIGN.md).

## Źródła i zakres

Backend realizuje modularną architekturę trójwarstwową opisaną w głównym
`README.md` i `AGENTS.md`. `database/data-model.md` określa uzgodnione reguły
domenowe, a `database/schema.sql` jest referencyjną specyfikacją schematu.
Migracje należą do `backend/`; plików w `database/` nie zmienia się w ramach
implementacji backendu. Fixture demonstracyjny nie jest kontraktem API ani
danymi do automatycznego importu.

## Stos i układ kodu

- FastAPI, Pydantic v2, SQLAlchemy 2.x, Psycopg 3, PostgreSQL i Alembic.
- Synchroniczny dostęp do bazy i synchroniczne funkcje tras korzystające z bazy.
- `uv` zarządza środowiskiem i zależnościami w `backend/pyproject.toml` oraz
  `backend/uv.lock`; Ruff formatuje i sprawdza kod, pytest i HTTPX służą do
  testów.
- REST API używa prefiksu `/api/v1`. Kontrakt endpointów powstanie osobno.

## Lokalne limity prób w MVP

Lokalne MVP zakłada **jedną replikę FastAPI i jeden worker Uvicorn**. SlowAPI
używa wyłącznie magazynu `memory://` i strategii moving window. Limity
logowania wynoszą 20 żądań na IP w 5 minut oraz 10 na znormalizowany e-mail
w 15 minut; odnowienia 60 na IP w minutę oraz 10 na znaną sesję w minutę;
aktywacji 20 na IP w 15 minut oraz 5 na znane konto w godzinę. Próby
nieudane również zużywają limit. Limit IP obejmuje także niepoprawny JSON;
drugi licznik wymaga możliwego do odczytania e-maila lub tokenu.

Liczniki należą do procesu, zerują się po jego restarcie i nie są
współdzielone między workerami ani replikami. Nie ma automatycznego fallbacku
na pamięć po błędzie konfiguracji magazynu. Przed skalowaniem lub wdrożeniem
na Azure wymagany jest ponowny przegląd rate limitingu. To opis założeń MVP,
nie istniejącego wdrożenia Azure.

Klucz IP pochodzi z `request.client.host`. Aplikacja nie odczytuje samodzielnie
`X-Forwarded-For`; Uvicorn może przyjąć nagłówki proxy tylko od adresów
jawnie wskazanych przez `--forwarded-allow-ips`. Nie używamy `*` jako listy
zaufanych proxy. Sekrety i adresy hostingu nadal pochodzą z konfiguracji.

Docelowy minimalny układ, tworzony stopniowo wraz z uzgodnionymi funkcjami:

```text
backend/
├── pyproject.toml
├── uv.lock
├── alembic/
├── src/
│   ├── main.py
│   ├── api.py
│   ├── core/                 # konfiguracja, silnik, sesja, baza modeli
│   ├── auth/
│   ├── users/
│   ├── positions/
│   ├── schedules/            # grafik i zapotrzebowanie na zmiany
│   ├── availability/
│   └── assignments/
└── tests/
```

Moduł funkcjonalny może zawierać `router.py`, `schemas.py`, `models.py`,
`service.py`, `repository.py` i `dependencies.py`. Każdy plik powstaje tylko
wtedy, gdy ma uzasadnioną odpowiedzialność. `src/shared/` powstaje dopiero dla
kodu rzeczywiście używanego przez kilka funkcji. Nie wprowadzamy ogólnych klas
`BaseService` lub `BaseRepository` bez wykazanej potrzeby.

## Granice warstw i zależności

Przepływ wywołania: **router → service → repository → modele ORM / PostgreSQL**.

- Router obsługuje HTTP, schematy wejścia i wyjścia, zależności FastAPI oraz
  przekazanie polecenia do serwisu. Nie zawiera reguł domenowych ani zapytań.
- Schematy Pydantic są publiczną granicą API; modeli ORM nie zwracamy jako
  niejawnego kontraktu.
- Serwis sprawdza reguły, koordynuje operację i jest właścicielem jej zapisu.
  Kod serwisu nie korzysta z `Depends()` ani `HTTPException`.
- Repozytorium zawiera potrzebne zapytania i operacje utrwalania. Nie zna HTTP,
  nie podejmuje decyzji o uprawnieniach i nie wykonuje `commit()`.
- `src/core/` dostarcza konfigurację, silnik, fabrykę sesji i wspólną bazę
  modeli. Moduły funkcjonalne nie importują swoich routerów nawzajem.
- Funkcja składająca zależności FastAPI może utworzyć serwis i repozytoria
  ze wspólną sesją. Konstruktory tych klas przyjmują zwykłe argumenty Pythona.

Operację obejmującą kilka funkcji prowadzi jeden serwis koordynujący, używając
repozytoriów ze wspólną sesją. Nie tworzymy cykli wywołań między serwisami.
Relacje ORM między funkcjami oraz import modeli do metadanych Alembic muszą być
jawne i testowane. Zależności autoryzacyjne nie zastępują kontroli dostępu do
konkretnego zasobu w serwisie; szczegóły zawiera `AUTH_DESIGN.md`.

## Sesja i granica transakcji

`src/core/` tworzy jedną fabrykę synchronicznych `Session`. Zależność HTTP
otwiera **jedną sesję na żądanie**, udostępnia ją wszystkim potrzebnym
repozytoriom i zamyka w `finally` lub kontekście `with`. Nie zatwierdza
automatycznie zmian przy zamykaniu. Jednostka pracy poza HTTP, np. polecenie
bootstrap, sama otwiera i zamyka własną sesję.

Serwis koordynujący zapis wykonuje odczyty i zmiany na tej samej sesji,
wywołuje `flush()` gdy potrzebuje wartości z bazy, a następnie dokładnie raz
wykonuje `commit()`. Przy błędzie wykonuje `rollback()` i przekazuje błąd do
obsługi. `commit()` musi zakończyć się **przed** zwróceniem odpowiedzi HTTP;
wyjątek z bazy nie może pojawić się dopiero po wysłaniu sukcesu. Repozytoria
nie zatwierdzają części operacji. Operacja tylko odczytująca nie wymaga
`commit()`; zamknięcie sesji kończy jej transakcję.

W SQLAlchemy odczyt może rozpocząć transakcję automatycznie. Zależność
autoryzacyjna może więc użyć sesji przed wywołaniem serwisu. Serwis nie może
zakładać, że `session.begin()` zawsze rozpocznie nową transakcję; cały zapis
pozostaje w już rozpoczętej transakcji tej sesji. Żadna część operacji nie
otwiera drugiej sesji ani nie zatwierdza jej po drodze. Po `rollback()` sesja
nie jest dalej używana do kolejnego zapisu w tym samym żądaniu.

Blokady wierszy i ograniczenia PostgreSQL chronią operacje podatne na
równoczesne zapisy. Dobór blokad do przydziałów i dyspozycyjności zależy od
uzgodnionych reguł tych funkcji. Rotacja refresh tokenu ma osobną, określoną
procedurę blokowania w `AUTH_DESIGN.md`. Testy takich operacji korzystają z
PostgreSQL i niezależnych połączeń.

## Migracje i konfiguracja

Alembic wdraża modele oraz wymagane indeksy, ograniczenia, funkcje i triggery
referencyjnego `database/schema.sql`. Autogeneracja migracji nie zastępuje
przeglądu tych elementów. Zmiany dotyczące sesji i aktywacji są rozszerzeniem
schematu opisanym w `AUTH_DESIGN.md`; nie wolno modyfikować migracji, która
mogła zostać zastosowana. Migracje używają `MIGRATION_DATABASE_URL` i konta
migracyjnego, aplikacja `DATABASE_URL` i konta aplikacyjnego. Migracje nie
uruchamiają się przy starcie API.

Konfiguracja i sekrety pochodzą ze środowiska lub uzgodnionego magazynu
sekretów. Połączenie do Azure PostgreSQL zachowuje TLS `verify-full` zgodnie z
`NEXT_STEPS.md` i `database/network-requirements.md`. Kod aplikacji pozostaje
przenośny między lokalnym PostgreSQL a Azure.

## Błędy i testy

Serwisy zgłaszają nazwane błędy aplikacyjne, np. brak zasobu, brak uprawnienia
lub konflikt. Granica HTTP tłumaczy je na spójne odpowiedzi i statusy;
`401` oznacza brak poprawnego uwierzytelnienia, `403` brak uprawnienia.
Szczegółowy format odpowiedzi zostanie ustalony w kontrakcie API. Znane
naruszenia ograniczeń bazy są tłumaczone po `rollback()` na bezpieczne błędy
aplikacyjne. Nieoczekiwany błąd jest logowany bez sekretów i danych połączenia,
a odpowiedź nie ujawnia szczegółów SQL ani stosu wywołań.

Testy obejmują usługi, walidację HTTP i uprawnienia. Zachowanie zależne od
transakcji, ograniczeń, migracji i blokad jest testowane na PostgreSQL.
Weryfikacja implementacji obejmuje `uv run pytest`, `uv run ruff check .` i
`uv run ruff format --check .` z katalogu `backend/`.

## Kwestie wdrożeniowe pozostające otwarte

- Szczegółowy kontrakt API i format błędów.
- Dokładne adresy hostingu, konfiguracja CORS i cookie.
- Reguły przejść stanów grafiku i blokowania jego zapisów.
- Dobór parametrów wydajnościowych Argon2id i limitów prób logowania na
  docelowym hostingu.
