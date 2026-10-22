# DreamPlanner backend

Wymagane: Python 3.12 i `uv`.

Z katalogu `backend/`:

```sh
uv sync
uv run uvicorn src.main:app --reload
```

API: `http://localhost:8000/api/v1/health` (`{"status":"ok"}`).
Dokumentacja OpenAPI: `http://localhost:8000/docs`; schemat JSON:
`http://localhost:8000/openapi.json`.

Opcjonalnie skopiuj `.env.example` do `.env` i ustaw `APP_NAME` oraz
`CORS_ORIGINS`. Lista originów musi być tablicą JSON, np.
`CORS_ORIGINS='["https://app.example.com"]'`. W produkcji zastąp lokalne
wartości rzeczywistymi adresami frontendu; nie używaj `*`.
CORS dopuszcza metody `GET`, `POST`, `PUT`, `PATCH`, `DELETE`, `OPTIONS` oraz
nagłówki `Authorization` i `Content-Type`. CORS nie zastępuje walidacji `Origin`
w przyszłych operacjach wykorzystujących refresh cookie.
Plik `.env` nie powinien trafić do repozytorium.

## PostgreSQL i migracje

Lokalny PostgreSQL uruchamia `compose.yaml`. Pracując bezpośrednio w WSL, wykonaj
poniższe polecenia z katalogu `backend/`:

1. Skopiuj `.env.example` do `.env`. Ustaw **trzy różne lokalne hasła**:
   `POSTGRES_SUPERUSER_PASSWORD`, `POSTGRES_APP_PASSWORD` i
   `POSTGRES_MIGRATION_PASSWORD`. Wstaw te same hasła aplikacyjne i migracyjne
   odpowiednio do `DATABASE_URL` i `MIGRATION_DATABASE_URL`. Jeśli hasło zawiera
   znaki specjalne URL, zakoduj je procentowo w URL. Plik `.env` jest ignorowany
   przez Git.
2. W razie zajętego portu `5432`, ustaw `POSTGRES_HOST_PORT` na wolny port i
   zmień ten port w obu URL. Adres `127.0.0.1` w URL oznacza usługę dostępną
   z WSL przez port hosta.
3. Sprawdź konfigurację i uruchom tylko lokalną usługę PostgreSQL:

   ```sh
   docker compose config --quiet
   docker compose up -d --wait postgres
   ```

4. Sprawdź oba połączenia oraz uprawnienia:

   ```sh
   RUN_DB_INTEGRATION=1 uv run pytest -m integration -v
   ```

5. Zatrzymaj usługę bez usuwania wolumenu:

   ```sh
   docker compose stop postgres
   ```

Ponowny start: `docker compose start postgres`. Zwykłe `uv run pytest -v`
pomija testy integracyjne, więc działa także bez PostgreSQL.

Obraz PostgreSQL ma ustaloną wersję `16.15-bookworm`, a port kontenera `5432`
jest publikowany wyłącznie na `127.0.0.1:${POSTGRES_HOST_PORT}`. Dane przechowuje
nazwany wolumen `postgres_data`; zatrzymanie kontenera ich nie usuwa.

Przy **pierwszym uruchomieniu pustego wolumenu** obraz tworzy bazę z
`POSTGRES_DB` i konto administracyjne z `POSTGRES_USER`. Następnie
`docker/init-roles.sh` tworzy `dreamplanner_app` i `dreamplanner_migration`.
Konto aplikacyjne ma dostęp do schematu `public` oraz prawa odczytu i zapisu do
tabel i sekwencji utworzonych później przez konto migracyjne. Konto migracyjne
może tworzyć obiekty w `public`; konto aplikacyjne nie może. Skrypt nie tworzy
tabel biznesowych. Zmiana haseł w `.env` przy istniejącym wolumenie **nie zmienia**
haseł zapisanych w PostgreSQL; skrypty inicjalizacyjne nie uruchamiają się
ponownie dla istniejących danych. Nie usuwaj wolumenu w celu ponownej
inicjalizacji; istniejące konto trzeba zaktualizować świadomie w bazie.

`DATABASE_URL` wskazuje konto aplikacyjne, a `MIGRATION_DATABASE_URL` osobne
konto migracyjne. Oba URL używają sterownika Psycopg 3, np.
`postgresql+psycopg://dreamplanner_app:hasło@127.0.0.1:5432/dreamplanner`.
Przykłady są w `.env.example`. Dla Azure zachowaj TLS `verify-full` i zaufany
certyfikat CA zgodnie z dokumentacją infrastruktury.

Silnik i fabryka sesji powstają dopiero przy pierwszym użyciu bazy. Import API
i endpoint health działają bez skonfigurowanego lub uruchomionego PostgreSQL.
`get_session()` udostępnia jedną sesję w żądaniu i zamyka ją po zakończeniu;
serwis zapisujący odpowiada za `commit()` oraz `rollback()` przy błędzie.

Testy migracji wymagają osobnej, pustej i jednorazowej lokalnej bazy
PostgreSQL z uprawnieniami obu kont jak w `docker/init-roles.sh`. Ustaw
`TEST_MIGRATION_DATABASE_URL` oraz `TEST_DATABASE_URL` na tę samą bazę, której
nazwa zaczyna się od `dreamplanner_test_`, i uruchom
`uv run pytest tests/integration -v`. Testy odmawiają
pracy na bazie wskazanej przez zwykłe URL developerskie. W bazie testowej
wykonują upgrade, sprawdzenia i downgrade; po testach usuń jednorazową bazę.
Bez tych dwóch zmiennych zwykłe `uv run pytest` pomija testy migracji.

Druga migracja odmawia upgrade, jeśli tabela `users` zawiera konta. Takie
środowisko wymaga przed migracją przeglądu stanu aktywacji zgodnie z
`AUTH_DESIGN.md`; migracja nie przypisuje kontom automatycznej daty aktywacji.

Konfiguracja Alembic znajduje się w `alembic.ini` i `alembic/env.py`.
Uruchamiaj polecenia Alembic z katalogu `backend/`, po ustawieniu
`MIGRATION_DATABASE_URL`. Moduł `src/core/models.py` jest miejscem na jawne
importy przyszłych modeli ORM do metadanych Alembic. Obecnie nie ma modeli
biznesowych ani migracji tabel. Migracje nie uruchamiają się przy starcie API.

Kontrole:

```sh
uv run ruff check .
uv run ruff format --check .
uv run pytest
```
