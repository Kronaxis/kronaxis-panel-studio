/**
 * Panel Studio Conversation Client
 *
 * Handles multi-turn conversation with synthetic persona panels.
 * Polls for run completion, renders aggregate and individual responses.
 */

function esc(str) {
    if (!str) return '';
    const d = document.createElement('div');
    d.textContent = str;
    return d.innerHTML;
}

let conversationId = null;
let panelId = null;
let pollInterval = null;
let currentTurns = [];

async function initConversation(pId, cId) {
    panelId = pId;

    if (cId) {
        conversationId = cId;
        await loadConversation();
    } else {
        // Create new conversation.
        const resp = await fetch(`/api/panels/${panelId}/conversations`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({title: 'New conversation'})
        });
        if (resp.ok) {
            const conv = await resp.json();
            conversationId = conv.id;
            history.replaceState(null, '', `/panels/${panelId}/conversations/${conv.id}`);
            document.getElementById('conv-title').textContent = conv.title;
        }
    }

    // Enable enter-to-submit.
    document.getElementById('stimulus-input').addEventListener('keydown', function(e) {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            askPanel();
        }
    });
}

async function loadConversation() {
    const resp = await fetch(`/api/panels/${panelId}/conversations/${conversationId}`);
    if (!resp.ok) return;
    const conv = await resp.json();

    document.getElementById('conv-title').textContent = conv.title;
    document.getElementById('turn-counter').textContent = `${conv.turn_count} turns, ${conv.total_responses} responses`;

    if (conv.turn_count > 0) {
        const exportMenu = document.getElementById('export-menu');
        if (exportMenu) exportMenu.style.display = '';
        const base = `/api/panels/${panelId}/conversations/${conversationId}/export`;
        const el = (id) => document.getElementById(id);
        if (el('export-jsonl')) el('export-jsonl').href = `${base}?format=jsonl`;
        if (el('export-jsonl-full')) el('export-jsonl-full').href = `${base}?format=jsonl_full`;
        if (el('export-focus-group')) el('export-focus-group').href = `${base}?format=focus_group`;
        if (el('export-csv')) el('export-csv').href = `${base}?format=csv`;
    }

    currentTurns = conv.turns || [];
    renderThread();
}

function renderThread() {
    const thread = document.getElementById('thread');
    thread.innerHTML = '';

    // Compute running cost total across all turns.
    let totalCost = 0;
    for (const t of currentTurns) {
        const agg = t.aggregated || {};
        totalCost += agg.estimated_cost || 0;
    }
    const costEl = document.getElementById('conv-cost');
    if (costEl) {
        costEl.textContent = totalCost > 0 ? `Cost: $${totalCost.toFixed(4)}` : '';
    }

    for (const turn of currentTurns) {
        // Stimulus bubble.
        const stimDiv = document.createElement('div');
        stimDiv.className = 'stimulus-bubble';
        stimDiv.textContent = turn.stimulus;
        thread.appendChild(stimDiv);

        // Response card.
        if (turn.completed_at || turn.aggregated) {
            const card = createResponseCard(turn);
            thread.appendChild(card);
        } else if (turn.run_id) {
            const loading = document.createElement('div');
            loading.className = 'response-card';
            loading.id = `turn-progress-${turn.turn_number}`;
            loading.innerHTML = `
                <div style="display:flex; align-items:center; gap:0.5rem; margin-bottom:0.5rem;">
                    <span class="pulse-dot"></span>
                    <span style="color:var(--forge); font-weight:600; font-size:0.85rem;">Consulting the panel...</span>
                </div>
                <div class="progress"><div class="progress-bar" id="pbar-${turn.turn_number}" style="width:0%"></div></div>
                <div style="display:flex; justify-content:space-between; align-items:center;">
                    <span id="pcount-${turn.turn_number}" style="font-size:0.8rem; color:var(--text-muted);">Waiting for responses...</span>
                    <span id="pelapsed-${turn.turn_number}" style="font-size:0.75rem; color:var(--text-muted);">0s</span>
                </div>
                <div id="platest-${turn.turn_number}" style="font-size:0.75rem; color:var(--text-muted); margin-top:0.35rem; font-style:italic;"></div>
            `;
            thread.appendChild(loading);
            startPolling(turn.turn_number);
        }
    }

    thread.scrollTop = thread.scrollHeight;
}

