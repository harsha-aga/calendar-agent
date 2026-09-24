"""The chat page served at /chat. One self-contained HTML page, no build step."""

CHAT_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Calendar agent</title>
<style>
  :root { --bg:#f7f7f5; --panel:#fff; --text:#1d1d1b; --muted:#6b6a65; --line:#e3e2dc;
          --me:#1a73e8; --me-text:#fff; --err:#b3261e; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#1b1b1a; --panel:#262624; --text:#ecebe6; --muted:#a3a29b; --line:#3a3936;
            --me:#3b82f6; --me-text:#fff; --err:#f2b8b5; }
  }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
         font:16px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
  .wrap { max-width:760px; margin:0 auto; min-height:100vh; display:flex; flex-direction:column; padding:0 16px; }
  header { display:flex; justify-content:space-between; align-items:center; padding:18px 0; border-bottom:1px solid var(--line); }
  header h1 { font-size:18px; margin:0; font-weight:600; }
  header a { color:var(--muted); font-size:14px; }
  #log { flex:1; padding:20px 0; display:flex; flex-direction:column; gap:12px; }
  .msg { max-width:85%; padding:10px 14px; border-radius:14px; white-space:pre-wrap; word-wrap:break-word; }
  .user { align-self:flex-end; background:var(--me); color:var(--me-text); border-bottom-right-radius:4px; }
  .bot { align-self:flex-start; background:var(--panel); border:1px solid var(--line); border-bottom-left-radius:4px; }
  .err { align-self:flex-start; color:var(--err); font-size:14px; }
  .thinking { color:var(--muted); font-style:italic; }
  .empty { color:var(--muted); text-align:center; margin-top:15vh; }
  .chips { display:flex; flex-wrap:wrap; gap:8px; justify-content:center; margin-top:16px; }
  .chip { border:1px solid var(--line); background:var(--panel); color:var(--text); border-radius:999px;
          padding:6px 12px; font-size:14px; cursor:pointer; }
  .chip:hover { border-color:var(--muted); }
  form { position:sticky; bottom:0; background:var(--bg); padding:12px 0 20px; display:flex; gap:8px; }
  textarea { flex:1; resize:none; font:inherit; color:var(--text); background:var(--panel);
             border:1px solid var(--line); border-radius:12px; padding:10px 12px; height:48px; }
  textarea:focus { outline:2px solid var(--me); outline-offset:-1px; }
  button.send { background:var(--me); color:var(--me-text); border:0; border-radius:12px; padding:0 18px; font:inherit; cursor:pointer; }
  button.send:disabled { opacity:.5; cursor:default; }
</style>
</head>
<body>
<div class="wrap">
  <header><h1>Calendar agent</h1><a href="/">Sync status</a></header>
  <div id="log">
    <div class="empty" id="empty">
      Ask about your calendar.
      <div class="chips">
        <button class="chip">Am I free Thursday afternoon?</button>
        <button class="chip">What does my week look like?</button>
        <button class="chip">Find me a free hour tomorrow</button>
        <button class="chip">Can I do 3 to 4pm on Friday?</button>
      </div>
    </div>
  </div>
  <form id="form">
    <textarea id="input" placeholder="Ask about your schedule..." autofocus></textarea>
    <button class="send" id="send" type="submit">Send</button>
  </form>
</div>
<script>
  const log = document.getElementById('log');
  const input = document.getElementById('input');
  const send = document.getElementById('send');
  const history = [];

  function add(cls, text) {
    const el = document.createElement('div');
    el.className = 'msg ' + cls;
    el.textContent = text;
    log.appendChild(el);
    window.scrollTo(0, document.body.scrollHeight);
    return el;
  }

  async function ask(text) {
    text = text.trim();
    if (!text || send.disabled) return;
    const empty = document.getElementById('empty');
    if (empty) empty.remove();
    add('user', text);
    history.push({ role: 'user', content: text });
    input.value = '';
    send.disabled = true;
    const pending = add('bot thinking', 'Checking your calendar...');
    try {
      const res = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ messages: history })
      });
      const data = await res.json();
      pending.remove();
      if (data.reply) {
        add('bot', data.reply);
        history.push({ role: 'assistant', content: data.reply });
      } else {
        history.pop();
        add('err', data.error || 'Something went wrong.');
      }
    } catch (e) {
      pending.remove();
      history.pop();
      add('err', 'Could not reach the app. Is it still running in Terminal?');
    }
    send.disabled = false;
    input.focus();
  }

  document.getElementById('form').addEventListener('submit', e => { e.preventDefault(); ask(input.value); });
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); ask(input.value); }
  });
  document.querySelectorAll('.chip').forEach(c => c.addEventListener('click', () => ask(c.textContent)));
</script>
</body>
</html>
"""
