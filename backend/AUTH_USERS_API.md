# DreamPlanner — kontrakt auth/users REST API v1

Status: kontrakt endpointów do zatwierdzenia. Dokument uszczegóławia
[ARCHITECTURE.md](ARCHITECTURE.md), [AUTH_DESIGN.md](AUTH_DESIGN.md) i
[API_DESIGN.md](API_DESIGN.md). Nie zmienia JWT, sesji, Argon2id, wspólnych
standardów REST ani referencyjnego modelu w `database/`.

## Zakres i kolejność implementacji

**Podstawowe MVP (pierwsza działająca wersja backendu):** kontrolowany bootstrap
pierwszego administratora poza API; tworzenie kont i wydawanie pierwszego
tokenu aktywacyjnego; ponowne wydawanie tokenu przed aktywacją; aktywacja;
logowanie, odnowienie i wylogowanie bieżącej sesji; `GET /users/me`; lista i
szczegóły użytkowników dla administratora. Od początku obowiązują wszystkie
zatwierdzone zabezpieczenia tych operacji, w tym ścisła rotacja refresh tokenu,
limity prób, sprawdzanie `Origin`, brak sekretów w logach i kontrola aktualnej
roli oraz stanu konta przy każdym chronionym żądaniu.

**Drugi etap implementacji:** wylogowanie ze wszystkich sesji, zmiana własnego
hasła, zmiana roli, dezaktywacja oraz ponowna aktywacja konta. Etap ten
domyka zarządzanie kontami przewidziane w MVP. Ochrona ostatniego aktywnego
administratora obowiązuje od chwili udostępnienia zmiany roli i dezaktywacji.

**Później:** administracyjny reset hasła jako planowane rozszerzenie,
samodzielny reset, automatyczna dostawa tokenów, MFA, lista urządzeń i alerty.
`AUTH_DESIGN.md` zalicza administracyjny reset do pierwotnie opisanego MVP;
zgodnie z nowszą decyzją projektową jego endpoint pozostaje w kontrakcie,
ale nie blokuje pierwszego ani drugiego etapu. Reaktywacja z drugiego etapu
korzysta z tego samego *typu rekordu tokenu* `password_reset`, ale ma osobny
endpoint użycia i nie wymaga administracyjnego resetu konta aktywnego.

## Wspólne reguły HTTP i danych

- Wszystkie ścieżki mają prefiks `/api/v1` i nie kończą się ukośnikiem.
  Żądania i udane odpowiedzi z treścią mają `application/json`. Błędy z
  treścią mają `application/problem+json` zgodnie z RFC 9457.
- Chronione endpointy przyjmują JWT wyłącznie w `Authorization: Bearer`.
  Zależność sprawdza JWT, sesję, `is_active=true`, `activated_at != null`
  oraz bieżącą rolę w bazie. Rola w JWT nie jest źródłem uprawnień.
- Refresh token występuje wyłącznie w cookie `HttpOnly`, produkcyjnie także
  `Secure`. Login, refresh i wylogowanie walidują `Origin` według
  `AUTH_DESIGN.md`; wymagają HTTPS poza uzgodnionym lokalnym HTTP. Nazwa,
  `SameSite`, `Path` i CORS zależą od adresów hostingu. Cookie usuwamy z tą
  samą nazwą i ścieżką, z jaką zostało ustawione.
- Odpowiedzi zawierające token oraz odpowiedzi z danymi chronionymi, także
  odpowiednie błędy, mają `Cache-Control: no-store`. Tokeny nie trafiają do
  URL, query, logów, metryk ani późniejszych odpowiedzi odczytu. Endpointy
  wydające token zwracają go **tylko raz**, w odpowiedzi na wydanie.
- E-mail normalizuje się jednolicie przed wyszukiwaniem i zapisem. Publiczny
  identyfikator użytkownika (`BIGINT`) jest dziesiętnym stringiem w JSON
  oraz w ścieżce. ID spoza zakresu `BIGINT` lub o złym formacie daje `422`.
  Chwile zdarzeń są w UTC w formacie RFC 3339. Nie wystawiamy `role_id`.
- Sekrety w poniższych przykładach są wyłącznie oznaczeniami pól. Przykłady
  OpenAPI nie powinny zawierać nawet fikcyjnych wartości haseł i tokenów.
  Hasła i tokeny nie są odzwierciedlane w błędach walidacji.
