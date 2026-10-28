import { useState } from 'react';
import type { FormEvent } from 'react';
import type { Position, ShiftRequirement, ShiftRequirementInput } from './api';
import { FieldError } from './components';
import { formatTime, toApiTime } from './format';
import { ui } from './styles';
import {
  validateDateInMonth,
  validateRequired,
  validateTimeRange,
  validateWorkers,
} from './validation';

interface RequirementFormProps {
  positions: Position[];
  month: { min: string; max: string };
  editing: ShiftRequirement | null;
  busy: boolean;
  onSubmit: (input: ShiftRequirementInput) => Promise<boolean>;
  onCancelEdit: () => void;
}

type Errors = Partial<Record<'position' | 'date' | 'time' | 'workers', string | null>>;

/** Formularz dodawania i edycji zapotrzebowania (stanowisko, dzień, godziny, liczba osób). */
export default function RequirementForm({
  positions,
  month,
  editing,
  busy,
  onSubmit,
  onCancelEdit,
}: RequirementFormProps) {
  const activePositions = positions.filter((p) => p.is_active || p.id === editing?.position_id);

  const [positionId, setPositionId] = useState(editing?.position_id ?? '');
  const [workDate, setWorkDate] = useState(editing?.work_date ?? month.min);
  const [start, setStart] = useState(editing ? formatTime(editing.start_time) : '08:00');
  const [end, setEnd] = useState(editing ? formatTime(editing.end_time) : '16:00');
  const [workers, setWorkers] = useState(String(editing?.required_workers ?? 1));
  const [note, setNote] = useState(editing?.note ?? '');
  const [errors, setErrors] = useState<Errors>({});

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const nextErrors: Errors = {
      position: validateRequired(positionId, 'Wybierz stanowisko.'),
      date: validateDateInMonth(workDate, month.min, month.max),
      time: validateTimeRange(start, end),
      workers: validateWorkers(workers),
    };
    setErrors(nextErrors);
    if (Object.values(nextErrors).some(Boolean)) return;

    const ok = await onSubmit({
      position_id: positionId,
      work_date: workDate,
      start_time: toApiTime(start),
      end_time: toApiTime(end),
      required_workers: Number(workers),
      note: note.trim() || null,
    });
    if (ok && !editing) {
      // Zostawiamy stanowisko i godziny – ułatwia dodawanie kolejnych dni z tą samą zmianą.
      setNote('');
    }
  }

  return (
    <form onSubmit={handleSubmit} noValidate style={ui.wideCard}>
      <strong>{editing ? 'Edycja zapotrzebowania' : 'Dodaj zapotrzebowanie (zmianę do obsadzenia)'}</strong>

      {positions.length > 0 && activePositions.length === 0 && (
        <div style={{ ...ui.banner, ...ui.bannerInfo }}>
          Brak aktywnych stanowisk. Administrator musi najpierw dodać stanowisko.
        </div>
      )}

      <div style={{ ...ui.formRow, marginTop: 10 }}>
        <label style={{ ...ui.formCol, flex: '2 1 180px' }}>
          <span style={ui.label}>Stanowisko</span>
          <select
            value={positionId}
            onChange={(e) => setPositionId(e.target.value)}
            disabled={busy}
            style={{ ...ui.select, ...(errors.position ? ui.inputError : {}) }}
          >
            <option value="">– wybierz –</option>
            {activePositions.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
                {!p.is_active && ' (nieaktywne)'}
              </option>
            ))}
          </select>
          <FieldError error={errors.position} />
        </label>

        <label style={ui.formCol}>
          <span style={ui.label}>Dzień</span>
          <input
            type="date"
            value={workDate}
            min={month.min}
            max={month.max}
            onChange={(e) => setWorkDate(e.target.value)}
            disabled={busy}
            style={{ ...ui.input, ...(errors.date ? ui.inputError : {}) }}
          />
          <FieldError error={errors.date} />
        </label>

        <label style={{ ...ui.formCol, minWidth: 110, flex: '1 1 110px' }}>
          <span style={ui.label}>Od</span>
          <input
            type="time"
            value={start}
            onChange={(e) => setStart(e.target.value)}
            disabled={busy}
            style={{ ...ui.input, ...(errors.time ? ui.inputError : {}) }}
          />
        </label>

        <label style={{ ...ui.formCol, minWidth: 110, flex: '1 1 110px' }}>
          <span style={ui.label}>Do</span>
          <input
            type="time"
            value={end}
            onChange={(e) => setEnd(e.target.value)}
            disabled={busy}
            style={{ ...ui.input, ...(errors.time ? ui.inputError : {}) }}
          />
        </label>

        <label style={{ ...ui.formCol, minWidth: 90, flex: '1 1 90px' }}>
          <span style={ui.label}>Liczba osób</span>
          <input
            type="number"
            min={1}
            value={workers}
            onChange={(e) => setWorkers(e.target.value)}
            disabled={busy}
            style={{ ...ui.input, ...(errors.workers ? ui.inputError : {}) }}
          />
          <FieldError error={errors.workers} />
        </label>
      </div>
      <FieldError error={errors.time} />

      <label style={{ ...ui.field, marginTop: 10 }}>
        <span style={ui.label}>Notatka (opcjonalnie)</span>
        <input
          value={note}
          onChange={(e) => setNote(e.target.value)}
          placeholder="np. zmiana poranna, dostawa towaru"
          disabled={busy}
          style={ui.input}
        />
      </label>

      <div style={ui.actions}>
        <button type="submit" disabled={busy} style={{ ...ui.button, opacity: busy ? 0.6 : 1 }}>
          {busy ? 'Zapisywanie...' : editing ? 'Zapisz zmiany' : 'Dodaj'}
        </button>
        {editing && (
          <button type="button" disabled={busy} onClick={onCancelEdit} style={ui.buttonSecondary}>
            Anuluj
          </button>
        )}
      </div>
    </form>
  );
}