function createResponseCard(turn) {
    const card = document.createElement('div');
    card.className = 'response-card';
    card.style.cursor = 'pointer';
    card.onclick = () => loadIndividualResponses(turn);

    const agg = turn.aggregated || {};
    const sentiment = agg.sentiment_breakdown || agg.sentiment || {};
    const themes = agg.themes || agg.key_themes || [];
    const total = (sentiment.positive || 0) + (sentiment.negative || 0) +
                  (sentiment.neutral || 0) + (sentiment.mixed || 0) || 1;

    const turnCost = agg.estimated_cost || 0;
    const costLabel = turnCost > 0 ? `$${turnCost.toFixed(4)}` : '';

    card.innerHTML = `
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:0.5rem;">
            <span style="font-weight:600; font-size:0.85rem;">Turn ${turn.turn_number}</span>
            <span style="font-size:0.8rem; color:var(--text-muted);">
                ${turn.response_count} responses${costLabel ? ` &middot; ${costLabel}` : ''}
            </span>
        </div>
        <div class="sentiment-bar">
            <div class="sentiment-positive" style="width:${((sentiment.positive||0)/total*100).toFixed(1)}%"></div>
            <div class="sentiment-negative" style="width:${((sentiment.negative||0)/total*100).toFixed(1)}%"></div>
            <div class="sentiment-neutral" style="width:${((sentiment.neutral||0)/total*100).toFixed(1)}%"></div>
            <div class="sentiment-mixed" style="width:${((sentiment.mixed||0)/total*100).toFixed(1)}%"></div>
        </div>
        <div style="display:flex; gap:0.5rem; margin:0.35rem 0; flex-wrap:wrap;">
            ${sentiment.positive ? `<span class="badge badge-positive">${((sentiment.positive/total)*100).toFixed(0)}% positive</span>` : ''}
            ${sentiment.negative ? `<span class="badge badge-negative">${((sentiment.negative/total)*100).toFixed(0)}% negative</span>` : ''}
            ${sentiment.neutral ? `<span class="badge badge-neutral">${((sentiment.neutral/total)*100).toFixed(0)}% neutral</span>` : ''}
        </div>
        ${themes.length ? `<div style="font-size:0.8rem; color:var(--text-muted); margin-top:0.5rem;">Themes: ${themes.slice(0,5).join(', ')}</div>` : ''}
        <div style="display:flex; gap:0.5rem; margin-top:0.5rem; align-items:center; flex-wrap:wrap;">
            <span style="font-size:0.75rem; color:var(--text-muted);">Click to view individual responses</span>
            <div style="margin-left:auto; display:flex; gap:0.35rem;">
                ${sentiment.positive ? `<button class="btn btn-ghost" style="font-size:0.65rem; padding:0.15rem 0.4rem; color:var(--data);" onclick="event.stopPropagation(); createSubPanelFromSentiment(${turn.turn_number}, 'positive')" title="Create sub-panel from positive respondents">+ Positive</button>` : ''}
                ${sentiment.negative ? `<button class="btn btn-ghost" style="font-size:0.65rem; padding:0.15rem 0.4rem; color:var(--danger);" onclick="event.stopPropagation(); createSubPanelFromSentiment(${turn.turn_number}, 'negative')" title="Create sub-panel from negative respondents">+ Negative</button>` : ''}
                <button class="btn btn-ghost" style="font-size:0.75rem; padding:0.2rem 0.5rem;"
                        onclick="event.stopPropagation(); generateFocusGroup(${turn.turn_number})">Focus Group</button>
            </div>
        </div>
    `;

    return card;
}

async function askPanel() {
    const input = document.getElementById('stimulus-input');
    const stimulus = input.value.trim();
    if (!stimulus) return;

    const btn = document.getElementById('ask-btn');
    btn.disabled = true;
    input.value = '';

    const sampleInput = document.getElementById('sample-size');
    const sampleSize = sampleInput ? parseInt(sampleInput.value) || 0 : 0;

    const resp = await fetch(`/api/panels/${panelId}/conversations/${conversationId}/ask`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({stimulus, sample_size: sampleSize})
    });

    btn.disabled = false;

    if (resp.ok) {
        const result = await resp.json();
        currentTurns.push({
            turn_number: result.turn_number,
            stimulus: stimulus,
            run_id: result.run_id,
            response_count: 0,
            aggregated: null,
            completed_at: null,
        });
        renderThread();
    } else {
        const err = await resp.json();
        alert(err.error || 'Failed to submit stimulus');
    }
}

