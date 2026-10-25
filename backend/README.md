# DreamPlanner - Backend

DreamPlanner pomaga planować dyspozycyjność pracowników i układać elastyczne grafiki pracy. Ten katalog zawiera API aplikacji. Opis całego projektu znajdziesz w [głównym README](../README.md).

Backend działa na Pythonie 3.12 i FastAPI. Do walidacji danych używa Pydantic, do pracy z PostgreSQL - SQLAlchemy, a do migracji bazy - Alembic.

## Spis treści

- [Funkcjonalności](#funkcjonalności)
- [Wymagania](#wymagania)
- [Przed uruchomieniem](#przed-uruchomieniem)
- [Instalacja](#instalacja)
  - [Pobranie projektu](#pobranie-projektu)
  - [Uruchomienie przez Docker Compose](#uruchomienie-przez-docker-compose)
  - [Uruchomienie bez kontenera aplikacji](#uruchomienie-bez-kontenera-aplikacji)
- [Dokumentacja API](#dokumentacja-api)
- [Testowanie](#testowanie)

## Funkcjonalności

- Zarządzanie kontami użytkowników i rolami `admin`, `manager` oraz `employee`.
- Logowanie, aktywacja i reaktywacja kont oraz odnawianie sesji.
- Zarządzanie stanowiskami.
- Tworzenie grafików pracy i wymagań obsadowych.
- Zgłaszanie dyspozycyjności przez pracowników.
- Przydzielanie pracowników do zmian.
- Publikowanie gotowych grafików i podgląd opublikowanych zmian.

## Wymagania

- **Python 3.12** i **uv** - do instalacji zależności, uruchamiania API poza kontenerem, migracji i testów.
- **PostgreSQL** - baza danych aplikacji. Możesz uruchomić ją lokalnie lub przez Docker Compose.
- **Docker i Docker Compose** - wymagane dla opisanych niżej wariantów kontenerowych; opcjonalne, jeśli masz własny lokalny PostgreSQL i uruchamiasz API bez kontenera.

## Przed uruchomieniem

Przy pracy bez kontenera aplikacji przygotuj `backend/.env` na podstawie [`.env.example`](.env.example). Dla pełnego uruchomienia przez Docker Compose przygotuj osobny `backend/.env.isolated` na podstawie [`docker/isolated.example.env`](docker/isolated.example.env). Te pliki służą do różnych wariantów uruchomienia. Nie zapisuj w repozytorium uzupełnionych plików ze swoimi hasłami.

Najważniejsze ustawienia:

- `DATABASE_URL` - połączenie API z PostgreSQL przez konto aplikacyjne. Przy uruchomieniu poza kontenerem ustaw je w `.env`; w `compose.isolated.yaml` adres aplikacji jest ustawiany dla kontenera automatycznie.
- `MIGRATION_DATABASE_URL` - osobne połączenie Alembic przez konto migracyjne. Ustaw je w odpowiednim pliku środowiskowym przed wykonaniem migracji.
- `POSTGRES_SUPERUSER_PASSWORD`, `POSTGRES_APP_PASSWORD` i `POSTGRES_MIGRATION_PASSWORD` - trzy różne hasła lokalnej bazy z `compose.yaml`. Hasła kont aplikacyjnego i migracyjnego muszą zgadzać się z odpowiednimi adresami bazy w `.env`.
- `ISOLATED_*_PASSWORD` - hasła dla izolowanego stosu Docker. Zmień wszystkie wartości przykładowe i dopasuj je do adresów w `.env.isolated`.
- `JWT_SIGNING_KEY_BASE64` w `.env` albo `ISOLATED_JWT_SIGNING_KEY_BASE64` w `.env.isolated` - klucz do podpisywania tokenów. W `.env` ustaw także `JWT_CURRENT_KID`, `JWT_ISSUER` i `JWT_AUDIENCE`; ich przykładowe wartości są już w szablonie.
- `CORS_ORIGINS` - lista dozwolonych adresów frontendu w formacie JSON, np. `CORS_ORIGINS='["http://localhost:5173"]'`. Dotyczy uruchomienia API z `.env`; kontener aplikacji używa domyślnych lokalnych adresów Vite.

Klucz JWT możesz wygenerować poleceniem `openssl rand -base64 32`. Wklej wynik do właściwego pliku środowiskowego. Hasła użyte w adresach PostgreSQL muszą być zgodne z hasłami kont; znaki specjalne w URL wymagają kodowania procentowego.

## Instalacja

### Pobranie projektu

```sh
git clone https://github.com/KacperPolarczyk/DreamPlanner.git
cd DreamPlanner/backend
```

Pozostałe polecenia wykonuj z katalogu `backend/`.

### Uruchomienie przez Docker Compose

Ten wariant korzysta z [`compose.isolated.yaml`](compose.isolated.yaml), który buduje obraz API z [`Dockerfile`](Dockerfile) i uruchamia osobny PostgreSQL. Python 3.12 i `uv` są potrzebne na hoście do jawnego wykonania migracji.

1. Skopiuj szablon: `cp docker/isolated.example.env .env.isolated`. Ustaw pięć różnych haseł, klucz JWT oraz zgodne hasła w adresach baz danych zapisanych w tym pliku.
2. Uruchom kontenery:

   ```sh
   docker compose --env-file .env.isolated -f compose.isolated.yaml up -d --build --wait
   ```

3. Wykonaj migracje osobnym poleceniem:

   Po uruchomieniu kontenerów należy osobno wykonać migracje schematu izolowanej bazy PostgreSQL. Służy do tego skrypt docker/migrate-isolated.py, korzystający z konfiguracji .env.isolated. Migracje nie są uruchamiane automatycznie podczas startu aplikacji.

   ```sh
   uv run --frozen --env-file .env.isolated python docker/migrate-isolated.py app
   ```

4. Jeśli to nowa baza, utwórz pierwszego administratora. Hasło zostanie podane w terminalu:

   ```sh
   docker compose --env-file .env.isolated -f compose.isolated.yaml exec app python -m src.users.bootstrap_admin --email admin@example.com --full-name "Anna Nowak"
   ```

API jest domyślnie dostępne pod `http://127.0.0.1:8001`. Aby zatrzymać stos bez usuwania danych z wolumenu:

```sh
docker compose --env-file .env.isolated -f compose.isolated.yaml down
```

NOTE: Migracje nie uruchamiają się automatycznie przy starcie kontenera!

### Uruchomienie bez kontenera aplikacji

Ten wariant korzysta z [`compose.yaml`](compose.yaml) tylko do uruchomienia PostgreSQL. Jeśli masz już lokalny PostgreSQL, pomiń polecenie Docker Compose i ustaw oba adresy bazy w `.env` na własną instancję.

1. Zainstaluj zależności:

   ```sh
   uv sync
   ```

2. Skopiuj szablon: `cp .env.example .env`. Ustaw hasła PostgreSQL, `DATABASE_URL`, `MIGRATION_DATABASE_URL`, klucz JWT i adres frontendu w `CORS_ORIGINS`.
3. Uruchom bazę, jeśli korzystasz z Docker Compose:

   ```sh
   docker compose up -d --wait postgres
   ```

4. Wykonaj migracje:

   ```sh
   uv run alembic upgrade head
   ```

5. Utwórz pierwszego administratora. Hasło zostanie pobrane w terminalu:

   ```sh
   uv run python -m src.users.bootstrap_admin --email admin@example.com --full-name "Anna Nowak"
   ```

6. Uruchom API:

   ```sh
   uv run uvicorn src.main:app --host 127.0.0.1 --port 8000 --workers 1 --no-proxy-headers
   ```

API będzie dostępne pod `http://127.0.0.1:8000`.

## Dokumentacja API

Poniższe adresy dotyczą uruchomienia API bez kontenera. Dla wariantu Docker Compose zamień port `8000` na `8001`.

- **Swagger UI:** `http://127.0.0.1:8000/docs` - interaktywna dokumentacja i testowanie endpointów.
- **OpenAPI JSON:** `http://127.0.0.1:8000/openapi.json` - schemat API do wykorzystania przez narzędzia.
- **Health check:** `http://127.0.0.1:8000/api/v1/health` - sprawdza, czy API działa.
- **DB ready check:** `http://127.0.0.1:8000/api/v1/health/ready` - sprawdza także połączenie z PostgreSQL.

## Testowanie

Z katalogu `backend/` uruchom:

```sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

Część testów integracyjnych wymaga osobnej bazy PostgreSQL. Nie uruchamiaj ich na bazie produkcyjnej.
