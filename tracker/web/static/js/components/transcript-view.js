import { store } from '../store.js';

export class TranscriptViewElement extends HTMLElement {
  connectedCallback() {
    this.headerEl = document.createElement('div');
    this.headerEl.className = 'main-header';
    this.appendChild(this.headerEl);

    this.feedEl = document.createElement('div');
    this.feedEl.className = 'transcript-feed';
    this.appendChild(this.feedEl);

    this.onActiveChanged = () => this.handleActiveChanged();
    this.onStateChanged = () => this.renderHeader();
    this.onEventAppended = (e) => this.appendEventRow(e.detail);
    this.onReset = () => this.handleReset();

    store.addEventListener('active-session-changed', this.onActiveChanged);
    store.addEventListener('session-state-changed', this.onStateChanged);
    store.addEventListener('event-appended', this.onEventAppended);
    store.addEventListener('transcript-reset', this.onReset);

    this.handleActiveChanged();
  }

  disconnectedCallback() {
    store.removeEventListener('active-session-changed', this.onActiveChanged);
    store.removeEventListener('session-state-changed', this.onStateChanged);
    store.removeEventListener('event-appended', this.onEventAppended);
    store.removeEventListener('transcript-reset', this.onReset);
  }

  renderHeader() {
    this.headerEl.replaceChildren();
    const session = store.getActiveSession();
    if (!session) {
      const emptyMsg = document.createElement('div');
      emptyMsg.textContent = 'No session selected. Select a session from the fleet list.';
      emptyMsg.style.color = 'var(--text-muted)';
      this.headerEl.appendChild(emptyMsg);
      return;
    }

    const titleRow = document.createElement('div');
    titleRow.style.display = 'flex';
    titleRow.style.justifyContent = 'space-between';
    titleRow.style.alignItems = 'center';
    titleRow.style.marginBottom = '6px';

    const titleLeft = document.createElement('div');
    titleLeft.style.display = 'flex';
    titleLeft.style.alignItems = 'center';
    titleLeft.style.gap = '8px';

    const idSpan = document.createElement('strong');
    idSpan.style.fontFamily = 'var(--font-mono)';
    idSpan.textContent = session.session_id;
    titleLeft.appendChild(idSpan);

    const statusBadge = document.createElement('span');
    const statusStr = (session.status || 'INITIALIZING').toUpperCase();
    statusBadge.className = `badge badge-${statusStr.toLowerCase()}`;
    statusBadge.textContent = statusStr;
    titleLeft.appendChild(statusBadge);

    titleRow.appendChild(titleLeft);

    const tokens = session.tokens || {};
    const tokenInfo = document.createElement('div');
    tokenInfo.style.fontFamily = 'var(--font-mono)';
    tokenInfo.style.fontSize = '0.8rem';
    tokenInfo.textContent = `Tokens: In ${tokens.input_tokens || 0} | Out ${tokens.output_tokens || 0}`;
    titleRow.appendChild(tokenInfo);

    this.headerEl.appendChild(titleRow);

    const metaRow = document.createElement('div');
    metaRow.style.fontSize = '0.8rem';
    metaRow.style.color = 'var(--text-muted)';
    metaRow.style.display = 'flex';
    metaRow.style.gap = '16px';

    const modelSpan = document.createElement('span');
    modelSpan.textContent = `Model: ${session.model_name || 'unknown'}`;
    metaRow.appendChild(modelSpan);

    if (session.working_dir) {
      const dirSpan = document.createElement('span');
      dirSpan.textContent = `Dir: ${session.working_dir}`;
      metaRow.appendChild(dirSpan);
    }

    this.headerEl.appendChild(metaRow);
  }

  async handleActiveChanged() {
    this.renderHeader();
    this.feedEl.replaceChildren();

    const activeId = store.activeSessionId;
    if (!activeId) return;

    const existingEvents = store.getActiveEvents();
    if (existingEvents.length > 0) {
      for (const ev of existingEvents) {
        this.appendEventRow(ev, false);
      }
      this.feedEl.scrollTop = this.feedEl.scrollHeight;
    } else {
      try {
        const res = await fetch(`/api/sessions/${activeId}/events?limit=500`);
        if (res.ok) {
          const events = await res.json();
          store.appendFetchedEvents(activeId, events);
        }
      } catch (err) {
        // Safe fallback
      }
    }
  }