function startPolling(turnNumber) {
    if (pollInterval) clearInterval(pollInterval);
    const pollStart = Date.now();

    // Elapsed time ticker (updates every second).
    const elapsedInterval = setInterval(() => {
        const el = document.getElementById(`pelapsed-${turnNumber}`);
        if (!el) { clearInterval(elapsedInterval); return; }
        const secs = Math.floor((Date.now() - pollStart) / 1000);
        if (secs < 60) {
            el.textContent = `${secs}s`;
        } else {
            el.textContent = `${Math.floor(secs/60)}m ${secs%60}s`;
        }
    }, 1000);

    // Try SSE first, fall back to polling.
    let useSSE = typeof EventSource !== 'undefined';
    if (useSSE) {
        try {
            const es = new EventSource(`/api/panels/${panelId}/conversations/${conversationId}/stream`);
            es.onmessage = async (event) => {
                const data = JSON.parse(event.data);
                if (data.status === 'complete') {
                    es.close();
                    clearInterval(elapsedInterval);
                    await loadConversation();
                    return;
                }
                const done = data.completed || 0;
                const total = data.total || 1;
                const pbar = document.getElementById(`pbar-${turnNumber}`);
                const pcount = document.getElementById(`pcount-${turnNumber}`);
                const platest = document.getElementById(`platest-${turnNumber}`);
                if (pbar) pbar.style.width = `${(done/total*100).toFixed(1)}%`;
                if (pcount) pcount.textContent = `${done} / ${total} responses`;
                if (platest && data.latest_name) platest.textContent = `Latest: ${data.latest_name}`;
            };
            es.onerror = () => {
                es.close();
                // Fall back to polling.
                _startPollingFallback(turnNumber, pollStart, elapsedInterval);
            };
            return;
        } catch(e) {
            useSSE = false;
        }
    }

    _startPollingFallback(turnNumber, pollStart, elapsedInterval);
}

function _startPollingFallback(turnNumber, pollStart, elapsedInterval) {
    pollInterval = setInterval(async () => {
        const resp = await fetch(`/api/panels/${panelId}/conversations/${conversationId}/status`);
        if (!resp.ok) return;
        const data = await resp.json();

        if (data.status === 'complete') {
            clearInterval(pollInterval);
            clearInterval(elapsedInterval);
            pollInterval = null;
            await loadConversation();
        } else if (data.detail) {
            const progress = data.detail.progress || {};
            const responses = data.detail.responses || [];
            const done = progress.completed || responses.length || 0;
            const total = progress.total || 1;
            const pbar = document.getElementById(`pbar-${turnNumber}`);
            const pcount = document.getElementById(`pcount-${turnNumber}`);
            const platest = document.getElementById(`platest-${turnNumber}`);
            if (pbar) pbar.style.width = `${(done/total*100).toFixed(1)}%`;
            if (pcount) pcount.textContent = `${done} / ${total} responses`;
            if (platest && responses.length > 0) {
                const last = responses[responses.length - 1];
                const name = last.persona_name || last.persona_id || '';
                if (name) platest.textContent = `Latest: ${name}`;
            }
        }
    }, 3000);
}

