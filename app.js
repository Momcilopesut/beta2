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
    Personal: ["Call a friend or family member", "Tidy up a space", "Run an errand", "Free time / hobby"],
    Learning: ["Read for 30 minutes", "Practice a skill", "Watch a course lesson", "Journal / review notes"]
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
      svg: '<svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" xmlns="http://www.w3.org/2000/svg"><circle cx="32" cy="32" r="24"/><rect x="10" y="10" width="44" height="44"/><line x1="32" y1="8" x2="32" y2="56"/><line x1="8" y1="32" x2="56" y2="32"/></svg>'
    },
    {
      name: "Isaac Newton",
      idea: "Universal gravitation",
      svg: '<svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" xmlns="http://www.w3.org/2000/svg"><circle cx="26" cy="22" r="12"/><line x1="27" y1="10" x2="30" y2="4"/><path d="M8 46c8-10 40-10 48 0"/></svg>'
    },
    {
      name: "Marie Curie",
      idea: "Radioactivity",
      svg: '<svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" xmlns="http://www.w3.org/2000/svg"><circle cx="32" cy="32" r="3" fill="currentColor" stroke="none"/><ellipse cx="32" cy="32" rx="26" ry="10"/><ellipse cx="32" cy="32" rx="26" ry="10" transform="rotate(60 32 32)"/><ellipse cx="32" cy="32" rx="26" ry="10" transform="rotate(120 32 32)"/></svg>'
    },
    {
      name: "Nikola Tesla",
      idea: "Alternating current",
      svg: '<svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" xmlns="http://www.w3.org/2000/svg"><path d="M6 32c6-16 12-16 18 0s12 16 18 0 12-16 16 0"/></svg>'
    },
    {
      name: "Ada Lovelace",
      idea: "The first published algorithm",
      svg: '<svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" xmlns="http://www.w3.org/2000/svg"><path d="M20 24h16a10 10 0 0 1 0 20H24"/><path d="M30 38l-8 6 8 6"/></svg>'
    },
    {
      name: "Buckminster Fuller",
      idea: "The geodesic dome",
      svg: '<svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" xmlns="http://www.w3.org/2000/svg"><path d="M8 48a24 24 0 0 1 48 0"/><line x1="8" y1="48" x2="56" y2="48"/><line x1="20" y1="48" x2="32" y2="24"/><line x1="44" y1="48" x2="32" y2="24"/><line x1="14" y1="48" x2="32" y2="30"/><line x1="50" y1="48" x2="32" y2="30"/></svg>'
    },
    {
      name: "Frank Lloyd Wright",
      idea: "Organic architecture",
      svg: '<svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" xmlns="http://www.w3.org/2000/svg"><line x1="6" y1="22" x2="50" y2="22"/><line x1="14" y1="32" x2="58" y2="32"/><line x1="6" y1="42" x2="50" y2="42"/><line x1="20" y1="14" x2="20" y2="50"/></svg>'
    },
    {
      name: "Zaha Hadid",
      idea: "Fluid, curving architecture",
      svg: '<svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" xmlns="http://www.w3.org/2000/svg"><path d="M12 50c0-16 30-4 30-20s10-12 10-18"/></svg>'
    },
    {
      name: "Antoni Gaudi",
      idea: "The Sagrada Familia",
      svg: '<svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" xmlns="http://www.w3.org/2000/svg"><path d="M22 52V26l6-16 6 16v26"/><path d="M38 52V32l5-12 5 12v20"/><line x1="12" y1="52" x2="52" y2="52"/></svg>'
    },
    {
      name: "Imhotep",
      idea: "The step pyramid",
      svg: '<svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round" xmlns="http://www.w3.org/2000/svg"><rect x="8" y="42" width="48" height="8"/><rect x="14" y="34" width="36" height="8"/><rect x="20" y="26" width="24" height="8"/><rect x="26" y="18" width="12" height="8"/></svg>'
    },
    {
      name: "Euclid",
      idea: "Geometry, in The Elements",
      svg: '<svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" xmlns="http://www.w3.org/2000/svg"><path d="M32 8l-14 16M32 8l14 16"/><path d="M12 52h40l-20-28z"/></svg>'
    },
    {
      name: "Archimedes",
      idea: "The lever and buoyancy",
      svg: '<svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" xmlns="http://www.w3.org/2000/svg"><line x1="8" y1="28" x2="56" y2="40"/><path d="M32 34l-4 8h8z"/><circle cx="12" cy="24" r="4"/><circle cx="52" cy="44" r="6"/></svg>'
    },
    {
      name: "Charles Darwin",
      idea: "Evolution by natural selection",
      svg: '<svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" xmlns="http://www.w3.org/2000/svg"><path d="M32 56V32M32 32L18 16M32 32l14-16M32 40L14 28M32 40l18-12"/></svg>'
    },
    {
      name: "Alan Turing",
      idea: "The Turing machine",
      svg: '<svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" xmlns="http://www.w3.org/2000/svg"><rect x="8" y="30" width="48" height="12"/><line x1="20" y1="30" x2="20" y2="42"/><line x1="32" y1="30" x2="32" y2="42"/><line x1="44" y1="30" x2="44" y2="42"/><path d="M32 30v-8"/><rect x="26" y="14" width="12" height="8"/></svg>'
    },
    {
      name: "Pythagoras",
      idea: "The Pythagorean theorem",
      svg: '<svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round" xmlns="http://www.w3.org/2000/svg"><path d="M12 12L12 52L52 52Z"/><rect x="3" y="12" width="8" height="40"/><rect x="12" y="53" width="40" height="8"/></svg>'
    },
    {
      name: "Galileo Galilei",
      idea: "Heliocentrism",
      svg: '<svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2" xmlns="http://www.w3.org/2000/svg"><circle cx="32" cy="32" r="6" fill="currentColor" stroke="none"/><ellipse cx="32" cy="32" rx="26" ry="12"/><circle cx="58" cy="32" r="3" fill="currentColor" stroke="none"/></svg>'
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

  // Rotates the featured figure and quote once per day, deterministically.
  function renderInspiration() {
    var day = dayOfYear(new Date());
    var figure = FIGURES[day % FIGURES.length];
    var quote = QUOTES[day % QUOTES.length];

    document.getElementById("figure-art").innerHTML = figure.svg;
    document.getElementById("figure-caption").innerHTML = "<strong>" + figure.name + "</strong> — " + figure.idea;
    document.getElementById("quote-text").textContent = "“" + quote.text + "”";
    document.getElementById("quote-author").textContent = "— " + quote.author;
  }

  var timeGrid = setupTimeGrid();
  setupTabs();
  setupPlanForm(setupCategoryPicker());
  watchForDateChange();
  render();
  renderInspiration();
})();
