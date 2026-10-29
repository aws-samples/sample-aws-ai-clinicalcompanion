/**
 * Known AG-UI event types for detection in incoming messages.
 */
var AGUI_EVENT_TYPES = [
  'RUN_STARTED', 'RUN_FINISHED', 'RUN_ERROR',
  'STEP_STARTED', 'STEP_FINISHED',
  'TOOL_CALL_START', 'TOOL_CALL_END',
  'TEXT_MESSAGE_START', 'TEXT_MESSAGE_CONTENT', 'TEXT_MESSAGE_END',
  'REASONING_START', 'REASONING_MESSAGE_CONTENT', 'REASONING_END',
  'STATE_SNAPSHOT', 'STATE_DELTA'
];

/**
 * ClinicalCompassWebSocket — WebSocket client for ClinicalCompass
 * Handles connection, reconnection, ping/pong, and AG-UI event routing.
 */
class ClinicalCompassWebSocket {
  constructor(url, options = {}) {
    this.url = url;
    this.ws = null;
    this.connected = false;
    this.reconnectAttempts = 0;
    this.maxReconnectAttempts = options.maxReconnectAttempts || 5;
    this.reconnectDelay = options.reconnectDelay || 1000;
    this.pingIntervalMs = options.pingIntervalMs || 30000;
    this.pingTimer = null;

    // Run-interruption reconnection settings
    this.runReconnectMaxAttempts = options.runReconnectMaxAttempts || 3;
    this.runReconnectBaseDelay = options.runReconnectBaseDelay || 1000;
    this._runReconnectAttempts = 0;
    this._runReconnectTimer = null;
    this._isRunReconnecting = false;

    // Callbacks (set by app.js)
    this.onConnectionChange = null;
    this.onError = null;
    this.onRunInterrupted = null;
    this.onRunReconnected = null;
    this.onRunReconnectFailed = null;

    // AG-UI event handler
    this.aguiHandler = new AGUIEventHandler();
  }

  connect() {
    if (this.ws && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING)) {
      return;
    }

    try {
      // Append Cognito access token as query parameter for WebSocket authorizer.
      // Access tokens can be validated server-side without extra JWT libraries.
      var connectUrl = this.url;
      if (typeof auth !== 'undefined' && auth && auth.getAccessToken()) {
        var separator = connectUrl.indexOf('?') === -1 ? '?' : '&';
        connectUrl = connectUrl + separator + 'Authorization=' + encodeURIComponent(auth.getAccessToken());
      }
      this.ws = new WebSocket(connectUrl);
    } catch (e) {
      console.error('WebSocket creation failed:', e);
      this._scheduleReconnect();
      return;
    }

    this.ws.onopen = () => {
      console.log('WebSocket connected');
      this.connected = true;
      this.reconnectAttempts = 0;
      this._startPing();

      // Check if this was a reconnection during a run interruption
      if (this._isRunReconnecting) {
        this._isRunReconnecting = false;
        this._runReconnectAttempts = 0;
        clearTimeout(this._runReconnectTimer);
        this._runReconnectTimer = null;
        if (this.onRunReconnected) this.onRunReconnected(this.aguiHandler.runState);
      }

      if (this.onConnectionChange) this.onConnectionChange(true);
    };

    this.ws.onclose = (event) => {
      console.log('WebSocket closed:', event.code, event.reason);
      this.connected = false;
      this._stopPing();
      if (this.onConnectionChange) this.onConnectionChange(false);

      // Check if a run was active when connection dropped
      if (this.aguiHandler.runState.status === 'running') {
        this._handleRunInterruption();
      } else {
        this._scheduleReconnect();
      }
    };

    this.ws.onerror = (event) => {
      console.error('WebSocket error:', event);
    };