async function loadIndividualResponses(turn) {
    const viewer = document.getElementById('individual-responses');
    viewer.innerHTML = '<div class="loading">Loading responses...</div>';

    // Fetch full conversation to get run data.
    const resp = await fetch(`/api/panels/${panelId}/conversations/${conversationId}`);
    if (!resp.ok) { viewer.innerHTML = '<p>Failed to load.</p>'; return; }
    const conv = await resp.json();

    const t = (conv.turns || []).find(t => t.turn_number === turn.turn_number);
    if (!t || !t.aggregated) {
        viewer.innerHTML = '<p style="color:var(--text-muted);">No response data available.</p>';
        return;
    }

    const agg = t.aggregated || {};
    const responses = agg.responses || agg.raw_responses || [];

    if (!responses.length) {
        viewer.innerHTML = '<p style="color:var(--text-muted);">Aggregate data only. Individual responses not yet available.</p>';
        return;
    }

    // Batch-resolve persona names from IDs.
    const idsNeedingNames = responses
        .filter(r => !r.persona_name && r.persona_id)
        .map(r => r.persona_id);
    let nameMap = {};
    if (idsNeedingNames.length) {
        try {
            const nameResp = await fetch(`/api/personas/names`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ids: idsNeedingNames})
            });
            if (nameResp.ok) nameMap = await nameResp.json();
        } catch(e) { /* fall back to IDs */ }
    }

    viewer.innerHTML = responses.map(r => {
        const text = esc(r.reaction || r.response || '');
        const resolvedName = r.persona_name || nameMap[r.persona_id] || null;
        const displayName = esc(resolvedName || r.persona_id || 'Unknown');
        const pid = r.persona_id || '';
        const sentiment = esc(r.sentiment || '');
        const intent = esc(r.intent || '');
        const confidence = r.confidence != null ? (r.confidence * 100).toFixed(0) + '%' : '';
        const influence = r.influence_score != null ? r.influence_score.toFixed(2) : '';
        const safeName2 = (resolvedName || '').replace(/'/g, "\\'");
        const nameClickable = resolvedName
            ? `<span style="cursor:pointer; text-decoration:underline; text-decoration-color:var(--border);" onclick="showPersonaModal('${safeName2}')">${displayName}</span>`
            : displayName;
        const directBtn = pid
            ? `<button class="btn btn-ghost" style="font-size:0.65rem; padding:0.1rem 0.35rem; margin-left:0.5rem;" onclick="startDirectConversation('${pid}', '${safeName2}')" title="Start a 1-to-1 conversation with this persona">Talk</button>`
            : '';
        return `
        <div class="persona-response">
            <div style="display:flex; align-items:center;">
                <div class="persona-name">${nameClickable}</div>
                ${directBtn}
            </div>
            <div class="persona-meta">
                ${pid ? '<span style="font-family:monospace; opacity:0.5;">' + esc(pid.slice(0,8)) + '</span> ' : ''}
                ${r.age ? r.age + ' yrs' : ''} ${r.occupation ? '&middot; ' + esc(r.occupation) : ''} ${r.location ? '&middot; ' + esc(r.location) : ''}
                ${confidence ? '&middot; confidence: ' + confidence : ''}
                ${influence ? '&middot; influence: ' + influence : ''}
            </div>
            <div class="persona-text">${text}</div>
            ${sentiment ? `<span class="badge badge-${sentiment}" style="margin-top:0.35rem;">${sentiment}</span>` : ''}
            ${intent ? `
                <details style="margin-top:0.5rem;">
                    <summary style="cursor:pointer; font-size:0.8rem; color:var(--text-muted);">Intent</summary>
                    <div style="padding:0.5rem; font-size:0.85rem; color:var(--text-secondary);">${intent}</div>
                </details>
            ` : ''}
            ${r.reasoning_context ? `
                <details style="margin-top:0.5rem;">
                    <summary style="cursor:pointer; font-size:0.8rem; color:var(--soul);">Life Context</summary>
                    <div style="padding:0.5rem; font-size:0.85rem; background:var(--surface); border-radius:6px; margin-top:0.25rem;">
                        <div><strong>Days since last interaction:</strong> ${r.reasoning_context.days_since_last_interaction || '?'}</div>
                        ${r.reasoning_context.life_events && r.reasoning_context.life_events.length ? '<div><strong>Life events:</strong><ul style="margin:0.25rem 0;">' + r.reasoning_context.life_events.map(e => '<li>' + esc(e) + '</li>').join('') + '</ul></div>' : ''}
                        ${r.reasoning_context.news_exposure && r.reasoning_context.news_exposure.length ? '<div><strong>News exposure:</strong><ul style="margin:0.25rem 0;">' + r.reasoning_context.news_exposure.map(e => '<li>' + esc(e) + '</li>').join('') + '</ul></div>' : ''}
                    </div>
                </details>
            ` : ''}
        </div>
    `;}).join('');
}

