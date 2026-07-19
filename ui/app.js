const dom = (id) => document.getElementById(id);

const state = {
  activeView: 'submit',
  job: null,
  activeRun: null,
  eventSource: null,
  statusPoll: null,
  elapsedTimer: null,
  activityPoll: null,
  startedAt: null,
};

const stageNames = new Set(['github_checkout', 'form_hypothesis', 'candidate_sandbox', 'replay_1', 'replay_2', 'verdict', 'jira_comment']);

async function api(url, options = {}) {
  const response = await fetch(url, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = typeof payload.detail === 'string' ? payload.detail : `Request failed (${response.status}).`;
    throw new Error(detail);
  }
  return payload;
}

function showView(view) {
  if (state.activeView === 'live' && view !== 'live') stopLiveConnections();
  if (state.activeView === 'activity' && view !== 'activity') stopActivityPolling();
  document.querySelectorAll('.screen').forEach((screen) => { screen.hidden = screen.id !== `screen-${view}`; });
  document.querySelectorAll('[data-view-target]').forEach((button) => {
    button.classList.toggle('is-active', button.dataset.viewTarget === view);
  });
  state.activeView = view;
  if (view === 'history') loadHistory();
  if (view === 'activity') startActivityPolling();
  window.scrollTo({ top: 0, behavior: prefersReducedMotion() ? 'auto' : 'smooth' });
}

