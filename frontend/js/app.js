/**
 * ClinicalCompass — Chat with Authentication
 * All streaming display is handled via AG-UI events.
 */

let auth = null;
let wsClient = null;
let isProcessing = false;
let sessionId = null;

// AG-UI state
let aguiRunTimeout = null;
let currentAguiBubble = null;
let aguiProgressHideTimer = null;

// Internal tool call state tracking (no DOM rendering per Requirement 6)
let toolCallCount = 0;
let activeTools = [];

// Patient selector state
let patientList = [];
let selectedPatient = null;
let patientContext = null;
let guidelinesUsed = null;
let safetyChecks = null;
let citations = null;
let trailData = null;
let trailEventLog = [];
let highlightedIndex = -1;

// Guideline selector state
let guidelineList = [];
let selectedGuidelines = [];

const el = {};

// ── Bootstrap ───────────────────────────────────────────────

function initializeApp() {
  // Auth elements
  el.loginScreen = document.getElementById('login-screen');
  el.appContainer = document.getElementById('app-container');
  el.loginForm = document.getElementById('login-form');
  el.loginUsername = document.getElementById('login-username');
  el.loginPassword = document.getElementById('login-password');
  el.loginError = document.getElementById('login-error');
  el.loginBtn = document.getElementById('login-btn');
  el.logoutBtn = document.getElementById('logout-btn');
  el.userDisplay = document.getElementById('user-display');

  // Chat elements
  el.chatMessages = document.getElementById('chat-messages');
  el.patientId = document.getElementById('patient-id');
  el.patientSelector = document.getElementById('patient-selector');
  el.patientSelectorList = document.getElementById('patient-selector-list');
  el.patientSelectorWrapper = document.getElementById('patient-selector-wrapper');
  el.guidelineSelector = document.getElementById('guideline-selector');
  el.guidelineSelectorList = document.getElementById('guideline-selector-list');
  el.guidelineSelectorWrapper = document.getElementById('guideline-selector-wrapper');
  el.rightCitationPanel = document.getElementById('right-citation-panel');
  el.rightPanelContent = document.getElementById('right-citation-content');
  el.citationPanelToggle = document.getElementById('citation-panel-toggle');
  el.rightTrailPanel = document.getElementById('right-trail-panel');
  el.rightTrailContent = document.getElementById('right-trail-content');
  el.trailPanelToggle = document.getElementById('trail-panel-toggle');
  el.question = document.getElementById('clinical-question');
  el.formError = document.getElementById('form-error');
  el.submitBtn = document.getElementById('submit-btn');
  el.statusDot = document.getElementById('statusDot');
  el.statusText = document.getElementById('statusText');

  // Initialize auth
  auth = new ClinicalCompassAuth(config);
  auth.onAuthStateChange = handleAuthStateChange;

  // Bind login form
  el.loginForm.addEventListener('submit', handleLogin);
  el.logoutBtn.addEventListener('click', handleLogout);

  // Check if already authenticated (e.g., page refresh)
  if (auth.isAuthenticated()) {
    showApp(auth.getUser());
  } else {
    showLogin();
  }
}

// ── Auth Handlers ───────────────────────────────────────────

async function handleLogin(e) {
  e.preventDefault();
  el.loginError.textContent = '';
  el.loginBtn.disabled = true;
  el.loginBtn.textContent = 'Signing in…';

  var username = el.loginUsername.value.trim();
  var password = el.loginPassword.value;

  if (!username || !password) {
    el.loginError.textContent = 'Please enter username and password.';
    el.loginBtn.disabled = false;
    el.loginBtn.textContent = 'Sign In';
    return;
  }

  var result = await auth.signIn(username, password);

  if (result.success) {
    showApp(username);
  } else {
    el.loginError.textContent = result.error;
  }

  el.loginBtn.disabled = false;
  el.loginBtn.textContent = 'Sign In';
}

function handleLogout() {
  if (wsClient) wsClient.disconnect();
  wsClient = null;
  auth.signOut();
  showLogin();
}

function handleAuthStateChange(isAuthenticated, user) {
  if (isAuthenticated) {
    showApp(user);
  } else {
    showLogin();
  }
}

function showLogin() {
  el.loginScreen.style.display = '';
  el.appContainer.style.display = 'none';
  el.loginUsername.focus();
}

function showApp(username) {
  el.loginScreen.style.display = 'none';
  el.appContainer.style.display = '';
  el.userDisplay.textContent = username;

  // Initialize chat if not already done
  if (!wsClient) {
    initializeChat();
  }

  // Fetch patient list and restore selection
  fetchPatientList();
  restorePatientSelection();

  // Fetch guideline list and restore selection
  fetchGuidelineList();
  restoreGuidelineSelection();
}

// ── Chat Initialization ─────────────────────────────────────

function initializeChat() {
  wsClient = new ClinicalCompassWebSocket(config.websocketUrl, {
    maxReconnectAttempts: config.maxReconnectAttempts,
    reconnectDelay: config.reconnectDelay,
    pingIntervalMs: config.pingIntervalMs
  });

  wsClient.onConnectionChange = handleConnectionChange;
  wsClient.onError = handleError;
  wsClient.onRunInterrupted = handleRunInterrupted;
  wsClient.onRunReconnected = handleRunReconnected;
  wsClient.onRunReconnectFailed = handleRunReconnectFailed;

  // Register AG-UI event callbacks
  registerAGUICallbacks(wsClient.getAGUIHandler());

  // Bind chat events
  el.submitBtn.addEventListener('click', handleSubmit);
  el.question.addEventListener('keydown', function(e) {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
      e.preventDefault();
      handleSubmit();
    }
  });

  el.question.addEventListener('input', function() {
    el.question.style.height = 'auto';
    el.question.style.height = Math.min(el.question.scrollHeight, 150) + 'px';
  });

  // Bind patient selector events
  initPatientSelector();

  // Bind guideline selector events
  initGuidelineSelector();

  // Initialize right citation panel toggle and persistence
  initRightCitationPanel();

  if (config.useWebSocket) wsClient.connect();
}

// ── Chat Handlers ───────────────────────────────────────────

function handleSubmit() {
  el.formError.textContent = '';

  var patientId = el.patientId.value.trim();
  var question = el.question.value.trim();

  if (!patientId || !selectedPatient) {
    el.formError.textContent = 'Please select a patient from the dropdown.';
    el.patientSelector.focus();
    return;
  }
  if (!question) {
    el.formError.textContent = 'Please enter a clinical question.';
    el.question.focus();
    return;
  }
  if (!wsClient || !wsClient.isConnected()) {
    el.formError.textContent = 'Server is not reachable. Please wait for reconnection.';
    return;
  }

  var welcome = el.chatMessages.querySelector('.welcome-message');
  if (welcome) welcome.remove();

  addUserMessage(question);
  scrollToBottom();

  setFormDisabled(true);
  el.question.value = '';
  el.question.style.height = 'auto';

  wsClient.sendQuery(question, patientId, sessionId, getSelectedGuidelineIds());
}

function addUserMessage(text) {
  var msg = document.createElement('div');
  msg.className = 'message user';
  var bubble = document.createElement('div');
  bubble.className = 'message-bubble';
  bubble.textContent = text;
  var time = document.createElement('div');
  time.className = 'message-time';
  time.textContent = formatTime();
  msg.appendChild(bubble);
  msg.appendChild(time);
  el.chatMessages.appendChild(msg);
}

function handleError(error) {
  if (currentAguiBubble) {
    var contentEl = currentAguiBubble.querySelector('.agui-recommendation');
    if (contentEl) {
      contentEl.classList.remove('streaming-cursor');
      contentEl.classList.add('agui-recommendation--error');
      contentEl.textContent = error.message || 'An error occurred.';
    }
  }
  currentAguiBubble = null;
  setFormDisabled(false);
}

function handleConnectionChange(connected) {
  if (connected) {
    el.statusDot.classList.add('connected');
    el.statusDot.classList.remove('connecting');
    el.statusText.textContent = 'Connected';
  } else {
    el.statusDot.classList.remove('connected');
    el.statusDot.classList.add('connecting');
    el.statusText.textContent = 'Disconnected';
  }
}

