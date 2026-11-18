export function initSSE(store) {
  const eventTypes = [
    'SESSION_INIT',
    'SESSION_RESET',
    'USER_PROMPT',
    'THINKING',
    'TOOL_CALL',
    'TOOL_RESULT',
    'ASSISTANT_MESSAGE',
    'STATE_CHANGE',
    'PARSE_WARNING',
  ];

  const source = new EventSource('/api/events');

  for (const type of eventTypes) {
    source.addEventListener(type, (e) => {
      try {
        const payload = JSON.parse(e.data);
        const parts = (e.lastEventId || '').split(':');
        const sid = parts[0] || (payload && payload.session_id) || '';
        const seq = parseInt(parts[1] || '0', 10);
        store.handleEvent({
          event_type: type,
          session_id: sid,
          sequence_num: seq,
          payload: payload,
          timestamp: new Date().toISOString(),
        });
      } catch (err) {
        // Safe fallback for parse errors
      }
    });
  }

  source.onopen = async () => {
    try {
      const res = await fetch('/api/sessions');
      if (res.ok) {
        const sessions = await res.json();
        store.setSessions(sessions);
      }
    } catch (err) {
      // Network error during session fetch
    }
  };

  source.onerror = () => {
    // EventSource handles reconnection natively
  };
}
