"""Polish presentation metadata for the generated OpenAPI document."""

import re
from copy import deepcopy

TAG_DESCRIPTIONS = {
    "auth": "Logowanie, sesje oraz aktywacja kont.",
    "health": "Kontrola działania procesu API.",
    "positions": "Stanowiska wykorzystywane przy planowaniu zmian.",
    "schedules": "Grafiki miesięczne i zapotrzebowanie na pracowników.",
    "availability-submissions": "Deklaracje dyspozycyjności pracowników.",
    "shift-assignments": "Przydziały pracowników do zmian.",
    "my-shifts": "Własne opublikowane zmiany pracownika.",
    "my-schedules": "Grafiki dostępne pracownikowi do zgłaszania dyspozycyjności.",
    "users": "Konta użytkowników i operacje administracyjne.",
    "employees": "Lista pracowników dostępnych do przydziału.",
}

SCHEMA_DESCRIPTIONS = {
    "AccessTokenResponse": "Token dostępu po zalogowaniu lub odnowieniu sesji.",
    "ActivateAccountInput": "Token aktywacyjny i nowe hasło użytkownika.",
    "ActivationTokenResponse": "Nowy, jednorazowy token pierwszej aktywacji konta.",
    "AvailabilityEntryInput": "Przedział dyspozycyjności podany przez pracownika.",
    "AvailabilityEntryResponse": "Publiczny zapis przedziału dyspozycyjności.",
    "AvailabilitySubmissionListResponse": "Strona deklaracji i łączna liczba wyników.",
    "AvailabilitySubmissionResponse": "Miesięczna deklaracja z przedziałami.",
    "AvailabilitySubmissionSummary": "Dane deklaracji bez szczegółów przedziałów.",
    "ChangePasswordInput": "Obecne i nowe hasło właściciela konta.",
    "ChangeRoleInput": "Nowa rola konta wskazana przez administratora.",
    "CreateAvailabilitySubmissionInput": "Dane nowej deklaracji dyspozycyjności.",
    "CreatePositionInput": "Nazwa nowego stanowiska.",
    "CreateScheduleInput": "Rok i miesiąc nowego grafiku.",
    "CreateShiftAssignmentInput": "Dane przydziału pracownika do zmiany.",
    "CreateShiftRequirementInput": "Dane nowego zapotrzebowania na pracowników.",
    "CreateUserInput": "Dane konta tworzonego przez administratora.",
    "CreatedUserResponse": "Dane konta i jednorazowy token aktywacyjny.",
    "EmployeeListItem": "Pracownik dostępny do przydziału.",
    "EmployeeListResponse": "Strona pracowników dostępnych do przydziału.",
    "EmployeeScheduleItem": "Grafik dostępny do zgłoszenia dyspozycyjności.",
    "EmployeeScheduleListResponse": "Strona grafików dostępnych do dyspozycyjności.",
    "HealthResponse": "Stan działania procesu API.",
    "LoginInput": "Dane logowania do sesji przeglądarkowej.",
    "MyShiftListResponse": "Strona własnych opublikowanych zmian.",
    "MyShiftResponse": "Własna opublikowana zmiana pracownika.",
    "PositionListResponse": "Strona stanowisk i łączna liczba wyników.",
    "PositionResponse": "Publiczne dane stanowiska.",
    "ReactivateAccountInput": "Token reaktywacyjny i nowe hasło.",
    "ReactivationResponse": "Dane konta i jednorazowy token reaktywacyjny.",
    "RenamePositionInput": "Nowa nazwa istniejącego stanowiska.",
    "ScheduleListResponse": "Strona grafików i łączna liczba wyników.",
    "ScheduleResponse": "Publiczne dane grafiku miesięcznego.",
    "ShiftAssignmentListResponse": "Strona przydziałów i łączna liczba wyników.",
    "ShiftAssignmentResponse": "Przydział i godziny zmiany bez danych pracownika.",
    "ShiftRequirementListResponse": "Strona zapotrzebowań i łączna liczba wyników.",
    "ShiftRequirementResponse": "Publiczne dane zapotrzebowania na pracowników.",
    "UpdateAvailabilitySubmissionInput": "Dane edycji lub przesłania deklaracji.",
    "UpdateShiftRequirementInput": "Pola edytowanego zapotrzebowania.",
    "UserListResponse": "Strona kont i łączna liczba wyników.",
    "UserProfile": "Publiczne dane konta dostępne po uwierzytelnieniu.",
}