- Jeden serwis koordynuje każdą operację zapisującą na jednej sesji SQLAlchemy.
  Repozytoria nie wykonują `commit()`. Serwis zatwierdza dokładnie raz przed
  odpowiedzią lub wycofuje transakcję przy błędzie; wyjątkiem jest zamierzone
  zatwierdzenie odwołania sesji przed błędem przy wykryciu ponownego użycia
  refresh tokenu. Odczyty nie wymagają `commit()`.

Publiczny `User` (jedyny kształt odczytu konta):

```json
{
  "id": "15",
  "email": "anna@example.com",
  "full_name": "Anna Nowak",
  "role": "employee",
  "is_active": true,
  "activated_at": "2027-01-05T12:00:00Z",
  "created_at": "2027-01-04T09:00:00Z",
  "updated_at": "2027-01-05T12:00:00Z"
}
```

`activated_at` jest `null` przed pierwszą aktywacją. Dezaktywacja i
reaktywacja nie zmieniają chwili pierwszej aktywacji. Nigdy nie zwracamy
`password_hash`, hashy tokenów, sekretów sesji ani obiektów ORM.

### Problem Details

Wspólne statusy błędów: `400` — błędna składnia JSON; `422` — niepoprawny
format lub wartość pola; `401` — brak lub nieważne uwierzytelnienie;
`403` — brak uprawnienia albo niedozwolony `Origin`; `404` — nieistniejący
lub ukryty zasób; `409` — konflikt stanu; `429` — ograniczenie prób, z
`Retry-After`. Chroniony endpoint przy `401` wysyła odpowiednie
`WWW-Authenticate: Bearer`. Problem `422` ma typ
`urn:dreamplanner:problem:validation-error` i tablicę `errors`, której
elementy zawierają tylko `location`, stabilny `code` i bezpieczny `message`.
Nie kopiujemy `input`, `ctx` ani wartości sekretów z Pydantic.

Przykład błędu logowania (identyczny dla nieznanego e-maila, nieaktywnego
konta i złego hasła):

```json
{
  "type": "urn:dreamplanner:problem:invalid-credentials",
  "title": "Nie udało się zalogować",
  "status": 401,
  "detail": "Nieprawidłowe dane logowania."
}
```

Stabilne typy problemów specyficznych dla tego kontraktu:

| Typ (`urn:dreamplanner:problem:` + sufiks) | Status | Zastosowanie |
| --- | --- | --- |
| `invalid-credentials` | `401` | Logowanie, bez ujawniania istnienia lub stanu konta. |
| `invalid-session` | `401` | Brak, wygaśnięcie, odwołanie lub ponowne użycie refresh tokenu; ten sam komunikat. |
| `invalid-action-token` | `401` | Nieznany, wygasły, zużyty, odwołany albo niepasujący do stanu konta token aktywacji/reaktywacji/resetu; jeden komunikat. Bez wyzwania Bearer. |
| `invalid-current-password` | `403` | Niepoprawne bieżące hasło zalogowanego użytkownika. |
| `email-conflict` | `409` | Duplikat e-maila przy administracyjnym tworzeniu konta. |
| `account-state-conflict` | `409` | Operacja administratora niedozwolona w jawnym stanie konta. |
| `last-admin-conflict` | `409` | Próba odebrania roli lub aktywności ostatniemu aktywnemu administratorowi. |
| `rate-limited` | `429` | Przekroczony limit prób. |
| `validation-error` | `422` | Walidacja wartości żądania. |

Pozostałe `400`, `401`, `403`, `404` mogą mieć `type: "about:blank"`.
Nieoczekiwany `500` ma bezpieczną odpowiedź bez szczegółów implementacji.
Wszystkie powyższe typy należy opisać w OpenAPI. Login, aktywacja,
reaktywacja i użycie resetu nie różnicują odpowiedzi według istnienia konta
ani przyczyny nieważności tokenu. Limity prób tych operacji obowiązują per IP
oraz per konto lub znormalizowany e-mail, we współdzielonym mechanizmie
między instancjami; progi i miejsce egzekwowania trzeba ustalić przed
publicznym uruchomieniem. Nie ma trwałej blokady konta.

## Podstawowe MVP — auth

### `POST /auth/sessions` — logowanie

Publiczny; wymaga dozwolonego `Origin`. Żądanie:

```json
{"email":"anna@example.com","password":"<hasło>"}
```

`200 OK`:

