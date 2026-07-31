let nextRunTime = null;
let nextHealthCheckRunTime = null;

function pad(n)
{
    return String(n).padStart(2, "0");
}

function countdownText(target)
{
    const diffMs = target - new Date();
    if (diffMs <= 0)
    {
        return "due now";
    }
    const totalSeconds = Math.floor(diffMs / 1000);
    const minutes = Math.floor(totalSeconds / 60);
    const seconds = totalSeconds % 60;
    return pad(minutes) + ":" + pad(seconds);
}

function tick()
{
    if (nextRunTime)
    {
        document.getElementById("queueCleanerCountdown").textContent = countdownText(nextRunTime);
    }
    if (nextHealthCheckRunTime)
    {
        document.getElementById("healthCheckCountdown").textContent = countdownText(nextHealthCheckRunTime);
    }
}

function fmtTime(iso)
{
    const d = new Date(iso);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

async function loadStatus()
{
    const res = await fetch("/api/status");
    const data = await res.json();
    nextRunTime = new Date(data.next_run);
    nextHealthCheckRunTime = new Date(data.next_health_check_run);
}

function hadAction(lines)
{
    const markers = ["DEAD:", "SECURITY", "removed", "imported", "NEEDS MANUAL REVIEW", "ERROR"];
    const text = lines.join("\\n");
    return markers.some(function (m) { return text.indexOf(m) !== -1; });
}

function highlight(line)
{
    const esc = line.replace(/&/g, "&amp;").replace(/</g, "&lt;");
    if (esc.indexOf("SECURITY") !== -1)
    {
        return '<span class="flag-security">' + esc + '</span>';
    }
    if (esc.indexOf("ERROR") !== -1)
    {
        return '<span class="flag-error">' + esc + '</span>';
    }
    if (esc.indexOf("DEAD:") !== -1 || esc.indexOf("removed") !== -1 || esc.indexOf("imported") !== -1 || esc.indexOf("NEEDS MANUAL REVIEW") !== -1)
    {
        return '<span class="flag-action">' + esc + '</span>';
    }
    return esc;
}

const MAIN_PAGE_RUN_LIMIT = 3;

function expandRun(idx)
{
    if (idx >= MAIN_PAGE_RUN_LIMIT)
    {
        window.location.href = "/history";
        return;
    }
    const el = document.getElementById("run-" + idx);
    if (!el)
    {
        return;
    }
    el.classList.add("open");
    el.scrollIntoView({ behavior: "smooth", block: "center" });
}

async function loadRuns()
{
    const res = await fetch("/api/log");
    const data = await res.json();
    const runs = data.runs || [];

    const container = document.getElementById("runs");
    if (runs.length === 0)
    {
        container.innerHTML = '<div class="empty">No runs logged yet.</div>';
        document.getElementById("recentThumbs").innerHTML = '<div class="thumb-card"><div class="thumb-time">Nothing yet</div></div>';
        return;
    }

    let actionCount = 0;
    const flags = runs.map(function (run) { return hadAction(run.lines); });
    actionCount = flags.filter(Boolean).length;

    const html = runs.slice(0, MAIN_PAGE_RUN_LIMIT).map(function (run, idx)
    {
        const action = flags[idx];
        const badgeClass = action ? "badge-action" : "badge-clean";
        const badgeText = action ? "action taken" : "clean";
        const bodyLines = run.lines.map(highlight).join("\\n");
        return (
            '<div class="run">' +
                '<div class="run-header" onclick="toggleRun(' + idx + ')">' +
                    '<span class="run-time">' + run.timestamp + '</span>' +
                    '<span class="run-badge ' + badgeClass + '">' + badgeText + '</span>' +
                '</div>' +
                '<div class="run-body" id="run-' + idx + '">' + bodyLines + '</div>' +
            '</div>'
        );
    }).join("");
    container.innerHTML = html;

    document.getElementById("statTotal").textContent = runs.length;
    document.getElementById("statActions").textContent = actionCount;
    document.getElementById("statLast").textContent = runs[0] ? fmtTime(runs[0].timestamp.replace(" ", "T")) : "-";

    // recent fixes thumbnails -- the last 2 runs that actually did something,
    // falling back to the 2 most recent runs if nothing needed fixing lately
    let recent = runs.filter(function (_, idx) { return flags[idx]; }).slice(0, 2);
    if (recent.length === 0)
    {
        recent = runs.slice(0, 2);
    }
    document.getElementById("recentThumbs").innerHTML = recent.map(function (run)
    {
        const idx = runs.indexOf(run);
        const action = flags[idx];
        return (
            '<div class="thumb-card" onclick="expandRun(' + idx + ')" style="cursor:pointer">' +
                '<div class="thumb-time">' + run.timestamp + '</div>' +
                '<div class="thumb-tag">' + (action ? "Action taken" : "Clean pass") + '</div>' +
            '</div>'
        );
    }).join("");
}

function toggleRun(idx)
{
    const el = document.getElementById("run-" + idx);
    el.classList.toggle("open");
}

document.getElementById("addMenuBtn").addEventListener("click", function (e)
{
    e.stopPropagation();
    document.getElementById("addDropdown").classList.toggle("open");
});

document.addEventListener("click", function ()
{
    document.getElementById("addDropdown").classList.remove("open");
});

function escapeHtml(s)
{
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;");
}

let downloadItems = [];
let dlIndex = 0;

function renderHero()
{
    const track = document.getElementById("heroProgressTrack");
    const fill = document.getElementById("heroProgressFill");

    if (downloadItems.length === 0)
    {
        document.getElementById("heroDlTitle").textContent = "Nothing downloading right now";
        document.getElementById("heroDlSubtitle").textContent = "";
        document.getElementById("heroProgressPct").textContent = "";
        track.style.display = "none";
        return;
    }

    const item = downloadItems[dlIndex % downloadItems.length];
    document.getElementById("heroDlTitle").textContent = item.title;
    document.getElementById("heroDlSubtitle").textContent = item.subtitle;
    document.getElementById("heroProgressPct").textContent = item.progress + "% complete";
    fill.style.width = item.progress + "%";
    track.style.display = "block";
}

function renderDownloadCard()
{
    const container = document.getElementById("dlCarousel");
    renderHero();
    if (downloadItems.length === 0)
    {
        container.innerHTML = '<div class="dl-card visible"><div class="dl-title">Nothing downloading right now</div></div>';
        return;
    }

    const item = downloadItems[dlIndex % downloadItems.length];
    const dots = downloadItems.map(function (_, i)
    {
        return '<span class="dl-dot' + (i === (dlIndex % downloadItems.length) ? " current" : "") + '"></span>';
    }).join("");

    container.innerHTML =
        '<div class="dl-card" id="dlCard">' +
            '<span class="dl-source ' + item.source + '">' + item.source.toUpperCase() + '</span>' +
            '<div class="dl-title">' + escapeHtml(item.title) + '</div>' +
            '<div class="dl-subtitle">' + escapeHtml(item.subtitle) + '</div>' +
            '<div class="dl-progress-track"><div class="dl-progress-fill" style="width:' + item.progress + '%"></div></div>' +
            '<div class="dl-progress-pct">' + item.progress + '% complete</div>' +
            '<div class="dl-dots">' + dots + '</div>' +
        '</div>';

    requestAnimationFrame(function ()
    {
        const card = document.getElementById("dlCard");
        if (card)
        {
            card.classList.add("visible");
        }
    });
}

async function loadDownloads()
{
    const res = await fetch("/api/downloads");
    const data = await res.json();
    downloadItems = data.items || [];
    if (dlIndex >= downloadItems.length)
    {
        dlIndex = 0;
    }
    renderDownloadCard();
    renderModalList();
}

function renderModalList()
{
    const list = document.getElementById("modalList");
    if (downloadItems.length === 0)
    {
        list.innerHTML = '<div class="modal-empty">Nothing downloading right now</div>';
        return;
    }

    list.innerHTML = downloadItems.map(function (item)
    {
        return (
            '<div class="modal-row">' +
                '<div class="modal-row-top">' +
                    '<div>' +
                        '<span class="dl-source ' + item.source + '">' + item.source.toUpperCase() + '</span>' +
                        '<div class="modal-row-title">' + escapeHtml(item.title) + '</div>' +
                        '<div class="modal-row-subtitle">' + escapeHtml(item.subtitle) + '</div>' +
                    '</div>' +
                    '<div class="modal-row-pct">' + item.progress + '%</div>' +
                '</div>' +
                '<div class="dl-progress-track"><div class="dl-progress-fill" style="width:' + item.progress + '%"></div></div>' +
            '</div>'
        );
    }).join("");
}

function openDownloadsModal()
{
    renderModalList();
    document.getElementById("downloadsModal").classList.add("open");
}

function closeDownloadsModal()
{
    document.getElementById("downloadsModal").classList.remove("open");
}

function openHealthCheckModal()
{
    document.getElementById("healthCheckModal").classList.add("open");
}

function closeHealthCheckModal()
{
    document.getElementById("healthCheckModal").classList.remove("open");
}

function openQueueCleanerModal()
{
    document.getElementById("queueCleanerModal").classList.add("open");
}

function closeQueueCleanerModal()
{
    document.getElementById("queueCleanerModal").classList.remove("open");
}

// --- Sonarr & Radarr command queue modal (live) ---
let commandsData = null;
let commandsFetchedAt = 0;   // client clock when the data was fetched, for smooth count-up
let commandsPollTimer = null;
let commandsTickTimer = null;

function fmtDuration(secs)
{
    secs = Math.max(0, Math.floor(secs));
    if (secs < 60) { return secs + "s"; }
    const m = Math.floor(secs / 60);
    const s = secs % 60;
    if (m < 60) { return m + "m " + pad(s) + "s"; }
    const h = Math.floor(m / 60);
    return h + "h " + pad(m % 60) + "m";
}

async function loadCommands()
{
    try
    {
        const res = await fetch("/api/commands");
        commandsData = await res.json();
        commandsFetchedAt = Date.now();
    }
    catch (e)
    {
        commandsData = { reachable: false };
    }
    renderCommandsModal();
}

function renderCommandsModal()
{
    const list = document.getElementById("commandsModalList");
    if (!commandsData || !commandsData.reachable)
    {
        document.getElementById("commandsLive").textContent = "offline";
        list.innerHTML = '<div class="modal-empty">Couldn&rsquo;t reach Sonarr or Radarr</div>';
        return;
    }
    document.getElementById("commandsLive").textContent = "live";

    const running = commandsData.running || [];
    const queued = commandsData.queued || [];
    const elapsedBonus = (Date.now() - commandsFetchedAt) / 1000;  // seconds since fetch, for live count-up

    let html = "";

    html += '<div class="cmd-section-label">Running now (' + running.length + ')</div>';
    if (running.length === 0)
    {
        html += '<div class="modal-empty" style="padding:1rem">Nothing running right now</div>';
    }
    running.forEach(function (c)
    {
        const secs = (c.running_seconds || 0) + elapsedBonus;
        html +=
            '<div class="modal-row">' +
                '<div class="modal-row-top">' +
                    '<div>' +
                        '<span class="cmd-source ' + c.source + '">' + c.source.toUpperCase() + '</span>' +
                        '<div class="modal-row-title">' + escapeHtml(c.friendly) + '</div>' +
                        (c.detail ? '<div class="modal-row-subtitle">' + escapeHtml(c.detail) + '</div>' : '') +
                    '</div>' +
                    '<div class="modal-row-pct cmd-elapsed">' + fmtDuration(secs) + '</div>' +
                '</div>' +
            '</div>';
    });

    html += '<div class="cmd-section-label">Waiting in line (' + queued.length + ')</div>';
    if (queued.length === 0)
    {
        html += '<div class="modal-empty" style="padding:1rem">Nothing waiting</div>';
    }
    queued.forEach(function (c, idx)
    {
        html +=
            '<div class="modal-row">' +
                '<div class="modal-row-top">' +
                    '<div>' +
                        '<span class="cmd-source ' + c.source + '">' + c.source.toUpperCase() + '</span>' +
                        '<div class="modal-row-title">' + escapeHtml(c.friendly) + '</div>' +
                        (c.detail ? '<div class="modal-row-subtitle">' + escapeHtml(c.detail) + '</div>' : '') +
                    '</div>' +
                    '<div class="modal-row-pct" style="color:var(--dim)">#' + (idx + 1) + '</div>' +
                '</div>' +
            '</div>';
    });

    list.innerHTML = html;
}

function tickCommands()
{
    // recompute the running-command elapsed labels every second without refetching
    if (!commandsData || !commandsData.reachable) { return; }
    const running = commandsData.running || [];
    const nodes = document.querySelectorAll("#commandsModalList .cmd-elapsed");
    const elapsedBonus = (Date.now() - commandsFetchedAt) / 1000;
    running.forEach(function (c, i)
    {
        if (nodes[i]) { nodes[i].textContent = fmtDuration((c.running_seconds || 0) + elapsedBonus); }
    });
}

function openCommandsModal()
{
    document.getElementById("commandsModal").classList.add("open");
    loadCommands();
    clearInterval(commandsPollTimer);
    clearInterval(commandsTickTimer);
    commandsPollTimer = setInterval(loadCommands, 3000);  // refetch every 3s
    commandsTickTimer = setInterval(tickCommands, 1000);  // smooth count-up
}

function closeCommandsModal()
{
    document.getElementById("commandsModal").classList.remove("open");
    clearInterval(commandsPollTimer);
    clearInterval(commandsTickTimer);
    commandsPollTimer = null;
    commandsTickTimer = null;
}

function advanceCarousel()
{
    if (downloadItems.length === 0)
    {
        return;
    }
    dlIndex = (dlIndex + 1) % downloadItems.length;
    renderDownloadCard();
}

// hero splash-screen: crossfades the hero banner's background through
// poster/fanart art for whatever's currently downloading, falling back to
// the static night-sky image when nothing has art or nothing is downloading
let heroImages = ["/bg.png"];
let heroIndex = 0;
let heroShowingA = true;

function buildHeroImages()
{
    const fromDownloads = downloadItems.map(function (i) { return i.image; }).filter(Boolean);
    heroImages = fromDownloads.length > 0 ? fromDownloads : ["/bg.png"];
    if (heroIndex >= heroImages.length)
    {
        heroIndex = 0;
    }
}

function advanceHero()
{
    if (heroImages.length <= 1)
    {
        return;
    }
    heroIndex = (heroIndex + 1) % heroImages.length;
    const nextUrl = heroImages[heroIndex];
    const incoming = document.getElementById(heroShowingA ? "heroBgB" : "heroBgA");
    const outgoing = document.getElementById(heroShowingA ? "heroBgA" : "heroBgB");
    incoming.style.backgroundImage = "url('" + nextUrl + "')";
    incoming.classList.add("current");
    outgoing.classList.remove("current");
    heroShowingA = !heroShowingA;
}

function formatSpeed(bytesPerSec)
{
    return (bytesPerSec / 1024 / 1024).toFixed(1) + " MB/s";
}

async function loadHealth()
{
    let data;
    try
    {
        const res = await fetch("/api/health");
        data = await res.json();
    }
    catch (e)
    {
        data = {};
    }

    const qbit = data.qbittorrent;
    const qbitIcon = document.getElementById("healthQbitIcon");
    if (!qbit || !qbit.reachable)
    {
        document.getElementById("healthQbitSpeed").textContent = "Unreachable";
        document.getElementById("healthQbitDesc").textContent = "qBittorrent did not respond";
        qbitIcon.className = "info-icon warn";
    }
    else if (qbit.stalled_count > 0)
    {
        document.getElementById("healthQbitSpeed").textContent = formatSpeed(qbit.dl_speed);
        document.getElementById("healthQbitDesc").textContent = qbit.stalled_count + " torrent" + (qbit.stalled_count === 1 ? "" : "s") + " stalled 20+ min";
        qbitIcon.className = "info-icon warn";
    }
    else
    {
        document.getElementById("healthQbitSpeed").textContent = formatSpeed(qbit.dl_speed);
        document.getElementById("healthQbitDesc").textContent = "Downloading, nothing stalled";
        qbitIcon.className = "info-icon mint";
    }

    const commands = data.commands;
    const commandsIcon = document.getElementById("healthCommandsIcon");
    if (!commands || !commands.reachable)
    {
        document.getElementById("healthCommandsTitle").textContent = "Unreachable";
        document.getElementById("healthCommandsDesc").textContent = "Sonarr and Radarr did not respond";
        commandsIcon.className = "info-icon warn";
    }
    else if (commands.longest_running_minutes > 15)
    {
        document.getElementById("healthCommandsTitle").textContent = commands.started_count + " running";
        document.getElementById("healthCommandsDesc").textContent = commands.queued_count + " queued, longest running " + Math.round(commands.longest_running_minutes) + " min";
        commandsIcon.className = "info-icon warn";
    }
    else
    {
        document.getElementById("healthCommandsTitle").textContent = commands.started_count + " running";
        document.getElementById("healthCommandsDesc").textContent = commands.queued_count + " queued";
        commandsIcon.className = "info-icon mint";
    }
}

let lastSuccessTime = null;
let liveHasErrored = false;

function updateLivePill()
{
    const pill = document.getElementById("livePill");
    const dot = document.getElementById("liveDot");
    const text = document.getElementById("liveText");

    if (!lastSuccessTime)
    {
        text.textContent = "Starting…";
        return;
    }

    const seconds = Math.floor((new Date() - lastSuccessTime) / 1000);
    let label;
    if (seconds < 5)
    {
        label = "Updated just now";
    }
    else if (seconds < 60)
    {
        label = "Updated " + seconds + "s ago";
    }
    else
    {
        label = "Updated " + Math.floor(seconds / 60) + "m ago";
    }

    // stale past 3 missed 30s refresh cycles, or the last attempt itself failed
    const stale = liveHasErrored || seconds > 90;
    pill.classList.toggle("warn", stale);
    dot.classList.toggle("warn", stale);
    text.textContent = stale && liveHasErrored ? "Connection issue" : label;
}

async function refreshAll()
{
    try
    {
        await loadHealth();
        await loadStatus();
        await loadRuns();
        await loadDownloads();
        buildHeroImages();
        lastSuccessTime = new Date();
        liveHasErrored = false;
    }
    catch (e)
    {
        liveHasErrored = true;
    }
    updateLivePill();
}

refreshAll();
setInterval(tick, 1000);
setInterval(updateLivePill, 1000);
setInterval(refreshAll, 30000);
setInterval(advanceCarousel, 4000);
setInterval(advanceHero, 6000);
