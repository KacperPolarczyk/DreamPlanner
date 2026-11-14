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
