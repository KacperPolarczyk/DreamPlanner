import { useState } from 'react';
import { getAvailability } from './api';
import type { AvailabilitySubmission, AvailabilitySummary, Employee } from './api';
import { formatDate, formatDateTime, formatTime } from './format';
import { ui } from './styles';
import { useApiAction } from './useApiAction';

interface AvailabilityOverviewProps {
  token: string;
  scheduleId: string;
  submissions: AvailabilitySummary[];
  employees: Employee[];
  onSessionExpired: () => void;
}

/** Podgląd zgłoszeń dyspozycyjności dla managera: kto przesłał i w jakich godzinach może pracować. */
export default function AvailabilityOverview({
  token,
  scheduleId,
  submissions,
  employees,
  onSessionExpired,
}: AvailabilityOverviewProps) {
  const action = useApiAction(onSessionExpired);
  const [openedId, setOpenedId] = useState<string | null>(null);
  const [details, setDetails] = useState<Record<string, AvailabilitySubmission>>({});

  const nameOf = (userId: string) =>
    employees.find((e) => e.user_id === userId)?.full_name ?? `Użytkownik nr ${userId} (konto nieaktywne lub nie jest pracownikiem)`;

  // Liczymy tylko obecnych pracowników – zgłoszenie konta, które zmieniło rolę lub zostało wyłączone, nie wlicza się.
  const submittedIds = new Set(submissions.filter((s) => s.submitted_at).map((s) => s.user_id));
  const submittedCount = employees.filter((e) => submittedIds.has(e.user_id)).length;
  const missing = employees.filter((e) => !submittedIds.has(e.user_id));

  async function toggle(submission: AvailabilitySummary) {
    if (openedId === submission.id) {
      setOpenedId(null);
      return;
    }
    setOpenedId(submission.id);
    if (!details[submission.id]) {
      const loaded = await action.run(() => getAvailability(token, scheduleId, submission.id), null);
      if (loaded) setDetails((prev) => ({ ...prev, [submission.id]: loaded }));
    }
  }

  return (
    <section style={ui.wideCard}>
      <h3 style={{ marginTop: 0 }}>Dyspozycyjność pracowników</h3>
      <p style={ui.muted}>
        Przesłało: {submittedCount} z {employees.length} pracowników.
        {missing.length > 0 && <> Brak zgłoszenia: {missing.map((e) => e.full_name).join(', ')}.</>}
      </p>

      {action.error && <div style={{ ...ui.banner, ...ui.bannerError }}>{action.error}</div>}

      {submissions.length === 0 ? (
        <p>Nikt jeszcze nie zgłosił dyspozycyjności.</p>
      ) : (
        <table style={ui.table}>
          <thead>
            <tr>
              <th style={ui.cell}>Pracownik</th>
              <th style={ui.cell}>Stan</th>
              <th style={ui.cell}>Ostatnia zmiana</th>
              <th style={ui.cell}></th>
            </tr>
          </thead>
          <tbody>
            {submissions.map((submission) => (
              <SubmissionRow
                key={submission.id}
                name={nameOf(submission.user_id)}
                submission={submission}
                opened={openedId === submission.id}
                details={details[submission.id]}
                loading={action.busy && openedId === submission.id}
                onToggle={() => toggle(submission)}
              />
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function SubmissionRow({
  name,
  submission,
  opened,
  details,
  loading,
  onToggle,
}: {
  name: string;
  submission: AvailabilitySummary;
  opened: boolean;
  details: AvailabilitySubmission | undefined;
  loading: boolean;
  onToggle: () => void;
}) {
  return (
    <>
      <tr>
        <td style={ui.cell}>{name}</td>
        <td style={{ ...ui.cell, color: submission.submitted_at ? 'green' : '#b45309' }}>
          {submission.submitted_at ? 'Przesłana' : 'Robocza (nieprzesłana)'}
        </td>
        <td style={ui.cell}>{formatDateTime(submission.submitted_at ?? submission.updated_at)}</td>
        <td style={ui.cell}>
          <button type="button" onClick={onToggle} style={ui.buttonSecondary}>
            {opened ? 'Ukryj' : 'Pokaż'}
          </button>
        </td>
      </tr>
      {opened && (
        <tr>
          <td colSpan={4} style={{ ...ui.cell, background: '#fafafa' }}>
            {loading && 'Ładowanie...'}
            {details && details.entries.length === 0 && 'Brak wpisów.'}
            {details && details.entries.length > 0 && (
              <ul style={{ margin: 0, paddingLeft: 18 }}>
                {details.entries.map((entry) => (
                  <li key={entry.id ?? `${entry.work_date}-${entry.start_time}`}>
                    {formatDate(entry.work_date)}, {formatTime(entry.start_time)}–{formatTime(entry.end_time)}:{' '}
                    <strong style={{ color: entry.status === 'available' ? 'green' : '#9b1c1c' }}>
                      {entry.status === 'available' ? 'dostępny' : 'niedostępny'}
                    </strong>
                    {entry.note && <span style={ui.muted}> – {entry.note}</span>}
                  </li>
                ))}
              </ul>
            )}
          </td>
        </tr>
      )}
    </>
  );
}