async function generateFocusGroup(turnNumber) {
    const viewer = document.getElementById('individual-responses');

    // Check if one already exists.
    const checkResp = await fetch(
        `/api/panels/${panelId}/conversations/${conversationId}/turns/${turnNumber}/focus-group`
    );
    if (checkResp.ok) {
        const existing = await checkResp.json();
        if (existing.exists) {
            displayFocusGroup(existing, turnNumber);
            return;
        }
    }

    // Generate new transcript.
    viewer.innerHTML = `
        <div style="padding:1rem; text-align:center;">
            <div style="color:var(--text-muted); margin-bottom:0.5rem;">Generating focus group discussion...</div>
            <div style="font-size:0.8rem; color:var(--text-muted);">This takes 30-60 seconds. The LLM is synthesising a multi-speaker debate from the individual responses.</div>
        </div>
    `;

    const resp = await fetch(
        `/api/panels/${panelId}/conversations/${conversationId}/turns/${turnNumber}/focus-group`,
        {method: 'POST'}
    );

    if (!resp.ok) {
        const err = await resp.json();
        viewer.innerHTML = `<p style="color:var(--negative);">Failed: ${err.error || 'unknown error'}</p>`;
        return;
    }

    const data = await resp.json();
    displayFocusGroup(data, turnNumber);
}

function displayFocusGroup(data, turnNumber) {
    const viewer = document.getElementById('individual-responses');
    const transcript = data.transcript || '';

    const lines = transcript.split('\n').filter(l => l.trim());
    const speakerColors = [
        '#4a9eff', '#ff6b6b', '#51cf66', '#ffd43b', '#cc5de8', '#ff922b', '#20c997', '#e599f7'
    ];
    const speakerMap = {};
    let speakerIdx = 0;
    let lastSpeaker = null;

    const bubbles = lines.map(line => {
        // Match both formats: "[Speaker 1 (Name)]:" and "[Name]:"
        const match = line.match(/^\[Speaker\s+(\d+)\s*\(([^)]+)\)\]:\s*(.*)/)
                   || line.match(/^\[([^\]]+)\]:\s*(.*)/);
        if (match) {
            let speakerNum, speakerName, dialogue;
            if (match.length === 4) {
                // [Speaker N (Name)]: dialogue
                speakerNum = match[1];
                speakerName = match[2];
                dialogue = match[3];
            } else {
                // [Name]: dialogue
                speakerName = match[1];
                speakerNum = speakerName; // Use name as key
                dialogue = match[2];
            }
            if (!speakerMap[speakerNum]) {
                speakerMap[speakerNum] = {
                    name: speakerName,
                    color: speakerColors[speakerIdx % speakerColors.length],
                    initials: speakerName.split(' ').map(w => w[0]).join('').slice(0, 2).toUpperCase(),
                    index: speakerIdx
                };
                speakerIdx++;
            }
            const s = speakerMap[speakerNum];
            const side = s.index % 2 === 0 ? 'left' : 'right';
            const isContinuation = lastSpeaker === speakerNum;
            lastSpeaker = speakerNum;

            const rightBubbleStyle = side === 'right'
                ? `background:${s.color}; color:#000; font-weight:400;`
                : '';

            const safeName = s.name.replace(/'/g, "\\'");
            return `
                <div class="fg-bubble-row ${side} ${isContinuation ? 'continuation' : ''}">
                    <div class="fg-avatar" style="background:${s.color}; cursor:pointer;" onclick="showPersonaModal('${safeName}')">${s.initials}</div>
                    <div class="fg-bubble-wrap">
                        <div class="fg-speaker-name" style="color:${s.color}; cursor:pointer;" onclick="showPersonaModal('${safeName}')">${s.name}</div>
                        <div class="fg-bubble" style="${rightBubbleStyle}">${esc(dialogue)}</div>
                    </div>
                </div>
            `;
        }
        // Stage directions / non-speaker text.
        lastSpeaker = null;
        return `<div style="text-align:center; font-size:0.75rem; color:var(--text-muted); font-style:italic; padding:0.5rem 0;">${esc(line)}</div>`;
    }).join('');

    // Build speaker legend (clickable).
    const legend = Object.values(speakerMap).map(s => {
        const sn = s.name.replace(/'/g, "\\'");
        return `<span style="display:inline-flex; align-items:center; gap:0.3rem; margin-right:0.75rem; cursor:pointer;" onclick="showPersonaModal('${sn}')">
            <span style="width:10px; height:10px; border-radius:50%; background:${s.color}; display:inline-block;"></span>
            <span style="font-size:0.75rem; text-decoration:underline; text-decoration-color:var(--border);">${s.name}</span>
        </span>`;
    }).join('');

    viewer.innerHTML = `
        <div class="fg-header">
            <h3>Focus Group — Turn ${turnNumber}</h3>
            <div style="font-size:0.8rem; color:var(--text-muted);">
                ${data.speaker_count || '?'} speakers
            </div>
        </div>
        <div style="padding:0.35rem 0.5rem; display:flex; flex-wrap:wrap; gap:0.25rem;">${legend}</div>
        <div class="fg-chat">
            ${bubbles}
        </div>
        <div class="fg-footer">
            Synthesised from ${data.word_count || '?'} words of individual persona responses
        </div>
    `;

    // Scroll chat to bottom.
    const chat = viewer.querySelector('.fg-chat');
    if (chat) chat.scrollTop = chat.scrollHeight;
}

