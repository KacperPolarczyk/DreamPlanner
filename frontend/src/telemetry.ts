import { ApplicationInsights } from '@microsoft/applicationinsights-web';

const connectionString = import.meta.env.VITE_APPINSIGHTS_CONNECTION_STRING;

// Bez connection stringa (np. lokalnie) telemetria jest wyłączona.
export const appInsights = connectionString
  ? new ApplicationInsights({
      config: {
        connectionString,
        enableAutoRouteTracking: true,                 // odsłony stron
        enableUnhandledPromiseRejectionTracking: true, // nieobsłużone błędy
      },
    })
  : null;

if (appInsights) {
  appInsights.loadAppInsights();
  appInsights.trackPageView({ name: 'DreamPlanner' });
}