function handleRunInterrupted(runState) {
  // Show "Connection lost" notification in the current AG-UI bubble
  if (currentAguiBubble) {
    var bubble = currentAguiBubble.querySelector('.message-bubble');
    if (bubble) {
      var notification = document.createElement('div');
      notification.className = 'agui-connection-lost';
      notification.textContent = 'Connection lost — attempting to reconnect…';
      bubble.appendChild(notification);
      scrollToBottom();
    }
  }
}

function handleRunReconnected(runState) {
  // Update the notification to show reconnection succeeded but stream was interrupted
  if (currentAguiBubble) {
    var notification = currentAguiBubble.querySelector('.agui-connection-lost');
    if (notification) {
      notification.className = 'agui-connection-restored';
      notification.textContent = 'Connection restored — stream was interrupted, results may be incomplete.';
    }
  }
}

function handleRunReconnectFailed(runState) {
  clearTimeout(aguiRunTimeout);

  if (currentAguiBubble) {
    // Update the notification to show reconnection failed
    var notification = currentAguiBubble.querySelector('.agui-connection-lost');
    if (notification) {
      notification.className = 'agui-connection-failed';
      notification.textContent = 'Connection lost — could not reconnect. Showing partial results.';
    }

    // Show partial results: finalize whatever text was accumulated
    var contentEl = currentAguiBubble.querySelector('.agui-recommendation');
    if (contentEl && runState.textBuffer) {
      contentEl.classList.remove('streaming-cursor');
      try {
        if (typeof marked !== 'undefined') {
          marked.setOptions({ breaks: true, gfm: true, headerIds: false, mangle: false });
          setSafeHtml(contentEl, marked.parse(runState.textBuffer));
        } else {
          contentEl.textContent = runState.textBuffer;
        }
      } catch (err) {
        contentEl.textContent = runState.textBuffer;
      }
    } else if (contentEl) {
      contentEl.classList.remove('streaming-cursor');
    }

    // Mark progress as interrupted
    var progressContainer = currentAguiBubble.querySelector('.agui-progress');
    if (progressContainer) {
      progressContainer.classList.add('agui-progress--interrupted');
      // Mark any running steps as interrupted
      var runningSteps = progressContainer.querySelectorAll('.agui-step--running');
      for (var i = 0; i < runningSteps.length; i++) {
        runningSteps[i].classList.remove('agui-step--running');
        runningSteps[i].classList.add('agui-step--interrupted');
        var icon = runningSteps[i].querySelector('.agui-step-icon');
        if (icon) icon.textContent = '⚠';
      }
    }
  }

  currentAguiBubble = null;
  // Re-enable input so user can retry
  setFormDisabled(false);
  el.question.focus();
}

function setFormDisabled(disabled) {
  isProcessing = disabled;
  el.patientSelector.disabled = disabled;
  el.guidelineSelector.disabled = disabled;
  el.question.disabled = disabled;
  el.submitBtn.disabled = disabled;
  el.submitBtn.setAttribute('aria-busy', String(disabled));
}

function scrollToBottom() {
  el.chatMessages.scrollTop = el.chatMessages.scrollHeight;
}

function formatTime() {
  return new Date().toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
}

