import { useCallback, useState } from 'react';
import ApiStatus from './ApiStatus';
import LoginForm from './LoginForm';
import PositionsPage from './PositionsPage';
import { logout } from './api';
import { ui } from './styles';

function App() {
  // Token trzymamy tylko w pamięci aplikacji (po odświeżeniu strony trzeba się zalogować ponownie).
  const [token, setToken] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const handleLogin = useCallback((newToken: string) => {
    setNotice(null);
    setToken(newToken);
  }, []);

  const handleSessionExpired = useCallback(() => {
    setToken(null);
    setNotice('Sesja wygasła. Zaloguj się ponownie.');
  }, []);

  function handleLogout(currentToken: string) {
    // Wylogowanie po stronie serwera jest "best effort" - lokalnie czyścimy sesję zawsze.
    void logout(currentToken).catch(() => undefined);
    setToken(null);
    setNotice(null);
  }

  return (
    <div style={ui.page}>
      <h1>DreamPlanner</h1>
      <ApiStatus />
      {token ? (
        <PositionsPage
          token={token}
          onLogout={() => handleLogout(token)}
          onSessionExpired={handleSessionExpired}
        />
      ) : (
        <LoginForm onLogin={handleLogin} notice={notice} />
      )}
    </div>
  );
}

export default App;
