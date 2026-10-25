# Frontend w Azure

Adres: https://app-dreamplanner-frontend-77076.azurewebsites.net

Hosting: `app-dreamplanner-frontend-77076`, Linux Container, port 80,
plan `ASP-rgdreamplannerdev-a12c` współdzielony z backendem.
Frontend nie wymaga integracji z VNet: przeglądarka wywołuje publiczne HTTPS API.

Obraz: `acrdreamplannerdev77076.azurecr.io/dreamplanner-frontend:<tag>`.
App Service pobiera obraz przez własną tożsamość systemową z rolą AcrPull.
Administrator rejestru i jego hasło nie są potrzebne.

## Dostęp Mikołaja

Konto `wrx87738@student.wroclaw.merito.pl` ma Contributor na aplikacji frontendu,
Reader i AcrPush na istniejącym ACR oraz Reader grupy zasobów. Może wdrażać
obrazy i zmieniać konfigurację swojej aplikacji. Nie otrzymuje zarządzania
backendem, bazą ani dostępu do jej sekretów.

ACR działa w trybie RBAC Registry Permissions. AcrPush obejmuje rejestr,
a nie tylko repozytorium frontendowe. Zmiana ACR na tryb ABAC wymaga migracji
ról wszystkich korzystających z niego tożsamości, również backendu.

## GitHub Actions

Tożsamość `id-dreamplanner-frontend-github` ma AcrPush i Reader wyłącznie na ACR
oraz Website Contributor wyłącznie na aplikacji frontendu. Zaufanie OIDC obejmuje:
`repo:KacperPolarczyk/DreamPlanner:ref:refs/heads/main`.

Workflow `.github/workflows/frontend-azure.yml` działa po zmianie frontendu na
`main` lub ręcznym uruchomieniu dla `main`. Gałęzie PR nie mogą wdrażać aplikacji.
Nie dodawać środowiska GitHub do joba bez dostosowania podmiotu zaufania OIDC.

Identyfikator klienta jest publicznym identyfikatorem tożsamości, nie hasłem.
Konfigurację można odczytać przez `terraform output frontend_github_configuration`.

Pipeline buduje obraz z tagiem SHA commita, wysyła go i aktualizuje App Service.
Terraform nie cofa tagu obrazu ustawionego przez pipeline.

`VITE_API_BASE_URL` jest argumentem budowania obrazu: statyczny frontend Vite
nie odczyta nowej wartości z App Settings po zbudowaniu. Zmiana adresu API
wymaga ponownego zbudowania obrazu. Nie umieszczać sekretów w zmiennych VITE_*.

Dawid powinien dopuścić docelowy adres HTTPS frontendu w `CORS_ORIGINS`
backendu. Test HTTP strony nie zastępuje testu komunikacji przeglądarki z API.

Zasoby hostingu, tożsamości i ról są zdefiniowane w `terraform/frontend.tf`.
Istniejący plan i ACR są odczytywane jako data sources i nie są odtwarzane.

Pierwszy obraz `latest` został zbudowany i wdrożony. Strona odpowiada HTTP 200.
Workflow trzeba scalić do `main`, aby działało automatyczne wdrażanie; logowanie
OIDC z runnera zostanie zweryfikowane przy pierwszym uruchomieniu pipeline.