FIELD_DESCRIPTIONS = {
    "access_token": "Token dostępu JWT.",
    "activated_at": "Czas aktywacji konta w UTC.",
    "activation_expires_at": "Czas wygaśnięcia tokenu aktywacyjnego w UTC.",
    "activation_token": "Jednorazowy token pierwszej aktywacji.",
    "assigned_at": "Czas utworzenia przydziału w UTC.",
    "assigned_by": "Identyfikator użytkownika, który utworzył przydział.",
    "assignment_id": "Identyfikator przydziału jako dziesiętny string BIGINT.",
    "collection_closed_at": "Czas zamknięcia zbierania dyspozycyjności w UTC.",
    "collection_opened_at": "Czas otwarcia zbierania dyspozycyjności w UTC.",
    "created_at": "Czas utworzenia w UTC.",
    "created_by": "Identyfikator twórcy grafiku jako dziesiętny string BIGINT.",
    "current_password": "Obecne hasło właściciela konta.",
    "email": "Adres e-mail konta.",
    "end_time": "Lokalna godzina końca w strefie Europe/Warsaw.",
    "entries": "Przedziały dyspozycyjności w deklaracji.",
    "expires_at": "Czas wygaśnięcia w UTC.",
    "expires_in": "Czas ważności tokenu dostępu w sekundach.",
    "full_name": "Imię i nazwisko użytkownika.",
    "id": "Identyfikator zasobu jako dziesiętny string BIGINT.",
    "is_active": "Informacja, czy zasób jest aktywny.",
    "items": "Elementy bieżącej strony wyników.",
    "limit": "Maksymalna liczba elementów na stronie.",
    "month": "Numer miesiąca grafiku.",
    "name": "Nazwa stanowiska.",
    "new_password": "Nowe hasło konta.",
    "note": "Opcjonalna notatka.",
    "offset": "Liczba pominiętych elementów.",
    "password": "Hasło użytkownika.",
    "position_id": "Identyfikator stanowiska jako dziesiętny string BIGINT.",
    "position_is_active": "Informacja, czy stanowisko jest aktywne.",
    "position_name": "Nazwa stanowiska przypisanego do zmiany.",
    "published_at": "Czas publikacji grafiku w UTC.",
    "reactivation_token": "Jednorazowy token reaktywacyjny.",
    "required_workers": "Wymagana liczba pracowników.",
    "role": "Rola konta.",
    "schedule_id": "Identyfikator grafiku jako dziesiętny string BIGINT.",
    "shift_requirement_id": "Identyfikator zapotrzebowania jako string BIGINT.",
    "start_time": "Lokalna godzina początku w strefie Europe/Warsaw.",
    "status": "Stan zasobu.",
    "submit": "Czy przesłać deklarację zamiast zapisać szkic.",
    "submitted_at": "Czas przesłania deklaracji w UTC.",
    "token": "Jednorazowy token akcji na koncie.",
    "token_type": "Typ tokenu dostępu.",
    "total": "Łączna liczba wyników przed paginacją.",
    "updated_at": "Czas ostatniej aktualizacji w UTC.",
    "user_id": "Identyfikator użytkownika jako dziesiętny string BIGINT.",
    "warning_acknowledged": "Czy potwierdzono ostrzeżenie o dyspozycyjności.",
    "warning_acknowledged_at": "Czas potwierdzenia ostrzeżenia w UTC.",
    "warning_type": "Rodzaj historycznego ostrzeżenia.",
    "work_date": "Lokalna data zmiany w formacie YYYY-MM-DD.",
    "year": "Rok grafiku.",
}

