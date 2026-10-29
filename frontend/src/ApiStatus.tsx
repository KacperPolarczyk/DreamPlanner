import { useEffect, useState } from 'react';

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL;

type Status =
  | { kind: 'loading' }
  | { kind: 'ok' }
  | { kind: 'error'; message: string };

export default function ApiStatus() {
  const [status, setStatus] = useState<Status>({ kind: 'loading' });

  useEffect(() => {
    fetch(`${API_BASE_URL}/health/ready`)
      .then(async (res) => {
        if (!res.ok) throw new Error(`Błąd HTTP ${res.status}`);
        const body = await res.json();
        if (body.status !== 'ok') throw new Error('Nieoczekiwana odpowiedź API');
        setStatus({ kind: 'ok' });
      })
      .catch((err: Error) =>
        setStatus({
          kind: 'error',
          message:
            err.message === 'Failed to fetch'
              ? 'Brak połączenia z API (sprawdź adres lub CORS)'
              : err.message,
        })
      );
  }, []);

  const color =
    status.kind === 'ok' ? 'green' : status.kind === 'error' ? 'red' : '#888';

  return (
    <p style={{ fontFamily: 'sans-serif', padding: '0 20px', color }}>
      Status API:{' '}
      {status.kind === 'loading' && 'sprawdzanie...'}
      {status.kind === 'ok' && 'działa (baza połączona)'}
      {status.kind === 'error' && status.message}
    </p>
  );
}