    this.ws.onmessage = (event) => {
      this._handleMessage(event.data);
    };
  }

  disconnect() {
    this._stopPing();
    this.reconnectAttempts = this.maxReconnectAttempts; // prevent reconnect
    this._isRunReconnecting = false;
    clearTimeout(this._runReconnectTimer);
    this._runReconnectTimer = null;
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
    this.connected = false;
  }

  isConnected() {
    return this.connected && this.ws && this.ws.readyState === WebSocket.OPEN;
  }

  sendQuery(query, patientId, sessionId, selectedGuidelines) {
    if (!this.isConnected()) {
      console.error('Cannot send query: not connected');
      return;
    }

    var message = {
      action: 'sendquery',
      query: query,
      patientId: patientId || null,
      sessionId: sessionId || null,
      selectedGuidelines: selectedGuidelines || [],
    };

    this.ws.send(JSON.stringify(message));
  }

  // ── Internal ──────────────────────────────────────────────

  _handleMessage(raw) {
    var data;
    try {
      data = JSON.parse(raw);
    } catch (e) {
      console.debug('Non-JSON WebSocket message received (ignored):', raw);
      return;
    }

    // Route AG-UI events to the dedicated handler
    if (data.type && AGUI_EVENT_TYPES.indexOf(data.type) !== -1) {
      this.aguiHandler.handleEvent(data);
      return;
    }

    // Non-AG-UI messages
    switch (data.type) {
      case 'error':
        if (this.onError) this.onError({ message: data.error || 'Unknown error' });
        break;

      case 'pong':
        break;

      default:
        console.debug('Unknown message type:', data.type, data);
    }
  }

  /**
   * Returns the AG-UI event handler instance so app.js can register callbacks.
   */
  getAGUIHandler() {
    return this.aguiHandler;
  }

  _startPing() {
    this._stopPing();
    this.pingTimer = setInterval(() => {
      if (this.isConnected()) {
        this.ws.send(JSON.stringify({ action: 'ping' }));
      }
    }, this.pingIntervalMs);
  }

  _stopPing() {
    if (this.pingTimer) {
      clearInterval(this.pingTimer);
      this.pingTimer = null;
    }
  }

  _scheduleReconnect() {
    if (this.reconnectAttempts >= this.maxReconnectAttempts) {
      console.log('Max reconnect attempts reached');
      return;
    }

    this.reconnectAttempts++;
    var delay = this.reconnectDelay * Math.pow(2, this.reconnectAttempts - 1);
    console.log('Reconnecting in ' + delay + 'ms (attempt ' + this.reconnectAttempts + '/' + this.maxReconnectAttempts + ')');

    setTimeout(() => {
      this.connect();
    }, delay);
  }

  /**
   * Handle WebSocket disconnection during an active run.
   * Notifies the UI and attempts reconnection with exponential backoff.
   */
  _handleRunInterruption() {
    console.log('WebSocket disconnected during active run — attempting reconnection');
    this._isRunReconnecting = true;
    this._runReconnectAttempts = 0;

    // Notify UI about the interruption
    if (this.onRunInterrupted) {
      this.onRunInterrupted(this.aguiHandler.runState);
    }

    // Start reconnection attempts
    this._scheduleRunReconnect();
  }

  /**
   * Schedule a reconnection attempt specifically for run interruption.
   * Uses exponential backoff: 1s, 2s, 4s (max 3 attempts).
   */
  _scheduleRunReconnect() {
    if (this._runReconnectAttempts >= this.runReconnectMaxAttempts) {
      console.log('Run reconnection failed after ' + this.runReconnectMaxAttempts + ' attempts');
      this._isRunReconnecting = false;
      if (this.onRunReconnectFailed) {
        this.onRunReconnectFailed(this.aguiHandler.runState);
      }
      return;
    }

    this._runReconnectAttempts++;
    var delay = this.runReconnectBaseDelay * Math.pow(2, this._runReconnectAttempts - 1);
    console.log('Run reconnect in ' + delay + 'ms (attempt ' + this._runReconnectAttempts + '/' + this.runReconnectMaxAttempts + ')');

    this._runReconnectTimer = setTimeout(() => {
      if (this._isRunReconnecting) {
        this._attemptRunReconnect();
      }
    }, delay);
  }

  /**
   * Attempt to reconnect the WebSocket during a run interruption.
   * If the connection fails, schedule the next attempt.
   */
  _attemptRunReconnect() {
    if (this.ws && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING)) {
      return;
    }

    try {
      // Append Cognito access token as query parameter for WebSocket authorizer
      var connectUrl = this.url;
      if (typeof auth !== 'undefined' && auth && auth.getAccessToken()) {
        var separator = connectUrl.indexOf('?') === -1 ? '?' : '&';
        connectUrl = connectUrl + separator + 'Authorization=' + encodeURIComponent(auth.getAccessToken());
      }
      this.ws = new WebSocket(connectUrl);
    } catch (e) {
      console.error('Run reconnect WebSocket creation failed:', e);
      this._scheduleRunReconnect();
      return;
    }

    this.ws.onopen = () => {
      console.log('WebSocket reconnected during run');
      this.connected = true;
      this._isRunReconnecting = false;
      this._runReconnectAttempts = 0;
      clearTimeout(this._runReconnectTimer);
      this._runReconnectTimer = null;
      this._startPing();
      if (this.onRunReconnected) this.onRunReconnected(this.aguiHandler.runState);
      if (this.onConnectionChange) this.onConnectionChange(true);
    };

    this.ws.onclose = () => {
      this.connected = false;
      this._stopPing();
      if (this._isRunReconnecting) {
        this._scheduleRunReconnect();
      }
    };

    this.ws.onerror = (event) => {
      console.error('Run reconnect WebSocket error:', event);
    };

    this.ws.onmessage = (event) => {
      this._handleMessage(event.data);
    };
  }
}

// Export for module usage
if (typeof module !== 'undefined' && module.exports) {
  module.exports = ClinicalCompassWebSocket;
  module.exports.AGUI_EVENT_TYPES = AGUI_EVENT_TYPES;
}