```json
{"access_token":"<JWT>","token_type":"bearer","expires_in":600}
```

Refresh token trafia tylko do `Set-Cookie`. Błędy: `400`, `422`,
`401 invalid-credentials`, `403`, `429 rate-limited`. W jednej transakcji
po sprawdzeniu hasła oraz obu warunków aktywności tworzy sesję i hash
pierwszego refresh tokenu. Commit poprzedza odpowiedź z JWT i cookie.

### `POST /auth/sessions/current/refresh` — odnowienie

Publiczny dla posiadacza refresh cookie; wymaga dozwolonego `Origin`; bez
Bearer. Brak treści JSON. `200 OK`:

```json
{"access_token":"<nowy JWT>","token_type":"bearer","expires_in":600}
```

Nowy refresh token trafia tylko do `Set-Cookie`. Błędy: `401 invalid-session`,
`403`, `429 rate-limited`. Rotacja odbywa się dokładnie według
`AUTH_DESIGN.md`: blokada wiersza sesji, ponowny odczyt stanu po blokadzie,
zużycie starego tokenu, zapis nowego hasha i terminu bezczynności w jednej
transakcji. Wykrycie znanego zużytego tokenu odwołuje sesję i zatwierdza to
przed odpowiedzią `401`; nieznany token nie wskazuje sesji do odwołania.

### `DELETE /auth/sessions/current` — wylogowanie bieżącej sesji

Bearer każdej roli, dozwolony `Origin`; brak JSON w żądaniu i odpowiedzi.
`204 No Content`; błędy: `401`, `403`. Sesję wskazuje `sid` access tokenu,
nie identyfikator z klienta. W jednej transakcji ustawia jej `revoked_at`.
Po commit usuwa refresh cookie, nawet jeśli go brakowało. Operacja nie
odwołuje sesji innych urządzeń.

### `POST /auth/activations` — pierwsza aktywacja

Publiczny; tylko token `purpose=activation`. Żądanie:

```json
{"token":"<token aktywacyjny>","new_password":"<nowe hasło>"}
```

`204 No Content`, bez JSON; błędy: `400`, `422`,
`401 invalid-action-token`, `429 rate-limited`. W jednej transakcji blokuje
konto i token, sprawdza hash, termin, jednorazowość oraz `activated_at=null`
i `is_active=false`; zapisuje hash hasła, `activated_at=now`,
`is_active=true`, `used_at=now`. Nie tworzy sesji. Token jest ważny 24 godziny.

## Podstawowe MVP — users

### `GET /users/me` — własne dane

Bearer każdej roli; zasób wynika wyłącznie z `sub` tokenu. Brak JSON w
żądaniu. `200 OK`: obiekt `User` pokazany wyżej. Błąd: `401`.
Tylko odczyt, bez commit.

### `POST /users` — konto i pierwszy token aktywacyjny

Bearer `admin`. Żądanie:

```json
{"email":"jan@example.com","full_name":"Jan Kowalski","role":"employee"}
```

`201 Created`, `Location: /api/v1/users/16`:

```json
{
  "id":"16","email":"jan@example.com","full_name":"Jan Kowalski",
  "role":"employee","is_active":false,"activated_at":null,
  "created_at":"2027-01-06T10:00:00Z","updated_at":"2027-01-06T10:00:00Z",
  "activation_token":"<wydany tylko raz>",
  "activation_expires_at":"2027-01-07T10:00:00Z"
}
```

Błędy: `400`, `401`, `403`, `409 email-conflict`, `422`. Jedna transakcja
tworzy konto z jawnie ustawionym `is_active=false`, `activated_at=null`,
hashem losowego nieprzekazywanego sekretu w wymaganym `password_hash` oraz
hashem tokenu aktywacyjnego. Nie ma samodzielnej rejestracji.

### `POST /users/{user_id}/activation-tokens` — ponowne wydanie

Bearer `admin`; wyłącznie istniejące konto `activated_at=null` i
`is_active=false`. Brak JSON w żądaniu. `200 OK`:

```json
{"activation_token":"<wydany tylko raz>","expires_at":"2027-01-07T11:00:00Z"}
```

Błędy: `401`, `403`, `404`, `409 account-state-conflict`, `422`.
W jednej transakcji blokuje konto, odwołuje poprzedni niezużyty token,
także już wygasły, i zapisuje hash nowego. Poprzedni token przestaje działać.

### `GET /users` — administracyjna lista

