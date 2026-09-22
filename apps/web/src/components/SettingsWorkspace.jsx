import React, { useEffect, useState } from 'react';
import ConnectorWorkspace from './ConnectorWorkspace.jsx';
import JobMonitor from './JobMonitor.jsx';

export function SettingsWorkspace() {
  const [alertsMuted, setAlertsMuted] = useState(() => (
    typeof window !== 'undefined' && window.localStorage.getItem('seacommons_alert_mute') === '1'
  ));

  useEffect(() => {
    window.localStorage.setItem('seacommons_alert_mute', alertsMuted ? '1' : '0');
  }, [alertsMuted]);

  return (
    <div className="panel-stack">
      <section className="panel-block">
        <p className="section-kicker">Preferences</p>
        <h3>Interface</h3>
        <p className="panel-copy">
          Personal display preferences stay in this browser. Infrastructure and connector configuration lives under System.
        </p>
        <label className="field-block" style={{ marginTop: 10 }}>
          Critical alert sound
          <select value={alertsMuted ? 'muted' : 'enabled'} onChange={(event) => setAlertsMuted(event.target.value === 'muted')}>
            <option value="enabled">Enabled</option>
            <option value="muted">Muted</option>
          </select>
        </label>
      </section>

      <section className="panel-block">
        <p className="section-kicker">Reference</p>
        <h3>Documentation</h3>
        <p className="panel-copy">
          SeaCommons documentation is maintained with the SeaCommons codebase. API reference remains generated from the deployed OpenAPI contract.
        </p>
        <div className="action-row">
          <a className="link-button" href="https://seacommons.org/docs" target="_blank" rel="noopener noreferrer">Docs ↗</a>
          <a className="link-button" href="https://api.seacommons.org/docs" target="_blank" rel="noopener noreferrer">API ↗</a>
          <a className="link-button" href="https://github.com/suezcanalxyz/seacommons" target="_blank" rel="noopener noreferrer">GitHub ↗</a>
        </div>
      </section>

      <section className="panel-block">
        <p className="section-kicker">Semantics</p>
        <h3>What the map is showing</h3>
        <ul className="service-list">
          <li><div><strong>Category</strong><span>Determines incident type and primary colour.</span></div></li>
          <li><div><strong>Lifecycle</strong><span>Active, needs review, resolved or archived; secondary styling only.</span></div></li>
          <li><div><strong>Evidence stage</strong><span>Observed, multi-indicator or independently corroborated.</span></div></li>
        </ul>
      </section>
    </div>
  );
}

export function SystemWorkspace({
  apiBase,
  setApiBase,
  fetchJson,
  localSettings,
  updateSetting,
  serviceRows,
}) {
  return (
    <div className="panel-stack">
      <ConnectorWorkspace apiBase={apiBase} fetchJson={fetchJson} />
      <JobMonitor apiBase={apiBase} fetchJson={fetchJson} />

      <section className="panel-block">
        <p className="section-kicker">Connectivity</p>
        <h3>API endpoint</h3>
        <label className="field-block">
          API base
          <input value={apiBase} onChange={(event) => setApiBase(event.target.value)} placeholder="http://127.0.0.1:8000" />
        </label>
        <div className="action-row" style={{ marginTop: 8 }}>
          <a className="link-button" href={`${apiBase}/docs`} target="_blank" rel="noopener noreferrer">API docs ↗</a>
          <a className="link-button" href={`${apiBase}/redoc`} target="_blank" rel="noopener noreferrer">ReDoc ↗</a>
        </div>
      </section>

      <section className="panel-block">
        <p className="section-kicker">TimeZero bridge</p>
        <h3>Chart plotter</h3>
        <label className="field-block">
          Enabled
          <select value={String(localSettings.timezeroEnabled)} onChange={(event) => updateSetting('timezeroEnabled', event.target.value)}>
            <option value="false">Disabled</option>
            <option value="true">Enabled</option>
          </select>
        </label>
        <label className="field-block" style={{ marginTop: 7 }}>
          Host
          <input value={localSettings.timezeroHost} onChange={(event) => updateSetting('timezeroHost', event.target.value)} />
        </label>
        <label className="field-block" style={{ marginTop: 7 }}>
          Port
          <input value={localSettings.timezeroPort} onChange={(event) => updateSetting('timezeroPort', event.target.value)} />
        </label>
      </section>

      <section className="panel-block">
        <p className="section-kicker">Service matrix</p>
        <h3>Runtime</h3>
        <ul className="service-list">
          {serviceRows.map((service) => (
            <li key={service.name}>
              <div>
                <strong>{service.name}</strong>
                <span>{service.detail}</span>
              </div>
              <span>{service.state}</span>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
