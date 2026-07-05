#!/usr/bin/env node
// Checks email via IMAP for verification codes, confirmations, recruiter replies.
// Usage:
//   node scripts/check_email.cjs                    # latest 5 emails from last 30m
//   node scripts/check_email.cjs --from greenhouse  # filter by sender
//   node scripts/check_email.cjs --subject "verify" # filter by subject
//   node scripts/check_email.cjs --code             # extract 4-8 char verification codes
//   node scripts/check_email.cjs --since 10m        # only emails from last 10 minutes
//
// Reads IMAP_HOST, IMAP_PORT, IMAP_USER, IMAP_PASS from ../.env

const { ImapFlow } = require('imapflow');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');

function loadEnv() {
  const envPath = path.join(ROOT, '.env');
  if (!fs.existsSync(envPath)) return;
  for (const line of fs.readFileSync(envPath, 'utf8').split('\n')) {
    const m = line.match(/^([A-Z_]+)=(.+)$/);
    if (m && !process.env[m[1]]) process.env[m[1]] = m[2].trim();
  }
}

function parseSince(s) {
  const m = s.match(/^(\d+)(m|h|d)$/);
  if (!m) return null;
  const n = parseInt(m[1]);
  const ms = { m: 60000, h: 3600000, d: 86400000 }[m[2]];
  return new Date(Date.now() - n * ms);
}

async function main() {
  loadEnv();
  const host = process.env.IMAP_HOST;
  const port = parseInt(process.env.IMAP_PORT || '993');
  const user = process.env.IMAP_USER;
  const pass = process.env.IMAP_PASS;

  if (!host || !user || !pass) {
    console.error('ERROR: set IMAP_HOST, IMAP_USER, IMAP_PASS in .env');
    process.exit(1);
  }

  const args = process.argv.slice(2);
  const fromFilter = args.includes('--from') ? args[args.indexOf('--from') + 1] : null;
  const subjectFilter = args.includes('--subject') ? args[args.indexOf('--subject') + 1] : null;
  const extractCode = args.includes('--code');
  const sinceArg = args.includes('--since') ? args[args.indexOf('--since') + 1] : '30m';
  const sinceDate = parseSince(sinceArg) || new Date(Date.now() - 30 * 60000);
  const limit = parseInt(args.includes('--limit') ? args[args.indexOf('--limit') + 1] : '5');

  const client = new ImapFlow({
    host,
    port,
    secure: port === 993,
    auth: { user, pass },
    logger: false,
  });

  try {
    await client.connect();
    // readOnly: fetching a verification code must NEVER mark the applicant's mail as
    // read. This opens their personal inbox — read-only, touch nothing.
    const lock = await client.getMailboxLock('INBOX', { readOnly: true });

    try {
      const searchCriteria = { since: sinceDate };
      if (fromFilter) searchCriteria.from = fromFilter;
      if (subjectFilter) searchCriteria.subject = subjectFilter;

      const messages = [];
      for await (const msg of client.fetch(
        { ...searchCriteria },
        { source: true, envelope: true },
        { uid: true }
      )) {
        messages.push(msg);
      }

      messages.sort((a, b) => (b.envelope?.date || 0) - (a.envelope?.date || 0));
      const recent = messages.slice(0, limit);

      if (recent.length === 0) {
        console.log('NO_MAIL: no matching emails found');
        process.exit(0);
      }

      for (const msg of recent) {
        const env = msg.envelope || {};
        const from = env.from?.[0]?.address || 'unknown';
        const subject = env.subject || '(no subject)';
        const date = env.date ? new Date(env.date).toISOString() : 'unknown';
        const body = msg.source?.toString() || '';

        console.log(`--- EMAIL ---`);
        console.log(`FROM: ${from}`);
        console.log(`SUBJECT: ${subject}`);
        console.log(`DATE: ${date}`);

        if (extractCode) {
          // Decode any base64 MIME parts (Greenhouse emails use base64 encoding)
          const b64Pattern = /Content-Transfer-Encoding:\s*base64\s*\r?\n\s*\r?\n([\s\S]*?)(?=\r?\n--|\r?\nContent-|$)/gi;
          let b64Match;
          let b64Decoded = '';
          while ((b64Match = b64Pattern.exec(body)) !== null) {
            try {
              const d = Buffer.from(b64Match[1].replace(/\s/g, ''), 'base64').toString('utf8');
              b64Decoded += ' ' + d;
              if (extractCode) process.stderr.write('B64_DECODED: ' + d.replace(/<[^>]+>/g,' ').replace(/\s+/g,' ').substring(0,500) + '\n');
            } catch(e) {}
          }
          const textBody = (body + ' ' + b64Decoded)
            .replace(/=\r?\n/g, '')
            .replace(/=([0-9A-F]{2})/gi, (_, hex) => String.fromCharCode(parseInt(hex, 16)));

          const codePatterns = [
            />\s*([A-Za-z0-9]{6,8})\s*<\/h[123]>/i,
            /(?:code|pin|verification|security)\s*(?:is|:)\s*[*\s]*([A-Za-z0-9]{4,8})\b/i,
            /\b([A-Za-z0-9]{6,8})\b(?=\s*(?:to verify|is your|as your|security))/i,
            /(?:enter|use|submit|type)\s+(?:the\s+)?(?:code\s+)?[*\s]*([A-Za-z0-9]{4,8})\b/i,
            />\s*([A-Za-z0-9]{6,8})\s*</,
          ];

          let code = null;
          for (const pat of codePatterns) {
            const cm = textBody.match(pat);
            if (cm) { code = cm[1]; break; }
          }
          console.log(code ? `CODE: ${code}` : `CODE: NOT_FOUND`);
        }

        const readable = body
          .replace(/=\r?\n/g, '')
          .replace(/=([0-9A-F]{2})/gi, (_, hex) => String.fromCharCode(parseInt(hex, 16)))
          .replace(/<[^>]+>/g, ' ')
          .replace(/\s+/g, ' ')
          .trim();
        console.log(`SNIPPET: ${readable.substring(0, 500)}`);
        console.log('');
      }
    } finally {
      lock.release();
    }

    await client.logout();
  } catch (err) {
    console.error(`IMAP_ERROR: ${err.message}`);
    process.exit(1);
  }
}

main();
