import { useCallback, useState } from 'react';
import type { FormEvent } from 'react';
import {
  changeUserRole,
  createUser,
  deactivateUser,
  listUsers,
  reactivateUser,
  reissueActivation,
} from './api';
import type { Role, UserProfile } from './api';
import { FieldError, LoadError, Loading, Messages } from './components';
import { ROLE_LABELS, formatDateTime } from './format';
import { ui } from './styles';
import { useApiAction } from './useApiAction';
import { useLoader } from './useLoader';
import { validateEmail, validateFullName } from './validation';

interface UsersPageProps {
  token: string;
  currentUserId: string;
  onSessionExpired: () => void;
}

interface IssuedToken {
  name: string;
  kind: 'activation' | 'reactivation';
  token: string;
  expiresAt: string;
}

/** Zarządzanie kontami (tylko admin): tworzenie kont, role, dezaktywacja i kody aktywacyjne. */
export default function UsersPage({ token, currentUserId, onSessionExpired }: UsersPageProps) {
  const load = useCallback(() => listUsers(token), [token]);
  const { data: users, loading, error, reload } = useLoader(load, onSessionExpired);
  const action = useApiAction(onSessionExpired);

  const [email, setEmail] = useState('');
  const [fullName, setFullName] = useState('');
  const [role, setRole] = useState<'manager' | 'employee'>('employee');
  const [errors, setErrors] = useState<{ email?: string | null; fullName?: string | null }>({});
  const [issued, setIssued] = useState<IssuedToken | null>(null);
  const [showInactive, setShowInactive] = useState(false);

  async function handleCreate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const nextErrors = { email: validateEmail(email), fullName: validateFullName(fullName) };
    setErrors(nextErrors);
    if (nextErrors.email || nextErrors.fullName) return;

    const created = await action.run(
      () => createUser(token, { email: email.trim(), full_name: fullName.trim(), role }),
      'Utworzono konto. Przekaż użytkownikowi kod aktywacyjny.'
    );
    if (created) {
      setIssued({
        name: created.full_name,
        kind: 'activation',
        token: created.activation_token,
        expiresAt: created.activation_expires_at,
      });
      setEmail('');
      setFullName('');
      reload();
    }
  }

  async function handleRoleChange(user: UserProfile, newRole: Role) {
    if (newRole === user.role) return;
    if (!window.confirm(`Zmienić rolę ${user.full_name} na „${ROLE_LABELS[newRole]}”?`)) return;
    const result = await action.run(() => changeUserRole(token, user.id, newRole), 'Zmieniono rolę.');
    if (result) reload();
  }

  async function handleDeactivate(user: UserProfile) {
    if (
      !window.confirm(
        `Usunąć (dezaktywować) konto ${user.full_name}?\n\nUżytkownik nie będzie mógł się zalogować i zniknie z listy pracowników. Historia jego zmian w grafikach zostaje. Konto można później przywrócić.`
      )
    ) {
      return;
    }
    const result = await action.run(() => deactivateUser(token, user.id), 'Konto usunięte (dezaktywowane). Możesz je przywrócić, zaznaczając „Pokaż usunięte konta”.');
    if (result) reload();
  }

  async function handleReactivate(user: UserProfile) {
    const result = await action.run(
      () => reactivateUser(token, user.id),
      'Wygenerowano kod reaktywacji. Przekaż go użytkownikowi.'
    );
    if (result) {
      setIssued({
        name: user.full_name,
        kind: 'reactivation',
        token: result.reactivation_token,
        expiresAt: result.expires_at,
      });
      reload();
    }
  }

  async function handleReissue(user: UserProfile) {
    const result = await action.run(
      () => reissueActivation(token, user.id),
      'Wygenerowano nowy kod aktywacyjny.'
    );
    if (result) {
      setIssued({
        name: user.full_name,
        kind: 'activation',
        token: result.activation_token,
        expiresAt: result.expires_at,
      });
    }
  }

  return (
    <div>
      <h2>Użytkownicy</h2>

      <form onSubmit={handleCreate} noValidate style={ui.wideCard}>
        <strong>Nowe konto</strong>
        <div style={{ ...ui.formRow, marginTop: 10 }}>
          <label style={{ ...ui.formCol, flex: '2 1 200px' }}>
            <span style={ui.label}>Imię i nazwisko</span>
            <input
              value={fullName}
              onChange={(e) => setFullName(e.target.value)}
              disabled={action.busy}
              style={{ ...ui.input, ...(errors.fullName ? ui.inputError : {}) }}
            />
            <FieldError error={errors.fullName} />
          </label>
          <label style={{ ...ui.formCol, flex: '2 1 200px' }}>
            <span style={ui.label}>E-mail</span>
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              disabled={action.busy}
              style={{ ...ui.input, ...(errors.email ? ui.inputError : {}) }}
            />
            <FieldError error={errors.email} />
          </label>
          <label style={ui.formCol}>
            <span style={ui.label}>Rola</span>
            <select
              value={role}
              onChange={(e) => setRole(e.target.value as 'manager' | 'employee')}
              disabled={action.busy}
              style={ui.select}
            >
              <option value="employee">Pracownik</option>
              <option value="manager">Manager</option>
            </select>
          </label>
          <div style={{ alignSelf: 'flex-end' }}>
            <button type="submit" disabled={action.busy} style={{ ...ui.button, opacity: action.busy ? 0.6 : 1 }}>
              {action.busy ? 'Zapisywanie...' : 'Utwórz konto'}
            </button>
          </div>
        </div>
      </form>

      <Messages message={action.message} error={action.error} />

      {issued && (
        <div style={{ ...ui.banner, ...ui.bannerInfo }}>
          <strong>
            Kod {issued.kind === 'activation' ? 'aktywacyjny' : 'reaktywacji'} dla: {issued.name}
          </strong>{' '}
          (ważny do {formatDateTime(issued.expiresAt)})
          <code style={ui.code}>{issued.token}</code>
          <div style={{ ...ui.actions, marginTop: 8 }}>
            <button
              type="button"
              onClick={() => void navigator.clipboard?.writeText(issued.token)}
              style={ui.buttonSecondary}
            >
              Kopiuj kod
            </button>
            <button type="button" onClick={() => setIssued(null)} style={ui.buttonSecondary}>
              Ukryj
            </button>
          </div>
          <div style={{ ...ui.muted, marginTop: 6 }}>
            Użytkownik wpisuje ten kod na ekranie logowania w „Aktywuj konto” i ustawia hasło. Kod jest pokazywany tylko raz.
          </div>
        </div>
      )}

      {loading && <Loading text="Ładowanie użytkowników..." />}
      {error && <LoadError error={error} onRetry={reload} />}

      {!loading && !error && users && (
        <>
        <label style={{ ...ui.muted, display: 'flex', alignItems: 'center', gap: 6, marginTop: 16 }}>
          <input type="checkbox" checked={showInactive} onChange={(e) => setShowInactive(e.target.checked)} />
          Pokaż usunięte (nieaktywne) konta ({users.filter((u) => !u.is_active).length})
        </label>
        <table style={ui.table}>
          <thead>
            <tr>
              <th style={ui.cell}>Imię i nazwisko</th>
              <th style={ui.cell}>E-mail</th>
              <th style={ui.cell}>Rola</th>
              <th style={ui.cell}>Stan konta</th>
              <th style={ui.cell}>Akcje</th>
            </tr>
          </thead>
          <tbody>
            {users.filter((user) => showInactive || user.is_active).map((user) => {
              const isMe = user.id === currentUserId;
              const state = !user.is_active
                ? { text: 'Nieaktywne', color: '#888' }
                : user.activated_at
                  ? { text: 'Aktywne', color: 'green' }
                  : { text: 'Czeka na aktywację', color: '#b45309' };
              return (
                <tr key={user.id}>
                  <td style={ui.cell}>
                    {user.full_name}
                    {isMe && <span style={ui.muted}> (Ty)</span>}
                  </td>
                  <td style={ui.cell}>{user.email}</td>
                  <td style={ui.cell}>
                    <select
                      value={user.role}
                      onChange={(e) => handleRoleChange(user, e.target.value as Role)}
                      disabled={action.busy || isMe}
                      style={ui.smallInput}
                    >
                      {(Object.keys(ROLE_LABELS) as Role[]).map((r) => (
                        <option key={r} value={r}>
                          {ROLE_LABELS[r]}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td style={{ ...ui.cell, color: state.color }}>{state.text}</td>
                  <td style={ui.cell}>
                    <div style={ui.actions}>
                      {user.is_active && !user.activated_at && (
                        <button type="button" disabled={action.busy} onClick={() => handleReissue(user)} style={ui.buttonSecondary}>
                          Nowy kod aktywacyjny
                        </button>
                      )}
                      {user.is_active && !isMe && (
                        <button type="button" disabled={action.busy} onClick={() => handleDeactivate(user)} style={ui.buttonDanger}>
                          Usuń konto
                        </button>
                      )}
                      {!user.is_active && (
                        <button type="button" disabled={action.busy} onClick={() => handleReactivate(user)} style={ui.buttonSecondary}>
                          Przywróć
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        </>
      )}
    </div>
  );
}
