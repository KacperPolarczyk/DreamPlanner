import { useCallback, useEffect, useState } from 'react';
import type { FormEvent } from 'react';
import {
  ApiError,
  createPosition,
  deactivatePosition,
  listPositions,
  reactivatePosition,
  renamePosition,
} from './api';
import type { Position } from './api';
import { ui } from './styles';
import { validatePositionName } from './validation';

interface PositionsPageProps {
  token: string;
  onLogout: () => void;
  onSessionExpired: () => void;
}

export default function PositionsPage({ token, onLogout, onSessionExpired }: PositionsPageProps) {
  const [positions, setPositions] = useState<Position[]>([]);
  const [loading, setLoading] = useState(true);
  const [listError, setListError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  const [newName, setNewName] = useState('');
  const [newNameError, setNewNameError] = useState<string | null>(null);

  const [editingId, setEditingId] = useState<string | null>(null);
  const [editName, setEditName] = useState('');
  const [editError, setEditError] = useState<string | null>(null);

  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  // Zwraca tekst błędu do pokazania, a przy 401 (wygasły token) wylogowuje użytkownika.
  const reportError = useCallback(
    (err: unknown): string | null => {
      if (err instanceof ApiError && err.status === 401) {
        onSessionExpired();
        return null;
      }
      return err instanceof ApiError ? err.message : 'Nieoczekiwany błąd.';
    },
    [onSessionExpired]
  );

  useEffect(() => {
    let cancelled = false;
    listPositions(token)
      .then((data) => {
        if (!cancelled) setPositions(data);
      })
      .catch((err) => {
        if (!cancelled) setListError(reportError(err));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [token, reloadKey, reportError]);

  async function runAction(action: () => Promise<unknown>, successMessage: string) {
    setBusy(true);
    setActionError(null);
    setMessage(null);
    try {
      await action();
      setMessage(successMessage);
      setReloadKey((key) => key + 1);
      return true;
    } catch (err) {
      setActionError(reportError(err));
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function handleAdd(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const validationError = validatePositionName(newName);
    setNewNameError(validationError);
    if (validationError) return;

    const ok = await runAction(() => createPosition(token, newName.trim()), 'Dodano stanowisko.');
    if (ok) setNewName('');
  }

  function startEdit(position: Position) {
    setEditingId(position.id);
    setEditName(position.name);
    setEditError(null);
  }

  async function saveEdit(position: Position) {
    const validationError = validatePositionName(editName);
    setEditError(validationError);
    if (validationError) return;

    const ok = await runAction(
      () => renamePosition(token, position.id, editName.trim()),
      'Zmieniono nazwę stanowiska.'
    );
    if (ok) setEditingId(null);
  }

  async function toggleActive(position: Position) {
    if (position.is_active) {
      if (!window.confirm(`Dezaktywować stanowisko „${position.name}”?`)) return;
      await runAction(() => deactivatePosition(token, position.id), 'Stanowisko zdezaktywowane.');
    } else {
      await runAction(() => reactivatePosition(token, position.id), 'Stanowisko ponownie aktywne.');
    }
  }

  function retry() {
    setListError(null);
    setLoading(true);
    setReloadKey((key) => key + 1);
  }

  return (
    <div>
      <div style={ui.header}>
        <h2>Stanowiska</h2>
        <button type="button" onClick={onLogout} style={ui.buttonSecondary}>
          Wyloguj
        </button>
      </div>

      <form onSubmit={handleAdd} noValidate style={{ ...ui.card, maxWidth: 'none' }}>
        <label style={ui.label} htmlFor="new-position">
          Nowe stanowisko
        </label>
        <div style={{ display: 'flex', gap: 8 }}>
          <input
            id="new-position"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            placeholder="np. Kuchnia"
            disabled={busy}
            style={{ ...ui.input, ...(newNameError ? ui.inputError : {}) }}
          />
          <button type="submit" disabled={busy} style={{ ...ui.button, opacity: busy ? 0.6 : 1 }}>
            {busy ? 'Zapisywanie...' : 'Dodaj'}
          </button>
        </div>
        {newNameError && <div style={ui.fieldError}>{newNameError}</div>}
      </form>

      {message && <div style={{ ...ui.banner, ...ui.bannerSuccess }}>{message}</div>}
      {actionError && <div style={{ ...ui.banner, ...ui.bannerError }}>{actionError}</div>}

      {loading && (
        <p style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span className="spinner" /> Ładowanie stanowisk...
        </p>
      )}

      {listError && (
        <div style={{ ...ui.banner, ...ui.bannerError }}>
          {listError}{' '}
          <button type="button" onClick={retry} style={ui.buttonSecondary}>
            Spróbuj ponownie
          </button>
        </div>
      )}

      {!loading && !listError && positions.length === 0 && <p>Brak stanowisk. Dodaj pierwsze powyżej.</p>}

      {!loading && !listError && positions.length > 0 && (
        <table style={ui.table}>
          <thead>
            <tr>
              <th style={ui.cell}>ID</th>
              <th style={ui.cell}>Nazwa</th>
              <th style={ui.cell}>Status</th>
              <th style={ui.cell}>Akcje</th>
            </tr>
          </thead>
          <tbody>
            {positions.map((position) => (
              <tr key={position.id}>
                <td style={ui.cell}>{position.id}</td>
                <td style={ui.cell}>
                  {editingId === position.id ? (
                    <>
                      <input
                        value={editName}
                        onChange={(e) => setEditName(e.target.value)}
                        disabled={busy}
                        style={{ ...ui.input, ...(editError ? ui.inputError : {}) }}
                      />
                      {editError && <div style={ui.fieldError}>{editError}</div>}
                    </>
                  ) : (
                    position.name
                  )}
                </td>
                <td style={{ ...ui.cell, color: position.is_active ? 'green' : '#888' }}>
                  {position.is_active ? 'Aktywne' : 'Nieaktywne'}
                </td>
                <td style={ui.cell}>
                  <div style={ui.actions}>
                    {editingId === position.id ? (
                      <>
                        <button type="button" disabled={busy} onClick={() => saveEdit(position)} style={ui.buttonSecondary}>
                          Zapisz
                        </button>
                        <button type="button" disabled={busy} onClick={() => setEditingId(null)} style={ui.buttonSecondary}>
                          Anuluj
                        </button>
                      </>
                    ) : (
                      <>
                        <button type="button" disabled={busy} onClick={() => startEdit(position)} style={ui.buttonSecondary}>
                          Zmień nazwę
                        </button>
                        <button type="button" disabled={busy} onClick={() => toggleActive(position)} style={ui.buttonSecondary}>
                          {position.is_active ? 'Dezaktywuj' : 'Aktywuj'}
                        </button>
                      </>
                    )}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
