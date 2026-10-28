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

export function validatePositionName(value: string): string | null {
  const name = value.trim();
  if (!name) return 'Podaj nazwę stanowiska.';
  if (name.length > 120) return 'Nazwa może mieć maksymalnie 120 znaków.';
  return null;
}