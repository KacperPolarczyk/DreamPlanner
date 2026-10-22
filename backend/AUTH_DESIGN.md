# DreamPlanner — projekt uwierzytelniania i autoryzacji

Status: zatwierdzona architektura i reguły bezpieczeństwa MVP. Dokument nie
definiuje jeszcze nazw endpointów ani pełnego kontraktu API. Zasady modułów,
sesji SQLAlchemy i transakcji opisuje [ARCHITECTURE.md](ARCHITECTURE.md).

## Podstawa i zakres

Użytkownicy aplikacji mają **lokalne konta**; nie korzystają z Microsoft Entra
ID ani innego zewnętrznego dostawcy tożsamości. Tożsamość hosta Azure służąca
do pobrania sekretu bazy nie jest tożsamością użytkownika. Logowanie używa
e-maila i hasła. `database/schema.sql` zawiera `users.id`, `email`,
`password_hash`, `is_active`, `role_id` i słownik `roles`; unikalny indeks
`lower(email)` zabezpiecza e-mail bez względu na wielkość liter.

Identyfikatorem w tokenach i relacjach jest niezmienny `users.id`, nie e-mail.
E-mail jest normalizowany jednolicie przed wyszukiwaniem i zapisem. MVP ma
jedną rolę na konto: `admin`, `manager` albo `employee`, zgodnie z
`database/data-model.md`. Publiczna samodzielna rejestracja nie istnieje.

## Konto przed aktywacją i istniejące dane

Administrator tworzy konto z e-mailem i rolą bez wybierania hasła. Operacja
jawnie ustawia `is_active=false`; `activated_at` pozostaje puste. Aby zachować
`users.password_hash TEXT NOT NULL`, backend zapisuje tam hash Argon2id
losowego sekretu o wysokiej entropii, którego nikomu nie przekazuje i nie
przechowuje po zakończeniu operacji. Login wymaga **obu** warunków:
`is_active=true` i `activated_at IS NOT NULL`. Pierwsze prawdziwe hasło
ustawia wyłącznie użytkownik podczas aktywacji.

Migracja w `backend/` dodaje `users.activated_at TIMESTAMPTZ NULL`. Znacznik
odróżnia konto oczekujące na aktywację od konta wcześniej aktywowanego, lecz
później dezaktywowanego. Dezaktywacja ustawia `is_active=false` i zachowuje
`activated_at` oraz historię użytkownika. Ponowna aktywacja takiego konta to
oddzielna czynność administratora, a nie ponowne użycie tokenu pierwszej
aktywacji. Warto dodać ograniczenie PostgreSQL
`CHECK (NOT is_active OR activated_at IS NOT NULL)`; nie zależy ono od czasu.
Istniejący domyślny `is_active=true` nie zwalnia kodu z jawnego zapisu
`false` przy tworzeniu konta.

Migracja **nie może automatycznie uznać wszystkich istniejących kont za
aktywowane**. Przed dodaniem ograniczenia trzeba przejrzeć rzeczywiste dane:

1. Dla potwierdzonych, istniejących kont z prawidłowym hashem i znanym
   wcześniejszym stanem aktywacji można ustawić `activated_at` na czas
   migracji, opisując go jako znacznik migracyjny, nie historyczną datę
   aktywacji. Ich dotychczasowy `is_active` pozostaje bez zmian.
2. Dla kont, których hash lub historia aktywacji są niepewne, ustawić
   `is_active=false`, pozostawić `activated_at=NULL` i przeprowadzić
   kontrolowane wydanie nowego tokenu aktywacyjnego lub resetu. Nie ufać
   hashom ani hasłom z danych demonstracyjnych.
3. Dopiero po przeglądzie i uzupełnieniu danych walidować ograniczenie.
   Procedura powinna być testowana na kopii danych i nie ujawniać hashy.

Jeśli tabela `users` jeszcze nie istnieje, te zasady uwzględnia pierwsza
migracja. `database/fixtures/dreamplanner.json` pomija `password_hash` i nie
jest źródłem działających kont.

## Aktywacja i bootstrap pierwszego administratora