  handleReset() {
    this.feedEl.replaceChildren();
    const existingEvents = store.getActiveEvents();
    for (const ev of existingEvents) {
      this.appendEventRow(ev, false);
    }
    this.feedEl.scrollTop = this.feedEl.scrollHeight;
  }

  appendEventRow(event, autoScroll = true) {
    const row = document.createElement('div');
    row.className = 'event-row';

    const type = event.event_type;

    if (type === 'USER_PROMPT') {
      const label = document.createElement('strong');
      label.textContent = 'User Prompt: ';
      label.style.color = 'var(--accent)';
      row.appendChild(label);

      const content = document.createElement('div');
      content.style.marginTop = '4px';
      content.textContent = (event.payload && event.payload.text) || '';
      row.appendChild(content);

    } else if (type === 'THINKING') {
      row.classList.add('event-thinking');
      const details = document.createElement('details');

      const summary = document.createElement('summary');
      const p = event.payload || {};
      const tokens = `↑${p.input_tokens || 0} ↓${p.output_tokens || 0}`;
      summary.textContent = `Thinking (${tokens})`;
      summary.style.cursor = 'pointer';
      details.appendChild(summary);

      const pre = document.createElement('pre');
      pre.style.marginTop = '6px';
      pre.style.whiteSpace = 'pre-wrap';
      pre.textContent = p.thinking || '';
      details.appendChild(pre);

      row.appendChild(details);

    } else if (type === 'TOOL_CALL') {
      row.classList.add('event-tool');
      const p = event.payload || {};
      const badge = document.createElement('span');
      badge.className = 'badge';
      badge.style.backgroundColor = 'rgba(88, 166, 255, 0.2)';
      badge.style.color = 'var(--accent)';
      badge.textContent = `Tool Call: ${p.tool_name || 'unknown'}`;
      row.appendChild(badge);

      const pre = document.createElement('pre');
      pre.style.marginTop = '6px';
      pre.style.whiteSpace = 'pre-wrap';
      pre.textContent = typeof p.args === 'string' ? p.args : JSON.stringify(p.args, null, 2);
      row.appendChild(pre);

    } else if (type === 'TOOL_RESULT') {
      const p = event.payload || {};
      const badge = document.createElement('span');
      badge.className = 'badge';
      badge.textContent = 'Tool Result';
      row.appendChild(badge);

      const pre = document.createElement('pre');
      pre.style.marginTop = '6px';
      pre.style.whiteSpace = 'pre-wrap';
      pre.textContent = typeof p.result === 'string' ? p.result : JSON.stringify(p.result, null, 2);
      row.appendChild(pre);

    } else if (type === 'ASSISTANT_MESSAGE') {
      const label = document.createElement('strong');
      label.textContent = 'Assistant: ';
      label.style.color = 'var(--success)';
      row.appendChild(label);

      const content = document.createElement('div');
      content.style.marginTop = '4px';
      content.style.whiteSpace = 'pre-wrap';
      content.textContent = (event.payload && event.payload.content) || '';
      row.appendChild(content);

    } else if (type === 'STATE_CHANGE') {
      const badge = document.createElement('span');
      badge.className = 'badge';
      badge.textContent = `State -> ${(event.payload && event.payload.status) || 'UNKNOWN'}`;
      row.appendChild(badge);

    } else if (type === 'PARSE_WARNING') {
      row.classList.add('event-error');
      const badge = document.createElement('span');
      badge.className = 'badge badge-error';
      badge.textContent = 'Warning';
      row.appendChild(badge);

      const content = document.createElement('div');
      content.style.marginTop = '4px';
      content.textContent = (event.payload && event.payload.warning) || 'Log parse warning';
      row.appendChild(content);

    } else {
      const content = document.createElement('div');
      content.textContent = `${type}: ${JSON.stringify(event.payload)}`;
      row.appendChild(content);
    }

    this.feedEl.appendChild(row);

    if (autoScroll) {
      this.feedEl.scrollTop = this.feedEl.scrollHeight;
    }
  }
}

customElements.define('transcript-view', TranscriptViewElement);
