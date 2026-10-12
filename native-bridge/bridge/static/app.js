const $ = (id) => document.getElementById(id);
/* escape untrusted strings before putting them in innerHTML (XSS) */
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const ACCOUNT_FIELDS = ["region","email","password","av_account","iotype_start",
  "av_channel","region_code","license_key","webrtc_candidate","ui_user","ui_pass"];

/* ---------- feedback helpers ---------- */
function toast(msg, kind="") {
  const t = document.createElement("div");
  t.className = "toast " + kind;
  t.textContent = msg;
  $("toasts").appendChild(t);
  setTimeout(() => { t.style.opacity = "0"; setTimeout(() => t.remove(), 300); }, 3200);
}
async function withLoading(btn, fn) {
  if (!btn) return fn();
  btn.classList.add("loading"); btn.disabled = true;
  try { return await fn(); }
  finally { btn.classList.remove("loading"); btn.disabled = false; }
}

/* ---------- copy (works on plain http too, where navigator.clipboard is blocked) ---------- */
async function copyText(text){
  try {
    if (navigator.clipboard && window.isSecureContext){ await navigator.clipboard.writeText(text); return true; }
  } catch(e){}
  try {
    const ta = document.createElement("textarea");
    ta.value = text; ta.setAttribute("readonly","");
    ta.style.position="fixed"; ta.style.top="-1000px"; ta.style.opacity="0";
    document.body.appendChild(ta); ta.select(); ta.setSelectionRange(0, text.length);
    const ok = document.execCommand("copy"); document.body.removeChild(ta); return ok;
  } catch(e){ return false; }
}
/* copy buttons: data-copy=<id>  OR  .copy inside a .url-row (copies its <code>) */
document.addEventListener("click", async (e) => {
  const b = e.target.closest("[data-copy], .copy"); if (!b) return;
  let text = "";
  if (b.dataset.copy) text = $(b.dataset.copy).textContent;
  else { const row = b.closest(".url-row"); if (row) text = row.querySelector("code").textContent; }
  const ok = await copyText(text);
  const o = b.textContent; b.textContent = ok ? "copied!" : "select & copy";
  setTimeout(() => b.textContent = o, 1300);
});

