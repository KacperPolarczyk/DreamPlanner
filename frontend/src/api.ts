const API_BASE_URL = import.meta.env.VITE_API_BASE_URL as string;

// ---------------------------------------------------------------------------
// Typy danych zwracanych przez API
// ---------------------------------------------------------------------------

export type Role = 'admin' | 'manager' | 'employee';

export type ScheduleStatus = 'draft' | 'collecting' | 'collection_closed' | 'closed' | 'published';

export type AvailabilityStatus = 'available' | 'unavailable';

export type WarningType = 'unavailable' | 'unknown' | null;

export interface Page<T> {
  items: T[];
  limit: number;
  offset: number;
  total: number;
}

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

export interface UserProfile {
  id: string;
  email: string;
  full_name: string;
  role: Role;
  is_active: boolean;
  activated_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface CreatedUser extends UserProfile {
  activation_token: string;
  activation_expires_at: string;
}

export interface ReactivatedUser extends UserProfile {
  reactivation_token: string;
  expires_at: string;
}

export interface ActivationToken {
  activation_token: string;
  expires_at: string;
}

export interface Employee {
  user_id: string;
  full_name: string;
}

export interface Schedule {
  id: string;
  year: number;
  month: number;
  status: ScheduleStatus;
  created_by: string;
  created_at: string;
  collection_opened_at: string | null;
  collection_closed_at: string | null;
  published_at: string | null;
}

export interface ShiftRequirement {
  id: string;
  schedule_id: string;
  position_id: string;
  position_name: string;
  position_is_active: boolean;
  work_date: string;
  start_time: string;
  end_time: string;
  required_workers: number;
  note: string | null;
}

export interface ShiftRequirementInput {
  position_id: string;
  work_date: string;
  start_time: string;
  end_time: string;
  required_workers: number;
  note: string | null;
}

export interface ShiftAssignment {
  id: string;
  schedule_id: string;
  shift_requirement_id: string;
  user_id: string;
  assigned_by: string;
  assigned_at: string;
  warning_type: WarningType;
  warning_acknowledged_at: string | null;
  work_date: string;
  start_time: string;
  end_time: string;
}

export interface MyShift {
  assignment_id: string;
  schedule_id: string;
  shift_requirement_id: string;
  position_id: string;
  position_name: string;
  work_date: string;
  start_time: string;
  end_time: string;
  warning_type: WarningType;
  warning_acknowledged_at: string | null;
}

export interface AvailabilityEntry {
  id?: string;
  work_date: string;
  start_time: string;
  end_time: string;
  status: AvailabilityStatus;
  note: string | null;
}

export interface AvailabilitySummary {
  id: string;
  schedule_id: string;
  user_id: string;
  submitted_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface AvailabilitySubmission extends AvailabilitySummary {
  entries: AvailabilityEntry[];
}

// ---------------------------------------------------------------------------
// Obsługa błędów (format "problem details" z backendu)
// ---------------------------------------------------------------------------

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

