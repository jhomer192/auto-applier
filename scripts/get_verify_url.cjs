#!/usr/bin/env node
// One-shot: extract NEOGOV/governmentjobs verification URL from latest email
const { ImapFlow } = require('imapflow');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const envPath = path.join(ROOT, '.env');
for (const line of fs.readFileSync(envPath, 'utf8').split('\n')) {
  const m = line.match(/^([A-Z_][A-Z0-9_]*)=(.+)$/);
  if (m && !process.env[m[1]]) process.env[m[1]] = m[2].trim();
}

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
    for await (const msg of client.fetch({ since, from: 'governmentjobs' }, { source: true }, { uid: true })) {
      const rawBody = msg.source?.toString() || '';
      // Try plain text first
      const urlMatch = rawBody.match(/https?:\/\/www\.governmentjobs\.com\/[^\s"'<>\r\n]+VerifyAccount[^\s"'<>\r\n]*/);
      if (urlMatch) {
        let url = urlMatch[0].replace(/=\r?\n/g, '').replace(/=([0-9A-F]{2})/gi, (_, h) => String.fromCharCode(parseInt(h, 16)));
        console.log('VERIFY_URL: ' + url);
        return;
      }
      // Try base64 parts
      const b64Parts = rawBody.match(/Content-Transfer-Encoding:\s*base64[\s\S]*?(?=(?:--|\r?\nContent-))/gi) || [];
      for (const part of b64Parts) {
        try {
          const dataOnly = part.replace(/Content-Transfer-Encoding:\s*base64\s*/i, '').replace(/Content-[^\n]+\n/gi, '').trim();
          const decoded = Buffer.from(dataOnly.replace(/\s/g, ''), 'base64').toString('utf8');
          const um = decoded.match(/https?:\/\/www\.governmentjobs\.com\/[^\s"'<>]+VerifyAccount[^\s"'<>]*/);
          if (um) { console.log('VERIFY_URL: ' + um[0]); return; }
        } catch(e) {}
      }
      console.log('URL_NOT_FOUND in email body');
    }
  } finally {
    lock.release();
  }
  await client.logout();
})().catch(e => { console.error('ERR: ' + e.message); process.exit(1); });
