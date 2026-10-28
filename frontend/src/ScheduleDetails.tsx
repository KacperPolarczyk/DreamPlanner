import { useCallback, useState } from 'react';
import {
  ApiError,
  changeScheduleStatus,
  createAssignment,
  createShiftRequirement,
  deleteAssignment,
  deleteShiftRequirement,
  getSchedule,
  listAssignments,
  listAvailability,
  listEmployees,
  listPositions,
  listShiftRequirements,
  updateShiftRequirement,
} from './api';
import type {
  ScheduleStatus,
  ScheduleTransition,
  ShiftAssignment,
  ShiftRequirement,
  ShiftRequirementInput,
} from './api';
import AvailabilityOverview from './AvailabilityOverview';
import { Info, LoadError, Loading, Messages, StatusBadge } from './components';
import { STATUS_LABELS, formatDate, formatTime, monthRange, scheduleLabel } from './format';
import RequirementForm from './RequirementForm';
import { ui } from './styles';
import { CancelledError, useApiAction } from './useApiAction';
import { useLoader } from './useLoader';

interface ScheduleDetailsProps {
  token: string;
  scheduleId: string;
  canEdit: boolean;
  onBack: () => void;
  onSessionExpired: () => void;
}

const STEPS: ScheduleStatus[] = ['draft', 'collecting', 'collection_closed', 'closed', 'published'];

const NEXT_STEP: Partial<Record<ScheduleStatus, { transition: ScheduleTransition; label: string; done: string }>> = {
  draft: {
    transition: 'collection-opening',
    label: 'Otwórz zbieranie dyspozycyjności',
    done: 'Otwarto zbieranie dyspozycyjności.',
  },
  collecting: {
    transition: 'collection-closing',
    label: 'Zamknij zbieranie dyspozycyjności',
    done: 'Zamknięto zbieranie dyspozycyjności.',
  },
  collection_closed: {
    transition: 'closing',
    label: 'Przejdź do obsadzania zmian',
    done: 'Można przypisywać pracowników do zmian.',
  },
  closed: {
    transition: 'publication',
    label: 'Opublikuj grafik',
    done: 'Grafik opublikowany. Pracownicy widzą swoje zmiany.',
  },
};

const STATUS_HINTS: Record<ScheduleStatus, string> = {
  draft: 'Dodaj zapotrzebowanie: na które stanowiska, w jakie dni i godziny potrzeba ilu osób. Zapotrzebowanie można zmieniać tylko w szkicu.',
  collecting: 'Pracownicy zgłaszają teraz swoją dyspozycyjność. Przekaż im numer grafiku widoczny w nagłówku.',
  collection_closed: 'Zbieranie zamknięte. Przejrzyj zgłoszenia i przejdź do obsadzania zmian.',
  closed: 'Przypisz pracowników do zmian. Częściowa obsada jest dozwolona, ale publikacja jest nieodwracalna.',
  published: 'Grafik jest opublikowany i nie można go już zmieniać.',
};

const WARNING_LABELS = {
  unavailable: 'pracownik zgłosił niedostępność',
  unknown: 'brak zgłoszonej dostępności',
};