/* ---------- account ---------- */
function collectAccount() {
  const o = {};
  for (const f of ACCOUNT_FIELDS) { const el = $(f); if (el) o[f] = el.value; }
  return o;
}
async function loadAccount() {
  const cfg = await (await fetch("/api/config")).json();
  for (const f of ACCOUNT_FIELDS) { const el = $(f); if (el && cfg[f] !== undefined) el.value = cfg[f]; }
  if (cfg.password === "********") $("password").placeholder = "(saved — leave to keep)";
}
async function saveAccount(btn) {
  return withLoading(btn, async () => {
    const body = collectAccount();
    if (body.password === "********") delete body.password;
    if (body.ui_pass === "********") delete body.ui_pass;
    const r = await fetch("/api/config", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
    toast(r.ok ? "Account saved." : "Couldn't save — config folder not writable (see log).", r.ok ? "good" : "bad");
    if (r.ok) loadAccount();   // re-mask the saved password so it isn't re-POSTed
    refreshStatus();
  });
}
async function testLogin(btn) {
  return withLoading(btn, async () => {
    const body = {region:$("region").value, email:$("email").value, password:$("password").value};
    if (!body.email && !body.password) { toast("Enter your Owlet email + password first.", "warn"); return; }
    if (!body.password) delete body.password;   // saved account: backend uses stored password
    await fetch("/api/diagnose", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
    toast("Testing your Owlet login… watch the log.", "");
    pollFindings();
  });
}

/* ---------- cameras ---------- */
function camUrls(name, s){
  const host = location.hostname, rp = s.rtsp_port || "8554", hp = s.http_port || "1984";
  return {
    rtsp: s.rtsp_user ? `rtsp://${s.rtsp_user}:<GO2RTC_PASS>@${host}:${rp}/${name}`
                      : `rtsp://${host}:${rp}/${name}`,
    web:  `http://${host}:${hp}/stream.html?src=${name}`,
    hls:  `http://${host}:${hp}/api/stream.m3u8?src=${name}`,
  };
}
function fmtBytes(n){ if(!n) return ""; const u=["B","KB","MB","GB"]; let i=0;
  while(n>=1024&&i<3){n/=1024;i++;} return n.toFixed(i?1:0)+" "+u[i]; }

function buildCard(cam, ports){
  const el = document.createElement("section");
  el.className = "card cam"; el.dataset.name = cam.name;
  const u = camUrls(cam.name, ports);
  el.innerHTML = `
    <div class="cam-head">
      <h2>📹 <span class="cam-title">${cam.name}</span></h2>
      <span class="live-badge"><span class="dot"></span><span class="lt">offline</span></span>
      <span class="spacer"></span><span class="stats"></span>
    </div>
    <div class="urls">
      <div class="url-row"><span class="lbl">RTSP</span><code class="u-rtsp">${u.rtsp}</code><button class="mini copy">copy</button></div>
      <div class="url-row"><span class="lbl">WebRTC</span><code class="u-web">${u.web}</code><button class="mini copy">copy</button></div>
      <div class="url-row"><span class="lbl">HLS</span><code class="u-hls">${u.hls}</code><button class="mini copy">copy</button></div>
    </div>
    <div class="row">
      <button class="connect primary"><span class="spin"></span>↻ Connect / refresh key</button>
      <button class="snap">📷 Snapshot ↗</button>
      <button class="adv-toggle mini">Advanced</button>
      <span class="spacer"></span>
      <button class="remove mini danger">Remove</button>
    </div>
    <div class="cam-banner"></div>
    <div class="cam-adv hidden">
      <p class="hint">Auto-filled by Connect. You normally don't touch these.</p>
      <div class="grid">
        <label>Camera DSN <input class="f-dsn"></label>
        <label>UID <input class="f-uid"></label>
        <label>AuthKey <input class="f-authkey"></label>
        <label>AV password <input type="password" class="f-avpw" placeholder="(saved)"></label>
        <label>Security mode
          <select class="f-sec">
            <option value="">auto-probe</option><option value="2">Auto</option>
            <option value="1">DTLS</option><option value="0">Simple</option>
          </select></label>
        <label style="grid-column:1/-1"><input type="checkbox" class="f-skip-spk"> Skip SPEAKERSTART IOCTL
          <span class="hint" style="display:inline"> — enable if Talk drops the video. Some cameras open speaker bi-directionally from AUDIOSTART alone.</span>
        </label>
      </div>
      <div class="row"><button class="save-adv">Save camera settings</button></div>
    </div>`;

  // advanced inputs — set once on creation so polling never clobbers typing
  el.querySelector(".f-dsn").value = cam.camera_dsn || "";
  el.querySelector(".f-uid").value = cam.uid || "";
  el.querySelector(".f-authkey").value = cam.authkey || "";
  el.querySelector(".f-sec").value = cam.av_security_mode || "";
  el.querySelector(".f-skip-spk").checked = !!(cam.skip_speakerstart);

  el.querySelector(".connect").onclick = (e) => withLoading(e.currentTarget, async () => {
    await fetch(`/api/cameras/${cam.name}/diagnose`, {method:"POST"});
    toast(`Connecting ${cam.name}… watch the log.`, "");
    setTimeout(refreshCameras, 2500);
  });
  el.querySelector(".snap").onclick = () => {
    const w = window.open(`/img/${cam.name}.jpg`, "_blank", "noopener");
    if (!w) toast("Popup blocked — open /img/" + cam.name + ".jpg", "warn");
  };
  el.querySelector(".adv-toggle").onclick = (e) => {
    const adv = el.querySelector(".cam-adv"); adv.classList.toggle("hidden");
    e.currentTarget.textContent = adv.classList.contains("hidden") ? "Advanced" : "Hide";
  };
  el.querySelector(".remove").onclick = async () => {
    if (!confirm(`Remove camera "${cam.name}"? Its stream stops immediately.`)) return;
    await fetch(`/api/cameras/${cam.name}`, {method:"DELETE"});
    toast(`Removed ${cam.name}.`, "good"); refreshCameras();
  };
  el.querySelector(".save-adv").onclick = (e) => withLoading(e.currentTarget, async () => {
    const body = {
      camera_dsn: el.querySelector(".f-dsn").value,
      uid: el.querySelector(".f-uid").value,
      authkey: el.querySelector(".f-authkey").value,
      av_security_mode: el.querySelector(".f-sec").value,
      skip_speakerstart: el.querySelector(".f-skip-spk").checked ? "1" : "",
    };
    const avpw = el.querySelector(".f-avpw").value;
    if (avpw) body.av_password = avpw;
    const r = await fetch(`/api/cameras/${cam.name}`, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
    toast(r.ok ? "Camera settings saved · stream restarting…" : "Couldn't save (see log).", r.ok ? "good" : "bad");
    setTimeout(refreshCameras, 1500);
  });
  return el;
}

function updateCard(el, cam){
  const up = cam.stream_up;
  const lb = el.querySelector(".live-badge"), lt = el.querySelector(".lt");
  lb.classList.toggle("live", up);
  lt.textContent = cam.busy ? "connecting…" : (up ? "live" : (cam.have_key ? "starting…" : "no key"));
  const st = el.querySelector(".stats");
  let h = "";
  if (up){ h += `<span class="chip ok">● live</span>`;
    if (cam.codec) h += `<span class="chip">${cam.codec}</span>`;
    if (cam.recv) h += `<span class="chip">${fmtBytes(cam.recv)}</span>`; }
  st.innerHTML = h;
  el.querySelector(".connect").classList.toggle("loading", !!cam.busy);

  const b = el.querySelector(".cam-banner");
  if (!cam.camera_dsn) b.innerHTML = banner("warn","✏️","Set this camera's DSN in <b>Advanced</b>, then Connect.");
  else if (cam.busy) b.innerHTML = banner("","⏳","Logging in and fetching the camera key…");
  else if (!cam.have_key) b.innerHTML = banner("warn","🔑","No camera key yet. Click <b>Connect / refresh key</b>.");
  else if (!up) b.innerHTML = banner("","⏳","Key set — stream is starting (first connect takes ~10s).");
  else b.innerHTML = banner("good","✅","Live. Point Frigate at the RTSP URL above.");
}

async function refreshCameras() {
  let data; try { data = await (await fetch("/api/cameras")).json(); } catch(e){ return; }
  window._ports = data;
  const list = $("camera-list");
  const have = new Set(data.cameras.map(c => c.name));
  // remove cards for cameras that are gone
  [...list.children].forEach(el => { if (!have.has(el.dataset.name)) el.remove(); });
  // add / update
  for (const cam of data.cameras) {
    let el = [...list.children].find(c => c.dataset.name === cam.name);
    if (!el) { el = buildCard(cam, data); list.appendChild(el); }
    updateCard(el, cam);
  }
  $("cameras-empty").style.display = data.cameras.length ? "none" : "flex";
  $("cam-count").textContent = data.cameras.length
    ? `${data.cameras.length} camera${data.cameras.length>1?"s":""}` : "";
  syncTalkCams(data.cameras.map(c => c.name));
}

/* ---------- talk & sounds (send audio TO the camera speaker) ---------- */
function syncTalkCams(names){
  const sel = $("talk-cam"); if(!sel) return;
  const cur = sel.value;
  const want = names.join(",");
  if (sel.dataset.names === want) return;       // unchanged
  sel.dataset.names = want;
  sel.innerHTML = names.map(n => `<option>${n}</option>`).join("");
  if (names.includes(cur)) sel.value = cur;
  $("card-talk").style.display = names.length ? "" : "none";
}
function talkCam(){ const s=$("talk-cam"); return (s && s.value) || "owlet"; }

async function loadSounds(){
  let data; try { data = await (await fetch("/api/sounds")).json(); } catch(e){ return; }
  const box = $("sound-list"); if(!box) return;
  if (!data.sounds.length){ box.innerHTML = `<span class="hint">No sounds yet — drop some lullaby MP3s above.</span>`; return; }
  box.innerHTML = "";
  for (const f of data.sounds){
    const row = document.createElement("div"); row.className = "snd-row";
    row.innerHTML = `<button class="mini snd-play">▶</button><span class="snd-name">${esc(f)}</span>
      <span class="spacer"></span><button class="mini snd-del danger">🗑</button>`;
    row.querySelector(".snd-play").onclick = async () => {
      const loop = !!($("snd-loop") && $("snd-loop").checked);
      const timer_min = ($("snd-timer") ? parseFloat($("snd-timer").value) : 0) || 0;
      const r = await fetch(`/api/play/${talkCam()}`, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({file:f, loop, timer_min})});
      const j = await r.json();
      const extra = (loop?" 🔁":"") + (timer_min?` ⏱${timer_min}m`:"");
      toast(r.ok ? `▶ ${f} → ${talkCam()}${extra}` : (j.message||j.error||"couldn't play"), r.ok?"good":"bad");
    };
    row.querySelector(".snd-del").onclick = async () => {
      if(!confirm(`Delete ${f}?`)) return;
      await fetch(`/api/sounds/${encodeURIComponent(f)}`, {method:"DELETE"}); loadSounds();
    };
    box.appendChild(row);
  }
}
function uploadSounds(files){
  if(!files || !files.length) return;
  const prog=$("snd-prog"), bar=$("snd-bar"); prog.classList.add("show"); bar.style.width="0%";
  let done=0;
  const one = (f) => new Promise(res => {
    const fd=new FormData(); fd.append("file", f);
    const xhr=new XMLHttpRequest(); xhr.open("POST","/api/sounds");
    xhr.onload=()=>{ done++; bar.style.width=Math.round(done/files.length*100)+"%"; res(); };
    xhr.onerror=()=>{ done++; res(); };
    xhr.send(fd);
  });
  toast(`Uploading ${files.length} sound(s)…`,"");
  Promise.all([...files].map(one)).then(()=>{
    setTimeout(()=>prog.classList.remove("show"), 700);
    toast("Sounds uploaded.","good"); loadSounds();
  });
}

let _mediaRec=null, _chunks=[];
async function startTalk(){
  try{
    const stream = await navigator.mediaDevices.getUserMedia({audio:true});
    _chunks=[]; _mediaRec=new MediaRecorder(stream);
    _mediaRec.ondataavailable = e => { if(e.data.size) _chunks.push(e.data); };
    _mediaRec.onstop = async () => {
      stream.getTracks().forEach(t=>t.stop());
      const blob=new Blob(_chunks,{type:_mediaRec.mimeType||"audio/webm"});
      const fd=new FormData(); fd.append("audio", blob, "talk.webm");
      const r=await fetch(`/api/talk/${talkCam()}`,{method:"POST",body:fd});
      const j=await r.json(); $("talk-status").textContent="";
      toast(r.ok?"🔊 sent to the room":(j.message||j.error||"talk failed"), r.ok?"good":"bad");
    };
    _mediaRec.start();
    $("talk-status").textContent="🔴 recording… release to send";
  }catch(e){ toast("Mic blocked — allow microphone access (needs https or localhost).","warn"); }
}
function stopTalk(){ if(_mediaRec && _mediaRec.state!=="inactive") _mediaRec.stop(); }

async function addCamera(btn) {
  return withLoading(btn, async () => {
    const dsn = $("new-dsn").value.trim(), name = $("new-name").value.trim();
    if (!dsn) { toast("Enter the camera DSN (OCD…).", "warn"); return; }
    const r = await fetch("/api/cameras", {method:"POST", headers:{"Content-Type":"application/json"},
      body:JSON.stringify({name, camera_dsn:dsn})});
    const j = await r.json();
    if (!r.ok) { toast(j.error || "Couldn't add camera.", "bad"); return; }
    $("new-dsn").value = ""; $("new-name").value = "";
    toast(`Added ${j.name} — connecting… watch the log.`, "good");
    refreshCameras();
    [1500,4000,8000].forEach(t => setTimeout(refreshCameras, t));
  });
}

/* ---------- libraries ---------- */
async function extractLibs(btn) {
  return withLoading(btn, async () => {
    toast("Looking for your Owlet APK…", "");
    const r = await (await fetch("/api/extract_libs", {method:"POST"})).json();
    toast(r.message, r.ok ? "good" : "bad");
    refreshStatus();
  });
}
function uploadApk() {
  const f = $("apk-file").files[0];
  if (!f) { toast("Choose your Owlet .apkm / .apk first.", "warn"); return; }
  const btn = $("btn-upload"), prog = $("up-prog"), bar = $("up-bar");
  btn.classList.add("loading"); btn.disabled = true;
  prog.classList.add("show"); bar.style.width = "0%";
  const fd = new FormData(); fd.append("apk", f);
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/upload_apk");
  xhr.upload.onprogress = (e) => {
    if (e.lengthComputable) bar.style.width = Math.round(e.loaded / e.total * 100) + "%";
  };
  const done = (ok, msg) => {
    btn.classList.remove("loading"); btn.disabled = false;
    bar.style.width = "100%"; setTimeout(() => prog.classList.remove("show"), 800);
    toast(msg, ok ? "good" : "bad"); refreshStatus();
  };
  xhr.onload = () => {
    try { const r = JSON.parse(xhr.responseText); done(r.ok, r.message); }
    catch (e) { done(false, xhr.status === 413 ? "File too large." : "Upload failed."); }
  };
  xhr.onerror = () => done(false, "Upload failed — network error.");
  toast("Uploading… this can take a minute for a full APK.", "");
  xhr.send(fd);
}

/* ---------- status ---------- */
function setPill(id, state){ const el = $(id); if (el) el.className = "pill " + state; }
function banner(kind, ico, html){
  return `<div class="banner ${kind}"><span class="ico">${ico}</span><div>${html}</div></div>`;
}
function updateSetup(s){
  const steps = [["libs",s.have_libs],["login",s.have_login],
    ["uid",s.cameras_with_key>0],["stream",s.streams_live>0]];
  let activeSet=false, allDone=true;
  for (const [name,done] of steps){
    const li=document.querySelector(`#checklist li[data-step="${name}"]`); if(!li) continue;
    li.classList.remove("done","active","pending");
    if(done) li.classList.add("done");
    else if(!activeSet){ li.classList.add("active"); activeSet=true; allDone=false; }
    else { li.classList.add("pending"); allDone=false; }
  }
  $("setup").classList.toggle("complete", allDone);
  const fresh = !s.have_libs && !s.have_login;
  $("setup-title").textContent = allDone ? "🦉 Owlet bridge"
    : (fresh ? "👋 First time? Let's get your camera streaming" : "🚀 Get your camera streaming");
}
async function refreshStatus() {
  let s; try { s = await (await fetch("/api/status")).json(); } catch(e){ return; }
  setPill("pill-libs",  s.have_libs ? "on" : "off");
  setPill("pill-login", s.have_login ? "on" : "off");
  setPill("pill-cams",  s.cameras_with_key>0 ? "on" : (s.cameras>0 ? "warn" : "off"));
  setPill("pill-live",  s.streams_live>0 ? "on" : (s.cameras>0 ? "warn" : "off"));
  $("card-account").classList.toggle("done", s.have_login);
  $("card-libs").classList.toggle("done", s.have_libs);
  $("card-cameras").classList.toggle("done", s.streams_live>0);
  updateSetup(s);

  const lb = $("libs-banner");
  if (lb) lb.innerHTML = s.have_libs
    ? banner("good", "✅", "TUTK libraries loaded.")
    : banner("warn", "📦",
        "Not found. Upload your Owlet <code>.apkm</code> (or <code>.apk</code>) below — "
        + "they're never bundled here, you supply the app you downloaded.");
  if (!s.config_writable) {
    toastOnce("⛔ Config folder not writable — run chmod -R 777 on your appdata config folder and restart.");
  }
}
let _warned=false;
function toastOnce(msg){ if(_warned) return; _warned=true; toast(msg,"bad"); }

/* ---------- log + findings ---------- */
function startLogStream() {
  const es = new EventSource("/api/logs");
  const l = $("log");
  es.onmessage = (e) => { l.textContent += e.data + "\n"; l.scrollTop = l.scrollHeight; };
}
function pollFindings(){ let n=0; const t=setInterval(async()=>{ await refreshFindings(); if(++n>30) clearInterval(t); }, 1500); }
async function refreshFindings() {
  const data = await (await fetch("/api/findings")).json();
  const box = $("findings"); if (!box) return;
  if (!data.candidates || !data.candidates.length) {
    box.textContent = data.devices === 0
      ? "Login OK but 0 Ayla devices on this account (the cam isn't an Ayla device — that's expected)."
      : "Run a login test to populate candidates.";
    return;
  }
  box.innerHTML = "";
  for (const c of data.candidates) {
    const d = document.createElement("div"); d.className = "cand";
    d.innerHTML = `<span class="f">${esc(c.field)}</span><span class="v">${esc(c.value)}</span>`;
    box.appendChild(d);
  }
}
async function discover(btn) {
  return withLoading(btn, async () => {
    const ip = $("cam_ip").value.trim();
    await fetch("/api/discover", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({ip})});
    toast("Probing the LAN on UDP 63616…", "");
    setTimeout(refreshFindings, 9000);
  });
}

/* ---------- sensors / vitals ---------- */
const SLEEP_STATES = {0:"Unknown",1:"Awake",8:"Light sleep",15:"Deep sleep"};
// US/Imperial units by default: temperatures shown in °F (camera/sock report °C).
const VITAL_META = {
  heart_rate:{icon:"❤️",label:"Heart rate",unit:" bpm"},
  oxygen:{icon:"🫁",label:"Oxygen",unit:"%"},
  skin_temperature:{icon:"🌡️",label:"Skin temp",unit:"°F",temp:1},
  sleep_state:{icon:"😴",label:"Sleep",unit:"",enum:SLEEP_STATES},
  movement:{icon:"🤸",label:"Movement",unit:""},
  battery:{icon:"🔋",label:"Sock battery",unit:"%"},
  battery_minutes:{icon:"⏳",label:"Battery left",unit:" min"},
  base_station_on:{icon:"📡",label:"Base station",unit:"",bool:1},
  charging:{icon:"⚡",label:"Charging",unit:"",bool:1},
  signal_strength:{icon:"📶",label:"Signal",unit:" dBm"},
  temperature:{icon:"🌡️",label:"Room temp",unit:"°F",temp:1},
  humidity:{icon:"💧",label:"Humidity",unit:"%"},
  noise:{icon:"🔊",label:"Noise",unit:" dB"},
  brightness:{icon:"💡",label:"Brightness",unit:" lux"},
  motion:{icon:"🏃",label:"Motion",unit:"",bool:1},
  sound:{icon:"👂",label:"Sound",unit:"",bool:1},
  wifi_rssi:{icon:"📶",label:"Cam WiFi",unit:" dBm"},
};
const VITAL_ORDER = Object.keys(VITAL_META);
// /api/vitals already returns °F (units=us), so no client-side conversion.
function fmtVital(k,v){
  const m=VITAL_META[k]||{unit:""};
  if(v===null||v===undefined||v==="") return "—";
  if(m.enum) return m.enum[v]||v;
  if(m.bool) return (v&&v!=="0")?"On":"Off";
  return v+m.unit;
}
async function probeSensors(btn){
  return withLoading(btn, async () => {
    const r = await fetch("/api/vitals/discover",{method:"POST"});
    if(!r.ok){ toast("Add your Owlet account first.","warn"); return; }
    toast("Probing your Owlet devices… watch the log.","");
    setTimeout(loadVitals, 4000);
    setTimeout(loadVitals, 9000);
  });
}
async function loadVitals(){
  let data; try { data = await (await fetch("/api/vitals")).json(); } catch(e){ return; }
  const wrap=$("vitals-wrap");
  const devs=(data&&data.devices)||[];
  const withReadings=devs.filter(d=>d.sensors&&Object.values(d.sensors).some(v=>v!==null&&v!==undefined&&v!==""));
  if(!withReadings.length){ return; }  // keep the empty hint until we have data
  // age from the NEWEST device ts (cam sensors update independently of the sock)
  const newest = Math.max(...withReadings.map(d=>d.ts||data.ts||0), data.ts||0);
  const age = newest ? Math.max(0, Math.round(Date.now()/1000 - newest)) : null;
  wrap.innerHTML = withReadings.map(d=>{
    const keys=VITAL_ORDER.filter(k=>k in d.sensors);
    const chips=keys.map(k=>{
      const m=VITAL_META[k]; const v=d.sensors[k];
      const dim=(v===null||v===undefined||v==="")?" dim":"";
      return `<div class="vchip${dim}"><span class="vi">${m.icon}</span>
        <span class="vv">${fmtVital(k,v)}</span><span class="vl">${m.label}</span></div>`;
    }).join("");
    const badge=d.kind==="sock"?"🍼 Smart Sock":(d.kind==="cam"?"📷 Camera":"📦 Device");
    return `<div class="vcard glass"><div class="vhead">${badge}
      <span class="vmodel">${esc(d.model||d.dsn||"")}</span></div>
      <div class="vgrid">${chips||"<span class='hint'>no recognized sensors</span>"}</div></div>`;
  }).join("");
  if(age!==null){
    const stale = age>180 ? " — ⚠️ stale (sock may be off-base or asleep)" : "";
    wrap.innerHTML += `<div class="vts hint">updated ${age}s ago${stale}</div>`;
  }
}

/* ---------- Home Assistant / MQTT ---------- */
async function loadMqtt(){
  let m; try { m = await (await fetch("/api/mqtt")).json(); } catch(e){ return; }
  $("mqtt-enabled").checked = String(m.enabled).toLowerCase()==="1"||m.enabled===true;
  $("mqtt-host").value = m.host||"";
  $("mqtt-port").value = m.port||"1883";
  $("mqtt-user").value = m.user||"";
  $("mqtt-pass").value = m.password ? "********" : "";
  $("mqtt-prefix").value = m.prefix||"homeassistant";
  if(m.env_host){ $("mqtt-status").textContent="(an OWLET_MQTT_HOST env var is also set)"; }
}
function mqttBody(){
  return {enabled:$("mqtt-enabled").checked, host:$("mqtt-host").value.trim(),
    port:$("mqtt-port").value.trim(), user:$("mqtt-user").value.trim(),
    password:$("mqtt-pass").value, prefix:$("mqtt-prefix").value.trim()};
}
async function saveMqtt(btn){
  return withLoading(btn, async () => {
    const r = await fetch("/api/mqtt",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify(mqttBody())});
    const j = await r.json();
    toast(j.ok?"Home Assistant settings saved.":("Save failed: "+(j.error||"")), j.ok?"good":"bad");
    if(j.ok) loadMqtt();
  });
}
async function testMqtt(btn){
  return withLoading(btn, async () => {
    $("mqtt-status").textContent="Testing…";
    const r = await fetch("/api/mqtt/test",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify(mqttBody())});
    const j = await r.json();
    $("mqtt-status").textContent = j.ok ? (j.msg||"Connected ✓") : ("✗ "+(j.error||"failed"));
    toast(j.ok?"Broker reachable ✓":("MQTT: "+(j.error||"failed")), j.ok?"good":"bad");
  });
}

