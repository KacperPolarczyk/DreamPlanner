# Prywatne migracje PostgreSQL

Migracje uruchamia aplikacja `app-dreamplanner-migrations-77076` w istniejącym
planie App Service backendu. Korzysta z integracji z `snet-backend` i Private
Endpoint PostgreSQL. Publiczny runner GitHub nie nawiązuje połączenia SQL.

## Przebieg

1. CI buduje obraz backendu oznaczony SHA commita i wysyła go do ACR.
2. `database/scripts/run_private_migration.py` zatrzymuje aplikację migracyjną,
   ustawia obraz oraz unikalny identyfikator wykonania. Dopuszcza tylko IP runnera
   do odczytu statusu HTTPS. Nie otwiera żadnej reguły zapory PostgreSQL.
3. Po uruchomieniu aplikacja sprawdza, czy FQDN bazy wskazuje adres z `10.13.3.0/24`.
   Następnie wykonuje `python -m src.migrate` z obrazu wdrażanej wersji backendu.
4. Pipeline czeka na sukces oraz zgodność SHA i identyfikatora wykonania.
   Dopiero wtedy aktualizuje kontener właściwego backendu.
5. W bloku `finally` zatrzymuje aplikację migracyjną i usuwa tymczasową regułę
   dostępu do jej endpointu statusu. Publiczny dostęp do SQL pozostaje wyłączony.

## Uprawnienia i sekrety

Systemowa tożsamość aplikacji migracyjnej ma `AcrPull` na rejestrze i
`Key Vault Secrets User` wyłącznie na `database-url-migration`.
Connection string jest odwołaniem Key Vault w ustawieniach App Service;
runner GitHub nie pobiera hasła bazy. Konto SQL to `dreamplanner_migrator`.
Migrator sprawdza docelowy serwer, bazę, konto, certyfikat TLS oraz rewizję Alembic.
Używa blokady advisory zapobiegającej równoległej migracji.

Tożsamość GitHub ma `Website Contributor` na aplikacji migracyjnej. To uprawnienie
pozwala sterować kodem uprzywilejowanej migracji, więc zaufanie OIDC i zmiany
workflow wymagają takiej samej ochrony jak pozostałe wdrożenia backendu.

Endpoint `/status` jest tylko do odczytu. Nie przyjmuje poleceń ani connection
stringów. Domyślna reguła wejściowa aplikacji to Deny; pipeline dopuszcza jeden
adres `/32` na czas wykonania. Dostęp SCM pozostaje zablokowany. Portów SQL nie
udostępniamy na zewnątrz.

## Eksploatacja

- Wszystkie wykonania muszą być szeregowane przez `backend-azure-main`.
  Nie uruchamiać skryptu ręcznie równolegle z deploymentem.
- Po awarii runnera sprawdzić, czy aplikacja migracyjna jest zatrzymana i czy
  nie pozostała reguła `migration-runner`. Wymuszone przerwanie procesu może
  uniemożliwić wykonanie cleanup.
- Skrypt nie ponawia nieudanej migracji. Przed następnym uruchomieniem należy
  ustalić przyczynę błędu i stan bazy. Restart hosta App Service uruchamia proces
  ponownie; migracje muszą być transakcyjne/idempotentne zgodnie z Alembic.
- Aplikacja współdzieli CPU/RAM obecnego planu B1. Nie powstaje nowy plan,
  maszyna ani NAT Gateway. Podczas migracji obserwować gotowość backendu.
- Testy lokalne narzędzi: `python -m unittest discover -s database/tests -v`.
- Przed zamknięciem publicznego dostępu zweryfikować migrację przez Private
  Endpoint i wdrożyć nową wersję workflow. Stary workflow z publicznym runnerem
  SQL nie działa przy `publicNetworkAccess=Disabled`.
