/**
 * Unit tests for ClinicalCompassWebSocket reconnection during interrupted runs.
 *
 * Run with: node lib/ClinicalCompassWebSocket.test.js
 */
'use strict';

// ── Mock WebSocket ──────────────────────────────────────────

var mockInstances = [];

function MockWebSocket(url) {
  this.url = url;
  this.readyState = 0; // CONNECTING
  this.onopen = null;
  this.onclose = null;
  this.onerror = null;
  this.onmessage = null;
  this._closed = false;
  mockInstances.push(this);
}

MockWebSocket.CONNECTING = 0;
MockWebSocket.OPEN = 1;
MockWebSocket.CLOSING = 2;
MockWebSocket.CLOSED = 3;

MockWebSocket.prototype.send = function() {};
MockWebSocket.prototype.close = function() {
  this._closed = true;
  this.readyState = MockWebSocket.CLOSED;
};

// Simulate successful connection
MockWebSocket.prototype._simulateOpen = function() {
  this.readyState = MockWebSocket.OPEN;
  if (this.onopen) this.onopen({});
};

// Simulate connection close
MockWebSocket.prototype._simulateClose = function(code, reason) {
  this.readyState = MockWebSocket.CLOSED;
  if (this.onclose) this.onclose({ code: code || 1006, reason: reason || '' });
};

// Simulate connection error
MockWebSocket.prototype._simulateError = function() {
  if (this.onerror) this.onerror({});
};

// Simulate incoming message
MockWebSocket.prototype._simulateMessage = function(data) {
  if (this.onmessage) this.onmessage({ data: JSON.stringify(data) });
};

// Set up global mocks
global.WebSocket = MockWebSocket;

// Load modules under test
var AGUIEventHandler = require('./AGUIEventHandler');
global.AGUIEventHandler = AGUIEventHandler;
var ClinicalCompassWebSocket = require('./ClinicalCompassWebSocket');

// ── Test Utilities ──────────────────────────────────────────

var passed = 0;
var failed = 0;

function assert(condition, message) {
  if (!condition) {
    throw new Error('Assertion failed: ' + message);
  }
}

function test(name, fn) {
  mockInstances = [];
  try {
    fn();
    passed++;
    console.log('PASS:', name);
  } catch (e) {
    failed++;
    console.error('FAIL:', name);
    console.error('  ', e.message);
  }
}

// Use fake timers
var pendingTimers = [];
var originalSetTimeout = global.setTimeout;
var originalClearTimeout = global.clearTimeout;

function useFakeTimers() {
  pendingTimers = [];
  global.setTimeout = function(fn, delay) {
    var id = pendingTimers.length + 1;
    pendingTimers.push({ id: id, fn: fn, delay: delay, cleared: false });
    return id;
  };
  global.clearTimeout = function(id) {
    for (var i = 0; i < pendingTimers.length; i++) {
      if (pendingTimers[i].id === id) {
        pendingTimers[i].cleared = true;
      }
    }
  };
  global.setInterval = function() { return 999; };
  global.clearInterval = function() {};
}

function advanceTimers(ms) {
  var toRun = [];
  for (var i = 0; i < pendingTimers.length; i++) {
    if (!pendingTimers[i].cleared && pendingTimers[i].delay <= ms) {
      toRun.push(pendingTimers[i]);
      pendingTimers[i].cleared = true;
    }
  }
  for (var j = 0; j < toRun.length; j++) {
    toRun[j].fn();
  }
}

function restoreTimers() {
  global.setTimeout = originalSetTimeout;
  global.clearTimeout = originalClearTimeout;
}

// ── Tests ───────────────────────────────────────────────────

test('onclose during active run triggers _handleRunInterruption', function() {
  useFakeTimers();
  var client = new ClinicalCompassWebSocket('wss://test.example.com', { runReconnectMaxAttempts: 3 });
  var interrupted = false;
  client.onRunInterrupted = function() { interrupted = true; };

  client.connect();
  mockInstances[0]._simulateOpen();

  // Simulate a run in progress
  mockInstances[0]._simulateMessage({ type: 'RUN_STARTED', run_id: 'r1', thread_id: 't1' });
  assert(client.aguiHandler.runState.status === 'running', 'run state should be running');

  // Simulate connection drop
  mockInstances[0]._simulateClose(1006);

  assert(interrupted === true, 'onRunInterrupted should have been called');
  assert(client._isRunReconnecting === true, 'should be in run reconnecting state');
  restoreTimers();
});

test('onclose when no run is active triggers normal reconnect', function() {
  useFakeTimers();
  var client = new ClinicalCompassWebSocket('wss://test.example.com');
  var interrupted = false;
  client.onRunInterrupted = function() { interrupted = true; };

  client.connect();
  mockInstances[0]._simulateOpen();

  // No run started — close connection
  mockInstances[0]._simulateClose(1006);

  assert(interrupted === false, 'onRunInterrupted should NOT have been called');
  assert(client._isRunReconnecting === false, 'should NOT be in run reconnecting state');
  restoreTimers();
});

