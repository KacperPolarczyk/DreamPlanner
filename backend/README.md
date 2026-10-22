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

`DATABASE_URL` wskazuje konto aplikacyjne, a `MIGRATION_DATABASE_URL` osobne
konto z uprawnieniami do zmian schematu. Oba URL używają sterownika Psycopg 3:
`postgresql+psycopg://użytkownik:hasło@host:5432/dreamplanner`.
Przykłady lokalnych wartości są w `.env.example`; zastąp hasła i konta
rzeczywistymi lokalnymi danymi. Dla Azure zachowaj TLS `verify-full` i
zaufany certyfikat CA zgodnie z dokumentacją infrastruktury.

Silnik i fabryka sesji powstają dopiero przy pierwszym użyciu bazy. Import API
i endpoint health działają bez skonfigurowanego lub uruchomionego PostgreSQL.
`get_session()` udostępnia jedną sesję w żądaniu i zamyka ją po zakończeniu;
serwis zapisujący odpowiada za `commit()` oraz `rollback()` przy błędzie.

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
