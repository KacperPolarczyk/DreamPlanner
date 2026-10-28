const API_BASE_URL = import.meta.env.VITE_API_BASE_URL as string;

export interface Position {
  id: string;
  name: string;
  is_active: boolean;
}

export interface LoginResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
}

interface ProblemDetails {
  type?: string;
  title?: string;
  status?: number;
  detail?: string;
  errors?: { location?: (string | number)[]; code?: string; message?: string }[];
}

export class ApiError extends Error {
  status: number;
  problem: ProblemDetails | null;

  constructor(message: string, status: number, problem: ProblemDetails | null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.problem = problem;
  }
}

function describeError(status: number, problem: ProblemDetails | null): string {
  if (status === 422 && problem?.errors?.length) {
    const fields = problem.errors
      .map((e) => (e.location ?? []).filter((part) => part !== 'body').join('.'))
      .filter(Boolean)
      .join(', ');
    return `Nieprawidłowe dane${fields ? ` (pole: ${fields})` : ''}.`;
  }
  if (problem?.detail) return problem.detail;
  switch (status) {
    case 401:
      return 'Brak uwierzytelnienia. Zaloguj się ponownie.';
    case 403:
      return 'Brak uprawnień do tej operacji.';
    case 404:
      return 'Nie znaleziono zasobu.';
    case 409:
      return 'Konflikt z aktualnym stanem danych.';
    case 429:
      return 'Zbyt wiele prób. Spróbuj ponownie za chwilę.';
    default:
      return status >= 500
        ? 'Błąd serwera. Spróbuj ponownie później.'
        : `Błąd żądania (HTTP ${status}).`;
  }
}

interface RequestOptions {
  method?: 'GET' | 'POST' | 'PATCH' | 'PUT' | 'DELETE';
  body?: unknown;
  token?: string | null;
}

async function request<T>(
  path: string,
  { method = 'GET', body, token }: RequestOptions = {}
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      method,
      headers: {
        Accept: 'application/json',
        ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError('Brak połączenia z serwerem (sprawdź internet lub CORS).', 0, null);
  }

  const text = await response.text();
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = null;
    }
  }

  if (!response.ok) {
    const problem = data && typeof data === 'object' ? (data as ProblemDetails) : null;
    throw new ApiError(describeError(response.status, problem), response.status, problem);
  }
  return data as T;
}

export function login(email: string, password: string): Promise<LoginResponse> {
  return request<LoginResponse>('/auth/sessions', {
    method: 'POST',
    body: { email, password },
  });
}

export function logout(token: string): Promise<void> {
  return request<void>('/auth/sessions/current', { method: 'DELETE', token });
}

export async function listPositions(token: string): Promise<Position[]> {
  // API stronicuje wyniki (domyślnie 25), więc prosimy o maksimum 100 na stronę.
  const data = await request<Position[] | { items?: Position[] }>(
    '/positions?limit=100&sort=name',
    { token }
  );
  return Array.isArray(data) ? data : (data?.items ?? []);
}

export function createPosition(token: string, name: string): Promise<Position> {
  return request<Position>('/positions', { method: 'POST', body: { name }, token });
}

export function renamePosition(token: string, id: string, name: string): Promise<Position> {
  return request<Position>(`/positions/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    body: { name },
    token,
  });
}

export function deactivatePosition(token: string, id: string): Promise<unknown> {
  return request<unknown>(`/positions/${encodeURIComponent(id)}/deactivation`, {
    method: 'POST',
    token,
  });
}

export function reactivatePosition(token: string, id: string): Promise<unknown> {
  return request<unknown>(`/positions/${encodeURIComponent(id)}/reactivation`, {
    method: 'POST',
    token,
  });
}