Bearer `admin`; obejmuje konta aktywne i nieaktywne. Brak JSON w żądaniu.
Przykład `GET /users?role=employee&is_active=true&limit=25&offset=0&sort=full_name`.
`200 OK`:

```json
{
  "items":[{
    "id":"15","email":"anna@example.com","full_name":"Anna Nowak",
    "role":"employee","is_active":true,
    "activated_at":"2027-01-05T12:00:00Z",
    "created_at":"2027-01-04T09:00:00Z",
    "updated_at":"2027-01-05T12:00:00Z"
  }],
  "limit":25,"offset":0,"total":1
}
```

Błędy: `401`, `403`, `422`. Tylko odczyt, bez commit. Dozwolone filtry:
`role` (`admin`, `manager`, `employee`), `is_active` (boolean), `activated`
(czy `activated_at` jest niepuste) oraz `q` (niewrażliwe na wielkość liter
wyszukiwanie w e-mailu i `full_name`). `limit` domyślnie 25, maksymalnie
100; `offset` domyślnie 0. `sort`: `full_name`, `email`, `created_at` oraz
warianty z `-`; domyślnie `full_name`. `id` stabilizuje remisy. `total`
uwzględnia filtry. Nieznane filtry i sortowanie dają `422`.

### `GET /users/{user_id}` — administracyjne szczegóły

Bearer `admin`; dowolne istniejące konto. Brak JSON w żądaniu.
`200 OK`: obiekt `User` pokazany wyżej. Błędy: `401`, `403`, `404`, `422`.
Tylko odczyt, bez commit. Manager i employee nie uzyskują tą drogą danych
innych osób.

## Drugi etap implementacji

### `DELETE /auth/sessions` — wylogowanie ze wszystkich sesji

Bearer każdej roli, dozwolony `Origin`; brak JSON w żądaniu i odpowiedzi.
`204 No Content`; błędy: `401`, `403`. Jedna transakcja odwołuje wszystkie
sesje użytkownika, w tym bieżącą; po commit usuwa refresh cookie.

### `POST /users/me/password-change` — zmiana własnego hasła

Bearer każdej roli; tylko własne konto. Żądanie:

```json
{"current_password":"<obecne hasło>","new_password":"<nowe hasło>"}
```

`204 No Content`, bez JSON; błędy: `400`, `401`,
`403 invalid-current-password`, `422`, `429 rate-limited`. W jednej
transakcji sprawdza obecne hasło, zapisuje hash nowego i odwołuje wszystkie
sesje, również bieżącą. Błąd obecnego hasła nie ujawnia hasha.

### `PUT /users/{user_id}/role` — zmiana roli

Bearer `admin`; istniejące konto. Administrator nie zmienia własnej roli.
Żądanie:

```json
{"role":"manager"}
```

`200 OK`:

```json
{
  "id":"15","email":"anna@example.com","full_name":"Anna Nowak",
  "role":"manager","is_active":true,
  "activated_at":"2027-01-05T12:00:00Z",
  "created_at":"2027-01-04T09:00:00Z",
  "updated_at":"2027-01-06T11:00:00Z"
}
```

Błędy: `400`, `401`, `403`, `404`, `409 last-admin-conflict`, `422`.
Serwis sprawdza zakaz odebrania roli ostatniemu aktywnemu, aktywowanemu
administratorowi i zapisuje zmianę w jednej transakcji. Współbieżne zmiany
roli i dezaktywacje administratorów muszą być serializowane wspólną blokadą
transakcyjną w PostgreSQL (np. blokadą doradczą dla operacji zmniejszających
liczbę aktywnych administratorów); sam odczyt liczby kont nie wystarcza.
Nowa rola działa od następnego chronionego żądania dzięki odczytowi z bazy.

### `POST /users/{user_id}/deactivation` — dezaktywacja

Bearer `admin`; tylko konto `is_active=true`, `activated_at != null`.
Administrator nie dezaktywuje siebie ani ostatniego aktywnego administratora.
Brak JSON w żądaniu. `200 OK`:

```json
{
  "id":"15","email":"anna@example.com","full_name":"Anna Nowak",
  "role":"employee","is_active":false,
  "activated_at":"2027-01-05T12:00:00Z",
  "created_at":"2027-01-04T09:00:00Z",
  "updated_at":"2027-01-06T11:00:00Z"
}
```