function escapeHtml(text) {
  if (!text) return '';
  var div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

/**
 * Trusted HTML sink. This is the ONLY place in the app that assigns
 * dynamic HTML to a DOM element. All markup passed here MUST be run
 * through DOMPurify so no untrusted markup can reach the DOM.
 *
 * Centralising the sink (OWASP "safe sink" pattern) means there is a
 * single auditable control point for DOM-based XSS instead of a sanitizer
 * call scattered across every render function.
 *
 * @param {Element} element  Target element (no-op if falsy).
 * @param {string}  dirtyHtml Raw HTML string to sanitize and render.
 */
function setSafeHtml(element, dirtyHtml) {
  if (!element) return;
  var markup = String(dirtyHtml == null ? '' : dirtyHtml);
  if (typeof DOMPurify === 'undefined') {
    // Fail closed: if the sanitizer is unavailable, never inject markup.
    element.textContent = markup;
    return;
  }
  // Sanitize to a DOM fragment (never an HTML string) so no innerHTML/
  // outerHTML/document.write sink is used. DOMPurify strips all unsafe
  // markup; the returned nodes are safe to attach directly.
  var fragment = DOMPurify.sanitize(markup, { RETURN_DOM_FRAGMENT: true });
  element.replaceChildren(fragment);
}

function cleanup() {
  clearTimeout(aguiRunTimeout);
  if (wsClient) wsClient.disconnect();
}

// ── AG-UI Event Rendering ───────────────────────────────────

function registerAGUICallbacks(handler) {
  handler.on('RUN_STARTED', handleAguiRunStarted);
  handler.on('RUN_FINISHED', handleAguiRunFinished);
  handler.on('RUN_ERROR', handleAguiRunError);
  handler.on('STEP_STARTED', handleAguiStepStarted);
  handler.on('STEP_FINISHED', handleAguiStepFinished);
  handler.on('TOOL_CALL_START', handleAguiToolCallStart);
  handler.on('TOOL_CALL_END', handleAguiToolCallEnd);
  handler.on('REASONING_START', handleAguiReasoningStart);
  handler.on('REASONING_MESSAGE_CONTENT', handleAguiReasoningContent);
  handler.on('REASONING_END', handleAguiReasoningEnd);
  handler.on('TEXT_MESSAGE_START', handleAguiTextMessageStart);
  handler.on('TEXT_MESSAGE_CONTENT', handleAguiTextMessageContent);
  handler.on('TEXT_MESSAGE_END', handleAguiTextMessageEnd);
  handler.on('STATE_SNAPSHOT', handleAguiStateSnapshot);
}

function handleAguiRunStarted(data, runState) {
  // Reset trail event log for new run
  trailEventLog = [];
  appendTrailEvent('RUN_STARTED', 'Run initiated', data.run_id || data.runId || '');

  // Reset progress hide timer
  clearTimeout(aguiProgressHideTimer);
  aguiProgressHideTimer = null;

  // Create disclaimer message at the top of each run with trail ID
  var runId = data.run_id || data.runId || runState.runId || 'unknown';
  var disclaimerMsg = document.createElement('div');
  disclaimerMsg.className = 'message system disclaimer-message';
  setSafeHtml(disclaimerMsg, '<div class="disclaimer-banner">' +
    '<span class="disclaimer-icon">⚠️</span>' +
    '<span class="disclaimer-text">' +
    '<strong>Clinical Decision Support Only</strong> — Final clinical judgment remains with the treating physician. ' +
    'All interactions are recorded for audit and compliance.' +
    '<br><a href="#" class="trail-id-link"><code class="trail-id">🔍 Trail ID: ' + escapeHtml(runId) + '</code></a>' +
    '</span></div>');
  el.chatMessages.appendChild(disclaimerMsg);

  // Bind trail ID link to open the trail panel
  var trailLink = disclaimerMsg.querySelector('.trail-id-link');
  if (trailLink) {
    trailLink.addEventListener('click', function(e) {
      e.preventDefault();
      openTrailPanel();
    });
  }

  // Create a fresh AG-UI bubble for this run
  currentAguiBubble = createAguiBubble();
  el.chatMessages.appendChild(currentAguiBubble);
  scrollToBottom();

  // Reset internal tool call state for new run
  toolCallCount = 0;
  activeTools = [];

  // Disable input during run
  setFormDisabled(true);

  // Start 120s timeout
  clearTimeout(aguiRunTimeout);
  aguiRunTimeout = setTimeout(function() {
    aguiShowTimeoutWarning();
  }, 120000);
}

function handleAguiRunFinished(data, runState) {
  console.log('[AG-UI] RUN_FINISHED received:', JSON.stringify(data).substring(0, 200));
  clearTimeout(aguiRunTimeout);

  if (currentAguiBubble) {
    var contentEl = currentAguiBubble.querySelector('.agui-recommendation');
    if (contentEl) contentEl.classList.remove('streaming-cursor');

    // Mark progress container as complete
    var progressContainer = currentAguiBubble.querySelector('.agui-progress');
    if (progressContainer) progressContainer.classList.add('agui-progress--complete');
  }

  // Capture decision trail from AG-UI run state
  trailData = {
    runId: runState.runId || data.run_id || data.runId || 'unknown',
    status: data.status || 'completed',
    totalDuration: null
  };
  if (trailEventLog.length > 1) {
    var first = trailEventLog[0].timestamp;
    var last = trailEventLog[trailEventLog.length - 1].timestamp;
    trailData.totalDuration = ((last - first) / 1000).toFixed(1) + 's';
  }
  // Add final RUN_FINISHED to the log
  appendTrailEvent('RUN_FINISHED', data.status || 'completed', '');
  renderTrailPanel();

  // Route citations exclusively to the right panel
  if (data.citations) {
    console.log('[AG-UI] Citations received in RUN_FINISHED:', data.citations.length, 'citations');
    citations = data.citations;
    sessionStorage.setItem('citations', JSON.stringify(citations));
    renderRightCitationPanel();
  } else {
    console.log('[AG-UI] RUN_FINISHED has no citations field. Keys:', Object.keys(data).join(', '));
    // Fallback: extract citations from the rendered chat content
    extractCitationsFromBubble(currentAguiBubble);
  }

  currentAguiBubble = null;
  setFormDisabled(false);
  el.question.focus();
}

function handleAguiRunError(data, runState) {
  clearTimeout(aguiRunTimeout);

  if (currentAguiBubble) {
    // Find the last active step and show error there
    var steps = currentAguiBubble.querySelectorAll('.agui-step');
    var lastStep = steps.length > 0 ? steps[steps.length - 1] : null;

    var errorMsg = data.message || 'An error occurred during processing.';

    if (lastStep) {
      // Mark last step as failed
      lastStep.classList.remove('agui-step--running');
      lastStep.classList.add('agui-step--error');
      var icon = lastStep.querySelector('.agui-step-icon');
      if (icon) icon.textContent = '✗';

      // Add error message inline at the failed step
      var errorEl = document.createElement('div');
      errorEl.className = 'agui-step-error';
      errorEl.textContent = errorMsg;
      lastStep.appendChild(errorEl);
    } else {
      // No steps yet — show error in recommendation area
      var contentEl = currentAguiBubble.querySelector('.agui-recommendation');
      if (contentEl) {
        contentEl.classList.remove('streaming-cursor');
        contentEl.classList.add('agui-recommendation--error');
        contentEl.textContent = errorMsg;
      }
    }
  }

  currentAguiBubble = null;
  setFormDisabled(false);
  el.question.focus();
}

function handleAguiStepStarted(data, runState) {
  var stepName = data.step_name || data.name || 'Processing';
  appendTrailEvent('STEP_STARTED', stepName, '');

  if (!currentAguiBubble) return;

  var progressContainer = currentAguiBubble.querySelector('.agui-progress');
  if (!progressContainer) return;

  var stepName = data.step_name || data.name || 'Processing';

  var stepEl = document.createElement('div');
  stepEl.className = 'agui-step agui-step--running';
  stepEl.setAttribute('data-step-name', stepName);

  var icon = document.createElement('span');
  icon.className = 'agui-step-icon';
  icon.innerHTML = '<span class="agui-spinner"></span>';

  var label = document.createElement('span');
  label.className = 'agui-step-label';
  label.textContent = stepName;

  stepEl.appendChild(icon);
  stepEl.appendChild(label);
  progressContainer.appendChild(stepEl);
  scrollToBottom();
}

function handleAguiStepFinished(data, runState) {
  var stepName = data.step_name || data.name || '';
  var status = data.status || 'completed';
  appendTrailEvent('STEP_FINISHED', stepName, status);

  if (!currentAguiBubble) return;

  var stepName = data.step_name || data.name || '';
  var status = data.status || 'completed';
  var progressContainer = currentAguiBubble.querySelector('.agui-progress');
  if (!progressContainer) return;

  // Find the matching running step
  var steps = progressContainer.querySelectorAll('.agui-step--running[data-step-name="' + stepName + '"]');
  var stepEl = steps.length > 0 ? steps[steps.length - 1] : null;

  if (!stepEl) return;

  stepEl.classList.remove('agui-step--running');

  if (status === 'error') {
    stepEl.classList.add('agui-step--error');
    var icon = stepEl.querySelector('.agui-step-icon');
    if (icon) icon.textContent = '✗';

    // Show error message if provided
    if (data.error) {
      var errorEl = document.createElement('div');
      errorEl.className = 'agui-step-error';
      errorEl.textContent = data.error;
      stepEl.appendChild(errorEl);
    }
  } else {
    stepEl.classList.add('agui-step--completed');
    var icon = stepEl.querySelector('.agui-step-icon');
    if (icon) icon.textContent = '✓';
  }
  scrollToBottom();
}

function handleAguiToolCallStart(data, runState) {
  // Internal state tracking only — no DOM rendering (Requirement 6.2, 6.5)
  var toolName = data.tool_call_name || data.tool_name || data.name || '';
  var toolId = data.tool_call_id || data.id || '';
  var agent = data.agent_name || data.agent || '';
  var kbName = data.kb_name || '';

  // Build detail
  var detail = '';
  if (agent) detail = 'Agent: ' + agent;
  if (kbName) {
    if (detail) detail += ' | ';
    detail += 'KB: ' + kbName;
  }
  var args = data.args || data.input || data.arguments || null;
  if (args && typeof args === 'object') {
    var argSummary = Object.keys(args).slice(0, 3).map(function(k) {
      return k + '=' + String(args[k] || '').substring(0, 60);
    }).join(', ');
    if (detail) detail += ' | ';
    detail += argSummary;
  }

  appendTrailEvent('TOOL_CALL', toolName || _inferToolFromStep((runState.steps && runState.steps.length > 0) ? runState.steps[runState.steps.length - 1].name : ''), detail);
  toolCallCount++;
  activeTools.push({ id: toolId, name: toolName });
}

function _inferToolFromStep(stepName) {
  var lower = (stepName || '').toLowerCase();
  if (lower.indexOf('parse') !== -1) return 'parse_clinical_query';
  if (lower.indexOf('coordinate') !== -1) return 'query_guidelines + query_protocols';
  if (lower.indexOf('conflict') !== -1) return 'detect_guideline_conflicts';
  if (lower.indexOf('precedence') !== -1) return 'apply_precedence_rules';
  if (lower.indexOf('reasoning') !== -1 || lower.indexOf('narrative') !== -1) return 'generate_reasoning_narrative';
  if (lower.indexOf('synthesize') !== -1 || lower.indexOf('recommendation') !== -1) return 'synthesize_recommendation';
  return stepName;
}

function handleAguiToolCallEnd(data, runState) {
  // Internal state tracking only — no DOM rendering (Requirement 6.3, 6.5)
  var toolId = data.tool_call_id || data.id || '';
  var toolName = data.tool_call_name || data.tool_name || '';

  // Fall back to looking up from activeTools
  if (!toolName) {
    for (var i = 0; i < activeTools.length; i++) {
      if (activeTools[i].id === toolId) { toolName = activeTools[i].name; break; }
    }
  }

  // Log result/args summary if available
  var detail = '';
  var args = data.tool_args || data.args || '';
  if (args && typeof args === 'string' && args.length > 0) {
    detail = args;
  }
  var result = data.result || data.output || '';
  if (result && typeof result === 'string' && result.length > 0) {
    if (detail) detail += ' → ';
    detail += result;
  }
  appendTrailEvent('TOOL_RESULT', toolName || 'tool', detail);

  activeTools = activeTools.filter(function(t) { return t.id !== toolId; });
}

function handleAguiReasoningStart(data, runState) {
  if (!currentAguiBubble) return;

  var bubble = currentAguiBubble.querySelector('.message-bubble');
  if (!bubble) return;

  // Create collapsible reasoning section
  var reasoningSection = document.createElement('details');
  reasoningSection.className = 'agui-reasoning';

  var summary = document.createElement('summary');
  summary.className = 'agui-reasoning-summary';
  summary.textContent = 'Reasoning';
  reasoningSection.appendChild(summary);

  var content = document.createElement('div');
  content.className = 'agui-reasoning-content';
  reasoningSection.appendChild(content);

  bubble.appendChild(reasoningSection);
  scrollToBottom();
}

function handleAguiReasoningContent(data, runState) {
  if (!currentAguiBubble) return;

  var reasoningContent = currentAguiBubble.querySelector('.agui-reasoning-content');
  if (!reasoningContent) return;

  var delta = data.delta || '';
  if (delta) {
    reasoningContent.textContent += delta;
    scrollToBottom();
  }
}

function handleAguiReasoningEnd(data, runState) {
  if (!currentAguiBubble) return;

  // Collapse the reasoning section by default
  var reasoningSection = currentAguiBubble.querySelector('.agui-reasoning');
  if (reasoningSection) {
    reasoningSection.removeAttribute('open');
  }
}

function handleAguiTextMessageStart(data, runState) {
  if (!currentAguiBubble) return;

  var contentEl = currentAguiBubble.querySelector('.agui-recommendation');
  if (contentEl) {
    contentEl.classList.add('streaming-cursor');
    contentEl.textContent = '';
  }
}

function handleAguiTextMessageContent(data, runState) {
  if (!currentAguiBubble) return;

  var contentEl = currentAguiBubble.querySelector('.agui-recommendation');
  if (!contentEl) return;

  // Auto-hide progress steps 10s after first text content arrives
  if (!aguiProgressHideTimer && currentAguiBubble) {
    var bubble = currentAguiBubble;
    aguiProgressHideTimer = setTimeout(function() {
      var progressContainer = bubble.querySelector('.agui-progress');
      if (progressContainer) {
        progressContainer.style.transition = 'opacity 0.5s, max-height 0.5s';
        progressContainer.style.opacity = '0';
        progressContainer.style.maxHeight = '0';
        progressContainer.style.overflow = 'hidden';
        setTimeout(function() { progressContainer.remove(); }, 500);
      }
    }, 10000);
  }

  var rawText = runState.textBuffer || '';

  // Render full text content (citations now handled exclusively via STATE_SNAPSHOT)
  try {
    if (typeof marked !== 'undefined') {
      marked.setOptions({ breaks: true, gfm: true, headerIds: false, mangle: false });
      setSafeHtml(contentEl, marked.parse(rawText));
    } else {
      contentEl.textContent = rawText;
    }
  } catch (err) {
    contentEl.textContent = rawText;
  }
  scrollToBottom();
}

function handleAguiTextMessageEnd(data, runState) {
  if (!currentAguiBubble) return;

  var contentEl = currentAguiBubble.querySelector('.agui-recommendation');
  if (contentEl) {
    contentEl.classList.remove('streaming-cursor');

    var rawText = runState.textBuffer || '';

    // Render full text content (citations now handled exclusively via STATE_SNAPSHOT)
    try {
      if (typeof marked !== 'undefined') {
        marked.setOptions({ breaks: true, gfm: true, headerIds: false, mangle: false });
        setSafeHtml(contentEl, marked.parse(rawText));
      } else {
        contentEl.textContent = rawText;
      }
    } catch (err) {
      contentEl.textContent = rawText;
    }
  }
}

function handleAguiStateSnapshot(data, runState) {
  console.log('[AG-UI] STATE_SNAPSHOT received:', JSON.stringify(data).substring(0, 200));

  var snapshot = data.snapshot || {};
  var snapshotSessionId = data.session_id || data.sessionId || snapshot.session_id || snapshot.sessionId;
  var snapshotPatientContext = data.patient_context || snapshot.patient_context;
  var snapshotGuidelinesUsed = data.guidelines_used || snapshot.guidelines_used;
  var snapshotSafetyChecks = data.safety_checks || snapshot.safety_checks;
  var snapshotCitations = data.citations || snapshot.citations;

  // Build detail for trail log
  var snapshotDetails = [];
  if (snapshotCitations) snapshotDetails.push('citations: ' + snapshotCitations.length);
  if (snapshotGuidelinesUsed) snapshotDetails.push('guidelines: ' + snapshotGuidelinesUsed.length);
  if (snapshotSafetyChecks) snapshotDetails.push('safety_checks: ' + snapshotSafetyChecks.length);
  if (snapshotPatientContext) snapshotDetails.push('patient_context');
  if (snapshotSessionId) snapshotDetails.push('session: ' + snapshotSessionId.substring(0, 20));
  appendTrailEvent('STATE_SNAPSHOT', 'State update', snapshotDetails.join(' | ') || 'session data');
  
  // Update sessionId from STATE_SNAPSHOT events
  if (snapshotSessionId) {
    sessionId = snapshotSessionId;
  }

  // Update patient context from STATE_SNAPSHOT if present
  if (snapshotPatientContext) {
    patientContext = snapshotPatientContext;
    sessionStorage.setItem('patientContext', JSON.stringify(patientContext));
  }

  // Update guidelines used from STATE_SNAPSHOT if present
  if (snapshotGuidelinesUsed) {
    guidelinesUsed = snapshotGuidelinesUsed;
    sessionStorage.setItem('guidelinesUsed', JSON.stringify(guidelinesUsed));
  }

  // Update safety checks from STATE_SNAPSHOT if present
  if (snapshotSafetyChecks) {
    safetyChecks = snapshotSafetyChecks;
    sessionStorage.setItem('safetyChecks', JSON.stringify(safetyChecks));
  }

  // Update citations from STATE_SNAPSHOT if present — route to right panel
  if (snapshotCitations) {
    console.log('[AG-UI] Citations received in STATE_SNAPSHOT:', snapshotCitations.length, 'citations');
    citations = snapshotCitations;
    sessionStorage.setItem('citations', JSON.stringify(citations));
    renderRightCitationPanel();
  } else {
    console.log('[AG-UI] STATE_SNAPSHOT has no citations field. Keys:', Object.keys(data).join(', '));
  }

  // Re-render the full patient context pane (includes guidelines and safety checks)
  renderPatientContext();
}

// ── AG-UI Helper Functions ──────────────────────────────────

function createAguiBubble() {
  var msg = document.createElement('div');
  msg.className = 'message agent agui-message';

  var bubble = document.createElement('div');
  bubble.className = 'message-bubble';

  // Progress container for steps and tool badges
  var progress = document.createElement('div');
  progress.className = 'agui-progress';
  bubble.appendChild(progress);

  // Recommendation area for streamed text
  var recommendation = document.createElement('div');
  recommendation.className = 'agui-recommendation';
  recommendation.setAttribute('aria-live', 'polite');
  bubble.appendChild(recommendation);

  var time = document.createElement('div');
  time.className = 'message-time';
  time.textContent = formatTime();

  msg.appendChild(bubble);
  msg.appendChild(time);
  return msg;
}

function aguiShowTimeoutWarning() {
  if (!currentAguiBubble) return;

  var progressContainer = currentAguiBubble.querySelector('.agui-progress');
  if (!progressContainer) return;

  var warning = document.createElement('div');
  warning.className = 'agui-timeout-warning';
  warning.textContent = 'Processing is taking longer than expected. Please wait or try again.';
  progressContainer.appendChild(warning);

  // Re-enable input so user can cancel/retry
  setFormDisabled(false);
  scrollToBottom();
}

// ── Patient Selector ────────────────────────────────────────

function initPatientSelector() {
  el.patientSelector.addEventListener('focus', function() {
    openPatientDropdown();
  });

  el.patientSelector.addEventListener('input', function() {
    filterPatientList(el.patientSelector.value);
    openPatientDropdown();
  });

  el.patientSelector.addEventListener('keydown', function(e) {
    var list = el.patientSelectorList;
    var items = list.querySelectorAll('li:not(.patient-selector-empty):not(.patient-selector-loading)');

    if (e.key === 'ArrowDown') {
      e.preventDefault();
      highlightedIndex = Math.min(highlightedIndex + 1, items.length - 1);
      updateHighlight(items);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      highlightedIndex = Math.max(highlightedIndex - 1, 0);
      updateHighlight(items);
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (highlightedIndex >= 0 && items[highlightedIndex]) {
        var patientId = items[highlightedIndex].getAttribute('data-patient-id');
        selectPatient(patientId);
      }
    } else if (e.key === 'Escape') {
      closePatientDropdown();
      el.patientSelector.blur();
    }
  });

  // Close dropdown when clicking outside
  document.addEventListener('click', function(e) {
    if (!el.patientSelectorWrapper.contains(e.target)) {
      closePatientDropdown();
    }
  });
}

// ── Authenticated fetch helper ──────────────────────────────
function authFetch(url, options) {
  options = options || {};
  options.headers = options.headers || {};
  var token = auth ? auth.getIdToken() : null;
  if (token) {
    options.headers['Authorization'] = 'Bearer ' + token;
  }
  return fetch(url, options);
}

function fetchPatientList() {
  var baseUrl = config.apiBaseUrl;
  if (!baseUrl) {
    // Fallback: use static patients from config if available
    patientList = config.staticPatients || [];
    renderPatientList(patientList);
    return;
  }

  // Show loading state
  el.patientSelectorList.innerHTML = '<li class="patient-selector-loading">Loading patients…</li>';

  authFetch(baseUrl + '/patients')
    .then(function(response) {
      if (!response.ok) throw new Error('Failed to fetch patients');
      return response.json();
    })
    .then(function(data) {
      patientList = Array.isArray(data) ? data : (data.patients || []);
      renderPatientList(patientList);
    })
    .catch(function(err) {
      console.error('Error fetching patient list:', err);
      patientList = [];
      el.patientSelectorList.innerHTML = '<li class="patient-selector-empty">Unable to load patients</li>';
    });
}

function renderPatientList(patients) {
  el.patientSelectorList.innerHTML = '';

  if (patients.length === 0) {
    el.patientSelectorList.innerHTML = '<li class="patient-selector-empty">No patients found</li>';
    return;
  }

  patients.forEach(function(patient) {
    var li = document.createElement('li');
    li.setAttribute('role', 'option');
    li.setAttribute('data-patient-id', patient.patient_id);
    var nameSpan = document.createElement('span');
    nameSpan.className = 'patient-option-name';
    nameSpan.textContent = patient.patient_name;
    var metaSpan = document.createElement('span');
    metaSpan.className = 'patient-option-meta';
    metaSpan.textContent = '— ' + patient.age + 'y ' + patient.sex + ' (' + patient.patient_id + ')';
    li.appendChild(nameSpan);
    li.appendChild(document.createTextNode(' '));
    li.appendChild(metaSpan);

    li.addEventListener('click', function() {
      selectPatient(patient.patient_id);
    });

    el.patientSelectorList.appendChild(li);
  });
}

function filterPatientList(query) {
  var q = query.toLowerCase().trim();

  if (!q) {
    renderPatientList(patientList);
    highlightedIndex = -1;
    return;
  }

  var filtered = patientList.filter(function(patient) {
    var name = (patient.patient_name || '').toLowerCase();
    var id = (patient.patient_id || '').toLowerCase();
    return name.indexOf(q) !== -1 || id.indexOf(q) !== -1;
  });

  renderPatientList(filtered);
  highlightedIndex = filtered.length > 0 ? 0 : -1;

  // Apply initial highlight
  var items = el.patientSelectorList.querySelectorAll('li:not(.patient-selector-empty):not(.patient-selector-loading)');
  updateHighlight(items);
}

function selectPatient(patientId) {
  var patient = patientList.find(function(p) { return p.patient_id === patientId; });
  if (!patient) return;

  // Clear previous session data when changing patients
  if (selectedPatient && selectedPatient.patient_id !== patientId) {
    // Clear chat messages
    if (el.chatMessages) el.chatMessages.innerHTML = '';
    // Clear right citation panel (resets citations, sessionStorage, and DOM)
    clearRightCitationPanel();
    // Clear other session state
    guidelinesUsed = null;
    safetyChecks = null;
    sessionStorage.removeItem('guidelinesUsed');
    sessionStorage.removeItem('safetyChecks');
  }

  selectedPatient = patient;
  el.patientId.value = patient.patient_id;
  el.patientSelector.value = patient.patient_name + ' — ' + patient.age + 'y ' + patient.sex + ' (' + patient.patient_id + ')';
  el.patientSelector.classList.add('has-selection');

  // Persist selection in sessionStorage
  sessionStorage.setItem('selectedPatientId', patient.patient_id);

  closePatientDropdown();

  // Mark selected item in list
  var items = el.patientSelectorList.querySelectorAll('li[data-patient-id]');
  items.forEach(function(item) {
    item.classList.toggle('selected', item.getAttribute('data-patient-id') === patientId);
  });

  // Fetch full patient record and load context
  fetchPatientContext(patientId);
}

function fetchPatientContext(patientId) {
  var baseUrl = config.apiBaseUrl;
  if (!baseUrl) {
    patientContext = null;
    renderPatientContext();
    return;
  }

  authFetch(baseUrl + '/patients/' + encodeURIComponent(patientId))
    .then(function(response) {
      if (!response.ok) throw new Error('Failed to fetch patient record');
      return response.json();
    })
    .then(function(data) {
      var demographics = data.demographics || {};
      var vitalSigns = data.vital_signs || {};
      patientContext = {
        name: data.patient_name || demographics.patient_name || '',
        age: demographics.age || calculateAge(demographics.date_of_birth),
        sex: demographics.sex_at_birth || '',
        gender_identity: demographics.gender_identity || '',
        date_of_birth: demographics.date_of_birth || '',
        mrn: demographics.mrn || '',
        chart_number: demographics.chart_number || '',
        race: demographics.race || '',
        ethnicity: demographics.ethnicity || '',
        allergies: (data.allergies || []).map(function(a) {
          return { substance: a.allergy_substance || a.substance || '', type: a.allergy_type || '', reaction: a.reaction || '', severity: a.severity || '' };
        }),
        medications: (data.medications || []).map(function(m) {
          return { name: m.medication_name || m.name || '', dose: m.dose || '', dose_unit: m.dose_unit || '', route: m.route || '', frequency: m.frequency || '', adherence: m.adherence_notes || '' };
        }),
        vital_signs: {
          bp: vitalSigns.blood_pressure_systolic && vitalSigns.blood_pressure_diastolic ? vitalSigns.blood_pressure_systolic + '/' + vitalSigns.blood_pressure_diastolic + ' mmHg' : '',
          heart_rate: vitalSigns.heart_rate ? vitalSigns.heart_rate + ' bpm' : '',
          respiratory_rate: vitalSigns.respiratory_rate ? vitalSigns.respiratory_rate + ' /min' : '',
          temperature: vitalSigns.temperature ? vitalSigns.temperature + ' °C' : '',
          spo2: vitalSigns.spo2 ? vitalSigns.spo2 + '%' : '',
          weight: vitalSigns.weight ? vitalSigns.weight + ' kg' : '',
          height: vitalSigns.height ? vitalSigns.height + ' cm' : '',
          bmi: vitalSigns.bmi || ''
        },
        labs: (data.lab_results || data.labs || []).map(function(l) {
          return { test: l.test || '', value: l.value, unit: l.unit || '', date: l.date || '', flag: l.flag || '' };
        })
      };

      // Persist context in sessionStorage for page refresh recovery
      sessionStorage.setItem('patientContext', JSON.stringify(patientContext));

      renderPatientContext();
    })
    .catch(function(err) {
      console.error('Error fetching patient context:', err);
      patientContext = null;
      renderPatientContext();
    });
}

function calculateAge(dateOfBirth) {
  if (!dateOfBirth) return null;
  var dob = new Date(dateOfBirth);
  var today = new Date();
  var age = today.getFullYear() - dob.getFullYear();
  var monthDiff = today.getMonth() - dob.getMonth();
  if (monthDiff < 0 || (monthDiff === 0 && today.getDate() < dob.getDate())) {
    age--;
  }
  return age;
}

function renderPatientContext() {
  var pane = document.getElementById('patient-context-pane');
  if (!pane) return;

  if (!patientContext) {
    pane.innerHTML = '<p class="patient-context-empty">No patient selected</p>';
    return;
  }

  var html = '';

  // Header: name, age, sex
  html += '<div class="patient-context-header">';
  html += '<h3>' + escapeHtml(patientContext.name) + '</h3>';
  html += '<p>' + escapeHtml(String(patientContext.age || '')) + 'y ' + escapeHtml(patientContext.sex) + '</p>';
  html += '</div>';

  // Demographics
  html += '<div class="patient-context-section">';
  html += '<h4>Demographics</h4>';
  html += '<ul class="patient-context-list patient-context-demographics">';
  if (patientContext.mrn) html += '<li><strong>MRN:</strong> ' + escapeHtml(patientContext.mrn) + '</li>';
  if (patientContext.date_of_birth) html += '<li><strong>DOB:</strong> ' + escapeHtml(patientContext.date_of_birth) + '</li>';
  if (patientContext.race) html += '<li><strong>Race:</strong> ' + escapeHtml(patientContext.race) + '</li>';
  if (patientContext.ethnicity) html += '<li><strong>Ethnicity:</strong> ' + escapeHtml(patientContext.ethnicity) + '</li>';
  if (patientContext.gender_identity && patientContext.gender_identity !== patientContext.sex) {
    html += '<li><strong>Gender:</strong> ' + escapeHtml(patientContext.gender_identity) + '</li>';
  }
  html += '</ul>';
  html += '</div>';

  // Vital Signs
  if (patientContext.vital_signs) {
    var vs = patientContext.vital_signs;
    html += '<div class="patient-context-section">';
    html += '<h4>Vital Signs</h4>';
    html += '<ul class="patient-context-list">';
    if (vs.bp) html += '<li><strong>BP:</strong> ' + escapeHtml(vs.bp) + '</li>';
    if (vs.heart_rate) html += '<li><strong>HR:</strong> ' + escapeHtml(vs.heart_rate) + '</li>';
    if (vs.respiratory_rate) html += '<li><strong>RR:</strong> ' + escapeHtml(vs.respiratory_rate) + '</li>';
    if (vs.temperature) html += '<li><strong>Temp:</strong> ' + escapeHtml(vs.temperature) + '</li>';
    if (vs.spo2) html += '<li><strong>SpO₂:</strong> ' + escapeHtml(vs.spo2) + '</li>';
    if (vs.weight) html += '<li><strong>Weight:</strong> ' + escapeHtml(vs.weight) + '</li>';
    if (vs.height) html += '<li><strong>Height:</strong> ' + escapeHtml(vs.height) + '</li>';
    if (vs.bmi) html += '<li><strong>BMI:</strong> ' + escapeHtml(String(vs.bmi)) + '</li>';
    html += '</ul>';
    html += '</div>';
  }

  // Allergies
  html += '<div class="patient-context-section">';
  html += '<h4>Allergies</h4>';
  if (patientContext.allergies.length === 0) {
    html += '<p class="patient-context-none">No known allergies</p>';
  } else {
    html += '<ul class="patient-context-list">';
    patientContext.allergies.forEach(function(a) {
      html += '<li><strong>' + escapeHtml(a.substance) + '</strong>';
      if (a.type) html += ' (' + escapeHtml(a.type) + ')';
      if (a.severity) html += ' <span class="severity-badge severity-' + escapeHtml(a.severity.toLowerCase()) + '">' + escapeHtml(a.severity) + '</span>';
      if (a.reaction) html += '<br><small>Reaction: ' + escapeHtml(a.reaction) + '</small>';
      html += '</li>';
    });
    html += '</ul>';
  }
  html += '</div>';

  // Medications
  html += '<div class="patient-context-section">';
  html += '<h4>Medications</h4>';
  if (patientContext.medications.length === 0) {
    html += '<p class="patient-context-none">No current medications</p>';
  } else {
    html += '<ul class="patient-context-list">';
    patientContext.medications.forEach(function(m) {
      html += '<li><strong>' + escapeHtml(m.name) + '</strong>';
      if (m.dose) html += ' ' + escapeHtml(m.dose) + (m.dose_unit ? escapeHtml(m.dose_unit) : '');
      if (m.route) html += ' ' + escapeHtml(m.route);
      if (m.frequency) html += ' ' + escapeHtml(m.frequency);
      html += '</li>';
    });
    html += '</ul>';
  }
  html += '</div>';

  // Lab Results
  html += '<div class="patient-context-section">';
  html += '<h4>Lab Results</h4>';
  if (patientContext.labs.length === 0) {
    html += '<p class="patient-context-none">No recent lab results</p>';
  } else {
    html += '<ul class="patient-context-list">';
    patientContext.labs.forEach(function(l) {
      html += '<li><strong>' + escapeHtml(l.test) + '</strong>: ' + escapeHtml(String(l.value)) + ' ' + escapeHtml(l.unit);
      if (l.flag && l.flag !== 'NORMAL') html += ' <span class="lab-flag lab-flag-' + escapeHtml(l.flag.toLowerCase()) + '">' + escapeHtml(l.flag) + '</span>';
      if (l.date) html += ' <span class="lab-date">(' + escapeHtml(l.date) + ')</span>';
      html += '</li>';
    });
    html += '</ul>';
  }
  html += '</div>';

  // Guidelines Used (populated from STATE_SNAPSHOT during recommendation)
  if (guidelinesUsed && guidelinesUsed.length > 0) {
    html += '<div class="patient-context-section">';
    html += '<h4>Guidelines Used</h4>';
    html += '<ul class="patient-context-list guidelines-used-list">';
    guidelinesUsed.forEach(function(g) {
      html += '<li><strong>' + escapeHtml(g.name) + '</strong>: ' + escapeHtml(g.verdict) + '</li>';
    });
    html += '</ul>';
    html += '</div>';
  }

  // Safety Checks (populated from STATE_SNAPSHOT during recommendation)
  if (safetyChecks && safetyChecks.length > 0) {
    html += '<div class="patient-context-section">';
    html += '<h4>Safety Checks</h4>';
    html += '<ul class="patient-context-list safety-checks-list">';
    safetyChecks.forEach(function(s) {
      var colorClass = 'safety-check-' + escapeHtml(s.color || 'green');
      html += '<li class="safety-check-item ' + colorClass + '">';
      html += '<span class="safety-check-bullet"></span>';
      html += '<span class="safety-check-text">' + escapeHtml(s.check) + '</span>';
      html += ' <span class="safety-check-status">' + escapeHtml(s.status) + '</span>';
      html += '</li>';
    });
    html += '</ul>';
    html += '</div>';
  }

  setSafeHtml(pane, html);
}

function restorePatientSelection() {
  // On page refresh: clear stale session data (citations, patient context, trail)
  // so the user starts fresh. Only the selected patient ID is preserved.
  sessionStorage.removeItem('citations');
  sessionStorage.removeItem('patientContext');
  sessionStorage.removeItem('guidelinesUsed');
  sessionStorage.removeItem('safetyChecks');
  citations = null;
  patientContext = null;
  guidelinesUsed = null;
  safetyChecks = null;
  trailData = null;

  var storedId = sessionStorage.getItem('selectedPatientId');
  if (storedId && patientList.length > 0) {
    selectPatient(storedId);
  }
}

function openPatientDropdown() {
  el.patientSelectorList.classList.add('open');
  el.patientSelector.setAttribute('aria-expanded', 'true');
}

function closePatientDropdown() {
  el.patientSelectorList.classList.remove('open');
  el.patientSelector.setAttribute('aria-expanded', 'false');
  highlightedIndex = -1;
}

function updateHighlight(items) {
  for (var i = 0; i < items.length; i++) {
    items[i].classList.toggle('highlighted', i === highlightedIndex);
  }

  // Scroll highlighted item into view
  if (highlightedIndex >= 0 && items[highlightedIndex]) {
    items[highlightedIndex].scrollIntoView({ block: 'nearest' });
  }
}

// ── Right Citation Panel Toggle & Persistence ────────────────

function initRightCitationPanel() {
  if (!el.rightCitationPanel || !el.rightPanelContent || !el.citationPanelToggle) return;

  // Restore persisted state (default to closed)
  var saved = sessionStorage.getItem('citationPanelOpen');
  if (saved === 'true') {
    openCitationPanel();
  } else {
    closeCitationPanel();
  }

  // Bind click event on citation toggle button
  el.citationPanelToggle.addEventListener('click', toggleCitationPanel);

  // Bind click event on trail toggle button
  if (el.trailPanelToggle) {
    el.trailPanelToggle.addEventListener('click', toggleTrailPanel);
  }
}

function toggleCitationPanel() {
  if (!el.rightCitationPanel) return;
  var isOpen = el.rightCitationPanel.classList.contains('right-panel--open');
  if (isOpen) {
    closeCitationPanel();
  } else {
    openCitationPanel();
  }
}

function openCitationPanel() {
  if (!el.rightCitationPanel) return;
  el.rightCitationPanel.classList.add('right-panel--open');
  if (el.citationPanelToggle) {
    el.citationPanelToggle.setAttribute('aria-expanded', 'true');
  }
  try { sessionStorage.setItem('citationPanelOpen', 'true'); } catch (e) { /* graceful */ }
}

function closeCitationPanel() {
  if (!el.rightCitationPanel) return;
  el.rightCitationPanel.classList.remove('right-panel--open');
  if (el.citationPanelToggle) {
    el.citationPanelToggle.setAttribute('aria-expanded', 'false');
  }
  try { sessionStorage.setItem('citationPanelOpen', 'false'); } catch (e) { /* graceful */ }
}

// ── Trail Panel Toggle ───────────────────────────────────────

function toggleTrailPanel() {
  if (!el.rightTrailPanel) return;
  var isOpen = el.rightTrailPanel.classList.contains('right-panel--open');
  if (isOpen) {
    closeTrailPanel();
  } else {
    openTrailPanel();
  }
}

function openTrailPanel() {
  if (!el.rightTrailPanel) return;
  el.rightTrailPanel.classList.add('right-panel--open');
  if (el.trailPanelToggle) {
    el.trailPanelToggle.setAttribute('aria-expanded', 'true');
  }
}

function closeTrailPanel() {
  if (!el.rightTrailPanel) return;
  el.rightTrailPanel.classList.remove('right-panel--open');
  if (el.trailPanelToggle) {
    el.trailPanelToggle.setAttribute('aria-expanded', 'false');
  }
}

// ── Decision Trail Rendering ─────────────────────────────────

function renderTrailPanel() {
  if (!el.rightTrailContent) return;
  if (!trailData && trailEventLog.length === 0) {
    el.rightTrailContent.innerHTML = '<p class="trail-panel-empty">No decision trail available. Submit a query to see the agent reasoning.</p>';
    return;
  }

  var html = '';

  // Header
  html += '<div class="trail-section">';
  html += '<div class="trail-section-header">Event Log</div>';
  if (trailData && trailData.runId) {
    html += '<code class="trail-id-badge">' + escapeHtml(trailData.runId) + '</code>';
  }
  if (trailData && trailData.totalDuration) {
    html += '<div style="font-size:11px;color:#6b7280;margin-bottom:8px;">Duration: <strong>' + trailData.totalDuration + '</strong> | Events: <strong>' + trailEventLog.length + '</strong></div>';
  }
  html += '</div>';

  // Log entries
  html += '<div class="trail-section">';
  html += '<div class="trail-log-container">';
  trailEventLog.forEach(function(entry) {
    var time = new Date(entry.timestamp).toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit', fractionalSecondDigits: 3 });
    var levelClass = 'trail-log-info';
    if (entry.type.indexOf('ERROR') !== -1) levelClass = 'trail-log-error';
    else if (entry.type.indexOf('FINISHED') !== -1) levelClass = 'trail-log-success';
    else if (entry.type.indexOf('STARTED') !== -1) levelClass = 'trail-log-start';
    else if (entry.type === 'TOOL_RESULT') levelClass = 'trail-log-result';
    else if (entry.type.indexOf('TOOL') !== -1) levelClass = 'trail-log-tool';
    else if (entry.type.indexOf('SNAPSHOT') !== -1) levelClass = 'trail-log-info';

    html += '<div class="trail-log-entry ' + levelClass + '">';
    html += '<span class="trail-log-time">' + time + '</span>';
    html += '<span class="trail-log-type">' + escapeHtml(entry.type) + '</span>';
    html += '<span class="trail-log-msg">' + escapeHtml(entry.message) + '</span>';
    if (entry.detail) {
      html += '<span class="trail-log-detail">' + escapeHtml(entry.detail) + '</span>';
    }
    html += '</div>';
  });
  html += '</div></div>';

  setSafeHtml(el.rightTrailContent, html);
  // Auto-scroll to bottom
  el.rightTrailContent.scrollTop = el.rightTrailContent.scrollHeight;
}

function appendTrailEvent(type, message, detail) {
  trailEventLog.push({
    timestamp: Date.now(),
    type: type,
    message: message || '',
    detail: detail || ''
  });
  // Live-render if trail panel is open
  if (el.rightTrailPanel && el.rightTrailPanel.classList.contains('right-panel--open')) {
    renderTrailPanel();
  }
}

// ── Citation Extraction from Rendered Chat ────────────────────

/**
 * Fallback: Extract citations from the rendered chat bubble when STATE_SNAPSHOT
 * doesn't provide structured citation data. Parses the citations table from
 * the markdown output and moves it to the right panel.
 */
function extractCitationsFromBubble(bubble) {
  if (!bubble) return;
  var contentEl = bubble.querySelector('.agui-recommendation');
  if (!contentEl) return;

  // Find the Citations heading — could be h2, h3, p, or strong with various emoji
  var allElements = contentEl.querySelectorAll('h2, h3, h4, p, strong');
  var citationHeading = null;
  for (var i = 0; i < allElements.length; i++) {
    var text = allElements[i].textContent.trim();
    if (text.match(/citations/i)) {
      citationHeading = allElements[i];
      break;
    }
  }

  if (!citationHeading) {
    console.log('[Citations] No citation section found in rendered content');
    return;
  }

  // Find the table after the citations heading
  var table = null;
  var sibling = citationHeading.nextElementSibling;
  while (sibling) {
    if (sibling.tagName === 'TABLE') { table = sibling; break; }
    if (sibling.tagName === 'H1' || sibling.tagName === 'H2' || sibling.tagName === 'H3') break;
    sibling = sibling.nextElementSibling;
  }

  if (!table) {
    console.log('[Citations] Citations heading found but no table after it');
    return;
  }

  // Parse table rows into citation objects
  var rows = table.querySelectorAll('tbody tr');
  if (rows.length === 0) {
    // No tbody — try all tr except the first (header)
    var allRows = table.querySelectorAll('tr');
    rows = Array.prototype.slice.call(allRows, 1);
  }
  var extracted = [];

  for (var j = 0; j < rows.length; j++) {
    var cells = rows[j].querySelectorAll('td');
    if (cells.length < 2) continue;

    var source = '', docTitle = '', typeText = '';
    if (cells.length >= 4) {
      // [#, Source, Document, Type]
      source = cells[1].textContent.trim();
      docTitle = cells[2].textContent.trim();
      typeText = cells[3].textContent.trim();
    } else if (cells.length === 3) {
      // [Source, Document, Type] or [#, Source, Document]
      var firstCell = cells[0].textContent.trim();
      if (firstCell.match(/^\d+$/)) {
        source = cells[1].textContent.trim();
        docTitle = cells[2].textContent.trim();
      } else {
        source = cells[0].textContent.trim();
        docTitle = cells[1].textContent.trim();
        typeText = cells[2].textContent.trim();
      }
    } else if (cells.length === 2) {
      source = cells[0].textContent.trim();
      docTitle = cells[1].textContent.trim();
    }

    if (source) {
      extracted.push({
        source_organization: source,
        document_title: docTitle,
        publication_reference: typeText || source,
        variant: '',
        rating: '',
        quoted_text: ''
      });
    }
  }

  if (extracted.length > 0) {
    console.log('[Citations] Extracted', extracted.length, 'citations from rendered content');
    citations = extracted;
    sessionStorage.setItem('citations', JSON.stringify(citations));
    renderRightCitationPanel();

    // Remove the citations section from the chat bubble to avoid duplication
    var toRemove = [];
    toRemove.push(citationHeading);
    var nextSib = citationHeading.nextElementSibling;
    while (nextSib) {
      if (nextSib.tagName === 'H1' || nextSib.tagName === 'H2' || nextSib.tagName === 'H3') break;
      toRemove.push(nextSib);
      nextSib = nextSib.nextElementSibling;
    }
    toRemove.forEach(function(node) { node.remove(); });
  }
}

// ── Right Citation Panel Rendering ───────────────────────────

function renderRightCitationPanel() {
  if (!el.rightPanelContent) return;

  if (!citations || citations.length === 0) {
    el.rightPanelContent.innerHTML = '<p class="citation-panel-empty">No citations available</p>';
    return;
  }

  // Group citations by source_organization preserving first-appearance order
  var seen = [];
  var map = {};
  citations.forEach(function(c) {
    var org = c.source_organization || 'Unknown Source';
    if (!map[org]) { map[org] = []; seen.push(org); }
    map[org].push(c);
  });

  var html = '';

  seen.forEach(function(org, idx) {
    var group = map[org];
    var typeInfo = classifyCitationType(group[0]);

    html += '<div class="citation-group">';
    html += '<div class="citation-group-header">';
    html += '<span class="citation-org-name">' + escapeHtml(org) + '</span>';
    html += '<span class="citation-type-badge ' + typeInfo.cssClass + '">' + escapeHtml(typeInfo.label) + '</span>';
    html += '</div>';

    group.forEach(function(citation, cIdx) {
      html += '<div class="citation-item">';

      if (citation.document_title) {
        html += '<p class="citation-title">' + escapeHtml(citation.document_title) + '</p>';
      }

      if (citation.variant) {
        html += '<p class="citation-variant">§ ' + escapeHtml(citation.variant) + '</p>';
      }

      if (citation.rating) {
        html += '<p class="citation-rating">"' + escapeHtml(citation.rating) + '"</p>';
      }

      // RAG chunk as expandable <details>/<summary> (collapsed by default)
      if (citation.quoted_text) {
        html += '<div class="rag-chunk-section">';
        html += '<details>';
        html += '<summary>Source text</summary>';
        html += '<blockquote>' + escapeHtml(citation.quoted_text) + '</blockquote>';
        html += '</details>';
        html += '</div>';
      }

      html += '</div>';
    });

    html += '</div>';

    if (idx < seen.length - 1) {
      html += '<hr class="citation-group-divider">';
    }
  });

  setSafeHtml(el.rightPanelContent, html);
}

function classifyCitationType(citation) {
  var ref = (citation.publication_reference || '').toLowerCase();
  var org = (citation.source_organization || '').toLowerCase();

  if (ref.match(/patient/) || org.match(/patient/)) {
    return { label: 'Patient', cssClass: 'citation-type-patient' };
  }
  if (ref.match(/local|national/) || org.match(/has france|nice|dgos/)) {
    return { label: 'Local/National', cssClass: 'citation-type-local' };
  }
  return { label: 'International', cssClass: 'citation-type-international' };
}

function clearRightCitationPanel() {
  citations = null;
  trailData = null;
  trailEventLog = [];
  try { sessionStorage.removeItem('citations'); } catch (e) { /* graceful */ }
  if (el.rightPanelContent) {
    el.rightPanelContent.innerHTML = '<p class="citation-panel-empty">No citations available</p>';
  }
  if (el.rightTrailContent) {
    el.rightTrailContent.innerHTML = '<p class="trail-panel-empty">No decision trail available. Submit a query to see the agent reasoning.</p>';
  }
}

// ── Guideline Selector ──────────────────────────────────────

function initGuidelineSelector() {
  el.guidelineSelector.addEventListener('click', function(e) {
    e.stopPropagation();
    toggleGuidelineDropdown();
  });

  // Close dropdown when clicking outside
  document.addEventListener('click', function(e) {
    if (!el.guidelineSelectorWrapper.contains(e.target)) {
      closeGuidelineDropdown();
    }
  });

  // Keyboard support
  el.guidelineSelector.addEventListener('keydown', function(e) {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      toggleGuidelineDropdown();
    } else if (e.key === 'Escape') {
      closeGuidelineDropdown();
    }
  });
}

