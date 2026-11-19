export class Store extends EventTarget {
  constructor() {
    super();
    this.sessions = new Map();
    this.events = new Map();
    this.seenSeq = new Map();
    this.resetEpochs = new Map();
    this.activeSessionId = null;
  }

  setSessions(sessionsList) {
    for (const s of sessionsList) {
      this.sessions.set(s.session_id, s);
      if (!this.resetEpochs.has(s.session_id)) {
        this.resetEpochs.set(s.session_id, s.reset_epoch || 0);
      }
    }
    if (!this.activeSessionId && sessionsList.length > 0) {
      this.activeSessionId = sessionsList[0].session_id;
    }
    this.dispatchEvent(new CustomEvent('sessions-updated'));
    this.dispatchEvent(new CustomEvent('active-session-changed'));
  }

  setActiveSession(sessionId) {
    if (this.activeSessionId !== sessionId) {
      this.activeSessionId = sessionId;
      this.dispatchEvent(new CustomEvent('active-session-changed'));
    }
  }

  getActiveSession() {
    return this.activeSessionId ? this.sessions.get(this.activeSessionId) : null;
  }

  getActiveEvents() {
    if (!this.activeSessionId) return [];
    return this.events.get(this.activeSessionId) || [];
  }

  appendFetchedEvents(sessionId, fetchedEvents) {
    if (!this.seenSeq.has(sessionId)) {
      this.seenSeq.set(sessionId, new Set());
    }
    const seen = this.seenSeq.get(sessionId);

    if (!this.events.has(sessionId)) {
      this.events.set(sessionId, []);
    }
    const list = this.events.get(sessionId);

    for (const ev of fetchedEvents) {
      const seq = ev.sequence_num || 0;
      if (seq > 0 && !seen.has(seq)) {
        seen.add(seq);
        list.push(ev);
      }
    }

    if (this.activeSessionId === sessionId) {
      this.dispatchEvent(new CustomEvent('transcript-reset'));
    }
  }

  handleEvent(event) {
    const sid = event.session_id;
    const seq = event.sequence_num || 0;

    // Sequence 0: metadata / state / reset routing
    if (seq === 0) {
      if (event.event_type === 'SESSION_INIT') {
        const s = event.payload;
        this.sessions.set(sid, s);
        if (!this.resetEpochs.has(sid)) {
          this.resetEpochs.set(sid, s.reset_epoch || 0);
        }
        if (!this.activeSessionId) {
          this.activeSessionId = sid;
          this.dispatchEvent(new CustomEvent('active-session-changed'));
        }
        this.dispatchEvent(new CustomEvent('sessions-updated'));
        return;
      }

      if (event.event_type === 'SESSION_RESET') {
        const epoch = event.payload.reset_epoch || 0;
        this.resetEpochs.set(sid, epoch);
        this.events.set(sid, []);
        this.seenSeq.set(sid, new Set());
        const s = this.sessions.get(sid);
        if (s) {
          s.reset_epoch = epoch;
          s.status = 'INITIALIZING';
          s.tokens = { input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, total_tokens: 0 };
        }
        this.dispatchEvent(new CustomEvent('sessions-updated'));
        if (this.activeSessionId === sid) {
          this.dispatchEvent(new CustomEvent('transcript-reset'));
        }
        return;
      }

      if (event.event_type === 'STATE_CHANGE') {
        const s = this.sessions.get(sid);
        if (s) {
          Object.assign(s, event.payload);
        } else {
          this.sessions.set(sid, event.payload);
        }
        this.dispatchEvent(new CustomEvent('sessions-updated'));
        if (this.activeSessionId === sid) {
          this.dispatchEvent(new CustomEvent('session-state-changed'));
        }
        return;
      }
    }

    // Sequence > 0: stream event deduplication & appending
    if (!this.seenSeq.has(sid)) {
      this.seenSeq.set(sid, new Set());
    }
    const seen = this.seenSeq.get(sid);
    if (seen.has(seq)) {
      return; // Deduplicate
    }
    seen.add(seq);

    if (!this.events.has(sid)) {
      this.events.set(sid, []);
    }
    const list = this.events.get(sid);
    list.push(event);

    // Update session tokens if thinking delta is present
    const s = this.sessions.get(sid);
    if (s && event.event_type === 'THINKING' && event.payload) {
      const deltaTokens = event.payload.delta_tokens || {};
      const deltaIn = event.payload.input_tokens || deltaTokens.input_tokens || 0;
      const deltaOut = event.payload.output_tokens || deltaTokens.output_tokens || 0;
      const deltaCache = event.payload.cache_read_tokens || deltaTokens.cache_read_tokens || 0;
      if (deltaIn || deltaOut || deltaCache) {
        if (!s.tokens) s.tokens = { input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, total_tokens: 0 };
        s.tokens.input_tokens = (s.tokens.input_tokens || 0) + deltaIn;
        s.tokens.output_tokens = (s.tokens.output_tokens || 0) + deltaOut;
        s.tokens.cache_read_tokens = (s.tokens.cache_read_tokens || 0) + deltaCache;
        s.tokens.total_tokens = s.tokens.input_tokens + s.tokens.output_tokens;
        this.dispatchEvent(new CustomEvent('sessions-updated'));
      }
    }

    if (this.activeSessionId === sid) {
      this.dispatchEvent(new CustomEvent('event-appended', { detail: event }));
    }
  }
}

export const store = new Store();
