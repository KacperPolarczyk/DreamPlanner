import { useState } from 'react';
import type { FormEvent } from 'react';
import {
  createAvailability,
  deleteAvailability,
  getAvailability,
  listAvailability,
  updateAvailability,
} from './api';
import type { AvailabilityStatus, AvailabilitySubmission } from './api';
import { FieldError, Info, Messages } from './components';
import { formatDateTime, formatTime, toApiTime } from './format';
import { ui } from './styles';
import { useApiAction } from './useApiAction';
import { validateScheduleNumber, validateTimeRange } from './validation';

interface MyAvailabilityPageProps {
  token: string;
  onSessionExpired: () => void;
}

interface Row {
  key: number;
  work_date: string;
  start: string;
  end: string;
  status: AvailabilityStatus;
  note: string;
}

const STORAGE_KEY = 'dreamplanner.lastSchedule';

function readLastSchedule(): string {
  try {
    return localStorage.getItem(STORAGE_KEY) ?? '';
  } catch {
    return '';
  }
}

function rememberSchedule(value: string) {
  try {
    localStorage.setItem(STORAGE_KEY, value);
  } catch {
    // Brak dostępu do localStorage (np. tryb prywatny) – zapamiętanie numeru jest tylko udogodnieniem.
  }
}

let nextKey = 1;
function emptyRow(date = ''): Row {
  return { key: nextKey++, work_date: date, start: '08:00', end: '16:00', status: 'available', note: '' };
}

