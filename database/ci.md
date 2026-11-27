# Automatyczna weryfikacja PostgreSQL

Workflow `.github/workflows/database-ci.yml` uruchamia job
**PostgreSQL - migrations and integration tests** na push do `main`, pull requestach
oraz ręcznie. Gałąź `ci/database-validation` uruchamia go także przed integracją.
Workflow udostępnia `workflow_call`, jeśli zespół chce wywoływać go ze wspólnego CI.
Nie należy uruchamiać go dwukrotnie jednocześnie jako osobnego i wywoływanego workflow.

## Zakres

Wykorzystujemy istniejące testy w `backend/tests/integration` i runner
`backend/scripts/ci_postgres.py`. Każdy moduł otrzymuje osobną bazę testową.
Testy obejmują migracje domeny i uwierzytelniania, klucze i ograniczenia SQL,
operacje na grafikach, dyspozycyjności i przydziałach, sesje i tokeny użytkowników
oraz współbieżność publikacji i przydziałów.

Dodatkowy moduł `test_runtime_privileges_postgres.py` sprawdza:

- pełny CRUD i generowanie identyfikatora na tabeli utworzonej przez Alembic;
- każdą z czterech operacji DML na wszystkich 11 tabelach aplikacji;
- brak uprawnień administracyjnych konta aplikacji;
- odmowę CREATE TABLE, ALTER TABLE, DROP TABLE, CREATE SCHEMA, CREATE ROLE
  i SET ROLE na konto migracji (SQLSTATE 42501).

## Kontrakt HTTP i trwałość danych

Moduł `backend/tests/integration/test_http_database_postgres.py` przechodzi pełną
ścieżkę FastAPI → uwierzytelnienie → PostgreSQL. Używa prawdziwego haszowania haseł,
JWT, sesji uwierzytelnienia i zależności aplikacji, bez atrap repozytorium i bez
podmiany `get_session`. Migracje wykonuje konto `dreamplanner_migration`, a API
łączy się jako `dreamplanner_app` bez praw superusera.

Sprawdzane zachowania:

- logowanie HTTP 200, utworzenie stanowiska HTTP 201, nagłówki JSON/Location/no-store
  i struktura odpowiedzi; odczyt z nowego klienta HTTP oraz osobnej sesji SQL;
- HTTP 409 przy powtórzonej nazwie, dokładnie jeden rekord i poprawny kolejny zapis;
- HTTP 401/403/422 i brak zapisu dla żądań bez sesji, z rolą employee lub błędną nazwą;
- trwała dezaktywacja stanowiska oraz zachowanie unikalności jego nazwy;
- wylogowanie HTTP 204, unieważnienie sesji w bazie i odrzucenie kolejnego zapisu.

Healthcheck korzysta z rzeczywistego `SELECT 1`. Testy działają w procesie z
`TestClient`, na odizolowanym PostgreSQL; nie są testem publicznego adresu Azure.
Uzupełniają test Playwright działający po wdrożeniu, który pozostaje w
`frontend/e2e/positions.spec.ts`.

## Uruchomienie lokalne (Bash)

Wymagane: Docker, uv oraz Python 3.12. Poniższy blok uruchamia świeży kontener
PostgreSQL 16, wiąże losowy port tylko na loopback i usuwa wyłącznie własny kontener
po zakończeniu. Nie wykorzystuje bazy Azure ani danych z Key Vault.

```bash
cd backend
uv sync --frozen --python 3.12
(
  set -euo pipefail
  export POSTGRES_PASSWORD="$(uv run --no-sync python -c 'import secrets; print(secrets.token_urlsafe(32))')"
  container_id=$(docker run --detach --rm --env POSTGRES_PASSWORD \
    --publish 127.0.0.1::5432 postgres:16)
  trap 'docker rm --force "$container_id" >/dev/null' EXIT
  for attempt in $(seq 1 30); do
    if docker exec "$container_id" pg_isready -U postgres >/dev/null 2>&1; then break; fi
    sleep 1
  done
  docker exec "$container_id" pg_isready -U postgres >/dev/null
  port=$(docker inspect --format '{{(index (index .NetworkSettings.Ports "5432/tcp") 0).HostPort}}' "$container_id")
  export CI=true
  export CI_POSTGRES_ADMIN_URL="postgresql://postgres:$POSTGRES_PASSWORD@127.0.0.1:$port/postgres"
  export CI_POSTGRES_REPORT_DIR="$(pwd)/../database-test-results"
  export JWT_CURRENT_KID=ci-v1 JWT_ISSUER=dreamplanner-ci JWT_AUDIENCE=dreamplanner-api
  export JWT_SIGNING_KEY_BASE64="$(uv run --no-sync python -c 'import base64,secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())')"
  uv run --no-sync python scripts/ci_postgres.py
)
```

Otwórz `database-test-results/summary.md` lub zaimportuj pliki JUnit XML do narzędzia
raportującego. Nie uruchamiaj samego `pytest -m integration` bez konfiguracji baz:
część modułów zostanie wtedy pominięta zamiast potwierdzić działanie SQL.

## Izolacja i sekrety

PostgreSQL 16 działa jako jednorazowy service container GitHub Actions.
Baza jest dostępna dla testów przez loopback i dynamicznie przydzielony port.
Nie używamy Key Vault, OIDC, uprawnień Azure ani produkcyjnych connection stringów.
Hasło administratora z YAML jest wyłącznie hasłem jednorazowego kontenera testowego.
Hasła kont SQL i klucz JWT są generowane podczas wykonania.
Konta SQL mają ustawioną strefę UTC, niezależnie od strefy systemu runnera.

Runner wymaga `CI=true` oraz adresu administratora z hostem `127.0.0.1`
lub `localhost`. Nowe testy dodatkowo wymagają konkretnej nazwy bazy testowej.
Konto aplikacji nie jest właścicielem bazy ani tabel, nie ma praw DDL i nie jest
superuserem. Konto migracji tworzy tabele, a default privileges nadają aplikacji
DML oraz dostęp do sekwencji. Lokalna nazwa konta migracji to
`dreamplanner_migration` (w Azure używamy `dreamplanner_migrator`).

Uruchamiać wyłącznie na świeżym, jednorazowym klastrze PostgreSQL. Runner celowo
nie usuwa istniejących baz ani kont o tych samych nazwach; ponowna próba wymaga
nowego klastra/kontenera. Nie przekazywać do niego sekretu administratora Azure.

## Wynik i artefakty

Każdy błąd, pusty wynik lub pominięty test kończy job niepowodzeniem.
Runner zapisuje JUnit XML dla każdego modułu oraz `summary.json` i `summary.md`
w `CI_POSTGRES_REPORT_DIR`. GitHub publikuje podsumowanie w zakładce Summary
oraz artefakt `postgres-test-results` przechowywany przez 14 dni, również przy błędzie.

W celu zablokowania scalania przy błędach bazy należy dodać status
`PostgreSQL - migrations and integration tests` do istniejącej ochrony `main`.
Nie usuwać czterech dotychczasowych wymaganych kontroli budowania i bezpieczeństwa.

CI bazodanowe uzupełnia skan sekretów i podatności ze wspólnego workflow;
nie zastępuje tych skanerów ani kontroli dostępu do rzeczywistej bazy Azure.
