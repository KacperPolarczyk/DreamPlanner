import { useState } from 'react';
import type { FormEvent } from 'react';
import { ApiError, activateAccount } from './api';
import { FieldError } from './components';
import { ui } from './styles';
import { validateNewPassword, validateRequired } from './validation';

interface ActivationFormProps {
  onDone: (message: string) => void;
  onCancel: () => void;
}

/** Ustawienie hasła kodem od administratora: aktywacja nowego konta albo reaktywacja. */
export default function ActivationForm({ onDone, onCancel }: ActivationFormProps) {
  const [kind, setKind] = useState<'activation' | 'reactivation'>('activation');
  const [code, setCode] = useState('');
  const [password, setPassword] = useState('');
  const [repeated, setRepeated] = useState('');
  const [errors, setErrors] = useState<{ code?: string | null; password?: string | null }>({});
  const [apiError, setApiError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setApiError(null);
    const nextErrors = {
      code: validateRequired(code, 'Wklej kod otrzymany od administratora.'),
      password: validateNewPassword(password, repeated),
    };
    setErrors(nextErrors);
    if (nextErrors.code || nextErrors.password) return;

    setLoading(true);
    try {
      await activateAccount(kind, code.trim(), password);
      onDone('Hasło ustawione. Możesz się teraz zalogować.');
    } catch (err) {
      setApiError(
        err instanceof ApiError && err.status === 401
          ? 'Kod jest nieprawidłowy, wygasł albo został już użyty. Poproś administratora o nowy.'
          : err instanceof ApiError
            ? err.message
            : 'Nieoczekiwany błąd.'
      );
      setLoading(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} noValidate style={ui.card}>
      <h2 style={{ marginTop: 0 }}>Aktywacja konta</h2>

      <label style={ui.field}>
        <span style={ui.label}>Rodzaj kodu</span>
        <select
          value={kind}
          onChange={(e) => setKind(e.target.value as 'activation' | 'reactivation')}
          disabled={loading}
          style={ui.select}
        >
          <option value="activation">Nowe konto (kod aktywacyjny)</option>
          <option value="reactivation">Przywrócenie konta (kod reaktywacji)</option>
        </select>
      </label>

      <label style={ui.field}>
        <span style={ui.label}>Kod od administratora</span>
        <input
          value={code}
          onChange={(e) => setCode(e.target.value)}
          autoComplete="off"
          disabled={loading}
          style={{ ...ui.input, ...(errors.code ? ui.inputError : {}) }}
        />
        <FieldError error={errors.code} />
      </label>

      <label style={ui.field}>
        <span style={ui.label}>Nowe hasło</span>
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete="new-password"
          disabled={loading}
          style={{ ...ui.input, ...(errors.password ? ui.inputError : {}) }}
        />
      </label>

      <label style={ui.field}>
        <span style={ui.label}>Powtórz hasło</span>
        <input
          type="password"
          value={repeated}
          onChange={(e) => setRepeated(e.target.value)}
          autoComplete="new-password"
          disabled={loading}
          style={{ ...ui.input, ...(errors.password ? ui.inputError : {}) }}
        />
        <FieldError error={errors.password} />
      </label>

      <div style={ui.actions}>
        <button type="submit" disabled={loading} style={{ ...ui.button, opacity: loading ? 0.6 : 1 }}>
          {loading ? 'Zapisywanie...' : 'Ustaw hasło'}
        </button>
        <button type="button" disabled={loading} onClick={onCancel} style={ui.buttonSecondary}>
          Wróć do logowania
        </button>
      </div>

      {apiError && <div style={{ ...ui.banner, ...ui.bannerError }}>{apiError}</div>}
    </form>
  );
}
