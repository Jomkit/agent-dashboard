import { store } from './store.js';
import { initSSE } from './sse.js';
import './components/session-list.js';
import './components/transcript-view.js';

window.addEventListener('DOMContentLoaded', () => {
  initSSE(store);
});
