/**
 * Property-Based Tests for Tool Badge Suppression with State Preservation
 * (Property 15)
 *
 * Verifies that TOOL_CALL_START and TOOL_CALL_END AG-UI events do NOT create
 * visible DOM elements but DO correctly update internal state tracking.
 *
 * Run with: node tests/test_tool_badge_removal.js
 */
// Feature: clinical-enhancements, Property 15: Tool badge suppression with state preservation
'use strict';

var fc = require('fast-check');
var AGUIEventHandler = require('../lib/AGUIEventHandler');

// ── Helpers ─────────────────────────────────────────────────

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
 * Generate a random tool name.
 */
var toolNameArb = fc.constantFrom(
  'retrieve_patient_context',
  'search_guidelines',
  'check_drug_interactions',
  'query_knowledge_base',
  'validate_contraindications',
  'fetch_lab_results',
  'apply_precedence_rules'
);

/**
 * Generate a unique tool call ID.
 */
var toolIdArb = fc.uuid();

/**
 * Generate an optional agent name.
 */
var agentNameArb = fc.constantFrom(
  'PatientContextAgent',
  'GlobalGuidelinesAgent',
  'LocalRegulatoryAgent',
  'SupervisorAgent',
  ''
);

/**
 * Generate a TOOL_CALL_START event.
 */
var toolCallStartArb = fc.record({
  toolId: toolIdArb,
  toolName: toolNameArb,
  agentName: agentNameArb,
}).map(function(params) {
  return {
    type: 'TOOL_CALL_START',
    tool_call_id: params.toolId,
    tool_name: params.toolName,
    agent_name: params.agentName,
    timestamp: Date.now(),
  };
});

/**
 * Generate a paired TOOL_CALL_START + TOOL_CALL_END sequence for a single tool.
 */
var toolCallPairArb = fc.record({
  toolId: toolIdArb,
  toolName: toolNameArb,
  agentName: agentNameArb,
  endStatus: fc.constantFrom('completed', 'error', 'cancelled'),
}).map(function(params) {
  return {
    start: {
      type: 'TOOL_CALL_START',
      tool_call_id: params.toolId,
      tool_name: params.toolName,
      agent_name: params.agentName,
      timestamp: Date.now(),
    },
    end: {
      type: 'TOOL_CALL_END',
      tool_call_id: params.toolId,
      status: params.endStatus,
      timestamp: Date.now(),
    },
    toolId: params.toolId,
    toolName: params.toolName,
    agentName: params.agentName,
    endStatus: params.endStatus,
  };
});

/**
 * Generate a sequence of N tool call pairs (start + end).
 */
var multipleToolCallsArb = fc.array(toolCallPairArb, { minLength: 1, maxLength: 10 });

/**
 * Generate an interleaved sequence of tool calls where multiple tools
 * can be active simultaneously.
 */
var interleavedToolCallsArb = fc.array(
  fc.record({
    toolId: toolIdArb,
    toolName: toolNameArb,
    agentName: agentNameArb,
    endStatus: fc.constantFrom('completed', 'error'),
  }),
  { minLength: 2, maxLength: 8 }
).map(function(tools) {
  // Create all starts first, then all ends (simulating parallel tool execution)
  var events = [];
  for (var i = 0; i < tools.length; i++) {
    events.push({
      type: 'TOOL_CALL_START',
      tool_call_id: tools[i].toolId,
      tool_name: tools[i].toolName,
      agent_name: tools[i].agentName,
      timestamp: Date.now(),
    });
  }
  for (var j = 0; j < tools.length; j++) {
    events.push({
      type: 'TOOL_CALL_END',
      tool_call_id: tools[j].toolId,
      status: tools[j].endStatus,
      timestamp: Date.now(),
    });
  }
  return { events: events, tools: tools };
});

// ── Property 15a: State tracking on TOOL_CALL_START ─────────
// **Validates: Requirements 6.2, 6.5**
//
// For any TOOL_CALL_START event, the handler SHALL add the tool to
// runState.toolCalls with correct id, name, agent, and 'running' status,
// without creating any DOM elements.

