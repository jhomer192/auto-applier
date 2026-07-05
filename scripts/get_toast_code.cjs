const { ImapFlow } = require('imapflow');
const fs = require('fs');
const path = require('path');

const ROOT = '/opt/auto-applier';
const envPath = path.join(ROOT, '.env');
if (fs.existsSync(envPath)) {
  for (const line of fs.readFileSync(envPath, 'utf8').split('\n')) {
    const m = line.match(/^([A-Z_]+)=(.+)$/);
    if (m && !process.env[m[1]]) process.env[m[1]] = m[2].trim();
  }
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
    const since = new Date(Date.now() - 10 * 60 * 1000);
    for await (const msg of client.fetch(
      { since, from: 'toast.mail' },
      { source: true, envelope: true }
    )) {
      const body = msg.source?.toString() || '';
      // Find all digit sequences
      const digits = body.match(/\b\d{4,8}\b/g);
      console.log('Subject:', msg.envelope?.subject);
      console.log('All digit sequences:', digits);
      // Print text content
      const text = body.replace(/=\r?\n/g, '').replace(/=([0-9A-F]{2})/gi, (_, h) => String.fromCharCode(parseInt(h, 16))).replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ');
      const relevant = text.substring(0, 2000);
      console.log('TEXT:', relevant);
    }
  } finally {
    lock.release();
    await client.logout();
  }
})().catch(console.error);
