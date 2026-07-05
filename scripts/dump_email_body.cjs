#!/usr/bin/env node
// Dump full body of latest governmentjobs email
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
      // Decode base64 parts and print all URLs
      const b64Parts = rawBody.split(/Content-Transfer-Encoding:\s*base64/i);
      for (let i = 1; i < b64Parts.length; i++) {
        try {
          const clean = b64Parts[i].replace(/^[\s\S]*?\n\n/, '').split(/\n--/)[0].replace(/\s/g, '');
          const decoded = Buffer.from(clean, 'base64').toString('utf8');
          const urls = decoded.match(/https?:\/\/[^\s"'<>]+/g) || [];
          urls.forEach(u => console.log('URL:', u));
          // Also print text
          const text = decoded.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
          if (text.length > 0) console.log('TEXT:', text.substring(0, 1000));
        } catch(e) {}
      }
      // Also print quoted-printable URLs
      const qpBody = rawBody.replace(/=\r?\n/g, '').replace(/=([0-9A-F]{2})/gi, (_, h) => String.fromCharCode(parseInt(h, 16)));
      const urls = qpBody.match(/https?:\/\/[^\s"'<>]+/g) || [];
      urls.forEach(u => console.log('QP_URL:', u));
    }
  } finally {
    lock.release();
  }
  await client.logout();
})().catch(e => { console.error('ERR: ' + e.message); process.exit(1); });
