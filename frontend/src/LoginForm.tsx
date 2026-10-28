import { useState } from 'react';
import type { FormEvent } from 'react';
import { ApiError, login } from './api';
import { ui } from './styles';
import { validateEmail, validatePassword } from './validation';

interface LoginFormProps {
  onLogin: (token: string) => void;
  notice?: string | null;
}

export default function LoginForm({ onLogin, notice }: LoginFormProps) {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [errors, setErrors] = useState<{ email?: string; password?: string }>({});
  const [apiError, setApiError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setApiError(null);

    const nextErrors = {
      email: validateEmail(email) ?? undefined,
      password: validatePassword(password) ?? undefined,
    };
    setErrors(nextErrors);
    if (nextErrors.email || nextErrors.password) return;

    setLoading(true);
    try {
      const result = await login(email.trim(), password);
      onLogin(result.access_token);
    } catch (err) {
      setApiError(err instanceof ApiError ? err.message : 'Nieoczekiwany błąd.');
      setLoading(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} noValidate style={ui.card}>
      <h2 style={{ marginTop: 0 }}>Logowanie</h2>

      {notice && <div style={{ ...ui.banner, ...ui.bannerInfo }}>{notice}</div>}

      <label style={{ ...ui.field, marginTop: 14 }}>
        <span style={ui.label}>E-mail</span>
        <input
          type="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          autoComplete="username"
          disabled={loading}
          style={{ ...ui.input, ...(errors.email ? ui.inputError : {}) }}
        />
        {errors.email && <div style={ui.fieldError}>{errors.email}</div>}
      </label>

      <label style={ui.field}>
        <span style={ui.label}>Hasło</span>
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete="current-password"
          disabled={loading}
          style={{ ...ui.input, ...(errors.password ? ui.inputError : {}) }}
        />
        {errors.password && <div style={ui.fieldError}>{errors.password}</div>}
      </label>

      <button type="submit" disabled={loading} style={{ ...ui.button, opacity: loading ? 0.6 : 1 }}>
        {loading ? 'Logowanie...' : 'Zaloguj się'}
      </button>

      {apiError && <div style={{ ...ui.banner, ...ui.bannerError }}>{apiError}</div>}
    </form>
  );
}
