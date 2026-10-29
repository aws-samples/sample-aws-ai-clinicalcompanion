/**
 * Property-Based Tests for AGUIEventHandler (Frontend Event Consumer)
 *
 * Uses fast-check to verify universal correctness properties of the
 * AG-UI event handling in the frontend.
 *
 * Run with: node tests/test_agui_properties.js
 */
'use strict';

var fc = require('fast-check');
var AGUIEventHandler = require('../lib/AGUIEventHandler');

// ── Helpers ─────────────────────────────────────────────────

var KNOWN_EVENT_TYPES = [
  'RUN_STARTED', 'RUN_FINISHED', 'RUN_ERROR',
  'STEP_STARTED', 'STEP_FINISHED',
  'TOOL_CALL_START', 'TOOL_CALL_END',
  'TEXT_MESSAGE_START', 'TEXT_MESSAGE_CONTENT', 'TEXT_MESSAGE_END',
  'REASONING_START', 'REASONING_MESSAGE_CONTENT', 'REASONING_END',
  'STATE_SNAPSHOT', 'STATE_DELTA',
];

var passed = 0;
var failed = 0;

function reportProperty(name, result) {
  if (result === true) {
    passed++;
    console.log('PASS:', name);
  } else {
    failed++;
    console.error('FAIL:', name);
    if (result && result.counterexample) {
      console.error('  Counterexample:', JSON.stringify(result.counterexample, null, 2));
    }
    if (result && result.error) {
      console.error('  Error:', result.error);
    }
  }
}

// ── Generators ──────────────────────────────────────────────

/**
 * Generate a random step name.
 */
var stepNameArb = fc.constantFrom(
  'Parse clinical query',
  'Coordinate specialized agents',
  'Retrieve patient context',
  'Detect guideline conflicts',
  'Apply precedence rules',
  'Generate reasoning narrative',
  'Synthesize final recommendation'
);

/**
 * Generate a non-empty text delta for TEXT_MESSAGE_CONTENT events.
 */
var textDeltaArb = fc.string({ minLength: 1, maxLength: 50 });

/**
 * Generate a valid STEP_STARTED event.
 */
var stepStartedArb = stepNameArb.map(function(name) {
  return { type: 'STEP_STARTED', step_name: name, timestamp: Date.now() };
});

/**
 * Generate a valid TEXT_MESSAGE_CONTENT event with a non-empty delta.
 */
var textContentArb = textDeltaArb.map(function(delta) {
  return { type: 'TEXT_MESSAGE_CONTENT', message_id: 'msg-1', delta: delta, timestamp: Date.now() };
});

/**
 * Generate a sequence of AG-UI events representing a valid run.
 * Structure: RUN_STARTED, then N STEP_STARTED events, then M TEXT_MESSAGE_CONTENT events,
 * then RUN_FINISHED.
 */
var validRunSequenceArb = fc.record({
  runId: fc.uuid(),
  threadId: fc.uuid(),
  stepNames: fc.array(stepNameArb, { minLength: 0, maxLength: 10 }),
  textDeltas: fc.array(textDeltaArb, { minLength: 0, maxLength: 20 }),
  finishStatus: fc.constantFrom('completed', 'error'),
}).map(function(params) {
  var events = [];

  // RUN_STARTED
  events.push({ type: 'RUN_STARTED', run_id: params.runId, thread_id: params.threadId, timestamp: Date.now() });

  // STEP_STARTED events
  for (var i = 0; i < params.stepNames.length; i++) {
    events.push({ type: 'STEP_STARTED', step_name: params.stepNames[i], timestamp: Date.now() });
  }

  // TEXT_MESSAGE_CONTENT events
  for (var j = 0; j < params.textDeltas.length; j++) {
    events.push({ type: 'TEXT_MESSAGE_CONTENT', message_id: 'msg-1', delta: params.textDeltas[j], timestamp: Date.now() });
  }

  // RUN_FINISHED
  events.push({ type: 'RUN_FINISHED', run_id: params.runId, status: params.finishStatus, timestamp: Date.now() });

  return { events: events, stepCount: params.stepNames.length, textDeltas: params.textDeltas, finishStatus: params.finishStatus };
});

