import { test, expect } from '@playwright/test';

const user = process.env.E2E_USER;
const password = process.env.E2E_PASSWORD;

test.describe('Stanowiska – kluczowa ścieżka administratora', () => {
  test.skip(!user || !password, 'Brak E2E_USER / E2E_PASSWORD');

  test('admin dodaje stanowisko i widzi je na liście', async ({ page }) => {
    const name = `E2E-${Date.now()}`;

    // 1. Otwarcie aplikacji i logowanie
    await page.goto('/');
    await page.getByLabel('E-mail').fill(user!);
    await page.getByLabel('Hasło').fill(password!);
    await page.getByRole('button', { name: 'Zaloguj się' }).click();
    await expect(page.getByText('Zalogowano:')).toBeVisible();

    // 2. Przejście do zakładki Stanowiska
    await page.getByRole('button', { name: 'Stanowiska', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Stanowiska' })).toBeVisible();

    // 3. Interakcja z formularzem (input + submit)
    await page.getByLabel('Nowe stanowisko').fill(name);
    await page.getByRole('button', { name: 'Dodaj' }).click();

    // 4. Asercja: komunikat sukcesu i nowy wiersz na liście
    await expect(page.getByText('Dodano stanowisko.')).toBeVisible();
    const row = page.getByRole('row').filter({ hasText: name });
    await expect(row).toBeVisible();
    await expect(row.getByText('Aktywne', { exact: true })).toBeVisible();

    // 5. Sprzątanie: dezaktywacja rekordu testowego (akceptujemy okno confirm)
    page.once('dialog', (dialog) => dialog.accept());
    await row.getByRole('button', { name: 'Dezaktywuj' }).click();
    await expect(row.getByText('Nieaktywne', { exact: true })).toBeVisible();
  });
});