// ---------------------------------------------------------------------------
// Persona detail modal
// ---------------------------------------------------------------------------

const DYNAMICS_LABELS = {
    D: 'Discipline', Y: 'Yielding', N: 'Novelty', A: 'Analytical',
    M: 'Morality', I: 'Intensity', C: 'Caution', S: 'Sociability'
};

async function showPersonaModal(name) {
    // Create or reuse modal overlay.
    let overlay = document.getElementById('persona-modal-overlay');
    if (!overlay) {
        overlay = document.createElement('div');
        overlay.id = 'persona-modal-overlay';
        overlay.className = 'modal-overlay active';
        overlay.onclick = (e) => { if (e.target === overlay) overlay.classList.remove('active'); };
        overlay.innerHTML = '<div class="modal" style="max-width:700px; max-height:85vh; overflow-y:auto;" id="persona-modal-content"><div class="loading">Loading persona...</div></div>';
        document.body.appendChild(overlay);
    } else {
        overlay.classList.add('active');
        document.getElementById('persona-modal-content').innerHTML = '<div class="loading">Loading persona...</div>';
    }

    const resp = await fetch(`/api/personas/lookup?name=${encodeURIComponent(name)}&panel_id=${panelId}`);
    if (!resp.ok) {
        document.getElementById('persona-modal-content').innerHTML = `<p style="color:var(--text-muted);">Persona "${name}" not found.</p>`;
        return;
    }
    const p = await resp.json();
    const id = p.identity || {};
    const pol = p.political || {};
    const fin = p.financial || {};
    const bel = p.beliefs || {};
    const emo = p.emotional_state || {};
    const rel = p.religious_cultural || {};
    const lc = p.lifecycle || {};
    const dyn = p.dynamics || {};

    // DYNAMICS-8 bars.
    const dynBars = ['D','Y','N','A','M','I','C','S'].map(dim => {
        const val = dyn[dim] || 0;
        const pct = (val * 100).toFixed(0);
        return `<div style="display:flex; align-items:center; gap:0.4rem; margin-bottom:0.25rem;">
            <span style="width:80px; font-size:0.75rem; color:var(--text-muted);">${DYNAMICS_LABELS[dim]}</span>
            <div style="flex:1; height:8px; background:var(--surface); border-radius:4px; overflow:hidden;">
                <div style="width:${pct}%; height:100%; background:var(--soul); border-radius:4px;"></div>
            </div>
            <span style="width:30px; font-size:0.7rem; color:var(--text-muted); text-align:right;">${val.toFixed(2)}</span>
        </div>`;
    }).join('');

    // Key issues pills.
    const issuesPills = (pol.key_issues || []).map(i =>
        `<span class="badge" style="background:var(--info); color:#fff; margin:0.15rem 0.1rem;">${i}</span>`
    ).join('');

    // Relationships list.
    const rels = (p.relationships || []).slice(0, 6).map(r => {
        if (typeof r === 'string') return `<li style="font-size:0.8rem;">${r}</li>`;
        return `<li style="font-size:0.8rem;"><strong>${r.name || r.type || '?'}</strong>: ${r.relationship || r.description || r.type || ''}</li>`;
    }).join('');

    // Formative experiences.
    const formative = (lc.formative_experiences || []).map(e =>
        `<li style="font-size:0.8rem;">${e}</li>`
    ).join('');

    document.getElementById('persona-modal-content').innerHTML = `
        <div style="display:flex; justify-content:space-between; align-items:flex-start;">
            <div>
                <h3 style="margin-bottom:0.25rem;">${p.name}</h3>
                <div style="font-size:0.7rem; font-family:monospace; color:var(--text-muted); margin-bottom:0.25rem;">${p.id || ''}</div>
                <div style="font-size:0.85rem; color:var(--text-muted);">
                    ${id.age || p.age || '?'} &middot; ${id.gender || '?'} &middot; ${id.ethnicity || ''}
                </div>
                <div style="font-size:0.85rem; color:var(--text-muted);">
                    ${id.occupation || p.occupation || '?'} &middot; ${id.occupation_sector || ''}
                </div>
                <div style="font-size:0.85rem; color:var(--text-muted);">
                    ${id.town || ''}, ${id.region || ''} &middot; ${id.housing_type || ''} &middot; ${id.household_composition || ''}
                </div>
            </div>
            <button class="btn btn-ghost" style="font-size:0.8rem;" onclick="document.getElementById('persona-modal-overlay').classList.remove('active')">Close</button>
        </div>

        <div style="display:grid; grid-template-columns:1fr 1fr; gap:0.75rem; margin-top:1rem;">
            <div class="card" style="margin-bottom:0;">
                <h3 style="font-size:0.85rem;">DYNAMICS-8 Profile</h3>
                ${dynBars}
                ${dyn.profile_summary ? `<p style="font-size:0.75rem; color:var(--text-muted); margin-top:0.5rem;">${dyn.profile_summary}</p>` : ''}
            </div>

            <div class="card" style="margin-bottom:0;">
                <h3 style="font-size:0.85rem;">Political</h3>
                <div style="font-size:0.85rem;">
                    <div><strong>Party:</strong> ${pol.party_affiliation || 'Unknown'}</div>
                    <div><strong>Engagement:</strong> ${pol.engagement_level || '?'}</div>
                </div>
                ${issuesPills ? `<div style="margin-top:0.35rem;">${issuesPills}</div>` : ''}
                ${Array.isArray(pol.voting_history) && pol.voting_history.length ? `
                    <div style="margin-top:0.5rem;">
                        <strong style="font-size:0.8rem;">Voting history:</strong>
                        ${pol.voting_history.map(v => `<div style="font-size:0.75rem; color:var(--text-muted); margin-top:0.2rem;">${v.year || '?'} ${v.election || ''}: <span style="color:var(--text);">${v.party_voted || '?'}</span>${v.reason ? ' — ' + v.reason : ''}</div>`).join('')}
                    </div>
                ` : ''}
                ${Array.isArray(pol.political_drift) && pol.political_drift.length ? `
                    <div style="margin-top:0.5rem;">
                        <strong style="font-size:0.8rem;">Political drift:</strong>
                        ${pol.political_drift.map(d => `<div style="font-size:0.75rem; color:var(--text-muted); margin-top:0.2rem;">${d.year || '?'}: ${d.from || '?'} → ${d.to || '?'}${d.trigger_event ? ' — ' + d.trigger_event : ''}</div>`).join('')}
                    </div>
                ` : ''}
            </div>

            <div class="card" style="margin-bottom:0;">
                <h3 style="font-size:0.85rem;">Financial</h3>
                <div style="font-size:0.85rem;">
                    <div><strong>Income:</strong> ${fin.annual_income ? '£' + Number(fin.annual_income).toLocaleString() : id.annual_income ? '£' + Number(id.annual_income).toLocaleString() : '?'}</div>
                    <div><strong>Housing:</strong> ${fin.housing_status || '?'}</div>
                    ${fin.credit_score_band ? `<div><strong>Credit band:</strong> ${fin.credit_score_band}</div>` : ''}
                    ${fin.savings_behaviour ? `<div><strong>Savings:</strong> ${fin.savings_behaviour}</div>` : ''}
                    ${fin.price_sensitivity ? `<div><strong>Price sensitivity:</strong> ${fin.price_sensitivity}</div>` : ''}
                </div>
            </div>

            <div class="card" style="margin-bottom:0;">
                <h3 style="font-size:0.85rem;">Beliefs &amp; Values</h3>
                <div style="font-size:0.8rem;">
                    ${bel.worldview_summary ? `<p>${bel.worldview_summary}</p>` : ''}
                    ${rel.faith ? `<div style="margin-top:0.35rem;"><strong>Faith:</strong> ${rel.faith} (${rel.practice_level || '?'})</div>` : ''}
                </div>
            </div>

            <div class="card" style="margin-bottom:0;">
                <h3 style="font-size:0.85rem;">Emotional Profile</h3>
                <div style="font-size:0.85rem;">
                    ${emo.baseline_mood ? `<div><strong>Baseline mood:</strong> ${emo.baseline_mood}</div>` : ''}
                    ${emo.emotional_volatility ? `<div><strong>Volatility:</strong> ${emo.emotional_volatility}</div>` : ''}
                    ${emo.resilience_rating ? `<div><strong>Resilience:</strong> ${emo.resilience_rating}</div>` : ''}
                    ${emo.stress_triggers ? `<div><strong>Stress triggers:</strong> ${Array.isArray(emo.stress_triggers) ? emo.stress_triggers.join(', ') : emo.stress_triggers}</div>` : ''}
                    ${emo.coping_mechanisms ? `<div><strong>Coping:</strong> ${Array.isArray(emo.coping_mechanisms) ? emo.coping_mechanisms.join(', ') : emo.coping_mechanisms}</div>` : ''}
                </div>
            </div>

            <div class="card" style="margin-bottom:0;">
                <h3 style="font-size:0.85rem;">Life &amp; Relationships</h3>
                <div style="font-size:0.85rem;">
                    ${lc.life_stage ? `<div><strong>Life stage:</strong> ${lc.life_stage}</div>` : ''}
                    ${lc.aspirations_short ? `<div><strong>Short-term goals:</strong> ${lc.aspirations_short}</div>` : ''}
                    ${lc.regrets ? `<div><strong>Regrets:</strong> ${lc.regrets}</div>` : ''}
                </div>
                ${rels ? `<ul style="margin:0.35rem 0 0 1rem; padding:0;">${rels}</ul>` : ''}
                ${formative ? `<div style="margin-top:0.5rem;"><strong style="font-size:0.8rem;">Formative experiences:</strong><ul style="margin:0.2rem 0 0 1rem; padding:0;">${formative}</ul></div>` : ''}
            </div>
        </div>
    `;
}