/**
 * Generate a string that is NOT a known AG-UI event type.
 */
var unknownEventTypeArb = fc.string({ minLength: 1, maxLength: 50 }).filter(function(s) {
  return KNOWN_EVENT_TYPES.indexOf(s) === -1;
});

// ── Property 10: Frontend run state consistency ─────────────
// **Validates: Requirements 8.2**
//
// For any sequence of AG-UI events processed by the Event Consumer,
// the local run state SHALL accurately reflect:
// (a) the number of steps equals the count of STEP_STARTED events received
// (b) the text buffer equals the concatenation of all TEXT_MESSAGE_CONTENT deltas
// (c) status is "running" between RUN_STARTED and RUN_FINISHED,
//     and "completed" or "error" after RUN_FINISHED

(function testProperty10_RunStateConsistency() {
  var result = fc.check(
    fc.property(validRunSequenceArb, function(run) {
      var handler = new AGUIEventHandler();

      // Process all events
      for (var i = 0; i < run.events.length; i++) {
        handler.handleEvent(run.events[i]);

        // (c) Check status is "running" between RUN_STARTED and RUN_FINISHED
        if (i === 0) {
          // After RUN_STARTED
          if (handler.runState.status !== 'running') {
            return false;
          }
        } else if (i < run.events.length - 1) {
          // Between RUN_STARTED and RUN_FINISHED
          if (handler.runState.status !== 'running') {
            return false;
          }
        }
      }

      // (a) Step count equals number of STEP_STARTED events
      if (handler.runState.steps.length !== run.stepCount) {
        return false;
      }

      // (b) Text buffer equals concatenation of all TEXT_MESSAGE_CONTENT deltas
      var expectedText = run.textDeltas.join('');
      if (handler.runState.textBuffer !== expectedText) {
        return false;
      }

      // (c) After RUN_FINISHED, status is "completed" or "error"
      if (run.finishStatus === 'error') {
        if (handler.runState.status !== 'error') {
          return false;
        }
      } else {
        if (handler.runState.status !== 'completed') {
          return false;
        }
      }

      return true;
    }),
    { numRuns: 200 }
  );

  reportProperty('Property 10: Frontend run state consistency', result.failed ? result : true);
})();

// ── Property 11: Unknown event type resilience ──────────────
// **Validates: Requirements 8.5**
//
// For any JSON message received by the Event Consumer with a `type` field
// that does not match any known AG-UI event type, the handler SHALL not
// throw an exception and the run state SHALL remain unchanged.

(function testProperty11_UnknownEventResilience() {
  var result = fc.check(
    fc.property(
      unknownEventTypeArb,
      fc.dictionary(fc.string({ minLength: 1, maxLength: 10 }), fc.jsonValue()),
      function(unknownType, extraFields) {
        var handler = new AGUIEventHandler();

        // Set up some initial state to verify it doesn't change
        handler.handleEvent({ type: 'RUN_STARTED', run_id: 'test-run', thread_id: 'test-thread' });
        handler.handleEvent({ type: 'STEP_STARTED', step_name: 'Initial step' });
        handler.handleEvent({ type: 'TEXT_MESSAGE_CONTENT', delta: 'initial text' });

        // Capture state before unknown event
        var stateBefore = JSON.stringify(handler.runState);

        // Build the unknown event
        var unknownEvent = Object.assign({}, extraFields, { type: unknownType });

        // Should not throw
        var threw = false;
        try {
          handler.handleEvent(unknownEvent);
        } catch (e) {
          threw = true;
        }

        if (threw) {
          return false;
        }

        // State should remain unchanged
        var stateAfter = JSON.stringify(handler.runState);
        if (stateBefore !== stateAfter) {
          return false;
        }

        return true;
      }
    ),
    { numRuns: 200 }
  );

  reportProperty('Property 11: Unknown event type resilience', result.failed ? result : true);
})();

// ── Summary ─────────────────────────────────────────────────

console.log('\n' + (passed + failed) + ' property tests run: ' + passed + ' passed, ' + failed + ' failed');
if (failed > 0) {
  process.exit(1);
}