/* ---------- wire up ---------- */
$("btn-probe").onclick = (e) => probeSensors(e.currentTarget);
$("btn-save-mqtt").onclick = (e) => saveMqtt(e.currentTarget);
$("btn-test-mqtt").onclick = (e) => testMqtt(e.currentTarget);
$("btn-save-account").onclick = (e) => saveAccount(e.currentTarget);
$("btn-testlogin").onclick = (e) => testLogin(e.currentTarget);
$("btn-add-cam").onclick = (e) => addCamera(e.currentTarget);
$("btn-extract").onclick = (e) => extractLibs(e.currentTarget);
$("btn-upload").onclick  = () => uploadApk();
$("btn-discover").onclick = (e) => discover(e.currentTarget);
$("btn-clear").onclick = () => $("log").textContent = "";
$("btn-copy").onclick  = async () => {
  const ok = await copyText($("log").textContent);
  toast(ok ? "Log copied." : "Couldn't copy — select the text and Ctrl+C.", ok ? "good" : "warn");
};

/* talk & sounds wire-up */
(function(){
  const t=$("btn-talk");
  const down=(e)=>{ e.preventDefault(); t.classList.add("rec"); startTalk(); };
  const up=(e)=>{ e.preventDefault(); t.classList.remove("rec"); stopTalk(); };
  t.addEventListener("mousedown",down); t.addEventListener("touchstart",down,{passive:false});
  t.addEventListener("mouseup",up);     t.addEventListener("mouseleave",up);
  t.addEventListener("touchend",up,{passive:false});
  $("btn-stop-sound").onclick=async()=>{ await fetch(`/api/talk/${talkCam()}/stop`,{method:"POST"}); toast("Stopped.",""); };
  const tc=$("talk-cam"); if(tc) tc.addEventListener("change", loadVolume);
  if($("btn-led-on")) $("btn-led-on").onclick=()=>setLed(true);
  if($("btn-led-off")) $("btn-led-off").onclick=()=>setLed(false);
  if($("btn-cam-info")) $("btn-cam-info").onclick=loadCamInfo;
  if($("btn-lullaby-load")) $("btn-lullaby-load").onclick=loadLullabies;
  if($("btn-lullaby-stop")) $("btn-lullaby-stop").onclick=async()=>{
    const r=await fetch(`/api/lullaby/${talkCam()}/stop`,{method:"POST"});
    toast(r.ok?"⏹ camera sound stopped":"stop failed", r.ok?"good":"bad");
  };
  $("btn-sound-browse").onclick=()=>$("sound-file").click();
  $("sound-file").onchange=(e)=>uploadSounds(e.target.files);
  const dz=$("sound-drop");
  ["dragover","dragenter"].forEach(ev=>dz.addEventListener(ev,e=>{e.preventDefault();dz.classList.add("over");}));
  ["dragleave","drop"].forEach(ev=>dz.addEventListener(ev,e=>{e.preventDefault();dz.classList.remove("over");}));
  dz.addEventListener("drop",e=>uploadSounds(e.dataTransfer.files));

  // speaker volume slider
  const vs=$("spk-vol"), vv=$("spk-vol-val");
  if(vs){
    vs.addEventListener("input", ()=>{ if(vv) vv.textContent=vs.value+"%"; });
    vs.addEventListener("change", async ()=>{
      const r=await fetch(`/api/volume/${talkCam()}`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({percent:parseInt(vs.value,10)})});
      toast(r.ok?`🔊 volume ${vs.value}%`:"couldn't set volume", r.ok?"good":"bad");
    });
  }
})();

