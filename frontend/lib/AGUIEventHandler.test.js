/**
 * Unit tests for AGUIEventHandler
 * Run with: node AGUIEventHandler.test.js
 */
var AGUIEventHandler = require('./AGUIEventHandler');

var passed = 0;
var failed = 0;

function assert(condition, message) {
  if (condition) {
    passed++;
  } else {
    failed++;
    console.error('FAIL:', message);
  }
}

function assertEqual(actual, expected, message) {
  if (actual === expected) {
    passed++;
  } else {
    failed++;
    console.error('FAIL:', message, '— expected:', expected, 'got:', actual);
  }
}

// ── Test: Constructor initializes run state ──────────────────

(function testConstructor() {
  var handler = new AGUIEventHandler();
  assertEqual(handler.runState.runId, null, 'runId should be null');
  assertEqual(handler.runState.threadId, null, 'threadId should be null');
  assertEqual(handler.runState.status, 'idle', 'status should be idle');
  assertEqual(handler.runState.steps.length, 0, 'steps should be empty');
  assertEqual(handler.runState.toolCalls.length, 0, 'toolCalls should be empty');
  assertEqual(handler.runState.textBuffer, '', 'textBuffer should be empty');
  assertEqual(handler.runState.reasoning, '', 'reasoning should be empty');
  assertEqual(handler.runState.sessionId, null, 'sessionId should be null');
  assertEqual(handler.runState.turnCount, 0, 'turnCount should be 0');
})();

// ── Test: on() registers callbacks ──────────────────────────

(function testOnRegistersCallbacks() {
  var handler = new AGUIEventHandler();
  var called = false;
  handler.on('RUN_STARTED', function() { called = true; });
  handler.handleEvent({ type: 'RUN_STARTED', run_id: 'r1' });
  assert(called, 'on() should register callback that gets invoked');
})();

// ── Test: handleEvent routes to correct callback ────────────

(function testHandleEventRouting() {
  var handler = new AGUIEventHandler();
  var receivedData = null;
  var receivedState = null;
  handler.on('STEP_STARTED', function(data, state) {
    receivedData = data;
    receivedState = state;
  });
  handler.handleEvent({ type: 'STEP_STARTED', step_name: 'Parse query' });
  assertEqual(receivedData.type, 'STEP_STARTED', 'callback receives event data');
  assertEqual(receivedState.status, 'idle', 'callback receives run state');
})();

// ── Test: Unknown event types don't crash ───────────────────

(function testUnknownEventType() {
  var handler = new AGUIEventHandler();
  // Should not throw
  handler.handleEvent({ type: 'TOTALLY_UNKNOWN_EVENT', foo: 'bar' });
  assertEqual(handler.runState.status, 'idle', 'state unchanged after unknown event');
})();

// ── Test: null/undefined data doesn't crash ─────────────────

(function testNullData() {
  var handler = new AGUIEventHandler();
  handler.handleEvent(null);
  handler.handleEvent(undefined);
  handler.handleEvent({});
  assertEqual(handler.runState.status, 'idle', 'state unchanged after null/undefined/empty');
})();

// ── Test: RUN_STARTED updates state ─────────────────────────

(function testRunStarted() {
  var handler = new AGUIEventHandler();
  handler.handleEvent({ type: 'RUN_STARTED', run_id: 'run-123', thread_id: 'thread-456' });
  assertEqual(handler.runState.runId, 'run-123', 'runId set from RUN_STARTED');
  assertEqual(handler.runState.threadId, 'thread-456', 'threadId set from RUN_STARTED');
  assertEqual(handler.runState.status, 'running', 'status set to running');
})();

// ── Test: RUN_FINISHED updates state ────────────────────────

(function testRunFinished() {
  var handler = new AGUIEventHandler();
  handler.handleEvent({ type: 'RUN_STARTED', run_id: 'r1' });
  handler.handleEvent({ type: 'RUN_FINISHED', status: 'completed' });
  assertEqual(handler.runState.status, 'completed', 'status set to completed');
})();

// ── Test: RUN_FINISHED with error status ────────────────────

(function testRunFinishedError() {
  var handler = new AGUIEventHandler();
  handler.handleEvent({ type: 'RUN_STARTED', run_id: 'r1' });
  handler.handleEvent({ type: 'RUN_FINISHED', status: 'error' });
  assertEqual(handler.runState.status, 'error', 'status set to error');
})();

// ── Test: STEP_STARTED pushes to steps array ────────────────

(function testStepStarted() {
  var handler = new AGUIEventHandler();
  handler.handleEvent({ type: 'STEP_STARTED', step_name: 'Parse clinical query' });
  assertEqual(handler.runState.steps.length, 1, 'one step added');
  assertEqual(handler.runState.steps[0].name, 'Parse clinical query', 'step name correct');
  assertEqual(handler.runState.steps[0].status, 'running', 'step status is running');
})();

