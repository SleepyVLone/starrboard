let calYear;
let calMonth;

function pad2(n)
{
    return String(n).padStart(2, "0");
}

function isoDate(d)
{
    return d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate());
}

function escapeHtml(s)
{
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;");
}

function colorForTitle(title)
{
    let hash = 0;
    for (let i = 0; i < title.length; i++)
    {
        hash = title.charCodeAt(i) + ((hash << 5) - hash);
    }
    const hue = Math.abs(hash) % 360;
    return "hsl(" + hue + ", 65%, 62%)";
}

async function loadCalendar()
{
    const firstOfMonth = new Date(calYear, calMonth, 1);
    const lastOfMonth = new Date(calYear, calMonth + 1, 0);

    const gridStart = new Date(firstOfMonth);
    gridStart.setDate(gridStart.getDate() - gridStart.getDay());
    const gridEnd = new Date(lastOfMonth);
    gridEnd.setDate(gridEnd.getDate() + (6 - gridEnd.getDay()));

    document.getElementById("calMonthLabel").textContent =
        firstOfMonth.toLocaleDateString([], { month: "long", year: "numeric" });

    let events = [];
    try
    {
        const res = await fetch("/api/calendar?start=" + isoDate(gridStart) + "&end=" + isoDate(gridEnd));
        const data = await res.json();
        events = data.events || [];
    }
    catch (e)
    {
        events = [];
    }

    renderCalendar(gridStart, gridEnd, events);
}

function renderCalendar(gridStart, gridEnd, events)
{
    const byDate = {};
    events.forEach(function (e)
    {
        (byDate[e.date] = byDate[e.date] || []).push(e);
    });

    const todayStr = isoDate(new Date());
    const cells = [];
    const cursor = new Date(gridStart);

    while (cursor <= gridEnd)
    {
        const dateStr = isoDate(cursor);
        const dayEvents = (byDate[dateStr] || []).slice().sort(function (a, b)
        {
            return (a.time || "").localeCompare(b.time || "");
        });
        const inMonth = cursor.getMonth() === calMonth;
        const isToday = dateStr === todayStr;

        const eventsHtml = dayEvents.map(function (e)
        {
            const color = colorForTitle(e.title);
            const sub = e.subtitle + (e.time ? " &middot; " + e.time : "");
            return (
                '<div class="cal-event' + (e.has_file ? " has-file" : "") + '" style="border-left-color:' + color + '" title="' + escapeHtml(e.title) + " (" + escapeHtml(e.subtitle) + ')">' +
                    '<div class="cal-event-title">' + escapeHtml(e.title) + '</div>' +
                    '<div class="cal-event-sub">' + escapeHtml(sub) + '</div>' +
                '</div>'
            );
        }).join("");

        cells.push(
            '<div class="cal-cell' + (inMonth ? "" : " dim") + (isToday ? " today" : "") + '">' +
                '<div class="cal-daynum">' + cursor.getDate() + '</div>' +
                eventsHtml +
            '</div>'
        );
        cursor.setDate(cursor.getDate() + 1);
    }

    document.getElementById("calGrid").innerHTML = cells.join("");
}

let viewingCurrentMonth = true;

function shiftMonth(delta)
{
    viewingCurrentMonth = false;
    calMonth += delta;
    if (calMonth < 0)
    {
        calMonth = 11;
        calYear -= 1;
    }
    if (calMonth > 11)
    {
        calMonth = 0;
        calYear += 1;
    }
    loadCalendar();
}

function goToday()
{
    viewingCurrentMonth = true;
    const now = new Date();
    calYear = now.getFullYear();
    calMonth = now.getMonth();
    loadCalendar();
}

goToday();
setInterval(function ()
{
    // only auto-follow the date rollover if the user hasn't manually browsed
    // to a different month -- don't yank them out of one they chose to view
    const now = new Date();
    if (viewingCurrentMonth && (now.getFullYear() !== calYear || now.getMonth() !== calMonth))
    {
        goToday();
    }
    else
    {
        loadCalendar();
    }
}, 60000);
