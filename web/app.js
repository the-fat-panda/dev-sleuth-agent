const dom = (id) => document.getElementById(id);
const state = { runs: [], active: null };

async function request(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`Request failed (${response.status})`);
  return response.json();
}

function short(value, limit = 12) {
  return value && value.length > limit ? `${value.slice(0, limit)}…` : value || '—';
}

function statusLabel(status) {
  return status.replaceAll('_', ' ');
}

function countSignals(verdict) {
  return (verdict.rationale || []).filter((reason) => reason.includes('(+')).length;
}

function timestamp(value) {
  return new Date(value).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function renderTimeline(events) {
  const timeline = dom('timeline');
  timeline.replaceChildren();
  for (const event of events) {
    const item = document.createElement('li');
    const time = document.createElement('time');
    time.textContent = timestamp(event.occurred_at);
    const name = document.createElement('strong');
    name.textContent = event.kind.replaceAll('_', ' ');
    const message = document.createElement('span');
    message.textContent = event.message;
    item.append(time, name, message);
    timeline.append(item);
  }
}

function renderRationale(verdict) {
  const list = dom('rationale');
  list.replaceChildren();
  const entries = [...(verdict.rationale || []), ...(verdict.disqualifiers || [])];
  for (const message of entries) {
    const item = document.createElement('li');
    item.textContent = message;
    list.append(item);
  }
}

function renderRun(run) {
  state.active = run;
  const { ticket, candidates, evidence, verdict, manifest, events } = run;
  const candidate = candidates[0] || {};
  const primary = evidence.find((item) => item.phase === 'CANDIDATE') || evidence[0] || {};
  const replays = evidence.filter((item) => item.phase === 'REPLAY');
  const badge = dom('status-badge');
  badge.className = `status-badge ${verdict.status.toLowerCase().replaceAll('_', '-')}`;
  badge.textContent = statusLabel(verdict.status);
  dom('score').textContent = `${verdict.evidence_score}/100 evidence score`;
  dom('verdict-summary').textContent = verdict.status === 'REPRODUCED'
    ? 'This failure was independently replayed in two clean containers before the verdict was issued.'
    : 'This result does not meet the threshold for a verified reproduction.';
  dom('repo-commit').textContent = short(manifest.repo_commit, 18);
  dom('sandbox-image').textContent = short(primary.environment_fingerprint?.sandbox_image, 18);
  dom('candidate-count').textContent = candidates.length;
  dom('replay-count').textContent = replays.length;
  dom('signal-count').textContent = `${countSignals(verdict)}/6`;
  dom('ticket-title').textContent = ticket.title;
  dom('ticket-id').textContent = ticket.id;
  dom('ticket-body').textContent = ticket.body;
  dom('expected-symptom').textContent = ticket.expected_error || candidate.expected_symptom || 'Not specified';
  dom('observed-signature').textContent = primary.normalized_signature || 'No matching failure signature';
  dom('candidate-code').textContent = candidate.content || '# No candidate test was generated.';
  dom('public-api').textContent = (candidate.public_api_claims || []).join(', ') || 'Not declared';
  dom('replay-command').textContent = `python -m bugagent replay --bundle .bugagent/checkpoint-3/${manifest.run_id} --repo <pinned-repository> --image ${primary.environment_fingerprint?.sandbox_image || '<immutable-sha256-image>'}`;
  renderTimeline(events);
  renderRationale(verdict);
}

async function selectRun(runId) {
  const run = await request(`/api/runs/${encodeURIComponent(runId)}`);
  renderRun(run);
}

async function boot() {
  try {
    const payload = await request('/api/runs');
    state.runs = payload.runs;
    dom('run-count').textContent = `${state.runs.length} immutable evidence bundle${state.runs.length === 1 ? '' : 's'}`;
    if (!state.runs.length) {
      dom('empty-state').hidden = false;
      return;
    }
    const select = dom('run-select');
    for (const run of state.runs) {
      const option = document.createElement('option');
      option.value = run.run_id;
      option.textContent = `${statusLabel(run.status)} · ${run.score}/100 · ${short(run.run_id, 10)}`;
      select.append(option);
    }
    select.addEventListener('change', () => selectRun(select.value));
    await selectRun(state.runs[0].run_id);
    dom('dashboard').hidden = false;
  } catch (error) {
    dom('empty-state').hidden = false;
    dom('empty-state').querySelector('p').textContent = `Unable to load evidence bundles: ${error.message}`;
  }
}

function copyText(button, text, resetLabel) {
  button.textContent = 'Copied';
  if (navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(text).catch(() => fallbackCopy(text));
  } else {
    fallbackCopy(text);
  }
  setTimeout(() => { button.textContent = resetLabel; }, 1200);
}

dom('copy-test').addEventListener('click', () => copyText(dom('copy-test'), dom('candidate-code').textContent, 'Copy test'));
dom('copy-replay').addEventListener('click', () => copyText(dom('copy-replay'), dom('replay-command').textContent, 'Copy command'));

function fallbackCopy(text) {
  const area = document.createElement('textarea');
  area.value = text;
  area.setAttribute('readonly', '');
  area.style.position = 'fixed';
  area.style.opacity = '0';
  document.body.append(area);
  area.select();
  document.execCommand('copy');
  area.remove();
}

boot();
