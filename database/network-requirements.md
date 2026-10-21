# DreamPlanner – Specyfikacja techniczna bazy danych

## 1. Informacje ogólne

| Parametr | Wartość |
|---|---|
| Dostawca chmury | Microsoft Azure |
| Usługa | Azure Database for PostgreSQL – Flexible Server |
| Silnik bazy danych | PostgreSQL |
| Model dostępu | Public Access (lista dozwolonych adresów IP) + Azure Private Endpoint |
| Port bazy danych | TCP 5432 |
| Szyfrowanie połączeń | TLS |
| DBA | Maciej Grzegorski |

## 2. Wymagania sieciowe

| Element | Nazwa / adresacja |
|---|---|
| Sieć wirtualna | `dreamplanner-vnet` – `10.13.0.0/16` |
| Podsieć frontend | `snet-frontend` – `10.13.1.0/24` |
| Podsieć backend | `snet-backend` – `10.13.2.0/24` |
| Podsieć Private Endpoint bazy | `snet-database` – `10.13.3.0/24` |
| Publiczny dostęp do PostgreSQL | Włączony, ograniczony zaporą usługi |
| Jedyny dozwolony publiczny adres źródłowy | `80.68.235.174/32` |
| Prywatny dostęp backendu | Azure Private Endpoint |
| Prywatna strefa DNS | `privatelink.postgres.database.azure.com` |

**Ważne:** Serwer PostgreSQL należy utworzyć w trybie **Public access (allowed IP addresses)**, a prywatną komunikację z backendem zapewnić przez **Private Endpoint**. Jest to inny model niż *Private access (VNet Integration)*. Podsieci `snet-database` przeznaczonej dla Private Endpoint **nie delegujemy** do `Microsoft.DBforPostgreSQL/flexibleServers`.

Private Endpoint otrzyma adres prywatny z podsieci `10.13.3.0/24`. Nie jest to stały adres IP samego zarządzanego serwera PostgreSQL. Aplikacja powinna używać nazwy FQDN serwera (`<nazwa-serwera>.postgres.database.azure.com`), nie adresu IP. Prywatna strefa DNS musi być powiązana z VNet tak, aby backend rozwiązywał tę nazwę na adres Private Endpoint.

## 3. Porty i dozwolona komunikacja

| Źródło | Cel | Protokół / port | Zasada |
|---|---|---|---|
| Backend w `10.13.2.0/24` | PostgreSQL przez Private Endpoint | TCP 5432 | Dozwolone |
| `80.68.235.174/32` (domowy publiczny adres IP DBA) | Publiczny endpoint PostgreSQL | TCP 5432 | Dozwolone po uwierzytelnieniu |
| Pozostałe publiczne adresy IP | Publiczny endpoint PostgreSQL | TCP 5432 | Zablokowane |
| Frontend / pozostali użytkownicy Internetu | PostgreSQL bezpośrednio | TCP 5432 | Niedozwolone |

**Reguła zapory na poziomie Azure Database for PostgreSQL:**

- Nazwa: `allow-dba`
- Start IP: `80.68.235.174`
- End IP: `80.68.235.174`

Zapis `/32` oznacza pojedynczy adres IPv4. Adres `80.68.235.174` jest domowym, publicznym adresem IP DBA i musi być faktycznym adresem **wyjściowym** połączenia. W razie zmiany adresu przez operatora konieczna będzie aktualizacja reguły zapory Azure.

Nie przewiduje się reguły dopuszczającej cały Internet ani ustawienia **Allow public access from any Azure service within Azure**. Ruch z backendu powinien korzystać z Private Endpoint, a nie publicznego wyjątku firewall. W sieci źródłowej musi być dozwolony ruch wychodzący TCP 5432. Ograniczenie publiczne do wskazanego IP nie zastępuje zabezpieczeń sieciowych dla Private Endpoint — dla dostępu prywatnego należy stosować odpowiednią konfigurację ruchu wewnątrz VNet.

## 4. Wymagane uprawnienia w Microsoft Azure

| Rola Azure RBAC | Zakres wykorzystania |
|---|---|
| `Contributor` | Tworzenie i konfiguracja zasobów PostgreSQL Flexible Server we właściwej Resource Group |
| `Reader` | Odczyt konfiguracji i stanu zasobów (alternatywnie dla dostępu tylko do odczytu) |
| `Network Contributor` | Zarządzanie zasobami sieciowymi, m.in. podsiecią i Private Endpoint, w odpowiednim zakresie |
| `Private DNS Zone Contributor` | Konfiguracja prywatnej strefy DNS i jej rekordów, jeśli jest powierzona DBA |

Uprawnienia należy nadać możliwie wąsko (konkretna Resource Group lub zasoby). Utworzenie Private Endpoint, jego zatwierdzenie i połączenie strefy DNS z VNet mogą wymagać uprawnień do kilku odrębnych zasobów.

**Azure RBAC nie jest tym samym co uprawnienia SQL.** Posiadanie roli `Contributor` nie daje automatycznie możliwości logowania do silnika PostgreSQL.

## 5. Wymagane uprawnienia PostgreSQL

Dostęp administracyjny do silnika PostgreSQL powinien pozwalać na:

- tworzenie baz danych (`CREATEDB`);
- tworzenie i zarządzanie rolami w zakresie wspieranym przez usługę (`CREATEROLE`);
- tworzenie oraz zmianę schematów, tabel, relacji, indeksów i ograniczeń;
- nadawanie uprawnień do baz, schematów i obiektów zgodnie z posiadanymi prawami;
- uruchamianie skryptów SQL i migracji struktury bazy danych.