PARAMETER_DESCRIPTIONS = {
    "activated": "Filtr kont według stanu pierwszej aktywacji.",
    "assignment_id": "Identyfikator przydziału: cyfry ASCII w zakresie BIGINT.",
    "from_date": "Początek przedziału dat, włącznie, YYYY-MM-DD.",
    "is_active": "Filtr według aktywności zasobu.",
    "limit": "Liczba wyników na stronie, od 1 do 100.",
    "month": "Numer miesiąca grafiku, od 1 do 12.",
    "offset": "Liczba pominiętych wyników, od 0 do maksimum BIGINT.",
    "position_id": "Identyfikator stanowiska w zakresie BIGINT.",
    "q": "Fragment nazwy wyszukiwany bez rozróżniania wielkości liter.",
    "requirement_id": "Identyfikator zapotrzebowania: cyfry ASCII w zakresie BIGINT.",
    "role": "Filtr według roli konta.",
    "schedule_id": "Identyfikator grafiku: cyfry ASCII w zakresie BIGINT.",
    "shift_requirement_id": "Identyfikator zapotrzebowania w zakresie BIGINT.",
    "sort": "Kolejność sortowania wyników.",
    "status": "Filtr według stanu grafiku.",
    "state": "Stan deklaracji: draft lub submitted.",
    "submission_id": "Identyfikator deklaracji: cyfry ASCII w zakresie BIGINT.",
    "to_date": "Koniec przedziału dat, włącznie, YYYY-MM-DD.",
    "user_id": "Identyfikator użytkownika w zakresie BIGINT.",
    "work_date": "Filtr daty zmiany w formacie YYYY-MM-DD.",
    "year": "Rok grafiku, od 2000 do 2100.",
}

RESPONSE_DESCRIPTIONS = {
    "200": "Operacja zakończona pomyślnie.",
    "201": "Zasób utworzony pomyślnie.",
    "204": "Operacja zakończona bez treści odpowiedzi.",
    "400": "Nieprawidłowa składnia JSON.",
    "401": "Brak prawidłowego uwierzytelnienia lub tokenu.",
    "403": "Brak uprawnienia lub niedozwolone żądanie.",
    "404": "Nie znaleziono wskazanego zasobu.",
    "409": "Konflikt z bieżącym stanem zasobu.",
    "422": "Nieprawidłowe dane żądania.",
    "429": "Przekroczono limit prób; spróbuj ponownie później.",
    "500": "Nieoczekiwany błąd serwera.",
    "503": "Uwierzytelnianie jest niedostępne z powodu braku konfiguracji podpisu JWT.",
}

FORBIDDEN_DESCRIPTIONS = {
    "Active employee required": "Wymagany jest aktywny pracownik.",
    "Active manager required": "Wymagany jest aktywny manager.",
    "Admin or manager required": "Wymagana jest rola admin lub manager.",
    "Administrator required": "Wymagana jest rola admin.",
    "Administrator required or self-change forbidden": (
        "Wymagana jest rola admin; zmiana własnej roli jest zabroniona."
    ),
    "Administrator required or self-deactivation forbidden": (
        "Wymagana jest rola admin; własna dezaktywacja jest zabroniona."
    ),
    "Employee role required": "Wymagana jest rola employee.",
    "Employee required": "Wymagana jest rola employee.",
    "HTTPS required": "Wymagane jest połączenie HTTPS.",
    "HTTPS required in production": "W środowisku produkcyjnym wymagany jest HTTPS.",
    "Manager or admin required": "Wymagana jest rola manager lub admin.",
    "Manager required": "Wymagana jest rola manager.",
    "Origin denied": "Nagłówek Origin jest niedozwolony.",
    "Role is forbidden": "Bieżąca rola nie ma dostępu.",
    "Role or filter is forbidden": "Bieżąca rola lub filtr jest niedozwolony.",
    "invalid-current-password or HTTPS required": (
        "Nieprawidłowe obecne hasło lub wymagany HTTPS. "
        "Możliwy typ: invalid-current-password."
    ),
}

NOT_FOUND_DESCRIPTIONS = {
    "Account not found": "Nie znaleziono konta.",
    "Assignment not found": "Nie znaleziono przydziału.",
    "Position not found": "Nie znaleziono stanowiska.",
    "Schedule not found": "Nie znaleziono grafiku.",
    "Schedule or assignment not found": "Nie znaleziono grafiku lub przydziału.",
    "Schedule or position not found": "Nie znaleziono grafiku lub stanowiska.",
    "Schedule or requirement not found": (
        "Nie znaleziono grafiku lub zapotrzebowania."
    ),
    "Schedule or visible submission not found": (
        "Nie znaleziono grafiku lub widocznej deklaracji."
    ),
    "Schedule, requirement or position not found": (
        "Nie znaleziono grafiku, zapotrzebowania lub stanowiska."
    ),
    "Schedule, requirement or user not found": (
        "Nie znaleziono grafiku, zapotrzebowania lub użytkownika."
    ),
}