(function testProperty15a_ToolCallStartStateTracking() {
  var result = fc.check(
    fc.property(toolCallStartArb, function(event) {
      var handler = new AGUIEventHandler();

      // Start a run first
      handler.handleEvent({ type: 'RUN_STARTED', run_id: 'test-run', thread_id: 'test-thread' });

      // Process the TOOL_CALL_START event
      handler.handleEvent(event);

      // Verify state was updated correctly
      if (handler.runState.toolCalls.length !== 1) {
        return false;
      }

      var tracked = handler.runState.toolCalls[0];
      if (tracked.id !== event.tool_call_id) {
        return false;
      }
      if (tracked.name !== event.tool_name) {
        return false;
      }
      if (tracked.agent !== event.agent_name) {
        return false;
      }
      if (tracked.status !== 'running') {
        return false;
      }

      // Verify no DOM-related properties exist on the handler
      // (AGUIEventHandler only manages state, no document/element references)
      if (handler.element || handler.container || handler.dom) {
        return false;
      }

      return true;
    }),
    { numRuns: 200 }
  );

  reportProperty('Property 15a: TOOL_CALL_START updates state correctly (Req 6.2, 6.5)', result.failed ? result : true);
})();

// ── Property 15b: State tracking on TOOL_CALL_END ───────────
// **Validates: Requirements 6.3, 6.5**
//
// For any TOOL_CALL_END event matching a previously started tool,
// the handler SHALL update that tool's status to the end status.

(function testProperty15b_ToolCallEndStateTracking() {
  var result = fc.check(
    fc.property(toolCallPairArb, function(pair) {
      var handler = new AGUIEventHandler();

      // Start a run
      handler.handleEvent({ type: 'RUN_STARTED', run_id: 'test-run', thread_id: 'test-thread' });

      // Process start event
      handler.handleEvent(pair.start);

      // Verify tool is running
      if (handler.runState.toolCalls[0].status !== 'running') {
        return false;
      }

      // Process end event
      handler.handleEvent(pair.end);

      // Verify status was updated
      if (handler.runState.toolCalls[0].status !== pair.endStatus) {
        return false;
      }

      // Verify id and name are preserved
      if (handler.runState.toolCalls[0].id !== pair.toolId) {
        return false;
      }
      if (handler.runState.toolCalls[0].name !== pair.toolName) {
        return false;
      }

      // Verify no DOM-related properties exist
      if (handler.element || handler.container || handler.dom) {
        return false;
      }

      return true;
    }),
    { numRuns: 200 }
  );

  reportProperty('Property 15b: TOOL_CALL_END updates state correctly (Req 6.3, 6.5)', result.failed ? result : true);
})();

// ── Property 15c: Multiple tool calls tracked correctly ─────
// **Validates: Requirements 6.2, 6.3, 6.5**
//
// For any sequence of N TOOL_CALL_START events followed by their
// corresponding TOOL_CALL_END events, the handler SHALL track exactly
// N tool calls with correct final statuses.

(function testProperty15c_MultipleToolCallsTracked() {
  var result = fc.check(
    fc.property(multipleToolCallsArb, function(pairs) {
      var handler = new AGUIEventHandler();

      // Start a run
      handler.handleEvent({ type: 'RUN_STARTED', run_id: 'test-run', thread_id: 'test-thread' });

      // Process all start events
      for (var i = 0; i < pairs.length; i++) {
        handler.handleEvent(pairs[i].start);
      }

      // Verify correct count of tool calls tracked
      if (handler.runState.toolCalls.length !== pairs.length) {
        return false;
      }

      // Verify all are in 'running' status
      for (var j = 0; j < pairs.length; j++) {
        if (handler.runState.toolCalls[j].status !== 'running') {
          return false;
        }
      }

      // Process all end events
      for (var k = 0; k < pairs.length; k++) {
        handler.handleEvent(pairs[k].end);
      }

      // Verify all have correct final status
      for (var m = 0; m < pairs.length; m++) {
        var found = false;
        for (var n = 0; n < handler.runState.toolCalls.length; n++) {
          if (handler.runState.toolCalls[n].id === pairs[m].toolId) {
            if (handler.runState.toolCalls[n].status !== pairs[m].endStatus) {
              return false;
            }
            found = true;
            break;
          }
        }
        if (!found) {
          return false;
        }
      }

      // Total count should still be N
      if (handler.runState.toolCalls.length !== pairs.length) {
        return false;
      }

      return true;
    }),
    { numRuns: 200 }
  );

  reportProperty('Property 15c: Multiple tool calls tracked correctly (Req 6.2, 6.3, 6.5)', result.failed ? result : true);
})();

