#!/usr/bin/env node
// Extract Greenhouse verification code from the latest email
const { ImapFlow } = require('imapflow');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
function loadEnv() {
  const envPath = path.join(ROOT, '.env');
  if (!fs.existsSync(envPath)) return;
  for (const line of fs.readFileSync(envPath, 'utf8').split('\n')) {
    const m = line.match(/^([A-Z_][A-Z0-9_]*)=(.+)$/);
    if (m && !process.env[m[1]]) process.env[m[1]] = m[2].trim();
  }
}
loadEnv();

async function main() {
  const company = process.argv[2] || '';
  const client = new ImapFlow({
    host: process.env.IMAP_HOST,
    port: 993,
    secure: true,
    auth: { user: process.env.IMAP_USER, pass: process.env.IMAP_PASS },
    logger: false,
  });
  await client.connect();
  const lock = await client.getMailboxLock('INBOX', { readOnly: true });
  const since = new Date(Date.now() - 15 * 60000);

  const messages = [];
  for await (const msg of client.fetch(
    { since, from: 'greenhouse-mail' },
    { source: true, envelope: true, bodyParts: ['TEXT', '1', '1.1', '1.2'] },
    { uid: true }
  )) {
    messages.push(msg);
  }

  // Sort newest first
  messages.sort((a, b) => (b.envelope?.date || 0) - (a.envelope?.date || 0));

  for (const msg of messages) {
    const subject = msg.envelope?.subject || '';
    if (company && !subject.toLowerCase().includes(company.toLowerCase())) continue;

    const raw = msg.source?.toString() || '';

    // Find base64 encoded parts
    const b64parts = raw.match(/Content-Transfer-Encoding: base64\r?\n\r?\n([\s\S]+?)(?=\r?\n--|\r?\n\r?\nContent-|$)/gi) || [];
    let decoded = raw;
    for (const part of b64parts) {
      const m = part.match(/base64\r?\n\r?\n([\s\S]+)/i);
      if (m) {
        try {
          const d = Buffer.from(m[1].replace(/\s/g,''), 'base64').toString('utf8');
          decoded += ' ' + d;
        } catch(e) {}
      }
    }

    // Also try quoted-printable
    const qpDecoded = decoded.replace(/=\r?\n/g,'').replace(/=([0-9A-F]{2})/gi, (_,h) => String.fromCharCode(parseInt(h,16)));
    const stripped = qpDecoded.replace(/<[^>]+>/g,' ').replace(/&[a-z]+;/g,' ').replace(/\s+/g,' ');

    console.log(`SUBJECT: ${subject}`);

    // Try to find the 8-char code
    const codePatterns = [
      /\b([A-Z0-9]{8})\b/g,
      /security.?code[^A-Z0-9]*([A-Z0-9]{6,8})/gi,
      /\b([A-Z0-9]{6})\b/g,
    ];

    for (const pat of codePatterns) {
      const matches = [...stripped.matchAll(pat)];
      for (const m of matches) {
        // Skip common non-code patterns
        const code = m[1];
        if (/^(HTTPS?|DOCTYPE|HTML|HEAD|BODY|TABLE|EMAIL|CLICK|OPEN)/i.test(code)) continue;
        console.log(`CANDIDATE_CODE: ${code}`);
        break;
      }
      if (matches.length > 0) break;
    }

    // Print a larger snippet for manual inspection
    console.log(`SNIPPET: ${stripped.substring(0, 1000)}`);
    console.log('---');
  }

  lock.release();
  await client.logout();
}
main().catch(e => console.error('ERR:', e.message));
