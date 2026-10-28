import type { ReactNode } from 'react';
import type { ScheduleStatus } from './api';
import { STATUS_COLORS, STATUS_LABELS } from './format';
import { ui } from './styles';

export function Loading({ text }: { text: string }) {
  return (
    <p style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
      <span className="spinner" /> {text}
    </p>
  );
}

export function Messages({ message, error }: { message: string | null; error: string | null }) {
  return (
    <>
      {message && <div style={{ ...ui.banner, ...ui.bannerSuccess }}>{message}</div>}
      {error && <div style={{ ...ui.banner, ...ui.bannerError }}>{error}</div>}
    </>
  );
}

export function LoadError({ error, onRetry }: { error: string; onRetry: () => void }) {
  return (
    <div style={{ ...ui.banner, ...ui.bannerError }}>
      {error}{' '}
      <button type="button" onClick={onRetry} style={ui.buttonSecondary}>
        Spróbuj ponownie
      </button>
    </div>
  );
}

export function Info({ children }: { children: ReactNode }) {
  return <div style={{ ...ui.banner, ...ui.bannerInfo }}>{children}</div>;
}

export function StatusBadge({ status }: { status: ScheduleStatus }) {
  return (
    <span style={{ ...ui.badge, background: STATUS_COLORS[status] }}>{STATUS_LABELS[status]}</span>
  );
}

export function FieldError({ error }: { error: string | null | undefined }) {
  return error ? <div style={ui.fieldError}>{error}</div> : null;
}

export function Tabs<T extends string>({
  tabs,
  active,
  onChange,
}: {
  tabs: { id: T; label: string }[];
  active: T;
  onChange: (id: T) => void;
}) {
  return (
    <nav style={ui.tabs}>
      {tabs.map((tab) => (
        <button
          key={tab.id}
          type="button"
          onClick={() => onChange(tab.id)}
          style={{ ...ui.tab, ...(active === tab.id ? ui.tabActive : {}) }}
        >
          {tab.label}
        </button>
      ))}
    </nav>
  );
}
