import React from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import "flag-icons/css/flag-icons.min.css";
import "./index.css";
import App from "./App";
import { SystemActionsProvider } from "./contexts/SystemActions";
import { I18nProvider } from "./i18n";
import { exposePluginSDK } from "./plugins";
import { ThemeProvider } from "./themes";
import { HERMES_BASE_PATH } from "./lib/api";

// Expose the plugin SDK before rendering so plugins loaded via <script>
// can access React, components, etc. immediately.
exposePluginSDK();

// KR-FE-A11Y-COMPLETION-FORCED-COLORS-AND-AXE-CORE-CI — dev-mode
// only @axe-core/react integration. Logs WCAG 2.1 AA violations
// to the browser console as the developer works; no production
// cost (the dynamic import is tree-shaken in `vite build`, which
// sets import.meta.env.PROD = true and import.meta.env.DEV = false,
// so the entire branch is dropped).
//
// CI guard via a standalone axe runner is deferred per §4 STOP-ASK
// (B.1): adding Playwright + chromium-download in GH Actions is
// enough new infra to warrant its own bucket. Dev-mode catches
// the majority of regressions during development; the static
// a11y grep pins in tests/test_tenants_endpoint.py catch the
// drift-pinned ones at CI time without any browser infra.
if (import.meta.env.DEV) {
  void import("@axe-core/react").then(({ default: axe }) => {
    void import("react-dom").then((ReactDOM) => {
      // 1000ms debounce — axe is heavy; this collapses rapid
      // re-render storms during dev (typing in forms, etc.).
      void axe(React, ReactDOM, 1000);
      // eslint-disable-next-line no-console
      console.info(
        "[a11y] @axe-core/react active — WCAG 2.1 AA violations will log to console",
      );
    });
  });
}

createRoot(document.getElementById("root")!).render(
  <BrowserRouter basename={HERMES_BASE_PATH || undefined}>
    <I18nProvider>
      <ThemeProvider>
        <SystemActionsProvider>
          <App />
        </SystemActionsProvider>
      </ThemeProvider>
    </I18nProvider>
  </BrowserRouter>,
);
