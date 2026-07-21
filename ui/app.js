const dom = (id) => document.getElementById(id);

const state = {
  activeView: 'submit',
  job: null,
  activeRun: null,
  activePlan: null,
  eventSource: null,
  statusPoll: null,
  elapsedTimer: null,
  activityPoll: null,
  fixPoll: null,
  publicationPoll: null,
  startedAt: null,
};

const stageNames = new Set(['github_checkout', 'form_hypothesis', 'candidate_sandbox', 'replay_1', 'replay_2', 'verdict', 'jira_comment', 'yolo']);

async function api(url, options = {}) {
  const response = await fetch(url, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = typeof payload.detail === 'string' ? payload.detail : `Request failed (${response.status}).`;
    throw new Error(detail);
  }
  return payload;
}

async function loadYoloMode() {
  const input = dom('yolo-mode');
  const status = dom('yolo-mode-status');
  try {
    const settings = await api('/automation/yolo');
    renderYoloMode(settings);
  } catch (error) {
    input.checked = false;
    input.disabled = true;
    status.textContent = `Automation unavailable: ${error.message}`;
  }
}

function renderYoloMode(settings) {
  const input = dom('yolo-mode');
  const status = dom('yolo-mode-status');
  input.checked = Boolean(settings.enabled);
  input.disabled = !settings.available;
  status.textContent = settings.enabled
    ? 'Autonomously creates draft PRs for future reproduced Jira tickets'
    : settings.available
      ? 'Off: enable for autonomous Jira-to-draft-PR handling'
      : (settings.reason || 'GitHub draft-PR publishing is not configured');
}

async function changeYoloMode(event) {
  const input = event.currentTarget;
  const enabled = input.checked;
  if (enabled && !window.confirm('Enable YOLO mode for future Jira tickets? A reproduced ticket will automatically validate a fix, push a new devsleuth/fix branch, create a draft GitHub pull request, and comment its link on Jira. It will not merge or deploy code.')) {
    input.checked = false;
    return;
  }
  input.disabled = true;
  try {
    const settings = await api('/automation/yolo', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled, confirm: enabled }),
    });
    renderYoloMode(settings);
    announce(enabled ? 'YOLO mode enabled for future Jira tickets.' : 'YOLO mode disabled for future Jira tickets.');
  } catch (error) {
    input.checked = !enabled;
    input.disabled = false;
    dom('yolo-mode-status').textContent = `YOLO mode unchanged: ${error.message}`;
  }
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
  } else if (event.state === 'completed' || event.state === 'skipped') {
    stage.classList.add('is-complete');
    stateLabel.textContent = event.state === 'skipped' ? 'Not requested' : 'Complete';
    announce(event.state === 'skipped' ? event.label : `${event.label} complete.`);
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
  setReproductionWorkflowStatus(verdict.status);
  configureFixPreparation(run);
  document.querySelectorAll('.evidence-details details').forEach((detail) => { detail.open = false; });
}

function configureFixPreparation(run) {
  stopFixPolling();
  stopPublicationPolling();
  const button = dom('prepare-fix');
  const publishButton = dom('publish-pr');
  const message = dom('fix-message');
  const canPrepare = run.verdict?.status === 'REPRODUCED' && githubSourceFromRun(run);
  dom('fix-workflow-section').hidden = !canPrepare;
  button.disabled = false;
  button.textContent = 'Prepare & validate fix';
  publishButton.hidden = true;
  publishButton.disabled = true;
  dom('publish-message').hidden = true;
  dom('publish-message').textContent = '';
  dom('published-pr-card').hidden = true;
  dom('published-pr-link').removeAttribute('href');
  message.hidden = true;
  message.textContent = '';
  dom('fix-progress-card').hidden = true;
  dom('fix-plan-details').hidden = true;
  dom('fix-diff-details').hidden = true;
  dom('fix-regression-details').hidden = true;
  state.activePlan = null;
  if (canPrepare) {
    setWorkflowStatus('fix-workflow-status', 'READY', 'pending');
    loadExistingFixPlan(run);
  }
}

