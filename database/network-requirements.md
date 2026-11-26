# Sieć PostgreSQL DreamPlanner

## Model docelowy

PostgreSQL Flexible Server 16 korzysta z Azure Private Endpoint. Publiczny dostęp
jest wyłączany przez `public_network_access_enabled = false` w Terraform po
zweryfikowaniu prywatnych migracji i wdrożeniu odpowiadającego im workflow.
Stan rzeczywisty należy potwierdzić odczytem `network.publicNetworkAccess` w Azure.

Serwer pozostaje w modelu obsługującym Private Link; nie migrujemy go do wariantu
Flexible Server z delegowaną podsiecią VNet Integration. Podsieć Private Endpoint
nie ma delegacji `Microsoft.DBforPostgreSQL/flexibleServers`.

| Element | Konfiguracja |
| --- | --- |
| VNet | `dreamplanner-vnet`, `10.13.0.0/16` |
| Integracja backendu i aplikacji migracyjnej | `snet-backend`, `10.13.2.0/24` |
| Private Endpoint | `pe-dreamplanner-postgresql`, `snet-database`, `10.13.3.0/24` |
| Private DNS | `privatelink.postgres.database.azure.com`, link do VNet |
| FQDN | `psql-dreamplanner-dev-77076.postgres.database.azure.com` |
| Baza / port | `dreamplanner`, TCP 5432 |
| TLS | `verify-full`, systemowy zestaw CA w obrazie Linux |

Aplikacja używa FQDN, nie adresu IP. Prywatny DNS rozwiązuje nazwę na IP Private
Endpoint. Samo uruchomienie aplikacji w Azure nie zapewnia dostępu do VNet.

## Kontrola ruchu

| Źródło | Cel | Dostęp |
| --- | --- | --- |
| `snet-backend` (`10.13.2.0/24`) | `snet-database`, TCP 5432 | Allow, NSG priorytet 100 |
| Pozostały ruch VNet | `snet-database` | Deny, NSG priorytet 200 |
| Internet, również dawny adres DBA | PostgreSQL | Brak dostępu po wyłączeniu Public Access |
| Przeglądarka / frontend | PostgreSQL | Brak bezpośredniego dostępu SQL |

Na podsieci Private Endpoint włączone jest egzekwowanie NSG
(`NetworkSecurityGroupEnabled`). Reguły NSG są stanowe — odpowiedzi na dozwolone
połączenia nie wymagają otwarcia nowego połączenia z bazy do Internetu.
Private Endpoint nie jest maszyną z usługami SSH lub RDP.

Usuwamy publiczną regułę `allow-dba`; nie tworzymy wyjątku `0.0.0.0` dla usług
Azure ani reguły obejmującej Internet. Runner GitHub nie może tymczasowo otwierać
SQL na publicznym endpointcie. Migracje wykonuje host z `snet-backend`, opisany
w [procedurze prywatnych migracji](private-migrations.md).

## Konta i dostęp administracyjny

| Konto / sekret | Przeznaczenie |
| --- | --- |
| `dreamplanner_app` / `database-url-app` | Operacje aplikacji, bez DDL |
| `dreamplanner_migrator` / `database-url-migration` | Migracje Alembic |
| Administrator / `database-url-admin` | Inicjalizacja i administracja |

Sekrety są w `kv-dreamplanner-77076`. Role Azure RBAC nie zastępują kont i
uprawnień PostgreSQL. URI nie wolno umieszczać w Git, logach ani zmiennych `VITE_*`.
Konto aplikacji i konto migracji mają oddzielne uprawnienia.

Administracja z laptopa wymaga uprzednio przygotowanego dostępu do VNet
(np. VPN lub zaufany host administracyjny). Nie zakładamy, że taki VPN już istnieje.
Po zamknięciu dostępu publicznego skryptów `bootstrap.py` i `check_connection.py`
nie uruchamia się bezpośrednio z Internetu. Używają one sekretów i wykonują operacje
SQL, więc wymagają upoważnionego hosta w sieci prywatnej.

## Weryfikacja przełączenia

1. Potwierdzić zatwierdzony Private Endpoint, link DNS i integrację VNet backendu.
2. Uruchomić prywatne migracje i sprawdzić prywatny adres w wyniku oraz sukces.
3. Wdrożyć nowy workflow bez publicznych reguł SQL.
4. Wyłączyć Public Access i usunąć publiczne reguły zapory.
5. Potwierdzić `Disabled` w Azure, odmowę publicznego połączenia SQL i HTTP 200
   z `/api/v1/health/ready`. Endpoint wykonuje `SELECT 1` w bazie.
6. Wykonać uzgodniony test funkcjonalny zapisu w aplikacji. Sam healthcheck nie
   dowodzi poprawności wszystkich operacji CRUD.
