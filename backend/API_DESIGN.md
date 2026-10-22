# DreamPlanner — wspólne standardy REST API v1

Status: zatwierdzone decyzje dotyczące wspólnego kontraktu HTTP. Dokument nie
definiuje jeszcze kompletu endpointów domenowych ani nie zmienia schematu bazy.
Należy czytać go razem z [ARCHITECTURE.md](ARCHITECTURE.md),
[AUTH_DESIGN.md](AUTH_DESIGN.md), `database/data-model.md` i
`database/schema.sql`. W razie projektowania konkretnej operacji obowiązują
również reguły domenowe i bezpieczeństwa zapisane w tych dokumentach.

## Konwencje REST i JSON

- Wszystkie endpointy aplikacyjne v1 mają prefiks `/api/v1`.
- Ścieżki zasobów używają rzeczowników w liczbie mnogiej, małych liter i
  `kebab-case`, np. `/api/v1/users/{user_id}`. Nazwy operacji wykraczających
  poza zwykły odczyt lub zapis będą ustalone przy projektowaniu danej funkcji.
- Metoda HTTP określa działanie na zasobie. Operacje odczytu nie zmieniają stanu.
  Kanoniczne ścieżki nie mają końcowego ukośnika.
- Żądania i odpowiedzi z treścią używają JSON (`application/json`), z wyjątkiem
  błędów opisanych jako Problem Details (`application/problem+json`).
- Nazwy pól JSON oraz parametrów query i path są w `snake_case`, np.
  `full_name`, `work_date`, `required_workers`. Nazwy segmentów ścieżki
  pozostają w `kebab-case`. Wartości statusów zachowują kody określone w modelu
  danych, np. `collection_closed` i `unavailable`.
- Publiczne schematy wejścia i wyjścia są jawne. Modele ORM, hashe, sekrety i
  pola techniczne bez uzasadnienia nie są automatycznie częścią odpowiedzi API.

## Identyfikatory

Referencyjny schemat PostgreSQL używa `BIGINT` jako klucza głównego dla
`users`, `positions`, `schedules`, `shift_requirements`,
`availability_submissions`, `availability_entries` i `shift_assignments`.
`roles.id` jest typu `SMALLINT`. Rozszerzenie auth opisane w `AUTH_DESIGN.md`
używa `UUID` dla rekordów sesji i tokenów; nie zmienia to identyfikatora
użytkownika.

- Publiczne identyfikatory zasobów opartych na `BIGINT` są dziesiętnymi
  stringami w JSON i w parametrach ścieżki, np. `"id": "15"` oraz
  `/api/v1/users/15`. API sprawdza poprawność i zakres wartości przed użyciem
  jej jako `BIGINT`.
- Frontend traktuje identyfikatory jako nieprzezroczyste stringi. Ta
  reprezentacja zapobiega utracie precyzji liczb `BIGINT` w JavaScript.
- Rolę w kontrakcie użytkownika identyfikuje kod `admin`, `manager` lub
  `employee`, jeśli nie ma wyraźnej potrzeby wystawienia `roles.id`.
- UUID z modułu auth występuje w API tylko wtedy, gdy wymaga tego zatwierdzony
  kontrakt konkretnej operacji. Nie zastępuje identyfikatorów domenowych.

## Daty, godziny i przedziały

| Znaczenie | Reprezentacja API | Przykład |
| --- | --- | --- |
| Data pracy | `YYYY-MM-DD` | `"2027-01-05"` |
| Lokalna godzina pracy lub dyspozycyjności | `HH:MM:SS`, bez offsetu i części ułamkowej | `"08:00:00"` |
| Chwila zdarzenia, np. `created_at` | RFC 3339, UTC | `"2027-01-05T07:12:30Z"` |
| Miesiąc grafiku | Osobne pola liczbowe `year` i `month` | `{"year": 2027, "month": 1}` |