Błędy: `401`, `403`, `404`, `409 account-state-conflict` lub
`last-admin-conflict`, `422`. Jedna transakcja blokuje konto, zachowuje
`activated_at` i historię, ustawia `is_active=false`, odwołuje wszystkie
sesje i wszystkie niezużyte tokeny `password_reset` tego konta. Dla admina
stosuje tę samą kontrolę współbieżności co zmiana roli. Nie usuwa istniejących
przydziałów; reguły przyszłych przydziałów należą do kontraktu grafiku.

### `POST /users/{user_id}/reactivation` — rozpoczęcie reaktywacji

Bearer `admin`; tylko konto wcześniej aktywowane (`activated_at != null`)
i obecnie nieaktywne (`is_active=false`). Brak JSON w żądaniu. `200 OK`:

```json
{
  "id":"15","email":"anna@example.com","full_name":"Anna Nowak",
  "role":"employee","is_active":false,
  "activated_at":"2027-01-05T12:00:00Z",
  "created_at":"2027-01-04T09:00:00Z",
  "updated_at":"2027-01-06T11:00:00Z",
  "reactivation_token":"<wydany tylko raz>",
  "expires_at":"2027-01-07T11:00:00Z"
}
```

Błędy: `401`, `403`, `404`, `409 account-state-conflict`, `422`.
W jednej transakcji blokuje konto, zastępuje stary `password_hash` hashem
losowego nieprzekazywanego sekretu, odwołuje wszystkie sesje oraz poprzedni
niezużyty token `password_reset`, po czym wydaje nowy token o tym samym
`purpose=password_reset`. **Pozostawia `is_active=false` i nie zmienia
`activated_at`.** Ponowne wydanie przez ten sam endpoint unieważnia
poprzedni token. Token obowiązuje 24 godziny.

### `POST /auth/reactivations` — zakończenie reaktywacji

Publiczny; posiadacz jednorazowego tokenu wydanego przez poprzedni endpoint.
Żądanie:

```json
{"token":"<token reaktywacji>","new_password":"<nowe hasło>"}
```

`204 No Content`, bez JSON; błędy: `400`, `422`,
`401 invalid-action-token`, `429 rate-limited`. W jednej transakcji blokuje
konto i token, sprawdza hash, termin, brak wcześniejszego użycia,
`purpose=password_reset`, `activated_at != null` oraz `is_active=false`.
Zapisuje hash nowego hasła, `is_active=true` i `used_at=now`; **nie zmienia
`activated_at`** i nie tworzy sesji. Wydanie albo użycie tokenu nie może
przywrócić wcześniej odwołanej sesji.

#### Spójność reaktywacji z modelem danych

Referencyjne `users` ma `is_active` i `password_hash`; zatwierdzone
rozszerzenie w `AUTH_DESIGN.md` dodaje `activated_at` i
`account_action_tokens` z `purpose` ograniczonym do `activation` albo
`password_reset`. Stan `is_active=false`, `activated_at != null` dokładnie
oznacza konto uprzednio aktywowane, obecnie nieaktywne. Reaktywacja używa
istniejącego `purpose=password_reset`; **nie wymaga nowej wartości `purpose`
ani nowej kolumny**. Osobny endpoint i warunek stanu konta odróżniają ją od
pierwszej aktywacji i przyszłego resetu aktywnego konta. Każda dezaktywacja
odwołuje starsze tokeny resetu; każdy nowy token reaktywacji odwołuje
poprzedni. Dzięki temu wcześniejszy token resetu nie może przypadkiem
reaktywować konta. Ograniczenie `CHECK (NOT is_active OR activated_at IS NOT
NULL)` pozostaje spełnione. Rozszerzenie auth i tak wymaga migracji opisanej
w zatwierdzonym projekcie; ten przepływ nie wprowadza dodatkowej migracji.

## Funkcjonalności późniejsze — planowany administracyjny reset

Endpointy pozostają w kontrakcie v1, lecz nie są warunkiem ukończenia
podstawowego MVP ani drugiego etapu.

### `POST /users/{user_id}/password-reset-tokens` — wydanie resetu

Bearer `admin`; tylko konto aktywowane i aktywne. Brak JSON w żądaniu.
`200 OK`:

```json
{"password_reset_token":"<wydany tylko raz>","expires_at":"2027-01-07T11:00:00Z"}
```