Token aktywacyjny ma co najmniej 256 losowych bitów, jest ważny **24 godziny**
i może być użyty tylko raz. W bazie znajduje się wyłącznie jego hash. Nowe
wydanie unieważnia wcześniejszy niezużyty token tego konta w jednej
transakcji. Administrator otrzymuje jawny token **tylko w odpowiedzi na
operację wydania**; backend nie udostępnia późniejszego odczytu. Zagubiony
token zastępuje się nowym. Odpowiedź ma `Cache-Control: no-store`.

MVP nie wymaga dostawcy e-maila. Administrator przekazuje token docelowemu
użytkownikowi zaufanym kanałem. Token jest sekretem typu bearer: osoba, która
go pozna przed użyciem, może ustawić hasło. Nie wpisujemy go do logów,
metryk, analityki, ścieżek URL ani parametrów query. Jeśli frontend oferuje
link, token może znaleźć się we fragmencie URL, który nie jest wysyłany do
serwera podczas pobierania strony; frontend usuwa fragment z historii przed
dalszą nawigacją i wysyła token w treści żądania HTTPS. Strona aktywacji nie
ładuje zbędnych zasobów zewnętrznych i używa `Referrer-Policy: no-referrer`.

Serwis aktywacji w jednej transakcji blokuje konto i rekord tokenu, sprawdza
hash, termin, brak wcześniejszego użycia oraz stan `activated_at=NULL` i
`is_active=false`; następnie zapisuje hash nowego hasła, `activated_at=now`,
`is_active=true` i `used_at=now`. Nie wydaje automatycznie sesji. Niepowodzenie
nie ujawnia, czy konto lub token istnieje.

Pierwszy administrator powstaje przez kontrolowane polecenie bootstrap
uruchomione przez zaufanego operatora z dostępem do bazy, bez publicznego
endpointu. Polecenie pobiera hasło interaktywnie przez `getpass` z potwierdzeniem,
zapisuje jego hash Argon2id i zakłada aktywne konto `admin` z `activated_at`
ustawionym na bieżący czas UTC. Hasło nie jest argumentem CLI ani treścią
komunikatu. Kontrola istnienia aktywnego administratora i zapis muszą być
atomowe; polecenie odmawia utworzenia kolejnego konta, gdy taki administrator
już istnieje. Bootstrap nie wydaje tokenu aktywacyjnego.

## Hasła i ochrona logowania

Hasła są haszowane Argon2id z odrębną losową solą; parametry są jawnie
ustalone i mierzone na docelowym hostingu. Początkowe minimum: 19 MiB pamięci,
2 przebiegi, równoległość 1 według [OWASP Password Storage Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html).
Haszowanie należy do serwisu `auth`, nigdy do schematu Pydantic. Hasła,
tokeny, nagłówki `Authorization`, cookies, hashe i connection stringi nie
trafiają do logów. Weryfikacja nieistniejącego konta może użyć stałego hasha
testowego, by ograniczyć różnicę czasu odpowiedzi.

Login i użycie tokenu aktywacyjnego lub resetu mają limity prób per adres IP
oraz per konto lub znormalizowany e-mail, współdzielone między instancjami
aplikacji. Konkretne progi i miejsce egzekwowania limitu trzeba dobrać przed
uruchomieniem publicznym; pamięć pojedynczego procesu nie wystarcza przy
wielu instancjach. Odpowiedzi nie ujawniają, czy e-mail istnieje, konto jest
nieaktywne lub hasło jest błędne. Nie stosujemy trwałej blokady konta po
kilku próbach, którą można wykorzystać do odmowy usługi. Dotyczy to również
kont administratorów.

Zmiana hasła wymaga potwierdzenia obecnego hasła i unieważnia **wszystkie**
sesje, łącznie z bieżącą. W MVP administrator może wydać użytkownikowi
jednorazowy token resetu, używający tych samych zasad przechowywania i
wygaśnięcia co token aktywacyjny. Reset hasła także odwołuje wszystkie
sesje. Automatyczna dostawa resetu e-mailem pozostaje późniejszym
rozszerzeniem.