// ---------------------------------------------------------------------------
// Sub-panel from sentiment filter
// ---------------------------------------------------------------------------

let _lastViewedTurn = null;

async function createSubPanelFromSentiment(turnNumber, sentiment) {
    const name = prompt(`Name for the ${sentiment} sub-panel:`, `${sentiment} respondents — Turn ${turnNumber}`);
    if (!name) return;

    const resp = await fetch(`/api/panels/${panelId}/conversations/${conversationId}/turns/${turnNumber}/subpanel`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({sentiment, name})
    });
    if (resp.ok) {
        const result = await resp.json();
        const pid = result.id || result.panel_id;
        if (pid) {
            alert(`Sub-panel created. ${result.persona_count || '?'} personas.`);
            window.open(`/panels/${pid}`, '_blank');
        }
    } else {
        const err = await resp.json();
        alert(err.error || 'Failed to create sub-panel');
    }
}


// ---------------------------------------------------------------------------
// Direct persona conversation (1-to-1)
// ---------------------------------------------------------------------------

async function startDirectConversation(personaId, personaName) {
    // Create a 1-persona panel and open a new conversation with it.
    const name = `Direct: ${personaName}`;
    const resp = await fetch('/api/panels', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            name: name,
            description: `1-to-1 conversation with ${personaName}`,
            filters: {},
            source_panel: panelId,
            _direct_persona_ids: [personaId]
        })
    });

    if (resp.ok) {
        const panel = await resp.json();
        const pid = panel.id || panel.panel_id;
        if (pid) {
            window.open(`/panels/${pid}/conversation`, '_blank');
        }
    } else {
        const err = await resp.json();
        alert(err.error || 'Failed to create direct conversation');
    }
}


// ---------------------------------------------------------------------------
// Conversation rename
// ---------------------------------------------------------------------------

async function renameConversation() {
    const current = document.getElementById('conv-title').textContent;
    const title = prompt('Conversation title:', current);
    if (!title || title === current) return;
    const resp = await fetch(`/api/panels/${panelId}/conversations/${conversationId}`, {
        method: 'PATCH',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({title})
    });
    if (resp.ok) {
        document.getElementById('conv-title').textContent = title;
    }
}