function fetchGuidelineList() {
  var baseUrl = config.apiBaseUrl;
  if (!baseUrl) {
    // Fallback: use default guidelines if no API configured
    guidelineList = [
      { name: 'ACR', id: 'acr' },
      { name: 'ESR', id: 'esr' },
      { name: 'NICE', id: 'nice' },
      { name: 'HAS France', id: 'has_france' }
    ];
    renderGuidelineList();
    return;
  }

  authFetch(baseUrl + '/guidelines')
    .then(function(response) {
      if (!response.ok) throw new Error('Failed to fetch guidelines');
      return response.json();
    })
    .then(function(data) {
      guidelineList = Array.isArray(data) ? data : [];
      if (guidelineList.length === 0) {
        // Fallback to defaults if API returns empty
        guidelineList = [
          { name: 'ACR', id: 'acr' },
          { name: 'ESR', id: 'esr' },
          { name: 'NICE', id: 'nice' },
          { name: 'HAS France', id: 'has_france' }
        ];
      }
      renderGuidelineList();
    })
    .catch(function(err) {
      console.error('Error fetching guideline list:', err);
      // Fallback to defaults on error
      guidelineList = [
        { name: 'ACR', id: 'acr' },
        { name: 'ESR', id: 'esr' },
        { name: 'NICE', id: 'nice' },
        { name: 'HAS France', id: 'has_france' }
      ];
      renderGuidelineList();
    });
}