## Access token JWT

Access token jest podpisany **HS256** i ważny **10 minut**. Klucz ma co
najmniej 256 losowych bitów, pochodzi ze środowiska lub magazynu sekretów,
nie z repozytorium. Backend ma jawnie skonfigurowane bieżący klucz oraz, na
czas rotacji, poprzedni klucz. Identyfikator `kid` może wskazywać wyłącznie
klucz z tej listy; nagłówek tokenu nie steruje pobieraniem kluczy z sieci ani
wyborem algorytmu.

Wymagane claims: `sub` jako tekstowy `users.id`, skonfigurowane `iss` i `aud`,
`iat`, `nbf`, `exp`, losowy identyfikator sesji `sid` i
`token_use="access"`. `jti` jest opcjonalny, ponieważ odwołanie odbywa się
przez `sid`; można go dodać dla identyfikacji tokenu w audycie bez logowania
całego JWT. W payloadzie nie ma hasła, e-maila, danych osobowych ani roli
traktowanej jako bieżące źródło uprawnień.

Zależność HTTP przyjmuje JWT tylko jako `Authorization: Bearer`. Sprawdza
podpis dozwolonym algorytmem, typ i komplet claims, `iss`, `aud`, daty oraz
limit długości życia tokenu. Następnie na tej samej sesji żądania pobiera
użytkownika i sesję z PostgreSQL: konto musi być aktywne i aktywowane,
sesja nieodwołana oraz niewygasła. Aktualną rolę odczytuje z bazy. Zmiana
roli lub dezaktywacja działa od następnego chronionego żądania.

## Sesje i ścisła rotacja refresh tokenów

Po poprawnym logowaniu backend tworzy osobną sesję dla przeglądarki i wydaje
losowy, nieprzezroczysty refresh token o co najmniej 256 bitach. W bazie
przechowuje wyłącznie hash SHA-256 całego tokenu. Token nie jest JWT. Sesja
wygasa po **24 godzinach bez odnowienia** albo bezwzględnie po **7 dniach od
zalogowania**. Przy rotacji `idle_expires_at` przesuwa się najwyżej do
`absolute_expires_at`; zwykłe żądania API nie przedłużają sesji.

Minimalny stan PostgreSQL:

| Tabela | Pola i indeksy |
| --- | --- |
| `auth_sessions` | `id UUID PK`, `user_id BIGINT NOT NULL FK users`, `created_at`, `idle_expires_at`, `absolute_expires_at`, `revoked_at NULL`; indeks po `user_id`; ograniczenie `created_at < idle_expires_at <= absolute_expires_at`. |
| `refresh_tokens` | `id UUID PK`, `session_id UUID NOT NULL FK auth_sessions`, `token_hash BYTEA NOT NULL UNIQUE`, `issued_at`, `used_at NULL`; indeks po `session_id`; indeks częściowy `UNIQUE(session_id) WHERE used_at IS NULL`. |
| `account_action_tokens` | `id UUID PK`, `user_id BIGINT NOT NULL FK users`, `purpose` z ograniczeniem do `activation` lub `password_reset`, `token_hash BYTEA NOT NULL UNIQUE`, `issued_at`, `expires_at`, `used_at NULL`, `revoked_at NULL`, `issued_by BIGINT NULL FK users` (`NULL` podczas bootstrap); indeks częściowy `UNIQUE(user_id, purpose) WHERE used_at IS NULL AND revoked_at IS NULL`; `CHECK (issued_at < expires_at)`. |

Żaden indeks częściowy ani `CHECK` nie używa `now()`: PostgreSQL nie powinien
opierać niezmiennych indeksów na zmieniającym się zegarze. Wygaśnięcie jest
sprawdzane przez serwis względem aktualnego czasu UTC. Przed wydaniem nowego
tokenu akcji stary niezużyty rekord zostaje oznaczony jako odwołany, także
jeśli już wygasł. Zużyte tokeny refresh pozostają do wykrywania ponownego
użycia aż do wygaśnięcia rodziny sesji; później podlegają czyszczeniu.

Rotacja przebiega w **jednej transakcji PostgreSQL**:

