# Monitoring i koszty DreamPlanner

## Zasoby i konfiguracja

Konfiguracja infrastruktury znajduje się w `terraform/monitoring.tf`, a dashboardu w `terraform/monitoring-workbook.json`. Workspace, Application Insights, Diagnostic settings, budżet i workbook są zarządzane przez Terraform. Wszystkie zasoby są w `rg-dreamplanner-dev`; monitoring jest w Poland Central. Nie tworzymy dodatkowego planu App Service ani maszyny wirtualnej.

Przed planowaniem Terraform ustaw `budget_contact_emails` w ignorowanym pliku `*.auto.tfvars`. Kwota budżetu to 100 EUR/miesiąc, próg kosztów rzeczywistych 80%. Budżet obejmuje wyłącznie tę grupę, nie całą subskrypcję. Pozostały kredyt studencki sprawdzaj osobno w Azure Education.

Pełny plan istniejącej infrastruktury wymaga przeglądu: deklaracje starszej tożsamości backendu nie odpowiadają aktualnej tożsamości systemowej aplikacji. Wdrożenie monitoringu wykonano osobnym planem zawierającym wyłącznie zasoby monitoringu i budżetu. Nie stosuj całego planu bez weryfikacji zmian.

Istniejące aplikacje mają konfigurację współdzieloną z zespołem aplikacyjnym. `bash scripts/configure-observability.sh` ustawia wyłącznie Health Check backendu i logi kontenerów/HTTP; nie nadpisuje zmiennych środowiskowych ani obrazów. Wykonuj po zalogowaniu `az login` i wybraniu właściwej subskrypcji.

Connection string Application Insights pobierz z Azure i zapisz bez wypisywania wartości jako sekret repozytorium `APPINSIGHTS_CONNECTION_STRING`. Uruchom workflow `frontend-azure.yml` na `main`, aby przebudować obraz. Klucz instrumentacji nie daje dostępu do odczytu danych, ale nie zapisujemy go w repozytorium. Dane użytkowników, hasła, connection stringi bazy i tokeny nie powinny trafiać do logów.

## Kontrola działania

1. Sprawdź `GET /api/v1/health/ready`: HTTP 200, `{"status":"ok"}`. Endpoint tworzy nowe połączenie i wykonuje `SELECT 1`; błąd bazy zwraca 503. Sam `/health` nie weryfikuje PostgreSQL.
2. Azure Portal → PostgreSQL → Diagnostic settings: `postgresql-to-log-analytics`, `PostgreSQLLogs` i `AllMetrics`.
3. Azure Portal → Log Analytics → `log-dreamplanner-dev-77076` → Logs. Pierwsze rekordy mogą pojawić się z opóźnieniem po utworzeniu ustawień; Azure podaje do 90 minut na rozpoczęcie przesyłania danych.
4. Azure Monitor → Workbooks → `DreamPlanner — monitoring`: sprawdź metryki i czas odpowiedzi. Odświeżaj widok podczas obserwacji.
5. Cost Management → właściwa grupa zasobów → Budgets → `budget-dreamplanner-monthly`: zweryfikuj kwotę, walutę, okres i trzech odbiorców progu 80%.

## Zapytania KQL w workspace

Liczba rekordów i najnowszy wpis z każdego źródła (bez ujawniania treści logów):

```kusto
union isfuzzy=true withsource=TableOrigin PGSQLServerLogs, AppServiceConsoleLogs, AppServiceHTTPLogs, AzureMetrics, AppPageViews, AppDependencies
| where TimeGenerated > ago(1h)
| summarize Records=count(), Latest=max(TimeGenerated) by TableOrigin
```

Logi serwera bazy:

```kusto
PGSQLServerLogs
| where TimeGenerated > ago(1h)
| project TimeGenerated, ErrorLevel, Message
| order by TimeGenerated desc
| take 20
```

Ruch backendu i czas odpowiedzi:

```kusto
AppServiceHTTPLogs
| where TimeGenerated > ago(1h)
| where _ResourceId has "/app-dreamplanner-backend-77076"
| summarize Requests=count(), ResponseTimeMs=avg(TimeTaken) by bin(TimeGenerated, 1m), ScStatus
```

Telemetria przeglądarki w workspace (w widoku Application Insights tabela ma alias `pageViews`):

```kusto
AppPageViews
| where TimeGenerated > ago(1h)
| summarize Views=count() by Name
```

## Kontrola kosztów

Workspace używa PerGB2018, retencji 30 dni i dziennego limitu 0,1 GB; Application Insights ma ten sam limit. Osiągnięcie limitu może przerwać zbieranie danych do następnego resetu; Azure może naliczyć także dane przekraczające limit w trakcie przetwarzania. Monitoruj `Usage` i bieżące koszty. Alert budżetowy nie zatrzymuje aplikacji ani bazy. Częstotliwość odświeżania kosztów nie pozwala traktować go jako alarmu natychmiastowego.

Dokumentacja: [opóźnienie uruchomienia diagnostyki](https://learn.microsoft.com/en-us/azure/azure-monitor/platform/diagnostic-settings), [limit danych Azure Monitor](https://learn.microsoft.com/en-us/azure/azure-monitor/logs/daily-cap), [logi PostgreSQL](https://learn.microsoft.com/en-us/azure/postgresql/monitor/how-to-configure-and-access-logs), [pola logów HTTP](https://learn.microsoft.com/en-us/azure/azure-monitor/reference/tables/appservicehttplogs).
