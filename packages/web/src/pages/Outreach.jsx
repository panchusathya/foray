import { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';

import { Badge, Empty, ErrorBox, Loading, PageHeader } from '../components/common.jsx';
import { api } from '../lib/api.js';
import { useAsync } from '../lib/useAsync.js';

const STARTER_STEPS = [
  {
    delay_days: 0,
    subject_template: '{{ subject_hint | A quick note }}',
    body_template:
      'Hi {{ first_name }},\n\n{{ opening_line }}\n\n{{ why_relevant }}\n\n' +
      "We're hiring a {{ role_title | senior engineer }} and I'd love 15 minutes to walk you " +
      'through it. Open to a chat this week?\n\nBest,\n{{ sender_name | The Talent Team }}',
  },
  {
    delay_days: 4,
    subject_template: 'Re: {{ subject_hint | A quick note }}',
    body_template:
      'Hi {{ first_name }},\n\nFollowing up on my note about the {{ role_title | role }} — happy ' +
      "to send over the team and scope details if that's easier than a call.\n\n" +
      '{{ sender_name | The Talent Team }}',
  },
];

export function OutreachPage() {
  const sequences = useAsync(() => api.listSequences(), []);
  const projects = useAsync(() => api.listProjects(), []);
  const candidates = useAsync(() => api.listCandidates(''), []);
  const mailboxes = useAsync(() => api.listMailboxes(), []);
  const [searchParams, setSearchParams] = useSearchParams();

  const [error, setError] = useState(null);
  const [status, setStatus] = useState('');
  const [preview, setPreview] = useState(null);
  const [form, setForm] = useState({
    name: 'Role outreach — 2 touch',
    project_id: '',
    steps: STARTER_STEPS,
  });

  const mailboxNotice = searchParams.get('mailbox');
  useEffect(() => {
    if (!mailboxNotice) return;
    if (mailboxNotice === 'connected') {
      setStatus('Outlook mailbox connected.');
      mailboxes.reload();
    } else {
      setError(new Error(searchParams.get('reason') || 'Could not connect Outlook'));
    }
    setSearchParams({}, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mailboxNotice]);

  async function connectOutlook() {
    setError(null);
    try {
      const { url } = await api.microsoftAuthorize();
      window.location.href = url;
    } catch (caught) {
      setError(caught);
    }
  }

  async function disconnectOutlook(id) {
    setError(null);
    try {
      await api.disconnectMailbox(id);
      mailboxes.reload();
      setStatus('Outlook mailbox disconnected.');
    } catch (caught) {
      setError(caught);
    }
  }

  function updateStep(index, patch) {
    setForm((previous) => ({
      ...previous,
      steps: previous.steps.map((step, i) => (i === index ? { ...step, ...patch } : step)),
    }));
  }

  async function createSequence(event) {
    event.preventDefault();
    setError(null);
    try {
      await api.createSequence({
        name: form.name,
        project_id: form.project_id || null,
        steps: form.steps,
      });
      setStatus('Sequence created');
      sequences.reload();
    } catch (caught) {
      setError(caught);
    }
  }

  async function previewFor(sequenceId) {
    const candidate = (candidates.data ?? [])[0];
    if (!candidate) {
      setError(new Error('Import candidates first'));
      return;
    }
    setError(null);
    try {
      setPreview(await api.previewSequence(sequenceId, { candidate_id: candidate.id, step_order: 0 }));
    } catch (caught) {
      setError(caught);
    }
  }

  async function run(sequenceId) {
    setError(null);
    setStatus('');
    try {
      const result = await api.runSequence(sequenceId);
      setStatus(`Sent ${result.sent}, failed ${result.failed}, completed ${result.completed}`);
    } catch (caught) {
      setError(caught);
    }
  }

  return (
    <div className="stack">
      <PageHeader
        title="Outreach"
        subtitle="Multi-step sequences. Personalisation variables are filled per candidate before each send."
      />
      <ErrorBox error={error} />
      {status ? <div className="card small">{status}</div> : null}

      <div className="card spread">
        <div>
          <strong className="small">Sending mailbox</strong>
          <div className="small muted">
            {(mailboxes.data?.connections ?? [])[0]
              ? `${mailboxes.data.connections[0].mailbox_email} · ${mailboxes.data.connections[0].status}`
              : mailboxes.data?.env_mailbox
                ? `App-only mailbox ${mailboxes.data.env_mailbox}`
                : mailboxes.data?.configured
                  ? 'No Outlook mailbox connected. Sequences still send via console/SMTP until you connect one and set EMAIL_PROVIDER=graph.'
                  : 'Outlook is not configured. Set GRAPH_CLIENT_ID to connect a mailbox, or keep using console/SMTP.'}
          </div>
        </div>
        <div className="row">
          {(mailboxes.data?.connections ?? [])[0] ? (
            <button
              type="button"
              className="tiny ghost"
              onClick={() => disconnectOutlook(mailboxes.data.connections[0].id)}
            >
              Disconnect
            </button>
          ) : (
            <button type="button" className="tiny" onClick={connectOutlook} disabled={!mailboxes.data?.configured}>
              Connect Outlook
            </button>
          )}
        </div>
      </div>

      <form className="card stack" onSubmit={createSequence}>
        <strong className="small">New sequence</strong>
        <div className="grid two">
          <input
            type="text"
            value={form.name}
            onChange={(event) => setForm({ ...form, name: event.target.value })}
            placeholder="Sequence name"
          />
          <select
            value={form.project_id}
            onChange={(event) => setForm({ ...form, project_id: event.target.value })}
          >
            <option value="">No project</option>
            {(projects.data ?? []).map((project) => (
              <option key={project.id} value={project.id}>
                {project.name}
              </option>
            ))}
          </select>
        </div>

        {form.steps.map((step, index) => (
          <div className="card stack" key={index}>
            <div className="spread">
              <strong className="small">Step {index + 1}</strong>
              <label className="small muted">
                send after{' '}
                <input
                  type="number"
                  min="0"
                  style={{ width: 70, display: 'inline-block' }}
                  value={step.delay_days}
                  onChange={(event) =>
                    updateStep(index, { delay_days: Number(event.target.value) })
                  }
                />{' '}
                days
              </label>
            </div>
            <input
              type="text"
              value={step.subject_template}
              onChange={(event) => updateStep(index, { subject_template: event.target.value })}
              placeholder="Subject"
            />
            <textarea
              value={step.body_template}
              onChange={(event) => updateStep(index, { body_template: event.target.value })}
              rows={7}
            />
          </div>
        ))}

        <div className="row">
          <button type="submit" className="primary">
            Create sequence
          </button>
          <span className="small muted">
            Variables: {'{{ first_name }} {{ current_company }} {{ opening_line }} {{ why_relevant }} {{ top_skill }}'}
          </span>
        </div>
      </form>

      {sequences.loading ? <Loading /> : null}
      {!sequences.loading && !(sequences.data ?? []).length ? (
        <Empty>No sequences yet.</Empty>
      ) : (
        <div className="stack">
          {(sequences.data ?? []).map((sequence) => (
            <div className="card" key={sequence.id}>
              <div className="spread">
                <div>
                  <strong>{sequence.name}</strong>
                  <div className="small muted">
                    {sequence.steps.length} steps ·{' '}
                    {sequence.steps.map((step) => `day ${step.delay_days}`).join(' → ')}
                  </div>
                </div>
                <div className="row">
                  <Badge tone={sequence.status === 'active' ? 'strong' : ''}>{sequence.status}</Badge>
                  <button type="button" className="tiny" onClick={() => previewFor(sequence.id)}>
                    Preview
                  </button>
                  <button type="button" className="tiny" onClick={() => run(sequence.id)}>
                    Run due steps
                  </button>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      {preview ? (
        <div className="card stack">
          <div className="spread">
            <strong className="small">Preview</strong>
            <button type="button" className="tiny ghost" onClick={() => setPreview(null)}>
              close
            </button>
          </div>
          <div className="small muted">Subject</div>
          <div>{preview.subject}</div>
          <div className="small muted">Body</div>
          <pre style={{ whiteSpace: 'pre-wrap', margin: 0 }}>{preview.body}</pre>
          <div className="small muted">
            Personalised by: {preview.personalization?.personalized_by ?? 'unknown'}
          </div>
        </div>
      ) : null}
    </div>
  );
}