1. Wyszukać token po hashu i zablokować wiersz jego sesji (`FOR UPDATE`).
   Wszystkie rotacje oraz odwołania tej sesji używają tej samej blokady.
2. Po uzyskaniu blokady odczytać świeży stan tokenu, konta i sesji, w tym
   terminy oraz `used_at`; nie polegać na obiekcie ORM pobranym przed blokadą.
3. Dla bieżącego tokenu ustawić `used_at`, dodać hash nowego tokenu i
   przedłużyć termin bezczynności, nie przekraczając 7 dni; zatwierdzić.
4. Dla **znanego, wcześniej użytego** tokenu odwołać całą sesję i zatwierdzić
   odwołanie **przed** zwróceniem błędu. Nie wydawać nowego tokenu. Dla
   nieznanego tokenu odmówić bez możliwości ustalenia sesji do odwołania.

Nie stosujemy okna łaski dla zużytego tokenu. Backend nie potrafi odróżnić
wyścigu dwóch kart od użycia skradzionego tokenu; wątpliwość kończy się
ponownym logowaniem. Przeglądarka wysyła tylko jedno odnowienie naraz w karcie
i koordynuje karty wspólną blokadą, np. Web Locks; może udostępnić nowy access
token kartom przez BroadcastChannel. Utrata odpowiedzi po zatwierdzeniu
rotacji też może wymagać ponownego logowania. To zamierzony kompromis
ścisłego wykrywania ponownego użycia.

Wylogowanie z jednej sesji ustawia jej `revoked_at`; wylogowanie ze wszystkich
urządzeń, dezaktywacja, zmiana i reset hasła odwołują wszystkie sesje
użytkownika. Zmiana konta lub hasła oraz odwołanie sesji należą do jednej
transakcji. Odwołany `sid` blokuje również niewygasły access token.

## React, cookies i ochrona żądań

Access token jest przechowywany tylko w pamięci React i wysyłany w nagłówku
`Authorization`; nie trafia do `localStorage` ani `sessionStorage`. Refresh
token jest przekazywany wyłącznie w cookie `HttpOnly`; w produkcji także
`Secure`. Cookie nie zawiera access tokenu. Odpowiedzi z tokenami mają
`Cache-Control: no-store`. Przy odświeżeniu strony React może odzyskać
access token przez odnowienie sesji.

`SameSite`, `Path` i CORS zależą od rzeczywistych adresów, nie są jeszcze
zamrożone. `Domain` domyślnie pomijamy, aby cookie należało tylko do hosta
API. Preferowany jest wąski `Path` obejmujący jedynie operacje auth; usunięcie
cookie używa tej samej nazwy i ścieżki.

| Wariant | Konfiguracja wyjściowa |
| --- | --- |
| Lokalnie, `http://localhost:5173` i `http://localhost:8000` | `HttpOnly`, `SameSite=Lax`, bez `Domain`, `Secure=false` wyłącznie dla lokalnego HTTP; CORS dopuszcza dokładnie origin frontendu i credentials. Nie mieszać `localhost` z `127.0.0.1`. |
| Azure, wspólny origin przez proxy | `HttpOnly; Secure`, zwykle `SameSite=Lax`; CORS zbędny. |
| Azure, różne origin, ten sam site | `HttpOnly; Secure`, zwykle `SameSite=Lax`; CORS dopuszcza dokładny origin frontendu i credentials. Cookie pozostaje host-only na API. |
| Azure, różne site | Może wymagać `SameSite=None; Secure` i zależy od ograniczeń cookies innych firm. Preferować wspólny site lub proxy; konfigurację potwierdzić testem w docelowych przeglądarkach. |

Operacje ustawiające lub wykorzystujące refresh cookie, w tym login,
odnowienie i wylogowanie, wymagają bezpiecznej metody HTTP oraz walidacji
`Origin` względem jawnej listy. Brak wiarygodnego `Origin` w żądaniu
przeglądarkowym jest odrzucany; `SameSite` i CORS nie zastępują ochrony CSRF.
Brak zmian stanu przez `GET`. Frontend ogranicza XSS przez bezpieczne
renderowanie, brak niekontrolowanego HTML, politykę CSP i brak sekretów w
trwałym magazynie przeglądarki. `HttpOnly` utrudnia odczyt tokenu, lecz nie
powstrzymuje złośliwego skryptu przed wykonaniem żądania w przeglądarce.

