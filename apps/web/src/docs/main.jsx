import React from 'react';
import { createRoot } from 'react-dom/client';
import '../ui/ui.css';
import './docs.css';
import DocsApp from './DocsApp.jsx';

const el = document.getElementById('docs-root');
if (el) createRoot(el).render(<React.StrictMode><DocsApp /></React.StrictMode>);