/** Pracownik zgłasza, w jakie dni i godziny może (lub nie może) pracować w danym grafiku. */
export default function MyAvailabilityPage({ token, onSessionExpired }: MyAvailabilityPageProps) {
  const action = useApiAction(onSessionExpired);

  const [scheduleInput, setScheduleInput] = useState(readLastSchedule);
  const [scheduleError, setScheduleError] = useState<string | null>(null);
  const [scheduleId, setScheduleId] = useState<string | null>(null);
  const [submission, setSubmission] = useState<AvailabilitySubmission | null>(null);
  const [rows, setRows] = useState<Row[]>([]);
  const [rowErrors, setRowErrors] = useState<Record<number, string>>({});

  function applySubmission(loaded: AvailabilitySubmission | null) {
    setSubmission(loaded);
    setRows(
      loaded && loaded.entries.length > 0
        ? loaded.entries.map((e) => ({
            key: nextKey++,
            work_date: e.work_date,
            start: formatTime(e.start_time),
            end: formatTime(e.end_time),
            status: e.status,
            note: e.note ?? '',
          }))
        : [emptyRow()]
    );
    setRowErrors({});
  }

  async function handleLoad(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const validationError = validateScheduleNumber(scheduleInput);
    setScheduleError(validationError);
    if (validationError) return;

    const id = scheduleInput.trim();
    const loaded = await action.run(async () => {
      const list = await listAvailability(token, id);
      return list.length > 0 ? getAvailability(token, id, list[0].id) : null;
    }, null);
    if (loaded !== undefined) {
      rememberSchedule(id);
      setScheduleId(id);
      applySubmission(loaded);
    }
  }

  function updateRow(key: number, patch: Partial<Row>) {
    setRows((prev) => prev.map((row) => (row.key === key ? { ...row, ...patch } : row)));
  }

  function validateRows(): boolean {
    const errors: Record<number, string> = {};
    for (const row of rows) {
      const message = !row.work_date ? 'Wybierz datę.' : validateTimeRange(row.start, row.end);
      if (message) errors[row.key] = message;
    }
    // Przedziały w tym samym dniu nie mogą się nakładać (mogą się stykać).
    for (const a of rows) {
      for (const b of rows) {
        if (a.key < b.key && a.work_date && a.work_date === b.work_date && a.start < b.end && b.start < a.end) {
          errors[b.key] = 'Ten przedział nakłada się na inny wpis z tego dnia.';
        }
      }
    }
    setRowErrors(errors);
    return Object.keys(errors).length === 0;
  }

  async function save(submit: boolean) {
    if (!scheduleId) return;
    if (rows.length === 0) {
      action.setError('Dodaj co najmniej jeden wpis.');
      return;
    }
    if (!validateRows()) return;
    if (submit && !window.confirm('Przesłać dyspozycyjność do managera?')) return;

    const entries = rows.map((row) => ({
      work_date: row.work_date,
      start_time: toApiTime(row.start),
      end_time: toApiTime(row.end),
      status: row.status,
      note: row.note.trim() || null,
    }));
    const saved = await action.run(
      () =>
        submission
          ? updateAvailability(token, scheduleId, submission.id, entries, submit)
          : createAvailability(token, scheduleId, entries, submit),
      submit ? 'Dyspozycyjność przesłana do managera.' : 'Zapisano wersję roboczą.'
    );
    if (saved) applySubmission(saved);
  }

  async function handleDelete() {
    if (!scheduleId || !submission) return;
    if (!window.confirm('Usunąć wersję roboczą dyspozycyjności?')) return;
    const ok = await action.run(
      () => deleteAvailability(token, scheduleId, submission.id).then(() => true),
      'Usunięto wersję roboczą.'
    );
    if (ok) applySubmission(null);
  }

  return (
    <div>
      <h2>Moja dyspozycyjność</h2>

      <form onSubmit={handleLoad} noValidate style={ui.wideCard}>
        <div style={ui.formRow}>
          <label style={{ ...ui.formCol, maxWidth: 260 }}>
            <span style={ui.label}>Numer grafiku</span>
            <input
              value={scheduleInput}
              onChange={(e) => setScheduleInput(e.target.value)}
              inputMode="numeric"
              placeholder="np. 3"
              disabled={action.busy}
              style={{ ...ui.input, ...(scheduleError ? ui.inputError : {}) }}
            />
            <FieldError error={scheduleError} />
          </label>
          <div style={{ alignSelf: 'flex-end' }}>
            <button type="submit" disabled={action.busy} style={{ ...ui.button, opacity: action.busy ? 0.6 : 1 }}>
              {action.busy && !scheduleId ? 'Wczytywanie...' : 'Wczytaj'}
            </button>
          </div>
        </div>
        <div style={{ ...ui.muted, marginTop: 8 }}>Numer grafiku podaje manager, gdy otwiera zbieranie dyspozycyjności.</div>
      </form>

      <Messages message={action.message} error={action.error} />

      {scheduleId && (
        <section style={ui.wideCard}>
          <div style={ui.header}>
            <h3 style={{ margin: 0 }}>Grafik nr {scheduleId}</h3>
            <span style={{ ...ui.muted, color: submission?.submitted_at ? 'green' : '#b45309' }}>
              {!submission && 'Nowe zgłoszenie'}
              {submission && !submission.submitted_at && 'Wersja robocza – jeszcze nie przesłana'}
              {submission?.submitted_at && `Przesłano ${formatDateTime(submission.submitted_at)}`}
            </span>
          </div>

          <Info>
            Dodaj dni i godziny, w których możesz pracować („dostępny”) albo nie możesz („niedostępny”).
            Daty muszą należeć do miesiąca grafiku. Zmiany są możliwe, dopóki manager nie zamknie zbierania.
          </Info>

          <table style={ui.table}>
            <thead>
              <tr>
                <th style={ui.cell}>Dzień</th>
                <th style={ui.cell}>Od</th>
                <th style={ui.cell}>Do</th>
                <th style={ui.cell}>Status</th>
                <th style={ui.cell}>Notatka</th>
                <th style={ui.cell}></th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.key}>
                  <td style={ui.cell}>
                    <input
                      type="date"
                      value={row.work_date}
                      onChange={(e) => updateRow(row.key, { work_date: e.target.value })}
                      disabled={action.busy}
                      style={{ ...ui.smallInput, ...(rowErrors[row.key] ? ui.inputError : {}) }}
                    />
                    <FieldError error={rowErrors[row.key]} />
                  </td>
                  <td style={ui.cell}>
                    <input
                      type="time"
                      value={row.start}
                      onChange={(e) => updateRow(row.key, { start: e.target.value })}
                      disabled={action.busy}
                      style={ui.smallInput}
                    />
                  </td>
                  <td style={ui.cell}>
                    <input
                      type="time"
                      value={row.end}
                      onChange={(e) => updateRow(row.key, { end: e.target.value })}
                      disabled={action.busy}
                      style={ui.smallInput}
                    />
                  </td>
                  <td style={ui.cell}>
                    <select
                      value={row.status}
                      onChange={(e) => updateRow(row.key, { status: e.target.value as AvailabilityStatus })}
                      disabled={action.busy}
                      style={ui.smallInput}
                    >
                      <option value="available">dostępny</option>
                      <option value="unavailable">niedostępny</option>
                    </select>
                  </td>
                  <td style={ui.cell}>
                    <input
                      value={row.note}
                      onChange={(e) => updateRow(row.key, { note: e.target.value })}
                      placeholder="opcjonalnie"
                      disabled={action.busy}
                      style={ui.smallInput}
                    />
                  </td>
                  <td style={ui.cell}>
                    <button
                      type="button"
                      disabled={action.busy}
                      onClick={() => setRows((prev) => prev.filter((r) => r.key !== row.key))}
                      style={ui.buttonDanger}
                    >
                      Usuń
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          <div style={{ ...ui.actions, marginTop: 12 }}>
            <button
              type="button"
              disabled={action.busy}
              onClick={() => setRows((prev) => [...prev, emptyRow(prev[prev.length - 1]?.work_date ?? '')])}
              style={ui.buttonSecondary}
            >
              + Dodaj wpis
            </button>
            <button type="button" disabled={action.busy} onClick={() => save(false)} style={ui.buttonSecondary}>
              Zapisz wersję roboczą
            </button>
            <button type="button" disabled={action.busy} onClick={() => save(true)} style={{ ...ui.button, opacity: action.busy ? 0.6 : 1 }}>
              {action.busy ? 'Zapisywanie...' : 'Prześlij do managera'}
            </button>
            {submission && !submission.submitted_at && (
              <button type="button" disabled={action.busy} onClick={handleDelete} style={ui.buttonDanger}>
                Usuń wersję roboczą
              </button>
            )}
          </div>
        </section>
      )}
    </div>
  );
}
