let currentType = "show";
let libraryItems = [];

function escapeHtml(s)
{
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;");
}

function friendlyError(raw)
{
    const text = String(raw || "");
    if (text.indexOf("Connection refused") !== -1 || text.indexOf("urlopen error") !== -1)
    {
        return "Couldn't reach Sonarr or Radarr right now.";
    }
    if (text.indexOf("timed out") !== -1)
    {
        return "Sonarr or Radarr took too long to respond.";
    }
    return "Something went wrong loading the library.";
}

function getUrlType()
{
    const params = new URLSearchParams(window.location.search);
    const t = params.get("type");
    return t === "movie" ? "movie" : "show";
}

function switchType(type)
{
    currentType = type;
    document.getElementById("tabShow").classList.toggle("active", type === "show");
    document.getElementById("tabMovie").classList.toggle("active", type === "movie");
    const url = new URL(window.location);
    url.searchParams.set("type", type);
    window.history.replaceState({}, "", url);
    document.getElementById("filterInput").value = "";
    loadLibrary();
}

async function loadLibrary()
{
    document.getElementById("libGrid").innerHTML = '<div class="empty">Loading&hellip;</div>';
    document.getElementById("libCount").textContent = "";

    let data;
    try
    {
        const res = await fetch("/api/library?type=" + currentType);
        data = await res.json();
    }
    catch (e)
    {
        data = { error: String(e) };
    }

    if (data.error)
    {
        document.getElementById("libGrid").innerHTML = '<div class="empty">' + escapeHtml(friendlyError(data.error)) + '</div>';
        return;
    }

    libraryItems = data.items || [];
    document.getElementById("libCount").textContent = libraryItems.length + (currentType === "show" ? " series" : " movies");
    renderLegendStats(data.stats || {});
    renderGrid();
}

function renderLegendStats(stats)
{
    const legendItems = currentType === "show"
        ? [
                ["continuing", "Continuing (All episodes downloaded)"],
                ["ended", "Ended (All episodes downloaded)"],
                ["missing_monitored", "Missing Episodes (Series monitored)"],
                ["missing_unmonitored", "Missing Episodes (Series not monitored)"],
                ["downloading", "Downloading (One or more episodes)"],
            ]
        : [
                ["downloaded", "Downloaded"],
                ["missing_monitored", "Missing (Monitored)"],
                ["missing_unmonitored", "Missing (Not monitored)"],
                ["downloading", "Downloading"],
            ];

    const legendHtml = legendItems.map(function (li)
    {
        return '<div class="legend-item"><span class="legend-swatch swatch-' + li[0] + '"></span>' + li[1] + '</div>';
    }).join("");

    const statCols = currentType === "show"
        ? [
                [["Series", stats.total], ["Monitored", stats.monitored], ["Unmonitored", stats.unmonitored]],
                [["Continuing", stats.continuing], ["Ended", stats.ended]],
                [["Episodes", stats.episodes], ["Files", stats.files]],
                [["Total File Size", stats.size_display]],
            ]
        : [
                [["Movies", stats.total], ["Monitored", stats.monitored], ["Unmonitored", stats.unmonitored]],
                [["Downloaded", stats.downloaded], ["Missing", stats.missing_monitored + stats.missing_unmonitored]],
                [["Total File Size", stats.size_display]],
            ];

    const statsHtml = statCols.map(function (col)
    {
        const rows = col.map(function (pair)
        {
            return '<div class="stat-row"><span>' + pair[0] + '</span><strong>' + pair[1] + '</strong></div>';
        }).join("");
        return '<div class="stat-col">' + rows + '</div>';
    }).join("");

    document.getElementById("legendStats").innerHTML =
        '<div class="legend">' + legendHtml + '</div>' +
        '<div class="stat-cols">' + statsHtml + '</div>';
}

function renderGrid()
{
    const term = document.getElementById("filterInput").value.trim().toLowerCase();
    const filtered = term
        ? libraryItems.filter(function (i) { return (i.title || "").toLowerCase().indexOf(term) !== -1; })
        : libraryItems;

    const grid = document.getElementById("libGrid");
    if (filtered.length === 0)
    {
        grid.innerHTML = '<div class="empty">No matches</div>';
        return;
    }

    grid.innerHTML = filtered.map(function (item)
    {
        const poster = item.poster ? '<img class="lib-poster" src="' + item.poster + '" loading="lazy">' : '<div class="lib-poster"></div>';
        const pct = item.total > 0 ? Math.round(100 * item.has_file / item.total) : 0;
        const barClass = item.category;
        const countLabel = currentType === "show" ? (item.has_file + "/" + item.total) : (item.has_file ? "Downloaded" : "Missing");

        return (
            '<div class="lib-card">' +
                poster +
                '<div class="lib-info">' +
                    '<div class="lib-title" title="' + escapeHtml(item.title) + '">' + escapeHtml(item.title) + '</div>' +
                    '<div class="lib-year">' + (item.year || "") + '</div>' +
                    '<div class="lib-bar-track"><div class="lib-bar-fill ' + barClass + '" style="width:' + pct + '%"></div></div>' +
                    '<div class="lib-meta"><span>' + escapeHtml(item.profile) + '</span><span>' + countLabel + '</span></div>' +
                '</div>' +
            '</div>'
        );
    }).join("");
}

currentType = getUrlType();
document.getElementById("tabShow").classList.toggle("active", currentType === "show");
document.getElementById("tabMovie").classList.toggle("active", currentType === "movie");
loadLibrary();
