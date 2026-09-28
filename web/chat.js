// chat.js — the Chat view of each tile (loaded by index.html).
//
// Reads the session's transcript through GET api/chat (incremental, by byte offset) and draws it
// like a chat app, but compact like a terminal: messages as chat, tool calls folded into ONE line
// per group with the real command as a grey hint. The terminal keeps running UNDER the chat
// (never display:none — with tmux `window-size latest` a hidden iframe would shrink the window).
// Writing = a navigational form POST into a hidden iframe (auth proxies can kill XHR POSTs);
// reading = plain GETs, like api/status.
"use strict";
const chatSt = {};                                   // idx → {sess, sid, off, busy}

// ── Markdown (small, safe: everything is escaped first) ──────────────────────────────────────
function mdInline(t){
  const codes = [];
  t = esc(t).replace(/`([^`]+)`/g, (_,c)=>{ codes.push(c); return "\u0000"+(codes.length-1)+"\u0000"; });
  t = t.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
       .replace(/(^|[^\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])/g, "$1<em>$2</em>")
       .replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, (_,alt,src)=> attHTML(src.replace(/&amp;/g,"&")))
       .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  return t.replace(/\u0000(\d+)\u0000/g, (_,i)=>"<code>"+codes[+i]+"</code>");
}
function md(src){
  const L = src.split("\n"), out = []; let i = 0;
  const isList = l => /^\s*([-*]|\d+\.)\s+/.test(l);
  while(i < L.length){
    const l = L[i];
    if(/^\s*```/.test(l)){
      const lang = l.trim().slice(3).trim(), buf = []; i++;
      while(i < L.length && !/^\s*```/.test(L[i])) buf.push(L[i++]);
      i++;
      out.push(`<div class="cb"><div class="cb-h">${esc(lang||"text")}<button data-copy>Copy</button></div><pre>${esc(buf.join("\n"))}</pre></div>`);
      continue;
    }
    if(/^\s*\|/.test(l) && i+1 < L.length && /^\s*\|[\s:|-]+\|\s*$/.test(L[i+1])){
      const row = s => s.trim().replace(/^\||\|$/g,"").split("|").map(c=>c.trim());
      const head = row(l); i += 2; const body = [];
      while(i < L.length && /^\s*\|/.test(L[i])) body.push(row(L[i++]));
      out.push('<div class="tb"><table><thead><tr>'+head.map(c=>`<th>${mdInline(c)}</th>`).join("")+"</tr></thead><tbody>"+
        body.map(r=>"<tr>"+r.map(c=>`<td>${mdInline(c)}</td>`).join("")+"</tr>").join("")+"</tbody></table></div>");
      continue;
    }
    const m = l.match(/^(#{1,4})\s+(.*)/);
    if(m){ const n = Math.min(m[1].length+2,5); out.push(`<h${n}>${mdInline(m[2])}</h${n}>`); i++; continue; }
    if(isList(l)){
      const ordered = /^\s*\d+\./.test(l), base = l.length - l.trimStart().length, items = [];
      while(i < L.length && (isList(L[i]) || (/^\s{2,}\S/.test(L[i]) && items.length))){
        if(isList(L[i])) items.push([L[i].length - L[i].trimStart().length, L[i].replace(/^\s*([-*]|\d+\.)\s+/,"")]);
        else items[items.length-1][1] += " " + L[i].trim();
        i++;
      }
      let h = ordered ? "<ol>" : "<ul>", depth = 0;
      for(const [ind, txt] of items){
        const d = ind > base ? 1 : 0;
        if(d > depth) h += "<ul>"; if(d < depth) h += "</ul>"; depth = d;
        h += `<li>${mdInline(txt)}</li>`;
      }
      out.push(h + "</ul>".repeat(depth) + (ordered ? "</ol>" : "</ul>")); continue;
    }
    if(l.startsWith(">")){
      const buf = []; while(i < L.length && L[i].startsWith(">")) buf.push(L[i++].replace(/^>\s?/,""));
      out.push(`<blockquote>${md(buf.join("\n"))}</blockquote>`); continue;
    }
    if(!l.trim()){ i++; continue; }
    const buf = [];
    while(i < L.length && L[i].trim() && !/^(\s*```|#{1,4}\s|\s*([-*]|\d+\.)\s+|>|\s*\|)/.test(L[i])) buf.push(L[i++]);
    if(!buf.length) buf.push(L[i++]);
    out.push("<p>"+buf.map(mdInline).join("<br>")+"</p>");
  }
  return out.join("");
}