function renderGuidelineList() {
  el.guidelineSelectorList.innerHTML = '';

  guidelineList.forEach(function(guideline) {
    var li = document.createElement('li');
    li.setAttribute('role', 'option');
    li.setAttribute('data-guideline-id', guideline.id);
    li.setAttribute('aria-selected', isGuidelineSelected(guideline.id) ? 'true' : 'false');

    var checkbox = document.createElement('span');
    checkbox.className = 'guideline-checkbox';
    checkbox.textContent = isGuidelineSelected(guideline.id) ? '✓' : '';

    var label = document.createElement('span');
    label.className = 'guideline-option-label';
    label.textContent = guideline.name;

    li.appendChild(checkbox);
    li.appendChild(label);

    if (isGuidelineSelected(guideline.id)) {
      li.classList.add('selected');
    }

    li.addEventListener('click', function(e) {
      e.stopPropagation();
      toggleGuidelineItem(guideline.id);
    });

    el.guidelineSelectorList.appendChild(li);
  });

  updateGuidelineSelectorText();
}

function toggleGuidelineItem(guidelineId) {
  var index = selectedGuidelines.indexOf(guidelineId);
  if (index === -1) {
    selectedGuidelines.push(guidelineId);
  } else {
    selectedGuidelines.splice(index, 1);
  }

  // Update the list item visuals
  var item = el.guidelineSelectorList.querySelector('[data-guideline-id="' + guidelineId + '"]');
  if (item) {
    var isSelected = selectedGuidelines.indexOf(guidelineId) !== -1;
    item.classList.toggle('selected', isSelected);
    item.setAttribute('aria-selected', isSelected ? 'true' : 'false');
    var checkbox = item.querySelector('.guideline-checkbox');
    if (checkbox) checkbox.textContent = isSelected ? '✓' : '';
  }

  updateGuidelineSelectorText();
  persistGuidelineSelection();
}

