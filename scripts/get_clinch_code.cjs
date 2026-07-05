#!/usr/bin/env node
// Fetches emails from clinchtalent sender in the last 10 minutes,
// decodes the full body (quoted-printable and base64), and prints
// all text content plus any verification code patterns found.

const { ImapFlow } = require('imapflow');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');

function loadEnv() {
  const envPath = path.join(ROOT, '.env');
  if (!fs.existsSync(envPath)) return;
  for (const line of fs.readFileSync(envPath, 'utf8').split('\n')) {
    const m = line.match(/^([A-Z_]+)=(.*)$/);
    if (m && !process.env[m[1]]) process.env[m[1]] = m[2].trim();
  }
}

function decodeQuotedPrintable(str) {
  return str
    .replace(/=\r?\n/g, '')
    .replace(/=([0-9A-Fa-f]{2})/g, (_, hex) => String.fromCharCode(parseInt(hex, 16)));
}

function decodeBase64Part(str) {
  try {
    return Buffer.from(str.replace(/\s/g, ''), 'base64').toString('utf8');
  } catch (e) {
    return '';
  }
}

function stripHtml(str) {
  return str
    .replace(/<style[^>]*>[\s\S]*?<\/style>/gi, ' ')
    .replace(/<script[^>]*>[\s\S]*?<\/script>/gi, ' ')
    .replace(/<[^>]+>/g, ' ')
    .replace(/&nbsp;/gi, ' ')
    .replace(/&amp;/gi, '&')
    .replace(/&lt;/gi, '<')
    .replace(/&gt;/gi, '>')
    .replace(/&quot;/gi, '"')
    .replace(/&#39;/gi, "'")
    .replace(/\s+/g, ' ')
    .trim();
}

function extractParts(rawBody) {
  const parts = [];

  // Split on MIME boundaries
  // Find boundary markers
  const boundaryMatch = rawBody.match(/boundary="?([^"\r\n;]+)"?/i);
  const boundary = boundaryMatch ? boundaryMatch[1] : null;

  if (boundary) {
    const sections = rawBody.split(new RegExp('--' + boundary.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
    for (const section of sections) {
      if (!section.trim() || section.trim() === '--') continue;
      processSection(section, parts);
    }
  } else {
    // No boundary - treat whole body as one section
    processSection(rawBody, parts);
  }

  return parts;
}

function processSection(section, parts) {
  // Find header/body split (blank line)
  const headerBodySplit = section.match(/^([\s\S]*?)\r?\n\r?\n([\s\S]*)$/);
  if (!headerBodySplit) return;

  const headers = headerBodySplit[1];
  let body = headerBodySplit[2];

  const contentType = (headers.match(/Content-Type:\s*([^\r\n;]+)/i) || [])[1] || '';
  const encoding = (headers.match(/Content-Transfer-Encoding:\s*([^\r\n]+)/i) || [])[1] || '';

  // Recursively handle nested multipart
  if (contentType.trim().toLowerCase().startsWith('multipart/')) {
    const nestedBoundaryMatch = headers.match(/boundary="?([^"\r\n;]+)"?/i)
      || body.match(/boundary="?([^"\r\n;]+)"?/i);
    if (nestedBoundaryMatch) {
      const nestedBoundary = nestedBoundaryMatch[1];
      const sections = body.split(new RegExp('--' + nestedBoundary.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
      for (const s of sections) {
        if (!s.trim() || s.trim() === '--') continue;
        processSection(s, parts);
      }
      return;
    }
  }

  // Decode based on encoding
  let decoded = body;
  const enc = encoding.trim().toLowerCase();
  if (enc === 'base64') {
    decoded = decodeBase64Part(body);
  } else if (enc === 'quoted-printable') {
    decoded = decodeQuotedPrintable(body);
  }

  const ct = contentType.trim().toLowerCase();
  if (ct.startsWith('text/plain') || ct.startsWith('text/html') || ct === '') {
    const text = ct.startsWith('text/html') ? stripHtml(decoded) : decoded.replace(/\s+/g, ' ').trim();
    if (text.length > 10) {
      parts.push({ type: ct || 'text/plain', text });
    }
  }
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

  console.log(`Connecting to ${host}:${port} as ${user}`);
  console.log(`Searching for emails from clinchtalent in the last 10 minutes...\n`);

  const sinceDate = new Date(Date.now() - 10 * 60 * 1000);

  const client = new ImapFlow({
    host,
    port,
    secure: port === 993,
    auth: { user, pass },
    logger: false,
  });

  try {
    await client.connect();
    const lock = await client.getMailboxLock('INBOX', { readOnly: true });

    try {
      const searchCriteria = {
        since: sinceDate,
        from: 'clinchtalent',
      };

      const messages = [];
      for await (const msg of client.fetch(
        searchCriteria,
        { source: true, envelope: true },
        { uid: true }
      )) {
        messages.push(msg);
      }

      messages.sort((a, b) => (b.envelope?.date || 0) - (a.envelope?.date || 0));

      if (messages.length === 0) {
        console.log('NO_MAIL: no matching emails from clinchtalent in last 10 minutes');
        // Try broader search - last 60 minutes
        console.log('\nTrying broader search (last 60 minutes)...');
        const broader = new Date(Date.now() - 60 * 60 * 1000);
        for await (const msg of client.fetch(
          { since: broader, from: 'clinchtalent' },
          { source: true, envelope: true },
          { uid: true }
        )) {
          messages.push(msg);
        }
        messages.sort((a, b) => (b.envelope?.date || 0) - (a.envelope?.date || 0));
        if (messages.length === 0) {
          console.log('NO_MAIL: no emails from clinchtalent found in last 60 minutes either');
          process.exit(0);
        }
      }

      console.log(`Found ${messages.length} email(s)\n`);

      for (const msg of messages) {
        const env = msg.envelope || {};
        const from = env.from?.[0]?.address || 'unknown';
        const subject = env.subject || '(no subject)';
        const date = env.date ? new Date(env.date).toISOString() : 'unknown';
        const rawBody = msg.source?.toString('utf8') || '';

        console.log('='.repeat(60));
        console.log(`FROM: ${from}`);
        console.log(`SUBJECT: ${subject}`);
        console.log(`DATE: ${date}`);
        console.log('='.repeat(60));

        // Extract and decode all parts
        const parts = extractParts(rawBody);

        if (parts.length === 0) {
          // Fallback: decode the whole raw body
          console.log('\n[FALLBACK: decoding raw body]\n');
          const decoded = decodeQuotedPrintable(rawBody);
          const stripped = stripHtml(decoded);
          console.log(stripped.substring(0, 3000));
        } else {
          for (let i = 0; i < parts.length; i++) {
            const part = parts[i];
            console.log(`\n--- PART ${i + 1} (${part.type}) ---`);
            console.log(part.text.substring(0, 3000));
          }
        }

        // Search for verification codes in all text
        const allText = parts.map(p => p.text).join(' ') || rawBody;

        console.log('\n--- CODE SEARCH ---');

        const codePatterns = [
          { label: 'Heading/bold code', re: />\s*([A-Za-z0-9]{4,8})\s*<\/(?:h[1-6]|strong|b|p|div|span|td)>/gi },
          { label: 'code/pin/verification label', re: /(?:code|pin|verification|security)\s*(?:is|:)\s*[*\s]*([A-Za-z0-9]{4,8})\b/gi },
          { label: 'Standalone 4-8 digit number', re: /\b([0-9]{4,8})\b/g },
          { label: 'Alphanumeric 6-8 chars', re: /\b([A-Za-z0-9]{6,8})\b/g },
          { label: 'enter/use/submit code', re: /(?:enter|use|submit|type)\s+(?:the\s+)?(?:code\s+)?[*\s]*([A-Za-z0-9]{4,8})\b/gi },
          { label: 'your code is', re: /your\s+(?:verification\s+)?code\s+is\s+[:\s]*([A-Za-z0-9]{4,8})\b/gi },
        ];

        // Also search the raw body for any number sequences
        const rawDecoded = decodeQuotedPrintable(rawBody);

        for (const { label, re } of codePatterns) {
          re.lastIndex = 0;
          const searchText = allText + ' ' + rawDecoded;
          let m;
          const found = new Set();
          while ((m = re.exec(searchText)) !== null) {
            found.add(m[1]);
            if (found.size > 5) break;
          }
          if (found.size > 0) {
            console.log(`${label}: ${[...found].join(', ')}`);
          }
        }

        // Direct scan for PIN/code in context
        const pinContextMatch = rawDecoded.match(/(?:PIN|code|verify|confirm)[^0-9A-Za-z]*([0-9A-Za-z]{4,8})/gi);
        if (pinContextMatch) {
          console.log(`PIN context matches: ${pinContextMatch.slice(0, 10).join(' | ')}`);
        }

        console.log('\n');
      }
    } finally {
      lock.release();
    }

    await client.logout();
  } catch (err) {
    console.error(`IMAP_ERROR: ${err.message}`);
    console.error(err.stack);
    process.exit(1);
  }
}

main();