// ── Attachments: images and PDFs, as thumbnails / cards that open in the in-page viewer ──────
const ATT_RE = /(?:^|[\s(`'"])(\/[^\s`'"()<>]+\.(?:png|jpe?g|gif|webp|pdf))(?=$|[\s)`'".,;:])/gi;
const viewURL = p => "view?p=" + encodeURIComponent(p);
function attHTML(path){
  const name = String(path).split("/").pop(), web = /^https?:/i.test(path);
  if(!web && /\.pdf$/i.test(path))
    return `<a class="att-pdf" href="${viewURL(path)}" target="_blank" rel="noopener"><span class="ic">PDF</span><span class="nm">${esc(name)}</span></a>`;
  if(web || /\.(png|jpe?g|gif|webp)$/i.test(path)){
    const src = web ? esc(path) : viewURL(path);
    return `<a class="att-img" title="${esc(name)}" href="${src}" target="_blank" rel="noopener"><img loading="lazy" alt="${esc(name)}" onerror="this.parentNode.classList.add('broken')" src="${src}"></a>`;
  }
  return esc(path);
}
function pathsIn(text){ const out = []; let m; ATT_RE.lastIndex = 0; while((m = ATT_RE.exec(text))) out.push(m[1]); return [...new Set(out)]; }

// ── Tool calls ──────────────────────────────────────────────────────────────────────────────
const TOOL_IC = {Bash:"⌘", Read:"▤", Edit:"✎", Write:"✎", MultiEdit:"✎", Grep:"⌕", Glob:"⌕", Skill:"✦",
                 Agent:"◎", Task:"◎", WebFetch:"↗", WebSearch:"↗", ToolSearch:"⌕", TodoWrite:"☑"};
const shortPath = p => String(p||"").replace(/^\/home\/[^/]+\//, "~/");
function toolTitle(e){
  const i = e.input || {};
  switch(e.name){
    case "Bash": return i.description || i.command || "command";
    case "Read": return "read " + shortPath(i.file_path);
    case "Edit": case "MultiEdit": return "edited " + shortPath(i.file_path);
    case "Write": return "wrote " + shortPath(i.file_path);
    case "Grep": case "Glob": return "searched " + (i.pattern || "");
    case "Skill": return "skill " + (i.skill || "");
    case "AskUserQuestion": return "asked you: " + ((i.questions||[])[0]||{}).question;
    default: return String(e.name||"tool").replace(/^mcp__[^_]+__/,"") + (i.description ? " · " + i.description : "");
  }
}
// The grey hint on the folded line: the REAL command, so you see what runs without expanding it
function toolHint(e){
  const i = e.input || {};
  if(e.name === "Bash" && i.description && i.command) return String(i.command).split("\n")[0].slice(0, 160);
  if((e.name === "Grep" || e.name === "Glob") && i.path) return shortPath(i.path);
  return "";
}
// Text a command loads into the clip. Walks the command line by line, telling COMMAND lines from heredoc
// BODY lines: cr-clip only counts on command lines (a heredoc writing a README that MENTIONED cr-clip made a fake card).
function clipFromCmd(cmd){
  const CLIP = "(?:[\\w./~-]*\\/)?(?:cr-clip|clip\\.sh)", L = cmd.split("\n");   // no "=" in the path: C=/…/cr-clip is an assignment
  const flagOnly = new RegExp("(?:cr-clip|clip\\.sh)\\s+-(-show|-clear|s|c)\\b");
  for(let k = 0; k < L.length; k++){
    const line = L[k], h = line.match(/<<-?\s*['"]?(\w+)['"]?/);
    // the segment (between ; && ||) holding the << must be `cr-clip <<…` or `… <<… | cr-clip`
    const seg = h ? (line.split(/;|&&|\|\|/).find(x => x.includes("<<")) || "") : line;
    const runsClip = new RegExp("^\\s*" + CLIP + "\\b").test(seg) || new RegExp("\\|\\s*" + CLIP + "\\b").test(seg);
    if(h){                                               // opens a heredoc: its body runs to the terminator
      let e = k + 1; while(e < L.length && L[e].trim() !== h[1]) e++;
      if(runsClip && !flagOnly.test(line)) return L.slice(k + 1, e).join("\n");
      k = e; continue;                                   // skip the body: it's text, not a command
    }
    const m = line.match(new RegExp("(?:^|[|;&]\\s*)\\s*" + CLIP + "\\s+([\"'])(.*?)\\1"));
    if(m && !flagOnly.test(line)) return m[2];
  }
  return null;
}
function toolBody(e){
  const i = e.input || {};
  if(e.name === "Bash") return `<div class="lbl">command</div><pre>${esc(i.command||"")}</pre>`;
  if(e.name === "Edit") return '<div class="diff">' +
    String(i.old_string||"").split("\n").map(x=>`<div class="del">- ${esc(x)}</div>`).join("") +
    String(i.new_string||"").split("\n").map(x=>`<div class="add">+ ${esc(x)}</div>`).join("") + "</div>";
  if(e.name === "Write") return `<div class="lbl">content</div><pre>${esc(String(i.content||"").slice(0,3000))}</pre>`;
  if(e.name === "Read" || e.name === "Skill") return "";
  return `<pre>${esc(JSON.stringify(i, null, 2).slice(0,3000))}</pre>`;
}

// ── Rendering ───────────────────────────────────────────────────────────────────────────────
function chatEls(idx){
  const t = tilesArr()[idx]; if(!t) return null;
  return {tile:t, msgs:t.querySelector(".cmsgs"), perm:t.querySelector(".cperm"), sel:t.querySelector("select")};
}
const nearBottom = el => el.scrollHeight - el.scrollTop - el.clientHeight < 80;
function addEvent(msgs, e){
  const last = msgs.lastElementChild;
  if(e.k === "user" && /^\[Request interrupted/.test(e.text)){
    const d = document.createElement("div"); d.className = "note";
    d.textContent = /tool use/.test(e.text) ? "✋ you declined the request" : "✋ interrupted"; msgs.append(d);
  } else if(e.k === "user"){
    msgs.querySelectorAll(".m-u.pend").forEach(p=>p.remove());
    const d = document.createElement("div"); d.className = "m-u";
    const ps = pathsIn(e.text);
    let txt = e.text; ps.forEach(p=> txt = txt.split(p).join("")); txt = txt.trim();
    if(txt){ const t = document.createElement("div"); t.textContent = txt; d.append(t); }
    if(ps.length) d.insertAdjacentHTML("beforeend", '<div class="atts">' + ps.map(attHTML).join("") + "</div>");
    msgs.append(d);
  } else if(e.k === "asst" && /^(You've hit your|API Error|Claude usage limit)/.test(e.text)){
    const d = document.createElement("div"); d.className = "note"; d.textContent = "⚠ " + e.text; msgs.append(d);
  } else if(e.k === "asst"){
    const d = document.createElement("div"); d.className = "m-a"; d.innerHTML = md(e.text); msgs.append(d);
    // files Claude handed you (the server checked they exist and are downloadable) → a Download card
    if(e.files && e.files.length) msgs.insertAdjacentHTML("beforeend", '<div class="dlcards">' + e.files.map(f=>{
      const ext = (f.n.split(".").pop() || "").slice(0,4).toUpperCase();
      return `<a class="dlcard" href="dl?p=${encodeURIComponent(f.p)}" download="${esc(f.n)}" title="${esc(f.p)}">` +
             `<span class="ic">${esc(ext)}</span><span class="dn"><b>${esc(f.n)}</b><small>${esc(f.size)}</small></span><span class="dl">⬇ Download</span></a>`;
    }).join("") + "</div>");
    // the clips loaded in this turn join the SAME row as the downloads
    if(e.files && e.files.length){
      const row = msgs.lastElementChild; let x = d.previousElementSibling;
      while(x && !x.classList.contains("m-u")){
        const prev = x.previousElementSibling;
        if(x.classList.contains("toolatt")){ x.querySelectorAll(".clipcard").forEach(c=> row.append(c)); if(!x.children.length) x.remove(); }
        x = prev;
      }
    }
  } else if(e.k === "cmd"){                            // a /skill or /clear you typed
    const d = document.createElement("div"); d.className = "skillchip user";
    const builtin = /^\/(clear|compact|resume|model|exit|login|logout|config|status|help)\b/.test(e.name);
    d.innerHTML = `<span class="sk-ic">${builtin ? "↺" : "✦"}</span><b>${esc(e.name)}</b>${e.args ? ` <span class="sk-a">${esc(e.args)}</span>` : ""}`;
    msgs.append(d);
  } else if(e.k === "tool" && e.name === "Skill"){       // Claude loads a skill: its own chip, not folded into the steps
    const i = e.input || {}, d = document.createElement("div"); d.className = "skillchip";
    d.innerHTML = `<span class="sk-ic">✦</span>Skill: <b>${esc(i.skill || "?")}</b>${i.args ? ` <span class="sk-a">${esc(String(i.args).slice(0,120))}</span>` : ""}`;
    msgs.append(d);
  } else if(e.k === "tool"){
    let g = last && last.classList.contains("grp") ? last :
            (last && last.classList.contains("toolatt") && last.previousElementSibling &&
             last.previousElementSibling.classList.contains("grp") ? last.previousElementSibling : null);
    if(!g){
      g = document.createElement("details"); g.className = "grp";
      g.innerHTML = '<summary><span class="n"></span><span class="last"></span></summary><div class="steps"></div>';
      msgs.append(g);
    }
    const hint = toolHint(e), s = document.createElement("details");
    s.className = "stp"; s.dataset.id = e.id || "";
    s.innerHTML = `<summary><span>${TOOL_IC[e.name]||"•"}</span><span class="t">${esc(toolTitle(e))}${hint?` <span class="hint">${esc(hint)}</span>`:""}</span><span class="r"></span></summary>` +
                  `<div class="body">${toolBody(e)}</div>`;
    g.querySelector(".steps").append(s);
    const n = g.querySelectorAll(".stp").length;
    g.querySelector(".n").textContent = `› ${n} step${n>1?"s":""}`;
    g.querySelector(".last").innerHTML = esc(toolTitle(e)) + (hint ? ` <span class="hint">${esc(hint)}</span>` : "");
    // A clip loaded with cr-clip → a visible "📋 Copy" card in the chat (next to the download cards).
    // The text comes from the command ITSELF (heredoc or argument), not the live clip: another session may overwrite it.
    const clipTxt = e.name === "Bash" ? clipFromCmd((e.input || {}).command || "") : null;
    if(clipTxt){
      let a = g.nextElementSibling;
      if(!a || !a.classList.contains("toolatt")){ a = document.createElement("div"); a.className = "atts toolatt"; g.after(a); }
      const n = clipTxt.split("\n").length, card = document.createElement("div"); card.className = "clipcard";
      card.innerHTML = `<span class="ic">📋</span><span class="dn"><b>${esc(clipTxt.split("\n")[0].slice(0,70))}</b><small>clip · ${n} line${n>1?"s":""} · ${clipTxt.length} chars</small></span><button type="button" class="cp">Copy</button>`;
      card.querySelector(".cp").onclick = ev => { ev.stopPropagation(); copyText(window, clipTxt);
        const b = ev.currentTarget; b.textContent = "✓ copied"; setTimeout(()=> b.textContent = "Copy", 1500); };
      a.append(card);
    }
    const fp = (e.input || {}).file_path || "";
    if(e.name === "Read" && /\.(png|jpe?g|gif|webp|pdf)$/i.test(fp)){
      let a = g.nextElementSibling;
      if(!a || !a.classList.contains("toolatt")){ a = document.createElement("div"); a.className = "atts toolatt"; g.after(a); }
      a.insertAdjacentHTML("beforeend", attHTML(fp));
    }
  } else if(e.k === "res"){
    const s = msgs.querySelector(`.stp[data-id="${CSS.escape(e.id||"")}"]`); if(!s) return;
    s.querySelector(".r").innerHTML = e.err ? '<span class="bad">✗</span>' : '<span class="ok">✓</span>';
    if(e.text && e.text.trim()) s.querySelector(".body").insertAdjacentHTML("beforeend",
      `<div class="lbl">${e.err?"error":"result"}</div><pre>${esc(e.text)}</pre>`);
  }
}
// Permission card: the question, WHAT is being asked (the pending step from the transcript —
// Claude Code writes the tool_use BEFORE asking — or the box as it is on screen), and the options.
function renderPerm(idx, scr){
  const c = chatEls(idx); if(!c) return;
  c.msgs.querySelectorAll(".stp.waiting").forEach(x=> x.classList.remove("waiting"));
  if(!scr || !scr.opts || !scr.opts.length){ c.perm.hidden = true; c.perm.innerHTML = ""; c.perm.dataset.key = ""; return; }
  const steps = [...c.msgs.querySelectorAll(".stp")];
  const pend = steps.length && !steps[steps.length-1].querySelector(".r").innerHTML ? steps[steps.length-1] : null;
  if(pend){ pend.classList.add("waiting"); const g = pend.closest(".grp"); if(g) g.open = true; }
  const key = JSON.stringify(scr) + (pend ? pend.dataset.id : ""); if(c.perm.dataset.key === key) return;
  c.perm.dataset.key = key; c.perm.hidden = false;
  let what = "";
  if(pend){
    what = `<div class="what"><div class="wt">${esc(pend.querySelector("summary span").textContent)} ${esc(pend.querySelector(".t").textContent)}</div>${pend.querySelector(".body").innerHTML}</div>`;
  } else if(scr.detail && scr.detail.trim()){
    what = `<div class="what"><pre>${esc(scr.detail.replace(/^\s*\n|\s+$/g,""))}</pre></div>`;
  }
  const isNo = l => /^no\b/i.test(l);
  c.perm.innerHTML = `<div class="q">${esc(scr.question || "Claude is waiting for you")}</div>` + what + '<div class="opts">' +
    scr.opts.map(o=>`<button type="button" class="${o.n==="1"?"yes":isNo(o.label)?"no":""}" data-key="${esc(o.n)}" title="${esc(o.label)}">${esc(o.n)}. ${esc(o.label)}</button>`).join("") +
    '<button type="button" data-key="Escape" title="cancel">Esc</button></div>';
}
async function pollChat(idx){
  const c = chatEls(idx); if(!c || c.tile.dataset.mode !== "chat") return;
  const sess = c.sel.value;
  let st = chatSt[idx];
  if(!st || st.sess !== sess){ st = chatSt[idx] = {sess, sid:"", off:0, busy:false}; c.msgs.innerHTML = ""; }
  if(st.busy) return; st.busy = true;
  try{
    const r = await fetch(`api/chat?s=${encodeURIComponent(sess)}&sid=${encodeURIComponent(st.sid)}&off=${st.off}&t=${Date.now()}`, {cache:"no-store"});
    if(!r.ok) return;
    const d = await r.json();
    if(chatSt[idx] !== st) return;                     // the tile switched session meanwhile
    const note = html => { if(c.msgs.dataset.note !== html){ c.msgs.innerHTML = html; c.msgs.dataset.note = html; } st.sid = ""; st.off = 0; renderPerm(idx, null); };
    c.tile.classList.toggle("away", !!(d.away || d.off === true));
    if(d.away){
      const where = d.shell ? `it is at the <b>shell</b> (<code>${esc(d.cmd)}</code>)` : `it is running <b><code>${esc(d.cmd)}</code></b>`;
      const why = d.shell ? "Claude exited (<code>/exit</code> or a crash) or was never started in this tile."
                          : "Claude is not in the foreground: another program is using this tile.";
      return note(`<div class="awaycard"><div class="aw-ic">⏻</div><div class="aw-t">No Claude in this tile</div>
        <div class="aw-d">Right now ${where}. ${why}</div>
        <div class="aw-d">The chat only talks to Claude — anything typed here would go to that program, so the box is locked.</div>
        <div class="aw-b">${d.shell ? '<button type="button" class="yes" data-run="claude">▶ Start Claude</button><button type="button" data-run="claude --continue">↻ Continue the last conversation</button>' : ""}
        <button type="button" data-goterm="1">Go to Term</button></div></div>`);
    }
    if(d.off === true) return note('<div class="awaycard"><div class="aw-ic">⏻</div><div class="aw-t">Session not started</div><div class="aw-d">It starts the first time its terminal opens.</div><div class="aw-b"><button type="button" class="yes" data-goterm="1">Open Term</button></div></div>');
    if(d.fresh && !st.sid) return note('<div class="shellnote">New conversation: no messages yet.<br>Type below to start.</div>');
    if(d.nohooks && !st.sid) return note('<div class="shellnote">No transcript for this session yet.<br>Chat needs the hooks (<code>install.sh --hooks</code>) — <b>Term</b> always works.</div>');
    c.msgs.dataset.note = "";
    const stick = nearBottom(c.msgs);
    if(d.reset) c.msgs.innerHTML = d.partial ? '<div class="note">… earlier messages are in the terminal</div>' : "";
    if((d.events || []).length) c.perm.dataset.key = "";
    (d.events || []).forEach(e=> addEvent(c.msgs, e));
    st.sid = d.sid; st.off = d.off;
    renderPerm(idx, d.screen);
    const w = c.tile.querySelector(".working");
    w.classList.toggle("spin", !!d.spin);
    w.querySelector(".verb").textContent = d.spin ? d.spin.verb : "Working…";
    w.querySelector(".meta").textContent = d.spin && d.spin.meta ? "(" + d.spin.meta + ")" : "";
    if(stick || d.reset) c.msgs.scrollTop = c.msgs.scrollHeight;
  }catch(e){ /* offline: retry on the next tick */ }
  finally{ st.busy = false; }
}
function chatSend(idx, fields, files){
  const c = chatEls(idx); if(!c) return;
  const f = document.createElement("form");
  f.method = "post"; f.action = "chat/send"; f.target = "chatsink"; f.hidden = true;
  if(files && files.length){                          // attachments: multipart with a hand-built file input
    f.enctype = "multipart/form-data";
    const dt = new DataTransfer(); files.forEach(x=> dt.items.add(x));
    const fi = document.createElement("input"); fi.type = "file"; fi.name = "f"; fi.multiple = true; fi.files = dt.files; f.append(fi);
  }
  Object.entries({s:c.sel.value, ...fields}).forEach(([k,v])=>{
    const i = document.createElement("input"); i.type = "hidden"; i.name = k; i.value = v; f.append(i);
  });
  document.body.append(f); f.submit(); f.remove();
  setTimeout(()=> pollChat(idx), 700);
}
function chatHTML(){
  const clip = '<svg class="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.5l-8.6 8.6a5 5 0 0 1-7.1-7.1l8.6-8.6a3.3 3.3 0 0 1 4.7 4.7l-8.6 8.6a1.7 1.7 0 0 1-2.4-2.4l7.9-7.9"/></svg>';
  const up = '<svg class="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5M5 12l7-7 7 7"/></svg>';
  return '<div class="cmsgs"></div>' +
    '<div class="working"><span class="star">✻</span><span class="verb">Working…</span><span class="meta"></span></div>' +
    '<div class="cperm" hidden></div>' +
    '<div class="ccwrap"><div class="cprev"></div><div class="ccomp">' +
    `<button type="button" class="attbtn" title="attach images or PDFs (or paste / drop them)">${clip}</button>` +
    '<input type="file" multiple accept="image/*,application/pdf" hidden>' +
    '<textarea rows="1" placeholder="Message Claude…  (Enter sends · Shift+Enter new line · paste images)"></textarea>' +
    `<button type="button" class="sendbtn" title="send">${up}</button></div></div>`;
}
function wireChat(tile, idx){
  const msgs = tile.querySelector(".cmsgs"), ta = tile.querySelector("textarea"), btn = tile.querySelector(".sendbtn");
  const prev = tile.querySelector(".cprev"), fin = tile.querySelector('.ccomp input[type="file"]'), chat = tile.querySelector(".chat");
  let pending = [];                                   // [{file, url}]
  const okFile = f => /^image\//.test(f.type) || f.type === "application/pdf" || /\.pdf$/i.test(f.name);
  const grow = ()=>{ btn.disabled = !ta.value.trim() && !pending.length; ta.style.height = "auto";
    if(ta.scrollHeight) ta.style.height = Math.min(ta.scrollHeight, 150) + "px"; };   // measures 0 outside the DOM
  const drawPrev = ()=>{
    prev.innerHTML = pending.map((p,i)=> /^image\//.test(p.file.type)
      ? `<div class="pv"><img src="${p.url}" alt=""><button type="button" class="x" data-i="${i}">✕</button></div>`
      : `<div class="pv pdf"><span class="ic">PDF</span><span class="nm">${esc(p.file.name)}</span><button type="button" class="x" data-i="${i}">✕</button></div>`).join("");
    grow();
  };
  const addFiles = list => { [...list].filter(okFile).forEach(f=>{
      const name = f.name && !/^image\.[a-z0-9]+$/i.test(f.name) ? f.name : `pasted-${Date.now()}.${(f.type.split("/")[1]||"png").replace("jpeg","jpg")}`;
      const file = f.name === name ? f : new File([f], name, {type:f.type});
      pending.push({file, url: /^image\//.test(f.type) ? URL.createObjectURL(f) : ""}); });
    drawPrev(); };
  prev.addEventListener("click", e=>{ const b = e.target.closest(".x"); if(!b) return;
    const [p] = pending.splice(+b.dataset.i, 1); if(p && p.url) URL.revokeObjectURL(p.url); drawPrev(); });
  tile.querySelector(".attbtn").addEventListener("click", ()=> fin.click());
  fin.addEventListener("change", ()=>{ addFiles(fin.files); fin.value = ""; });
  ta.addEventListener("paste", e=>{
    const fs = [...(e.clipboardData && e.clipboardData.files || [])];
    if(fs.length){ e.preventDefault(); e.stopPropagation(); addFiles(fs); }
  });
  chat.addEventListener("dragover", e=>{ if([...e.dataTransfer.types].includes("Files")){ e.preventDefault(); e.stopPropagation(); chat.classList.add("drop"); } });
  chat.addEventListener("dragleave", e=>{ if(!chat.contains(e.relatedTarget)) chat.classList.remove("drop"); });
  chat.addEventListener("drop", e=>{ if(!e.dataTransfer.files.length) return; e.preventDefault(); e.stopPropagation();
    chat.classList.remove("drop"); addFiles(e.dataTransfer.files); ta.focus(); });
  const send = ()=>{
    const text = ta.value.trim(); if(!text && !pending.length) return;
    const p = document.createElement("div"); p.className = "m-u pend";
    if(text){ const t = document.createElement("div"); t.textContent = text; p.append(t); }
    if(pending.length) p.insertAdjacentHTML("beforeend", '<div class="atts">' + pending.map(x=> x.url
      ? `<span class="att-img"><img src="${x.url}" alt=""></span>` : `<span class="att-pdf"><span class="ic">PDF</span><span class="nm">${esc(x.file.name)}</span></span>`).join("") + "</div>");
    // Still gray after 1.5 s = QUEUED (Claude is busy): offer "send now". This doesn't rely on the
    // tile's state, which can lag; if the message was delivered, the pending bubble is already gone.
    setTimeout(()=>{ if(p.isConnected && !p.querySelector(".sendnow"))
      p.insertAdjacentHTML("afterbegin", '<button type="button" class="sendnow" title="interrupt what Claude is doing so it reads this now (Esc + Enter)">send now</button>'); }, 1500);
    msgs.append(p); msgs.scrollTop = msgs.scrollHeight;
    chatSend(idx, {text}, pending.map(x=> x.file));
    pending = []; prev.innerHTML = ""; ta.value = ""; grow();
  };
  ta.addEventListener("keydown", e=>{ if(e.key === "Enter" && !e.shiftKey && !e.isComposing){ e.preventDefault(); send(); } });
  btn.addEventListener("click", send);
  tile.querySelector(".cperm").addEventListener("click", e=>{
    const b = e.target.closest("button[data-key]"); if(b) chatSend(idx, {key:b.dataset.key});
  });
  // "send now": Esc interrupts; the Enter after it sends the message if Claude put it back in the
  // prompt (if Claude already sent it on its own, the Enter lands on an empty prompt and does nothing)
  msgs.addEventListener("click", e=>{
    const b = e.target.closest(".sendnow"); if(!b) return;
    b.disabled = true; b.textContent = "interrupting…";
    chatSend(idx, {key:"Escape"}); setTimeout(()=> chatSend(idx, {key:"Enter"}), 1200);
  });
  msgs.addEventListener("click", e=>{                  // "no Claude" card: start / continue / go to Term
    const r = e.target.closest("[data-run]"), g = e.target.closest("[data-goterm]");
    if(r){ r.disabled = true; r.textContent = "starting…"; chatSend(idx, {text:r.dataset.run}); }
    if(g){ const b = tile.querySelector('.mseg [data-m="term"]'); if(b) b.click(); }
  });
  msgs.addEventListener("click", e=>{
    const b = e.target.closest("[data-copy]"); if(!b) return;
    copyText(window, b.closest(".cb").querySelector("pre").innerText);
    b.textContent = "Copied"; setTimeout(()=> b.textContent = "Copy", 1200);
  });
  grow();
}
setInterval(()=> tilesArr().forEach((_, i)=> pollChat(i)), 1500);

// ── Viewer: a thumbnail / PDF opens large right here, not in another tab ─────────────────────
(function lightbox(){
  const lb = document.getElementById("lightbox"), body = lb.querySelector(".lbbody");
  const close = ()=>{ lb.classList.remove("on"); body.innerHTML = ""; };
  function open(src, name, pdf){
    lb.querySelector(".lbname").textContent = name || "";
    lb.querySelector(".lbopen").href = src;
    body.innerHTML = pdf ? `<iframe src="${esc(src)}" title="${esc(name||"PDF")}"></iframe>` : `<img src="${esc(src)}" alt="${esc(name||"")}">`;
    lb.classList.add("on");
  }
  document.addEventListener("click", e=>{
    const a = e.target.closest(".att-img, .att-pdf, .pv img");
    if(!a || e.target.closest("#lightbox") || a.classList.contains("broken")) return;
    e.preventDefault();
    if(a.matches(".pv img")) return open(a.src, "preview", false);
    const href = a.getAttribute("href") || (a.querySelector("img") || {}).src; if(!href) return;
    open(href, a.getAttribute("title") || (a.querySelector(".nm") || {}).textContent || "", a.classList.contains("att-pdf"));
  });
  lb.addEventListener("click", e=>{ if(e.target === lb || e.target === body || e.target.closest(".lbx")) close(); });
  document.addEventListener("keydown", e=>{ if(e.key === "Escape" && lb.classList.contains("on")){ e.preventDefault(); close(); } });   // this Esc doesn't interrupt Claude
})();

// ── Plan usage (5-hour session / week) + context % per tile, from the silent statusLine ─────
let lastUsage = null;
function fmtReset(ts){
  if(!ts) return "";
  const d = new Date(ts*1000), hm = d.toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"});
  return "resets " + (d.toDateString() === new Date().toDateString() ? hm : d.toLocaleDateString([], {weekday:"short"}) + " " + hm);
}
function paintUsage(){
  const u = lastUsage, box = document.getElementById("usage"); if(!u || !box) return;
  const meter = (lbl, sh, v)=> v && v.pct != null ? `<span class="u" title="${lbl}: ${fmtReset(v.resets_at)}"><span class="ll">${lbl}</span><span class="ls">${sh}</span> ` +
    `<span class="bar"><i class="${v.pct>=80?"hi":""}" style="width:${Math.min(100,v.pct)}%"></i></span><b>${v.pct}%</b><span class="rs">· ${fmtReset(v.resets_at)}</span></span>` : "";
  box.innerHTML = meter("Session", "5h", u.five_hour) + meter("Week", "wk", u.seven_day);
  tilesArr().forEach((t,i)=>{ const c = t.querySelector(".ctx"), v = (u.ctx || {})[sessOf(i)]; if(c) c.textContent = v != null ? `ctx ${v}%` : ""; });
}
async function pollUsage(){
  try{ const r = await fetch("api/usage?t="+Date.now(), {cache:"no-store"}); if(r.ok){ lastUsage = await r.json(); paintUsage(); } }catch(e){}
}
setInterval(pollUsage, 15000); pollUsage();

// ── Esc in Chat mode = interrupt Claude in that tile, like in the terminal ───────────────────────
// In Chat the focus is on the page, not the xterm: without this the keyboard's Esc did nothing.
// The viewer has priority: if it's open, Esc closes it and doesn't interrupt.
function interruptTile(idx){
  const t = tilesArr()[idx]; if(!t) return;
  chatSend(idx, {key:"Escape"});
  const w = t.querySelector(".working");
  if(w){ w.classList.add("spin"); w.querySelector(".verb").textContent = "⏹ interrupting…"; w.querySelector(".meta").textContent = ""; }
}
document.addEventListener("keydown", e=>{
  if(e.key !== "Escape" || e.defaultPrevented) return;
  const lb = document.getElementById("lightbox"); if(lb && lb.classList.contains("on")) return;
  const t = tilesArr()[activeIdx]; if(!t || t.dataset.mode !== "chat") return;
  e.preventDefault(); interruptTile(activeIdx);
});

// ── Tap the mode pill = Shift+Tab in that tile (normal → accept edits → plan → auto) ──────────
document.addEventListener("click", e=>{
  const m = e.target.closest(".tbar .mode"); if(!m) return;
  const idx = tilesArr().indexOf(m.closest(".tile")); if(idx < 0) return;
  m.classList.add("busy"); setTimeout(()=> m.classList.remove("busy"), 3500);
  chatSend(idx, {key:"BTab"}); setTimeout(pollStatus, 900);
});

// ── Disconnected terminal: ttyd shows "Press ⏎ to Reconnect" and waits for an Enter nobody sees
// in Chat mode. Read it from the (same-origin) iframe and reload the terminal (max once / 10 s).
const DISCONNECTED = /Press ⏎ to Reconnect|Connection Closed/;
setInterval(()=> tilesArr().forEach((t,i)=>{
  const ifr = t.querySelector("iframe"); if(!ifr) return;
  let off = false;
  try{ const b = ifr.contentDocument && ifr.contentDocument.body; off = !!(b && DISCONNECTED.test(b.innerText || "")); }catch(e){}
  t.dataset.conn = off ? "off" : "on";
  if(off && Date.now() - (+t.dataset.retry || 0) > 10000){ t.dataset.retry = Date.now(); ifr.src = TTY(sessOf(i)); }
}), 3000);
document.addEventListener("click", e=>{
  const c = e.target.closest(".tbar .conn"); if(!c) return;
  const t = c.closest(".tile"); t.dataset.retry = Date.now();
  t.querySelector("iframe").src = TTY(sessOf(tilesArr().indexOf(t)));
});
