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
      done: false,
      feedback: null
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

  var FEEDBACK_OPTIONS = [
    { value: "up", label: "👍" },
    { value: "down", label: "👎" },
    { value: "smile", label: "🙂" }
  ];

  function setFeedback(key, id, value) {
    var tasks = getTasks(key);
    var task = tasks.find(function (t) { return t.id === id; });
    if (!task) return;
    task.feedback = task.feedback === value ? null : value;
    save(data);
    recordFeedback(key, task);
    return task.feedback;
  }

  // Hook for the hosted version to log feedback to a database; the
  // plain downloadable app has no server, so this is a no-op here.
  function recordFeedback(key, task) {}

  function renderList(listEl, emptyEl, key, showCheckbox) {
    var tasks = sortedTasks(key);
    listEl.innerHTML = "";
    emptyEl.style.display = tasks.length === 0 ? "block" : "none";

    tasks.forEach(function (task) {
      var li = document.createElement("li");
      li.className = "task-item" + (task.done ? " done" : "");

      var row = document.createElement("div");
      row.className = "task-row";

      if (showCheckbox) {
        var checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.checked = task.done;
        checkbox.addEventListener("change", function () {
          toggleTask(key, task.id);
          render();
        });
        row.appendChild(checkbox);
      }

      var timeEl = document.createElement("span");
      timeEl.className = "task-time";
      timeEl.textContent = formatTimes(task.times);
      row.appendChild(timeEl);

      if (task.category) {
        var categoryEl = document.createElement("span");
        categoryEl.className = "task-category";
        categoryEl.textContent = task.category;
        row.appendChild(categoryEl);
      }

      var span = document.createElement("span");
      span.className = "task-text";
      span.textContent = task.text;
      row.appendChild(span);

      var del = document.createElement("button");
      del.className = "task-delete";
      del.textContent = "remove";
      del.addEventListener("click", function () {
        deleteTask(key, task.id);
        render();
      });
      row.appendChild(del);

      li.appendChild(row);

      var feedback = document.createElement("div");
      feedback.className = "task-feedback";
      FEEDBACK_OPTIONS.forEach(function (opt) {
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "feedback-btn" + (task.feedback === opt.value ? " selected" : "");
        btn.textContent = opt.label;
        btn.setAttribute("aria-label", opt.value);
        btn.addEventListener("click", function () {
          setFeedback(key, task.id, opt.value);
          render();
        });
        feedback.appendChild(btn);
      });
      li.appendChild(feedback);

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

    if (timeGrid) timeGrid.refresh();
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

  function takenSlots() {
    var taken = {};
    getTasks(tomorrowKey()).forEach(function (task) {
      (task.times || []).forEach(function (t) { taken[t] = true; });
    });
    return taken;
  }

  function setupTimeGrid() {
    var grid = document.getElementById("time-grid");
    var summary = document.getElementById("time-summary");
    var selected = {};
    var lastKey = tomorrowKey();

    function updateSummary() {
      var times = Object.keys(selected);
      summary.textContent = times.length ? formatTimes(times) : "No time selected";
    }

    // Rebuilds the grid from scratch, leaving out any slot a task for
    // tomorrow already occupies.
    function renderGrid() {
      var currentKey = tomorrowKey();
      if (currentKey !== lastKey) {
        lastKey = currentKey;
        selected = {};
      }
      var taken = takenSlots();
      grid.innerHTML = "";
      allSlots().forEach(function (slot) {
        if (taken[slot]) {
          delete selected[slot];
          return;
        }
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "time-slot" + (selected[slot] ? " selected" : "");
        btn.dataset.time = slot;
        btn.textContent = formatTime(slot);
        grid.appendChild(btn);
      });
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
      renderGrid();
      updateSummary();
    }

    renderGrid();
    updateSummary();

    return {
      getSelected: function () { return Object.keys(selected); },
      reset: reset,
      refresh: function () {
        renderGrid();
        updateSummary();
      }
    };
  }

  var SUGGESTIONS = {
    Work: ["Deep work block", "Respond to emails", "Team check-in", "Plan tomorrow's priorities"],
    Health: ["Workout", "Prep a healthy meal", "Stretch / mobility", "Get to bed on time"],
    Chores: ["Do laundry", "Grocery shopping", "Clean the house", "Wash the dishes"],
    Learning: ["Reading", "Practice a skill", "Watch a course lesson", "Journal / review notes"]
  };
  var OTHER_VALUE = "__other__";

  // Once a category is picked, offers 4 suggested tasks for it plus
  // "Other"; picking a suggestion fills the task name for you.
  function setupCategoryPicker() {
    var categorySelect = document.getElementById("plan-category");
    var suggestionSelect = document.getElementById("plan-suggestion");
    var textInput = document.getElementById("plan-input");

    function populate() {
      var options = SUGGESTIONS[categorySelect.value];
      suggestionSelect.innerHTML = "";
      if (!options) {
        suggestionSelect.hidden = true;
        return;
      }
      options.forEach(function (text) {
        var opt = document.createElement("option");
        opt.value = text;
        opt.textContent = text;
        suggestionSelect.appendChild(opt);
      });
      var other = document.createElement("option");
      other.value = OTHER_VALUE;
      other.textContent = "Other";
      suggestionSelect.appendChild(other);
      suggestionSelect.hidden = false;
      suggestionSelect.value = options[0];
      textInput.value = options[0];
    }

    categorySelect.addEventListener("change", populate);

    suggestionSelect.addEventListener("change", function () {
      if (suggestionSelect.value === OTHER_VALUE) {
        textInput.value = "";
        textInput.focus();
      } else {
        textInput.value = suggestionSelect.value;
      }
    });

    return {
      reset: function () {
        categorySelect.value = "";
        suggestionSelect.hidden = true;
        suggestionSelect.innerHTML = "";
      }
    };
  }

  function setupPlanForm(categoryPicker) {
    var form = document.getElementById("plan-form");
    var input = document.getElementById("plan-input");
    var categoryInput = document.getElementById("plan-category");
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var text = input.value.trim();
      if (!text) return;
      addTask(tomorrowKey(), text, timeGrid.getSelected(), categoryInput.value);
      input.value = "";
      categoryPicker.reset();
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
        renderInspiration();
      }
    }, 60000);
  }

  var FIGURES = [
    {
      name: "Leonardo da Vinci",
      idea: "Vitruvian Man, the proportions of the human body",
      svg: '<svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" xmlns="http://www.w3.org/2000/svg"><circle cx="32" cy="32" r="24"/><rect x="10" y="10" width="44" height="44"/><line x1="32" y1="8" x2="32" y2="56"/><line x1="8" y1="32" x2="56" y2="32"/><line x1="14" y1="14" x2="50" y2="50" stroke-width="1.3"/><line x1="50" y1="14" x2="14" y2="50" stroke-width="1.3"/><circle cx="32" cy="32" r="2" fill="currentColor" stroke="none"/></svg>'
    },
    {
      name: "Michelangelo",
      idea: "The Sistine Chapel ceiling",
      svg: '<svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" xmlns="http://www.w3.org/2000/svg"><path d="M4 16a34 20 0 0 1 56 0" stroke-width="1.3"/><path d="M8 20c10 4 16 10 20 16"/><path d="M56 44c-10-4-16-10-20-16"/><line x1="31" y1="31" x2="34" y2="28" stroke-width="1.3"/></svg>'
    },
    {
      name: "Vincent van Gogh",
      idea: "The Starry Night",
      svg: '<svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" xmlns="http://www.w3.org/2000/svg"><path d="M38 20c6 2 8 8 4 12s-12 2-12-4 8-10 14-6-2 16-10 14-12-10-6-18"/><path d="M14 6a7 7 0 1 0 0 14A5.5 5.5 0 1 1 14 6Z" stroke-width="1.3"/><circle cx="52" cy="10" r="1.3" fill="currentColor" stroke="none"/><circle cx="58" cy="18" r="1" fill="currentColor" stroke="none"/><line x1="4" y1="52" x2="60" y2="52" stroke-width="1.3"/><path d="M12 52V32c3 0 3 6 0 10s3 6 0 10" stroke-width="1.3"/></svg>'
    },
    {
      name: "Pablo Picasso",
      idea: "Cubism",
      svg: '<svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round" xmlns="http://www.w3.org/2000/svg"><path d="M16 44V20l16-8 16 8v24l-16 8z"/><line x1="16" y1="20" x2="32" y2="32"/><line x1="48" y1="20" x2="32" y2="32"/><line x1="32" y1="32" x2="32" y2="52"/><line x1="20" y1="26" x2="26" y2="30" stroke-width="1.3"/><line x1="44" y1="26" x2="38" y2="30" stroke-width="1.3"/><line x1="16" y1="44" x2="32" y2="52" stroke-width="1.3"/></svg>'
    },
    {
      name: "Claude Monet",
      idea: "Water Lilies, and Impressionism",
      svg: '<svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2" xmlns="http://www.w3.org/2000/svg"><ellipse cx="24" cy="30" rx="10" ry="5"/><ellipse cx="42" cy="38" rx="9" ry="4.5"/><circle cx="24" cy="30" r="1.4" fill="currentColor" stroke="none"/><circle cx="42" cy="38" r="1.4" fill="currentColor" stroke="none"/><path d="M6 46c8 4 40 4 52 0" stroke-width="1.3"/><path d="M10 52c8 3 32 3 40 0" stroke-width="1.3"/></svg>'
    },
    {
      name: "M.C. Escher",
      idea: "Impossible constructions and tessellation",
      svg: '<svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round" xmlns="http://www.w3.org/2000/svg"><path d="M32 6l22 38H10z"/><path d="M32 22l14 24H18z"/><path d="M32 38l6 10H26z"/><line x1="18" y1="44" x2="46" y2="44" stroke-width="1.2"/></svg>'
    },
    {
      name: "Katsushika Hokusai",
      idea: "The Great Wave",
      svg: '<svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" xmlns="http://www.w3.org/2000/svg"><path d="M4 42c10-2 14-10 10-16-3-4-9-3-9 2 0 6 8 8 14 4"/><circle cx="8" cy="30" r="1" fill="currentColor" stroke="none"/><circle cx="14" cy="26" r="1" fill="currentColor" stroke="none"/><path d="M40 50l6-3 6 3" stroke-width="1.3"/><path d="M4 50c14 4 34 4 48-2"/></svg>'
    },
    {
      name: "Buckminster Fuller",
      idea: "The geodesic dome",
      svg: '<svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" xmlns="http://www.w3.org/2000/svg"><path d="M6 48a26 22 0 0 1 52 0"/><line x1="6" y1="48" x2="58" y2="48"/><line x1="16" y1="48" x2="32" y2="26"/><line x1="48" y1="48" x2="32" y2="26"/><line x1="24" y1="48" x2="32" y2="33"/><line x1="40" y1="48" x2="32" y2="33"/><line x1="12" y1="48" x2="26" y2="34" stroke-width="1.3"/><line x1="52" y1="48" x2="38" y2="34" stroke-width="1.3"/></svg>'
    },
    {
      name: "Frank Lloyd Wright",
      idea: "Organic architecture",
      svg: '<svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" xmlns="http://www.w3.org/2000/svg"><line x1="4" y1="20" x2="48" y2="20"/><line x1="12" y1="30" x2="58" y2="30"/><line x1="4" y1="40" x2="48" y2="40"/><line x1="18" y1="12" x2="18" y2="46"/><path d="M30 44c1 4-1 6 0 10" stroke-width="1.3"/><path d="M36 44c1 4-1 6 0 10" stroke-width="1.3"/></svg>'
    },
    {
      name: "Antoni Gaudi",
      idea: "The Sagrada Familia",
      svg: '<svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" xmlns="http://www.w3.org/2000/svg"><path d="M18 52V32l4-12 4 12v20"/><path d="M32 52V24l6-16 6 16v28"/><path d="M48 52V34l4-10 4 10v18"/><line x1="8" y1="52" x2="58" y2="52"/><circle cx="38" cy="8" r="1.5" fill="currentColor" stroke="none"/></svg>'
    },
    {
      name: "Imhotep",
      idea: "The step pyramid",
      svg: '<svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round" xmlns="http://www.w3.org/2000/svg"><rect x="6" y="46" width="52" height="7"/><rect x="12" y="39" width="40" height="7"/><rect x="18" y="32" width="28" height="7"/><rect x="24" y="25" width="16" height="7"/><rect x="28" y="18" width="8" height="7"/><rect x="29" y="46" width="6" height="7" stroke-width="1.3"/></svg>'
    },
    {
      name: "I.M. Pei",
      idea: "The Louvre Pyramid",
      svg: '<svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round" xmlns="http://www.w3.org/2000/svg"><path d="M32 10L6 50h52z"/><line x1="32" y1="10" x2="32" y2="50"/><line x1="18" y1="50" x2="32" y2="25"/><line x1="46" y1="50" x2="32" y2="25"/><path d="M8 50l4-6 4 6z" stroke-width="1.3"/><path d="M48 50l4-6 4 6z" stroke-width="1.3"/></svg>'
    },
    {
      name: "Le Corbusier",
      idea: "Villa Savoye, architecture on stilts",
      svg: '<svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round" xmlns="http://www.w3.org/2000/svg"><rect x="8" y="18" width="48" height="18"/><line x1="8" y1="27" x2="56" y2="27" stroke-width="1.3"/><line x1="14" y1="36" x2="14" y2="54"/><line x1="32" y1="36" x2="32" y2="54"/><line x1="50" y1="36" x2="50" y2="54"/><line x1="4" y1="54" x2="60" y2="54" stroke-width="1.3"/></svg>'
    },
    {
      name: "Eero Saarinen",
      idea: "The Gateway Arch",
      svg: '<svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" xmlns="http://www.w3.org/2000/svg"><path d="M12 54C12 26 22 12 32 12s20 14 20 42"/><line x1="4" y1="54" x2="60" y2="54"/><line x1="46" y1="54" x2="46" y2="46" stroke-width="1.3"/><line x1="52" y1="54" x2="52" y2="42" stroke-width="1.3"/></svg>'
    },
    {
      name: "Isaac Newton",
      idea: "Universal gravitation",
      svg: '<svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" xmlns="http://www.w3.org/2000/svg"><line x1="8" y1="6" x2="30" y2="10" stroke-width="1.3"/><circle cx="26" cy="22" r="12"/><line x1="27" y1="10" x2="29" y2="5"/><path d="M8 46c8-10 40-10 48 0"/><circle cx="14" cy="44" r="1" fill="currentColor" stroke="none"/><circle cx="50" cy="44" r="1" fill="currentColor" stroke="none"/></svg>'
    },
    {
      name: "Marie Curie",
      idea: "Radioactivity",
      svg: '<svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" xmlns="http://www.w3.org/2000/svg"><circle cx="32" cy="32" r="3" fill="currentColor" stroke="none"/><ellipse cx="32" cy="32" rx="26" ry="10"/><ellipse cx="32" cy="32" rx="26" ry="10" transform="rotate(60 32 32)"/><ellipse cx="32" cy="32" rx="26" ry="10" transform="rotate(120 32 32)"/><line x1="32" y1="4" x2="32" y2="9" stroke-width="1.3"/><line x1="32" y1="55" x2="32" y2="60" stroke-width="1.3"/><line x1="4" y1="32" x2="9" y2="32" stroke-width="1.3"/></svg>'
    },
    {
      name: "Nikola Tesla",
      idea: "Alternating current",
      svg: '<svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" xmlns="http://www.w3.org/2000/svg"><path d="M4 28c6-16 12-16 18 0s12 16 18 0 12-16 16 0"/><path d="M4 40c6-12 12-12 18 0s12 12 18 0 12-12 16 0" stroke-width="1.3"/><circle cx="4" cy="28" r="1.5" fill="currentColor" stroke="none"/><circle cx="60" cy="28" r="1.5" fill="currentColor" stroke="none"/></svg>'
    },
    {
      name: "Charles Darwin",
      idea: "Evolution by natural selection",
      svg: '<svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" xmlns="http://www.w3.org/2000/svg"><path d="M32 58V36M32 36L16 18M32 36l16-18M32 44L12 30M32 44l20-14M24 27L18 20M40 27l6-7"/><circle cx="16" cy="18" r="1.3" fill="currentColor" stroke="none"/><circle cx="48" cy="18" r="1.3" fill="currentColor" stroke="none"/><circle cx="12" cy="30" r="1.3" fill="currentColor" stroke="none"/><circle cx="52" cy="30" r="1.3" fill="currentColor" stroke="none"/></svg>'
    },
    {
      name: "Alan Turing",
      idea: "The Turing machine",
      svg: '<svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" xmlns="http://www.w3.org/2000/svg"><rect x="4" y="32" width="56" height="12"/><line x1="14" y1="32" x2="14" y2="44"/><line x1="24" y1="32" x2="24" y2="44"/><line x1="34" y1="32" x2="34" y2="44"/><line x1="44" y1="32" x2="44" y2="44"/><line x1="54" y1="32" x2="54" y2="44"/><circle cx="19" cy="38" r="1" fill="currentColor" stroke="none"/><circle cx="39" cy="38" r="1" fill="currentColor" stroke="none"/><path d="M34 32v-8" stroke-width="1.3"/><rect x="28" y="16" width="12" height="8" stroke-width="1.3"/></svg>'
    },
    {
      name: "Pythagoras",
      idea: "The Pythagorean theorem",
      svg: '<svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round" xmlns="http://www.w3.org/2000/svg"><path d="M12 12L12 52L52 52Z"/><rect x="3" y="12" width="9" height="40"/><rect x="12" y="53" width="40" height="9"/><polygon points="12,12 26.1,26.1 40.3,12 26.1,-2.1" stroke-width="1.3"/></svg>'
    },
    {
      name: "Galileo Galilei",
      idea: "Heliocentrism",
      svg: '<svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2" xmlns="http://www.w3.org/2000/svg"><circle cx="32" cy="32" r="6" fill="currentColor" stroke="none"/><ellipse cx="32" cy="32" rx="18" ry="9"/><ellipse cx="32" cy="32" rx="27" ry="13" stroke-width="1.3"/><circle cx="49" cy="32" r="2" fill="currentColor" stroke="none"/><circle cx="14" cy="32" r="1.5" fill="currentColor" stroke="none"/></svg>'
    },
    {
      name: "Albert Einstein",
      idea: "The theory of relativity",
      svg: '<svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2" xmlns="http://www.w3.org/2000/svg"><path d="M4 18c12 4 18 4 26 0s16-4 26 0"/><path d="M4 26c12 5 18 5 26 0s16-5 26 0" stroke-width="1.3"/><path d="M4 32c12 6 18 6 26 0s16-6 26 0"/><path d="M4 38c12 5 18 5 26 0s16-5 26 0" stroke-width="1.3"/><path d="M4 46c12 4 18 4 26 0s16-4 26 0"/><circle cx="32" cy="32" r="4" fill="currentColor" stroke="none"/></svg>'
    },
    {
      name: "Rosalind Franklin",
      idea: "The structure of DNA",
      svg: '<svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2" xmlns="http://www.w3.org/2000/svg"><path d="M20 4c-8 8-8 16 0 24s8 16 0 24"/><path d="M44 4c8 8 8 16 0 24s-8 16 0 24"/><line x1="20" y1="10" x2="44" y2="10"/><line x1="18" y1="18" x2="46" y2="18" stroke-width="1.3"/><line x1="20" y1="28" x2="44" y2="28"/><line x1="18" y1="38" x2="46" y2="38" stroke-width="1.3"/><line x1="20" y1="46" x2="44" y2="46"/></svg>'
    },
    {
      name: "Louis Pasteur",
      idea: "Germ theory",
      svg: '<svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" xmlns="http://www.w3.org/2000/svg"><path d="M26 8h12v14l12 24a4 4 0 0 1-4 6H18a4 4 0 0 1-4-6l12-24z"/><line x1="24" y1="8" x2="40" y2="8"/><rect x="27" y="3" width="10" height="5" stroke-width="1.3"/><line x1="20" y1="40" x2="44" y2="40"/><circle cx="28" cy="46" r="1.3" fill="currentColor" stroke="none"/><circle cx="36" cy="49" r="1" fill="currentColor" stroke="none"/></svg>'
    },
    {
      name: "Michael Faraday",
      idea: "Electromagnetic induction",
      svg: '<svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" xmlns="http://www.w3.org/2000/svg"><path d="M18 8v48"/><path d="M18 8c7 0 7 7 0 7s-7 7 0 7 7 7 0 7 7 7 0 7 7 7 0 7"/><path d="M38 18c5 3 5 6 0 9" stroke-width="1.3"/><path d="M43 14c8 5 8 12 0 17"/><path d="M48 10c11 7 11 16 0 23" stroke-width="1.3"/></svg>'
    }
  ];

  var QUOTES = [
    { text: "By failing to prepare, you are preparing to fail.", author: "Benjamin Franklin" },
    { text: "An investment in knowledge pays the best interest.", author: "Benjamin Franklin" },
    { text: "Well done is better than well said.", author: "Benjamin Franklin" },
    { text: "Energy and persistence conquer all things.", author: "Benjamin Franklin" },
    { text: "Lost time is never found again.", author: "Benjamin Franklin" },
    { text: "The best investment you can make is in yourself.", author: "Warren Buffett" },
    { text: "It takes 20 years to build a reputation and five minutes to ruin it.", author: "Warren Buffett" },
    { text: "Risk comes from not knowing what you're doing.", author: "Warren Buffett" },
    { text: "Someone's sitting in the shade today because someone planted a tree a long time ago.", author: "Warren Buffett" },
    { text: "The big money is not in the buying and the selling, but in the waiting.", author: "Charlie Munger" },
    { text: "Spend each day trying to be a little wiser than you were when you woke up.", author: "Charlie Munger" },
    { text: "Knowing what you don't know is more useful than being brilliant.", author: "Charlie Munger" },
    { text: "You have power over your mind, not outside events. Realize this, and you will find strength.", author: "Marcus Aurelius" },
    { text: "We suffer more in imagination than in reality.", author: "Seneca" },
    { text: "Efficiency is doing things right; effectiveness is doing the right things.", author: "Peter Drucker" }
  ];

  function dayOfYear(d) {
    var start = new Date(d.getFullYear(), 0, 0);
    return Math.floor((d - start) / 86400000);
  }

  // Rotates the quote once per day, deterministically.
  function renderInspiration() {
    var day = dayOfYear(new Date());
    var quote = QUOTES[day % QUOTES.length];
    document.getElementById("quote-text").textContent = "“" + quote.text + "”";
    document.getElementById("quote-author").textContent = "— " + quote.author;
  }

  // Builds one repeating tile scattering every figure's icon across a
  // grid with slight jitter and rotation, as a single background-image
  // data URI so it tiles cheaply at any screen size.
  function buildWallpaperTile() {
    var cols = 5, rows = 5, cellSize = 170, tile = cols * cellSize;
    var iconSize = 72;
    var scale = iconSize / 64;
    var color = "#7a7a7a";
    var cellCount = cols * rows;
    var parts = [];

    for (var i = 0; i < cellCount; i++) {
      var figure = FIGURES[i % FIGURES.length];
      var col = i % cols;
      var row = Math.floor(i / cols);
      var cx = col * cellSize + cellSize / 2;
      var cy = row * cellSize + cellSize / 2;
      var jitterX = ((i * 17) % 19) - 9;
      var jitterY = ((i * 29) % 19) - 9;
      var rot = ((i * 37) % 29) - 14;
      // Rotate around the icon's own center (32,32) before scaling and
      // placing it, so the shape stays rigid instead of swinging around
      // an off-center pivot.
      var tx = cx + jitterX - 32 * scale;
      var ty = cy + jitterY - 32 * scale;

      var inner = figure.svg
        .replace(/currentColor/g, color)
        .replace(/^<svg[^>]*>/, "")
        .replace(/<\/svg>$/, "");

      var g = '<g transform="translate(' + tx + " " + ty + ") scale(" + scale + ") rotate(" + rot + ' 32 32)" ' +
        'fill="none" stroke="' + color + '" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
        inner + "</g>";
      parts.push(g);
    }

    var full = '<svg xmlns="http://www.w3.org/2000/svg" width="' + tile + '" height="' + tile +
      '" viewBox="0 0 ' + tile + " " + tile + '">' + parts.join("") + "</svg>";
    return "data:image/svg+xml," + encodeURIComponent(full);
  }

  function setupWallpaper() {
    var el = document.getElementById("art-wallpaper");
    el.style.backgroundImage = "url('" + buildWallpaperTile() + "')";
  }

  var timeGrid = setupTimeGrid();
  setupWallpaper();
  setupTabs();
  setupPlanForm(setupCategoryPicker());
  watchForDateChange();
  render();
  renderInspiration();
})();