export default function ScheduleDetails({
  token,
  scheduleId,
  canEdit,
  onBack,
  onSessionExpired,
}: ScheduleDetailsProps) {
  const load = useCallback(async () => {
    const [schedule, requirements, assignments, positions, employees, submissions] = await Promise.all([
      getSchedule(token, scheduleId),
      listShiftRequirements(token, scheduleId),
      listAssignments(token, scheduleId),
      listPositions(token),
      listEmployees(token),
      listAvailability(token, scheduleId),
    ]);
    return { schedule, requirements, assignments, positions, employees, submissions };
  }, [token, scheduleId]);

  const { data, loading, error, reload } = useLoader(load, onSessionExpired);
  const action = useApiAction(onSessionExpired);
  const [editing, setEditing] = useState<ShiftRequirement | null>(null);
  const [picked, setPicked] = useState<Record<string, string>>({});

  if (loading && !data) return <Loading text="Ładowanie grafiku..." />;
  if (error && !data) return <LoadError error={error} onRetry={reload} />;
  if (!data) return null;

  const { schedule, requirements, assignments, positions, employees, submissions } = data;
  const status = schedule.status;
  const month = monthRange(schedule.year, schedule.month);
  const canEditRequirements = canEdit && status === 'draft';
  const canAssign = canEdit && status === 'closed';
  const next = NEXT_STEP[status];
  // Bez zapotrzebowania grafiku nie da się opublikować, a po wyjściu ze szkicu nie można go już dodać.
  const blockedByNoRequirements =
    requirements.length === 0 && (next?.transition === 'collection-opening' || next?.transition === 'publication');

  const assignmentsByRequirement = new Map<string, ShiftAssignment[]>();
  for (const assignment of assignments) {
    const list = assignmentsByRequirement.get(assignment.shift_requirement_id) ?? [];
    list.push(assignment);
    assignmentsByRequirement.set(assignment.shift_requirement_id, list);
  }
  const totalRequired = requirements.reduce((sum, r) => sum + r.required_workers, 0);
  const nameOf = (userId: string) =>
    employees.find((e) => e.user_id === userId)?.full_name ?? `Użytkownik nr ${userId} (konto nieaktywne lub nie jest pracownikiem)`;

  async function refreshAfter<T>(fn: () => Promise<T>, success: string): Promise<boolean> {
    const result = await action.run(async () => {
      await fn();
      return true;
    }, success);
    if (result) reload();
    return Boolean(result);
  }

  async function handleTransition() {
    if (!next) return;
    // Backend nie pozwala cofnąć grafiku do poprzedniego etapu, więc każde przejście potwierdzamy.
    let question = `${next.label}?\n\nTego kroku nie można cofnąć.`;
    if (next.transition === 'collection-opening') {
      question += ` Po otwarciu zbierania nie da się już dodawać ani zmieniać zapotrzebowania (${requirements.length} pozycji).`;
    }
    if (next.transition === 'publication') {
      const free = totalRequired - assignments.length;
      question = `Opublikować grafik? Tej operacji nie można cofnąć.${free > 0 ? `\n\nUwaga: ${free} miejsc pozostanie nieobsadzonych.` : ''}`;
    }
    if (!window.confirm(question)) return;
    await refreshAfter(() => changeScheduleStatus(token, scheduleId, next.transition), next.done);
  }

  async function handleRequirementSubmit(input: ShiftRequirementInput): Promise<boolean> {
    if (editing) {
      const ok = await refreshAfter(
        () => updateShiftRequirement(token, scheduleId, editing.id, input),
        'Zapisano zmiany w zapotrzebowaniu.'
      );
      if (ok) setEditing(null);
      return ok;
    }
    return refreshAfter(() => createShiftRequirement(token, scheduleId, input), 'Dodano zapotrzebowanie.');
  }

  async function handleRequirementDelete(requirement: ShiftRequirement) {
    if (!window.confirm(`Usunąć zapotrzebowanie: ${requirement.position_name}, ${formatDate(requirement.work_date)}?`)) {
      return;
    }
    if (editing?.id === requirement.id) setEditing(null);
    await refreshAfter(
      () => deleteShiftRequirement(token, scheduleId, requirement.id),
      'Usunięto zapotrzebowanie.'
    );
  }

  async function handleAssign(requirement: ShiftRequirement) {
    const userId = picked[requirement.id];
    if (!userId) {
      action.setMessage(null);
      action.setError('Wybierz pracownika z listy.');
      return;
    }
    const ok = await refreshAfter(async () => {
      try {
        return await createAssignment(token, scheduleId, {
          shift_requirement_id: requirement.id,
          user_id: userId,
          warning_acknowledged: false,
        });
      } catch (err) {
        // Backend wymaga potwierdzenia, gdy pracownik nie zgłosił dostępności w tych godzinach.
        if (err instanceof ApiError && err.problemType?.endsWith('assignment-warning-required')) {
          const confirmed = window.confirm(
            `${nameOf(userId)} nie zgłosił(a) dostępności w tych godzinach albo zgłosił(a) niedostępność.\n\nPrzypisać mimo to?`
          );
          if (!confirmed) throw new CancelledError();
          return createAssignment(token, scheduleId, {
            shift_requirement_id: requirement.id,
            user_id: userId,
            warning_acknowledged: true,
          });
        }
        throw err;
      }
    }, `Przypisano: ${nameOf(userId)}.`);
    if (ok) setPicked((prev) => ({ ...prev, [requirement.id]: '' }));
  }

  async function handleUnassign(assignment: ShiftAssignment) {
    if (!window.confirm(`Usunąć ${nameOf(assignment.user_id)} z tej zmiany?`)) return;
    await refreshAfter(
      () => deleteAssignment(token, scheduleId, assignment.id),
      'Usunięto przydział.'
    );
  }

  return (
    <div>
      <button type="button" onClick={onBack} style={{ ...ui.buttonSecondary, marginTop: 12 }}>
        ← Wszystkie grafiki
      </button>

      <div style={{ ...ui.header, marginTop: 8 }}>
        <h2 style={{ marginBottom: 4 }}>
          Grafik: {scheduleLabel(schedule)} <span style={ui.muted}>(nr {schedule.id})</span>
        </h2>
        <StatusBadge status={status} />
      </div>

      {/* Etapy cyklu życia grafiku */}
      <ol style={{ display: 'flex', gap: 6, listStyle: 'none', padding: 0, flexWrap: 'wrap', margin: '12px 0' }}>
        {STEPS.map((step, index) => {
          const current = STEPS.indexOf(status);
          const done = index < current;
          const active = index === current;
          return (
            <li
              key={step}
              style={{
                padding: '6px 10px',
                borderRadius: 4,
                fontSize: 14,
                background: active ? '#2563eb' : done ? '#dbeafe' : '#f3f4f6',
                color: active ? '#fff' : done ? '#1e40af' : '#6b7280',
                fontWeight: active ? 600 : 400,
              }}
            >
              {index + 1}. {STATUS_LABELS[step]}
            </li>
          );
        })}
      </ol>

      <Info>{STATUS_HINTS[status]}</Info>

      {canEdit && next && (
        <div style={{ marginTop: 12 }}>
          <button
            type="button"
            disabled={action.busy || blockedByNoRequirements}
            onClick={handleTransition}
            style={{
              ...ui.button,
              background: next.transition === 'publication' ? '#15803d' : ui.button.background,
              opacity: action.busy ? 0.6 : 1,
            }}
          >
            {action.busy ? 'Zapisywanie...' : `${next.label} →`}
          </button>
          {blockedByNoRequirements && (
            <span style={{ ...ui.muted, marginLeft: 10 }}>
              {next.transition === 'collection-opening'
                ? 'Najpierw dodaj zapotrzebowanie – po otwarciu zbierania nie będzie to możliwe.'
                : 'Grafik musi mieć co najmniej jedno zapotrzebowanie.'}
            </span>
          )}
        </div>
      )}

      <Messages message={action.message} error={action.error} />
      {loading && <Loading text="Odświeżanie..." />}

      {canEditRequirements && (
        <RequirementForm
          key={editing?.id ?? 'new'}
          positions={positions}
          month={month}
          editing={editing}
          busy={action.busy}
          onSubmit={handleRequirementSubmit}
          onCancelEdit={() => setEditing(null)}
        />
      )}

      <section style={ui.wideCard}>
        <div style={ui.header}>
          <h3 style={{ margin: 0 }}>Zmiany i obsada</h3>
          <span style={ui.muted}>
            Obsadzono {assignments.length} z {totalRequired} miejsc
          </span>
        </div>

        {requirements.length === 0 ? (
          <p>
            Brak zapotrzebowania w tym grafiku.
            {status !== 'draft' && status !== 'published' && (
              <span style={ui.muted}>
                {' '}Zapotrzebowanie można dodać tylko w szkicu, a etapów nie da się cofnąć, więc tego grafiku nie da się opublikować
                ani usunąć z poziomu aplikacji. Usunąć go może osoba z dostępem do bazy danych.
              </span>
            )}
          </p>
        ) : (
          <table style={ui.table}>
            <thead>
              <tr>
                <th style={ui.cell}>Dzień</th>
                <th style={ui.cell}>Godziny</th>
                <th style={ui.cell}>Stanowisko</th>
                <th style={ui.cell}>Obsada</th>
                <th style={ui.cell}>Przypisani pracownicy</th>
                {(canEditRequirements || canAssign) && <th style={ui.cell}>Akcje</th>}
              </tr>
            </thead>
            <tbody>
              {requirements.map((requirement) => {
                const assigned = assignmentsByRequirement.get(requirement.id) ?? [];
                const full = assigned.length >= requirement.required_workers;
                const available = employees.filter((e) => !assigned.some((a) => a.user_id === e.user_id));
                return (
                  <tr key={requirement.id} style={editing?.id === requirement.id ? { background: '#eff6ff' } : undefined}>
                    <td style={ui.cell}>{formatDate(requirement.work_date)}</td>
                    <td style={ui.cell}>
                      {formatTime(requirement.start_time)}–{formatTime(requirement.end_time)}
                    </td>
                    <td style={ui.cell}>
                      {requirement.position_name}
                      {!requirement.position_is_active && <span style={ui.muted}> (nieaktywne)</span>}
                      {requirement.note && <div style={ui.muted}>{requirement.note}</div>}
                    </td>
                    <td style={{ ...ui.cell, color: full ? 'green' : assigned.length ? '#b45309' : '#6b7280', fontWeight: 600 }}>
                      {assigned.length}/{requirement.required_workers}
                    </td>
                    <td style={ui.cell}>
                      <div style={ui.actions}>
                        {assigned.length === 0 && <span style={ui.muted}>–</span>}
                        {assigned.map((a) => (
                          <span
                            key={a.id}
                            style={ui.chip}
                            title={a.warning_type ? `Ostrzeżenie: ${WARNING_LABELS[a.warning_type]}` : undefined}
                          >
                            {a.warning_type && '⚠ '}
                            {nameOf(a.user_id)}
                            {canAssign ? (
                              <button
                                type="button"
                                disabled={action.busy}
                                onClick={() => handleUnassign(a)}
                                aria-label={`Usuń ${nameOf(a.user_id)}`}
                                style={{ border: 'none', background: 'none', cursor: 'pointer', color: '#9b1c1c', fontSize: 16 }}
                              >
                                ×
                              </button>
                            ) : (
                              <span style={{ width: 6 }} />
                            )}
                          </span>
                        ))}
                      </div>
                    </td>
                    {(canEditRequirements || canAssign) && (
                      <td style={ui.cell}>
                        {canEditRequirements && (
                          <div style={ui.actions}>
                            <button
                              type="button"
                              disabled={action.busy}
                              onClick={() => setEditing(requirement)}
                              style={ui.buttonSecondary}
                            >
                              Edytuj
                            </button>
                            <button
                              type="button"
                              disabled={action.busy}
                              onClick={() => handleRequirementDelete(requirement)}
                              style={ui.buttonDanger}
                            >
                              Usuń
                            </button>
                          </div>
                        )}
                        {canAssign && !full && (
                          <div style={ui.actions}>
                            <select
                              value={picked[requirement.id] ?? ''}
                              onChange={(e) => setPicked((prev) => ({ ...prev, [requirement.id]: e.target.value }))}
                              disabled={action.busy}
                              style={ui.smallInput}
                            >
                              <option value="">– pracownik –</option>
                              {available.map((e) => (
                                <option key={e.user_id} value={e.user_id}>
                                  {e.full_name}
                                </option>
                              ))}
                            </select>
                            <button
                              type="button"
                              disabled={action.busy}
                              onClick={() => handleAssign(requirement)}
                              style={ui.buttonSecondary}
                            >
                              Przypisz
                            </button>
                          </div>
                        )}
                        {canAssign && full && <span style={{ ...ui.muted, color: 'green' }}>Pełna obsada</span>}
                      </td>
                    )}
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
        {canAssign && employees.length === 0 && (
          <Info>Brak aktywnych pracowników. Administrator musi utworzyć konta z rolą „Pracownik”, a pracownicy muszą je aktywować.</Info>
        )}
      </section>

      {status !== 'draft' && (
        <AvailabilityOverview
          token={token}
          scheduleId={scheduleId}
          submissions={submissions}
          employees={employees}
          onSessionExpired={onSessionExpired}
        />
      )}
    </div>
  );
}