// ── Property 15d: Interleaved tool calls independently tracked ──
// **Validates: Requirements 6.2, 6.3, 6.5**
//
// For any interleaved sequence of TOOL_CALL_START and TOOL_CALL_END events,
// each tool's status SHALL be independently tracked.

(function testProperty15d_InterleavedToolCallsIndependent() {
  var result = fc.check(
    fc.property(interleavedToolCallsArb, function(scenario) {
      var handler = new AGUIEventHandler();

      // Start a run
      handler.handleEvent({ type: 'RUN_STARTED', run_id: 'test-run', thread_id: 'test-thread' });

      // Process all interleaved events
      for (var i = 0; i < scenario.events.length; i++) {
        handler.handleEvent(scenario.events[i]);
      }

      // After all starts and ends, verify each tool has correct final status
      for (var j = 0; j < scenario.tools.length; j++) {
        var tool = scenario.tools[j];
        var found = false;
        for (var k = 0; k < handler.runState.toolCalls.length; k++) {
          if (handler.runState.toolCalls[k].id === tool.toolId) {
            if (handler.runState.toolCalls[k].status !== tool.endStatus) {
              return false;
            }
            found = true;
            break;
          }
        }
        if (!found) {
          return false;
        }
      }

      // Total tracked tools should equal number of unique tool starts
      if (handler.runState.toolCalls.length !== scenario.tools.length) {
        return false;
      }

      return true;
    }),
    { numRuns: 200 }
  );

  reportProperty('Property 15d: Interleaved tool calls independently tracked (Req 6.2, 6.3, 6.5)', result.failed ? result : true);
})();

// ── Property 15e: No DOM manipulation in handler ────────────
// **Validates: Requirements 6.2, 6.3, 6.5**
//
// The AGUIEventHandler SHALL NOT have any DOM-related properties or methods
// that would create visible elements for tool call events. After processing
// any number of tool call events, only runState is modified.

(function testProperty15e_NoDomManipulation() {
  var result = fc.check(
    fc.property(
      fc.array(toolCallStartArb, { minLength: 1, maxLength: 15 }),
      function(events) {
        var handler = new AGUIEventHandler();

        // Start a run
        handler.handleEvent({ type: 'RUN_STARTED', run_id: 'test-run', thread_id: 'test-thread' });

        // Capture keys before tool call events
        var keysBefore = Object.keys(handler).sort().join(',');

        // Process all tool call events
        for (var i = 0; i < events.length; i++) {
          handler.handleEvent(events[i]);
        }

        // Verify no new properties were added to the handler instance
        var keysAfter = Object.keys(handler).sort().join(',');
        if (keysBefore !== keysAfter) {
          return false;
        }

        // Verify handler has no DOM-related properties
        var domProps = ['element', 'container', 'dom', 'node', 'badge', 'indicator'];
        for (var j = 0; j < domProps.length; j++) {
          if (handler[domProps[j]] !== undefined) {
            return false;
          }
        }

        // Verify state was still tracked (not silently dropped)
        if (handler.runState.toolCalls.length !== events.length) {
          return false;
        }

        return true;
      }
    ),
    { numRuns: 200 }
  );

  reportProperty('Property 15e: No DOM manipulation — only state updates (Req 6.2, 6.3, 6.5)', result.failed ? result : true);
})();

// ── Summary ─────────────────────────────────────────────────

console.log('\n' + (passed + failed) + ' property tests run: ' + passed + ' passed, ' + failed + ' failed');
if (failed > 0) {
  process.exit(1);
}