function prefersReducedMotion() {
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

function announce(message) {
  dom('app-announcer').textContent = message;
}

function runPayload(form) {
  const title = form.elements.title.value.trim();
  const body = form.elements.body.value.trim();
  const expectedError = form.elements['expected-error'].value.trim();
  const kind = form.elements['repository-kind'].value;
  if (!title || !body) throw new Error('Add a title and description before running the investigation.');
  let repository;
  let repoRef;
  if (kind === 'github') {
    const repositoryName = form.elements['github-repository'].value.trim();
    const ref = form.elements['github-ref'].value.trim();
    if (!repositoryName || !ref) throw new Error('Add a GitHub repository and branch or tag before running the investigation.');
    repoRef = `${repositoryName}@${ref}`;
    repository = { kind: 'github', repository: repositoryName, ref };
  } else {
    const path = form.elements['repository-path'].value.trim();
    const commit = form.elements['commit-label'].value.trim();
    if (!path || !commit) throw new Error('Add a repository folder and commit label before running the investigation.');
    repoRef = `local@${commit}`;
    repository = { kind: 'local_path', path, commit };
  }
  const ticket = {
    id: `UI-${Date.now()}`,
    title,
    body,
    repo_ref: repoRef,
  };
  if (expectedError) ticket.expected_error = expectedError;
  return { ticket, repository };
}

function setRepositoryFields(kind) {
  document.querySelectorAll('[data-source-kind]').forEach((field) => {
    const active = field.dataset.sourceKind === kind;
    field.hidden = !active;
    field.querySelectorAll('input').forEach((input) => {
      input.disabled = !active;
      input.required = active;
    });
  });
}

async function submitInvestigation(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = dom('run-button');
  const message = dom('form-message');
  message.textContent = '';
  let payload;
  try {
    payload = runPayload(form);
  } catch (error) {
    message.textContent = error.message;
    return;
  }

  button.disabled = true;
  button.textContent = 'Starting investigation';
  try {
    const accepted = await api('/investigations', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    startLiveView(accepted, payload.ticket.title);
  } catch (error) {
    message.textContent = `The investigation did not start. ${error.message}`;
  } finally {
    button.disabled = false;
    button.textContent = 'Run investigation';
  }
}

function startLiveView(accepted, ticketTitle) {
  stopLiveConnections();
  state.job = accepted;
  state.activeRun = null;
  const startedAt = accepted.created_at ? new Date(accepted.created_at).getTime() : Number.NaN;
  state.startedAt = Number.isNaN(startedAt) ? Date.now() : startedAt;
  resetStages();
  dom('live-ticket-title').textContent = accepted.issue_key ? `${accepted.issue_key} · ${ticketTitle}` : ticketTitle;
  dom('live-status-message').textContent = 'Investigation queued';
  dom('live-status-dot').className = 'status-dot is-running';
  dom('live-error').hidden = true;
  dom('live-actions').hidden = true;
  showView('live');
  tickElapsed();
  state.elapsedTimer = window.setInterval(tickElapsed, 1000);
  subscribeToProgress(accepted.events_url);
  pollJob(accepted.status_url);
  announce('Investigation queued. Live progress is available.');
}

function resetStages() {
  document.querySelectorAll('.stage').forEach((item) => {
    item.className = 'stage';
    item.querySelector('small').textContent = 'Waiting';
  });
}

function subscribeToProgress(url) {
  const source = new EventSource(url);
  state.eventSource = source;
  source.addEventListener('progress', (message) => {
    try {
      applyProgress(JSON.parse(message.data));
    } catch {
      // Polling remains the reliable completion path if a malformed transient event is received.
    }
  });
}

function applyProgress(event) {
  if (event.stage === 'job') {
    if (event.state === 'running') dom('live-status-message').textContent = 'Investigation running in a restricted sandbox';
    return;
  }
  if (!stageNames.has(event.stage)) return;
  const stage = document.querySelector(`.stage[data-stage="${event.stage}"]`);
  if (!stage) return;
  stage.className = 'stage';
  const stateLabel = stage.querySelector('small');
  if (event.state === 'started') {
    stage.classList.add('is-active');
    stateLabel.textContent = 'In progress';
    dom('live-status-message').textContent = event.label;
    announce(`${event.label} started.`);
  } else if (event.state === 'completed') {
    stage.classList.add('is-complete');
    stateLabel.textContent = 'Complete';
    announce(`${event.label} complete.`);
  } else if (event.state === 'failed') {
    stage.classList.add('is-failed');
    stateLabel.textContent = 'Stopped';
  }
}

function pollJob(url) {
  const poll = async () => {
    try {
      const job = await api(url);
      if (job.status === 'done') {
        stopLiveConnections();
        dom('live-status-dot').className = 'status-dot is-done';
        dom('live-status-message').textContent = 'Investigation complete. Evidence bundle is ready.';
        dom('live-actions').hidden = false;
        state.activeRun = await api(job.run_url);
        renderEvidence(state.activeRun);
        announce('Investigation complete. Evidence bundle is ready.');
        window.setTimeout(() => {
          if (state.activeView === 'live') showView('evidence');
        }, prefersReducedMotion() ? 0 : 650);
      } else if (job.status === 'failed') {
        stopLiveConnections();
        dom('live-status-dot').className = 'status-dot is-failed';
        dom('live-status-message').textContent = 'Investigation stopped';
        dom('live-error').textContent = `${job.error || 'The worker stopped before producing a result.'} Check the repository folder and service configuration, then start a new investigation.`;
        dom('live-error').hidden = false;
        dom('live-actions').hidden = false;
        announce('Investigation failed. Review the error and start a new investigation.');
      }
    } catch (error) {
      dom('live-error').textContent = `Unable to read job progress. ${error.message}`;
      dom('live-error').hidden = false;
    }
  };
  poll();
  state.statusPoll = window.setInterval(poll, 1000);
}

function stopLiveConnections() {
  if (state.eventSource) state.eventSource.close();
  if (state.statusPoll) window.clearInterval(state.statusPoll);
  if (state.elapsedTimer) window.clearInterval(state.elapsedTimer);
  state.eventSource = null;
  state.statusPoll = null;
  state.elapsedTimer = null;
}

function startActivityPolling() {
  stopActivityPolling();
  loadActivity();
  state.activityPoll = window.setInterval(loadActivity, 3000);
}

function stopActivityPolling() {
  if (state.activityPoll) window.clearInterval(state.activityPoll);
  state.activityPoll = null;
}

async function loadActivity() {
  const loading = dom('activity-loading');
  const empty = dom('activity-empty');
  const error = dom('activity-error');
  const list = dom('activity-list');
  loading.hidden = false;
  empty.hidden = true;
  error.hidden = true;
  try {
    const { jobs } = await api('/investigations?limit=25');
    list.replaceChildren();
    if (!jobs.length) {
      empty.hidden = false;
      return;
    }
    jobs.forEach((job) => list.append(activityRow(job)));
  } catch (requestError) {
    error.textContent = `Unable to load active investigations. ${requestError.message}`;
    error.hidden = false;
  } finally {
    loading.hidden = true;
  }
}

function activityRow(job) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'history-row activity-row';
  button.innerHTML = '<span class="history-ticket"><strong></strong><span></span></span><span class="history-status"></span><span class="history-score"></span><span class="history-arrow" aria-hidden="true">›</span>';
  const title = job.ticket?.title || job.issue_key || 'Investigation';
  const source = job.source === 'jira' ? `Jira ${job.issue_key || job.ticket?.id || ''}` : 'Manual submission';
  button.querySelector('.history-ticket strong').textContent = title;
  button.querySelector('.history-ticket span').textContent = `${source} · ${formatDate(job.updated_at)}`;
  const status = button.querySelector('.history-status');
  status.textContent = job.status;
  status.classList.add(`is-job-${job.status}`);
  const detail = button.querySelector('.history-score');
  detail.textContent = job.status === 'done' ? `${job.verdict?.score ?? '—'}/100` : job.status === 'failed' ? 'Needs review' : 'Live';
  button.addEventListener('click', () => startLiveView(job, title));
  return button;
}

function tickElapsed() {
  const seconds = Math.max(0, Math.floor((Date.now() - state.startedAt) / 1000));
  const minutes = String(Math.floor(seconds / 60)).padStart(2, '0');
  const remainder = String(seconds % 60).padStart(2, '0');
  dom('elapsed-time').textContent = `${minutes}:${remainder}`;
}

function renderEvidence(run) {
  state.activeRun = run;
  const { ticket, candidates, evidence, verdict, manifest } = run;
  const candidate = candidates[0] || {};
  const candidateEvidence = evidence.find((item) => item.phase === 'CANDIDATE') || evidence[0] || {};
  const replays = evidence.filter((item) => item.phase === 'REPLAY');
  const presentation = verdictPresentation(verdict.status);
  const hero = dom('verdict-hero');
  hero.className = `verdict-hero ${presentation.className}`;
  dom('evidence-title').textContent = presentation.label;
  dom('evidence-score').textContent = `${verdict.evidence_score}/100`;
  dom('verdict-summary').textContent = verdictSummary(verdict);
  dom('evidence-ticket-title').textContent = ticket.title;
  dom('evidence-run-id').textContent = manifest.run_id;
  dom('evidence-commit').textContent = manifest.repo_commit;
  dom('candidate-test').textContent = candidate.content || '# The investigation did not generate a candidate test.';
  dom('sandbox-result').textContent = sandboxReport(candidateEvidence);
  dom('grounded-result').textContent = groundedReport(candidateEvidence);
  dom('replay-result').textContent = replayReport(replays);
  renderScoreBreakdown(verdict);
  document.querySelectorAll('.evidence-details details').forEach((detail) => { detail.open = false; });
}

function verdictPresentation(status) {
  if (status === 'REPRODUCED') return { label: 'REPRODUCED', className: 'is-reproduced' };
  if (status === 'NEED_INFO') return { label: 'NEED INFO', className: 'is-need-info' };
  if (status === 'INCONCLUSIVE') return { label: 'INCONCLUSIVE', className: 'is-inconclusive' };
  return { label: 'CANNOT REPRODUCE', className: 'is-not-reproduced' };
}

function verdictSummary(verdict) {
  if (verdict.status === 'REPRODUCED') return 'The generated test failed in the repository and two clean sandbox replays produced the same signature.';
  if (verdict.status === 'NEED_INFO') return 'The report did not include enough information to begin a reliable investigation.';
  return 'The available evidence did not meet the threshold for a verified reproduction.';
}

function sandboxReport(evidence) {
  if (!evidence.normalized_signature) return 'No normalized sandbox failure was recorded.';
  return [
    `Collection: ${evidence.setup_valid ? 'succeeded' : 'failed'}`,
    `Candidate execution: ${evidence.test_failed ? 'failed as expected' : 'did not produce a failing test'}`,
    `Normalized crash: ${evidence.normalized_signature}`,
    `Repository frame: ${evidence.relevant_frame_matches ? 'confirmed' : 'not confirmed'}`,
    `Failure origin: ${evidence.failure_origin || 'not classified'}`,
  ].join('\n');
}

function groundedReport(evidence) {
  const proof = evidence.silent_output;
  if (!proof) return 'This investigation did not use a contract-backed silent-output proof.';
  const pairs = (values) => (values || []).map((value) => `${value.name}=${value.minor}`).join(', ') || '(none)';
  const inputs = (proof.input_values || []).map((value) => `${value.name}=${value.value}`).join(', ') || '(none)';
  return [
    `Verified: ${proof.probe_verified ? 'yes' : 'no'}`,
    `Policy: ${proof.policy_id}`,
    `Repository contract: ${proof.contract_path}`,
    `Contract hash: ${proof.contract_sha256 || 'unavailable'}`,
    `Contract text: ${proof.contract_anchor}`,
    `Inputs: ${inputs}`,
    `Deterministic expected values: ${pairs(proof.expected_values)}`,
    `Observed product values: ${pairs(proof.observed_values)}`,
    proof.verification_error ? `Verification issue: ${proof.verification_error}` : 'Verification issue: none',
  ].join('\n');
}

function replayReport(replays) {
  if (!replays.length) return 'No clean replay executions were recorded.';
  return replays.map((replay, index) => `Replay ${index + 1}: ${replay.normalized_signature || 'no signature'}\n  Collection: ${replay.setup_valid ? 'succeeded' : 'failed'}\n  Test: ${replay.test_failed ? 'failed' : 'did not fail'}`).join('\n\n');
}

function renderScoreBreakdown(verdict) {
  const list = dom('score-breakdown');
  list.replaceChildren();
  const rows = [...(verdict.rationale || []), ...(verdict.disqualifiers || [])];
  if (!rows.length) rows.push('No score rationale was recorded.');
  rows.forEach((reason) => {
    const item = document.createElement('li');
    item.textContent = reason;
    list.append(item);
  });
}

async function loadHistory() {
  const loading = dom('history-loading');
  const empty = dom('history-empty');
  const error = dom('history-error');
  const list = dom('history-list');
  const clearButton = dom('clear-history');
  loading.hidden = false;
  empty.hidden = true;
  error.hidden = true;
  clearButton.hidden = true;
  list.replaceChildren();
  try {
    const { runs } = await api('/runs');
    if (!runs.length) {
      empty.hidden = false;
      return;
    }
    clearButton.hidden = false;
    const completeRuns = await Promise.all(runs.map(async (run) => {
      try { return await api(`/runs/${encodeURIComponent(run.run_id)}`); }
      catch { return { manifest: run, ticket: { title: 'Evidence bundle', id: run.run_id }, verdict: { status: run.status, evidence_score: run.score } }; }
    }));
    completeRuns.forEach((run) => list.append(historyRow(run)));
  } catch (requestError) {
    error.textContent = `Unable to load investigation history. ${requestError.message}`;
    error.hidden = false;
  } finally {
    loading.hidden = true;
  }
}

async function clearHistory() {
  const button = dom('clear-history');
  if (!window.confirm('Clear all stored investigation history for this local demo? This permanently removes evidence bundles from this computer and does not change Jira.')) {
    return;
  }

  button.disabled = true;
  button.textContent = 'Clearing history';
  try {
    const result = await api('/runs', { method: 'DELETE' });
    announce(`Cleared ${result.deleted_run_count} stored investigation ${result.deleted_run_count === 1 ? 'bundle' : 'bundles'}.`);
    await loadHistory();
  } catch (error) {
    const message = dom('history-error');
    message.textContent = `Unable to clear investigation history. ${error.message}`;
    message.hidden = false;
  } finally {
    button.disabled = false;
    button.textContent = 'Clear history';
  }
}

function historyRow(run) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'history-row';
  const title = run.ticket?.title || 'Evidence bundle';
  const created = run.manifest?.created_at;
  button.innerHTML = '<span class="history-ticket"><strong></strong><span></span></span><span class="history-status"></span><span class="history-score"></span><span class="history-arrow" aria-hidden="true">›</span>';
  button.querySelector('.history-ticket strong').textContent = title;
  button.querySelector('.history-ticket span').textContent = created ? formatDate(created) : 'Time unavailable';
  const status = button.querySelector('.history-status');
  status.textContent = verdictPresentation(run.verdict.status).label;
  status.classList.add(verdictPresentation(run.verdict.status).className);
  button.querySelector('.history-score').textContent = `${run.verdict.evidence_score}/100`;
  button.addEventListener('click', () => { renderEvidence(run); showView('evidence'); });
  return button;
}

function formatDate(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? 'Time unavailable' : date.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
}

document.querySelectorAll('[data-view-target]').forEach((button) => {
  button.addEventListener('click', () => showView(button.dataset.viewTarget));
});
dom('investigation-form').addEventListener('submit', submitInvestigation);
dom('repository-kind').addEventListener('change', (event) => setRepositoryFields(event.target.value));
dom('open-proof').addEventListener('click', () => { if (state.activeRun) showView('evidence'); });
dom('refresh-activity').addEventListener('click', loadActivity);
dom('clear-history').addEventListener('click', clearHistory);
setRepositoryFields(dom('repository-kind').value);
showView('submit');