NO_STORE = {
    "description": "Odpowiedź nie może być przechowywana w pamięci podręcznej.",
    "schema": {"type": "string", "const": "no-store"},
}
BEARER = {
    "description": "Żądanie uwierzytelnienia schematem Bearer.",
    "schema": {"type": "string", "const": "Bearer"},
}
LOCATION = {
    "description": "Kanoniczna ścieżka utworzonego zasobu.",
    "schema": {"type": "string"},
}
SET_COOKIE = {
    "description": (
        "Ustawia ciasteczko sesji odświeżania; wartość tokenu "
        "nie jest ujawniana w schemacie."
    ),
    "schema": {"type": "string"},
}


def _problem_response(description: str, schema: dict) -> dict:
    """Build one documented RFC 9457 response."""
    return {
        "description": description,
        "content": {"application/problem+json": {"schema": deepcopy(schema)}},
    }


def _response_description(code: str, original: str) -> str:
    """Translate known response prose and retain stable error identifiers."""
    if code == "403":
        return FORBIDDEN_DESCRIPTIONS.get(original, RESPONSE_DESCRIPTIONS[code])
    if code == "404":
        return NOT_FOUND_DESCRIPTIONS.get(original, RESPONSE_DESCRIPTIONS[code])
    if code == "409":
        identifiers = re.findall(
            r"(?:urn:dreamplanner:problem:)?[a-z]+(?:-[a-z]+)+", original
        )
        description = RESPONSE_DESCRIPTIONS[code]
        if identifiers:
            description += " Warianty: " + ", ".join(identifiers) + "."
        if "The latter covers" in original:
            description += (
                " Blokada publikacji obejmuje brak zapotrzebowań, nadmierną "
                "obsadę i pracowników niespełniających warunków."
            )
        return description
    if code == "401" and original in {
        "invalid-action-token",
        "invalid-credentials",
        "invalid-session",
    }:
        return RESPONSE_DESCRIPTIONS[code] + f" Typ: {original}."
    return RESPONSE_DESCRIPTIONS[code]


def _localize_problem_schema(schema: dict, *, issue: bool = False) -> None:
    """Translate inline problem metadata without changing its fields."""
    schema["description"] = (
        "Błąd walidacji pojedynczego pola."
        if issue
        else "Odpowiedź Problem Details zgodna z RFC 9457."
    )
    fields = {
        "type": "Stały identyfikator rodzaju błędu.",
        "title": "Krótki tytuł błędu.",
        "status": "Kod statusu HTTP.",
        "detail": "Bezpieczny opis błędu.",
        "errors": "Lista błędów walidacji pól, jeśli dotyczy.",
        "location": "Położenie pola w żądaniu.",
        "code": "Stabilny kod błędu walidacji.",
        "message": "Bezpieczny opis błędu pola.",
    }
    for name, property_schema in schema.get("properties", {}).items():
        property_schema["description"] = fields[name]
        property_schema["title"] = fields[name].rstrip(".")
        if name == "errors":
            _localize_problem_schema(property_schema["items"], issue=True)


