# Uruchomienie aplikacji w Azure

Baza PostgreSQL, Private Endpoint, Key Vault i konta SQL są przygotowane.
Hosting aplikacji i tabele aplikacyjne wymagają jeszcze wdrożenia. Połączenie
z bazą sprawdzono z komputera DBA; test z hosta backendu pozostaje do wykonania.

## DevOps — Kacper

1. Utworzyć hosting backendu i frontendu, zapisując konfigurację w Terraform.
2. Zapewnić backendowi połączenie wychodzące przez `snet-backend` w
   `dreamplanner-vnet` oraz prywatny DNS. FQDN bazy powinien rozwiązywać się
   na `10.13.3.4`, a TCP 5432 musi być dostępny. Dla App Service potrzebna
   jest integracja wychodząca z VNet i odpowiednia delegacja podsieci.
3. Przypiąć do hosta backendu tożsamość `id-dreamplanner-backend` i skonfigurować
   pobieranie `database-url-app` z Key Vault `kv-dreamplanner-77076`
   do zmiennej `DATABASE_URL`. Samo przypięcie tożsamości nie ustawia zmiennej.
4. Przygotować uruchamianie migracji z dostępem do tej samej sieci i sekretu
   `database-url-migration`. Tożsamość aplikacji ma dostęp tylko do sekretu aplikacji.
5. Nadać backendowcowi uprawnienia do wdrażania jego aplikacji, a frontendowcowi
   do jego hostingu. Obecne role nie pozwalają im samodzielnie tworzyć serwerów.
6. Przekazać zespołowi adresy aplikacji HTTPS i sposób wdrażania; dopasować
   routing oraz reguły sieciowe do wybranego hostingu i sposobu wywoływania API.

## Backend — Dawid (`merchwadawid@fastmail.com`)

1. Zaakceptować zaproszenie do Azure przekazane przez Macieja.
2. Przygotować migracje Alembic na podstawie `database/schema.sql` i modelu danych.
   Uruchamiać je kontem `dreamplanner_migrator`, korzystając z
   `database-url-migration` jako `MIGRATION_DATABASE_URL`.
3. Podłączyć FastAPI do bazy `dreamplanner` przez `DATABASE_URL`
   (konto `dreamplanner_app`). Zachować TLS `verify-full`; obraz Linux musi
   zawierać certyfikaty CA pod `/etc/ssl/certs/ca-certificates.crt`.
4. Wdrożyć API i sprawdzić rzeczywisty zapis, odczyt, zmianę oraz usunięcie danych
   z hosta w Azure. Uzgodnić z frontendem endpointy, formaty odpowiedzi
   i sposób logowania; skonfigurować CORS dla adresu frontendu, jeśli potrzebny.
5. W razie potrzeby przygotować import danych demonstracyjnych z
   `database/fixtures/dreamplanner.json` — plik nie jest automatycznie importowany.

## Frontend — Mikołaj (`wrx87738@student.wroclaw.merito.pl`)

1. Zaakceptować zaproszenie do Azure przekazane przez Macieja.
2. Do czasu uruchomienia API korzystać z `database/fixtures/dreamplanner.json`.
3. Po otrzymaniu adresu API zastąpić mock wywołaniami backendu i ustawić adres
   API w konfiguracji aplikacji. Frontend nie potrzebuje connection stringa
   ani dostępu do Key Vault lub PostgreSQL.
4. Wdrożyć frontend na przygotowany hosting i wspólnie z backendem sprawdzić
   logowanie oraz operacje na danych z poziomu interfejsu.

Kolejność: **hosting i sieć → migracje oraz API → integracja frontendu → wspólny test**.
Hasła i connection stringi pozostają poza repozytorium i logami.
Szczegóły konfiguracji i polecenia: [terraform/README.md](terraform/README.md).
