import { useCallback, useState } from 'react';
import { listMyShifts } from './api';
import { LoadError, Loading } from './components';
import { formatDate, formatTime } from './format';
import { ui } from './styles';
import { useLoader } from './useLoader';

function todayIso(): string {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
}

interface MyShiftsPageProps {
  token: string;
  onSessionExpired: () => void;
}

/** Zmiany pracownika z opublikowanych grafików. */
export default function MyShiftsPage({ token, onSessionExpired }: MyShiftsPageProps) {
  const load = useCallback(() => listMyShifts(token), [token]);
  const { data: shifts, loading, error, reload } = useLoader(load, onSessionExpired);
  const [today] = useState(todayIso);

  return (
    <div>
      <div style={ui.header}>
        <h2>Moje zmiany</h2>
        <button type="button" onClick={reload} disabled={loading} style={ui.buttonSecondary}>
          Odśwież
        </button>
      </div>

      {loading && <Loading text="Ładowanie zmian..." />}
      {error && <LoadError error={error} onRetry={reload} />}

      {!loading && !error && shifts && shifts.length === 0 && (
        <p>Nie masz jeszcze żadnych zmian w opublikowanych grafikach.</p>
      )}

      {!loading && !error && shifts && shifts.length > 0 && (
        <table style={ui.table}>
          <thead>
            <tr>
              <th style={ui.cell}>Dzień</th>
              <th style={ui.cell}>Godziny</th>
              <th style={ui.cell}>Stanowisko</th>
            </tr>
          </thead>
          <tbody>
            {shifts.map((shift) => {
              const past = shift.work_date < today;
              return (
                <tr key={shift.assignment_id} style={past ? { color: '#9ca3af' } : undefined}>
                  <td style={ui.cell}>
                    {formatDate(shift.work_date)}
                    {shift.work_date === today && <strong style={{ color: '#2563eb' }}> • dziś</strong>}
                  </td>
                  <td style={ui.cell}>
                    {formatTime(shift.start_time)}–{formatTime(shift.end_time)}
                  </td>
                  <td style={ui.cell}>{shift.position_name}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}
