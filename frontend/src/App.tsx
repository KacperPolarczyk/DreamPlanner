import { useCallback, useEffect, useState } from 'react';
import ActivationForm from './ActivationForm';
import ApiStatus from './ApiStatus';
import { ApiError, getCurrentUser, logout } from './api';
import type { Role, UserProfile } from './api';
import { LoadError, Loading, Tabs } from './components';
import { ROLE_LABELS } from './format';
import LoginForm from './LoginForm';
import MyAvailabilityPage from './MyAvailabilityPage';
import MyShiftsPage from './MyShiftsPage';
import PositionsPage from './PositionsPage';
import SchedulesPage from './SchedulesPage';
import { ui } from './styles';
import UsersPage from './UsersPage';

type TabId = 'schedules' | 'positions' | 'users' | 'availability' | 'shifts';

/** Zakładki zależą od roli: admin zarządza słownikami i kontami, manager grafikami, pracownik swoimi zmianami. */
const TABS_BY_ROLE: Record<Role, { id: TabId; label: string }[]> = {
  admin: [
    { id: 'users', label: 'Użytkownicy' },
    { id: 'positions', label: 'Stanowiska' },
    { id: 'schedules', label: 'Grafiki (podgląd)' },
  ],
  manager: [
    { id: 'schedules', label: 'Grafiki' },
    { id: 'positions', label: 'Stanowiska' },
  ],
  employee: [
    { id: 'shifts', label: 'Moje zmiany' },
    { id: 'availability', label: 'Moja dyspozycyjność' },
  ],
};

function App() {
  // Token trzymamy tylko w pamięci aplikacji (po odświeżeniu strony trzeba się zalogować ponownie).
  const [token, setToken] = useState<string | null>(null);
  const [user, setUser] = useState<UserProfile | null>(null);
  const [userError, setUserError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [showActivation, setShowActivation] = useState(false);
  const [tab, setTab] = useState<TabId | null>(null);

  const handleSessionExpired = useCallback(() => {
    setToken(null);
    setUser(null);
    setNotice('Sesja wygasła. Zaloguj się ponownie.');
  }, []);

  const handleLogin = useCallback((newToken: string) => {
    setNotice(null);
    setUserError(null);
    setUser(null);
    setTab(null);
    setToken(newToken);
  }, []);

  // Po zalogowaniu pobieramy profil, żeby wiedzieć, jaką rolę ma użytkownik.
  const [profileAttempt, setProfileAttempt] = useState(0);
  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    getCurrentUser(token)
      .then((profile) => {
        if (cancelled) return;
        setUser(profile);
        setUserError(null);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 401) {
          handleSessionExpired();
          return;
        }
        setUserError(err instanceof ApiError ? err.message : 'Nie udało się pobrać profilu.');
      });
    return () => {
      cancelled = true;
    };
  }, [token, profileAttempt, handleSessionExpired]);

  function handleLogout() {
    // Wylogowanie po stronie serwera jest "best effort" - lokalnie czyścimy sesję zawsze.
    if (token) void logout(token).catch(() => undefined);
    setToken(null);
    setUser(null);
    setNotice(null);
  }

  function renderContent() {
    if (!token) {
      return showActivation ? (
        <ActivationForm
          onDone={(message) => {
            setShowActivation(false);
            setNotice(message);
          }}
          onCancel={() => setShowActivation(false)}
        />
      ) : (
        <LoginForm onLogin={handleLogin} notice={notice} onShowActivation={() => setShowActivation(true)} />
      );
    }

    if (userError) {
      return <LoadError error={userError} onRetry={() => setProfileAttempt((n) => n + 1)} />;
    }
    if (!user) return <Loading text="Wczytywanie konta..." />;

    const tabs = TABS_BY_ROLE[user.role];
    const activeTab = tab && tabs.some((t) => t.id === tab) ? tab : tabs[0].id;

    return (
      <>
        <div style={{ ...ui.header, borderBottom: '1px solid #e5e7eb', paddingBottom: 8 }}>
          <span>
            Zalogowano: <strong>{user.full_name}</strong> <span style={ui.muted}>({ROLE_LABELS[user.role]})</span>
          </span>
          <button type="button" onClick={handleLogout} style={ui.buttonSecondary}>
            Wyloguj
          </button>
        </div>

        <Tabs tabs={tabs} active={activeTab} onChange={setTab} />

        {activeTab === 'schedules' && (
          <SchedulesPage token={token} canEdit={user.role === 'manager'} onSessionExpired={handleSessionExpired} />
        )}
        {activeTab === 'positions' && (
          <PositionsPage token={token} readOnly={user.role !== 'admin'} onSessionExpired={handleSessionExpired} />
        )}
        {activeTab === 'users' && (
          <UsersPage token={token} currentUserId={user.id} onSessionExpired={handleSessionExpired} />
        )}
        {activeTab === 'availability' && <MyAvailabilityPage token={token} onSessionExpired={handleSessionExpired} />}
        {activeTab === 'shifts' && <MyShiftsPage token={token} onSessionExpired={handleSessionExpired} />}
      </>
    );
  }

  return (
    <div style={ui.page}>
      <h1 style={{ marginBottom: 4 }}>DreamPlanner</h1>
      <ApiStatus />
      {renderContent()}
    </div>
  );
}

export default App;
