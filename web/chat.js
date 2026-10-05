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
// Formulas (optional: only if KaTeX is installed, see server.py /katex): $$…$$ and \[…\] as a block, $…$
// and \(…\) inline. They're taken out BEFORE the markdown (or the `*` and `_` of the LaTeX turn into italics)
// and left as a \u0001N\u0001 marker, skipping ``` blocks and `code`. The inline $ follows Pandoc's rule so
// prices survive: no space right after the opening $ or before the closing one, and the closing one is not
// followed by a digit ("$1M to $2M", "US$100" and "$500k–$800k" stay text). If KaTeX isn't there or the
// formula doesn't compile, the raw text stays.
const MATH_RE = /`[^`\n]+`|\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]|\\\(([\s\S]+?)\\\)|(?<![\\$\w])\$(?=[^\s$])((?:\\.|[^$\\\n])+?)(?<=[^\s\\])\$(?![\d$])/g;
function md(src){
  const math = [], segs = []; let fence = false, buf = [];
  const flush = () => { if(buf.length) segs.push(buf.join("\n").replace(MATH_RE, (m, d1, d2, i1, i2) => {
    if(m[0] === "`" || typeof katex === "undefined") return m;
    const tex = d1 ?? d2 ?? i1 ?? i2, display = d1 != null || d2 != null;
    try{ math.push(katex.renderToString(tex.trim(), {displayMode: display, throwOnError: true, output: "html"})); }
    catch(e){ return m; }
    return "\u0001" + (math.length-1) + "\u0001";
  })); buf = []; };
  for(const l of String(src).split("\n")){
    if(/^\s*```/.test(l)){ if(!fence) flush(); fence = !fence; segs.push(l); continue; }
    if(fence) segs.push(l); else buf.push(l);
  }
  flush();
  const html = mdBlocks(segs.join("\n"));
  return math.length ? html.replace(/\u0001(\d+)\u0001/g, (_, n) => math[+n]) : html;
}
const b64u = s => { let b = ""; for(const c of new TextEncoder().encode(s)) b += String.fromCharCode(c);
  return btoa(b).replace(/\+/g,"-").replace(/\//g,"_").replace(/=+$/,""); };
const DOT_LANGS = {dot:"dot", graphviz:"dot", gv:"dot", neato:"neato", fdp:"fdp", circo:"circo", twopi:"twopi"};
function mdBlocks(src){
  const L = src.split("\n"), out = []; let i = 0;
  const isList = l => /^\s*([-*]|\d+\.)\s+/.test(l);
  while(i < L.length){
    const l = L[i];
    if(/^\s*```/.test(l)){
      const lang = l.trim().slice(3).trim(), buf = []; i++;
      while(i < L.length && !/^\s*```/.test(L[i])) buf.push(L[i++]);
      i++;
      const code = `<div class="cb"><div class="cb-h">${esc(lang||"text")}<button data-copy>Copy</button></div><pre>${esc(buf.join("\n"))}</pre></div>`;
      // ```dot / ```graphviz (or the engine: ```neato, ```fdp…) → a diagram the server renders (optional:
      // needs Graphviz). The code stays folded underneath; if it doesn't render, the image goes and the code opens.
      const eng = DOT_LANGS[lang.toLowerCase()], g = eng && b64u(buf.join("\n"));
      if(g && g.length <= 30000){
        const u = `dot?v=1&e=${eng}&g=${g}`;
        out.push(`<figure class="dotfig"><a href="${u}" target="_blank" rel="noopener"><img alt="diagram" src="${u}" onerror="const f=this.closest('figure');f.classList.add('broken');f.querySelector('details').open=true"></a><details><summary>DOT</summary>${code}</details></figure>`);
        continue;
      }
      out.push(code);
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
      out.push(`<blockquote>${mdBlocks(buf.join("\n"))}</blockquote>`); continue;
    }
    if(!l.trim()){ i++; continue; }
    const buf = [];
    while(i < L.length && L[i].trim() && !/^(\s*```|#{1,4}\s|\s*([-*]|\d+\.)\s+|>|\s*\|)/.test(L[i])) buf.push(L[i++]);
    if(!buf.length) buf.push(L[i++]);
    out.push("<p>"+buf.map(mdInline).join("<br>")+"</p>");
  }
  return out.join("");
}

// ── Attachments: images and PDFs open in the in-page viewer; any other uploaded file is a card ──
// that downloads it (uploads/<day>/HHMMSS_<name> is how the server names what you attach)
const ATT_RE = /(?:^|[\s(`'"])(\/[^\s`'"()<>]+\.(?:png|jpe?g|gif|webp|pdf)|\/[^\s`'"()<>]*\/uploads\/\d{4}-\d\d-\d\d\/\d{6}_[^\s`'"()<>]+\.[a-z0-9]{1,5})(?=$|[\s)`'".,;:])/gi;
const extLabel = n => ((String(n).match(/\.([a-z0-9]{1,5})$/i) || [,"FILE"])[1]).toUpperCase();
const viewURL = p => "view?p=" + encodeURIComponent(p);
function attHTML(path){
  const name = String(path).split("/").pop(), web = /^https?:/i.test(path);
  if(!web && /\.pdf$/i.test(path))
    return `<a class="att-pdf" href="${viewURL(path)}" target="_blank" rel="noopener"><span class="ic">PDF</span><span class="nm">${esc(name)}</span></a>`;
  if(web || /\.(png|jpe?g|gif|webp)$/i.test(path)){
    const src = web ? esc(path) : viewURL(path);
    return `<a class="att-img" title="${esc(name)}" href="${src}" target="_blank" rel="noopener"><img loading="lazy" alt="${esc(name)}" onerror="this.parentNode.classList.add('broken')" src="${src}"></a>`;
  }
  if(/^\//.test(path))
    return `<a class="att-pdf other" href="dl?p=${encodeURIComponent(path)}" download="${esc(name)}" title="${esc(path)}"><span class="ic">${esc(extLabel(name))}</span><span class="nm">${esc(name)}</span></a>`;
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
  } else if(e.k === "think"){                       // thinking the terminal shows too: muted, but not hidden
    const d = document.createElement("div"); d.className = "m-a think"; d.innerHTML = md(e.text); msgs.append(d);
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
    // already visible in one of your messages → don't repeat the thumbnail when Claude reads it
    const shown = [...msgs.querySelectorAll(".m-u .att-img, .m-u .att-pdf")].some(a => (a.getAttribute("href")||"") === viewURL(fp));
    if(e.name === "Read" && /\.(png|jpe?g|gif|webp|pdf)$/i.test(fp) && !shown){
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
  if(!scr || (!scr.setup && !(scr.opts || []).length)){ c.perm.hidden = true; c.perm.innerHTML = ""; c.perm.dataset.key = ""; return; }
  const steps = [...c.msgs.querySelectorAll(".stp")];
  const pend = steps.length && !steps[steps.length-1].querySelector(".r").innerHTML ? steps[steps.length-1] : null;
  if(pend){ pend.classList.add("waiting"); const g = pend.closest(".grp"); if(g) g.open = true; }
  const key = JSON.stringify(scr) + (pend ? pend.dataset.id : ""); if(c.perm.dataset.key === key) return;
  if(scr.setup) return renderSetup(c, scr, key);
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
// First-run / sign-in screens (theme, login method, the sign-in link + its code, trust this folder,
// Press Enter…): Claude waits for an answer before any transcript exists, so they come from the screen.
function renderSetup(c, scr, key){
  c.perm.dataset.key = key; c.perm.hidden = false;
  const opts = scr.opts || [];
  c.perm.innerHTML = `<div class="q">${esc(scr.question || "Claude is setting up — it's waiting for you")}</div>` +
    (scr.detail && scr.detail.trim() ? `<div class="what"><pre>${esc(scr.detail)}</pre></div>` : "") +
    (scr.url ? `<div class="surl"><a href="${esc(scr.url)}" target="_blank" rel="noopener">Open the sign-in page ↗</a><button type="button" data-copyurl="1">Copy link</button></div>` : "") +
    (scr.code ? '<div class="shint">Sign in there, then paste the code it gives you in the box below and send it.</div>' : "") +
    '<div class="opts">' + (opts.length
      ? opts.map((o,i)=>`<button type="button" class="${i===scr.cur?"yes":""}" data-pick="${esc(o.n)}" title="${esc(o.label)}">${esc(o.label)}</button>`).join("")
      : (scr.code ? "" : '<button type="button" class="yes" data-key="Enter">Enter ⏎</button>')) +
    '<button type="button" data-key="Escape" title="cancel">Esc</button></div>';
  c.perm.querySelector("[data-copyurl]")?.addEventListener("click", e=>{ copyText(window, scr.url);
    e.target.textContent = "Copied"; setTimeout(()=> e.target.textContent = "Copy link", 1200); });
}
// Text sitting in Claude's input box, NOT sent: typed in Term, or put back by Esc (the transcript
// keeps that message, so without this the chat shows it as sent while the terminal is still editing it).
function renderDraft(c, text){
  let dr = c.tile.querySelector(".cdraft");
  c.msgs.querySelectorAll(".m-u.back").forEach(x=> x.classList.remove("back"));
  if(!text){ c.tile.dataset.drseen = ""; if(dr) dr.remove(); return; }
  // Only if the SAME text stays in the box ≥3 s: when you send from the chat, the paste sits there for an
  // instant before the Enter, and the "not sent" box flashed on every message.
  const [seenTxt, seenAt] = JSON.parse(c.tile.dataset.drseen || '["",0]');
  if(seenTxt !== text){ c.tile.dataset.drseen = JSON.stringify([text, Date.now()]); if(dr) dr.remove(); return; }
  if(!dr && Date.now() - seenAt < 3000) return;
  const last = [...c.msgs.querySelectorAll(".m-u:not(.pend)")].pop();
  if(last && last.textContent.trim() === text.trim()) last.classList.add("back");
  if(!dr){ dr = document.createElement("div"); dr.className = "cdraft"; c.tile.querySelector(".ccwrap").before(dr); }
  if(dr.dataset.text === text) return;
  dr.dataset.text = text;
  dr.innerHTML = '<div class="dt">✎ In the terminal box, <b>not sent</b></div><pre></pre><div class="opts">' +
    '<button type="button" class="yes" data-dr="send">Send it</button><button type="button" data-dr="edit">Edit here</button>' +
    '<button type="button" data-dr="clear">Discard</button></div>';
  dr.querySelector("pre").textContent = text;
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
    const note = (html, scr) => { if(c.msgs.dataset.note !== html){ c.msgs.innerHTML = html; c.msgs.dataset.note = html; } st.sid = ""; st.off = 0; renderPerm(idx, scr || null); renderDraft(c, ""); };
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
    if(!d.sid && d.screen && d.screen.setup) return note('<div class="shellnote">Claude is getting ready (first run, sign-in or trusting this folder).<br>Answer it below ↓</div>', d.screen);
    if(d.fresh && !st.sid) return note('<div class="shellnote">New conversation: no messages yet.<br>Type below to start.</div>', d.screen);
    if(d.nohooks && !st.sid) return note('<div class="shellnote">No transcript for this session yet.<br>Chat needs the hooks (<code>install.sh --hooks</code>) — <b>Term</b> always works.</div>', d.screen);
    c.msgs.dataset.note = "";
    const stick = nearBottom(c.msgs);
    if(d.reset) c.msgs.innerHTML = d.partial ? '<div class="note">… earlier messages are in the terminal</div>' : "";
    if((d.events || []).length) c.perm.dataset.key = "";
    (d.events || []).forEach(e=> addEvent(c.msgs, e));
    st.sid = d.sid; st.off = d.off;
    renderPerm(idx, d.screen);
    renderDraft(c, d.draft || "");
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
  // The answer lands in the hidden iframe: nobody would see a failed send (a proxy body cap,
  // a session that is gone…), so a missing data-ok becomes a note in the chat.
  const sink = document.querySelector('iframe[name="chatsink"]');
  if(sink) sink.onload = ()=>{ sink.onload = null;
    let ok = false, why = "";
    try{ const d = sink.contentDocument; ok = !!d.querySelector("[data-ok]"); why = (d.body && d.body.innerText || "").trim().slice(0,160); }catch(e){ why = "no readable answer"; }
    if(ok) return;
    c.msgs.querySelectorAll(".m-u.pend").forEach(p=> p.classList.add("fail"));
    const n = document.createElement("div"); n.className = "note";
    n.textContent = "⚠ not sent" + (files && files.length ? " (with attachments)" : "") + (why ? ": " + why : "");
    c.msgs.append(n); c.msgs.scrollTop = c.msgs.scrollHeight; };
  document.body.append(f); f.submit(); f.remove();
  setTimeout(()=> pollChat(idx), 700);
}
// A navigational POST into an iframe of its own, as a promise (auth proxies can kill XHR POSTs).
// → {ok, text, path}; path = the data-path the last piece of a large attachment returns.
function sinkPost(fields, file){
  return new Promise(async res=>{
    const name = "up" + Math.random().toString(36).slice(2);
    const ifr = document.createElement("iframe"); ifr.name = name; ifr.hidden = true;
    // Into the DOM first and wait for its about:blank: otherwise that first load is taken as the
    // answer (empty ⇒ "failed") and the submit, with the frame not ready, opens the answer in a new tab.
    await new Promise(r=>{ ifr.onload = r; ifr.src = "about:blank"; document.body.append(ifr); setTimeout(r, 1500); });
    const f = document.createElement("form"); f.method = "post"; f.action = "chat/send";
    f.target = name; f.hidden = true; f.enctype = "multipart/form-data";
    const dt = new DataTransfer(); dt.items.add(file);
    const fi = document.createElement("input"); fi.type = "file"; fi.name = "f"; fi.files = dt.files; f.append(fi);
    Object.entries(fields).forEach(([k,v])=>{ const i = document.createElement("input"); i.type = "hidden"; i.name = k; i.value = v; f.append(i); });
    ifr.onload = ()=>{ let r = {ok:false, text:"no readable answer", path:""};
      try{ const d = ifr.contentDocument, pe = d.querySelector("[data-path]");
        r = {ok: !!d.querySelector("[data-ok]"), text: (d.body && d.body.innerText || "").trim().slice(0,160), path: pe ? pe.dataset.path : ""}; }catch(e){}
      ifr.remove(); res(r); };
    document.body.append(f); f.submit(); f.remove();
  });
}
// Attachments over 90 MB in total: each file goes in 40 MB pieces (Cloudflare caps a body at 100 MB),
// one at a time, each with its SHA-256; then the text with the paths is sent like any message.
const CHUNK = 40*1024*1024;
async function chunkedSend(idx, text, files, bubble){
  const c = chatEls(idx); if(!c) return;
  const prog = document.createElement("div"); prog.className = "uprog"; bubble.append(prog);
  const fail = why=>{ bubble.classList.add("fail"); prog.textContent = "";
    const n = document.createElement("div"); n.className = "note"; n.textContent = "⚠ not sent: " + why;
    c.msgs.append(n); c.msgs.scrollTop = c.msgs.scrollHeight; };
  const paths = [], total = files.reduce((n,f)=> n + f.size, 0); let done = 0;
  for(const file of files){
    const n = Math.max(1, Math.ceil(file.size / CHUNK)), up = Math.random().toString(36).slice(2, 14).padEnd(8, "0");
    if(n > 64) return fail(file.name + " is too large (max ~2.5 GB)");
    for(let i = 0; i < n; i++){
      prog.textContent = `uploading ${Math.round(done / total * 100)}%  (${(done/1048576).toFixed(0)} of ${(total/1048576).toFixed(0)} MB)`;
      const part = new File([file.slice(i*CHUNK, (i+1)*CHUNK)], file.name, {type: "application/octet-stream"});
      const sha = [...new Uint8Array(await crypto.subtle.digest("SHA-256", await part.arrayBuffer()))]
        .map(b=> b.toString(16).padStart(2, "0")).join("");                 // the server checks it before appending
      let r;
      for(let t = 0; t < 3; t++){                                           // damaged piece or network hiccup: retry
        r = await sinkPost({up, ui: i, un: n, sha, sz: file.size, s: c.sel.value}, part);
        if(r.ok) break;
      }
      if(!r.ok) return fail(file.name + ", piece " + (i+1) + "/" + n + ": " + r.text);
      done += part.size;
      if(i === n - 1) paths.push(r.path);
    }
  }
  prog.textContent = "uploaded ✔ (SHA-256 checked per piece)";
  chatSend(idx, {text: (text ? text + "\n\n" : "") + paths.join("\n")});
}
function chatHTML(){
  const clip = '<svg class="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.5l-8.6 8.6a5 5 0 0 1-7.1-7.1l8.6-8.6a3.3 3.3 0 0 1 4.7 4.7l-8.6 8.6a1.7 1.7 0 0 1-2.4-2.4l7.9-7.9"/></svg>';
  const up = '<svg class="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5M5 12l7-7 7 7"/></svg>';
  return '<div class="cmsgs"></div>' +
    '<div class="working"><span class="star">✻</span><span class="verb">Working…</span><span class="meta"></span></div>' +
    '<div class="cperm" hidden></div>' +
    '<div class="ccwrap"><div class="cprev"></div><div class="ccomp">' +
    `<button type="button" class="attbtn" title="attach files: images, PDFs, zips… (or paste / drop them)">${clip}</button>` +
    '<input type="file" multiple hidden>' +
    '<textarea rows="1" placeholder="Message Claude…  (Enter sends · Shift+Enter new line · paste images)"></textarea>' +
    `<button type="button" class="sendbtn" title="send">${up}</button></div></div>`;
}
function wireChat(tile, idx){
  const msgs = tile.querySelector(".cmsgs"), ta = tile.querySelector("textarea"), btn = tile.querySelector(".sendbtn");
  const prev = tile.querySelector(".cprev"), fin = tile.querySelector('.ccomp input[type="file"]'), chat = tile.querySelector(".chat");
  let pending = [];                                   // [{file, url}]
  const okFile = f => f.size > 0;                    // any type (the server keeps whatever arrives); size 0 = a dropped folder
  const grow = ()=>{ btn.disabled = !ta.value.trim() && !pending.length; ta.style.height = "auto";
    if(ta.scrollHeight) ta.style.height = Math.min(ta.scrollHeight, 150) + "px"; };   // measures 0 outside the DOM
  const drawPrev = ()=>{
    prev.innerHTML = pending.map((p,i)=> /^image\//.test(p.file.type)
      ? `<div class="pv"><img src="${p.url}" alt=""><button type="button" class="x" data-i="${i}">✕</button></div>`
      : `<div class="pv pdf${extLabel(p.file.name)==="PDF"?"":" other"}"><span class="ic">${esc(extLabel(p.file.name))}</span><span class="nm">${esc(p.file.name)}</span><button type="button" class="x" data-i="${i}">✕</button></div>`).join("");
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
  tile._addFiles = list=>{ addFiles(list); ta.focus(); }; tile._pickFiles = ()=> fin.click();   // the upload button in Chat mode
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
    const big = pending.reduce((n,x)=> n + x.file.size, 0) > 90*1024*1024;   // proxies cap a body at ~100 MB → in pieces
    const p = document.createElement("div"); p.className = "m-u pend";
    if(text){ const t = document.createElement("div"); t.textContent = text; p.append(t); }
    if(pending.length) p.insertAdjacentHTML("beforeend", '<div class="atts">' + pending.map(x=> x.url
      ? `<span class="att-img"><img src="${x.url}" alt=""></span>` : `<span class="att-pdf${extLabel(x.file.name)==="PDF"?"":" other"}"><span class="ic">${esc(extLabel(x.file.name))}</span><span class="nm">${esc(x.file.name)}</span></span>`).join("") + "</div>");
    // Still gray after 1.5 s = QUEUED (Claude is busy): offer "send now". This doesn't rely on the
    // tile's state, which can lag; if the message was delivered, the pending bubble is already gone.
    setTimeout(()=>{ if(p.isConnected && !p.querySelector(".sendnow"))
      p.insertAdjacentHTML("afterbegin", '<button type="button" class="sendnow" title="interrupt what Claude is doing so it reads this now (Esc + Enter)">send now</button>'); }, 1500);
    msgs.append(p); msgs.scrollTop = msgs.scrollHeight;
    if(big) chunkedSend(idx, text, pending.map(x=> x.file), p);
    else chatSend(idx, {text}, pending.map(x=> x.file));
    pending = []; prev.innerHTML = ""; ta.value = ""; grow();
  };
  ta.addEventListener("keydown", e=>{ if(e.key === "Enter" && !e.shiftKey && !e.isComposing){ e.preventDefault(); send(); } });
  btn.addEventListener("click", send);
  tile.querySelector(".cperm").addEventListener("click", e=>{
    const b = e.target.closest("button[data-key]"); if(b) chatSend(idx, {key:b.dataset.key});
    const p = e.target.closest("button[data-pick]"); if(p){ p.disabled = true; chatSend(idx, {pick:p.dataset.pick}); }
  });
  tile.querySelector(".chat").addEventListener("click", e=>{   // the "not sent" box: send it / edit it here / discard it
    const b = e.target.closest("[data-dr]"); if(!b) return;
    const dr = b.closest(".cdraft"), text = dr ? dr.dataset.text : "";
    if(b.dataset.dr === "send") chatSend(idx, {key:"Enter"});
    else { if(b.dataset.dr === "edit"){ ta.value = text; grow(); ta.focus(); } chatSend(idx, {clear:"1"}); }
    if(dr) dr.remove();
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
    if(!a || e.target.closest("#lightbox") || a.classList.contains("broken") || a.classList.contains("other")) return;   // "other" downloads
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
  tilesArr().forEach((t,i)=>{ const c = t.querySelector(".ctx"), v = (u.ctx || {})[sessOf(i)]; if(c) c.innerHTML = v != null ? `<span class="cl">ctx </span>${v}%` : ""; });
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
  const cm = document.getElementById("clipmenu"); if(cm && !cm.hidden){ cm.hidden = true; e.preventDefault(); return; }
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

// ── Clip history (⌄): clip.txt + clip-1..3.txt, rotated by cr-clip ─────────────────────────────
// The clip is one for every session; when another session overwrote yours, the previous ones are here.
(function clipHistory(){
  const btn = document.getElementById("cliphist"), menu = document.getElementById("clipmenu");
  if(!btn || !menu) return;
  const NAMES = ["clip.txt", "clip-1.txt", "clip-2.txt", "clip-3.txt"];
  let items = [];
  const when = d => { if(!d) return ""; const t = new Date(d);
    return (t.toDateString() === new Date().toDateString() ? "" : t.toLocaleDateString([], {day:"2-digit", month:"2-digit"}) + " ") +
           t.toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"}); };
  async function load(){
    items = (await Promise.all(NAMES.map(async (n, i)=>{
      try{ const r = await fetch(n + "?t=" + Date.now(), {cache:"no-store"}); if(!r.ok) return null;
        const text = (await r.text()).replace(/\n+$/, ""); if(!text) return null;
        return {i, text, at: r.headers.get("Last-Modified")}; }catch(e){ return null; }
    }))).filter(Boolean);
  }
  async function open(){
    await load();
    menu.innerHTML = '<div class="ct">last clips — tap one to copy it</div>' + (items.length ? items.map((x,k)=>
      `<button type="button" data-k="${k}"><div class="cm-h"><span>${x.i === 0 ? "<b>current</b>" : "previous " + x.i}</span><span>${when(x.at)}</span></div>` +
      `<div class="cm-t">${esc(x.text.slice(0, 400))}</div></button>`).join("") : '<div class="empty">no clips yet</div>');
    const r = btn.getBoundingClientRect();
    menu.style.top = (r.bottom + 6) + "px";
    menu.style.left = Math.max(8, Math.min(r.right - 300, window.innerWidth - 310)) + "px";
    menu.hidden = false;
  }
  btn.addEventListener("click", e=>{ e.stopPropagation(); menu.hidden ? open() : (menu.hidden = true); });
  menu.addEventListener("click", e=>{
    const b = e.target.closest("button[data-k]"); if(!b) return;
    const x = items[+b.dataset.k]; if(!x) return;
    copyText(window, x.text); if(x.i === 0){ clipMarkSeen(x.text); clipDot(false); }
    menu.hidden = true; clipFlash("✓ copied");
  });
  document.addEventListener("click", e=>{ if(!menu.hidden && !menu.contains(e.target) && e.target !== btn) menu.hidden = true; });
})();