def localize_openapi(
    schema: dict, *, ordinary_problem: dict, validation_problem: dict
) -> None:
    """Add Polish metadata while retaining every public JSON identifier."""
    schema["info"]["description"] = (
        "DreamPlanner: API do planowania dyspozycyjności i grafików pracy."
    )
    schema["tags"] = [
        {"name": name, "description": description}
        for name, description in TAG_DESCRIPTIONS.items()
    ]
    schema["components"]["securitySchemes"]["BearerAuth"]["description"] = (
        "Token dostępu JWT w nagłówku Authorization ze schematem Bearer."
    )
    components = schema["components"]["schemas"]
    if set(components) != set(SCHEMA_DESCRIPTIONS):
        raise ValueError("OpenAPI component descriptions require review")
    for name, component in components.items():
        component["description"] = SCHEMA_DESCRIPTIONS[name]
        for field, field_schema in component.get("properties", {}).items():
            field_schema["description"] = FIELD_DESCRIPTIONS[field]
            field_schema["title"] = FIELD_DESCRIPTIONS[field].rstrip(".")

    _localize_problem_schema(ordinary_problem)
    _localize_problem_schema(validation_problem)

    for path, methods in schema["paths"].items():
        for method, operation in methods.items():
            if not operation.get("description"):
                if path == "/api/v1/health":
                    operation["description"] = (
                        "Sprawdza działanie procesu bez sprawdzania usług zależnych."
                    )
                elif path == "/api/v1/users/me":
                    operation["description"] = (
                        "Zwraca aktualne dane konta uwierzytelnionego użytkownika."
                    )
            elif path == "/api/v1/health":
                operation["description"] = (
                    "Sprawdza działanie procesu bez sprawdzania usług zależnych."
                )
            elif path == "/api/v1/users/me":
                operation["description"] = (
                    "Zwraca aktualne dane konta uwierzytelnionego użytkownika."
                )
            if path.startswith("/api/v1/auth/"):
                operation["summary"] = {
                    ("post", "/api/v1/auth/sessions"): "Zaloguj i utwórz sesję",
                    ("delete", "/api/v1/auth/sessions"): "Wyloguj wszystkie sesje",
                    ("post", "/api/v1/auth/sessions/current/refresh"): "Odśwież sesję",
                    (
                        "delete",
                        "/api/v1/auth/sessions/current",
                    ): "Wyloguj bieżącą sesję",
                    ("post", "/api/v1/auth/activations"): "Aktywuj konto",
                    ("post", "/api/v1/auth/reactivations"): "Reaktywuj konto",
                }[(method, path)]

            for parameter in operation.get("parameters", []):
                parameter.setdefault(
                    "description", PARAMETER_DESCRIPTIONS[parameter["name"]]
                )
            if "requestBody" in operation:
                operation["requestBody"]["description"] = (
                    "Dane żądania zgodne ze wskazanym schematem."
                )

            protected = bool(operation.get("security"))
            public_auth = path.startswith("/api/v1/auth/")
            responses = operation["responses"]
            responses["500"] = _problem_response(
                RESPONSE_DESCRIPTIONS["500"], ordinary_problem
            )
            if protected or path in {
                "/api/v1/auth/sessions",
                "/api/v1/auth/sessions/current/refresh",
                "/api/v1/health/ready",
            }:
                responses["503"] = _problem_response(
                    RESPONSE_DESCRIPTIONS["503"], ordinary_problem
                )

            if path in {
                "/api/v1/auth/activations",
                "/api/v1/auth/reactivations",
            }:
                field_validation = deepcopy(validation_problem)
                field_validation["required"] = [
                    *field_validation["required"],
                    "errors",
                ]
                service_validation = deepcopy(ordinary_problem)
                service_validation["properties"]["type"]["const"] = (
                    "urn:dreamplanner:problem:validation-error"
                )
                service_validation["properties"]["status"]["const"] = 422
                service_validation["additionalProperties"] = False
                responses["422"]["content"]["application/problem+json"]["schema"] = {
                    "oneOf": [field_validation, service_validation]
                }
                responses["422"]["description"] = (
                    "Nieprawidłowe dane żądania lub hasło odrzucone przez usługę."
                )

            for code, response in responses.items():
                problem_schema = (
                    response.get("content", {})
                    .get("application/problem+json", {})
                    .get("schema")
                )
                if problem_schema and "properties" in problem_schema:
                    _localize_problem_schema(problem_schema)
                if code != "422" or path not in {
                    "/api/v1/auth/activations",
                    "/api/v1/auth/reactivations",
                }:
                    response["description"] = _response_description(
                        code, response.get("description", "")
                    )
                if path == "/api/v1/health/ready" and code == "503":
                    response["description"] = "Baza danych jest niedostępna."
                headers = response.setdefault("headers", {})
                if (
                    protected
                    or public_auth
                    or (path == "/api/v1/health" and code == "500")
                    or (path == "/api/v1/health/ready" and code in {"500", "503"})
                ):
                    headers["Cache-Control"] = deepcopy(NO_STORE)
                if code == "401" and protected:
                    headers["WWW-Authenticate"] = deepcopy(BEARER)
                if code == "201":
                    headers["Location"] = deepcopy(LOCATION)
                if code == "200" and (method, path) in {
                    ("post", "/api/v1/auth/sessions"),
                    ("post", "/api/v1/auth/sessions/current/refresh"),
                }:
                    headers["Set-Cookie"] = deepcopy(SET_COOKIE)
                for header_name, header in headers.items():
                    if header_name == "Cache-Control":
                        header["description"] = NO_STORE["description"]
                    elif header_name == "WWW-Authenticate":
                        header["description"] = BEARER["description"]
                    elif header_name == "Location":
                        header["description"] = LOCATION["description"]
