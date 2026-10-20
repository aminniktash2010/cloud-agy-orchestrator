/**
 * Zero-Cost Bridge: Forwards Gmail messages labeled `agy-queue` directly to your Cloud Run
 * Antigravity Orchestrator and replies to the email thread with the output so you can
 * read and dispatch jobs straight from your Gemini Chat!
 *
 * Setup in https://script.google.com:
 * 1. Paste this script and set Script Properties:
 *    - CLOUD_RUN_URL: https://agy-cloud-orchestrator-202076903469.us-central1.run.app/chat
 *    - WEBHOOK_SECRET: Your secret token matching Google Secret Manager
 * 2. Add a Time-driven Trigger -> Every 1 minute -> dispatchToAntigravityCloudRun
 */

function dispatchToAntigravityCloudRun() {
  const threads = GmailApp.search('label:agy-queue is:unread');
  const props = PropertiesService.getScriptProperties();
  const cloudRunUrl = props.getProperty('CLOUD_RUN_URL');
  const webhookSecret = props.getProperty('WEBHOOK_SECRET');

  for (const thread of threads) {
    const messages = thread.getMessages();
    const msg = messages[messages.length - 1];
    // Subject format: AGY | Atari-TV | main
    const subjectParts = msg.getSubject().split('|');
    const repoName = (subjectParts[1] || 'Atari-TV').trim();
    const branchName = (subjectParts[2] || 'main').trim();
    const promptBody = msg.getPlainBody().trim();

    const response = UrlFetchApp.fetch(cloudRunUrl, {
      method: 'post',
      contentType: 'application/json',
      headers: { 'x-webhook-token': webhookSecret },
      payload: JSON.stringify({
        repo: repoName,
        prompt: promptBody,
        branch: branchName
      }),
      muteHttpExceptions: true
    });

    msg.reply("Antigravity Cloud Run Execution Report:\n\n" + response.getContentText());
    thread.markRead();
  }
}