Błędy: `401`, `403`, `404`, `409 account-state-conflict`, `422`. Jedna
transakcja blokuje konto, odwołuje poprzedni niezużyty token `password_reset`,
zastępuje hash hasła hashem losowego nieprzekazywanego sekretu, odwołuje
wszystkie sesje i zapisuje hash nowego tokenu. Konto pozostaje `is_active=true`,
ale stare hasło nie działa; zgubiony token można zastąpić nowym.

### `POST /auth/password-resets` — użycie resetu

Publiczny; posiadacz tokenu wydanego dla aktywnego konta. Żądanie:

```json
{"token":"<token resetu>","new_password":"<nowe hasło>"}
```

`204 No Content`, bez JSON; błędy: `400`, `422`,
`401 invalid-action-token`, `429 rate-limited`. W jednej transakcji
blokuje konto i token, wymaga `purpose=password_reset`, `is_active=true`,
`activated_at != null`, ważności i jednorazowości tokenu; zapisuje nowe
hasło, `used_at` i odwołuje wszystkie sesje. Nie zmienia `activated_at`,
nie tworzy sesji. Token reaktywacji nie przejdzie warunku aktywnego konta.

## Macierz uprawnień

| Operacja | Admin | Manager | Employee |
| --- | --- | --- | --- |
| Logowanie, odnowienie, aktywacja, użycie tokenu reaktywacji/resetu | Według stanu konta lub tokenu | Tak samo | Tak samo |
| Własne dane, własne hasło, wylogowanie bieżące i globalne | Tak | Tak | Tak |
| Lista i szczegóły dowolnych kont | Tak | Nie | Nie |
| Tworzenie konta, wydanie tokenów | Tak | Nie | Nie |
| Zmiana roli, dezaktywacja, rozpoczęcie reaktywacji | Tak, z ograniczeniami własnego konta i ostatniego admina | Nie | Nie |

Manager nie otrzymuje ogólnej listy użytkowników. Widok kandydatów do
przydziału powstanie osobno, z zakresem danych potrzebnym dla grafiku.

## Wpływ na frontend React

- Access token żyje tylko w pamięci. Klient wysyła Bearer do chronionych
  operacji, a `credentials: "include"` do logowania, odnowienia i wylogowania.
  Po odświeżeniu strony może odzyskać access token przez refresh. Odnowienie
  musi być pojedyncze także między kartami, np. przez Web Locks i
  BroadcastChannel; utrata odpowiedzi po rotacji może wymagać logowania.
- Po `204` z aktywacji, reaktywacji lub resetu pokazuje formularz logowania.
  Po zmianie hasła i wylogowaniu czyści access token we wszystkich kartach.
  `401` refresh oznacza przejście do logowania.
- Panel administratora pokazuje wydany token tylko bezpośrednio po operacji
  tworzenia, ponownego wydania lub reaktywacji. Nie próbuje go później
  pobierać. Link aktywacji/reaktywacji może mieć token tylko we fragmencie
  URL; strona usuwa fragment z historii przed dalszą nawigacją, nie ładuje
  zbędnych zasobów zewnętrznych i używa `Referrer-Policy: no-referrer`.
- Typy TypeScript traktują `id` jako string, `activated_at` jako
  `string | null`, a `is_active` jako boolean. Formularze nie wnioskują o
  stanie aktywacji wyłącznie z `is_active`: `false/null` oznacza oczekiwanie
  na pierwszą aktywację, `false/data` oznacza konto dezaktywowane lub
  oczekujące na ukończenie reaktywacji.
- Jeden moduł błędów odczytuje RFC 9457. Formularze wiążą
  `errors[].location` z polami; logika klienta używa statusu i `type`, a
  nie `title` lub `detail`. Lista użytkowników używa `limit`, `offset`,
  `total` i wyłącznie udokumentowanych filtrów i sortowania.

## Granice kontraktu

Kontrakt nie definiuje zasad wyboru kandydatów do przydziału ani skutków
dezaktywacji dla już istniejących, przyszłych przydziałów. Zachowuje historię
i uniemożliwia nowe chronione żądania dezaktywowanego konta. Szczegóły
stanowisk i grafiku będą opisane w ich własnych kontraktach.

Przed publicznym uruchomieniem trzeba ustalić adresy hostingu i atrybuty
cookie/CORS, progi oraz współdzielone egzekwowanie limitów prób, parametry
Argon2id po pomiarach i bezpieczny kanał ręcznego przekazania tokenu. Nie
wpływa to na kształt opisanych endpointów.