function githubSourceFromRun(run) {
  const repoRef = run.ticket?.repo_ref || '';
  const at = repoRef.lastIndexOf('@');
  if (at < 3) return null;
  const repository = repoRef.slice(0, at);
  const ref = repoRef.slice(at + 1);
  return /^[A-Za-z0-9][A-Za-z0-9_.-]{0,38}\/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$/.test(repository) && ref ? { repository, ref } : null;
}

async function prepareFix() {
  const run = state.activeRun;
  const source = githubSourceFromRun(run || {});
  if (!run?.manifest?.run_id || !source) return;
  if (!window.confirm('Prepare and validate a local fix from this verified reproduction? This makes one model call and runs sandbox validation in a disposable checkout. It will not push a branch or create a GitHub pull request.')) {
    return;
  }
  const button = dom('prepare-fix');
  const message = dom('fix-message');
  button.disabled = true;
  button.textContent = 'Preparing & validating';
  setWorkflowStatus('fix-workflow-status', 'IN PROGRESS', 'progress');
  message.hidden = false;
  message.textContent = 'Generating and validating a source-only patch in a disposable checkout. No GitHub changes will be made.';
  try {
    const job = await api(`/runs/${encodeURIComponent(run.manifest.run_id)}/fixes`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ repository: { kind: 'github', repository: source.repository, ref: source.ref }, base_branch: source.ref }),
    });
    renderFixProgress(job);
    pollFixPreparation(job, button, message);
  } catch (error) {
    message.textContent = `Fix preparation did not start. ${error.message}`;
    button.disabled = false;
    button.textContent = 'Prepare & validate fix';
    setWorkflowStatus('fix-workflow-status', 'NEEDS REVIEW', 'review');
  }
}

function stopFixPolling() {
  if (state.fixPoll?.timer) window.clearTimeout(state.fixPoll.timer);
  state.fixPoll = null;
}

function pollFixPreparation(job, button, message) {
  stopFixPolling();
  const session = { timer: null };
  state.fixPoll = session;
  const poll = async () => {
    if (state.fixPoll !== session) return;
    try {
      const current = await api(job.status_url);
      if (state.fixPoll !== session) return;
      renderFixProgress(current);
      if (current.status === 'queued' || current.status === 'running') {
        session.timer = window.setTimeout(poll, 1500);
        return;
      }
      stopFixPolling();
      button.disabled = false;
      button.textContent = 'Prepare & validate fix';
      if (current.status === 'done') {
        message.textContent = 'Validated local PR plan is ready. It has not been published to GitHub.';
        if (current.plan_url) showFixPlan(await api(current.plan_url));
        announce('Validated local pull-request plan is ready and has not been published.');
      } else {
        message.textContent = `Fix preparation did not validate. ${current.error || 'Review the job details.'}`;
      }
    } catch (error) {
      if (state.fixPoll !== session) return;
      stopFixPolling();
      button.disabled = false;
      button.textContent = 'Prepare & validate fix';
      message.textContent = `Unable to read fix preparation status. ${error.message}`;
    }
  };
  poll();
}

const fixStages = [
  ['repository_checkout', 'Pin reproduced commit', 'Clone the exact GitHub branch and verify the reproduced commit.'],
  ['patch_generation', 'Generate source fix', 'Ask the model for one minimal source-only Git diff.'],
  ['regression_before', 'Prove failure before patch', 'Run the verified regression in the restricted sandbox.'],
  ['patch_apply', 'Apply fix in isolation', 'Apply the proposed diff only in a disposable checkout.'],
  ['regression_after', 'Prove fix after patch', 'Run the same regression again and require it to pass.'],
  ['suite_validation', 'Run repository suite', 'Run the repository pytest suite in the restricted sandbox.'],
  ['pr_plan', 'Prepare draft PR plan', 'Write a local branch, test, patch, and PR-body plan.'],
];

