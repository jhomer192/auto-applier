#!/usr/bin/env node
const { ImapFlow } = require('imapflow');
const path = require('path');
const fs = require('fs');

const envPath = path.join('/opt/auto-applier', '.env');
for (const line of fs.readFileSync(envPath, 'utf8').split('\n')) {
  const m = line.match(/^([A-Z_]+)=(.+)$/);
  if (m && !process.env[m[1]]) process.env[m[1]] = m[2].trim();
}

const target = process.argv[2] || 'PagerDuty';
const client = new ImapFlow({
  host: process.env.IMAP_HOST,
  port: parseInt(process.env.IMAP_PORT || '993'),
  secure: true,
  auth: { user: process.env.IMAP_USER, pass: process.env.IMAP_PASS },
  logger: false,
});

(async () => {
  await client.connect();
  const lock = await client.getMailboxLock('INBOX', { readOnly: true });
  try {
    const since = new Date(Date.now() - 15 * 60000);
    const msgs = [];
    for await (const msg of client.fetch({ since, from: 'greenhouse' }, { source: true, envelope: true }, { uid: true })) {
      const subj = msg.envelope?.subject || '';
      if (!subj.toLowerCase().includes(target.toLowerCase())) continue;
      msgs.push({date: msg.envelope?.date, src: msg.source?.toString() || ''});
    }
    msgs.sort((a, b) => new Date(b.date) - new Date(a.date));
    if (msgs.length === 0) { console.log('NO_MAIL'); process.exit(0); }

    const src = msgs[0].src;
    // Split on MIME boundaries to find HTML/text parts
    const parts = src.split(/--[a-zA-Z0-9_\-]+/);
    for (const part of parts) {
      if (!part.includes('Content-Type')) continue;
      const isHtml = part.includes('text/html') || part.includes('text/plain');
      if (!isHtml) continue;
      // Get the body after the headers
      const bodyStart = part.indexOf('\r\n\r\n');
      if (bodyStart < 0) continue;
      const rawBody = part.substring(bodyStart + 4);
      const decoded = rawBody.replace(/=\r?\n/g, '').replace(/=([0-9A-F]{2})/gi, (_, h) => String.fromCharCode(parseInt(h, 16)));
      const text = decoded.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ');

      // Look for the code near "security" or code-like patterns
      const secIdx = text.search(/security code|enter.*code|your code/i);
      if (secIdx >= 0) {
        const ctx = text.substring(secIdx, secIdx + 300);
        console.log('CONTEXT: ' + ctx);
        // Find 6-8 char alphanumeric codes in the context
        const codeMatch = ctx.match(/\b([A-Z0-9]{6,8})\b/);
        if (codeMatch) console.log('CODE: ' + codeMatch[1]);
        break;
      }

      // Try to find any isolated 8-char code in the body (not headers)
      const allCodes = [...text.matchAll(/\b([A-Z0-9]{8})\b/g)].map(m => m[1]);
      const filtered = allCodes.filter(c => !['RECEIVED', 'DELIVERED', 'DKIM', 'CONTENT', 'BOUNDARY', 'ENCODING', 'TRANSFER', 'ARCHIVED'].includes(c));
      if (filtered.length > 0) {
        console.log('POSSIBLE_CODES: ' + filtered.slice(0, 5).join(', '));
      }
    }
  } finally { lock.release(); }
  await client.logout();
})().catch(e => { console.error('ERROR: ' + e.message); process.exit(1); });
