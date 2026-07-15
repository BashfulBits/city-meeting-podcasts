const PREFIX = "City Meeting Podcasts";

function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function layout(title, content, projectUrl) {
  return `<!doctype html><html><body style="margin:0;background:#f7f7f8;color:#1d1d20;font-family:Arial,sans-serif"><main style="max-width:600px;margin:0 auto;padding:32px 24px"><h1 style="font-size:22px">${escapeHtml(title)}</h1><div style="font-size:16px;line-height:1.5">${content}</div><hr style="border:0;border-top:1px solid #ddd;margin:28px 0"><p style="font-size:13px;color:#555">City Meeting Podcasts · <a href="${escapeHtml(projectUrl)}">${escapeHtml(projectUrl)}</a><br>Reply to this email if you need to correct your request.</p></main></body></html>`;
}

const messages = {
  submission_received: ({ issueUrl }) => ({
    subject: `${PREFIX} — request received`,
    text: `Thanks for your city request. We created a public tracking issue: ${issueUrl}\n\nDiscovery normally runs overnight. You can follow progress on that issue.`,
    html: (projectUrl) => layout("Your request was received", `<p>Thanks for your city request.</p><p>We created a public tracking issue: <a href="${escapeHtml(issueUrl)}">follow your request</a>.</p><p>Discovery normally runs overnight. You can follow progress on that issue.</p>`, projectUrl),
  }),
  evidence_ready: ({ issueUrl }) => ({ subject: `${PREFIX} — research is ready`, text: `Research is ready for review: ${issueUrl}`, html: (u) => layout("Research is ready", `<p><a href="${escapeHtml(issueUrl)}">Review the evidence</a>.</p>`, u) }),
  batched_for_review: ({ prUrl }) => ({ subject: `${PREFIX} — request is in review`, text: `Your request is included in a review PR: ${prUrl}`, html: (u) => layout("Request in review", `<p>Your request is in a maintainer review PR: <a href="${escapeHtml(prUrl)}">view the PR</a>.</p>`, u) }),
  applied: ({ prUrl }) => ({ subject: `${PREFIX} — request applied`, text: `Your request was merged: ${prUrl}`, html: (u) => layout("Request applied", `<p>Your request was merged: <a href="${escapeHtml(prUrl)}">view the change</a>.</p>`, u) }),
  needs_more_information: ({ issueUrl }) => ({ subject: `${PREFIX} — more information needed`, text: `Please check the request for needed information: ${issueUrl}`, html: (u) => layout("More information needed", `<p>Please check <a href="${escapeHtml(issueUrl)}">your request</a> for the information needed.</p>`, u) }),
  research_only: ({ issueUrl }) => ({ subject: `${PREFIX} — provider research recorded`, text: `We found a provider that needs further support work: ${issueUrl}`, html: (u) => layout("Research recorded", `<p>We found a provider that needs further support work. <a href="${escapeHtml(issueUrl)}">View the research</a>.</p>`, u) }),
  evidence_expired: ({ issueUrl }) => ({ subject: `${PREFIX} — research needs a refresh`, text: `Earlier evidence has expired and will be refreshed: ${issueUrl}`, html: (u) => layout("Research refresh needed", `<p>Earlier evidence has expired and will be refreshed. <a href="${escapeHtml(issueUrl)}">View the request</a>.</p>`, u) }),
};

export function emailTemplate(kind, data, projectUrl) {
  const message = messages[kind];
  if (!message) throw new Error("unknown email template");
  const rendered = message(data);
  return { subject: rendered.subject, text: rendered.text, html: rendered.html(projectUrl) };
}