- Organizacyjna strefa IANA jest konfigurowalna; wartością dla MVP jest
  `Europe/Warsaw`. `work_date`, `start_time` i `end_time` oznaczają datę i czas
  lokalny w tej strefie. Nie są chwilami UTC. Znaczniki zdarzeń są zwracane
  w UTC; strefa procesu lub przeglądarki nie zmienia znaczenia godzin pracy.
- Przedziały godzinowe są półotwarte: `[start_time, end_time)`. Koniec jednego
  przedziału może być początkiem następnego bez nakładania. Obowiązuje
  `start_time < end_time`; przedział nie przechodzi przez północ. Nie używamy
  `24:00:00` jako godziny zakończenia.
- Daty zapotrzebowania i dyspozycyjności muszą należeć do miesiąca grafiku.
  API waliduje tę regułę; ograniczenia PostgreSQL pozostają dodatkową ochroną.
- Odrzucane są **wyłącznie przedziały faktycznie obejmujące nieistniejące lub
  niejednoznaczne godziny lokalne** podczas przejścia na czas letni lub
  zimowy. Dotyczy to także niejednoznacznego punktu granicznego, gdy bez
  offsetu nie da się ustalić chwili początku lub końca. Inne przedziały
  w tym samym dniu są dozwolone. Obecny model `DATE` + `TIME` nie przechowuje
  wyboru pierwszego lub drugiego wystąpienia powtórzonej godziny.

Przykład godzin zmiany:

```json
{
  "work_date": "2027-01-05",
  "start_time": "08:00:00",
  "end_time": "16:00:00"
}
```

## Odpowiedzi sukcesu

- Pojedynczy zasób lub wynik operacji jest zwracany bez koperty `data`.
- `200 OK` oznacza udany odczyt lub operację zwracającą wynik.
- `201 Created` oznacza utworzenie zasobu; odpowiedź zawiera jego
  reprezentację i nagłówek `Location` z kanonicznym adresem.
- `204 No Content` oznacza sukces bez treści odpowiedzi.
- Listy mają wspólny format opisany w sekcji o paginacji.

## Błędy: RFC 9457 Problem Details