// ── Test: STEP_FINISHED updates matching step ───────────────

(function testStepFinished() {
  var handler = new AGUIEventHandler();
  handler.handleEvent({ type: 'STEP_STARTED', step_name: 'Parse clinical query' });
  handler.handleEvent({ type: 'STEP_FINISHED', step_name: 'Parse clinical query', status: 'completed' });
  assertEqual(handler.runState.steps[0].status, 'completed', 'step status updated');
  assert(handler.runState.steps[0].finishedAt !== null, 'finishedAt set');
})();

// ── Test: TOOL_CALL_START pushes to toolCalls ───────────────

(function testToolCallStart() {
  var handler = new AGUIEventHandler();
  handler.handleEvent({ type: 'TOOL_CALL_START', tool_call_id: 'tc-1', tool_name: 'retrieve_kb', agent_name: 'patient_agent' });
  assertEqual(handler.runState.toolCalls.length, 1, 'one tool call added');
  assertEqual(handler.runState.toolCalls[0].id, 'tc-1', 'tool call id correct');
  assertEqual(handler.runState.toolCalls[0].name, 'retrieve_kb', 'tool name correct');
  assertEqual(handler.runState.toolCalls[0].agent, 'patient_agent', 'agent name correct');
  assertEqual(handler.runState.toolCalls[0].status, 'running', 'tool call status is running');
})();

// ── Test: TOOL_CALL_END updates matching tool call ──────────

(function testToolCallEnd() {
  var handler = new AGUIEventHandler();
  handler.handleEvent({ type: 'TOOL_CALL_START', tool_call_id: 'tc-1', tool_name: 'retrieve_kb' });
  handler.handleEvent({ type: 'TOOL_CALL_END', tool_call_id: 'tc-1', status: 'completed' });
  assertEqual(handler.runState.toolCalls[0].status, 'completed', 'tool call status updated');
})();

// ── Test: TEXT_MESSAGE_CONTENT appends to textBuffer ────────

(function testTextMessageContent() {
  var handler = new AGUIEventHandler();
  handler.handleEvent({ type: 'TEXT_MESSAGE_CONTENT', delta: 'Hello ' });
  handler.handleEvent({ type: 'TEXT_MESSAGE_CONTENT', delta: 'world' });
  assertEqual(handler.runState.textBuffer, 'Hello world', 'text buffer accumulates deltas');
})();

// ── Test: REASONING_MESSAGE_CONTENT appends to reasoning ────

(function testReasoningContent() {
  var handler = new AGUIEventHandler();
  handler.handleEvent({ type: 'REASONING_MESSAGE_CONTENT', delta: 'Thinking ' });
  handler.handleEvent({ type: 'REASONING_MESSAGE_CONTENT', delta: 'about this...' });
  assertEqual(handler.runState.reasoning, 'Thinking about this...', 'reasoning accumulates deltas');
})();

// ── Test: STATE_SNAPSHOT updates sessionId and turnCount ────

(function testStateSnapshot() {
  var handler = new AGUIEventHandler();
  handler.handleEvent({ type: 'STATE_SNAPSHOT', session_id: 'sess-789', turn_count: 3 });
  assertEqual(handler.runState.sessionId, 'sess-789', 'sessionId set from STATE_SNAPSHOT');
  assertEqual(handler.runState.turnCount, 3, 'turnCount set from STATE_SNAPSHOT');
})();

// ── Test: reset() clears all state ──────────────────────────

(function testReset() {
  var handler = new AGUIEventHandler();
  handler.handleEvent({ type: 'RUN_STARTED', run_id: 'r1', thread_id: 't1' });
  handler.handleEvent({ type: 'STEP_STARTED', step_name: 'Step 1' });
  handler.handleEvent({ type: 'TEXT_MESSAGE_CONTENT', delta: 'text' });
  handler.reset();
  assertEqual(handler.runState.runId, null, 'runId reset');
  assertEqual(handler.runState.status, 'idle', 'status reset');
  assertEqual(handler.runState.steps.length, 0, 'steps reset');
  assertEqual(handler.runState.textBuffer, '', 'textBuffer reset');
})();

// ── Test: Empty delta doesn't append ────────────────────────

(function testEmptyDelta() {
  var handler = new AGUIEventHandler();
  handler.handleEvent({ type: 'TEXT_MESSAGE_CONTENT', delta: '' });
  handler.handleEvent({ type: 'TEXT_MESSAGE_CONTENT' });
  assertEqual(handler.runState.textBuffer, '', 'empty/missing delta does not append');
})();

// ── Summary ─────────────────────────────────────────────────

console.log('\n' + (passed + failed) + ' tests run: ' + passed + ' passed, ' + failed + ' failed');
if (failed > 0) {
  process.exit(1);
}
