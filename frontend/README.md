# DreamPlanner – Frontend

Warstwa prezentacji (Presentation Layer) aplikacji **DreamPlanner**, służącej do ustalania i zarządzania grafikiem zleceniobiorców oraz pracowników.

Frontend komunikuje się wyłącznie z REST API backendu. Nie łączy się bezpośrednio z bazą danych i nie przechowuje żadnych haseł ani connection stringów.

## Stos technologiczny

- React
- Vite
- TypeScript
- ESLint

## Wymagania

- Node.js 20.19+ lub 22.12+ (wymóg Vite)
- npm

## Uruchomienie lokalne

```bash
cd frontend
cp .env.example .env.local
npm install
npm run dev
```

Aplikacja uruchomi się pod adresem wyświetlonym w terminalu (domyślnie `http://localhost:5173/`).

## Dostępne polecenia

| Polecenie | Opis |
|---|---|
| `npm run dev` | uruchamia serwer developerski z hot reload |
| `npm run build` | buduje wersję produkcyjną do katalogu `dist/` |
| `npm run preview` | lokalny podgląd zbudowanej aplikacji |
| `npm run lint` | sprawdza kod przy użyciu ESLint |

## Zmienne środowiskowe

Adres API nie jest zapisany w kodzie. Konfiguruje się go w pliku `.env.local`, wzorowanym na `.env.example`:

```
VITE_API_BASE_URL=http://localhost:3000/api
```

- `.env.example` jest w repozytorium i służy jako wzór dla zespołu.
- `.env.local` jest wykluczony w `.gitignore` i nie może trafić na GitHuba.
- Zmienne Vite są wczytywane przy starcie serwera i wbudowywane w aplikację podczas `npm run build`. Po zmianie wartości trzeba zrestartować `npm run dev` lub zbudować aplikację ponownie.

W kodzie adres jest dostępny jako `import.meta.env.VITE_API_BASE_URL`.

## Struktura katalogu

```
frontend/
├── src/
│   ├── App.tsx                          # główny komponent aplikacji
│   ├── DataList.tsx                     # widok grafiku zmian (tabela)
│   └── database/fixtures/
│       └── dreamplanner.json            # dane makietowe (kopia z /database/fixtures)
├── public/
├── .env.example                         # wzór konfiguracji
├── Dockerfile                           # build + serwowanie przez Nginx
├── package.json
└── vite.config.ts
```

## Dane makietowe (mock data)

Do czasu uruchomienia API widok korzysta z pliku `src/database/fixtures/dreamplanner.json`, przygotowanego przez zespół i odzwierciedlającego model danych (stanowiska, role, użytkownicy, grafiki, zmiany, dostępności, przypisania).

Aktualny widok (`DataList.tsx`) pokazuje grafik zmian na dany miesiąc: datę, godziny, stanowisko, obsadę (przypisani / wymagani), nazwiska pracowników i uwagi. Dane łączone są po kluczach obcych (`position_id`, `shift_requirement_id`, `user_id`).

Plik w `src/database/fixtures/` jest kopią pliku z katalogu `/database/fixtures` w głównym repozytorium. Przy zmianie modelu danych kopię należy zaktualizować.

## Integracja z backendem (plan)

1. Otrzymać od zespołu backendu adres API i ustawić go w `VITE_API_BASE_URL`.
2. Zastąpić import pliku JSON wywołaniami `fetch()` do API.
3. Dodać ekran logowania i obsługę uwierzytelniania zgodnie z ustaleniami z backendem.
4. Przetestować logowanie i operacje na danych z poziomu interfejsu na wdrożonej wersji.

## Wdrożenie

Aplikacja jest przygotowana do wdrożenia w warstwie Web (Public Subnet) jako pliki statyczne serwowane przez Nginx.

```bash
docker build -t dreamplanner-frontend .
docker run -p 8080:80 dreamplanner-frontend
```

Po uruchomieniu aplikacja jest dostępna pod `http://localhost:8080/`.

Alternatywnie zbudowany katalog `dist/` można wdrożyć na statyczny hosting, np. Azure Static Web Apps.

## Architektura

```
Użytkownik → Frontend (React/Vite) → REST API (Backend) → PostgreSQL
```

Frontend należy do warstwy prezentacji. Dostęp do danych odbywa się wyłącznie przez REST API.
