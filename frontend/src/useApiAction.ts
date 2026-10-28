import { useCallback, useState } from 'react';
import { ApiError } from './api';

export class CancelledError extends Error {
  constructor() {
    super('Anulowano.');
    this.name = 'CancelledError';
  }
}

/**
 * Wspólna obsługa stanów operacji: "zapisywanie...", komunikat sukcesu i komunikat błędu.
 * Przy 401 (wygasły token) wywołuje onSessionExpired, żeby wrócić do ekranu logowania.
 */
export function useApiAction(onSessionExpired: () => void) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const reportError = useCallback(
    (err: unknown): string | null => {
      // Użytkownik sam anulował operację (np. w oknie potwierdzenia) – bez komunikatu błędu.
      if (err instanceof CancelledError) return null;
      if (err instanceof ApiError && err.status === 401) {
        onSessionExpired();
        return null;
      }
      return err instanceof ApiError ? err.message : 'Nieoczekiwany błąd.';
    },
    [onSessionExpired]
  );

  const run = useCallback(
    async <T,>(action: () => Promise<T>, successMessage: string | null): Promise<T | undefined> => {
      setBusy(true);
      setError(null);
      setMessage(null);
      try {
        const result = await action();
        setMessage(successMessage);
        return result;
      } catch (err) {
        setError(reportError(err));
        return undefined;
      } finally {
        setBusy(false);
      }
    },
    [reportError]
  );

  const clear = useCallback(() => {
    setMessage(null);
    setError(null);
  }, []);

  return { busy, message, error, setError, setMessage, run, reportError, clear };
}