function renderFixProgress(job) {
  const card = dom('fix-progress-card');
  const status = dom('fix-progress-status');
  const list = dom('fix-stage-list');
  const progress = job.progress || {};
  const events = job.events || [];
  card.hidden = false;
  dom('fix-progress-title').textContent = job.status === 'done' ? 'Local fix plan validated' : job.status === 'failed' ? 'Fix preparation needs review' : 'Preparing a local fix';
  dom('fix-progress-summary').textContent = job.status === 'done'
    ? 'The source patch, regression test, and suite gate passed in disposable sandboxes. Nothing has been published to GitHub.'
    : job.status === 'failed'
      ? (job.error || 'The fix did not pass a required validation gate. No GitHub changes were made.')
      : (progress.label || 'Preparing a verified fix in a disposable checkout.');
  status.className = `history-status ${job.status === 'done' ? 'is-reproduced' : job.status === 'failed' ? 'is-job-failed' : 'is-job-running'}`;
  status.textContent = job.status === 'done' ? 'Validated locally' : job.status === 'failed' ? 'Needs review' : 'In progress';
  if (job.status === 'done') setWorkflowStatus('fix-workflow-status', 'VALIDATED', 'success');
  else if (job.status === 'failed') setWorkflowStatus('fix-workflow-status', 'NEEDS REVIEW', 'review');
  else setWorkflowStatus('fix-workflow-status', 'IN PROGRESS', 'progress');
  list.replaceChildren();
  fixStages.forEach(([id, title, description]) => {
    const matching = events.filter((event) => event.stage === id);
    const latest = matching[matching.length - 1];
    const state = latest?.state === 'completed' ? 'complete' : latest?.state === 'started' ? 'active' : job.status === 'failed' && progress.stage === id ? 'failed' : 'waiting';
    const row = document.createElement('li');
    row.className = `stage ${state === 'complete' ? 'is-complete' : state === 'active' ? 'is-active' : state === 'failed' ? 'is-failed' : ''}`;
    row.innerHTML = '<span class="stage-marker" aria-hidden="true"></span><div><strong></strong><span></span></div><small></small>';
    row.querySelector('strong').textContent = title;
    row.querySelector('div span').textContent = latest?.label || description;
    row.querySelector('small').textContent = state === 'complete' ? 'Done' : state === 'active' ? 'In progress' : state === 'failed' ? 'Failed' : 'Waiting';
    list.append(row);
  });
}

async function loadExistingFixPlan(run) {
  if (!run.manifest?.run_id || run.verdict?.status !== 'REPRODUCED') return;
  try {
    const job = await api(`/runs/${encodeURIComponent(run.manifest.run_id)}/fix-status`);
    if (job.status === 'queued' || job.status === 'running') {
      const button = dom('prepare-fix');
      const message = dom('fix-message');
      button.disabled = true;
      button.textContent = 'Fix validation in progress';
      message.hidden = false;
      message.textContent = 'Fix validation is already in progress. This view will update automatically.';
      renderFixProgress(job);
      pollFixPreparation(job, button, message);
      return;
    }
    if (job.status === 'done' && job.plan_url) {
      renderFixProgress({
        ...job,
        events: job.events?.length ? job.events : fixStages.map(([stage]) => ({ stage, state: 'completed' })),
      });
      showFixPlan(await api(job.plan_url));
    }
  } catch (_) {
    // A reproduced run does not need to have a prepared plan yet, and the
    // primary evidence view remains usable if transient status polling fails.
  }
}