Błędy z treścią używają `Content-Type: application/problem+json` i modelu
[RFC 9457](https://www.rfc-editor.org/rfc/rfc9457.html). Podstawowe pola to:
`type` (identyfikator rodzaju problemu), `title` (krótki opis rodzaju),
`status` (ten sam kod co odpowiedź HTTP), opcjonalnie `detail` (bezpieczny
opis przypadku) i `instance` (identyfikator wystąpienia, bez sekretów).
Frontend nie rozpoznaje błędów przez analizę tekstu `title` lub `detail`.

- Dla ogólnych błędów HTTP można użyć `type: "about:blank"`.
- Dla problemów domenowych wymagających odrębnej obsługi przez klienta
  używamy stabilnego, bezwzględnego URI typu, np.
  `urn:dreamplanner:problem:schedule-state-conflict`. Typy te muszą być
  opisane w OpenAPI. Gdy będzie dostępna trwała domena dokumentacji, można
  rozważyć rozwiązywalne URI dla nowych typów bez zmiany tożsamości już
  opublikowanych problemów.
- `detail` nie zawiera SQL, stosu wywołań, danych połączenia ani informacji
  pozwalających ustalić istnienie konta podczas logowania, aktywacji lub resetu.
- Znane błędy ograniczeń bazy są tłumaczone po `rollback()` na bezpieczne
  problemy aplikacyjne. Nieoczekiwane błędy nie ujawniają szczegółów
  implementacyjnych.

```json
{
  "type": "urn:dreamplanner:problem:schedule-state-conflict",
  "title": "Grafik nie pozwala na tę operację",
  "status": 409,
  "detail": "Operacja jest niedostępna w bieżącym stanie grafiku."
}
```

| Kod | Znaczenie w API v1 |
| --- | --- |
| `400` | Niepoprawna składnia żądania, np. błędny JSON. |
| `401` | Brak poprawnego uwierzytelnienia lub nieważna sesja; dla chronionego zasobu stosuje się właściwe wyzwanie `WWW-Authenticate: Bearer`. |
| `403` | Uwierzytelniony użytkownik nie ma wymaganych uprawnień. |
| `404` | Zasób nie istnieje lub nie należy ujawniać jego istnienia temu użytkownikowi. |
| `409` | Konflikt z aktualnym stanem danych, np. duplikat, kolizja lub niedozwolona operacja w bieżącym stanie grafiku. |
| `422` | Poprawne składniowo żądanie ma nieprawidłowe wartości wejściowe. |

Nie ujawniamy poprzez różne komunikaty, czy konto podane przy logowaniu,
aktywacji lub resecie istnieje. Szczegółowe mapowanie błędów takich operacji
musi zachować `AUTH_DESIGN.md`.

### Walidacja wejścia

Błędy walidacji Pydantic są przedstawiane jako Problem Details `422` z
bezpiecznym rozszerzeniem `errors`. Każdy element wskazuje publiczną
lokalizację, stabilny kod błędu i komunikat. Nie kopiujemy surowych pól
walidatora takich jak `input`, `ctx`, fragmentów sekretów ani wyjątków.
Walidacja składni JSON daje `400`; walidacja wartości pól daje `422`;
konflikt z bieżącym stanem danych daje `409`.

```json
{
  "type": "urn:dreamplanner:problem:validation-error",
  "title": "Nieprawidłowe dane żądania",
  "status": 422,
  "errors": [
    {
      "location": ["body", "required_workers"],
      "code": "greater_than",
      "message": "Wartość musi być większa od zera."
    }
  ]
}
```

## Listy, filtrowanie i sortowanie

- Listy mogące rosnąć używają `limit` i `offset`. Domyślne `limit` wynosi
  `25`, maksymalne `100`, a domyślne `offset` wynosi `0`. Parametry muszą być
  liczbami całkowitymi; `limit` musi być dodatni, `offset` nieujemny.
- Odpowiedź ma pola `items`, `limit`, `offset`, `total`. `total` liczy wyłącznie
  zasoby widoczne dla danego użytkownika po zastosowaniu filtrów.
- Każdy endpoint listy jawnie określa dozwolone filtry i pola sortowania.
  Nie przekazujemy dowolnych nazw kolumn ani fragmentów SQL z query.
- Parametr `sort` używa nazwy pola dla porządku rosnącego i prefiksu `-` dla
  malejącego, np. `sort=-work_date`. Serwer stosuje dodatkowy, stabilny klucz
  `id` do rozstrzygania remisów. Nieobsługiwane filtry i pola sortowania
  są błędami wartości wejściowych (`422`).
- Mały, zamknięty słownik może mieć niepaginowaną listę, jeżeli zostanie to
  jawnie zapisane przy projektowaniu jego endpointu.

```json
{
  "items": [
    {"id": "15", "year": 2027, "month": 1, "status": "draft"}
  ],
  "limit": 25,
  "offset": 0,
  "total": 1
}
```

## OpenAPI i kompatybilność

- OpenAPI generowane przez FastAPI jest kontraktem integracyjnym dla React.
  Każda operacja powinna mieć opis celu, schematy wejścia i wyjścia, wymagane
  uprawnienia, możliwe statusy i odpowiedzi Problem Details.
- Dokumentacja opisuje format identyfikatorów, dat i godzin, strefę
  organizacji, dostępne filtry i sortowanie, zasady widoczności danych oraz
  istotne reguły walidacji. Przykłady nie zawierają prawdziwych sekretów.
- Zmiany zgodne z v1 mogą dodawać endpointy i opcjonalne pola bez zmiany
  istniejącej semantyki. Klient ignoruje nieznane pola odpowiedzi oraz
  nieznane rozszerzenia Problem Details.
- Zmiana typu lub znaczenia istniejącego pola, wymagalności, widoczności
  danych albo semantyki istniejącej operacji jest zmianą niekompatybilną i
  wymaga nowej wersji API, np. `/api/v2`, oraz planu migracji klientów.
  Nie stosujemy równoległego wersjonowania w nagłówkach.

## Bezpieczeństwo HTTP

- Produkcyjne połączenia z API używają HTTPS. Lokalny HTTP pozostaje
  dopuszczony zgodnie z `AUTH_DESIGN.md`.
- Chronione endpointy przyjmują access token wyłącznie jako
  `Authorization: Bearer`. Refresh token pozostaje w cookie `HttpOnly`, w
  produkcji również `Secure`. Szczegóły sesji, rotacji, ochrony `Origin` i
  odwołania określa `AUTH_DESIGN.md`; ten dokument ich nie zmienia.
- Odpowiedzi z tokenami i danymi chronionymi, w tym odpowiednie błędy,
  mają `Cache-Control: no-store`.
- CORS używa jawnej listy dozwolonych originów, ustalonej po poznaniu
  rzeczywistych adresów hostingu. Przy żądaniach z credentials nie używa
  wildcard `*`. Operacje wykorzystujące refresh cookie walidują `Origin`
  według `AUTH_DESIGN.md`; CORS nie zastępuje tej kontroli.
- Hasła, tokeny, cookie, nagłówek `Authorization`, hashe i connection stringi
  nie trafiają do URL, logów, metryk, przykładów OpenAPI ani odpowiedzi
  błędów. Dane innych użytkowników są zwracane tylko po sprawdzeniu roli i
  prawa do konkretnego zasobu. Pracownik nie widzi nieopublikowanych
  przydziałów.

## Wpływ na frontend React

- Typy danych API używają `snake_case`, a identyfikatory `BIGINT` są
  stringami. Frontend nie wykonuje na nich arytmetyki.
- Formularze mogą wyświetlać `HH:MM`, lecz API wymaga `HH:MM:SS`. Widoki
  grafiku pokazują godziny w strefie organizacji, a chwile zdarzeń mogą
  formatować do wybranej strefy przy prezentacji.
- Jeden mechanizm obsługi błędów odczytuje Problem Details. Formularze
  przypisują `errors[].location` do pól, a logika klienta używa `type` i
  statusu HTTP, nie tekstu komunikatu.
- Listy używają `limit`, `offset`, `total` oraz filtrów i sortowania
  udokumentowanych dla konkretnego endpointu. Po konflikcie `409` frontend
  może odświeżyć stan zasobu przed ponowną próbą.
- Wymagania przechowywania access tokenu, refresh cookie i koordynacji
  odnowienia między kartami pozostają określone w `AUTH_DESIGN.md`.

## Do doprecyzowania przy projektowaniu endpointów

- Dokładne ścieżki i schematy operacji użytkowników, stanowisk, grafików,
  dyspozycyjności i przydziałów oraz zasady uprawnień do każdego zasobu.
- Dozwolone przejścia stanów grafiku i operacje zapisu w poszczególnych
  stanach; zasady edycji wysłanej deklaracji.
- Szczegółowe typy problemów domenowych, mapowanie konfliktów bazy i
  odpowiedzi operacji auth, przy zachowaniu ochrony przed ujawnianiem kont.
- Dopuszczalne filtry, pola sortowania i domyślna kolejność dla każdej listy;
  ewentualne wyjątki dla małych słowników.
- Znaczenie dezaktywacji konta i stanowiska dla istniejących danych oraz
  warunki kwalifikowania osoby do przydziału.
- Dokładne originy hostingu, konfiguracja CORS i atrybuty cookie, zgodnie z
  wariantami opisanymi w `AUTH_DESIGN.md`.

Projektowanie tych punktów nie zmienia referencyjnego `database/schema.sql`
bez osobnej decyzji. Fixture demonstracyjny nie jest kontraktem API.