async function loadLullabies(){
  const box=$("lullaby-list"); if(!box) return;
  box.innerHTML = `<span class="hint">Asking the camera…</span>`;
  let j; try{ j=await (await fetch(`/api/lullaby/${talkCam()}/tracks`)).json(); }
  catch(e){ box.innerHTML=`<span class="hint">couldn't reach camera</span>`; return; }
  if(!j.ok){ box.innerHTML=`<span class="hint">${esc(j.error||"no tracks")}</span>`; return; }
  const items=j.items||[];
  if(!items.length){ box.innerHTML=`<span class="hint">Camera reported no built-in tracks.</span>`; return; }
  box.innerHTML="";
  items.forEach((it,i)=>{
    const dur = it.duration_ms ? Math.round(it.duration_ms/1000)+"s" : "";
    const row=document.createElement("div"); row.className="snd-row";
    row.innerHTML=`<button class="mini snd-play">▶</button><span class="snd-name">Track ${i+1}
      <span class="hint">${esc((it.uuid||"").slice(0,8))} ${dur}</span></span>`;
    row.querySelector(".snd-play").onclick=async()=>{
      const loop=!!($("snd-loop")&&$("snd-loop").checked);
      const tmin=($("snd-timer")?parseFloat($("snd-timer").value):0)||0;
      const payload={uuid:it.uuid, repeat:loop};
      if(tmin>0) payload.timeout_ms=Math.round(tmin*60000);
      const r=await fetch(`/api/lullaby/${talkCam()}/play`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
      const jj=await r.json();
      toast(r.ok?`🎼 playing on camera${loop?" 🔁":""}${tmin?` ⏱${tmin}m`:""}`:(jj.error||"play failed"), r.ok?"good":"bad");
    };
    box.appendChild(row);
  });
}

async function setLed(on){
  const r=await fetch(`/api/led/${talkCam()}`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({on})});
  toast(r.ok?`💡 status light ${on?"on":"off"}`:"couldn't set light", r.ok?"good":"bad");
}
async function loadCamInfo(){
  const box=$("cam-info"); if(box) box.textContent="Asking the camera…";
  let j; try{ j=await (await fetch(`/api/info/${talkCam()}`)).json(); }catch(e){ if(box) box.textContent="couldn't reach camera"; return; }
  if(!j.ok){ if(box) box.textContent=esc(j.error||"no info"); return; }
  const parts=[]; if(j.model) parts.push("Model "+j.model); if(j.firmware) parts.push("FW "+j.firmware);
  if(j.vendor) parts.push(j.vendor); if(j.version) parts.push("v"+j.version);
  if(box) box.textContent=parts.join(" · ")||"(no fields)";
}

async function loadVolume(){
  const vs=$("spk-vol"), vv=$("spk-vol-val"); if(!vs) return;
  try{
    const j=await (await fetch(`/api/volume/${talkCam()}`)).json();
    if(j && j.percent!=null){ vs.value=j.percent; if(vv) vv.textContent=j.percent+"%"; }
  }catch(e){}
}

loadAccount();
startLogStream();
refreshStatus();
refreshCameras();
refreshFindings();
loadSounds();
loadVolume();
loadVitals();
loadMqtt();
setInterval(refreshStatus, 5000);
setInterval(loadVitals, 2500);   // near-live: temp rides every video frame, rest polls ~2s
setInterval(refreshCameras, 5000);
