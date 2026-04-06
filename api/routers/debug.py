"""Debug monitoring UI and event stream.

Endpoints:
  GET /debug        — interactive terrarium dashboard (HTML)
  GET /debug/stream — Server-Sent Events stream of all system activity
  GET /debug/snapshot — current state snapshot (JSON)
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_db
from core.debug.event_stream import debug_stream

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/debug", tags=["debug"])


# ── snapshot ──────────────────────────────────────────────────────────────────

@router.get("/snapshot")
async def snapshot(db: AsyncSession = Depends(get_db)) -> dict:
    """Return current state of windows, proposals, tasks, and scheduler."""
    from core.models.conversation import AgentTask, ConversationWindow
    from core.models.proposal import EditProposal
    from core.services.scheduler_service import scheduler

    windows = list((await db.scalars(
        select(ConversationWindow).order_by(ConversationWindow.created_at.desc()).limit(20)
    )).all())

    proposals = list((await db.scalars(
        select(EditProposal).order_by(EditProposal.created_at.desc()).limit(20)
    )).all())

    tasks = list((await db.scalars(
        select(AgentTask).order_by(AgentTask.created_at.desc()).limit(30)
    )).all())

    return {
        "windows": [
            {
                "id": w.id,
                "external_source_id": w.external_source_id,
                "status": w.status,
                "message_count": w.message_count,
                "total_char_count": w.total_char_count,
                "first_message_at": w.first_message_at.isoformat() if w.first_message_at else None,
                "last_message_at": w.last_message_at.isoformat() if w.last_message_at else None,
            }
            for w in windows
        ],
        "proposals": [
            {
                "id": p.id,
                "status": p.status,
                "proposed_title": p.proposed_title,
                "proposer_agent_id": p.proposer_agent_id,
                "batch_id": p.batch_id,
                "created_at": p.created_at.isoformat() if p.created_at else None,
            }
            for p in proposals
        ],
        "tasks": [
            {
                "id": t.id,
                "agent_type": t.agent_type,
                "instruction": t.instruction,
                "status": t.status,
                "batch_id": t.batch_id,
                "created_at": t.created_at.isoformat() if t.created_at else None,
            }
            for t in tasks
        ],
        "scheduler": {
            "flush_rate": scheduler.flush_rate(),
            "current_interval_seconds": scheduler.current_interval(),
        },
    }


# ── SSE stream ────────────────────────────────────────────────────────────────

@router.get("/stream")
async def event_stream(request: Request):
    """Server-Sent Events stream. Clients receive all debug events in real-time."""
    queue = debug_stream.subscribe()

    async def generate():
        # Send a heartbeat immediately so the browser knows the connection is up.
        yield "data: " + json.dumps({"type": "connected"}) + "\n\n"
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    line = await asyncio.wait_for(queue.get(), timeout=5.0)
                    yield f"data: {line}\n\n"
                except asyncio.TimeoutError:
                    # Send keepalive comment so proxies don't close the connection.
                    yield ": keepalive\n\n"
        finally:
            debug_stream.unsubscribe(queue)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── dashboard HTML ────────────────────────────────────────────────────────────

_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Wolfina Wiki — Debug Monitor</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#070c17;--panel:#0b1120;--border:#162035;--border2:#1e2d48;
  --text:#8090b4;--text2:#c0cce0;--dim:#3a4a60;
  --green:#3de07a;--amber:#f0a030;--blue:#38a8f0;--purple:#c060f0;
  --red:#f04040;--teal:#20d0b0;--pink:#f060a0;
  --idle:#2a3a50;--font:'Courier New',monospace;
}
html,body{height:100%;overflow:hidden;background:var(--bg);color:var(--text);font-family:var(--font);font-size:12px}
/* ── top bar ── */
#topbar{height:32px;background:#060a14;border-bottom:1px solid var(--border2);display:flex;align-items:center;padding:0 14px;gap:16px;flex-shrink:0}
#topbar .title{color:var(--blue);font-size:11px;letter-spacing:3px;text-transform:uppercase}
#topbar .sep{color:var(--dim)}
#conn{font-size:11px;margin-left:auto}
#conn.live{color:var(--green)}
#conn.connecting{color:var(--amber)}
#conn.dead{color:var(--red)}
/* ── layout ── */
#layout{display:flex;flex-direction:column;height:calc(100% - 32px)}
#panels{flex:1;display:grid;grid-template-columns:270px 1fr 270px;gap:1px;background:var(--border);overflow:hidden;min-height:0}
.panel{background:var(--panel);padding:10px;overflow-y:auto;display:flex;flex-direction:column;gap:8px}
.panel-hdr{color:var(--blue);font-size:9px;letter-spacing:2px;text-transform:uppercase;border-bottom:1px solid var(--border2);padding-bottom:4px;flex-shrink:0}
/* ── timeline ── */
#timeline{height:170px;flex-shrink:0;background:#050910;border-top:1px solid var(--border2);overflow-y:auto;padding:6px 12px}
#timeline .ev{font-size:11px;padding:2px 0;border-bottom:1px solid #0c1220;display:flex;gap:8px;line-height:1.4}
#timeline .ev .ts{color:var(--dim);flex-shrink:0;width:90px}
#timeline .ev .badge{padding:0 5px;border-radius:2px;font-size:10px;flex-shrink:0;min-width:130px;text-align:center}
#timeline .ev .detail{color:var(--text2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* ── window cards ── */
.win-card{border:1px solid var(--border2);padding:7px 9px;border-radius:3px;transition:border-color .3s}
.win-card.active{border-color:var(--green)}
.win-card.flushing{border-color:var(--amber);animation:glow-amber 1s infinite alternate}
.win-card.cleared{opacity:.45}
.win-title{color:var(--text2);font-size:11px;display:flex;justify-content:space-between;margin-bottom:4px}
.win-badge{font-size:9px;padding:1px 5px;border-radius:2px}
.badge-active{background:#0d3020;color:var(--green)}
.badge-flushing{background:#3a2000;color:var(--amber)}
.badge-cleared{background:#1a1a2a;color:var(--dim)}
.win-stats{color:var(--dim);font-size:10px;display:flex;gap:8px}
.win-stats span{color:var(--text)}
/* ── agent cards ── */
#agents-grid{display:flex;flex-direction:column;gap:6px}
.agent-card{border:1px solid var(--border);padding:8px 10px;border-radius:3px;transition:all .2s}
.agent-card.thinking{border-color:var(--blue);background:#08111e}
.agent-card.tool_call{border-color:var(--purple);background:#100a18}
.agent-card.done{border-color:var(--green);background:#06180e}
.agent-card.error{border-color:var(--red);background:#180608}
.agent-row{display:flex;align-items:center;gap:8px;margin-bottom:3px}
.agent-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0;background:var(--idle)}
.agent-dot.idle{background:var(--idle)}
.agent-dot.thinking{background:var(--blue);animation:pulse .8s infinite}
.agent-dot.tool_call{background:var(--purple);animation:pulse .4s infinite}
.agent-dot.done{background:var(--green)}
.agent-dot.error{background:var(--red)}
.agent-name{color:var(--text2);font-size:11px;text-transform:uppercase;letter-spacing:1px;flex:1}
.agent-iter{color:var(--dim);font-size:10px}
.agent-action{font-size:10px;color:var(--purple);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding-left:16px}
.agent-action.thinking{color:var(--blue)}
.agent-action.tool_call{color:var(--purple)}
.agent-action.done{color:var(--green)}
.agent-action.idle{color:var(--idle)}
.agent-args{font-size:9px;color:var(--dim);padding-left:16px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-top:2px}
/* ── scheduler block ── */
.sched-row{display:flex;justify-content:space-between;font-size:11px;padding:2px 0}
.sched-row .lbl{color:var(--dim)}
.sched-row .val{color:var(--text2)}
/* ── proposal / task list ── */
.list-item{border-left:2px solid var(--border2);padding:4px 7px;margin-bottom:4px;font-size:10px}
.li-title{color:var(--text2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.li-meta{color:var(--dim);margin-top:1px}
.status-pending{border-color:var(--amber)}
.status-approved{border-color:var(--green)}
.status-rejected{border-color:var(--red)}
.status-applied{border-color:var(--teal)}
.status-done{border-color:var(--green)}
.status-failed{border-color:var(--red)}
/* ── event type colours ── */
.ev-window_created .badge{background:#0a2010;color:var(--green)}
.ev-message_added .badge{background:#0a1a10;color:#60c070}
.ev-flush_started .badge{background:#201500;color:var(--amber)}
.ev-flush_completed .badge{background:#0a2010;color:var(--teal)}
.ev-agent_start .badge{background:#08101e;color:var(--blue)}
.ev-agent_thinking .badge{background:#06101c;color:#5090d0}
.ev-agent_tool_call .badge{background:#120818;color:var(--purple)}
.ev-agent_tool_result .badge{background:#0e0614;color:#a070d0}
.ev-agent_done .badge{background:#061208;color:var(--green)}
.ev-agent_error .badge{background:#180606;color:var(--red)}
.ev-proposal_created .badge{background:#141020;color:var(--pink)}
.ev-proposal_reviewed .badge{background:#101a10;color:#a0d060}
.ev-proposal_applied .badge{background:#081414;color:var(--teal)}
.ev-task_created .badge{background:#101808;color:#80c040}
.ev-task_updated .badge{background:#0c1408;color:#60a030}
.ev-scheduler_tick .badge{background:#0a0a1c;color:#6060c0}
.ev-connected .badge{background:#061006;color:var(--green)}
.ev-subagent_start .badge{background:#08101e;color:var(--blue)}
.ev-subagent_done .badge{background:#061208;color:var(--green)}
.ev-subagent_error .badge{background:#180606;color:var(--red)}
.ev-ingest_pipeline_start .badge{background:#101808;color:#80c040}
.ev-ingest_pipeline_complete .badge{background:#061408;color:var(--teal)}
.ev-file_ingest_complete .badge{background:#061208;color:#60c070}
.ev-file_read .badge{background:#0a0a1c;color:#6060c0}
.ev-file_search .badge{background:#0a0a1c;color:#7070d0}
.ev-file_list .badge{background:#0a0a1c;color:#5050b0}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
@keyframes glow-amber{from{box-shadow:0 0 4px #f0a03060}to{box-shadow:0 0 10px #f0a03099}}
#empty-msg{color:var(--dim);font-size:11px;padding:8px 0}
</style>
</head>
<body>
<div id="topbar">
  <span class="title">Wolfina Wiki</span>
  <span class="sep">|</span>
  <span style="color:var(--text);font-size:11px">Debug Monitor</span>
  <span id="conn" class="connecting">○ CONNECTING</span>
</div>
<div id="layout">
  <div id="panels">
    <!-- Windows -->
    <div class="panel" id="panel-windows">
      <div class="panel-hdr">Conversation Windows</div>
      <div id="windows-list"><div id="empty-msg">No windows yet.</div></div>
    </div>
    <!-- Agents -->
    <div class="panel" id="panel-agents">
      <div class="panel-hdr">Agent Arena</div>
      <div id="agents-grid"></div>
    </div>
    <!-- Scheduler + Wiki -->
    <div class="panel" id="panel-right">
      <div class="panel-hdr">Scheduler</div>
      <div id="sched-info"></div>
      <div class="panel-hdr" style="margin-top:8px">Recent Proposals</div>
      <div id="proposals-list"><div class="empty-msg" style="color:var(--dim);font-size:10px">None yet.</div></div>
      <div class="panel-hdr" style="margin-top:8px">Agent Tasks</div>
      <div id="tasks-list"><div class="empty-msg" style="color:var(--dim);font-size:10px">None yet.</div></div>
    </div>
  </div>
  <div id="timeline"></div>
</div>

<script>
const AGENT_TYPES = ['orchestrator','research','proposer','reviewer','executor','relation','ingest','subagent'];
const MAX_TIMELINE = 200;
const MAX_PROPOSALS = 12;
const MAX_TASKS = 15;

const state = {
  windows: {},      // id → {id, status, message_count, total_char_count, external_source_id, ...}
  agents: {},       // type → {status, action, args, iter, batch_id}
  scheduler: { flush_rate: 0, current_interval_seconds: 0 },
  proposals: [],    // [{id, status, proposed_title, proposer_agent_id, batch_id}]
  tasks: [],        // [{id, agent_type, instruction, status, batch_id}]
};

AGENT_TYPES.forEach(t => state.agents[t] = { status: 'idle', action: '', args: '', iter: 0 });

// ── helpers ──────────────────────────────────────────────────────────────────

function shortId(id) { return id ? id.slice(0,8) : '?'; }
function shortBatch(id) { return id ? id.slice(0,8) : ''; }

function esc(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ── render functions ─────────────────────────────────────────────────────────

function renderWindows() {
  const el = document.getElementById('windows-list');
  const wins = Object.values(state.windows).sort((a,b)=>b._ts-a._ts);
  if (!wins.length) { el.innerHTML = '<div id="empty-msg">No windows yet.</div>'; return; }
  el.innerHTML = wins.map(w => {
    const cls = w.status || 'cleared';
    const badgeCls = 'badge-' + cls;
    const src = w.external_source_id ? esc(w.external_source_id.slice(0,20)) : '(unnamed)';
    const last = w.last_message_at ? new Date(w.last_message_at).toLocaleTimeString() : '—';
    return `<div class="win-card ${esc(cls)}">
      <div class="win-title">
        <span>${esc(shortId(w.id))}</span>
        <span class="win-badge ${badgeCls}">${esc(cls)}</span>
      </div>
      <div class="win-stats">
        msgs: <span>${w.message_count||0}</span>
        chars: <span>${(w.total_char_count||0).toLocaleString()}</span>
      </div>
      <div class="win-stats" style="margin-top:2px">
        src: <span style="color:var(--text)">${src}</span>
      </div>
      <div class="win-stats" style="margin-top:2px">last: <span>${last}</span></div>
    </div>`;
  }).join('');
}

function renderAgents() {
  const el = document.getElementById('agents-grid');
  el.innerHTML = AGENT_TYPES.map(t => {
    const a = state.agents[t];
    const st = a.status || 'idle';
    const hasAction = st !== 'idle';
    return `<div class="agent-card ${esc(st)}">
      <div class="agent-row">
        <div class="agent-dot ${esc(st)}"></div>
        <div class="agent-name">${t}</div>
        ${a.batch_id ? `<div class="agent-iter">#${esc(shortBatch(a.batch_id))}</div>` : ''}
        ${a.iter > 0 ? `<div class="agent-iter">i${a.iter}</div>` : ''}
      </div>
      ${hasAction ? `<div class="agent-action ${esc(st)}">${esc(a.action)}</div>` : ''}
      ${a.args ? `<div class="agent-args">${esc(a.args)}</div>` : ''}
    </div>`;
  }).join('');
}

function renderScheduler() {
  const s = state.scheduler;
  const el = document.getElementById('sched-info');
  const mins = Math.floor((s.current_interval_seconds||0) / 60);
  const secs = (s.current_interval_seconds||0) % 60;
  const intervalStr = mins > 0 ? `${mins}m ${secs}s` : `${secs}s`;
  el.innerHTML = `
    <div class="sched-row"><span class="lbl">flush rate</span><span class="val">${s.flush_rate||0} / window</span></div>
    <div class="sched-row"><span class="lbl">next interval</span><span class="val">${intervalStr}</span></div>
  `;
}

function renderProposals() {
  const el = document.getElementById('proposals-list');
  if (!state.proposals.length) {
    el.innerHTML = '<div style="color:var(--dim);font-size:10px">None yet.</div>';
    return;
  }
  el.innerHTML = state.proposals.map(p => {
    const st = (p.status||'').replace(' ','_');
    return `<div class="list-item status-${esc(st)}">
      <div class="li-title">${esc(p.proposed_title || p.title || '(untitled)')}</div>
      <div class="li-meta">${esc(st)} · ${esc(shortId(p.id))} · by ${esc(shortId(p.proposer_agent_id||p.proposer))}</div>
    </div>`;
  }).join('');
}

function renderTasks() {
  const el = document.getElementById('tasks-list');
  if (!state.tasks.length) {
    el.innerHTML = '<div style="color:var(--dim);font-size:10px">None yet.</div>';
    return;
  }
  el.innerHTML = state.tasks.map(t => {
    const st = t.status||'pending';
    return `<div class="list-item status-${esc(st)}">
      <div class="li-title">${esc(t.agent_type)} — ${esc((t.instruction||'').slice(0,50))}</div>
      <div class="li-meta">${esc(st)} · #${esc(shortBatch(t.batch_id))}</div>
    </div>`;
  }).join('');
}

function renderAll() {
  renderWindows(); renderAgents(); renderScheduler(); renderProposals(); renderTasks();
}

// ── timeline ──────────────────────────────────────────────────────────────────

const tlEl = document.getElementById('timeline');
let tlCount = 0;

function addTimelineEntry(ev) {
  if (ev.type === 'connected') return;
  tlCount++;
  if (tlCount > MAX_TIMELINE) {
    const old = tlEl.lastChild;
    if (old) tlEl.removeChild(old);
  }
  const ts = ev.ts ? new Date(ev.ts).toLocaleTimeString() : '';
  const detail = buildDetail(ev);
  const div = document.createElement('div');
  div.className = `ev ev-${ev.type}`;
  div.innerHTML = `<span class="ts">${esc(ts)}</span><span class="badge">${esc(ev.type)}</span><span class="detail">${esc(detail)}</span>`;
  tlEl.insertBefore(div, tlEl.firstChild);
}

function buildDetail(ev) {
  switch(ev.type) {
    case 'window_created': return `window=${shortId(ev.window_id)} src=${ev.external_source_id||''}`;
    case 'message_added': return `window=${shortId(ev.window_id)} role=${ev.role} msgs=${ev.message_count} chars=${ev.total_char_count}${ev.flush_triggered?' [FLUSH]':''}`;
    case 'flush_started': return `window=${shortId(ev.window_id)} batch=${shortBatch(ev.batch_id)} msgs=${ev.message_count}`;
    case 'flush_completed': return `window=${shortId(ev.window_id)} batch=${shortBatch(ev.batch_id)}`;
    case 'agent_start': return `${ev.agent_type} batch=${shortBatch(ev.batch_id)}`;
    case 'agent_thinking': return `${ev.agent_type} iter=${ev.iteration} model=${ev.model||''}`;
    case 'agent_tool_call': return `${ev.agent_type} → ${ev.tool}  ${(ev.args_preview||'').slice(0,80)}`;
    case 'agent_tool_result': return `${ev.agent_type} ← ${ev.tool}  ${ev.ok?'ok':'ERROR'} ${(ev.result_preview||'').slice(0,60)}`;
    case 'agent_done': return `${ev.agent_type} batch=${shortBatch(ev.batch_id)} "${(ev.result_preview||'').slice(0,60)}"`;
    case 'agent_error': return `${ev.agent_type} iter=${ev.iteration}`;
    case 'subagent_start': return `task=${ev.task_id}`;
    case 'subagent_done': return `task=${ev.task_id} "${(ev.result_preview||'').slice(0,80)}"`;
    case 'subagent_error': return `task=${ev.task_id} ${ev.error||''}`;
    case 'ingest_pipeline_start': return `batch=${shortBatch(ev.batch_id)} force=${JSON.stringify(ev.force_paths||null)}`;
    case 'ingest_pipeline_complete': return `batch=${shortBatch(ev.batch_id)} proposed=${ev.proposed??''}`;
    case 'file_ingest_complete': return `record=${ev.record_id} outcome=${ev.outcome||''}`;
    case 'file_read': return `${ev.path} lines=${ev.lines} offset=${ev.offset}`;
    case 'file_search': return `${ev.path} pattern=${ev.pattern} matches=${ev.matches}`;
    case 'file_list': return `pattern=${ev.pattern} count=${ev.count}`;
    case 'proposal_created': return `[${shortId(ev.proposal_id)}] "${ev.title||''}" by ${shortId(ev.proposer||ev.proposer_agent_id)} batch=${shortBatch(ev.batch_id)}`;
    case 'proposal_reviewed': return `[${shortId(ev.proposal_id)}] ${ev.decision} by ${shortId(ev.reviewer)} → ${ev.status}`;
    case 'proposal_applied': return `[${shortId(ev.proposal_id)}] → page=${shortId(ev.page_id)} by ${shortId(ev.executor)}`;
    case 'task_created': return `[${shortId(ev.task_id)}] ${ev.agent_type}: ${(ev.instruction||'').slice(0,60)}`;
    case 'task_updated': return `[${shortId(ev.task_id)}] ${ev.agent_type} → ${ev.status}`;
    case 'scheduler_tick': return `rate=${ev.flush_rate} interval=${ev.current_interval}s`;
    default: return JSON.stringify(ev).slice(0,120);
  }
}

// ── event handling ─────────────────────────────────────────────────────────────

function handleEvent(ev) {
  const t = ev.type;

  if (t === 'window_created') {
    state.windows[ev.window_id] = { id: ev.window_id, status: ev.status, message_count: 0, total_char_count: 0, external_source_id: ev.external_source_id, last_message_at: null, _ts: Date.now() };
    renderWindows(); return;
  }
  if (t === 'message_added') {
    if (!state.windows[ev.window_id]) state.windows[ev.window_id] = { id: ev.window_id, status: 'active', message_count: 0, total_char_count: 0, external_source_id: '', _ts: Date.now() };
    const w = state.windows[ev.window_id];
    w.message_count = ev.message_count;
    w.total_char_count = ev.total_char_count;
    w.last_message_at = ev.ts;
    w._ts = Date.now();
    renderWindows(); return;
  }
  if (t === 'flush_started') {
    if (state.windows[ev.window_id]) state.windows[ev.window_id].status = 'flushing';
    renderWindows(); return;
  }
  if (t === 'flush_completed') {
    if (state.windows[ev.window_id]) state.windows[ev.window_id].status = 'cleared';
    renderWindows(); return;
  }
  if (t === 'agent_start') {
    state.agents[ev.agent_type] = { status: 'thinking', action: 'starting…', args: '', iter: 0, batch_id: ev.batch_id };
    renderAgents(); return;
  }
  if (t === 'agent_thinking') {
    if (state.agents[ev.agent_type]) {
      state.agents[ev.agent_type].status = 'thinking';
      state.agents[ev.agent_type].action = `thinking (iter ${ev.iteration})`;
      state.agents[ev.agent_type].iter = ev.iteration;
      state.agents[ev.agent_type].args = '';
    }
    renderAgents(); return;
  }
  if (t === 'agent_tool_call') {
    if (state.agents[ev.agent_type]) {
      state.agents[ev.agent_type].status = 'tool_call';
      state.agents[ev.agent_type].action = `→ ${ev.tool}`;
      state.agents[ev.agent_type].args = ev.args_preview || '';
    }
    renderAgents(); return;
  }
  if (t === 'agent_tool_result') {
    if (state.agents[ev.agent_type]) {
      state.agents[ev.agent_type].status = ev.ok ? 'thinking' : 'error';
      state.agents[ev.agent_type].action = `← ${ev.tool} ${ev.ok?'ok':'ERROR'}`;
      state.agents[ev.agent_type].args = (ev.result_preview||'').slice(0,100);
    }
    renderAgents(); return;
  }
  if (t === 'agent_done') {
    if (state.agents[ev.agent_type]) {
      state.agents[ev.agent_type].status = 'done';
      state.agents[ev.agent_type].action = `done: ${(ev.result_preview||'').slice(0,60)}`;
      state.agents[ev.agent_type].args = '';
    }
    renderAgents();
    setTimeout(() => {
      if (state.agents[ev.agent_type] && state.agents[ev.agent_type].status === 'done') {
        state.agents[ev.agent_type] = { status: 'idle', action: '', args: '', iter: 0, batch_id: '' };
        renderAgents();
      }
    }, 4000);
    return;
  }
  if (t === 'agent_error') {
    if (state.agents[ev.agent_type]) {
      state.agents[ev.agent_type].status = 'error';
      state.agents[ev.agent_type].action = 'error';
    }
    renderAgents(); return;
  }
  if (t === 'subagent_start') {
    state.agents['subagent'] = { status: 'thinking', action: `task: ${ev.task_id}`, args: '', iter: 0, batch_id: '' };
    renderAgents(); return;
  }
  if (t === 'subagent_done') {
    state.agents['subagent'] = { status: 'done', action: `done: ${(ev.result_preview||'').slice(0,60)}`, args: '', iter: 0, batch_id: '' };
    renderAgents();
    setTimeout(() => {
      if (state.agents['subagent'] && state.agents['subagent'].status === 'done') {
        state.agents['subagent'] = { status: 'idle', action: '', args: '', iter: 0, batch_id: '' };
        renderAgents();
      }
    }, 4000);
    return;
  }
  if (t === 'subagent_error') {
    state.agents['subagent'] = { status: 'error', action: `error: ${(ev.error||'').slice(0,60)}`, args: '', iter: 0, batch_id: '' };
    renderAgents(); return;
  }
  if (t === 'proposal_created') {
    state.proposals.unshift({ id: ev.proposal_id, status: 'pending', proposed_title: ev.title, proposer_agent_id: ev.proposer, batch_id: ev.batch_id });
    if (state.proposals.length > MAX_PROPOSALS) state.proposals.pop();
    renderProposals(); return;
  }
  if (t === 'proposal_reviewed') {
    const p = state.proposals.find(x => x.id === ev.proposal_id);
    if (p) p.status = ev.status;
    renderProposals(); return;
  }
  if (t === 'proposal_applied') {
    const p = state.proposals.find(x => x.id === ev.proposal_id);
    if (p) p.status = 'applied';
    renderProposals(); return;
  }
  if (t === 'task_created') {
    state.tasks.unshift({ id: ev.task_id, agent_type: ev.agent_type, instruction: ev.instruction, status: 'pending', batch_id: ev.batch_id });
    if (state.tasks.length > MAX_TASKS) state.tasks.pop();
    renderTasks(); return;
  }
  if (t === 'task_updated') {
    const tk = state.tasks.find(x => x.id === ev.task_id);
    if (tk) tk.status = ev.status;
    renderTasks(); return;
  }
  if (t === 'scheduler_tick') {
    state.scheduler.flush_rate = ev.flush_rate;
    state.scheduler.current_interval_seconds = ev.current_interval;
    renderScheduler(); return;
  }
}

// ── connection ────────────────────────────────────────────────────────────────

function setConn(text, cls) {
  const el = document.getElementById('conn');
  el.textContent = text;
  el.className = cls;
}

async function loadSnapshot() {
  try {
    const r = await fetch('/debug/snapshot');
    const d = await r.json();
    d.windows.forEach(w => { w._ts = new Date(w.last_message_at||w.first_message_at||0).getTime(); state.windows[w.id] = w; });
    state.proposals = d.proposals.map(p=>({...p}));
    state.tasks = d.tasks.map(t=>({...t}));
    if (d.scheduler) Object.assign(state.scheduler, d.scheduler);
    renderAll();
  } catch(e) { console.warn('snapshot load failed', e); }
}

function connectSSE() {
  setConn('○ CONNECTING', 'connecting');
  const es = new EventSource('/debug/stream');
  es.onopen = () => setConn('● LIVE', 'live');
  es.onerror = () => {
    setConn('○ DISCONNECTED', 'dead');
    es.close();
    setTimeout(connectSSE, 3000);
  };
  es.onmessage = (e) => {
    try {
      const ev = JSON.parse(e.data);
      handleEvent(ev);
      addTimelineEntry(ev);
    } catch(err) { console.warn('parse error', err); }
  };
}

renderAll();
loadSnapshot().then(() => connectSSE());
</script>
</body>
</html>"""


@router.get("", response_class=HTMLResponse)
async def debug_dashboard() -> str:
    return _HTML
