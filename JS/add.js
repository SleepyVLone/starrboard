let currentType = "show";
let searchTimer = null;
let searchToken = 0;

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
    return "Something went wrong.";
}

function getUrlType()
{
    const params = new URLSearchParams(window.location.search);
    const t = params.get("type");
    return t === "movie" ? "movie" : "show";
}

let folderSeriesTypeMap = {};

async function loadDefaults()
{
    let data;
    try
    {
        const res = await fetch("/api/add-defaults?type=" + currentType);
        data = await res.json();
    }
    catch (e)
    {
        data = { error: String(e) };
    }

    if (data.error)
    {
        document.getElementById("results").innerHTML = '<div class="empty">' + escapeHtml(friendlyError(data.error)) + '</div>';
        return;
    }

    const profileSelect = document.getElementById("profileSelect");
    const folderSelect = document.getElementById("folderSelect");

    profileSelect.innerHTML = (data.profiles || []).map(function (p)
    {
        return '<option value="' + p.id + '"' + (p.id === data.default_profile_id ? " selected" : "") + '>' + escapeHtml(p.name) + '</option>';
    }).join("");

    folderSelect.innerHTML = (data.folders || []).map(function (f)
    {
        return '<option value="' + escapeHtml(f.path) + '"' + (f.path === data.default_folder ? " selected" : "") + '>' + escapeHtml(f.path) + '</option>';
    }).join("");

    document.getElementById("typeField").style.display = currentType === "show" ? "" : "none";
    folderSeriesTypeMap = {};
    (data.folders || []).forEach(function (f) { folderSeriesTypeMap[f.path] = f.default_series_type || "standard"; });
    document.getElementById("typeSelect").value = data.default_series_type || "standard";
}

function onFolderChange()
{
    if (currentType !== "show") return;
    const folder = document.getElementById("folderSelect").value;
    document.getElementById("typeSelect").value = folderSeriesTypeMap[folder] || "standard";
}

function switchType(type)
{
    currentType = type;
    document.getElementById("tabShow").classList.toggle("active", type === "show");
    document.getElementById("tabMovie").classList.toggle("active", type === "movie");
    const url = new URL(window.location);
    url.searchParams.set("type", type);
    window.history.replaceState({}, "", url);
    loadDefaults();
    const term = document.getElementById("searchInput").value.trim();
    if (term.length >= 2)
    {
        runSearch(term);
    }
    else
    {
        document.getElementById("results").innerHTML = '<div class="empty">Start typing to search</div>';
    }
}

function onSearchInput()
{
    const term = document.getElementById("searchInput").value.trim();
    clearTimeout(searchTimer);
    if (term.length < 2)
    {
        document.getElementById("results").innerHTML = '<div class="empty">Start typing to search</div>';
        return;
    }
    searchTimer = setTimeout(function () { runSearch(term); }, 400);
}

async function runSearch(term)
{
    const myToken = ++searchToken;
    document.getElementById("results").innerHTML = '<div class="empty">Searching&hellip;</div>';

    let data;
    try
    {
        const res = await fetch("/api/lookup?type=" + currentType + "&term=" + encodeURIComponent(term));
        data = await res.json();
    }
    catch (e)
    {
        data = { error: String(e) };
    }

    if (myToken !== searchToken)
    {
        return;  // a newer search superseded this one
    }

    if (data.error)
    {
        document.getElementById("results").innerHTML = '<div class="empty">' + escapeHtml(friendlyError(data.error)) + '</div>';
        return;
    }

    const results = data.results || [];
    if (results.length === 0)
    {
        document.getElementById("results").innerHTML = '<div class="empty">No matches found</div>';
        return;
    }

    document.getElementById("results").innerHTML = results.map(function (r, idx)
    {
        const poster = r.poster ? '<img class="result-poster" src="' + r.poster + '">' : '<div class="result-poster"></div>';
        const btn = r.already_added
            ? '<button class="add-btn added" disabled>In library</button>'
            : '<button class="add-btn" id="add-btn-' + idx + '" onclick="doAdd(' + idx + ')">Add</button>';
        return (
            '<div class="result-card">' +
                poster +
                '<div class="result-info">' +
                    '<div class="result-title">' + escapeHtml(r.title) + ' <span class="result-year">' + (r.year || "") + '</span></div>' +
                    '<div class="result-overview">' + escapeHtml(r.overview || "") + '</div>' +
                '</div>' +
                '<div class="result-action">' + btn + '</div>' +
            '</div>'
        );
    }).join("");

    window.currentResults = results;
}

async function doAdd(idx)
{
    const r = window.currentResults[idx];
    const btn = document.getElementById("add-btn-" + idx);
    btn.disabled = true;
    btn.textContent = "Adding…";

    const payload = {
        type: currentType,
        tvdb_id: r.tvdb_id,
        tmdb_id: r.tmdb_id,
        profile_id: document.getElementById("profileSelect").value,
        root_folder: document.getElementById("folderSelect").value,
        series_type: currentType === "show" ? document.getElementById("typeSelect").value : undefined,
    };

    try
    {
        const res = await fetch("/api/add", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (data.ok)
        {
            btn.textContent = "Added";
            btn.classList.add("added");
        }
        else
        {
            btn.textContent = "Failed";
            btn.classList.add("failed");
            btn.disabled = false;
        }
    }
    catch (e)
    {
        btn.textContent = "Failed";
        btn.classList.add("failed");
        btn.disabled = false;
    }
}

currentType = getUrlType();
document.getElementById("tabShow").classList.toggle("active", currentType === "show");
document.getElementById("tabMovie").classList.toggle("active", currentType === "movie");
loadDefaults();
