# Student sign-in codes by e-mail (HTTPS relay)

Students sign in on `/exams/login` with Institution ID, College ID and their registered e-mail, then enter a
6-digit code. The backend must be able to deliver that code.

Hugging Face Spaces only allow outbound connections on ports 80, 443 and 8080, so SMTP (25/465/587) cannot work
there. The `webhook` provider sends the code over HTTPS to a small relay you control; the relay composes and sends the
e-mail itself.

```
backend --HTTPS POST {secret, to, code, ttl_minutes}--> relay --Gmail--> student
backend <---------------- {"ok": true} ------------------
```

The relay builds the message text, so the shared secret only ever allows "sign-in code" messages to be sent, never
arbitrary mail. The code is never logged by the backend.

## 1. Create the relay (Google Apps Script)

1. Sign in to the Google account that should send the mail (a dedicated account is best), open
   <https://script.google.com> and create a new project.
2. Replace the contents of `Code.gs` with:

```javascript
function reply(ok) {
  return ContentService.createTextOutput(JSON.stringify({ ok: ok })).setMimeType(ContentService.MimeType.JSON);
}

function sameText(a, b) {
  if (a.length !== b.length) return false;
  var diff = 0;
  for (var i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

function doPost(e) {
  try {
    var body = JSON.parse(e.postData.contents);
    var secret = PropertiesService.getScriptProperties().getProperty('RELAY_SECRET') || '';
    if (!secret || !sameText(String(body.secret || ''), secret)) return reply(false);

    var to = String(body.to || '').trim();
    var code = String(body.code || '');
    var minutes = Math.max(1, Math.min(60, Number(body.ttl_minutes) || 5));
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(to) || !/^\d{6}$/.test(code)) return reply(false);
    if (MailApp.getRemainingDailyQuota() < 1) return reply(false);

    MailApp.sendEmail({
      to: to,
      subject: 'Your HireLog Exams sign-in code',
      body: 'Your sign-in code is ' + code + '.\n\nIt expires in ' + minutes + ' minutes and works once.\n' +
            'If you did not ask for it, ignore this message; nobody can sign in without it.',
      name: 'HireLog Exams',
    });
    return reply(true);
  } catch (err) {
    return reply(false);
  }
}
```

3. Project Settings, Script properties: add `RELAY_SECRET` with a long random value (for example 40 random
   characters). Keep it; the backend needs the same value.
4. Deploy, New deployment, type Web app. Execute as: Me. Who has access: Anyone. Authorize the Gmail permission when
   asked. Copy the Web app URL (`https://script.google.com/macros/s/.../exec`).
5. After changing the code later, use Deploy, Manage deployments, Edit, New version. The URL stays the same.

Mail limits: a consumer Gmail account sends about 100 recipients a day, Google Workspace about 1,500. One exam of 90
students needs at least 90 codes, plus re-sends, so use a Workspace account or split the sign-in over two days if
you stay on a consumer account. When the quota is used up the relay answers `ok: false` and the backend reports that
delivery failed.

## 2. Point the backend at it

Set these in the Space (Settings, Variables and secrets). The two values marked secret must be added as secrets.

| Name | Kind | Value |
| --- | --- | --- |
| `EXAM_OTP_DELIVERY_PROVIDER` | variable | `webhook` |
| `EXAM_OTP_WEBHOOK_URL` | secret | the Web app URL |
| `EXAM_OTP_WEBHOOK_SECRET` | secret | the `RELAY_SECRET` value |
| `EXAM_OTP_HMAC_SECRET` | secret | any long random string (hashes the codes; keep it stable) |

Restart the Space. Without the first three, OTP sign-in answers 503 `otp_delivery_not_configured`; staff who sign in
with Google are not affected.

## 3. Check it

Request a code on `/exams/login` for a student who is on a roster. The mail should arrive within a few seconds.
A relay that is unreachable, over quota or given the wrong secret makes the request fail without revealing whether
the e-mail is registered.

## Notes

- The relay URL is not a credential on its own, but anyone who has both it and the secret can send "sign-in code"
  e-mails from your account. Treat both as secrets and rotate `RELAY_SECRET` if either leaks (change it in Script
  properties and in the Space).
- E-mail codes prove access to the mailbox, not a second factor.
- Any other HTTPS mail API can stand in for Apps Script: it only needs to accept the JSON above and answer
  `{"ok": true}` with HTTP 200.