  /** Typ problemu z backendu, np. "urn:dreamplanner:problem:assignment-overlap". */
  get problemType(): string | undefined {
    return this.problem?.type;
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

function id(value: string): string {
  return encodeURIComponent(value);
}

// ---------------------------------------------------------------------------
// Logowanie, sesja, aktywacja konta
// ---------------------------------------------------------------------------

export function login(email: string, password: string): Promise<LoginResponse> {
  return request<LoginResponse>('/auth/sessions', {
    method: 'POST',
    body: { email, password },
  });
}

export function logout(token: string): Promise<void> {
  return request<void>('/auth/sessions/current', { method: 'DELETE', token });
}

/** Ustawienie hasła jednorazowym tokenem (nowe konto lub reaktywacja). */
export function activateAccount(
  kind: 'activation' | 'reactivation',
  activationToken: string,
  newPassword: string
): Promise<void> {
  const path = kind === 'activation' ? '/auth/activations' : '/auth/reactivations';
  return request<void>(path, {
    method: 'POST',
    body: { token: activationToken, new_password: newPassword },
  });
}

export function getCurrentUser(token: string): Promise<UserProfile> {
  return request<UserProfile>('/users/me', { token });
}

// ---------------------------------------------------------------------------
// Stanowiska (admin: zapis, manager: odczyt)
// ---------------------------------------------------------------------------

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

export function renamePosition(token: string, positionId: string, name: string): Promise<Position> {
  return request<Position>(`/positions/${id(positionId)}`, {
    method: 'PATCH',
    body: { name },
    token,
  });
}

export function deactivatePosition(token: string, positionId: string): Promise<unknown> {
  return request<unknown>(`/positions/${id(positionId)}/deactivation`, {
    method: 'POST',
    token,
  });
}

export function reactivatePosition(token: string, positionId: string): Promise<unknown> {
  return request<unknown>(`/positions/${id(positionId)}/reactivation`, {
    method: 'POST',
    token,
  });
}

// ---------------------------------------------------------------------------
// Użytkownicy (tylko admin) i lista pracowników (manager/admin)
// ---------------------------------------------------------------------------

export async function listUsers(token: string): Promise<UserProfile[]> {
  const data = await request<Page<UserProfile>>('/users?limit=100&sort=full_name', { token });
  return data.items;
}

export function createUser(
  token: string,
  input: { email: string; full_name: string; role: 'manager' | 'employee' }
): Promise<CreatedUser> {
  return request<CreatedUser>('/users', { method: 'POST', body: input, token });
}

export function changeUserRole(token: string, userId: string, role: Role): Promise<UserProfile> {
  return request<UserProfile>(`/users/${id(userId)}/role`, {
    method: 'PUT',
    body: { role },
    token,
  });
}

export function deactivateUser(token: string, userId: string): Promise<UserProfile> {
  return request<UserProfile>(`/users/${id(userId)}/deactivation`, { method: 'POST', token });
}

export function reactivateUser(token: string, userId: string): Promise<ReactivatedUser> {
  return request<ReactivatedUser>(`/users/${id(userId)}/reactivation`, { method: 'POST', token });
}

export function reissueActivation(token: string, userId: string): Promise<ActivationToken> {
  return request<ActivationToken>(`/users/${id(userId)}/activation-tokens`, {
    method: 'POST',
    token,
  });
}

export async function listEmployees(token: string): Promise<Employee[]> {
  const data = await request<Page<Employee>>('/employees?limit=100', { token });
  return data.items;
}

// ---------------------------------------------------------------------------
// Grafiki (manager: zapis, admin: odczyt)
// ---------------------------------------------------------------------------

export async function listSchedules(token: string): Promise<Schedule[]> {
  const data = await request<Page<Schedule>>('/schedules?limit=100', { token });
  return data.items;
}

export function getSchedule(token: string, scheduleId: string): Promise<Schedule> {
  return request<Schedule>(`/schedules/${id(scheduleId)}`, { token });
}

export function createSchedule(token: string, year: number, month: number): Promise<Schedule> {
  return request<Schedule>('/schedules', { method: 'POST', body: { year, month }, token });
}

export function deleteSchedule(token: string, scheduleId: string): Promise<void> {
  return request<void>(`/schedules/${id(scheduleId)}`, { method: 'DELETE', token });
}

/** Kolejne kroki cyklu życia grafiku: draft → collecting → collection_closed → closed → published. */
export type ScheduleTransition =
  | 'collection-opening'
  | 'collection-closing'
  | 'closing'
  | 'publication';

export function changeScheduleStatus(
  token: string,
  scheduleId: string,
  transition: ScheduleTransition
): Promise<Schedule> {
  return request<Schedule>(`/schedules/${id(scheduleId)}/${transition}`, {
    method: 'POST',
    token,
  });
}

// ---------------------------------------------------------------------------
// Zapotrzebowanie na pracowników (zmiany do obsadzenia)
// ---------------------------------------------------------------------------

export async function listShiftRequirements(
  token: string,
  scheduleId: string
): Promise<ShiftRequirement[]> {
  const data = await request<Page<ShiftRequirement>>(
    `/schedules/${id(scheduleId)}/shift-requirements?limit=100`,
    { token }
  );
  return data.items;
}

export function createShiftRequirement(
  token: string,
  scheduleId: string,
  input: ShiftRequirementInput
): Promise<ShiftRequirement> {
  return request<ShiftRequirement>(`/schedules/${id(scheduleId)}/shift-requirements`, {
    method: 'POST',
    body: input,
    token,
  });
}

export function updateShiftRequirement(
  token: string,
  scheduleId: string,
  requirementId: string,
  input: Partial<ShiftRequirementInput>
): Promise<ShiftRequirement> {
  return request<ShiftRequirement>(
    `/schedules/${id(scheduleId)}/shift-requirements/${id(requirementId)}`,
    { method: 'PATCH', body: input, token }
  );
}

export function deleteShiftRequirement(
  token: string,
  scheduleId: string,
  requirementId: string
): Promise<void> {
  return request<void>(`/schedules/${id(scheduleId)}/shift-requirements/${id(requirementId)}`, {
    method: 'DELETE',
    token,
  });
}

// ---------------------------------------------------------------------------
// Przydziały pracowników do zmian (obsada)
// ---------------------------------------------------------------------------

export async function listAssignments(token: string, scheduleId: string): Promise<ShiftAssignment[]> {
  const data = await request<Page<ShiftAssignment>>(
    `/schedules/${id(scheduleId)}/shift-assignments?limit=100`,
    { token }
  );
  return data.items;
}

export function createAssignment(
  token: string,
  scheduleId: string,
  input: { shift_requirement_id: string; user_id: string; warning_acknowledged: boolean }
): Promise<ShiftAssignment> {
  return request<ShiftAssignment>(`/schedules/${id(scheduleId)}/shift-assignments`, {
    method: 'POST',
    body: input,
    token,
  });
}

export function deleteAssignment(
  token: string,
  scheduleId: string,
  assignmentId: string
): Promise<void> {
  return request<void>(`/schedules/${id(scheduleId)}/shift-assignments/${id(assignmentId)}`, {
    method: 'DELETE',
    token,
  });
}

export async function listMyShifts(token: string): Promise<MyShift[]> {
  const data = await request<Page<MyShift>>('/me/shifts?limit=100', { token });
  return data.items;
}

// ---------------------------------------------------------------------------
// Dyspozycyjność pracowników
// ---------------------------------------------------------------------------

export async function listAvailability(
  token: string,
  scheduleId: string
): Promise<AvailabilitySummary[]> {
  const data = await request<Page<AvailabilitySummary>>(
    `/schedules/${id(scheduleId)}/availability-submissions?limit=100`,
    { token }
  );
  return data.items;
}

export function getAvailability(
  token: string,
  scheduleId: string,
  submissionId: string
): Promise<AvailabilitySubmission> {
  return request<AvailabilitySubmission>(
    `/schedules/${id(scheduleId)}/availability-submissions/${id(submissionId)}`,
    { token }
  );
}

type EntryInput = Omit<AvailabilityEntry, 'id'>;

export function createAvailability(
  token: string,
  scheduleId: string,
  entries: EntryInput[],
  submit: boolean
): Promise<AvailabilitySubmission> {
  return request<AvailabilitySubmission>(`/schedules/${id(scheduleId)}/availability-submissions`, {
    method: 'POST',
    body: { entries, submit },
    token,
  });
}

export function updateAvailability(
  token: string,
  scheduleId: string,
  submissionId: string,
  entries: EntryInput[],
  submit: boolean
): Promise<AvailabilitySubmission> {
  return request<AvailabilitySubmission>(
    `/schedules/${id(scheduleId)}/availability-submissions/${id(submissionId)}`,
    { method: 'PATCH', body: { entries, submit }, token }
  );
}

export function deleteAvailability(
  token: string,
  scheduleId: string,
  submissionId: string
): Promise<void> {
  return request<void>(`/schedules/${id(scheduleId)}/availability-submissions/${id(submissionId)}`, {
    method: 'DELETE',
    token,
  });
}
