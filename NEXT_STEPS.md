# Uruchomienie aplikacji w Azure

Baza PostgreSQL, Private Endpoint, Key Vault i konta SQL są przygotowane.
Backend działa w Azure; migracje wykonano do rewizji 20261023_02.
Endpoint gotowości potwierdza połączenie backendu z bazą.
Hosting frontendu i role wdrożeniowe opisano w [frontend/AZURE.md](frontend/AZURE.md).

## DevOps — Kacper

1. Uzgodnić z backendowcem hosting backendu i utworzyć hosting frontendu; zapisać konfigurację w Terraform.
2. Zapewnić backendowi połączenie wychodzące przez `snet-backend` w
   `dreamplanner-vnet` oraz prywatny DNS. FQDN bazy powinien rozwiązywać się
   na `10.13.3.4`, a TCP 5432 musi być dostępny. Dla App Service potrzebna
   jest integracja wychodząca z VNet i odpowiednia delegacja podsieci.
3. Przypiąć do hosta backendu tożsamość `id-dreamplanner-backend` i skonfigurować
   pobieranie `database-url-app` z Key Vault `kv-dreamplanner-77076`
   do zmiennej `DATABASE_URL`. Samo przypięcie tożsamości nie ustawia zmiennej.
4. Przygotować uruchamianie migracji z dostępem do tej samej sieci i sekretu
   `database-url-migration`. Tożsamość aplikacji ma dostęp tylko do sekretu aplikacji.
5. Uprawnienia frontendowca do jego hostingu i ACR definiuje `terraform/frontend.tf`. Backendowiec ma już
   Contributor w grupie projektu i może samodzielnie tworzyć zasoby backendu.
6. Przekazać zespołowi adresy aplikacji HTTPS i sposób wdrażania; dopasować
   routing oraz reguły sieciowe do wybranego hostingu i sposobu wywoływania API.

## Backend — Dawid (`merchwadawid@fastmail.com`)

1. Zaakceptować zaproszenie do Azure przekazane przez Macieja.
   Backend ma Contributor w `rg-dreamplanner-dev`: może tworzyć ACR, hosting,
   konfigurować sieć i wdrażać aplikację. Ma również push/pull i listowanie
   repozytoriów ACR w trybie RBAC oraz ABAC. Zakres obejmuje całą grupę projektu.
   Przy pobieraniu obrazu przez hosting użyć `id-dreamplanner-backend`, która
   ma już role odczytu obrazów. Przypiąć ją do hosta i wskazać w konfiguracji
   uwierzytelniania rejestru. Contributor nie nadaje prawa do przypisywania ról IAM.
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
