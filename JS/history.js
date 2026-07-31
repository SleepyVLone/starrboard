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

function toggleRun(idx)
{
    document.getElementById("run-" + idx).classList.toggle("open");
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
        return;
    }

    container.innerHTML = runs.map(function (run, idx)
    {
        const action = hadAction(run.lines);
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
}

loadRuns();
setInterval(loadRuns, 30000);