## Autoryzacja i błędy

Zależność HTTP weryfikuje token i bieżącą rolę; serwis sprawdza dodatkowo
prawo do konkretnego zasobu. `admin` zarządza kontami i stanowiskami,
`manager` grafikiem i przydziałami, `employee` własną dyspozycyjnością i
opublikowanymi zmianami według `database/data-model.md`. Sam poprawny JWT
lub rola nie uprawnia do odczytu cudzego zasobu. Pracownik nie otrzymuje
nieopublikowanych przydziałów.

Niepoprawny lub wygasły token i brak uwierzytelnienia dają `401`; brak
uprawnienia po uwierzytelnieniu daje `403`. Komunikaty logowania, aktywacji i
resetu nie ujawniają istnienia konta. Format odpowiedzi i nazwy tras pozostają
częścią przyszłego kontraktu API. Serwis zgłasza błędy aplikacyjne, a warstwa
HTTP tłumaczy je na odpowiedzi zgodnie z `ARCHITECTURE.md`.

## Testy wymagane w MVP

- Aktywacja: prawidłowy token, wygaśnięcie po 24 godzinach, ponowne użycie,
  ponowne wydanie, konto już aktywowane, równoczesne użycie jednego tokenu,
  brak ujawnienia tokenu w logach i brak późniejszego odczytu.
- Logowanie: poprawne hasło, błędny e-mail/hasło, konto nieaktywne,
  `activated_at=NULL`, limity prób i jednolite komunikaty.
- JWT: podpis i lista algorytmów, `iss`, `aud`, czasy, typ, `sid`, rotacja
  klucza, aktywność konta i aktualna rola. Brak dostępu po dezaktywacji.
- Sesje: limit 24 godzin i 7 dni, wylogowanie pojedyncze i globalne,
  zmiana/reset hasła odwołujące także bieżącą sesję.
- Rotacja na PostgreSQL przy dwóch niezależnych połączeniach: dokładnie jedno
  skuteczne użycie tokenu, brak dwóch aktywnych następców, ponowne użycie
  odwołujące sesję, wyścig z wylogowaniem oraz utrata odpowiedzi.
- HTTP i przeglądarka: `HttpOnly`, produkcyjne `Secure`, usuwanie cookie z
  właściwą ścieżką, dokładny CORS, odrzucenie obcego lub brakującego
  `Origin`, brak sekretów w URL, logach i pamięci trwałej frontendu.
- Uprawnienia: rola i własność zasobu, zmiana roli w bazie, brak podglądu
  nieopublikowanych przydziałów przez pracownika.

Testy ograniczeń, migracji i współbieżności używają PostgreSQL, nie atrap
sesji ani produkcyjnej bazy.

## MVP i dalsze rozszerzenia

MVP obejmuje: administracyjne tworzenie kont, kontrolowany bootstrap,
jednorazową aktywację, Argon2id, JWT i sesje, ścisłą rotację refresh tokenów,
wylogowanie pojedyncze i globalne, administracyjny reset hasła, ochronę
logowania, CSRF i XSS oraz RBAC z kontrolą zasobów. Frontend koordynuje
odnowienie między kartami. Wszystkie sekrety i parametry hostingu pochodzą z
konfiguracji poza kodem.

Później można dodać: automatyczną dostawę aktywacji i resetu e-mailem,
samodzielny reset, MFA, widok urządzeń i alerty o podejrzanym użyciu.

Otwarte przed implementacją pozostają: nazwy i formaty API, adresy hostingu
oraz ostateczne ustawienia cookie/CORS, kanał ręcznego przekazania tokenu,
parametry Argon2id i progi limitów prób po pomiarach, sposób współdzielonego
egzekwowania limitów, a także plan przeglądu istniejących kont przed migracją.