W Azure Database for PostgreSQL Flexible Server konto administracyjne nie jest pełnym systemowym superuserem. Część działań jest ograniczona przez zarządzaną usługę. Należy rozdzielić konto administratora, konto do migracji Alembic oraz konto aplikacji z minimalnymi uprawnieniami do odczytu i zapisu niezbędnych danych.

## 6. Połączenie administracyjne

Obsługiwane narzędzia: **pgAdmin 4**, **DBeaver**, **psql**.

| Parametr | Wymaganie |
|---|---|
| Host | FQDN serwera PostgreSQL w Azure |
| Port | `5432` |
| Database | Nazwa bazy projektu DreamPlanner |
| Username | Konto PostgreSQL z odpowiednimi uprawnieniami |
| Password | Sekret poza kodem i repozytorium Git |
| SSL/TLS | Wymagane; zalecana walidacja certyfikatu (`verify-full`) |

Połączenie administracyjne przez publiczny endpoint jest dopuszczone wyłącznie z domowego adresu DBA `80.68.235.174/32` i wymaga prawidłowego uwierzytelnienia w PostgreSQL. Dostęp przez Private Endpoint wymaga łączności sieciowej z VNet oraz prawidłowej konfiguracji DNS.

## 7. Integracja z aplikacją i bezpieczeństwo

- Backend DreamPlanner: **Python / FastAPI**; połączenie do bazy przez Private Endpoint na TCP 5432.
- Frontend komunikuje się wyłącznie z REST API; nie posiada bezpośredniego połączenia SQL.
- Zmiany struktury danych: **Alembic**, we współpracy z backendem.
- Dane dostępowe: zmienne środowiskowe albo Azure Key Vault; żadnych haseł w Git.
- Połączenia: TLS oraz konta PostgreSQL o minimalnych niezbędnych uprawnieniach.
- Kopie zapasowe: mechanizmy wbudowane w usługę Azure, z retencją ustaloną dla projektu.

## 8. Założenia do weryfikacji

- `80.68.235.174` jest aktualnym, publicznym adresem wyjściowym domowej sieci DBA; jego ewentualna zmiana wymaga modyfikacji reguły firewall.
- Backend jest podłączony do VNet i rozwiązuje FQDN bazy na prywatny adres Private Endpoint.
- Zakres `10.13.0.0/16` nie koliduje z innymi sieciami, które będą łączone z Azure (VPN, peering).
- Po wdrożeniu można wykonać test: połączenie z backendu, połączenie z dozwolonego IP publicznego oraz odmowa połączenia z innego publicznego IP.

---

## 9. Konfiguracja wdrożeniowa i dostęp backendu

Zasoby warstwy danych są zdefiniowane w `terraform/database.tf`, a uprawnienia
zespołu w `terraform/team.tf`. Parametry projektu:

| Element | Wartość |
| --- | --- |
| Serwer | `psql-dreamplanner-dev-77076.postgres.database.azure.com` |
| Baza | `dreamplanner` |
| Silnik | PostgreSQL 16, Burstable B1ms, 32 GiB |
| Konto aplikacji | `dreamplanner_app` — odczyt i zapis danych, bez DDL |
| Konto migracji | `dreamplanner_migrator` — właściciel schematu `public` |
| Magazyn sekretów | `kv-dreamplanner-77076` |
| Sekret aplikacji | `database-url-app` |
| Sekret migracji | `database-url-migration` |
| Tożsamość backendu | `id-dreamplanner-backend` |

Backendowiec (`merchwadawid@fastmail.com`) otrzymuje odczyt zasobów
grupy projektu oraz dwóch sekretów: aplikacji i migracji. Frontendowiec
(`wrx87738@student.wroclaw.merito.pl`) otrzymuje odczyt zasobów bez dostępu do
sekretów bazy. Konta gości wymagają zaakceptowania zaproszenia do katalogu.
Uprawnienia do wdrażania aplikacji należy nadać na konkretnych zasobach hostingu,
gdy powstaną, bez udostępniania frontendowi zarządzania hostem backendu.

Terraform nie tworzy tabel aplikacyjnych ani kont SQL poza administratorem.
DBA uruchamia `database/bootstrap.py` po wdrożeniu, aby utworzyć konta SQL
i nadać uprawnienia. Kolejne tabele tworzą migracje Alembic uruchamiane kontem
migracji; domyślne uprawnienia przyznają kontu aplikacji dostęp do tych tabel
i sekwencji. Pełne polecenia znajdują się w `terraform/README.md`.

Sekrety zawierają standardowe URI PostgreSQL. Dla SQLAlchemy z Psycopg 3 należy
ustawić sterownik bez ręcznej przebudowy hasła:

```python
import os
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

url = make_url(os.environ["DATABASE_URL"]).set(drivername="postgresql+psycopg")
engine = create_engine(url, pool_pre_ping=True)
```

URI wymaga `sslmode=verify-full` i wskazuje zestaw certyfikatów CA w pliku
`/etc/ssl/certs/ca-certificates.crt`. W obrazie Linux backendu musi być zainstalowany
pakiet `ca-certificates`; dla innego systemu należy dostosować ścieżkę CA.
Connection string trafia wyłącznie do
konfiguracji backendu. Nie należy umieszczać go w zmiennych `VITE_*` ani logach.

Backend musi faktycznie korzystać z podsieci `snet-backend` i prywatnego DNS.
Sam hosting w Azure nie daje dostępu do prywatnego endpointu. W przypadku
App Service DevOps musi skonfigurować wychodzącą integrację z VNet i delegację
podsieci zgodnie z wymaganiami hostingu. Test `check_connection.py` uruchomiony
z komputera DBA weryfikuje dostęp administracyjny; test z hosta backendu jest
oddzielnym etapem integracji.