function showFixPlan(plan) {
  state.activePlan = plan;
  const lines = [
    `Target: ${plan.repository}@${plan.base_branch}`,
    `Pinned commit: ${plan.base_commit}`,
    `Proposed branch: ${plan.head_branch}`,
    `Regression test: ${plan.regression_path}`,
  ];
  dom('fix-plan-result').textContent = lines.join('\n');
  renderGitDiff(plan.patch || '');
  dom('fix-regression-result').textContent = plan.regression_content || '(unavailable)';
  dom('fix-plan-details').hidden = false;
  dom('fix-diff-details').hidden = false;
  dom('fix-regression-details').hidden = false;
  if (plan.publication?.pull_request?.url) {
    showPublishedPullRequest(plan.publication);
    return;
  }
  const publishButton = dom('publish-pr');
  dom('published-pr-card').hidden = true;
  dom('published-pr-link').removeAttribute('href');
  const capability = plan.publication_capability || { available: false, reason: 'GitHub publishing is not configured for this service.' };
  publishButton.hidden = false;
  publishButton.disabled = !capability.available;
  publishButton.textContent = capability.available ? 'Create draft PR' : 'Draft PR publishing disabled';
  publishButton.title = capability.available ? '' : capability.reason;
  if (!capability.available) {
    const message = dom('publish-message');
    message.hidden = false;
    message.textContent = `${capability.reason} The validated diff remains local and reviewable.`;
  }
  setWorkflowStatus('fix-workflow-status', 'VALIDATED', 'success');
}

function renderGitDiff(patch) {
  const target = dom('fix-diff-result');
  target.replaceChildren();
  (patch || '(unavailable)').split('\n').forEach((line) => {
    const row = document.createElement('span');
    row.className = 'git-diff-line';
    if (line.startsWith('+') && !line.startsWith('+++')) row.classList.add('is-add');
    else if (line.startsWith('-') && !line.startsWith('---')) row.classList.add('is-remove');
    else if (line.startsWith('diff --git') || line.startsWith('index ') || line.startsWith('---') || line.startsWith('+++') || line.startsWith('@@')) row.classList.add('is-header');
    row.textContent = line || ' ';
    target.append(row);
  });
}

async function publishDraftPullRequest() {
  const plan = state.activePlan;
  if (!plan?.plan_id) return;
  if (!window.confirm(`Create a draft GitHub pull request for ${plan.repository}? The service will recheck that ${plan.base_branch} still points to the validated commit before it pushes the proposed branch.`)) {
    return;
  }
  const button = dom('publish-pr');
  const message = dom('publish-message');
  button.disabled = true;
  button.textContent = 'Creating draft PR';
  message.hidden = false;
  message.textContent = 'Rechecking the validated base commit and creating a draft pull request. This is the first GitHub write.';
  try {
    const job = await api(`/pull-request-plans/${encodeURIComponent(plan.plan_id)}/publish`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ confirm: true }),
    });
    renderPublicationProgress(job);
    pollPublication(job, button, message);
  } catch (error) {
    message.textContent = `Draft pull request was not started. ${error.message}`;
    button.disabled = false;
    button.textContent = 'Create draft PR';
  }
}

function stopPublicationPolling() {
  if (state.publicationPoll?.timer) window.clearTimeout(state.publicationPoll.timer);
  state.publicationPoll = null;
}

function pollPublication(job, button, message) {
  stopPublicationPolling();
  const session = { timer: null };
  state.publicationPoll = session;
  const poll = async () => {
    if (state.publicationPoll !== session) return;
    try {
      const current = await api(job.status_url);
      if (state.publicationPoll !== session) return;
      renderPublicationProgress(current);
      if (current.status === 'queued' || current.status === 'running') {
        session.timer = window.setTimeout(poll, 1500);
        return;
      }
      stopPublicationPolling();
      button.disabled = false;
      button.textContent = 'Create draft PR';
      if (current.status === 'done' && current.publication) {
        showPublishedPullRequest(current.publication);
        message.textContent = 'Draft pull request created. The Jira backlink status is shown below.';
        announce('Draft pull request created from the validated fix.');
      } else {
        message.textContent = `Draft pull request could not be created. ${current.error || 'Review the publication details.'}`;
      }
    } catch (error) {
      if (state.publicationPoll !== session) return;
      stopPublicationPolling();
      button.disabled = false;
      button.textContent = 'Create draft PR';
      message.textContent = `Unable to read draft pull-request status. ${error.message}`;
    }
  };
  poll();
}

