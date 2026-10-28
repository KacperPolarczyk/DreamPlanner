export function validateEmail(value: string): string | null {
  const email = value.trim();
  if (!email) return 'Podaj adres e-mail.';
  if (email.length > 255) return 'Adres e-mail może mieć maksymalnie 255 znaków.';
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) return 'Podaj poprawny adres e-mail.';
  return null;
}

export function validatePassword(value: string): string | null {
  if (!value) return 'Podaj hasło.';
  return null;
}

/** Nowe hasło przy aktywacji konta: niepuste i powtórzone identycznie. */
export function validateNewPassword(password: string, repeated: string): string | null {
  if (!password) return 'Podaj nowe hasło.';
  if (password.length < 8) return 'Hasło powinno mieć co najmniej 8 znaków.';
  if (password !== repeated) return 'Hasła nie są takie same.';
  return null;
}

export function validateRequired(value: string, message: string): string | null {
  return value.trim() ? null : message;
}

export function validatePositionName(value: string): string | null {
  const name = value.trim();
  if (!name) return 'Podaj nazwę stanowiska.';
  if (name.length > 120) return 'Nazwa może mieć maksymalnie 120 znaków.';
  return null;
}

export function validateFullName(value: string): string | null {
  const name = value.trim();
  if (!name) return 'Podaj imię i nazwisko.';
  if (name.length > 150) return 'Imię i nazwisko może mieć maksymalnie 150 znaków.';
  return null;
}

export function validateYear(value: string): string | null {
  const year = Number(value);
  if (!value.trim()) return 'Podaj rok.';
  if (!Number.isInteger(year) || year < 2000 || year > 2100) return 'Rok musi być z zakresu 2000–2100.';
  return null;
}

/** Data musi należeć do miesiąca grafiku (min/max w formacie YYYY-MM-DD). */
export function validateDateInMonth(value: string, min: string, max: string): string | null {
  if (!value) return 'Wybierz datę.';
  if (value < min || value > max) return 'Data musi należeć do miesiąca grafiku.';
  return null;
}

/** Godziny w formacie HH:MM; koniec musi być po początku. */
export function validateTimeRange(start: string, end: string): string | null {
  if (!start || !end) return 'Podaj godzinę rozpoczęcia i zakończenia.';
  if (end <= start) return 'Godzina zakończenia musi być późniejsza niż rozpoczęcia.';
  return null;
}

export function validateWorkers(value: string): string | null {
  const count = Number(value);
  if (!value.trim()) return 'Podaj liczbę osób.';
  if (!Number.isInteger(count) || count < 1) return 'Liczba osób musi być liczbą całkowitą ≥ 1.';
  if (count > 1000) return 'Podaj realną liczbę osób (maks. 1000).';
  return null;
}

export function validateScheduleNumber(value: string): string | null {
  if (!value.trim()) return 'Podaj numer grafiku.';
  if (!/^[0-9]{1,19}$/.test(value.trim())) return 'Numer grafiku składa się z samych cyfr.';
  return null;
}
