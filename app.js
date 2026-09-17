(function () {
  "use strict";

  var STORAGE_KEY = "discipline_v1";

  function dateKey(d) {
    var y = d.getFullYear();
    var m = String(d.getMonth() + 1).padStart(2, "0");
    var day = String(d.getDate()).padStart(2, "0");
    return y + "-" + m + "-" + day;
  }

  function formatLabel(key) {
    var d = new Date(key + "T00:00:00");
    return d.toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" });
  }

  function todayKey() {
    return dateKey(new Date());
  }

  function tomorrowKey() {
    var d = new Date();
    d.setDate(d.getDate() + 1);
    return dateKey(d);
  }

  // Migrates tasks saved before multi-slot times existed (a single
  // "time" string) to the "times" array shape used everywhere below.
  function normalize(parsed) {
    Object.keys(parsed.days).forEach(function (key) {
      parsed.days[key].forEach(function (task) {
        if (!task.times) {
          task.times = task.time ? [task.time] : null;
        }
        delete task.time;
      });
    });
    return parsed;
  }

  function load() {
    try {
      var raw = localStorage.getItem(STORAGE_KEY);
      return raw ? normalize(JSON.parse(raw)) : { days: {} };
    } catch (e) {
      return { days: {} };
    }
  }

  function save(data) {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(data));
  }

  var data = load();

  function getTasks(key) {
    return data.days[key] || [];
  }

  function timeToMinutes(t) {
    var parts = t.split(":");
    return Number(parts[0]) * 60 + Number(parts[1]);
  }

  function minutesToTime(min) {
    min = ((min % 1440) + 1440) % 1440;
    var h = Math.floor(min / 60);
    var m = min % 60;
    return String(h).padStart(2, "0") + ":" + String(m).padStart(2, "0");
  }

  function earliestMinutes(task) {
    if (!task.times || task.times.length === 0) return null;
    return Math.min.apply(null, task.times.map(timeToMinutes));
  }

  // Timed tasks sort by their earliest selected slot; untimed tasks
  // keep their add order and fall after all timed ones.
  function sortedTasks(key) {
    return getTasks(key)
      .map(function (t, i) { return { t: t, i: i, m: earliestMinutes(t) }; })
      .sort(function (a, b) {
        if (a.m !== null && b.m !== null) return a.m - b.m || a.i - b.i;
        if (a.m !== null) return -1;
        if (b.m !== null) return 1;
        return a.i - b.i;
      })
      .map(function (x) { return x.t; });
  }

  function formatTime(time) {
    var parts = time.split(":");
    var d = new Date();
    d.setHours(Number(parts[0]), Number(parts[1]), 0, 0);
    return d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  }

  // Renders selected slots as merged ranges, e.g. two adjacent 30-minute
  // slots become "7:00 – 8:00 AM"; separate periods are comma-joined.
  function formatTimes(times) {
    if (!times || times.length === 0) return "";
    var sorted = times.slice().sort(function (a, b) { return timeToMinutes(a) - timeToMinutes(b); });
    var groups = [[sorted[0]]];
    for (var i = 1; i < sorted.length; i++) {
      var group = groups[groups.length - 1];
      if (timeToMinutes(sorted[i]) === timeToMinutes(group[group.length - 1]) + 30) {
        group.push(sorted[i]);
      } else {
        groups.push([sorted[i]]);
      }
    }
    return groups.map(function (group) {
      var end = minutesToTime(timeToMinutes(group[group.length - 1]) + 30);
      return formatTime(group[0]) + " – " + formatTime(end);
    }).join(", ");
  }

  function addTask(key, text, times, category) {
    if (!data.days[key]) data.days[key] = [];
    data.days[key].push({
      id: String(Date.now()) + Math.random().toString(36).slice(2),
      text: text,
      times: times && times.length ? times.slice().sort(function (a, b) { return timeToMinutes(a) - timeToMinutes(b); }) : null,
      category: category || null,
      done: false
    });
    save(data);
  }

  function toggleTask(key, id) {
    var tasks = getTasks(key);
    var task = tasks.find(function (t) { return t.id === id; });
    if (task) {
      task.done = !task.done;
      save(data);
    }
  }

  function deleteTask(key, id) {
    if (!data.days[key]) return;
    data.days[key] = data.days[key].filter(function (t) { return t.id !== id; });
    save(data);
  }

  // Streak: consecutive fully-completed days counting back from yesterday.
  // Today is excluded because it isn't finished yet.
  function computeStreak() {
    var streak = 0;
    var cursor = new Date();
    cursor.setDate(cursor.getDate() - 1);
    while (true) {
      var key = dateKey(cursor);
      var tasks = getTasks(key);
      if (tasks.length === 0) break;
      var allDone = tasks.every(function (t) { return t.done; });
      if (!allDone) break;
      streak++;
      cursor.setDate(cursor.getDate() - 1);
    }
    return streak;
  }

  function renderList(listEl, emptyEl, key, showCheckbox) {
    var tasks = sortedTasks(key);
    listEl.innerHTML = "";
    emptyEl.style.display = tasks.length === 0 ? "block" : "none";

    tasks.forEach(function (task) {
      var li = document.createElement("li");
      li.className = "task-item" + (task.done ? " done" : "");

      if (showCheckbox) {
        var checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.checked = task.done;
        checkbox.addEventListener("change", function () {
          toggleTask(key, task.id);
          render();
        });
        li.appendChild(checkbox);
      }

      var timeEl = document.createElement("span");
      timeEl.className = "task-time";
      timeEl.textContent = formatTimes(task.times);
      li.appendChild(timeEl);

      if (task.category) {
        var categoryEl = document.createElement("span");
        categoryEl.className = "task-category";
        categoryEl.textContent = task.category;
        li.appendChild(categoryEl);
      }

      var span = document.createElement("span");
      span.className = "task-text";
      span.textContent = task.text;
      li.appendChild(span);

      var del = document.createElement("button");
      del.className = "task-delete";
      del.textContent = "remove";
      del.addEventListener("click", function () {
        deleteTask(key, task.id);
        render();
      });
      li.appendChild(del);

      listEl.appendChild(li);
    });
  }

  function render() {
    var tKey = todayKey();
    var pKey = tomorrowKey();

    document.getElementById("today-date").textContent = formatLabel(tKey);
    document.getElementById("plan-date").textContent = "For " + formatLabel(pKey);

    var todayTasks = getTasks(tKey);
    var doneCount = todayTasks.filter(function (t) { return t.done; }).length;
    document.getElementById("today-progress").textContent =
      todayTasks.length === 0 ? "" : doneCount + " of " + todayTasks.length + " done";

    renderList(document.getElementById("today-list"), document.getElementById("today-empty"), tKey, true);
    renderList(document.getElementById("plan-list"), document.getElementById("plan-empty"), pKey, false);

    var streak = computeStreak();
    document.getElementById("streak").textContent = streak > 0 ? streak + " day streak" : "";
  }

  function setupTabs() {
    var buttons = document.querySelectorAll(".tab-btn");
    buttons.forEach(function (btn) {
      btn.addEventListener("click", function () {
        buttons.forEach(function (b) { b.classList.remove("active"); });
        document.querySelectorAll(".view").forEach(function (v) { v.classList.remove("active"); });
        btn.classList.add("active");
        document.getElementById("view-" + btn.dataset.view).classList.add("active");
      });
    });
  }

  // 30-minute slots across a full day, e.g. "00:00", "00:30", ... "23:30".
  function allSlots() {
    var slots = [];
    for (var m = 0; m < 1440; m += 30) slots.push(minutesToTime(m));
    return slots;
  }

  function setupTimeGrid() {
    var grid = document.getElementById("time-grid");
    var summary = document.getElementById("time-summary");
    var selected = {};

    allSlots().forEach(function (slot) {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "time-slot";
      btn.dataset.time = slot;
      btn.textContent = formatTime(slot);
      grid.appendChild(btn);
    });

    function updateSummary() {
      var times = Object.keys(selected);
      summary.textContent = times.length ? formatTimes(times) : "No time selected";
    }

    grid.addEventListener("click", function (e) {
      var btn = e.target.closest(".time-slot");
      if (!btn) return;
      var slot = btn.dataset.time;
      if (selected[slot]) {
        delete selected[slot];
        btn.classList.remove("selected");
      } else {
        selected[slot] = true;
        btn.classList.add("selected");
      }
      updateSummary();
    });

    document.getElementById("time-clear").addEventListener("click", function () {
      reset();
    });

    function reset() {
      selected = {};
      grid.querySelectorAll(".time-slot.selected").forEach(function (b) { b.classList.remove("selected"); });
      updateSummary();
    }

    updateSummary();

    return {
      getSelected: function () { return Object.keys(selected); },
      reset: reset
    };
  }

  function setupPlanForm(timeGrid) {
    var form = document.getElementById("plan-form");
    var input = document.getElementById("plan-input");
    var categoryInput = document.getElementById("plan-category");
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var text = input.value.trim();
      if (!text) return;
      addTask(tomorrowKey(), text, timeGrid.getSelected(), categoryInput.value);
      input.value = "";
      categoryInput.value = "";
      timeGrid.reset();
      render();
    });
  }

  // Re-render on date rollover if the page is left open across midnight.
  function watchForDateChange() {
    var currentKey = todayKey();
    setInterval(function () {
      var key = todayKey();
      if (key !== currentKey) {
        currentKey = key;
        render();
      }
    }, 60000);
  }

  setupTabs();
  setupPlanForm(setupTimeGrid());
  watchForDateChange();
  render();
})();