function renderPublicationProgress(job) {
  const message = dom('publish-message');
  message.hidden = false;
  if (job.status === 'done') {
    message.textContent = 'Draft pull request created. Recording the final publication result.';
  } else if (job.status === 'failed') {
    message.textContent = job.error || 'Draft pull-request publication failed. No additional changes will be attempted.';
  } else {
    message.textContent = job.progress?.label || 'Creating the draft pull request.';
  }
}

function showPublishedPullRequest(publication) {
  const pullRequest = publication.pull_request || {};
  let url;
  try {
    url = new URL(pullRequest.url);
  } catch (_) {
    return;
  }
  if (url.protocol !== 'https:') return;
  const link = dom('published-pr-link');
  link.href = url.href;
  link.textContent = `Open draft PR #${pullRequest.number || ''}`.trim();
  const jira = publication.jira_comment || {};
  const jiraText = jira.status === 'posted'
    ? `Draft PR #${pullRequest.number || ''} is open. Its evidence link was posted to Jira ${jira.issue_key || ''}.`.trim()
    : jira.status === 'failed'
      ? `Draft PR #${pullRequest.number || ''} is open, but the Jira backlink needs review: ${jira.error || 'comment failed'}`
      : `Draft PR #${pullRequest.number || ''} is open and ready for review.`;
  dom('published-pr-summary').textContent = jiraText;
  dom('published-pr-card').hidden = false;
  dom('publish-pr').hidden = true;
  setWorkflowStatus('fix-workflow-status', 'DRAFT PR OPEN', 'success');
}

function setReproductionWorkflowStatus(verdictStatus) {
  if (verdictStatus === 'REPRODUCED') {
    setWorkflowStatus('reproduction-workflow-status', 'VERIFIED', 'success');
    return;
  }
  if (verdictStatus === 'NEED_INFO') {
    setWorkflowStatus('reproduction-workflow-status', 'NEEDS INFO', 'review');
    return;
  }
  if (verdictStatus === 'INCONCLUSIVE') {
    setWorkflowStatus('reproduction-workflow-status', 'INCONCLUSIVE', 'review');
    return;
  }
  setWorkflowStatus('reproduction-workflow-status', 'NOT REPRODUCED', 'review');
}

function setWorkflowStatus(id, label, tone) {
  const badge = dom(id);
  badge.textContent = label;
  badge.className = `workflow-section-status is-${tone}`;
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
      catch {
        return {
          manifest: run,
          ticket: { title: 'Evidence bundle', id: run.run_id },
          verdict: { status: run.status, evidence_score: run.score },
          fix: run.fix,
        };
      }
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
  const presentation = historyPresentation(run);
  status.textContent = presentation.label;
  status.classList.add(presentation.className);
  button.querySelector('.history-score').textContent = `${run.verdict.evidence_score}/100`;
  button.addEventListener('click', () => { renderEvidence(run); showView('evidence'); });
  return button;
}

function historyPresentation(run) {
  if (run.fix?.status === 'DRAFT_PR_OPEN') {
    return { label: run.fix.label || 'DRAFT PR OPEN', className: 'is-draft-pr-open' };
  }
  if (run.fix?.status === 'FIX_VALIDATED') {
    return { label: run.fix.label || 'FIX VALIDATED', className: 'is-fix-validated' };
  }
  return verdictPresentation(run.verdict.status);
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
dom('prepare-fix').addEventListener('click', prepareFix);
dom('publish-pr').addEventListener('click', publishDraftPullRequest);
dom('yolo-mode').addEventListener('change', changeYoloMode);
setRepositoryFields(dom('repository-kind').value);
loadYoloMode();
showView('submit');