function isGuidelineSelected(guidelineId) {
  // If no explicit selection, all are considered selected
  if (selectedGuidelines.length === 0) return true;
  return selectedGuidelines.indexOf(guidelineId) !== -1;
}

function getSelectedGuidelineIds() {
  // If none explicitly selected, return all (default behavior)
  if (selectedGuidelines.length === 0) {
    return guidelineList.map(function(g) { return g.id; });
  }
  return selectedGuidelines.slice();
}

function updateGuidelineSelectorText() {
  var textEl = el.guidelineSelector.querySelector('.guideline-selector-text');
  if (!textEl) return;

  if (selectedGuidelines.length === 0 || selectedGuidelines.length === guidelineList.length) {
    textEl.textContent = 'All guidelines';
    el.guidelineSelector.classList.remove('has-selection');
  } else if (selectedGuidelines.length === 1) {
    var g = guidelineList.find(function(gl) { return gl.id === selectedGuidelines[0]; });
    textEl.textContent = g ? g.name : '1 selected';
    el.guidelineSelector.classList.add('has-selection');
  } else {
    textEl.textContent = selectedGuidelines.length + ' selected';
    el.guidelineSelector.classList.add('has-selection');
  }
}

function persistGuidelineSelection() {
  sessionStorage.setItem('selectedGuidelines', JSON.stringify(selectedGuidelines));
}

function restoreGuidelineSelection() {
  var stored = sessionStorage.getItem('selectedGuidelines');
  if (stored) {
    try {
      selectedGuidelines = JSON.parse(stored);
      if (!Array.isArray(selectedGuidelines)) selectedGuidelines = [];
    } catch (e) {
      selectedGuidelines = [];
    }
  } else {
    selectedGuidelines = [];
  }
  // Re-render to reflect restored selection
  if (guidelineList.length > 0) {
    renderGuidelineList();
  }
}

function toggleGuidelineDropdown() {
  var isOpen = el.guidelineSelectorList.classList.contains('open');
  if (isOpen) {
    closeGuidelineDropdown();
  } else {
    openGuidelineDropdown();
  }
}

function openGuidelineDropdown() {
  el.guidelineSelectorList.classList.add('open');
  el.guidelineSelector.setAttribute('aria-expanded', 'true');
}

function closeGuidelineDropdown() {
  el.guidelineSelectorList.classList.remove('open');
  el.guidelineSelector.setAttribute('aria-expanded', 'false');
}

document.addEventListener('DOMContentLoaded', initializeApp);
window.addEventListener('beforeunload', cleanup);
