import { useCallback, useState } from 'react';
import type { FormEvent } from 'react';
import { createSchedule, deleteSchedule, listSchedules } from './api';
import type { Schedule } from './api';
import { FieldError, Info, LoadError, Loading, Messages, StatusBadge } from './components';
import { MONTHS, formatDateTime, scheduleLabel } from './format';
import ScheduleDetails from './ScheduleDetails';
import { ui } from './styles';
import { useApiAction } from './useApiAction';
import { useLoader } from './useLoader';
import { validateYear } from './validation';

function nextMonth(): Date {
  const now = new Date();
  return new Date(now.getFullYear(), now.getMonth() + 1, 1);
}

interface SchedulesPageProps {
  token: string;
  canEdit: boolean;
  onSessionExpired: () => void;
}

export default function SchedulesPage({ token, canEdit, onSessionExpired }: SchedulesPageProps) {
  const [openedId, setOpenedId] = useState<string | null>(null);

  const load = useCallback(() => listSchedules(token), [token]);
  const { data: schedules, loading, error, reload } = useLoader(load, onSessionExpired);
  const action = useApiAction(onSessionExpired);

  // Domyślnie proponujemy grafik na kolejny miesiąc.
  const [year, setYear] = useState(() => String(nextMonth().getFullYear()));
  const [month, setMonth] = useState(() => String(nextMonth().getMonth() + 1));
  const [yearError, setYearError] = useState<string | null>(null);

  if (openedId) {
    return (
      <ScheduleDetails
        token={token}
        scheduleId={openedId}
        canEdit={canEdit}
        onBack={() => {
          setOpenedId(null);
          reload();
        }}
        onSessionExpired={onSessionExpired}
      />
    );
  }

  async function handleCreate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const validationError = validateYear(year);
    setYearError(validationError);
    if (validationError) return;

    const created = await action.run(
      () => createSchedule(token, Number(year), Number(month)),
      null
    );
    if (created) {
      // Po utworzeniu od razu otwieramy grafik, żeby dodać zapotrzebowanie.
      setOpenedId(created.id);
    }
  }

  async function handleDelete(schedule: Schedule) {
    if (!window.confirm(`Usunąć szkic grafiku „${scheduleLabel(schedule)}”?`)) return;
    const result = await action.run(
      () => deleteSchedule(token, schedule.id).then(() => true),
      'Usunięto szkic grafiku.'
    );
    if (result) reload();
  }

  return (
    <div>
      <h2>Grafiki</h2>

      {canEdit ? (
        <form onSubmit={handleCreate} noValidate style={ui.wideCard}>
          <strong>Nowy grafik miesięczny</strong>
          <div style={{ ...ui.formRow, marginTop: 10 }}>
            <label style={ui.formCol}>
              <span style={ui.label}>Miesiąc</span>
              <select
                value={month}
                onChange={(e) => setMonth(e.target.value)}
                disabled={action.busy}
                style={ui.select}
              >
                {MONTHS.map((name, index) => (
                  <option key={name} value={index + 1}>
                    {name}
                  </option>
                ))}
              </select>
            </label>
            <label style={ui.formCol}>
              <span style={ui.label}>Rok</span>
              <input
                type="number"
                value={year}
                onChange={(e) => setYear(e.target.value)}
                disabled={action.busy}
                style={{ ...ui.input, ...(yearError ? ui.inputError : {}) }}
              />
              <FieldError error={yearError} />
            </label>
            <div style={{ alignSelf: 'flex-end' }}>
              <button type="submit" disabled={action.busy} style={{ ...ui.button, opacity: action.busy ? 0.6 : 1 }}>
                {action.busy ? 'Tworzenie...' : 'Utwórz grafik'}
              </button>
            </div>
          </div>
        </form>
      ) : (
        <Info>Podgląd grafików. Tworzyć i edytować grafiki może tylko konto z rolą managera.</Info>
      )}

      <Messages message={action.message} error={action.error} />

      {loading && <Loading text="Ładowanie grafików..." />}
      {error && <LoadError error={error} onRetry={reload} />}

      {!loading && !error && schedules && schedules.length === 0 && (
        <p>Nie ma jeszcze żadnego grafiku.{canEdit && ' Utwórz pierwszy powyżej.'}</p>
      )}

      {!loading && !error && schedules && schedules.length > 0 && (
        <table style={ui.table}>
          <thead>
            <tr>
              <th style={ui.cell}>Nr</th>
              <th style={ui.cell}>Okres</th>
              <th style={ui.cell}>Status</th>
              <th style={ui.cell}>Utworzony</th>
              <th style={ui.cell}>Opublikowany</th>
              <th style={ui.cell}>Akcje</th>
            </tr>
          </thead>
          <tbody>
            {schedules.map((schedule) => (
              <tr key={schedule.id}>
                <td style={ui.cell}>{schedule.id}</td>
                <td style={ui.cell}>
                  <strong>{scheduleLabel(schedule)}</strong>
                </td>
                <td style={ui.cell}>
                  <StatusBadge status={schedule.status} />
                </td>
                <td style={ui.cell}>{formatDateTime(schedule.created_at)}</td>
                <td style={ui.cell}>{formatDateTime(schedule.published_at)}</td>
                <td style={ui.cell}>
                  <div style={ui.actions}>
                    <button type="button" onClick={() => setOpenedId(schedule.id)} style={ui.buttonSecondary}>
                      Otwórz
                    </button>
                    {canEdit && schedule.status === 'draft' && (
                      <button
                        type="button"
                        disabled={action.busy}
                        onClick={() => handleDelete(schedule)}
                        style={ui.buttonDanger}
                      >
                        Usuń
                      </button>
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
