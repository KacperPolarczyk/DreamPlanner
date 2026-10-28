import { useCallback, useEffect, useState } from 'react';
import { ApiError } from './api';

/**
 * Pobiera dane przy pierwszym renderze i po każdym wywołaniu reload().
 * Zwraca też stan ładowania i błąd, żeby ekran mógł pokazać spinner albo komunikat.
 */
export function useLoader<T>(
  load: () => Promise<T>,
  onSessionExpired: () => void
): {
  data: T | null;
  loading: boolean;
  error: string | null;
  reload: () => void;
} {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    let cancelled = false;
    load()
      .then((result) => {
        if (!cancelled) {
          setData(result);
          setError(null);
        }
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 401) {
          onSessionExpired();
          return;
        }
        setError(err instanceof ApiError ? err.message : 'Nieoczekiwany błąd.');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [load, reloadKey, onSessionExpired]);

  const reload = useCallback(() => {
    setLoading(true);
    setReloadKey((key) => key + 1);
  }, []);

  return { data, loading, error, reload };
}