test('run reconnection uses exponential backoff (1s, 2s, 4s)', function() {
  useFakeTimers();
  var client = new ClinicalCompassWebSocket('wss://test.example.com', {
    runReconnectMaxAttempts: 3,
    runReconnectBaseDelay: 1000
  });
  client.onRunInterrupted = function() {};

  client.connect();
  mockInstances[0]._simulateOpen();
  mockInstances[0]._simulateMessage({ type: 'RUN_STARTED', run_id: 'r1', thread_id: 't1' });
  mockInstances[0]._simulateClose(1006);

  // First attempt should be scheduled at 1000ms
  assert(pendingTimers.length >= 1, 'should have scheduled first reconnect timer');
  assert(pendingTimers[pendingTimers.length - 1].delay === 1000, 'first delay should be 1000ms');

  // Trigger first attempt — it will create a new WebSocket that fails
  advanceTimers(1000);
  var ws2 = mockInstances[mockInstances.length - 1];
  ws2._simulateClose(1006);

  // Second attempt should be at 2000ms
  var lastTimer = pendingTimers[pendingTimers.length - 1];
  assert(lastTimer.delay === 2000, 'second delay should be 2000ms, got ' + lastTimer.delay);

  // Trigger second attempt — fails again
  advanceTimers(2000);
  var ws3 = mockInstances[mockInstances.length - 1];
  ws3._simulateClose(1006);

  // Third attempt should be at 4000ms
  lastTimer = pendingTimers[pendingTimers.length - 1];
  assert(lastTimer.delay === 4000, 'third delay should be 4000ms, got ' + lastTimer.delay);

  restoreTimers();
});

test('successful reconnection during run calls onRunReconnected', function() {
  useFakeTimers();
  var client = new ClinicalCompassWebSocket('wss://test.example.com', {
    runReconnectMaxAttempts: 3,
    runReconnectBaseDelay: 1000
  });
  var reconnected = false;
  client.onRunInterrupted = function() {};
  client.onRunReconnected = function() { reconnected = true; };

  client.connect();
  mockInstances[0]._simulateOpen();
  mockInstances[0]._simulateMessage({ type: 'RUN_STARTED', run_id: 'r1', thread_id: 't1' });
  mockInstances[0]._simulateClose(1006);

  // Trigger first reconnect attempt
  advanceTimers(1000);
  var ws2 = mockInstances[mockInstances.length - 1];

  // Simulate successful reconnection
  ws2._simulateOpen();

  assert(reconnected === true, 'onRunReconnected should have been called');
  assert(client._isRunReconnecting === false, 'should no longer be in run reconnecting state');
  assert(client.connected === true, 'should be connected');
  restoreTimers();
});

test('failed reconnection after max attempts calls onRunReconnectFailed', function() {
  useFakeTimers();
  var client = new ClinicalCompassWebSocket('wss://test.example.com', {
    runReconnectMaxAttempts: 3,
    runReconnectBaseDelay: 1000
  });
  var reconnectFailed = false;
  var failedRunState = null;
  client.onRunInterrupted = function() {};
  client.onRunReconnectFailed = function(runState) {
    reconnectFailed = true;
    failedRunState = runState;
  };

  client.connect();
  mockInstances[0]._simulateOpen();
  mockInstances[0]._simulateMessage({ type: 'RUN_STARTED', run_id: 'r1', thread_id: 't1' });
  mockInstances[0]._simulateMessage({ type: 'TEXT_MESSAGE_CONTENT', message_id: 'm1', delta: 'partial ' });
  mockInstances[0]._simulateMessage({ type: 'TEXT_MESSAGE_CONTENT', message_id: 'm1', delta: 'results' });
  mockInstances[0]._simulateClose(1006);

  // Fail all 3 attempts
  advanceTimers(1000);
  mockInstances[mockInstances.length - 1]._simulateClose(1006);
  advanceTimers(2000);
  mockInstances[mockInstances.length - 1]._simulateClose(1006);
  advanceTimers(4000);
  mockInstances[mockInstances.length - 1]._simulateClose(1006);

  assert(reconnectFailed === true, 'onRunReconnectFailed should have been called');
  assert(failedRunState !== null, 'runState should be passed to callback');
  assert(failedRunState.textBuffer === 'partial results', 'partial results should be preserved in runState');
  assert(client._isRunReconnecting === false, 'should no longer be in run reconnecting state');
  restoreTimers();
});

test('partial results are preserved in runState after disconnection', function() {
  useFakeTimers();
  var client = new ClinicalCompassWebSocket('wss://test.example.com', { runReconnectMaxAttempts: 3 });
  client.onRunInterrupted = function() {};

  client.connect();
  mockInstances[0]._simulateOpen();
  mockInstances[0]._simulateMessage({ type: 'RUN_STARTED', run_id: 'r1', thread_id: 't1' });
  mockInstances[0]._simulateMessage({ type: 'STEP_STARTED', step_name: 'Parse clinical query' });
  mockInstances[0]._simulateMessage({ type: 'STEP_FINISHED', step_name: 'Parse clinical query', status: 'completed' });
  mockInstances[0]._simulateMessage({ type: 'TEXT_MESSAGE_CONTENT', message_id: 'm1', delta: 'Based on the ' });
  mockInstances[0]._simulateMessage({ type: 'TEXT_MESSAGE_CONTENT', message_id: 'm1', delta: 'clinical evidence' });

  // Connection drops
  mockInstances[0]._simulateClose(1006);

  // Verify partial state is preserved
  var state = client.aguiHandler.runState;
  assert(state.status === 'running', 'status should still be running');
  assert(state.steps.length === 1, 'should have 1 step');
  assert(state.steps[0].name === 'Parse clinical query', 'step name preserved');
  assert(state.steps[0].status === 'completed', 'step status preserved');
  assert(state.textBuffer === 'Based on the clinical evidence', 'text buffer should contain partial results');
  restoreTimers();
});

// ── Summary ─────────────────────────────────────────────────

console.log('\n' + (passed + failed) + ' tests run: ' + passed + ' passed, ' + failed + ' failed');
if (failed > 0) {
  process.exit(1);
}
