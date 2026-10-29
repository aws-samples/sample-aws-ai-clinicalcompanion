/**
 * AGUIEventHandler — Lightweight AG-UI event dispatcher for ClinicalCompass.
 * Parses incoming AG-UI protocol events, maintains local run state,
 * and routes events to registered callbacks.
 */
class AGUIEventHandler {
  constructor() {
    this.runState = {
      runId: null,
      threadId: null,
      status: 'idle',
      steps: [],
      toolCalls: [],
      textBuffer: '',
      reasoning: '',
      sessionId: null,
      turnCount: 0,
    };
    this.callbacks = Object.create(null);
  }

  /**
   * Register a callback for a specific AG-UI event type.
   * @param {string} eventType - AG-UI event type (e.g. 'RUN_STARTED')
   * @param {function} callback - Function called with (eventData, runState)
   */
  on(eventType, callback) {
    this.callbacks[eventType] = callback;
  }

  /**
   * Dispatch an incoming AG-UI event. Updates local run state and
   * invokes the registered callback if one exists.
   * Unknown event types are logged but do not throw.
   * @param {object} data - Parsed AG-UI event object with a `type` field
   */
  handleEvent(data) {
    if (!data || !data.type) {
      console.debug('AGUIEventHandler: received event with no type', data);
      return;
    }

    // Update internal run state based on event type
    this._updateRunState(data);

    // Route to registered callback
    var handler = this.callbacks[data.type];
    if (handler) {
      handler(data, this.runState);
    } else {
      console.debug('Unhandled AG-UI event:', data.type);
    }
  }

  /**
   * Reset run state to initial values (e.g. before a new run).
   */
  reset() {
    this.runState.runId = null;
    this.runState.threadId = null;
    this.runState.status = 'idle';
    this.runState.steps = [];
    this.runState.toolCalls = [];
    this.runState.textBuffer = '';
    this.runState.reasoning = '';
    this.runState.sessionId = null;
    this.runState.turnCount = 0;
  }

  // ── Internal State Management ─────────────────────────────

  _updateRunState(data) {
    switch (data.type) {
      case 'RUN_STARTED':
        this.runState.runId = data.run_id || data.runId || null;
        this.runState.threadId = data.thread_id || data.threadId || null;
        this.runState.status = 'running';
        break;

      case 'RUN_FINISHED':
        this.runState.status = (data.status === 'error') ? 'error' : 'completed';
        break;

      case 'RUN_ERROR':
        this.runState.status = 'error';
        break;

      case 'STEP_STARTED':
        this.runState.steps.push({
          name: data.step_name || data.name || '',
          status: 'running',
          startedAt: data.timestamp || Date.now(),
          finishedAt: null,
          error: null,
        });
        break;

      case 'STEP_FINISHED':
        var stepName = data.step_name || data.name || '';
        for (var i = this.runState.steps.length - 1; i >= 0; i--) {
          if (this.runState.steps[i].name === stepName && this.runState.steps[i].status === 'running') {
            this.runState.steps[i].status = data.status || 'completed';
            this.runState.steps[i].finishedAt = data.timestamp || Date.now();
            if (data.error) {
              this.runState.steps[i].error = data.error;
            }
            break;
          }
        }
        break;

      case 'TOOL_CALL_START':
        this.runState.toolCalls.push({
          id: data.tool_call_id || data.id || '',
          name: data.tool_call_name || data.tool_name || data.name || '',
          agent: data.agent_name || data.agent || '',
          status: 'running',
          startedAt: data.timestamp || Date.now(),
        });
        break;

      case 'TOOL_CALL_END':
        var toolId = data.tool_call_id || data.id || '';
        for (var j = this.runState.toolCalls.length - 1; j >= 0; j--) {
          if (this.runState.toolCalls[j].id === toolId && this.runState.toolCalls[j].status === 'running') {
            this.runState.toolCalls[j].status = data.status || 'completed';
            break;
          }
        }
        break;

      case 'TEXT_MESSAGE_CONTENT':
        if (data.delta) {
          this.runState.textBuffer += data.delta;
        }
        break;

      case 'REASONING_MESSAGE_CONTENT':
        if (data.delta) {
          this.runState.reasoning += data.delta;
        }
        break;

      case 'STATE_SNAPSHOT':
        if (data.session_id || data.sessionId) {
          this.runState.sessionId = data.session_id || data.sessionId;
        }
        if (typeof data.turn_count !== 'undefined') {
          this.runState.turnCount = data.turn_count;
        } else if (typeof data.turnCount !== 'undefined') {
          this.runState.turnCount = data.turnCount;
        }
        break;

      // Events that don't require state updates but are valid AG-UI types
      case 'TEXT_MESSAGE_START':
      case 'TEXT_MESSAGE_END':
      case 'REASONING_START':
      case 'REASONING_END':
      case 'STATE_DELTA':
        break;

      default:
        // Unknown event type — no state update needed
        break;
    }
  }
}

// Export for module usage (Node.js testing) while remaining browser-compatible
if (typeof module !== 'undefined' && module.exports) {
  module.exports = AGUIEventHandler;
}
