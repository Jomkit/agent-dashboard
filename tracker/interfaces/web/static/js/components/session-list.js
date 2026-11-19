import { store } from '../store.js';

export class SessionListElement extends HTMLElement {
  connectedCallback() {
    this.listEl = document.createElement('ul');
    this.listEl.className = 'session-list';
    this.appendChild(this.listEl);

    this.onSessionsUpdated = () => this.render();
    this.onActiveChanged = () => this.updateActiveHighlight();

    store.addEventListener('sessions-updated', this.onSessionsUpdated);
    store.addEventListener('active-session-changed', this.onActiveChanged);

    this.render();
  }

  disconnectedCallback() {
    store.removeEventListener('sessions-updated', this.onSessionsUpdated);
    store.removeEventListener('active-session-changed', this.onActiveChanged);
  }

  updateActiveHighlight() {
    const activeId = store.activeSessionId;
    const cards = this.listEl.querySelectorAll('.session-card');
    cards.forEach((card) => {
      if (card.dataset.sessionId === activeId) {
        card.classList.add('active');
      } else {
        card.classList.remove('active');
      }
    });
  }

  render() {
    this.listEl.replaceChildren();
    const sessions = Array.from(store.sessions.values());
    const activeId = store.activeSessionId;

    for (const session of sessions) {
      const item = document.createElement('li');
      item.className = 'session-card';
      item.dataset.sessionId = session.session_id;

      if (session.session_id === activeId) {
        item.classList.add('active');
      }
      if (session.parent_session_id) {
        item.classList.add('subagent-badge');
      }

      item.addEventListener('click', () => {
        store.setActiveSession(session.session_id);
      });

      const metaRow = document.createElement('div');
      metaRow.className = 'session-meta';

      const idSpan = document.createElement('span');
      idSpan.className = 'session-id';
      const shortId = session.session_id.length > 8 ? session.session_id.substring(0, 8) : session.session_id;
      idSpan.textContent = shortId;
      metaRow.appendChild(idSpan);

      const statusBadge = document.createElement('span');
      const statusStr = (session.status || 'INITIALIZING').toUpperCase();
      statusBadge.className = `badge badge-${statusStr.toLowerCase()}`;
      statusBadge.textContent = statusStr;
      metaRow.appendChild(statusBadge);

      item.appendChild(metaRow);

      const detailsRow = document.createElement('div');
      detailsRow.className = 'session-details';
      detailsRow.style.fontSize = '0.75rem';
      detailsRow.style.color = 'var(--text-muted)';
      detailsRow.style.display = 'flex';
      detailsRow.style.justifyContent = 'space-between';

      const modelSpan = document.createElement('span');
      modelSpan.textContent = session.model_name || 'agent';
      detailsRow.appendChild(modelSpan);

      const tokens = session.tokens || {};
      const tokensSpan = document.createElement('span');
      tokensSpan.textContent = `↑${tokens.input_tokens || 0} ↓${tokens.output_tokens || 0}`;
      detailsRow.appendChild(tokensSpan);

      item.appendChild(detailsRow);
      this.listEl.appendChild(item);
    }
  }
}

customElements.define('session-list', SessionListElement);
