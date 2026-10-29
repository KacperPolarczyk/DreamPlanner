import type { Role, ScheduleStatus } from './api';

export const MONTHS = [
  'Styczeń',
  'Luty',
  'Marzec',
  'Kwiecień',
  'Maj',
  'Czerwiec',
  'Lipiec',
  'Sierpień',
  'Wrzesień',
  'Październik',
  'Listopad',
  'Grudzień',
];

export function monthName(month: number): string {
  return MONTHS[month - 1] ?? String(month);
}

export function scheduleLabel(schedule: { year: number; month: number }): string {
  return `${monthName(schedule.month)} ${schedule.year}`;
}

export const STATUS_LABELS: Record<ScheduleStatus, string> = {
  draft: 'Szkic',
  collecting: 'Zbieranie dyspozycyjności',
  collection_closed: 'Zbieranie zamknięte',
  closed: 'Obsadzanie zmian',
  published: 'Opublikowany',
};

export const STATUS_COLORS: Record<ScheduleStatus, string> = {
  draft: '#6b7280',
  collecting: '#b45309',
  collection_closed: '#7c3aed',
  closed: '#2563eb',
  published: '#15803d',
};

export const ROLE_LABELS: Record<Role, string> = {
  admin: 'Administrator',
  manager: 'Manager',
  employee: 'Pracownik',
};

const WEEKDAYS = ['nd', 'pn', 'wt', 'śr', 'cz', 'pt', 'sb'];

/** "2026-11-03" → "03.11.2026 (wt)" */
export function formatDate(isoDate: string): string {
  const [year, month, day] = isoDate.split('-').map(Number);
  const weekday = WEEKDAYS[new Date(year, month - 1, day).getDay()];
  return `${String(day).padStart(2, '0')}.${String(month).padStart(2, '0')}.${year} (${weekday})`;
}

/** "08:00:00" → "08:00" */
export function formatTime(time: string): string {
  return time.slice(0, 5);
}

/** Pole <input type="time"> daje "08:00", a API wymaga "08:00:00". */
export function toApiTime(time: string): string {
  return time.length === 5 ? `${time}:00` : time;
}

export function formatDateTime(iso: string | null): string {
  if (!iso) return '–';
  return new Date(iso).toLocaleString('pl-PL', { dateStyle: 'short', timeStyle: 'short' });
}

/** Pierwszy i ostatni dzień miesiąca grafiku w formacie YYYY-MM-DD (do pól z datą). */
export function monthRange(year: number, month: number): { min: string; max: string } {
  const last = new Date(year, month, 0).getDate();
  const mm = String(month).padStart(2, '0');
  return { min: `${year}-${mm}-01`, max: `${year}-${mm}-${String(last).padStart(2, '0')}` };
}
